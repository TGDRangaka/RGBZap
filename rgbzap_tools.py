"""RGBZap's TOOLS tab: global hotkeys, the On-Air light, the OBS link, the local control
API, and the strip's color balance.

The app hands this module itself (`app`) and its drawing theme (`app.ui`); this module
never imports the main program. Everything that touches the lights goes through a few
App methods (app.set_color, app.on_off, app.apply_scene, ...), always on the Tk thread:
background threads (hotkeys, camera watcher, OBS, the API server) hop over with
root.after().
"""
import os
import sys
import threading
import time
import tkinter as tk

import rgbzap_api as api_mod
import rgbzap_hotkeys as hotkeys
import rgbzap_obs as obs_mod
import rgbzap_onair as onair_mod

_MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                     "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
                     "Caps_Lock", "Num_Lock", "Scroll_Lock", "ISO_Level3_Shift"}
_KEYSYM_NAMES = {"Prior": "pageup", "Next": "pagedown", "Home": "home", "End": "end",
                 "Insert": "insert", "Delete": "delete", "Up": "up", "Down": "down",
                 "Left": "left", "Right": "right", "space": "space", "Tab": "tab",
                 "Return": "enter"}

POST_PATHS = {"/on", "/off", "/color", "/flash", "/brightness", "/scene", "/effect",
              "/sync", "/audio"}


def combo_from_event(state, keysym):
    """A Tk key event (state bits + keysym) -> 'ctrl+alt+pageup' text, or None while only a
    modifier is held or the key isn't usable. Tk on Windows: Control 0x4, Shift 0x1,
    Alt 0x20000 (0x8 elsewhere)."""
    if keysym in _MODIFIER_KEYSYMS:
        return None
    key = _KEYSYM_NAMES.get(keysym)
    if key is None:
        k = keysym.lower()
        if len(k) == 1 and k.isalnum():
            key = k
        elif k.startswith("f") and k[1:].isdigit():
            key = k
        elif k.startswith("kp_") and k[3:].isdigit() and len(k) == 4:
            key = "numpad" + k[3:]
        else:
            return None
    parts = [n for n, bit in (("ctrl", 0x4), ("alt", 0x20000 | 0x8), ("shift", 0x1))
             if state & bit]
    return "+".join(parts + [key])


class ToolsTab:
    def __init__(self, app, parent, cfg):
        self.app, self.ui, self.root = app, app.ui, app.root
        # ---- hotkeys
        self.hk_on, self.hk = cfg["hotkeys_on"], dict(cfg["hotkeys"])
        self.hotkey_mgr = hotkeys.HotkeyManager(
            lambda action: self.root.after(0, self.app.run_hotkey, action))
        # ---- On-Air: webcam / microphone, plus OBS going live
        self.onair_enabled, self.onair_cam, self.onair_mic = (
            cfg["onair"], cfg["onair_cam"], cfg["onair_mic"])
        self._caps = {"cam": False, "mic": False}
        self._obs_state = (False, False)               # (streaming, recording)
        self._test_until = 0.0
        self.watcher = onair_mod.CapabilityWatcher(
            lambda s: self.root.after(0, self._caps_update, s))
        # ---- OBS link
        self.obs_on, self.obs_host, self.obs_port = cfg["obs_on"], cfg["obs_host"], cfg["obs_port"]
        self.obs_password = cfg["obs_password"]
        self.obs_live, self.obs_rec = cfg["obs_live"], cfg["obs_rec"]
        self.obs_scenes = dict(cfg["obs_scenes"])      # OBS scene -> RGBZap scene
        self.obs_scene_names = []
        self.obs_connected = False
        self.obs = obs_mod.ObsLink(
            lambda t, c: self.root.after(0, self._obs_status, t, c),
            lambda s, r: self.root.after(0, self._obs_state_changed, s, r),
            lambda n: self.root.after(0, self._obs_scene, n),
            lambda names: self.root.after(0, self._obs_scene_list, names))
        # ---- local API
        self.api_on, self.api_port, self.api_token = cfg["api_on"], cfg["api_port"], cfg["api_token"]
        self.api = api_mod.LocalApi(self._api_dispatch, lambda: self.api_token)
        self._build(parent)

    # ==================================================================== UI
    def _check(self, parent, text, var, cmd, indent=0, help=None):
        ui = self.ui
        row = tk.Frame(parent, bg=ui.PANEL)
        row.pack(fill="x")
        tk.Checkbutton(row, text=text, variable=var, command=cmd, anchor="w",
                       font=ui.FONT_BODY, fg=ui.TEXT, bg=ui.PANEL, selectcolor=ui.PANEL2,
                       activebackground=ui.PANEL, activeforeground=ui.TEXT,
                       highlightthickness=0, bd=0).pack(side="left", padx=(indent, 0))
        if help:
            self.app._help(row, help).pack(side="left", padx=(6, 0))
        return row

    def _entry(self, parent, value, width, show=None):
        ui = self.ui
        e = tk.Entry(parent, width=width, font=ui.FONT_MONO, bg=ui.PANEL2, fg=ui.TEXT,
                     insertbackground=ui.TEXT, relief="flat", bd=0, show=show or "",
                     highlightthickness=1, highlightbackground=ui.LINE,
                     highlightcolor=ui.MUTE)
        e.insert(0, value)
        return e

    def _label(self, parent, text, muted=True, mono=False):
        ui = self.ui
        return tk.Label(parent, text=text, font=ui.FONT_MONO if mono else ui.FONT_BODY,
                        fg=ui.MUTE if muted else ui.TEXT, bg=ui.PANEL, anchor="w")

    def _build(self, p):
        app, ui = self.app, self.ui

        # ---- Hotkeys
        app._heading(p, "Hotkeys", pady=(0, 4), help=(
            "Press these keys from anywhere, even in a full-screen game, to control the "
            "lights. RGBZap only listens for these exact combinations and never sees "
            "anything else you type. If one doesn't work, another program is probably "
            "using it: press Change and pick a different combination."))
        self.hk_var = tk.BooleanVar(value=self.hk_on)
        self._check(p, "Enable global hotkeys", self.hk_var, self.on_hotkeys_toggle)
        grid = tk.Frame(p, bg=ui.PANEL)
        grid.pack(fill="x", pady=(4, 0))
        self.hk_labels = {}
        for i, (action, (label, _default)) in enumerate(ui.HOTKEY_ACTIONS.items()):
            self._label(grid, label, muted=False).grid(row=i, column=0, sticky="w", pady=2)
            lb = self._label(grid, "", mono=True)
            lb.grid(row=i, column=1, sticky="e", padx=(8, 8))
            self.hk_labels[action] = lb
            app._flat(grid, "Change", lambda a=action: self.change_hotkey(a), pady=2
                      ).grid(row=i, column=2, pady=1)
        grid.columnconfigure(0, weight=1)
        self.hk_note = tk.Label(p, text="", font=ui.FONT_BODY, fg=ui.BAD, bg=ui.PANEL,
                                anchor="w", justify="left", wraplength=360)
        self.hk_note.pack(fill="x")
        self._refresh_hotkey_labels({})

        # ---- On-Air
        app._heading(p, "On-Air light", pady=(18, 4), help=(
            "Turns the strip red while your webcam (and, if you tick it, your microphone) "
            "is in use, so people around you know you're live on a call or stream. "
            "RGBZap only asks Windows whether the camera or mic is switched on. It never "
            "uses the camera, hears audio, or sees which app is using them. When it "
            "stops, the strip goes back to what it was showing."))
        self.onair_var = tk.BooleanVar(value=self.onair_enabled)
        self._check(p, "Show red while I'm on air", self.onair_var, self.on_onair_toggle)
        self.cam_var = tk.BooleanVar(value=self.onair_cam)
        self.mic_var = tk.BooleanVar(value=self.onair_mic)
        self._check(p, "when my webcam is on", self.cam_var, self.on_onair_opts, indent=22)
        self._check(p, "when my microphone is on", self.mic_var, self.on_onair_opts, indent=22,
                    help="Off by default: voice chat apps like Discord use the microphone "
                         "all the time, which would keep the strip red while you game.")
        trow = tk.Frame(p, bg=ui.PANEL)
        trow.pack(fill="x", pady=(6, 0))
        app._flat(trow, "Test for 3 seconds", self.test_onair, pady=4).pack(side="left")

        # ---- OBS link
        app._heading(p, "OBS link", pady=(18, 4), help=(
            "Connects to OBS Studio (version 28 or newer) so the strip can follow your "
            "stream: red while you're live or recording, and a different look for each OBS "
            "scene. In OBS turn on Tools > WebSocket Server Settings > Enable WebSocket "
            "server. RGBZap only listens; it can't change anything in OBS."))
        self.obs_var = tk.BooleanVar(value=self.obs_on)
        self._check(p, "Connect to OBS", self.obs_var, self.on_obs_toggle)
        row = tk.Frame(p, bg=ui.PANEL)
        row.pack(fill="x", pady=(6, 0))
        self._label(row, "Host").pack(side="left")
        self.host_e = self._entry(row, self.obs_host, 13)
        self.host_e.pack(side="left", padx=(6, 0), ipady=3)
        app._help(row, "Where OBS is running. Leave as localhost when OBS is on this "
                       "same PC.").pack(side="left", padx=(6, 12))
        self._label(row, "Port").pack(side="left")
        self.port_e = self._entry(row, str(self.obs_port), 5)
        self.port_e.pack(side="left", padx=(6, 0), ipady=3)
        app._help(row, "The WebSocket server port shown in OBS. OBS's default is 4455."
                  ).pack(side="left", padx=(6, 0))
        row2 = tk.Frame(p, bg=ui.PANEL)
        row2.pack(fill="x", pady=(6, 0))
        self._label(row2, "Password").pack(side="left")
        self.pw_e = self._entry(row2, self.obs_password, 22, show="•")
        self.pw_e.pack(side="left", padx=(6, 0), ipady=3)
        app._help(row2, "The password from OBS's WebSocket Server Settings. Leave empty if "
                        "you didn't set one. It's kept in RGBZap's settings on this PC and "
                        "is never sent in the clear (OBS only gets a one-way proof of it)."
                  ).pack(side="left", padx=(6, 0))
        for e in (self.host_e, self.port_e, self.pw_e):
            e.bind("<Return>", lambda _e: self._obs_apply())
            e.bind("<FocusOut>", lambda _e: self._obs_apply())
        self.obs_status = tk.Label(p, text="●  OBS link is off", font=ui.FONT_BODY,
                                   fg=ui.MUTE, bg=ui.PANEL, anchor="w")
        self.obs_status.pack(fill="x", pady=(6, 2))
        self.live_var = tk.BooleanVar(value=self.obs_live)
        self.rec_var = tk.BooleanVar(value=self.obs_rec)
        self._check(p, "Red On-Air light while I'm streaming", self.live_var, self.on_obs_opts,
                    help="The same red light as above, switched on by OBS going live.")
        self._check(p, "Red On-Air light while I'm recording", self.rec_var, self.on_obs_opts)
        srow = tk.Frame(p, bg=ui.PANEL)
        srow.pack(fill="x", pady=(6, 0))
        app._flat(srow, "Scene lighting…", self.edit_obs_scenes, pady=4).pack(side="left")
        app._help(srow, "Pick a RGBZap scene (colors, brightness, effect) for each OBS "
                        "scene. When you switch scenes in OBS, the lights follow."
                  ).pack(side="left", padx=(8, 0))

        # ---- Local control
        app._heading(p, "Local control", pady=(18, 4), help=(
            "Lets other programs on THIS PC control the lights: the rgbzap command line, "
            "Stream Deck, AutoHotkey, OBS scripts, build scripts. It's off until you turn "
            "it on, only reachable from this PC (not your network, not web pages), and "
            "every request needs the secret token below. It can only do what the buttons "
            "in RGBZap can do."))
        self.api_var = tk.BooleanVar(value=self.api_on)
        self._check(p, "Let other programs control RGBZap", self.api_var, self.on_api_toggle)
        self.api_status = tk.Label(p, text="", font=ui.FONT_BODY, fg=ui.MUTE, bg=ui.PANEL,
                                   anchor="w", justify="left", wraplength=360)
        self.api_status.pack(fill="x", pady=(4, 2))
        trow2 = tk.Frame(p, bg=ui.PANEL)
        trow2.pack(fill="x", pady=(2, 0))
        app._flat(trow2, "Copy token", self.copy_token, pady=4).pack(side="left")
        app._flat(trow2, "New token", self.new_token, pady=4).pack(side="left", padx=(6, 0))
        app._help(trow2, "The token is the password other programs must send. 'New token' "
                         "replaces it and locks out anything using the old one. The rgbzap "
                         "command line finds the token on its own."
                  ).pack(side="left", padx=(8, 0))
        mrow = tk.Frame(p, bg=ui.PANEL)
        mrow.pack(fill="x", pady=(4, 0))
        app._flat(mrow, "Copy AI assistant setup", self.copy_mcp_command, pady=4).pack(side="left")
        app._help(mrow, "Lets an AI assistant such as Claude control your lights (scenes, "
                        "colors, effects, brightness). Copies a one-line command; paste it "
                        "in a terminal to add RGBZap to Claude Code. For other apps, use "
                        "'rgbzap mcp' as the server command. Needs the switch above."
                  ).pack(side="left", padx=(8, 0))
        self._refresh_api_status()

        # ---- Strip color balance
        app._heading(p, "Strip color balance", pady=(18, 4), help=(
            "Cheap strips often make a 'white' that looks pink or blue, which tints "
            "everything, including screen sync. Lower the channel that's too strong until "
            "the white test looks neutral. 100% leaves a channel untouched. It applies to "
            "every color and mode."))
        self.bal_scales = []
        for idx, name in enumerate(("Red", "Green", "Blue")):
            r = tk.Frame(p, bg=ui.PANEL)
            r.pack(fill="x")
            lbl = self._label(r, name)
            lbl.config(width=6)                       # same width, so the sliders line up
            lbl.pack(side="left", padx=(0, 4))
            holder = tk.Frame(r, bg=ui.PANEL)
            holder.pack(side="left", fill="x", expand=True)
            self.bal_scales.append(app._gauge(
                holder, 50, 100, lambda v, i=idx: app.set_balance(i, float(v)),
                app.balance_pct[idx], "%"))
        brow = tk.Frame(p, bg=ui.PANEL)
        brow.pack(fill="x", pady=(6, 0))
        app._flat(brow, "White test", lambda: app.set_color((255, 255, 255)), pady=4
                  ).pack(side="left")
        app._flat(brow, "Reset", self.reset_balance, pady=4).pack(side="left", padx=(6, 0))

    # ============================================================== hotkeys
    def _refresh_hotkey_labels(self, failed):
        ui = self.ui
        for action, lb in self.hk_labels.items():
            combo = hotkeys.pretty(self.hk.get(action, ""))
            lb.config(text=combo or "not set",
                      fg=ui.BAD if action in failed else ui.TEXT if combo else ui.MUTE)
        self.hk_note.config(text="".join(
            f"{ui.HOTKEY_ACTIONS[a][0]}: {why}. Press Change to pick another.\n"
            for a, why in failed.items()).strip())

    def _register_hotkeys(self):
        if self.hk_on:
            failed = self.hotkey_mgr.start(self.hk)
        else:
            self.hotkey_mgr.stop()
            failed = {}
        self._refresh_hotkey_labels(failed)
        return failed

    def on_hotkeys_toggle(self):
        self.hk_on = self.hk_var.get()
        self._register_hotkeys()
        self.app.set_status("Global hotkeys on" if self.hk_on else "Global hotkeys off")

    def _set_hotkey(self, action, combo):
        """-> (ok, message). Refuses duplicates and shortcuts Windows won't give us."""
        if combo:
            for other, c in self.hk.items():
                if other != action and hotkeys.normalize(c) == combo:
                    return False, f"Already used for “{self.ui.HOTKEY_ACTIONS[other][0]}”"
        old = self.hk[action]
        self.hk[action] = combo
        if self.hk_on:
            failed = self._register_hotkeys()
            if action in failed:
                reason = failed[action]
                self.hk[action] = old
                self._register_hotkeys()
                return False, reason[:1].upper() + reason[1:]
        else:
            self._refresh_hotkey_labels({})
        return True, ""

    def change_hotkey(self, action):
        ui = self.ui
        top = tk.Toplevel(self.root)
        top.title("Change shortcut")
        top.configure(bg=ui.PANEL, padx=24, pady=20)
        top.transient(self.root)
        top.resizable(False, False)
        tk.Label(top, text=f"Press the new shortcut for\n“{ui.HOTKEY_ACTIONS[action][0]}”",
                 font=ui.FONT_BODY, fg=ui.TEXT, bg=ui.PANEL, justify="center").pack()
        tk.Label(top, text="Hold Ctrl or Alt and press a key.\nEsc cancels · Backspace clears it.",
                 font=ui.FONT_BODY, fg=ui.MUTE, bg=ui.PANEL, justify="center").pack(pady=(10, 0))
        err = tk.Label(top, text="", font=ui.FONT_BODY, fg=ui.BAD, bg=ui.PANEL)
        err.pack(pady=(8, 0))

        def on_key(e):
            if e.keysym == "Escape":
                top.destroy()
                return
            if e.keysym == "BackSpace":
                self._set_hotkey(action, "")
                top.destroy()
                return
            raw = combo_from_event(e.state, e.keysym)
            if raw is None:
                return                                    # only a modifier so far
            combo = hotkeys.normalize(raw)
            if not combo:
                err.config(text="Needs Ctrl or Alt (or Win) plus one key")
                return
            ok, msg = self._set_hotkey(action, combo)
            if ok:
                top.destroy()
            else:
                err.config(text=msg)
        top.bind("<KeyPress>", on_key)
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + 120
        top.geometry(f"+{max(0, x)}+{y}")
        top.grab_set()
        top.focus_force()

    # =============================================================== On-Air
    def _want_onair(self):
        cam_mic = self.onair_enabled and ((self.onair_cam and self._caps["cam"])
                                          or (self.onair_mic and self._caps["mic"]))
        obs = self.obs_on and ((self.obs_live and self._obs_state[0])
                               or (self.obs_rec and self._obs_state[1]))
        return bool(cam_mic or obs or self._test_until > time.monotonic())

    def _onair_update(self):
        """Light or release the red On-Air light to match what's happening. Safe to call
        as often as you like (it's also a heartbeat)."""
        ble = self.app.ble
        want = self._want_onair()
        ble.onair_on = want
        running = ble._alert_on and ble._alert_kind == "onair"
        if want and not running and ble._connected():
            ble.submit(ble._alert("onair", self.app.brightness, max_s=self.ui.ONAIR_MAX_S))

    def _caps_update(self, state):
        self._caps = state
        self._onair_update()

    def on_strip_connected(self):
        self._onair_update()

    def on_onair_toggle(self):
        self.onair_enabled = self.onair_var.get()
        if self.onair_enabled:
            self.watcher.start()
        else:
            self.watcher.stop()
            self._caps = {"cam": False, "mic": False}
        self._onair_update()
        self.app.set_status("On-Air light on" if self.onair_enabled else "On-Air light off")

    def toggle_onair(self):
        """From the tray menu."""
        self.onair_var.set(not self.onair_enabled)
        self.on_onair_toggle()

    def on_onair_opts(self):
        self.onair_cam, self.onair_mic = self.cam_var.get(), self.mic_var.get()
        self._onair_update()

    def test_onair(self):
        if not self.app.ble._connected():
            self.app.set_status("Connect a light strip first")
            return
        self._test_until = time.monotonic() + 3.0
        self._onair_update()
        self.root.after(3300, self._onair_update)         # release it after the test
        self.app.set_status("On-Air test: red for 3 seconds")

    # ================================================================== OBS
    def _obs_apply(self):
        """Read host / port / password from the fields and (re)connect with them."""
        host = self.host_e.get().strip() or "localhost"
        try:
            port = int(self.port_e.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.port_e.delete(0, "end")
            self.port_e.insert(0, str(self.obs_port))
            self.app.set_status("OBS port must be a number from 1 to 65535")
            return
        self.obs_host, self.obs_port, self.obs_password = host, port, self.pw_e.get()
        self.obs.configure(self.obs_host, self.obs_port, self.obs_password)

    def on_obs_toggle(self):
        self.obs_on = self.obs_var.get()
        if self.obs_on:
            self._obs_apply()
            self.obs.start()
        else:
            self.obs.stop()
            self.obs_connected = False
            self._obs_state = (False, False)
            self._obs_status("OBS link is off", False)
            self._onair_update()

    def on_obs_opts(self):
        self.obs_live, self.obs_rec = self.live_var.get(), self.rec_var.get()
        self._onair_update()

    def _obs_status(self, text, connected):
        self.obs_connected = connected
        ui = self.ui
        self.obs_status.config(text=f"●  {text}", fg=ui.GOOD if connected else ui.MUTE)

    def _obs_state_changed(self, streaming, recording):
        self._obs_state = (streaming, recording)
        self._onair_update()
        if streaming or recording:
            self.app.set_status("OBS is " + ("live" if streaming else "recording")
                                + " - On-Air light")

    def _obs_scene_list(self, names):
        self.obs_scene_names = list(names)

    def _obs_scene(self, name):
        target = self.obs_scenes.get(name)
        if self.obs_on and target and target in self.app.scenes:
            self.app.apply_scene(target)

    def edit_obs_scenes(self):
        from tkinter import ttk
        ui, app = self.ui, self.app
        top = tk.Toplevel(self.root)
        top.title("OBS scene lighting")
        top.configure(bg=ui.PANEL, padx=22, pady=18)
        top.transient(self.root)
        top.resizable(False, False)
        head = tk.Frame(top, bg=ui.PANEL)
        head.pack(fill="x")
        tk.Label(head, text="WHEN OBS SWITCHES TO…", font=ui.FONT_EYE, fg=ui.MUTE,
                 bg=ui.PANEL).pack(side="left")
        app._help(head, "Choose the RGBZap scene to apply for each OBS scene. '(no change)' "
                        "leaves the lights alone for that scene.").pack(side="left", padx=(6, 0))
        names = self.obs_scene_names or sorted(self.obs_scenes)
        if not names:
            tk.Label(top, text="Connect to OBS first, then your scenes appear here.",
                     font=ui.FONT_BODY, fg=ui.MUTE, bg=ui.PANEL).pack(pady=(14, 6))
        none = "(no change)"
        grid = tk.Frame(top, bg=ui.PANEL)
        grid.pack(fill="x", pady=(10, 0))
        for i, name in enumerate(names):
            tk.Label(grid, text=name, font=ui.FONT_BODY, fg=ui.TEXT, bg=ui.PANEL, anchor="w"
                     ).grid(row=i, column=0, sticky="w", padx=(0, 14), pady=3)
            saved = self.obs_scenes.get(name)
            v = tk.StringVar(value=saved if saved in app.scenes else none)
            cb = ttk.Combobox(grid, textvariable=v, values=[none] + list(app.scenes),
                              state="readonly", width=18, style="Dark.TCombobox",
                              font=ui.FONT_BODY)
            cb.grid(row=i, column=1, pady=3)

            def changed(_e, n=name, var=v):
                if var.get() == none:
                    self.obs_scenes.pop(n, None)
                else:
                    self.obs_scenes[n] = var.get()
            cb.bind("<<ComboboxSelected>>", changed)
        app._flat(top, "Done", top.destroy, pady=6).pack(pady=(16, 0))
        top.update_idletasks()
        top.geometry(f"+{self.root.winfo_rootx() + 40}+{self.root.winfo_rooty() + 120}")
        top.grab_set()

    # ============================================================ local API
    def _api_start(self):
        if not self.api_token:
            self.api_token = api_mod.new_token()
        try:
            self.api.start(self.api_port)
        except OSError:
            self.api_on = False
            self.api_var.set(False)
            self.app.set_status(f"Port {self.api_port} is already in use, so the local API "
                                "couldn't start")
        self._refresh_api_status()
        self.app._save()                          # so the rgbzap command finds the token at once

    def on_api_toggle(self):
        self.api_on = self.api_var.get()
        if self.api_on:
            self._api_start()
        else:
            self.api.stop()
            self._refresh_api_status()

    def _refresh_api_status(self):
        if self.api.running:
            self.api_status.config(
                text=f"On · 127.0.0.1:{self.api.port}  ·  try:  rgbzap color red",
                fg=self.ui.GOOD)
        else:
            self.api_status.config(text="Off", fg=self.ui.MUTE)

    def copy_token(self):
        if not self.api_token:
            self.app.set_status("Turn on local control first")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.api_token)
        self.app.set_status("Token copied")

    def copy_mcp_command(self):
        import rgbzap_mcp
        here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                               else __file__))
        cmd = (rgbzap_mcp.add_command(os.path.join(here, "cli", "rgbzap.exe"))
               if getattr(sys, "frozen", False)
               else rgbzap_mcp.add_command(sys.executable, os.path.join(here, "rgbzap_cli.py")))
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self.app.set_status("Copied. Paste it in a terminal to add RGBZap to Claude Code"
                            + ("" if self.api_on else " (and turn on the switch above)"))

    def new_token(self):
        self.api_token = api_mod.new_token()
        self.app._save()
        self.app.set_status("New token made. Anything using the old one is locked out.")

    def _on_tk(self, fn, timeout=5.0):
        """Run fn on the Tk thread and return its result (called from API server threads)."""
        box, done = {}, threading.Event()

        def run():
            try:
                box["value"] = fn()
            except BaseException as e:            # handed back to the server thread
                box["error"] = e
            finally:
                done.set()
        self.root.after(0, run)
        if not done.wait(timeout):
            raise api_mod.ApiError(503, "RGBZap is busy, try again")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _api_dispatch(self, method, path, body):
        return self._on_tk(lambda: self._api_handle(method, path, body))

    def _api_handle(self, method, path, body):
        """Tk thread. -> (status, payload) or raises ApiError."""
        app = self.app
        gets = {"/status": app.status_info,
                "/scenes": lambda: {"items": list(app.scenes)},
                "/effects": lambda: {"items": app.effect_names()}}
        if method == "GET":
            if path in gets:
                return 200, gets[path]()
            if path in POST_PATHS:
                raise api_mod.ApiError(405, "use POST for this")
            raise api_mod.ApiError(404, "unknown endpoint")
        if method != "POST" or (path not in POST_PATHS):
            raise api_mod.ApiError(405 if path in gets else 404, "unknown endpoint")

        def take_over():                          # an explicit command ends sync / audio
            app.set_sync(False)
            app.set_audio(False)

        if path == "/on":
            app.on_on()
        elif path == "/off":
            app.on_off()
        elif path in ("/color", "/flash"):
            rgb = api_mod.parse_color(body.get("color"))
            if rgb is None:
                raise api_mod.ApiError(400, "color must be a name, #hex or r,g,b")
            if path == "/flash":
                app.flash(rgb)
            else:
                take_over()
                app.set_color(rgb)
        elif path == "/brightness":
            v = body.get("value")
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100:
                raise api_mod.ApiError(400, "value must be a whole number from 1 to 100")
            app.set_brightness_value(v)
        elif path == "/scene":
            name = body.get("name")
            if name not in app.scenes:
                raise api_mod.ApiError(404, f"no scene called {name!r}")
            app.apply_scene(name)
        elif path == "/effect":
            name = body.get("name")
            if name is not None and name not in app.effect_names():
                raise api_mod.ApiError(404, f"no effect called {name!r}")
            take_over()
            app.set_effect(name)
        elif path in ("/sync", "/audio"):
            on = body.get("on", "toggle")
            if not (isinstance(on, bool) or on == "toggle"):      # 1 / 0 / "yes" are not bools
                raise api_mod.ApiError(400, "on must be true, false or \"toggle\"")
            (app.set_sync if path == "/sync" else app.set_audio)(on)
        return 200, {"message": "ok"}

    # ========================================================= color balance
    def reset_balance(self):
        for s in self.bal_scales:
            s.set(100)                              # fires set_balance via the gauge

    # ===================================================== lifecycle + saving
    def start(self):
        """Start whatever the saved settings ask for (call once the app is up)."""
        if self.hk_on:
            failed = self._register_hotkeys()
            if failed:
                n = len(failed)
                self.app.set_status(f"{n} hotkey{'s' * (n != 1)} couldn't be set (another "
                                    "program uses them) - see the Tools tab")
        if self.onair_enabled:
            self.watcher.start()
        if self.obs_on:
            self._obs_apply()
            self.obs.start()
        if self.api_on:
            self._api_start()

    def stop(self):
        self.hotkey_mgr.stop()
        self.watcher.stop()
        self.obs.stop()
        self.api.stop()
        self.app.ble.onair_on = False

    def snapshot(self):
        """The settings this tab owns, for the app's settings file."""
        return {
            "hotkeys_on": self.hk_on, "hotkeys": dict(self.hk),
            "onair": self.onair_enabled, "onair_cam": self.onair_cam,
            "onair_mic": self.onair_mic,
            "obs_on": self.obs_on, "obs_host": self.obs_host, "obs_port": self.obs_port,
            "obs_password": self.obs_password, "obs_live": self.obs_live,
            "obs_rec": self.obs_rec, "obs_scenes": dict(self.obs_scenes),
            "api_on": self.api_on, "api_port": self.api_port, "api_token": self.api_token,
        }


def _selftest():
    """python rgbzap_tools.py --selftest (headless: no windows are opened)"""
    # Tk key events -> shortcut text
    assert combo_from_event(0x4 | 0x20000, "Prior") == "ctrl+alt+pageup"
    assert combo_from_event(0x4, "F10") == "ctrl+f10"
    assert combo_from_event(0x4 | 0x1, "A") == "ctrl+shift+a"
    assert combo_from_event(0x20000, "7") == "alt+7"
    assert combo_from_event(0x4, "KP_3") == "ctrl+numpad3"
    assert combo_from_event(0x4 | 0x20000, "Home") == "ctrl+alt+home"
    assert combo_from_event(0x4, "Control_L") is None and combo_from_event(0, "Alt_R") is None
    assert combo_from_event(0x4, "dead_acute") is None
    assert hotkeys.normalize(combo_from_event(0x4 | 0x20000 | 0x1, "Right")) == "ctrl+alt+shift+right"
    assert hotkeys.normalize(combo_from_event(0x1, "a") or "") is None, "shift alone not allowed"
    print("OK  tools: key events become shortcut text; bare/shift-only combos rejected")

    # the API routes, against a fake app (no Tk): what each call does, and what it refuses
    log = []

    class FakeApp:
        scenes = {"Movie night": {}, "Focus": {}}

        def __getattr__(self, name):                      # record any control call
            return lambda *a: log.append((name, a))

        def effect_names(self):
            return ["Aurora", "Fireworks", "Blink Red"]

        def status_info(self):
            return {"version": "x", "connected": True}

    t = ToolsTab.__new__(ToolsTab)
    t.app = FakeApp()
    h = t._api_handle
    assert h("GET", "/status", {}) == (200, {"version": "x", "connected": True})
    assert h("GET", "/scenes", {})[1] == {"items": ["Movie night", "Focus"]}
    assert h("GET", "/effects", {})[1]["items"][0] == "Aurora"
    assert h("POST", "/color", {"color": "red"}) == (200, {"message": "ok"})
    assert ("set_color", ((255, 0, 0),)) in log and ("set_sync", (False,)) in log
    log.clear()
    h("POST", "/flash", {"color": "#00ff00"})
    assert log == [("flash", ((0, 255, 0),))], "flash must not take over sync/audio"
    log.clear()
    h("POST", "/brightness", {"value": 40})
    h("POST", "/scene", {"name": "Focus"})
    h("POST", "/effect", {"name": "Aurora"})
    h("POST", "/effect", {"name": None})
    h("POST", "/sync", {"on": True})
    h("POST", "/audio", {})
    h("POST", "/on", {})
    h("POST", "/off", {})
    names = [n for n, _a in log]
    assert ("set_brightness_value", (40,)) in log and ("apply_scene", ("Focus",)) in log
    assert ("set_effect", ("Aurora",)) in log and ("set_effect", (None,)) in log
    assert ("set_sync", (True,)) in log and ("set_audio", ("toggle",)) in log
    assert "on_on" in names and "on_off" in names
    for bad_path, bad_body, code in (
            ("/color", {"color": "mauve-ish"}, 400), ("/color", {}, 400),
            ("/flash", {"color": 5}, 400), ("/brightness", {"value": 0}, 400),
            ("/brightness", {"value": 101}, 400), ("/brightness", {"value": "50"}, 400),
            ("/brightness", {"value": True}, 400), ("/brightness", {}, 400),
            ("/scene", {"name": "Nope"}, 404), ("/scene", {}, 404),
            ("/effect", {"name": "Nope"}, 404), ("/sync", {"on": "maybe"}, 400),
            ("/audio", {"on": 1}, 400), ("/nothing", {}, 404)):
        try:
            h("POST", bad_path, bad_body)
            raise AssertionError(f"accepted {bad_path} {bad_body}")
        except api_mod.ApiError as e:
            assert e.status == code, (bad_path, bad_body, e.status, e.message)
    for method, path, code in (("GET", "/on", 405), ("GET", "/nothing", 404),
                               ("POST", "/status", 405), ("DELETE", "/on", 404)):
        try:
            h(method, path, {})
            raise AssertionError(f"accepted {method} {path}")
        except api_mod.ApiError as e:
            assert e.status == code, (method, path, e.status)
    print("OK  tools: every API route does its one thing; bad colors, ranges, names and "
          "methods are refused with the right status")

    # _on_tk: runs on the 'Tk' thread, returns the value, re-raises errors, times out
    class Loop:
        def __init__(self):
            self.q = []

        def after(self, _ms, fn, *a):
            self.q.append((fn, a))
    t2 = ToolsTab.__new__(ToolsTab)
    t2.root = Loop()
    out = {}

    def caller():
        try:
            out["v"] = t2._on_tk(lambda: 41 + 1)
            try:
                t2._on_tk(lambda: (_ for _ in ()).throw(api_mod.ApiError(404, "gone")))
            except api_mod.ApiError as e:
                out["err"] = e.status
            try:
                t2._on_tk(lambda: 1, timeout=0.2)          # nobody pumps the loop now
            except api_mod.ApiError as e:
                out["timeout"] = e.status
        except Exception as e:
            out["boom"] = e
    th = threading.Thread(target=caller)
    th.start()
    for _ in range(2):                                     # pump two jobs like Tk would
        while not t2.root.q:
            time.sleep(0.01)
        fn, a = t2.root.q.pop(0)
        fn(*a)
    th.join(5)
    assert out == {"v": 42, "err": 404, "timeout": 503}, out
    print("OK  tools: API calls hop to the UI thread and back (value, error, timeout)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
