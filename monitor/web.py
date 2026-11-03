"""The monitor in a browser (laptop, phone), meant to be exposed through Tailscale Serve.

Run:  python monitor/web.py      (scripts/start-web-monitor.ps1 keeps it running in the background)

The server listens on 127.0.0.1 only. From other devices it is reachable only through Tailscale Serve,
which adds the caller's login to every request (`Tailscale-User-Login`). Access rule:
  * no proxy headers  -> the request comes from this machine -> allowed;
  * through the proxy -> only logins listed in monitor.allowed_users (config.json or CRK_ALLOWED_USERS).
Being in the tailnet is not enough: shared devices and other users of the tailnet are refused.

Each frame is drawn for the browser window size (cols x rows) by the same function as the terminal
monitor and streamed as ANSI over Server-Sent Events; the page shows it with xterm.js (bundled, no CDN).
"""
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent))
import push  # noqa: E402
from kitconfig import ALLOWED_USERS, WEB_PORT  # noqa: E402

STATIC = Path(__file__).with_name('web')
FILES = {'/': ('index.html', 'text/html; charset=utf-8'),
         '/xterm.min.js': ('xterm.min.js', 'application/javascript'),
         '/xterm.min.css': ('xterm.min.css', 'text/css'),
         '/addon-fit.min.js': ('addon-fit.min.js', 'application/javascript'),
         '/sw.js': ('sw.js', 'application/javascript'),
         '/manifest.json': ('manifest.json', 'application/manifest+json'),
         '/icon-192.png': ('icon-192.png', 'image/png'),
         '/icon-512.png': ('icon-512.png', 'image/png')}
FRAMES_PER_SECOND = 4

FORBIDDEN = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Access denied</title>
<body style="font-family:system-ui;background:#000;color:#d8dee9;max-width:32rem;margin:15vh auto;padding:0 1rem">
<h1 style="font-size:1.4rem">Access denied</h1><p>This monitor is private: over the network only the owner may open it.</p>
<p style="color:#6b7280">If this is you, add your Tailscale login to <code>monitor.allowed_users</code>
in config.json and restart the web monitor.</p><p style="color:#6b7280">403</p></body></html>'''.encode('utf-8')


def start(collector, draw, lock, port=WEB_PORT):
    """Starts the web server in the background. draw(collector, console) is the terminal screen function."""

    def render(cols, rows):
        buf = io.StringIO()
        console = Console(file=buf, width=cols, height=rows, force_terminal=True, color_system='truecolor',
                          legacy_windows=False, no_color=False)
        with lock:
            console.print(draw(collector, console), height=rows)
        return '\x1b[H' + buf.getvalue().rstrip('\n')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def allowed(self):
            login = (self.headers.get('Tailscale-User-Login') or '').strip().lower()
            proxied = bool(login) or 'X-Forwarded-For' in self.headers
            return not proxied or login in ALLOWED_USERS

        def do_GET(self):
            if not self.allowed():
                self.send_response(403)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(FORBIDDEN)
                return
            path = urlparse(self.path)
            if path.path == '/stream':
                return self.stream(parse_qs(path.query))
            if path.path == '/vapid':
                return self.send_json({'key': push.public_key()})
            if path.path not in FILES:
                self.send_error(404)
                return
            name, ctype = FILES[path.path]
            data = (STATIC / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache' if name == 'index.html' else 'max-age=86400')
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # JSON only and same origin only: another page cannot plant a subscription
            if not self.allowed() or 'application/json' not in (self.headers.get('Content-Type') or ''):
                return self.send_json({'error': 'forbidden'}, 403)
            host = self.headers.get('Host') or ''
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != host:
                return self.send_json({'error': 'foreign origin'}, 403)
            try:
                length = min(int(self.headers.get('Content-Length') or 0), 20000)
                data = json.loads(self.rfile.read(length) or b'{}')
                path = urlparse(self.path).path
                if path == '/subscribe':
                    push.subscribe(data.get('subscription'), str(data.get('device', '')),
                                   origin or f'https://{host}')
                    return self.send_json({'ok': True})
                if path == '/unsubscribe':
                    push.unsubscribe(str(data.get('endpoint', '')))
                    return self.send_json({'ok': True})
                if path == '/test':
                    return self.send_json({'ok': True, 'delivered': push.send(
                        '🔔 Claude monitor', "Notifications work: you'll hear from me when a session waits for you.",
                        tag='test')})
            except Exception as error:
                return self.send_json({'error': str(error)[:200]}, 400)
            return self.send_json({'error': 'not found'}, 404)

        def stream(self, params):
            try:
                cols = max(40, min(400, int(params.get('cols', ['120'])[0])))
                rows = max(15, min(200, int(params.get('rows', ['40'])[0])))
            except ValueError:
                cols, rows = 120, 40
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            try:
                while True:
                    self.wfile.write(('data: ' + json.dumps(render(cols, rows), ensure_ascii=False) + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                    time.sleep(1 / FRAMES_PER_SECOND)
            except OSError:
                pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    os.environ.pop('NO_COLOR', None)
    import monitor
    collector, lock = monitor.Collector(), threading.Lock()
    server = start(collector, monitor.screen, lock)
    push.watch(collector, lock)
    print(f'web monitor: http://127.0.0.1:{WEB_PORT}', flush=True)
    if not ALLOWED_USERS:
        print('monitor.allowed_users is empty: only this machine can open the page.', flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
