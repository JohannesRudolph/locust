# encoding: utf-8

import csv
import json
import os.path
from time import time
from itertools import chain
from collections import defaultdict
from six.moves import StringIO, xrange
import six

from gevent import wsgi
from flask import Flask, make_response, request, render_template

from . import runners
from .cache import memoize
from .runners import MasterLocustRunner
from locust.stats import median_from_dict
from locust import __version__ as version

import gevent
import logging
logger = logging.getLogger(__name__)

DEFAULT_CACHE_TIME = 2.0

app = Flask(__name__)
app.debug = True
app.root_path = os.path.dirname(os.path.abspath(__file__))

def is_running():
    return runners.locust_runner.state != runners.STATE_INIT or runners.locust_runner.state != runners.STATE_STOPPED

def make_already_running_response():
    response = make_response(json.dumps({'success':False, 'message': 'Runner already executing a test, please stop it first'}))
    response.headers["Content-type"] = "application/json"
    return response

@app.route('/')
def index():
    is_distributed = isinstance(runners.locust_runner, MasterLocustRunner)
    if is_distributed:
        slave_count = runners.locust_runner.slave_count
    else:
        slave_count = 0
    
    return render_template("index.html",
        state=runners.locust_runner.state,
        is_distributed=is_distributed,
        slave_count=slave_count,
        user_count=runners.locust_runner.user_count,
        ramp = True,
        version=version)

@app.route('/swarm', methods=["POST"])
def swarm():
    if (is_running()):
        return make_already_running_response()

    locust_count = int(request.form["locust_count"])
    hatch_rate = float(request.form["hatch_rate"])
    runners.locust_runner.start_hatching(locust_count, hatch_rate)
    response = make_response(json.dumps({'success':True, 'message': 'Swarming started'}))
    response.headers["Content-type"] = "application/json"
    return response

@app.route("/ramp", methods=["POST"])
def ramp():
    from ramping import start_ramping
    
    if (is_running()):
        return make_already_running_response()

    init_clients = int(request.form["init_count"])
    hatch_rate = int(request.form["hatch_rate"])
    hatch_stride = int(request.form["hatch_stride"])
    precision = int(request.form["precision"])
    max_clients = int(request.form["max_count"])
    response_time = int(request.form["response_time"])
    percentile = float(int(request.form["percentile"]) / 100.0)
    fail_rate = float(int(request.form["fail_rate"]) / 100.0)
    calibration_time = int(request.form["wait_time"])
    
    # ramping acts as a "controller" for locust and runs on its own thread
    gevent.spawn(start_ramping, hatch_rate, max_clients, hatch_stride, percentile, response_time, fail_rate, precision, init_clients, calibration_time)

    response = make_response(json.dumps({'success':True, 'message': 'Ramping started'}))
    response.headers["Content-type"] = "application/json"
    return response

@app.route('/state')
def state():
    response = make_response(json.dumps({'state': runners.locust_runner.state}))
    response.headers["Content-type"] = "application/json"
    return response

@app.route('/stop')
def stop():
    runners.locust_runner.stop()
    response = make_response(json.dumps({'success':True, 'message': 'Test stopped'}))
    response.headers["Content-type"] = "application/json"
    return response

@app.route("/stats/reset")
def reset_stats():
    runners.locust_runner.stats.reset_all()
    return "ok"
    
@app.route("/stats/requests/csv")
def request_stats_csv():
    rows = [",".join(['"Method"',
            '"Name"',
            '"# requests"',
            '"# failures"',
            '"Median response time"',
            '"Average response time"',
            '"Min response time"', 
            '"Max response time"',
            '"Average Content Size"',
            '"Requests/s"',])]
    
    for s in chain(_sort_stats(runners.locust_runner.request_stats), [runners.locust_runner.stats.aggregated_stats("Total", full_request_history=True)]):
        rows.append('"%s","%s",%i,%i,%i,%i,%i,%i,%i,%.2f' % (s.method,
            s.name,
            s.num_requests,
            s.num_failures,
            s.median_response_time,
            s.avg_response_time,
            s.min_response_time or 0,
            s.max_response_time,
            s.avg_content_length,
            s.total_rps,))

    response = make_response("\n".join(rows))
    file_name = "requests_{0}.csv".format(time())
    disposition = "attachment;filename={0}".format(file_name)
    response.headers["Content-type"] = "text/csv"
    response.headers["Content-disposition"] = disposition
    return response

@app.route("/stats/distribution/csv")
def distribution_stats_csv():
    rows = [",".join(('"Name"',
        '"# requests"',
        '"50%"',
        '"66%"',
        '"75%"',
        '"80%"',
        '"90%"',
        '"95%"',
        '"98%"',
        '"99%"',
        '"100%"',))]
    for s in chain(_sort_stats(runners.locust_runner.request_stats), [runners.locust_runner.stats.aggregated_stats("Total", full_request_history=True)]):
        if s.num_requests:
            rows.append(s.percentile(tpl='"%s",%i,%i,%i,%i,%i,%i,%i,%i,%i,%i'))
        else:
            rows.append('"%s",0,"N/A","N/A","N/A","N/A","N/A","N/A","N/A","N/A","N/A"' % s.name)

    response = make_response("\n".join(rows))
    file_name = "distribution_{0}.csv".format(time())
    disposition = "attachment;filename={0}".format(file_name)
    response.headers["Content-type"] = "text/csv"
    response.headers["Content-disposition"] = disposition
    return response

@app.route('/stats/requests')
@memoize(timeout=DEFAULT_CACHE_TIME, dynamic_timeout=True)
def request_stats():
    stats = []
    for s in chain(_sort_stats(runners.locust_runner.request_stats), [runners.locust_runner.stats.aggregated_stats("Total")]):
        stats.append({
            "method": s.method,
            "name": s.name,
            "num_requests": s.num_requests,
            "num_failures": s.num_failures,
            "avg_response_time": s.avg_response_time,
            "min_response_time": s.min_response_time or 0,
            "max_response_time": s.max_response_time,
            "current_rps": s.current_rps,
            "median_response_time": s.median_response_time,
            "avg_content_length": s.avg_content_length,
        })
    
    report = {"stats":stats, "errors":[e.to_dict() for e in six.itervalues(runners.locust_runner.errors)]}
    if stats:
        report["total_rps"] = stats[len(stats) - 1]["current_rps"]
        report["fail_ratio"] = runners.locust_runner.stats.aggregated_stats("Total").fail_ratio
        
        # since generating a total response times dict with all response times
        # from all
        # urls is slow, we make a new total response time dict which will
        # consist of one
        # entry per url with the median response time as key and the number of
        # requests as
        # value
        response_times = defaultdict(int) # used for calculating total median
        for i in xrange(len(stats) - 1):
            response_times[stats[i]["median_response_time"]] += stats[i]["num_requests"]
        
        # calculate total median
        stats[len(stats) - 1]["median_response_time"] = median_from_dict(stats[len(stats) - 1]["num_requests"], response_times)
    
    is_distributed = isinstance(runners.locust_runner, MasterLocustRunner)
    if is_distributed:
        report["slave_count"] = runners.locust_runner.slave_count
    
    report["state"] = runners.locust_runner.state
    report["user_count"] = runners.locust_runner.user_count
    return json.dumps(report)

@app.route("/exceptions")
def exceptions():
    response = make_response(json.dumps({'exceptions': [{"count": row["count"], "msg": row["msg"], "traceback": row["traceback"], "nodes" : ", ".join(row["nodes"])} for row in six.itervalues(runners.locust_runner.exceptions)]}))
    response.headers["Content-type"] = "application/json"
    return response

@app.route("/exceptions/csv")
def exceptions_csv():
    data = StringIO()
    writer = csv.writer(data)
    writer.writerow(["Count", "Message", "Traceback", "Nodes"])
    for exc in six.itervalues(runners.locust_runner.exceptions):
        nodes = ", ".join(exc["nodes"])
        writer.writerow([exc["count"], exc["msg"], exc["traceback"], nodes])
    
    data.seek(0)
    response = make_response(data.read())
    file_name = "exceptions_{0}.csv".format(time())
    disposition = "attachment;filename={0}".format(file_name)
    response.headers["Content-type"] = "text/csv"
    response.headers["Content-disposition"] = disposition
    return response

def start(locust, options):
    wsgi.WSGIServer((options.web_host, options.port), app, log=None).serve_forever()

def _sort_stats(stats):
    return [stats[key] for key in sorted(six.iterkeys(stats))]
