# (c) Copyright IBM Corporation 2016
# LICENSE: BSD-3, https://opensource.org/licenses/BSD-3-Clause

import os, subprocess
from shutil import make_archive
from tornado import gen
from .sparkapp_bundler import *
from . import SPARKAPP_LOG


HOME = os.getenv('HOME')
APPDIR = HOME + '/projects/sparkapp'
SOURCEFILE = APPDIR + '/src/main/scala/notebook.scala'

DASHDBHOST = os.environ.get('DASHDBHOST') or 'localhost'
DASHDBUSER = os.environ.get('DASHDBUSER')


@gen.coroutine
def bundle(handler, absolute_notebook_path):
    '''
    Converts the notebook into a Spark-Scala app, compiles it and uploads the
    application JAR file to the dashDB target server

    :param handler: The tornado.web.RequestHandler that serviced the request
    :param absolute_notebook_path: The path of the notebook on disk
    '''

    notebook_filename = os.path.splitext(os.path.basename(absolute_notebook_path))[0]

    handler.set_header('Content-Type', 'text/plain; charset=us-ascii ')
    handler.write("Building scala application...\n")
    handler.flush()
    deps = export_to_scalafile(absolute_notebook_path, SOURCEFILE)
    jarfile = build_scala_project(handler, APPDIR, SOURCEFILE, notebook_filename, deps)
    if not jarfile: return

    upload = subprocess.run(["upload-sparkapp.py", jarfile],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if (upload.returncode != 0):
        handler.write("Failed!\n\n")
        handler.write(upload.stdout)
        handler.finish()
        return

    resource = os.path.basename(jarfile)
    handler.write("Successfully uploaded {0} to {1}!\n\n".format(resource, DASHDBHOST))
    SPARKAPP_LOG.info("Upload output: %s", upload.stdout)

    submit_commands = [
        "export DASHDBURL=https://{0}:8443".format(DASHDBHOST),
        "export DASHDBUSER={0}".format(DASHDBUSER),
        "spark-submit.sh {0} --class SampleApp".format(resource),
    ]

    handler.write("\n\nTo run your spark application, use one of the following alternatives:\n\n\n")
    handler.write("- Submit via dashDB's spark-submit.sh command line tool. Run the following sequence of commands:\n\n")
    for cmd in submit_commands:
        handler.write("  {0}\n".format(cmd))
    handler.write("\n")
    handler.write("- Submit via dashDB's REST API, e.g using cURL: Set the shell variable DASHDBPASS to the dashDB password for {1}\n"
                  "  and run the following command:\n\n"
                  "curl -k -u {1}:$DASHDBPASS -XPOST https://{0}:8443/dashdb-api/analytics/public/apps/submit \\\n"
                  "  --header 'Content-Type:application/json;charset=UTF-8'  \\\n"
                  "  --data '{{ \"appResource\" : \"{2}\", \"mainClass\" : \"SampleApp\" }}'\n"
                  .format(DASHDBHOST, DASHDBUSER, resource))
    handler.write("\n- Submit via stored procedure in dashDB. Connect to the dashDB database on {0} as user {1} and then run:\n\n"
                  "  CALL IDAX.SPARK_SUBMIT(?, '{{ \"appResource\" : \"{2}\", \"mainClass\" : \"SampleApp\"}}')\n"
                  .format(DASHDBHOST, DASHDBUSER, resource))
    handler.finish()

    # for testing: return the submit commands
    return submit_commands

