# -*- coding: utf-8 -*-

# Copyright © 2012-2017 Chris Warrick, Roberto Alsina and others.

# Permission is hereby granted, free of charge, to any
# person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the
# Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the
# Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice
# shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
# OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""Automatic rebuilds for Nikola."""

import mimetypes
import datetime
import re
import os
import sys
import subprocess
import asyncio
try:
    import aiohttp
    from aiohttp import web
    from aiohttp.web_urldispatcher import StaticResource
    from yarl import unquote
    from aiohttp.web_exceptions import HTTPNotFound, HTTPForbidden
    from aiohttp.web_response import Response
    from aiohttp.web_fileresponse import FileResponse
except ImportError:
    aiohttp = web = unquote = None
    StaticResource = HTTPNotFound = HTTPForbidden = Response = FileResponse = object

try:
    from watchdog.observers import Observer
except ImportError:
    Observer = None

import webbrowser
import pkg_resources

from nikola.plugin_categories import Command
from nikola.utils import dns_sd, req_missing, get_logger, get_theme_path
LRJS_PATH = os.path.join(os.path.dirname(__file__), 'livereload.js')

if sys.platform == 'win32':
    asyncio.set_event_loop(asyncio.ProactorEventLoop())


class CommandAuto(Command):
    """Automatic rebuilds for Nikola."""

    name = "auto"
    logger = None
    has_server = True
    doc_purpose = "builds and serves a site; automatically detects site changes, rebuilds, and optionally refreshes a browser"
    dns_sd = None
    delta_last_rebuild = datetime.timedelta(milliseconds=100)

    cmd_options = [
        {
            'name': 'port',
            'short': 'p',
            'long': 'port',
            'default': 8000,
            'type': int,
            'help': 'Port number (default: 8000)',
        },
        {
            'name': 'address',
            'short': 'a',
            'long': 'address',
            'type': str,
            'default': '127.0.0.1',
            'help': 'Address to bind (default: 127.0.0.1 -- localhost)',
        },
        {
            'name': 'browser',
            'short': 'b',
            'long': 'browser',
            'type': bool,
            'help': 'Start a web browser',
            'default': False,
        },
        {
            'name': 'ipv6',
            'short': '6',
            'long': 'ipv6',
            'default': False,
            'type': bool,
            'help': 'Use IPv6',
        },
        {
            'name': 'no-server',
            'long': 'no-server',
            'default': False,
            'type': bool,
            'help': 'Disable the server, automate rebuilds only'
        },
    ]

    def _execute(self, options, args):
        """Start the watcher."""
        self.logger = get_logger('auto')
        self.sockets = []
        self.rebuild_queue = asyncio.Queue()
        self.last_rebuild = datetime.datetime.now()

        if aiohttp is None and Observer is None:
            req_missing(['aiohttp', 'watchdog'], 'use the "auto" command')
        elif aiohttp is None:
            req_missing(['aiohttp'], 'use the "auto" command')
        elif Observer is None:
            req_missing(['watchdog'], 'use the "auto" command')

        if sys.argv[0].endswith('__main__.py'):
            self.nikola_cmd = [sys.executable, '-m', 'nikola', 'build']
        else:
            self.nikola_cmd = [sys.argv[0], 'build']

        if self.site.configuration_filename != 'conf.py':
            self.nikola_cmd.append('--conf=' + self.site.configuration_filename)

        # Run an initial build so we are up-to-date (synchronously)
        self.logger.info("Rebuilding the site...")
        subprocess.call(self.nikola_cmd)

        port = options and options.get('port')
        self.snippet = '''<script>document.write('<script src="http://'
            + (location.host || 'localhost').split(':')[0]
            + ':{0}/livereload.js?snipver=1"></'
            + 'script>')</script>
        </head>'''.format(port)

        # Deduplicate entries by using a set -- otherwise, multiple rebuilds are triggered
        watched = set([
            'templates/'
        ] + [get_theme_path(name) for name in self.site.THEMES])
        for item in self.site.config['post_pages']:
            watched.add(os.path.dirname(item[0]))
        for item in self.site.config['FILES_FOLDERS']:
            watched.add(item)
        for item in self.site.config['GALLERY_FOLDERS']:
            watched.add(item)
        for item in self.site.config['LISTINGS_FOLDERS']:
            watched.add(item)
        for item in self.site._plugin_places:
            watched.add(item)
        # Nikola itself (useful for developers)
        watched.add(pkg_resources.resource_filename('nikola', ''))

        out_folder = self.site.config['OUTPUT_FOLDER']
        if options and options.get('browser'):
            browser = True
        else:
            browser = False

        if options['ipv6']:
            dhost = '::'
        else:
            dhost = '0.0.0.0'

        host = options['address'].strip('[').strip(']') or dhost

        # Set up asyncio server
        webapp = web.Application()
        webapp.router.add_get('/livereload.js', self.serve_livereload_js)
        webapp.router.add_get('/robots.txt', self.serve_robots_txt)
        webapp.router.add_route('*', '/livereload', self.websocket_handler)
        resource = IndexHtmlStaticResource(True, self.snippet, '', out_folder)
        webapp.router.register_resource(resource)

        # Prepare asyncio event loop
        # Required for subprocessing to work
        loop = asyncio.get_event_loop()

        # Set debug setting
        loop.set_debug(self.site.debug)

        # Server can be disabled (Issue #1883)
        self.has_server = not options['no-server']

        if self.has_server:
            handler = webapp.make_handler()
            srv = loop.run_until_complete(loop.create_server(handler, host, port))

        self.wd_observer = Observer()
        # Watch output folders and trigger reloads
        if self.has_server:
            self.wd_observer.schedule(NikolaEventHandler(self.reload_page, loop), 'output/', recursive=True)

        # Watch input folders and trigger rebuilds
        for p in watched:
            if os.path.exists(p):
                self.wd_observer.schedule(NikolaEventHandler(self.run_nikola_build, loop), p, recursive=True)

        # Watch config file (a bit of a hack, but we need a directory)
        _conf_fn = os.path.abspath(self.site.configuration_filename or 'conf.py')
        _conf_dn = os.path.dirname(_conf_fn)
        self.wd_observer.schedule(ConfigEventHandler(_conf_fn, self.run_nikola_build, loop), _conf_dn, recursive=False)
        self.wd_observer.start()

        if not self.has_server:
            self.logger.info("Watching for changes...")
            # Run the event loop forever (no server mode).
            try:
                # Run rebuild queue
                loop.run_until_complete(self.run_rebuild_queue())

                loop.run_forever()
            except KeyboardInterrupt:
                pass
            finally:
                self.wd_observer.stop()
                self.wd_observer.join()
            loop.close()
            return

        host, port = srv.sockets[0].getsockname()

        self.logger.info("Serving HTTP on {0} port {1}...".format(host, port))
        if browser:
            if options['ipv6'] or '::' in host:
                server_url = "http://[{0}]:{1}/".format(host, port)
            else:
                server_url = "http://{0}:{1}/".format(host, port)

            self.logger.info("Opening {0} in the default web browser...".format(server_url))
            webbrowser.open('http://{0}:{1}'.format(host, port))

        # Run the event loop forever and handle shutdowns.
        try:
            # Run rebuild queue
            loop.run_until_complete(self.run_rebuild_queue())

            self.dns_sd = dns_sd(port, (options['ipv6'] or '::' in host))
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.logger.info("Server is shutting down.")
            if self.dns_sd:
                self.dns_sd.Reset()
            srv.close()
            self.rebuild_queue.put((None, None))
            loop.run_until_complete(srv.wait_closed())
            loop.run_until_complete(webapp.shutdown())
            loop.run_until_complete(handler.shutdown(5.0))
            loop.run_until_complete(webapp.cleanup())
            self.wd_observer.stop()
            self.wd_observer.join()
        loop.close()

    @asyncio.coroutine
    def run_nikola_build(self, event):
        """Rebuild the site."""
        # Move events have a dest_path, some editors like gedit use a
        # move on larger save operations for write protection
        event_path = event.dest_path if hasattr(event, 'dest_path') else event.src_path
        fname = os.path.basename(event_path)
        if (fname.endswith('~') or
                fname.startswith('.') or
                '__pycache__' in event_path or
                event_path.endswith(('.pyc', '.pyo', '.pyd', '_bak')) or
                event.is_directory):  # Skip on folders, these are usually duplicates
            return

        self.logger.debug('Queuing rebuild from {0}'.format(event_path))
        yield from self.rebuild_queue.put((datetime.datetime.now(), event_path))

    @asyncio.coroutine
    def run_rebuild_queue(self):
        """Run rebuilds from a queue (Nikola can only build in a single instance)."""
        while True:
            date, event_path = yield from self.rebuild_queue.get()
            if date is None:
                # Shutdown queue
                return
            if date < (self.last_rebuild + self.delta_last_rebuild):
                self.logger.debug("Skipping rebuild from {0} (within delta)".format(event_path))
                continue
            self.last_rebuild = datetime.datetime.now()
            self.logger.info('REBUILDING SITE (from {0})'.format(event_path))
            p = yield from asyncio.create_subprocess_exec(*self.nikola_cmd, stderr=subprocess.PIPE)
            exit_code = yield from p.wait()
            error = yield from p.stderr.read()
            errord = error.decode('utf-8')

            if exit_code != 0:
                self.logger.error(errord)
                yield from self.send_to_websockets({'command': 'alert', 'message': errord})
            else:
                self.logger.info("Rebuild successful\n" + errord)

    @asyncio.coroutine
    def reload_page(self, event):
        """Reload the page."""
        # Move events have a dest_path, some editors like gedit use a
        # move on larger save operations for write protection
        event_path = event.dest_path if hasattr(event, 'dest_path') else event.src_path
        p = os.path.relpath(event_path, os.path.abspath(self.site.config['OUTPUT_FOLDER'])).replace(os.sep, '/')
        self.logger.info('REFRESHING: {0}'.format(p))
        yield from self.send_to_websockets({'command': 'reload', 'path': p, 'liveCSS': True})

    @asyncio.coroutine
    def serve_livereload_js(self, request):
        """Handle requests to /livereload.js and serve the JS file."""
        return FileResponse(LRJS_PATH)

    @asyncio.coroutine
    def serve_robots_txt(self, request):
        """Handle requests to /robots.txt."""
        return Response(body=b'User-Agent: *\nDisallow: /\n', content_type='text/plain', charset='utf-8')

    @asyncio.coroutine
    def websocket_handler(self, request):
        """Handle requests to /livereload and initiate WebSocket communication."""
        ws = web.WebSocketResponse()
        yield from ws.prepare(request)
        self.sockets.append(ws)

        while True:
            msg = yield from ws.receive()

            self.logger.debug("Received message: {0}".format(msg))
            if msg.type == aiohttp.WSMsgType.TEXT:
                message = msg.json()
                if message['command'] == 'hello':
                    response = {
                        'command': 'hello',
                        'protocols': [
                            'http://livereload.com/protocols/official-7',
                        ],
                        'serverName': 'Nikola Auto (livereload)',
                    }
                    yield from ws.send_json(response)
                elif message['command'] != 'info':
                    self.logger.warn("Unknown command in message: {0}".format(message))
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                break
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                self.logger.debug("Closing WebSocket")
                yield from ws.close()
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                self.logger.error('WebSocket connection closed with exception {0}'.format(ws.exception()))
                break
            else:
                self.logger.warn("Received unknown message: {0}".format(msg))

        self.sockets.remove(ws)
        self.logger.debug("WebSocket connection closed: {0}".format(ws))

        return ws

    @asyncio.coroutine
    def send_to_websockets(self, message):
        """Send a message to all open WebSockets."""
        to_delete = []
        for ws in self.sockets:
            if ws.closed:
                to_delete.append(ws)
                continue

            try:
                yield from ws.send_json(message)
            except RuntimeError as e:
                if 'closed' in e.args[0]:
                    self.logger.warn("WebSocket {0} closed uncleanly".format(ws))
                    to_delete.append(ws)
                else:
                    raise

        for ws in to_delete:
            self.sockets.remove(ws)


class IndexHtmlStaticResource(StaticResource):
    """A StaticResource implementation that serves /index.html in directory roots."""

    modify_html = True
    snippet = "</head>"

    def __init__(self, modify_html=True, snippet="</head>", *args, **kwargs):
        """Initialize a resource."""
        self.modify_html = modify_html
        self.snippet = snippet
        super().__init__(*args, **kwargs)

    @asyncio.coroutine
    def _handle(self, request):
        """Handle incoming requests (pass to handle_file)."""
        filename = unquote(request.match_info['filename'])
        ret = yield from self.handle_file(request, filename)
        return ret

    @asyncio.coroutine
    def handle_file(self, request, filename, from_index=None):
        """Handle file requests."""
        try:
            filepath = self._directory.joinpath(filename).resolve()
            if not self._follow_symlinks:
                filepath.relative_to(self._directory)
        except (ValueError, FileNotFoundError) as error:
            # relatively safe
            raise HTTPNotFound() from error
        except Exception as error:
            # perm error or other kind!
            request.app.logger.exception(error)
            raise HTTPNotFound() from error

        # on opening a dir, load it's contents if allowed
        if filepath.is_dir():
            if filename.endswith('/') or not filename:
                ret = yield from self.handle_file(request, filename + 'index.html', from_index=filename)
            else:
                ret = yield from self.handle_file(request, filename + '/index.html', from_index=filename)
        elif filepath.is_file():
            ct, encoding = mimetypes.guess_type(str(filepath))
            encoding = encoding or 'utf-8'
            if ct == 'text/html' and self.modify_html:
                if sys.version_info[0] == 3 and sys.version_info[1] <= 5:
                    # Python 3.4 and 3.5 do not accept pathlib.Path objects in calls to open()
                    filepath = str(filepath)
                with open(filepath, 'r', encoding=encoding) as fh:
                    text = fh.read()
                    text = self.transform_html(text)
                    ret = Response(text=text, content_type=ct, charset=encoding)
            else:
                ret = FileResponse(filepath, chunk_size=self._chunk_size)
        elif from_index:
            filepath = self._directory.joinpath(from_index).resolve()
            try:
                return Response(text=self._directory_as_html(filepath),
                                content_type="text/html")
            except PermissionError:
                raise HTTPForbidden
        else:
            raise HTTPNotFound

        return ret

    def transform_html(self, text):
        """Apply some transforms to HTML content."""
        # Inject livereload.js
        text = text.replace('</head>', self.snippet, 1)
        # Disable <base> tag
        text = re.sub(r'<base\s([^>]*)>', '<!--base \g<1>-->', text, flags=re.IGNORECASE)
        return text


# Based on code from the 'hachiko' library by John Biesnecker — thanks!
# https://github.com/biesnecker/hachiko
class NikolaEventHandler:
    """A Nikola-specific event handler for Watchdog. Based on code from hachiko."""

    def __init__(self, function, loop):
        """Initialize the handler."""
        self.function = function
        self.loop = loop

    @asyncio.coroutine
    def on_any_event(self, event):
        """Handle all file events."""
        yield from self.function(event)

    def dispatch(self, event):
        """Dispatch events to handler."""
        self.loop.call_soon_threadsafe(asyncio.async, self.on_any_event(event))


class ConfigEventHandler(NikolaEventHandler):
    """A Nikola-specific handler for Watchdog that handles the config file (as a workaround)."""

    def __init__(self, configuration_filename, function, loop):
        """Initialize the handler."""
        self.configuration_filename = configuration_filename
        self.function = function
        self.loop = loop

    @asyncio.coroutine
    def on_any_event(self, event):
        """Handle file events if they concern the configuration file."""
        if event._src_path == self.configuration_filename:
            yield from self.function(event)
