"""
This module adds a tool to locust with intention to help find a highest amount of simulated users, a system can handle.
Parameters to define thresholds for this tool are configured in the web-user-interface

When this module is used, additional response time -data is recorded.
This so that we can calculate a percentile value of the current response times,
meaning we account for the response times recorded in a moving time window.

"""

from . import runners 

#runners.locust_runner, runners.DistributedLocustRunner, runners.SLAVE_REPORT_INTERVAL, runners.STATE_HATCHING
from collections import deque
from statsd import StatsClient

import os
import events
import math
import gevent
import logging

logger = logging.getLogger(__name__) 

# we use statsd for logging for external reporint instead of locusts internal
# metrics
# internal metrics are used for ramping decisions though
statsd = StatsClient(host=os.environ.get('STATSD_HOST', "192.168.99.100"), 
                port=os.environ.get('STATSD_PORT', "8125"), 
                prefix=os.environ.get('STATSD_PREFIX', "locust"))

statsd_tags = os.environ.get('STATSD_TAGS', "testId=test")
response_times = list()

# global result variables
ramp_index = 0
ramp_result = None
ramp_error = ""

def percentile(N, percent, key=lambda x:x):
    """
    Find the percentile of a list of values.

    @parameter N - is a list of values. Note N MUST BE already sorted.
    @parameter percent - a float value from 0.0 to 1.0.
    @parameter key - optional key function to compute value from each element of N.

    @return - the percentile of the values
    """
    if not N:
        return 0
    k = (len(N) - 1) * percent
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return key(N[int(k)])
    d0 = key(N[int(f)]) * (c - k)
    d1 = key(N[int(c)]) * (k - f)
    return d0 + d1

def current_percentile(percent):
    p = percentile(sorted(response_times), percent)
    logger.info("current {0:.2f}% percentile of response times: {1:.1f}".format(percent, p))
    return p

def reset_responses():
    global response_times 
    response_times = list()

def reset_results(): 
    global ramp_index
    global ramp_result
    global ramp_error

    ramp_index = 0
    ramp_result = None
    ramp_error = ""

def current_stats():
    return runners.locust_runner.stats.aggregated_stats("Total")

def on_request_success_ramping(request_type, name, response_time, response_length):
    #statsd.incr("locust.requests");
    tags = statsd_tags + ",type=" + request_type + ",name=" + name
    statsd.timing("responses," + tags, response_time) 
    statsd.incr("responses.contentlength" + tags, response_length) 

    response_times.append(response_time)
       
def on_report_to_master_ramping(client_id, data):
    # report all response timings to master
    global response_times
    data["current_responses"] = response_times
    reset_responses() 

def on_slave_report_ramping(client_id, data):
    # add all reported response timings
    if "current_responses" in data:
        response_times.extend(data["current_responses"])

def register_listeners():
    events.report_to_master += on_report_to_master_ramping
    events.slave_report += on_slave_report_ramping
    events.request_success += on_request_success_ramping
    
def start_ramping(hatch_rate=None, max_locusts=1000, hatch_stride=100,
          percent=0.95, response_time_limit=2000, acceptable_fail=0.05,
          precision=200, start_count=0, calibration_time=15, cooldown_time=10):
    
    reset_results()

    def ramp_execute():
        """execute a ramp stage"""
        global ramp_index 
        ramp_index += 1
        statsd.gauge("ramp," + statsd_tags, ramp_index)

        reset_responses()
        gevent.sleep(calibration_time)

    def ramp_stop(error=None):
        global ramp_error
        if (error != None):
            logger.warn(error)
            ramp_error = error

        logger.info("RAMING STOPPED")
        runners.locust_runner.stop()
        statsd.gauge("locusts," + statsd_tags , 0)
        statsd.gauge("ramp," + statsd_tags, 0)

    def ramp_success(result):
        global ramp_result

        logger.info("Sweet spot found! Ramping stopped at %i locusts" % (result))    
        ramp_result = result
        ramp_stop()
        
    def ramp_set_locusts(clients):
        runners.locust_runner.start_hatching(clients, runners.locust_runner.hatch_rate)
        statsd.gauge("locusts," + statsd_tags , clients)
        
        # wait for hatching to complete
        while runners.locust_runner.state == runners.STATE_HATCHING:
            gevent.sleep(1)

    def ramp_check_failed():
        fail_ratio = current_stats().fail_ratio            
        if fail_ratio > acceptable_fail:
            logger.info("Ramp failed; Acceptable fail ratio %d%% exceeded with fail ratio %d%%" % (acceptable_fail * 100, fail_ratio * 100))
            return True
                    
        p = current_percentile(percent)
        if p >= response_time_limit:
            logger.info("Ramp failed; Acceptable %.1f%% response time of %dms exceeded with %dms" % (percent * 100, response_time_limit, p))
            return True
        
        # ramp passed
        return False

    # implements a binary search for optimum number of clients
    # will exponentially increase clients each step until the first step fails,
    # then start binary search until within target precision
    def test(clients, stride, lower_bound, upper_bound):
        logger.info("Ramp #%d will start with %d locusts, calibration time %d seconds." % (ramp_index, clients, calibration_time))
        logger.info("Current stride is %d and bounds are [%s, %s]" % (stride, str(lower_bound), str(upper_bound)))
        ramp_set_locusts(clients)
        ramp_execute()

        step_failed = ramp_check_failed()
        
        # update bounds
        if step_failed:
            upper_bound = clients
        else:
            lower_bound = clients

        if (upper_bound == None):
            # like TCP slow start, our goal ist to quickly find an upper
            # boundary so we increase our stride exponentially
            stride = stride * 2
        # we have an upper bound, adjust stride to use binary search (either up
        # or down)
        else:
            # stop searching if we're within precision already
            if ((upper_bound - lower_bound) <= precision):
                return ramp_success(lower_bound)
            
            # half the stride
            stride = (stride / 2)
       
        if step_failed:
            clients = clients - stride
            if (clients <= start_count):
                return ramp_stop("Host can't support minimum number of users, check your ramp configuration, locustfile and \"--host\" address")

            logger.info("Initiating cooldown of {0}s to clear congestion".format(cooldown_time))
            ramp_set_locusts(0);
            gevent.sleep(cooldown_time)

            logger.info("Cooldown complete")
            logger.info("Ramping down")

            return test(clients, stride, lower_bound, upper_bound)
        else:
            # if we just tested maximum number of locusts, don't go further
            if (clients == max_locusts):
                return ramp_stop("Max locusts limit reached: %d" % max_locusts) 
            
            logger.info("Ramping up")
            clients = min(clients + stride, max_locusts)
            return test(clients , stride, lower_bound, upper_bound)
                
    if hatch_rate:
        runners.locust_runner.hatch_rate = hatch_rate
    if start_count > 0:
        runners.locust_runner.start_hatching(start_count, hatch_rate)
    
    logger.info("RAMPING STARTED")
    test(start_count, hatch_stride, 0, None)

register_listeners()