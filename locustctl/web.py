# encoding: utf-8

import csv
import json
import os.path

from gevent import wsgi
from flask import Flask, Response, request
from locust import __version__ as version

import gevent
import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.debug = True
app.root_path = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = 'uploads'

if not os.path.exists('uploads'):
    os.makedirs('uploads')

locust_process = None
locust_args = ""

@app.route('/boot', methods=["POST"])
def boot():
    global locust_process # we need write access to this variable

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

    locust_process = subprocess.Popen(args, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    return Response(json.dumps({'message': "Locust started"}), status=200, mimetype='application/json')

@app.route('/kill', methods=["POST"])
def stop():
    if (locust_process != None):
        locust_process.kill()
        locust_process = None

    return Response(json.dumps({'message': "Locust has stopped"}), status=200, mimetype='application/json')

def start(options):
    locust_args = options.locust_args
    wsgi.WSGIServer((options.web_host, options.port), app, log=None).serve_forever()
