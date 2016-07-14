# encoding: utf-8

import csv
import json
import os
import os.path
import sys
import multiprocessing

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

locust_processes = list()
locust_args = ""
locust_spawn = 1

@app.route('/boot', methods=["POST"])
def boot():
    global locust_processes # we need write access to this variable

    if (len(locust_processes) > 0):
        return Response(json.dumps({'message': "Locust already running"}), status=400, mimetype='application/json')

    filename = 'locustfile.py'
    
    # check if the post request has the file part
    if (filename not in request.files):
        return Response(json.dumps({'message': "Request did not contain a locustfile.py"}), status=400, mimetype='application/json')

    host = request.form["host"]
    if (not host):
        return Response(json.dumps({'message': "Request did not contain a target host"}), status=400, mimetype='application/json')

    tags = request.form["tags"]
    if (not tags):
        return Response(json.dumps({'message': "Request did not contain a tag"}), status=400, mimetype='application/json')

    file = request.files[filename]
    locustfile = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # ensure the path is clear
    try:
        os.remove(filename)
    except OSError: 
        pass

    file.save(locustfile)
    
    # form fields are decoded as unicode, but shell only accepts ANSI
    # They shouldn't use any utf-8 characters anway, so we just coerce them.
    tags = str(tags)
    host = str(host)

    args = ["locust"]
    args.extend(shlex.split(locust_args))
    args.extend(["-f", locustfile])
    args.extend(["--host", host])

    locust_env = os.environ.copy()
    locust_env["STATSD_TAGS"] = tags

    def poll(process, i):
        while True:
            s = process.stdout.readline()
            if s == "": # EOF, process exited
                break
            else:
                logger.info("#{0} {1}".format(i, s.strip()))

    for i in xrange(locust_spawn):
        logger.info("starting locust slave process #{0}".format(i))
        process = subprocess.Popen(args, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.getcwd(), env=locust_env)
        # starts a greenlet to read stdout from process
        poller = gevent.spawn(poll, process, i)
        locust_processes.append((i, process, poller))
     
    return Response(json.dumps({'message': "Locust started"}), status = 200, mimetype = 'application/json')

@app.route('/kill', methods=["POST"])
def stop():
    slaves = len(locust_processes)
    for i in xrange(slaves):
        (n, proc, poll) = locust_processes.pop()
        logger.info("killing locust slave process #{0}".format(n))
        try:
            proc.kill()
            poll.kill()
            logger.info("locust process killed")
        except:
             print "Unexpected error killing locust {0}: {1}".format(n, sys.exc_info())
             pass

    return Response(json.dumps({'message': "Locust has stopped"}), status=200, mimetype='application/json')

def start(options):
    global locust_args
    global locust_spawn

    locust_args = options.locust_args
    
    if (options.multiple):
        cores = multiprocessing.cpu_count()
        logger.info("multiprocessing enabled, machine has {0} cpu cores".format(cores))
        locust_spawn = cores
    
    wsgi.WSGIServer((options.web_host, options.port), app, log=None).serve_forever()
