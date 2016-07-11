# encoding: utf-8

import csv
import json
import os.path, sys

from gevent import wsgi, subprocess
from flask import Flask, Response, request
from locust import __version__ as version

import gevent
import logging
import shlex

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.debug = True
app.root_path = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = 'uploads'

if not os.path.exists('uploads'):
    os.makedirs('uploads')

locust_process = None
locust_process_poll = None
locust_args = ""

@app.route('/boot', methods=["POST"])
def boot():
    global locust_process # we need write access to this variable
    global locust_process_poll

    if (locust_process != None):
        return Response(json.dumps({'message': "Locust already running"}), status=400, mimetype='application/json')

    filename = 'locustfile.py'
    
    # check if the post request has the file part
    if (filename not in request.files):
        return Response(json.dumps({'message': "Request did not contain a locustfile.py"}), status=400, mimetype='application/json')

    host = request.form["host"]
    if (not host):
        return Response(json.dumps({'message': "Request did not contain a target host"}), status=400, mimetype='application/json')

    file = request.files[filename]
    locustfile = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # ensure the path is clear
    try:
        os.remove(filename)
    except OSError: 
        pass

    file.save(locustfile)
    
    args = ["locust"]
    args.extend(shlex.split(locust_args))
    args.extend(["-f", locustfile])
    args.extend(["--host", host])

    locust_process = subprocess.Popen(args, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.getcwd())
    # start a greenlet to read stdout from process
    def poll():
        while True:
            s = locust_process.stdout.readline()
            if s == "": # EOF, process exited
                break
            else:
                logger.info(s.strip())

    locust_process_poll = gevent.spawn(poll)
     
    return Response(json.dumps({'message': "Locust started"}), status = 200, mimetype = 'application/json')

@app.route('/kill', methods=["POST"])
def stop():
    global locust_process # we need write access to this variable
    global locust_process_poll

    if (locust_process != None):
        logger.info("killing locust process");
        try:
            locust_process.kill()
            locust_process_poll.kill()
            logger.info("locust process killed");
        except:
             print "Unexpected error killing locust:", sys.exc_info()[0]
             pass
        finally:
            locust_process = None
            locust_process_poll = None

    return Response(json.dumps({'message': "Locust has stopped"}), status=200, mimetype='application/json')

def start(options):
    locust_args = options.locust_args
    wsgi.WSGIServer((options.web_host, options.port), app, log=None).serve_forever()
