"""
This module adds a tool to locust with intention to help find a highest amount of simulated users, a system can handle.
Parameters to define thresholds for this tool are configured in the web-user-interface

When this module is used, additional response time -data is recorded.
This so that we can calculate a percentile value of the current response times,
meaning we account for the response times recorded in a moving time window.

"""

from runners import locust_runner, DistributedLocustRunner, SLAVE_REPORT_INTERVAL, STATE_HATCHING
from collections import deque
from statsd import StatsClient

import os
import events
import math
import gevent
import logging

logger = logging.getLogger(__name__) 

# we use statsd for logging for external reporint instead of locusts internal metrics
# internal metrics are used for ramping decisions though
statsd = StatsClient(host=os.environ.get('STATSD_HOST', "192.168.99.100"), 
                port=os.environ.get('STATSD_PORT', "8125"), 
                prefix=os.environ.get('STATSD_PREFIX', "locust"))

statsd_tags = os.environ.get('STATSD_TAGS', "testId=test")
response_times = deque([])
ramp_index = 0

# Are we running in distributed mode or not?
is_distributed = isinstance(locust_runner, DistributedLocustRunner)

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
    if is_distributed:
        # Flatten out the deque of lists and calculate the percentile to be
        # returned
        return percentile(sorted([item for sublist in response_times for item in sublist]), percent)
    else:
        p = percentile(sorted(response_times), percent)
        logger.info("current {0:.2f}% percentile of response times: {1:.1f}".format(percent, p))
        return p

def reset():
    global response_times 
    response_times = deque([])

def current_stats():
    return locust_runner.stats.aggregated_stats("Total")

def on_request_success_ramping(request_type, name, response_time, response_length):
    #statsd.incr("locust.requests");
    statsd.timing("requests," + statsd_tags + ",type=" + request_type + ",name=" + name, response_time)

    response_times.append(response_time)
       
def on_report_to_master_ramping(client_id, data):
    # report all response timings to master
    global response_times
    data["current_responses"] = response_times
    reset() 

def on_slave_report_ramping(client_id, data):
    # append all reported response timings
    if "current_responses" in data:
        response_times.append(data["current_responses"])

def register_listeners():
    events.report_to_master += on_report_to_master_ramping
    events.slave_report += on_slave_report_ramping
    events.request_success += on_request_success_ramping
    
def remove_listeners():
    events.report_to_master -= on_report_to_master_ramping
    events.slave_report -= on_slave_report_ramping
    events.request_success -= on_request_success_ramping

def start_ramping(hatch_rate=None, max_locusts=1000, hatch_stride=100,
          percent=0.95, response_time_limit=2000, acceptable_fail=0.05,
          precision=200, start_count=0, calibration_time=15):
    
    register_listeners()
    
    def ramp_execute(clients):
        """execute a ramp stage"""
        global ramp_index 
        ramp_index += 1
        logger.info("Ramp #%d will start with %d locusts, calibration time %d seconds" % (ramp_index, clients, calibration_time))
        
        statsd.gauge("ramp", ramp_index)

        reset()
        gevent.sleep(calibration_time)

    def ramp_stop():
        logger.info("RAMING STOPPED")
        locust_runner.stop()
        statsd.gauge("ramp", 0)
        return remove_listeners()

    def ramp_success():
        logger.info("Sweet spot found! Ramping stopped at %i locusts" % (locust_runner.num_clients))    
        ramp_stop()
        
    def ramp_set_locusts(clients):
        if (clients < 0):
            logger.warning("No responses met the ramping thresholds, check your ramp configuration, locustfile and \"--host\" address")
            return False

        if (clients > max_locusts):
            logger.info("Max locusts limit reached: %d" % max_locusts)
            return False   
        
        locust_runner.start_hatching(clients, locust_runner.hatch_rate)
        statsd.gauge("locusts", clients)
        return True

    def test(clients, hatch_stride, boundary_found):
        if (not ramp_set_locusts(clients)):
            ramp_stop()

        # wait for hatching to complete
        while locust_runner.state == STATE_HATCHING:
            gevent.sleep(1)
        
        ramp_execute(clients)

        # we're currently ramping up
        ramp_failed = False
        
        fail_ratio = current_stats().fail_ratio            
        if fail_ratio > acceptable_fail:
            logger.info("Ramp failed; Acceptable fail ratio %d%% exceeded with fail ratio %d%%" % (acceptable_fail * 100, fail_ratio * 100))
            ramp_failed = True 
                    
        p = current_percentile(percent)
        if p >= response_time_limit:
            logger.info("Ramp failed; Percentile response times getting high: %d" % p)
            ramp_failed = True

        if ramp_failed:
            # half the hatch_stride and move down (binary search), ensure we never move smaller than precision
            hatch_stride = max((hatch_stride / 2), precision)
            boundary_found = True
            logger.info("Ramping down")
            return test(clients-hatch_stride, hatch_stride, boundary_found)

        if (boundary_found and (hatch_stride <= precision)):
            # ramp did not fail, but current hatch_stride is smaller than precision so no need to test further
            return ramp_success()
    
        # continue ramping up with the same hatch_stride
        logger.info("Ramping up")
        return test(min(clients+hatch_stride, max_locusts) , hatch_stride, boundary_found)
                
    if hatch_rate:
        locust_runner.hatch_rate = hatch_rate
    if start_count > 0:
        locust_runner.start_hatching(start_count, hatch_rate)
    
    logger.info("RAMPING STARTED")
    test(start_count, hatch_stride, False)