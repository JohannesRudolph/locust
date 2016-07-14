import locust

import gevent
import sys
import os
import signal
import inspect
import logging
import socket
from optparse import OptionParser

from . import web
from locust.log import setup_logging, console_logger

version = locust.__version__

def parse_options():
    """
    Handle command-line options with optparse.OptionParser.
    Return list of arguments, largely for use in `parse_arguments`.
    """

    # Initialize
    parser = OptionParser(usage="locustctl [options]")

    parser.add_option('--web-host',
        dest="web_host",
        default="",
        help="Host to bind the ctl web interface to. Defaults to '' (all interfaces)")
    
    parser.add_option('-P', '--port', '--web-port',
        type="int",
        dest="port",
        default=8088,
        help="Port on which to run ctl web host")
    
    parser.add_option('-A', '--locust-args',
        dest="locust_args",
        default="",
        help="command line args to pass to locust (not checked by locusctl!)")

    parser.add_option('-m',
        dest="multiple",
        action="store_true",
        help="start multiple locusts workers (one per cpu core). Use this for spawning slaves")
    
    # log level
    parser.add_option('--loglevel', '-L',
        action='store',
        type='str',
        dest='loglevel',
        default='INFO',
        help="Choose between DEBUG/INFO/WARNING/ERROR/CRITICAL. Default is INFO.",)
    
    # Version number (optparse gives you --version but we have to do it
    # ourselves to get -V too.  sigh)
    parser.add_option('-V', '--version',
        action='store_true',
        dest='show_version',
        default=False,
        help="show program's version number and exit")

    # Finalize
    # Return three-tuple of parser + the output from parse_args (opt obj, args)
    opts, args = parser.parse_args()
    return parser, opts, args
 
def main():
    parser, options, arguments = parse_options()

    # setup logging
    setup_logging(options.loglevel, None)
    logger = logging.getLogger(__name__)
    
    if options.show_version:
        print("Locustctl %s" % (version,))
        sys.exit(0)
   
    try:
        logger.info("Starting Locustctl %s" % version)
        logger.info("Starting web monitor at %s:%s" % (options.web_host or "*", options.port))
        logger.info("Args for launching locust: %s"% (options.locust_args))
        main_greenlet = gevent.spawn(web.start, options)
        main_greenlet.join()
        code = 0
    except KeyboardInterrupt as e:
        code = 0

if __name__ == '__main__':
    main()
