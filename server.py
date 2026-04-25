"""
Windows Remote Terminal - Pure Python, zero dependencies.
Uses Server-Sent Events (SSE) for output and POST for input.

Usage:
    python server.py [--port 3000] [--host 0.0.0.0]

Access at http://localhost:3000
"""

import sys
import os
import json
import uuid
import threading
import subprocess
import argparse
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from queue import Queue, Empty

# ── Session management ────────────────────────────────────────────────────────

sessions: dict[str, "Session"] = {}
sessions_lock = threading.Lock()


class Session:
    def __init__(self, session_id: str):
        self.id = session_id
        self.queue: Queue[str | None] = Queue()
        self.proc = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "TERM": "xterm-color"},
        )
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        print(f"[session {session_id[:8]}] started (pid={self.proc.pid})")

    def _read_loop(self):
        """Read subprocess stdout and push chunks to the SSE queue."""
        try:
            while self._alive:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                # Encode as a JSON SSE event
                text = chunk.decode("utf-8", errors="replace")
                self.queue.put(json.dumps({"type": "output", "data": text}))
        except Exception as e:
            print(f"[session {self.id[:8]}] reader error: {e}")
        finally:
            self.queue.put(None)  # sentinel → close SSE stream
            self._alive = False
            self.close()

    def write(self, data: str):
        if self._alive and self.proc.stdin:
            try:
                self.proc.stdin.write(data.encode("utf-8"))
                self.proc.stdin.flush()
            except OSError:
                pass

    def close(self):
        self._alive = False
        try:
            self.proc.terminate()
        except Exception:
            pass
        with sessions_lock:
            sessions.pop(self.id, None)
        print(f"[session {self.id[:8]}] closed")


# ── HTTP handler ──────────────────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "public")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress noisy per-request logs; only print errors
        if int(args[1]) >= 400:
            super().log_message(fmt, *args)

    # ── routing ──

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html")
        elif path == "/terminal":
            self._handle_sse(parsed)
        elif path.startswith("/"):
            # Try to serve static file
            filename = path.lstrip("/") or "index.html"
            self._serve_file(filename)
        else:
            self._send(404, "text/plain", b"Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/input":
            self._handle_input(params)
        elif path == "/close":
            self._handle_close(params)
        else:
            self._send(404, "text/plain", b"Not Found")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── SSE: open a shell session and stream output ──

    def _handle_sse(self, parsed):
        session_id = str(uuid.uuid4())
        session = Session(session_id)
        with sessions_lock:
            sessions[session_id] = session

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()

        # First event: tell the client its session ID
        self._sse_write(json.dumps({"type": "session", "id": session_id}))

        try:
            while True:
                try:
                    msg = session.queue.get(timeout=15)
                except Empty:
                    # Heartbeat to keep the connection alive
                    self._sse_write(json.dumps({"type": "ping"}))
                    continue

                if msg is None:  # sentinel: shell exited
                    self._sse_write(json.dumps({"type": "exit"}))
                    break

                self._sse_write(msg)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            session.close()

    def _sse_write(self, data: str):
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except OSError:
            raise BrokenPipeError

    # ── POST /input ──

    def _handle_input(self, params):
        session_id = (params.get("session") or [None])[0]
        if not session_id:
            return self._send(400, "text/plain", b"Missing session")

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

        with sessions_lock:
            session = sessions.get(session_id)

        if not session:
            return self._send(410, "text/plain", b"Session not found")

        session.write(body)
        self._send(200, "text/plain", b"ok")

    # ── POST /close ──

    def _handle_close(self, params):
        session_id = (params.get("session") or [None])[0]
        with sessions_lock:
            session = sessions.pop(session_id, None)
        if session:
            session.close()
        self._send(200, "text/plain", b"ok")

    # ── helpers ──

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_file(self, filename: str, ctype: str | None = None):
        filepath = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            return self._send(404, "text/plain", b"Not Found")
        ext = os.path.splitext(filename)[1]
        mime = ctype or {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(filepath, "rb") as f:
            data = f.read()
        self._send(200, mime, data)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Windows Remote Terminal")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.allow_reuse_address = True

    local_ip = _get_local_ip()
    print(f"Remote Terminal ready.")
    print(f"  Local:   http://localhost:{args.port}")
    if local_ip:
        print(f"  Network: http://{local_ip}:{args.port}")
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        # Kill all active sessions
        with sessions_lock:
            for s in list(sessions.values()):
                s.close()
        server.shutdown()


def _get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


if __name__ == "__main__":
    main()
