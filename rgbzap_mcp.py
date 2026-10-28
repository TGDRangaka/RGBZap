"""rgbzap mcp - an MCP server (stdio) so AI assistants can control the lights.

    claude mcp add rgbzap -- "<RGBZap folder>\\cli\\rgbzap.exe" mcp

It is a thin layer over the rgbzap command line: each tool becomes the matching `rgbzap`
command and goes through RGBZap's local API (same port, same secret token, same limits),
so it works only while RGBZap runs with "Let other programs control RGBZap" on.
MCP over stdio is newline-delimited JSON-RPC 2.0, small enough to speak without an SDK.
"""
import json
import sys

import rgbzap_cli

PROTOCOL = "2025-06-18"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOLS_VERSION = "1.0"     # of this tool set; bump when tools change (not the app version: importing
                          # elk_gui here would drag the whole GUI into the small CLI exe)

_STR = {"type": "string"}
_STATE = {"type": "string", "enum": ["on", "off", "toggle"]}


def _tool(name, desc, props, required, argv):
    return {"name": name, "description": desc, "argv": argv,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    _tool("rgbzap_status", "Current state of the LED strip: connected, power, color, "
          "brightness, effect, scene, screen sync, audio.", {}, [], lambda a: ["status"]),
    _tool("rgbzap_list", "List the scenes or effects that exist, so you can pick a valid name.",
          {"what": {"type": "string", "enum": ["scenes", "effects"]}}, ["what"],
          lambda a: [a["what"]]),
    _tool("rgbzap_power", "Turn the strip on or off.", {"on": {"type": "boolean"}}, ["on"],
          lambda a: ["on" if a["on"] else "off"]),
    _tool("rgbzap_color", "Set a solid color: a name (red, warm white...), #rrggbb, or 'r g b' "
          "(0-255).", {"color": _STR}, ["color"], lambda a: ["color", *str(a["color"]).split()]),
    _tool("rgbzap_brightness", "Set brightness, 1-100 percent.",
          {"value": {"type": "integer", "minimum": 1, "maximum": 100}}, ["value"],
          lambda a: ["brightness", str(a["value"])]),
    _tool("rgbzap_scene", "Apply a saved scene (color + brightness + effect) by name.",
          {"name": _STR}, ["name"], lambda a: ["scene", str(a["name"])]),
    _tool("rgbzap_effect", "Start an effect by name (see rgbzap_list), or 'off' to stop the "
          "current one.", {"name": _STR}, ["name"], lambda a: ["effect", str(a["name"])]),
    _tool("rgbzap_flash", "A short alert flash in a color, then the strip returns to what it "
          "was showing. Good for 'tell me when this finishes'.", {"color": _STR}, ["color"],
          lambda a: ["flash", *str(a["color"]).split()]),
    _tool("rgbzap_sync", "Screen sync (Ambilight) on, off or toggle.", {"state": _STATE},
          ["state"], lambda a: ["sync", str(a["state"])]),
    _tool("rgbzap_audio", "Audio-reactive lighting on, off or toggle.", {"state": _STATE},
          ["state"], lambda a: ["audio", str(a["state"])]),
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def call_tool(name, args, env=None, urlopen=None):
    """-> (text, is_error). Runs the matching rgbzap command."""
    tool = _BY_NAME.get(name)
    if tool is None:
        return f"unknown tool '{name}'", True
    lines = []
    kw = {"urlopen": urlopen} if urlopen else {}
    try:
        argv = tool["argv"](args or {})
    except (KeyError, TypeError):
        return f"missing or invalid arguments for {name}", True
    code = rgbzap_cli.main(argv, env, lines.append, **kw)
    return "\n".join(lines) or "ok", code != 0


def handle(msg, env=None, urlopen=None):
    """One JSON-RPC message -> reply dict, or None for notifications."""
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:                                   # notification (initialized, cancelled...)
        return None
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return ok({"protocolVersion": want if want in SUPPORTED else PROTOCOL,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "rgbzap", "version": TOOLS_VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                             for t in TOOLS]})
    if method == "tools/call":
        p = msg.get("params") or {}
        text, is_error = call_tool(p.get("name"), p.get("arguments"), env, urlopen)
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None, env=None, urlopen=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            reply = handle(msg, env, urlopen) if isinstance(msg, dict) else None
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0


def main():
    for s in (sys.stdin, sys.stdout):                 # JSON is UTF-8 whatever the console says
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    return serve()


def add_command(exe, script=None):
    """The one-liner that registers this server with Claude Code (exe = the rgbzap exe, or
    python plus script when running from source)."""
    target = f'"{exe}" "{script}"' if script else f'"{exe}"'
    return f"claude mcp add rgbzap -- {target} mcp"


def _selftest():
    """python rgbzap_mcp.py --selftest (talks JSON-RPC to the server, with a real throwaway API)"""
    import io
    from rgbzap_api import ApiError, LocalApi
    seen = []

    def dispatch(method, path, body):
        seen.append((method, path, body))
        if path == "/status":
            return 200, {"version": "9.9.9", "connected": True, "power": "on"}
        if path == "/scene" and body.get("name") == "Nope":
            raise ApiError(404, f"no scene called {body.get('name')!r}")
        return 200, {"message": "ok"}
    token = "m" * 32
    api = LocalApi(dispatch, lambda: token)
    api.start(0)
    env = {"RGBZAP_TOKEN": token, "RGBZAP_PORT": str(api.port)}

    def rpc(*msgs):
        out = io.StringIO()
        serve(io.StringIO("\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs)
                          + "\n"), out, env)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def call(tool, **args):
        r = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                 "params": {"name": tool, "arguments": args}})[0]["result"]
        return r["content"][0]["text"], r["isError"]

    init, listing = rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},       # no reply
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert init["result"]["protocolVersion"] == "2024-11-05" and init["id"] == 1
    assert init["result"]["serverInfo"]["name"] == "rgbzap"
    names = [t["name"] for t in listing["result"]["tools"]]
    assert names == [t["name"] for t in TOOLS] and len(names) == 10, names
    assert all("argv" not in t and t["inputSchema"]["type"] == "object"
               for t in listing["result"]["tools"])
    assert rpc({"jsonrpc": "2.0", "id": 3, "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"}})[0]["result"]["protocolVersion"] == PROTOCOL
    assert rpc({"jsonrpc": "2.0", "id": 4, "method": "ping"})[0]["result"] == {}
    assert rpc({"jsonrpc": "2.0", "id": 5, "method": "nope"})[0]["error"]["code"] == -32601
    bad = rpc("{not json", "[1]", "", {"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert bad[0]["error"]["code"] == -32700 and bad[1]["id"] == 6, bad

    # every tool lands on the right API request
    for tool, args, want in (
            ("rgbzap_power", {"on": True}, ("POST", "/on", {})),
            ("rgbzap_power", {"on": False}, ("POST", "/off", {})),
            ("rgbzap_color", {"color": "warm white"}, ("POST", "/color", {"color": "warm white"})),
            ("rgbzap_color", {"color": "255 136 0"}, ("POST", "/color", {"color": "255,136,0"})),
            ("rgbzap_brightness", {"value": 35}, ("POST", "/brightness", {"value": 35})),
            ("rgbzap_scene", {"name": "Movie night"}, ("POST", "/scene", {"name": "Movie night"})),
            ("rgbzap_effect", {"name": "Lava lamp"}, ("POST", "/effect", {"name": "Lava lamp"})),
            ("rgbzap_effect", {"name": "off"}, ("POST", "/effect", {"name": None})),
            ("rgbzap_flash", {"color": "green"}, ("POST", "/flash", {"color": "green"})),
            ("rgbzap_sync", {"state": "toggle"}, ("POST", "/sync", {"on": "toggle"})),
            ("rgbzap_audio", {"state": "off"}, ("POST", "/audio", {"on": False}))):
        text, err = call(tool, **args)
        assert not err and seen[-1] == want, (tool, args, seen[-1], text)
    text, err = call("rgbzap_status")
    assert not err and "RGBZap 9.9.9" in text and "connected" in text
    # failures come back as tool errors the model can read, not protocol errors
    text, err = call("rgbzap_scene", name="Nope")
    assert err and "no scene called 'Nope'" in text, text
    text, err = call("rgbzap_brightness", value="loud")
    assert err and "usage" in text, text
    text, err = call("rgbzap_color")                              # missing argument
    assert err and "missing" in text, text
    text, err = call("rgbzap_bogus")
    assert err and "unknown tool" in text
    api.stop()
    text, err = call("rgbzap_power", on=True)                     # RGBZap not running
    assert err and "can't reach RGBZap" in text, text
    assert add_command("C:/x/cli/rgbzap.exe") == 'claude mcp add rgbzap -- "C:/x/cli/rgbzap.exe" mcp'
    assert add_command("py.exe", "a.py").endswith('"py.exe" "a.py" mcp')
    print("OK  mcp: handshake, 10 tools, every tool hits the right API call, bad input and "
          "'RGBZap not running' come back as readable tool errors, junk input can't kill it")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
