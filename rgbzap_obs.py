"""RGBZap's link to OBS Studio, over OBS's built-in WebSocket server (OBS 28 and newer,
obs-websocket protocol v5: Tools > WebSocket Server Settings). No plugin needed.

Read-only: it listens for which scene is live and whether OBS is streaming or recording.
It never sends OBS a command that changes anything.

    link = ObsLink(on_status, on_state, on_scene, on_scenes)
    link.configure("localhost", 4455, password); link.start() ... link.stop()
"""
import asyncio
import base64
import hashlib
import json
import sys
import threading
import uuid

try:
    import websockets
except ImportError:                       # the link is simply unavailable without it
    websockets = None

SUBPROTOCOL = "obswebsocket.json"
SUB_SCENES, SUB_OUTPUTS = 1 << 2, 1 << 6  # event groups we ask OBS to send us


def obs_auth(password, salt, challenge):
    """The obs-websocket v5 password proof: base64(sha256(base64(sha256(password+salt))
    + challenge)). The password itself is never sent."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsLink:
    """Runs on its own thread with its own asyncio loop, reconnecting by itself while
    started (OBS may open after RGBZap). Callbacks run on that thread, so hand work to
    the UI thread:
      on_status(text, connected)   on_state(streaming, recording)
      on_scene(name)               on_scenes([names])"""

    def __init__(self, on_status, on_state, on_scene, on_scenes):
        self.on_status, self.on_state = on_status, on_state
        self.on_scene, self.on_scenes = on_scene, on_scenes
        self.host, self.port, self.password = "localhost", 4455, ""
        self.streaming = self.recording = False
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._ws = None
        self._wake = None
        self._pending = {}

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def configure(self, host, port, password):
        changed = (host, port, password) != (self.host, self.port, self.password)
        self.host, self.port, self.password = host or "localhost", int(port), password or ""
        if changed and self.running:
            self._kick()                  # drop the current session; reconnect with new details

    def start(self):
        if websockets is None:
            self.on_status("The OBS link needs the 'websockets' package", False)
            return
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._kick()

    def _kick(self):
        loop, ws = self._loop, self._ws
        if loop is None:
            return

        def go():
            if self._wake is not None:
                self._wake.set()
            if ws is not None:
                asyncio.ensure_future(ws.close())
        try:
            loop.call_soon_threadsafe(go)
        except RuntimeError:
            pass                          # loop already closed

    # ---- connection loop ----
    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        delay = 2.0
        while not self._stop.is_set():
            try:
                self.on_status("Connecting to OBS...", False)
                await self._session()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_status(self._explain(e), False)
            self._ws = None
            self._set_state(False, False)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            delay = min(delay * 2, 15.0)
        self.on_status("OBS link is off", False)
        self._loop = None

    @staticmethod
    def _explain(e):
        code = getattr(getattr(e, "rcvd", None), "code", None) or getattr(e, "code", None)
        if code == 4009:
            return "OBS rejected the password"
        if isinstance(e, (ConnectionRefusedError, OSError, asyncio.TimeoutError)):
            return "OBS isn't reachable - is it open, with the WebSocket server on?"
        return f"OBS link error: {e}"

    async def _session(self):
        url = f"ws://{self.host}:{self.port}"
        async with websockets.connect(url, subprotocols=[SUBPROTOCOL], open_timeout=4,
                                      ping_interval=20, max_size=2 ** 22) as ws:
            self._ws = ws
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if hello.get("op") != 0:
                raise RuntimeError("not an OBS WebSocket server")
            ident = {"rpcVersion": 1, "eventSubscriptions": SUB_SCENES | SUB_OUTPUTS}
            auth = hello["d"].get("authentication")
            if auth:
                if not self.password:
                    self.on_status("OBS needs a password - enter it above", False)
                    return
                ident["authentication"] = obs_auth(self.password, auth["salt"],
                                                   auth["challenge"])
            await ws.send(json.dumps({"op": 1, "d": ident}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if reply.get("op") != 2:
                raise RuntimeError("OBS did not accept the connection")
            self.on_status("Connected to OBS", True)
            asyncio.ensure_future(self._initial(ws))
            async for raw in ws:
                self._handle(json.loads(raw))
            raise ConnectionResetError("OBS closed the connection")

    async def _request(self, ws, rtype, data=None, timeout=4.0):
        rid = uuid.uuid4().hex[:8]
        fut = self._loop.create_future()
        self._pending[rid] = fut
        d = {"requestType": rtype, "requestId": rid}
        if data:
            d["requestData"] = data
        await ws.send(json.dumps({"op": 6, "d": d}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def _initial(self, ws):
        """What's already happening when we connect: scene list, live scene, outputs."""
        try:
            scenes = (await self._request(ws, "GetSceneList")).get("responseData", {})
            self.on_scenes([s["sceneName"] for s in reversed(scenes.get("scenes", []))])
            cur = (await self._request(ws, "GetCurrentProgramScene")).get("responseData", {})
            name = cur.get("sceneName") or cur.get("currentProgramSceneName")
            if name:
                self.on_scene(name)
            stream = (await self._request(ws, "GetStreamStatus")).get("responseData", {})
            rec = (await self._request(ws, "GetRecordStatus")).get("responseData", {})
            self._set_state(bool(stream.get("outputActive")), bool(rec.get("outputActive")))
        except Exception:
            pass                          # events will still keep us up to date

    def _handle(self, msg):
        op, d = msg.get("op"), msg.get("d", {})
        if op == 7:                       # a reply to one of our requests
            fut = self._pending.get(d.get("requestId"))
            if fut is not None and not fut.done():
                fut.set_result(d)
        elif op == 5:                     # an event
            kind, data = d.get("eventType"), d.get("eventData", {})
            if kind == "CurrentProgramSceneChanged" and data.get("sceneName"):
                self.on_scene(data["sceneName"])
            elif kind == "SceneListChanged":
                self.on_scenes([s["sceneName"] for s in reversed(data.get("scenes", []))])
            elif kind == "StreamStateChanged":
                self._set_state(bool(data.get("outputActive")), self.recording)
            elif kind == "RecordStateChanged":
                self._set_state(self.streaming, bool(data.get("outputActive")))

    def _set_state(self, streaming, recording):
        if (streaming, recording) != (self.streaming, self.recording):
            self.streaming, self.recording = streaming, recording
            self.on_state(streaming, recording)


def _selftest():
    """python rgbzap_obs.py --selftest  (talks to a fake OBS on localhost)"""
    # the documented example from the obs-websocket protocol spec
    assert obs_auth("supersecretpassword", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
                    "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=") == \
        "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=", "auth proof differs from the spec"
    print("OK  obs: password proof matches the protocol spec's example")
    if websockets is None:
        print("SKIP obs: websockets not installed")
        return
    from websockets.asyncio.server import serve

    async def scenario():
        salt, challenge = "salty", "chal"
        script = {"password": "", "events": []}
        seen = {"identify": None, "requests": []}

        async def fake_obs(ws):
            try:
                await fake_session(ws)
            except websockets.exceptions.ConnectionClosed:
                pass                              # a client leaving early is normal

        async def fake_session(ws):
            d = {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}
            if script["password"]:
                d["authentication"] = {"salt": salt, "challenge": challenge}
            await ws.send(json.dumps({"op": 0, "d": d}))
            ident = json.loads(await ws.recv())["d"]
            seen["identify"] = ident
            if script["password"] and ident.get("authentication") != \
                    obs_auth(script["password"], salt, challenge):
                await ws.close(4009, "Authentication failed")
                return
            await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
            answers = {
                "GetSceneList": {"scenes": [{"sceneName": "Gameplay"}, {"sceneName": "Starting"}]},
                "GetCurrentProgramScene": {"sceneName": "Starting"},
                "GetStreamStatus": {"outputActive": False},
                "GetRecordStatus": {"outputActive": True},
            }
            async def push_events():              # after the app's startup questions
                for ev in script["events"]:
                    await asyncio.sleep(0.15)
                    await ws.send(json.dumps({"op": 5, "d": ev}))
            async for raw in ws:
                m = json.loads(raw)
                if m["op"] == 6:
                    seen["requests"].append(m["d"]["requestType"])
                    await ws.send(json.dumps({"op": 7, "d": {
                        "requestType": m["d"]["requestType"], "requestId": m["d"]["requestId"],
                        "requestStatus": {"result": True, "code": 100},
                        "responseData": answers.get(m["d"]["requestType"], {})}}))
                    if m["d"]["requestType"] == "GetRecordStatus":    # the last one
                        asyncio.ensure_future(push_events())

        async with serve(fake_obs, "127.0.0.1", 0, subprotocols=[SUBPROTOCOL]) as server:
            port = server.sockets[0].getsockname()[1]

            def make():
                log = {"status": [], "state": [], "scene": [], "scenes": []}
                link = ObsLink(lambda t, c: log["status"].append((t, c)),
                               lambda s, r: log["state"].append((s, r)),
                               log["scene"].append, log["scenes"].append)
                return link, log

            async def wait_for(cond, secs=4.0):
                for _ in range(int(secs / 0.05)):
                    if cond():
                        return True
                    await asyncio.sleep(0.05)
                return False

            # 1) no password: scene list, live scene and output state arrive, then events
            script.update(password="", events=[
                {"eventType": "CurrentProgramSceneChanged", "eventData": {"sceneName": "Gameplay"}},
                {"eventType": "StreamStateChanged", "eventData": {"outputActive": True}},
                {"eventType": "RecordStateChanged", "eventData": {"outputActive": False}}])
            link, log = make()
            link.configure("127.0.0.1", port, "")
            link.start()
            assert await wait_for(lambda: log["state"] and log["state"][-1] == (True, False)), log
            assert sorted(log["scenes"][0]) == ["Gameplay", "Starting"], log["scenes"]
            assert log["scene"][0] == "Starting" and log["scene"][-1] == "Gameplay", log["scene"]
            assert (False, True) in log["state"], "initial recording state missed"
            assert ("Connected to OBS", True) in log["status"], log["status"]
            assert seen["identify"]["eventSubscriptions"] == SUB_SCENES | SUB_OUTPUTS
            assert "authentication" not in seen["identify"]
            assert not any(r not in ("GetSceneList", "GetCurrentProgramScene",
                                     "GetStreamStatus", "GetRecordStatus")
                           for r in seen["requests"]), f"sent a non-read request: {seen['requests']}"
            link.stop()
            assert await wait_for(lambda: not link.running), "link did not stop"

            # 2) password: right one connects, wrong one is reported clearly
            script.update(password="hunter2", events=[])
            link, log = make()
            link.configure("127.0.0.1", port, "hunter2")
            link.start()
            assert await wait_for(lambda: ("Connected to OBS", True) in log["status"]), log
            assert seen["identify"]["authentication"] == obs_auth("hunter2", salt, challenge)
            link.stop()
            link, log = make()
            link.configure("127.0.0.1", port, "wrong")
            link.start()
            assert await wait_for(lambda: any("password" in t for t, _ in log["status"])), log
            assert not any(c for _t, c in log["status"]), "connected with a wrong password"
            link.stop()
            link, log = make()
            link.configure("127.0.0.1", port, "")
            link.start()
            assert await wait_for(lambda: any("needs a password" in t for t, _ in log["status"])), log
            link.stop()

        # 3) nothing listening: a plain-language message, no crash, reconnect keeps trying
        link, log = make()
        link.configure("127.0.0.1", port, "")               # server is closed now
        link.start()
        assert await wait_for(lambda: any("isn't reachable" in t for t, _ in log["status"])), log
        link.stop()

    asyncio.run(scenario())
    print("OK  obs: fake OBS - scenes, live scene, stream/record state, events, password "
          "right/wrong/missing, unreachable; only read-only requests sent")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
