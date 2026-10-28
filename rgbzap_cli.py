"""rgbzap - control a running RGBZap from the command line.

    rgbzap status                 rgbzap on | off
    rgbzap color red              (a name, #ff8800, or  255 136 0)
    rgbzap brightness 40          rgbzap scene "Movie night"
    rgbzap effect Aurora          rgbzap effect off
    rgbzap sync on|off|toggle     rgbzap audio on|off|toggle
    rgbzap flash green            (a short alert flash, then back to what it was)
    rgbzap scenes | effects       (list what's available)
    rgbzap mcp                    (MCP server so AI assistants can use all of the above)

RGBZap must be running with "Let other programs control RGBZap" turned on (Tools tab).
The port and secret token are read from RGBZap's settings file, so there is nothing to
configure; set RGBZAP_PORT / RGBZAP_TOKEN to point at a different setup.
Exit codes: 0 ok, 1 refused or failed, 2 RGBZap not running / API off.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def settings_path(env=None):
    env = os.environ if env is None else env
    return os.path.join(env.get("APPDATA") or os.path.expanduser("~"), "RGBZap", "settings.json")


def load_connection(env=None):
    """(port, token, api_on) from RGBZAP_PORT/RGBZAP_TOKEN or RGBZap's settings file."""
    env = os.environ if env is None else env
    if env.get("RGBZAP_TOKEN"):
        return int(env.get("RGBZAP_PORT") or 49732), env["RGBZAP_TOKEN"], True
    try:
        with open(settings_path(env), encoding="utf-8") as f:
            s = json.load(f)
        return int(s.get("api_port", 49732)), str(s.get("api_token", "")), bool(s.get("api_on"))
    except (OSError, ValueError):
        return 49732, "", False


def call(method, path, body, port, token, timeout=5, urlopen=urllib.request.urlopen):
    """-> (status, parsed JSON dict). Raises urllib.error.URLError if nothing listens."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def build_request(argv):
    """argv (after the program name) -> (method, path, body), or raises ValueError with
    a usage message."""
    if not argv:
        raise ValueError("what should RGBZap do? try: rgbzap status | on | off | color red")
    cmd, args = argv[0].lower(), argv[1:]
    onoff = lambda a: {"on": True, "off": False, "toggle": "toggle"}.get(a[0].lower()) if a else None
    if cmd == "status":
        return "GET", "/status", None
    if cmd in ("scenes", "effects"):
        return "GET", f"/{cmd}", None
    if cmd in ("on", "off"):
        return "POST", f"/{cmd}", {}
    if cmd == "color":
        if not args:
            raise ValueError("usage: rgbzap color <name | #hex | r g b>")
        return "POST", "/color", {"color": ",".join(args) if len(args) == 3 else " ".join(args)}
    if cmd == "flash":
        if not args:
            raise ValueError("usage: rgbzap flash <color>")
        return "POST", "/flash", {"color": ",".join(args) if len(args) == 3 else " ".join(args)}
    if cmd == "brightness":
        try:
            return "POST", "/brightness", {"value": int(args[0].rstrip("%"))}
        except (IndexError, ValueError):
            raise ValueError("usage: rgbzap brightness <1-100>")
    if cmd == "scene":
        if not args:
            raise ValueError('usage: rgbzap scene "<name>"')
        return "POST", "/scene", {"name": " ".join(args)}
    if cmd == "effect":
        if not args:
            raise ValueError('usage: rgbzap effect "<name>" | off')
        name = " ".join(args)
        return "POST", "/effect", {"name": None if name.lower() == "off" else name}
    if cmd in ("sync", "audio"):
        state = onoff(args)
        if state is None:
            raise ValueError(f"usage: rgbzap {cmd} on|off|toggle")
        return "POST", f"/{cmd}", {"on": state}
    raise ValueError(f"unknown command '{argv[0]}' - run rgbzap with no arguments for the list")


def render(path, payload):
    """Human-friendly text for a reply."""
    if path == "/status":
        lines = [f"RGBZap {payload.get('version', '?')}  -  strip "
                 f"{'connected' if payload.get('connected') else 'not connected'}"]
        for key in ("power", "color", "brightness", "mode", "effect", "scene", "sync", "audio"):
            if key in payload:
                lines.append(f"  {key:<10} {payload[key]}")
        return "\n".join(lines)
    if path in ("/scenes", "/effects"):
        return "\n".join(payload.get("items", []))
    return payload.get("message", "ok")


def main(argv=None, env=None, out=print, urlopen=urllib.request.urlopen):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["mcp"]:                    # MCP server for AI assistants (see rgbzap_mcp)
        import rgbzap_mcp
        return rgbzap_mcp.main()
    raw_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    try:
        method, path, body = build_request(argv)
    except ValueError as e:
        out(f"rgbzap: {e}")
        return 2 if not argv else 1
    port, token, api_on = load_connection(env)
    if not token:
        out("rgbzap: RGBZap's local API isn't set up. Open RGBZap > Tools > Local control "
            "and turn on \"Let other programs control RGBZap\".")
        return 2
    try:
        status, payload = call(method, path, body, port, token, urlopen=urlopen)
    except (urllib.error.URLError, OSError):
        out("rgbzap: can't reach RGBZap. Is it running, and is \"Let other programs control "
            "RGBZap\" on (Tools tab)?")
        return 2
    if status == 401:
        out("rgbzap: RGBZap refused the token - it may have been regenerated. If you "
            "set RGBZAP_TOKEN, update it.")
        return 1
    if status >= 400:
        out(f"rgbzap: {payload.get('error', f'error {status}')}")
        return 1
    out(json.dumps(payload) if raw_json else render(path, payload))
    return 0


def _selftest():
    """python rgbzap_cli.py --selftest (starts a throwaway API on a free port)"""
    # argument parsing
    assert build_request(["status"]) == ("GET", "/status", None)
    assert build_request(["on"]) == ("POST", "/on", {})
    assert build_request(["color", "red"]) == ("POST", "/color", {"color": "red"})
    assert build_request(["color", "255", "136", "0"]) == ("POST", "/color", {"color": "255,136,0"})
    assert build_request(["color", "#ff8800"])[2] == {"color": "#ff8800"}
    assert build_request(["brightness", "40%"]) == ("POST", "/brightness", {"value": 40})
    assert build_request(["scene", "Movie", "night"])[2] == {"name": "Movie night"}
    assert build_request(["effect", "Lava", "lamp"])[2] == {"name": "Lava lamp"}
    assert build_request(["effect", "OFF"])[2] == {"name": None}
    assert build_request(["sync", "toggle"])[2] == {"on": "toggle"}
    assert build_request(["audio", "off"])[2] == {"on": False}
    for bad in ([], ["nope"], ["color"], ["brightness"], ["brightness", "x"], ["scene"],
                ["effect"], ["sync"], ["sync", "maybe"], ["flash"]):
        try:
            build_request(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    # connection details: env wins, then the settings file
    import tempfile
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "RGBZap"))
    with open(os.path.join(d, "RGBZap", "settings.json"), "w") as f:
        json.dump({"api_on": True, "api_port": 50001, "api_token": "abc"}, f)
    assert load_connection({"APPDATA": d}) == (50001, "abc", True)
    assert load_connection({"APPDATA": d, "RGBZAP_TOKEN": "zzz", "RGBZAP_PORT": "6"}) == (6, "zzz", True)
    assert load_connection({"APPDATA": os.path.join(d, "none")}) == (49732, "", False)

    # end to end against a real (throwaway) API server
    from rgbzap_api import ApiError, LocalApi
    seen = []

    def dispatch(method, path, body):
        seen.append((method, path, body))
        if path == "/status":
            return 200, {"version": "9.9.9", "connected": True, "power": "on",
                         "color": "#FF0000", "brightness": 40}
        if path == "/effects":
            return 200, {"items": ["Aurora", "Fireworks"]}
        if path == "/scene":
            raise ApiError(404, f"no scene called {body.get('name')!r}")
        return 200, {"message": "ok"}
    token = "k" * 32
    api = LocalApi(dispatch, lambda: token)
    api.start(0)
    env = {"RGBZAP_TOKEN": token, "RGBZAP_PORT": str(api.port)}
    lines = []
    out = lines.append
    assert main(["color", "red"], env, out) == 0 and lines[-1] == "ok"
    assert seen[-1] == ("POST", "/color", {"color": "red"})
    assert main(["status"], env, out) == 0 and "RGBZap 9.9.9" in lines[-1] and "40" in lines[-1]
    assert main(["effects"], env, out) == 0 and lines[-1] == "Aurora\nFireworks"
    assert main(["status", "--json"], env, out) == 0 and json.loads(lines[-1])["brightness"] == 40
    assert main(["scene", "Nope"], env, out) == 1 and "no scene called 'Nope'" in lines[-1]
    assert main(["brightness", "x"], env, out) == 1, "usage errors exit 1"
    assert main(["color", "red"], {**env, "RGBZAP_TOKEN": "wrong"}, out) == 1
    assert "refused the token" in lines[-1]
    api.stop()
    assert main(["on"], env, out) == 2 and "can't reach RGBZap" in lines[-1], lines[-1]
    assert main(["on"], {"APPDATA": os.path.join(d, "none")}, out) == 2
    assert "isn't set up" in lines[-1]
    print("OK  cli: argument parsing, connection lookup, end-to-end against a real API, "
          "clear messages and exit codes for refused / not running / not set up")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
