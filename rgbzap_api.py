"""RGBZap's local control API: a tiny HTTP server so other programs (the `rgbzap`
command line, Stream Deck, AutoHotkey, OBS scripts, a build-status script...) can drive
the lights while RGBZap is running.

Built to be safe to leave on:
  * binds 127.0.0.1 only, so nothing on your network can reach it
  * every request needs the secret token (Authorization: Bearer <token>)
  * requests that look like they come from a web page (an Origin header) are refused,
    and so are Host headers other than localhost, which blocks "DNS rebinding"
  * tiny bodies only (4 KB), JSON only, no files, no shell, no way to read anything
    off the PC: it can only do what the buttons in the app can do
"""
import hmac
import json
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 4096

NAMED_COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "white": (255, 255, 255),
    "yellow": (255, 255, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255),
    "orange": (255, 90, 0), "purple": (150, 0, 255), "pink": (255, 40, 120),
    "warm": (255, 138, 18), "cool": (180, 220, 255),
}


class ApiError(Exception):
    """Raise from a dispatch function to answer with a specific HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def new_token():
    return secrets.token_hex(16)


def parse_color(value):
    """'red' | '#ff8800' | 'f80' | 'ff8800' | '255,136,0' | [255, 136, 0] -> (r, g, b),
    or None when it isn't a color."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
    elif isinstance(value, str):
        s = value.strip().lower()
        if s in NAMED_COLORS:
            return NAMED_COLORS[s]
        h = s[1:] if s.startswith("#") else s
        if len(h) in (3, 6) and all(c in "0123456789abcdef" for c in h):
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        parts = [p.strip() for p in s.replace(" ", ",").split(",") if p.strip()]
    else:
        return None
    try:
        rgb = tuple(int(p) if not isinstance(p, bool) else None for p in parts)
    except (TypeError, ValueError):
        return None
    if len(rgb) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in rgb):
        return rgb
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "RGBZap"
    sys_version = ""
    timeout = 5                                     # a stalled client can't hang a thread

    def log_message(self, *_a):                     # never log: tokens could appear in errors
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        api = self.server.api
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        # Always consume what the client already sent (up to a limit) before replying.
        # Answering without reading it can make Windows reset the connection, and the
        # client would see "connection aborted" instead of our 401/403/413.
        raw = b""
        if 0 < length <= 65536:
            try:
                raw = self.rfile.read(length)
            except OSError:
                return
        host = (self.headers.get("Host") or "").lower()
        if host not in api.allowed_hosts or self.headers.get("Origin") is not None:
            return self._send(403, {"error": "forbidden"})
        got = (self.headers.get("Authorization") or "")
        expect = "Bearer " + (api.get_token() or "")
        if not api.get_token() or not hmac.compare_digest(got.encode(), expect.encode()):
            return self._send(401, {"error": "unauthorized"})
        if length < 0:
            return self._send(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self._send(413, {"error": "request too large"})
        body = {}
        if length:
            try:
                body = json.loads(raw)
            except ValueError:
                return self._send(400, {"error": "body must be JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "body must be a JSON object"})
        try:
            status, payload = api.dispatch(method, self.path.split("?")[0], body)
        except ApiError as e:
            return self._send(e.status, {"error": e.message})
        except Exception:
            return self._send(500, {"error": "internal error"})   # details stay in the app
        self._send(status, payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False            # on Windows SO_REUSEADDR lets another program
                                           # share (and read) our port: never set it

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LocalApi:
    """dispatch(method, path, body) -> (status, payload dict); raise ApiError for
    errors. It runs on a server thread, so it must be thread-safe (the app hands the
    real work to its UI thread). get_token() is read per request, so changing the
    token takes effect at once."""

    def __init__(self, dispatch, get_token):
        self.dispatch, self.get_token = dispatch, get_token
        self.allowed_hosts = set()
        self.port = None
        self._server = None
        self._thread = None

    @property
    def running(self):
        return self._server is not None

    def start(self, port):
        """Raises OSError if the port is taken."""
        if self._server is not None:
            return
        server = _Server(("127.0.0.1", port), _Handler)
        server.api = self
        self.port = server.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = self._thread = None


def _selftest():
    """python rgbzap_api.py --selftest"""
    import http.client
    for good, want in (("red", (255, 0, 0)), (" Orange ", (255, 90, 0)),
                       ("#ff8800", (255, 136, 0)), ("ff8800", (255, 136, 0)),
                       ("#f80", (255, 136, 0)), ("255,136,0", (255, 136, 0)),
                       ("255 136 0", (255, 136, 0)), ([1, 2, 3], (1, 2, 3)),
                       ((0, 0, 0), (0, 0, 0))):
        assert parse_color(good) == want, (good, parse_color(good))
    for bad in ("", "reddish", "#12", "#gggggg", "256,0,0", "-1,0,0", "1,2", "1,2,3,4",
                [1, 2], [1, 2, "x"], [True, 0, 0], None, 5, {"r": 1}):
        assert parse_color(bad) is None, f"accepted {bad!r}"
    assert len(new_token()) == 32 and new_token() != new_token()
    print("OK  api: color parsing (names, hex, rgb) accepts good input, rejects the rest")

    calls = []

    def dispatch(method, path, body):
        calls.append((method, path, body))
        if path == "/boom":
            raise RuntimeError("secret internal detail")
        if path == "/missing":
            raise ApiError(404, "no such thing")
        return 200, {"ok": True, "echo": body}

    token = "t" * 32
    api = LocalApi(dispatch, lambda: token)
    api.start(0)
    port = api.port
    assert port and api.running

    def req(method, path, body=None, headers=None, host=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{port}"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None
                                            else None)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    auth = {"Authorization": f"Bearer {token}"}
    assert req("GET", "/status")[0] == 401, "no token should be refused"
    assert req("GET", "/status", headers={"Authorization": "Bearer nope"})[0] == 401
    assert req("GET", "/status", headers={"Authorization": token})[0] == 401, "needs 'Bearer '"
    assert not calls, "an unauthorized request reached the app"
    st, body = req("GET", "/status", headers=auth)
    assert st == 200 and json.loads(body)["ok"] and calls[-1] == ("GET", "/status", {})
    st, body = req("POST", "/color", {"color": "red"}, headers=auth)
    assert st == 200 and calls[-1] == ("POST", "/color", {"color": "red"})
    assert req("GET", "/status?x=1", headers=auth)[0] == 200 and calls[-1][1] == "/status"
    # a web page can't use it, even if it somehow knew the token
    n = len(calls)
    assert req("POST", "/on", {}, headers={**auth, "Origin": "https://evil.example"})[0] == 403
    assert req("POST", "/on", {}, headers=auth, host="evil.example")[0] == 403, "DNS rebinding"
    assert req("POST", "/on", {}, headers=auth, host=f"localhost:{port + 1}")[0] == 403
    assert req("GET", "/status", headers=auth, host=f"localhost:{port}")[0] == 200
    assert len(calls) == n + 1, "a forbidden request reached the app"
    # malformed input
    assert req("POST", "/x", raw=b"{not json", headers=auth)[0] == 400
    assert req("POST", "/x", raw=b"[1,2]", headers=auth)[0] == 400
    assert req("POST", "/x", raw=b"{" + b" " * (MAX_BODY + 10) + b"}", headers=auth)[0] == 413
    assert req("PUT", "/x", headers=auth)[0] == 501
    # errors: specific for ApiError, generic (no internals) for anything else
    st, body = req("GET", "/missing", headers=auth)
    assert st == 404 and json.loads(body) == {"error": "no such thing"}
    st, body = req("GET", "/boom", headers=auth)
    assert st == 500 and b"secret" not in body, "internal details leaked"
    # token changes take effect immediately; an empty token locks everything
    token = "u" * 32
    assert req("GET", "/status", headers=auth)[0] == 401
    assert req("GET", "/status", headers={"Authorization": "Bearer " + token})[0] == 200
    token = ""
    assert req("GET", "/status", headers={"Authorization": "Bearer "})[0] == 401
    api.stop()
    assert not api.running
    api2 = LocalApi(dispatch, lambda: "x")
    api2.start(0)
    api3 = LocalApi(dispatch, lambda: "x")
    try:
        api3.start(api2.port)
        raise AssertionError("second server on a taken port should fail")
    except OSError:
        pass
    taken = api2.port
    api2.stop()
    api4 = LocalApi(dispatch, lambda: "x")           # the port is free again right away
    api4.start(taken)
    api4.stop()
    print("OK  api: token required, browser/rebinding requests refused, bad input handled, "
          "no internals leaked, token swap live, port can't be shared, restart works")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
