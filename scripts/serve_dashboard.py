#!/usr/bin/env python3
"""Serve the read-only dashboard on loopback, with a hash-verified download."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit


ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/data.json': ('data.json', 'application/json; charset=utf-8'),
}


def make_server(root, port=8765):
    """Bind only 127.0.0.1. A port of zero is useful for isolated tests."""
    root = Path(root).resolve()
    dashboard = root / 'dashboard'
    outputs = root / 'outputs'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Avoid recording source data or arbitrary request strings.
            pass

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            super().end_headers()

        def do_GET(self):
            self._serve(head=False)

        def do_HEAD(self):
            self._serve(head=True)

        def _serve(self, head):
            # Reject DNS rebinding: arbitrary sites cannot address this local API
            # under their own hostname, even when that name resolves to loopback.
            port = self.server.server_port
            if self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
                self.send_error(403, 'Local dashboard host required')
                return
            try:
                path = unquote(urlsplit(self.path).path, errors='strict')
            except (ValueError, UnicodeDecodeError):
                self.send_error(403, 'Path not allowed')
                return
            attachment = None
            try:
                if path in ASSETS:
                    filename, content_type = ASSETS[path]
                    target = (dashboard / filename).resolve(strict=True)
                    if not target.is_relative_to(dashboard):
                        self.send_error(403, 'Path not allowed')
                        return
                    payload = target.read_bytes()
                elif path == '/submission':
                    metadata_path = (dashboard / 'data.json').resolve(strict=True)
                    if not metadata_path.is_relative_to(dashboard):
                        self.send_error(403, 'Path not allowed')
                        return
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                    configured = Path(metadata['sources']['submission']['path'])
                    target = (root / configured).resolve(strict=True)
                    if not target.is_relative_to(outputs) or not target.is_file():
                        self.send_error(403, 'Submission must be inside outputs')
                        return
                    expected = metadata['pipeline']['sha256']
                    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
                        self.send_error(409, 'Dashboard has no valid submission hash; rebuild the export')
                        return
                    # Verify and send the same immutable bytes, avoiding a file
                    # replacement between verification and transfer.
                    payload = target.read_bytes()
                    if hashlib.sha256(payload).hexdigest() != expected.lower():
                        self.send_error(409, 'Submission changed; rebuild the dashboard export')
                        return
                    attachment = target.name
                    content_type = 'application/gzip' if target.suffix == '.gz' else 'text/csv; charset=utf-8'
                else:
                    self.send_error(403, 'Path not allowed')
                    return
            except FileNotFoundError:
                self.send_error(404, 'Dashboard artifact missing')
                return
            except (OSError, ValueError, KeyError, TypeError):
                self.send_error(409, 'Dashboard artifacts unavailable; rebuild the export')
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            if attachment:
                fallback = re.sub(r'[^A-Za-z0-9._-]', '_', attachment)
                self.send_header('Content-Disposition',
                                 f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(attachment, safe="")}')
            self.end_headers()
            if not head:
                self.wfile.write(payload)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    with make_server(Path(__file__).resolve().parents[1], args.port) as server:
        print(f'Dashboard: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
