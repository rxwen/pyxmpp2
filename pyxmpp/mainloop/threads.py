#
# (C) Copyright 2011 Jacek Konieczny <jajcus@jajcus.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License Version
# 2.1 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#

"""I/O Handling classes

This module has a purpose similar to `asyncore` from the base library, but
should be more usable, especially for PyXMPP.

Also, these interfaces should allow building application not only in
asynchronous event loop model, but also threaded model.
"""

from __future__ import absolute_import

__docformat__ = "restructuredtext en"

import time
import select
import threading
import logging
import sys
import Queue

from .interfaces import MainLoop, HandlerReady, PrepareAgain
from .interfaces import EventHandler, IOHandler, QUIT
from .events import EventDispatcher
from ..settings import XMPPSettings

logger = logging.getLogger("pyxmpp.mainloop.threads")

class IOThread(object):
    """Base class for `ReaderThread` and `WritterThread`

    :Ivariables:
        - `name`: thread name (for debugging)
        - `io_handler`: the I/O handler object to poll
        - `thread`: the actual thread object
        - `exc_info`: this will hold exception information tuple
        whenever the thread was aborted by an exception.
    :Types:
        - `name`: `unicode`
        - `io_handler`: `IOHandler`
        - `thread`: `threading.Thread`
        - `exc_info`: (type, value, traceback) tuple
    """
    def __init__(self, io_handler, name, daemon = True, exc_queue = None):
        self.name = name
        self.io_handler = io_handler
        self.thread = threading.Thread(name = name, target = self._run)
        self.thread.daemon = daemon
        self.exc_info = None
        self.exc_queue = exc_queue
        self._quit = False

    def start(self):
        self.thread.start()
    
    def is_alive(self):
        return self.thread.is_alive()

    def stop(self):
        self._quit = True

    def join(self, timeout):
        return self.thread.join(timeout)

    def _run(self):
        """The thread function. Calls `self.run()` and if it raises
        an exception, sotres it in self.exc_info
        """
        logger.debug("{0}: entering thread".format(self.name))
        try:
            self.run()
        except:
            logger.debug("{0}: aborting thread".format(self.name))
            self.exc_info = sys.exc_info()
            logger.debug(u"exception in the {0!r} thread:"
                            .format(self.name), exc_info = self.exc_info)
            if self.exc_queue:
                self.exc_queue.put( (self, self.exc_info) )
        else:
            logger.debug("{0}: exiting thread".format(self.name))

    def run(self):
        """The thread function."""
        raise NotImplementedError


class ReadingThread(IOThread):
    """A thread reading from io_handler.

    This thread will be also the one to call the `IOHandler.prepare` method
    until HandlerReady is returned.
    
    It can be used (together with `WrittingThread`) instead of 
    a main loop."""
    def __init__(self, io_handler, name = None, daemon = True,
                                                            exc_queue = None):
        if name is None:
            name = u"{0!r} reader".format(io_handler)
        IOThread.__init__(self, io_handler, name, daemon, exc_queue)

    def run(self):
        """The thread function."""
        self.io_handler.set_blocking(True)
        prepared = False
        timeout = 0.1
        while not self._quit:
            if not prepared:
                logger.debug("{0}: preparing handler: {1!r}".format(
                                                   self.name, self.io_handler))
                ret = self.io_handler.prepare()
                logger.debug("{0}: prepare result: {1!r}".format(self.name,
                                                                        ret))
                if isinstance(ret, HandlerReady):
                    prepared = True
                elif isinstance(ret, PrepareAgain):
                    if ret.timeout is not None:
                        timeout = ret.timeout
                else:
                    raise TypeError("Unexpected result type from prepare()")
            if self.io_handler.is_readable():
                logger.debug("{0}: readable".format(self.name))
                fileno = self.io_handler.fileno()
                if fileno is not None:
                    readable = select.select([fileno], [], [], 1)[0]
                    if readable:
                        self.io_handler.handle_read()
            elif not prepared:
                if timeout:
                    time.sleep(timeout)
            else:
                logger.debug("{0}: waiting for readability".format(self.name))
                if not self.io_handler.wait_for_readability():
                    break

class WrittingThread(IOThread):
    """A thread reading from io_handler.
    
    It can be used (together with `WrittingThread`) instead of 
    a main loop."""
    def __init__(self, io_handler, name = None, daemon = True,
                                                            exc_queue = None):
        if name is None:
            name = u"{0!r} writer".format(io_handler)
        IOThread.__init__(self, io_handler, name, daemon, exc_queue)

    def run(self):
        """The thread function."""
        self.io_handler.set_blocking(True)
        while not self._quit:
            if self.io_handler.is_writable():
                logger.debug("{0}: writable".format(self.name))
                fileno = self.io_handler
                if fileno:
                    writable = select.select([], [fileno], [], 1)[1]
                    if writable:
                        self.io_handler.handle_read()
                    self.io_handler.handle_write()
            else:
                logger.debug("{0}: waiting for writaility".format(self.name))
                if not self.io_handler.wait_for_writability():
                    break

class EventDispatcherThread(object):
    """Event dispatcher thread.
    
    :Ivariables:
        - `name`: thread name (for debugging)
        - `event_queue`: the event queue to poll
        - `thread`: the actual thread object
        - `exc_info`: this will hold exception information tuple
        whenever the thread was aborted by an exception.
    :Types:
        - `name`: `unicode`
        - `event_queue`: `Queue.Queue`
        - `thread`: `threading.Thread`
        - `exc_info`: (type, value, traceback) tuple
    """
    def __init__(self, event_dispatcher, name = None,
                                            daemon = True, exc_queue = None):
        if name is None:
            name = "event dispatcher"
        self.name = name
        self.thread = threading.Thread(name = name, target = self.run)
        self.thread.daemon = daemon
        self.exc_info = None
        self.exc_queue = exc_queue
        self.event_dispatcher = event_dispatcher

    def start(self):
        self.thread.start()

    def is_alive(self):
        return self.thread.is_alive()

    def join(self, timeout):
        return self.thread.join(timeout)

    def run(self):
        """The thread function. Calls `self.run()` and if it raises
        an exception, stores it in self.exc_info and exc_queue
        """
        logger.debug("{0}: entering thread".format(self.name))
        try:
            self.event_dispatcher.loop()
        except:
            logger.debug("{0}: aborting thread".format(self.name))
            self.exc_info = sys.exc_info()
            logger.debug(u"exception in the {0!r} thread:"
                            .format(self.name), exc_info = self.exc_info)
            if self.exc_queue:
                self.exc_queue.put( (self, self.exc_info) )
        else:
            logger.debug("{0}: exiting thread".format(self.name))


class ThreadPool(MainLoop):
    """Thread pool object, as a replacement for an asychronous event loop."""
    def __init__(self, settings = None, handlers = None):
        if settings is None:
            self.settings = XMPPSettings()
        else:
            self.settings = settings
        self.io_handlers = []
        self.event_queue = self.settings["event_queue"]
        self.event_dispatcher = EventDispatcher(self.settings, handlers)
        if handlers:
            for handler in handlers:
                if isinstance(handler, IOHandler):
                    self.io_handlers.append(handler)
        self.exc_queue = Queue.Queue()
        self.io_threads = []
        self.event_thread = None

    def start(self, daemon = False):
        self.io_threads = []
        for handler in self.io_handlers:
            reader = ReadingThread(handler, daemon = daemon,
                                                    exc_queue = self.exc_queue)
            writter = WrittingThread(handler, daemon = daemon,
                                                    exc_queue = self.exc_queue)
            self.io_threads += [reader, writter]
        self.event_thread = EventDispatcherThread(self.event_dispatcher,
                                    daemon = daemon, exc_queue = self.exc_queue)
        self.event_thread.start()
        for thread in self.io_threads:
            thread.start()

    def stop(self, join = False, timeout = None):
        logger.debug("Closing the io handlers...")
        for handler in self.io_handlers:
            handler.close()
        if self.event_thread.is_alive():
            logger.debug("Sending the QUIT signal")
            self.event_queue.put(QUIT)
        logger.debug("  sent")
        for thread in self.io_threads:
            logger.debug("Stopping thread: {0!r}".format(thread))
            thread.stop()
        if not join:
            return
        threads = list(self.io_threads)
        if self.event_thread:
            threads.append(self.event_thread)
        if timeout is None:
            for thread in threads:
                thread.join()
        else:
            timeout1 = (timeout * 0.01) / len(threads)
            threads_left = []
            for thread in threads:
                logger.debug("Quick-joining thread {0!r}...".format(thread))
                thread.join(timeout1)
                if thread.is_alive():
                    logger.debug("  thread still alive".format(thread))
                    threads_left.append(thread)
            if threads_left:
                timeout2 = (timeout * 0.99) / len(threads_left)
                for thread in threads_left:
                    logger.debug("Joining thread {0!r}...".format(thread))
                    thread.join(timeout2)
        self.io_threads = []
        self.event_thread = None

    def finished(self):
        return self.event_thread is None or not self.event_thread.is_alive()

    def loop(self, timeout):
        if not self.event_thread:
            return
        while self.event_thread.is_alive():
            self.loop_iteration(timeout)

    def loop_iteration(self, timeout = 0.1):
        try:
            thread, exc_info = self.exc_queue.get(True, timeout)
        except Queue.Empty:
            return
        exc_type, exc_value, ext_stack = exc_info
        raise exc_type, exc_value, ext_stack