# (c) Copyright IBM Corporation 2016
# LICENSE: BSD-3, https://opensource.org/licenses/BSD-3-Clause

import warnings, os, io, glob, json, re, subprocess
from nbconvert import TemplateExporter, preprocessors
from jinja2 import FileSystemLoader
from . import SPARKAPP_LOG

# path for looking up jinja2 template
INSTALLDIR = os.path.dirname(os.path.realpath(__file__))

# marker indicating code cells that should not be added to the Spark application
FILTER_CELL_MARKER = "//NOT-FOR-APP"

def export_to_scala(absolute_notebook_path):
    '''convert the notebook source to scala'''
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=DeprecationWarning)
        exporter = TemplateExporter(extra_loaders=[FileSystemLoader(INSTALLDIR)],
                                    preprocessors=[ScalaAppPreprocessor])
        exporter.template_file = 'scala_sparkapp'
        return exporter.from_file(absolute_notebook_path)


def export_to_scalafile(absolute_notebook_path, scala_source):
    '''
    convert the notebook source to scala and save it into the given filename.
    return a list of maven/sbt dependencies defined via %AddDeps in the notebook
    '''

    SPARKAPP_LOG.info("Exporting noteboook to %s", scala_source)
    (scalacode, resources) = export_to_scala(absolute_notebook_path)
    with open(scala_source, 'wt') as sourcefile:
        sourcefile.write(scalacode)
    return resources['mvn_deps']

def format_dependency(dep):
    quoted_components = map(lambda x: '"{0}"'.format(x), dep.split())
    return " % ".join(quoted_components) + ",\n"

def build_scala_project(handler, project_dir, scalafile, appname, dependencies = []):
    '''build the given scala project, replacing the <appname> tag in build.sbt.template
    with the given application name.
    If the build fails, display the build output via the given tornado handler.
    Return the name of the generated JAR'''

    dependencyString = ""
    for dep in dependencies:
        dependencyString += format_dependency(dep)
    SPARKAPP_LOG.info("Building scala application in %s with dependencies %s", 
                      project_dir, dependencyString)
    with open(project_dir+"/../build.sbt.template", "rt") as buildfile_in:
        with open(project_dir+"/build.sbt", "wt") as buildfile_out:
            for line in buildfile_in:
                buildfile_out.write(line
                                    .replace('<appname>', appname)
                                    .replace('// <dependencies>', dependencyString))
    build = subprocess.run(["./build.sh"], cwd=project_dir,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if (build.returncode != 0):
        show_build_error(handler, build.stdout, scalafile)
        return None

    jars = glob.glob(project_dir + "/target/**/*.jar")
    assert len(jars) == 1, "Expected exactly one output JAR bout found {0}".format(','.join(jars))
    SPARKAPP_LOG.info("Created jar file %s", jars[0])

    return jars[0]


def show_build_error(handler, errmsg, scalafile):
    handler.set_header('Content-Type', 'text/plain; charset=us-ascii ')
    handler.write("SBT build failed!\n\n")
    for line in errmsg.splitlines(True):
        # strip all the dependency resolution info from the error output
        if not line.startswith(b"[info] Resolving "):
            handler.write(line)
    handler.write("\n\nScala source generated from notebook:\n\n")
    with open(scalafile, "rt") as source:
        for (n, line) in enumerate(source, 1):
            handler.write("{0:>5}:  {1}".format(n, line))
    handler.finish()


def add_launcher_scripts(project_dir, jarfile, appname):
    DASHDBHOST = os.environ.get('DASHDBHOST')
    DASHDBUSER = os.environ.get('DASHDBUSER')

    scriptfile = "{0}/settings.sh".format(project_dir)
    with open(scriptfile, "wt") as script:
        script.write("export DASHDBURL=https://{0}:8443\n".format(DASHDBHOST))
        script.write("export DASHDBUSER={0}\n".format(DASHDBUSER))
        script.write("#export DASHDBPASS=<set passsword>\n")
        script.write("echo 'Edit settings.sh and set DAHSDBPASS'\n")

    scriptfile = "{0}/upload_{1}.sh".format(project_dir, appname)
    with open(scriptfile, "wt") as script:
        url = "$DASHDBURL/dashdb-api/home/spark/apps"
        script.write("#!/bin/sh\n")
        script.write(". ./settings.sh\n")
        script.write("header=Content-Type:multipart/form-data\n")
        script.write("curl -k -u $DASHDBUSER:$DASHDBPASS -XPOST -H $header -F data=@{0} {1}\n"
                     .format(jarfile, url))
    os.chmod(scriptfile, 0o755)

    resource = os.path.basename(jarfile)
    scriptfile = "{0}/submit_{1}.sh".format(project_dir, appname)
    with open(scriptfile, "wt") as script:
        submit_spec = { "appResource" : resource, "mainClass" : "SampleApp" }
        url = "$DASHDBURL/dashdb-api/analytics/public/apps/submit"
        script.write("#!/bin/sh\n")
        script.write(". ./settings.sh\n")
        script.write("header=Content-Type:application/json;charset=UTF-8\n")
        script.write("curl -k -v -u $DASHDBUSER:$DASHDBPASS -XPOST -H $header --data '{0}' {1}\n"
             .format(json.dumps(submit_spec), url))
    os.chmod(scriptfile, 0o755)


class ScalaAppPreprocessor(preprocessors.Preprocessor):
    """A preprocessor to handle Spark/Scala applications written as notebooks"""

    def keepCell(self, cell):
        """filter out cells marked by the user and cell magics"""
        return (not cell.source.startswith(FILTER_CELL_MARKER)
            and not cell.source.startswith('%%'))

    def processCode(self, source, resources):
        """filter out line magics. collect maven dependencies from %AddDeps into the notebook resources"""
        # process %AddDeps and add to resources
        for match in re.finditer(r"^(//)?%AddDeps\s+(.+?)$", source, re.MULTILINE):
            dep = match.group(2)
            if (match.group(1) == "//"):
                # treat dependencies that are commented out as "provided" deps, which
                # will be used for compile but not added into the .jar assembly
                # this is for deps that are pre-installed in dashDB via globallibs etc.
                dep += " provided"
            resources.setdefault('mvn_deps', []).append(dep)
        # comment all line magics 
        return re.sub(r"^(%.*?)$", r"//\1", source, 0, re.MULTILINE)
        
    def processCell(self, cell, resources):
        if (cell.cell_type == 'code'):
            cell.source = self.processCode(cell.source, resources)
        return cell

    def preprocess(self, nb, resources):
        nb.cells = [self.processCell(cell, resources) for cell in nb.cells if self.keepCell(cell)]
        return nb, resources
