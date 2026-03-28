#!/usr/bin/env python3
"""MeTVe Mega V2 - Expanded Features Edition.

A public-facing Python web application that simulates a WebForms-era virtual TV
operator portal with creator tools, channel lifecycle APIs, reliability-first
client behavior, and legacy broadcast workflow modules.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CHANNELS_FILE = DATA_DIR / "channels.json"
USERS_FILE = DATA_DIR / "users.json"
SOCKET_FILE = DATA_DIR / "socket.json"
EVENTS_FILE = DATA_DIR / "events.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)


class JsonStore:
    """Simple thread-safe JSON persistence."""

    def __init__(self, path: Path, default: Any):
        self.path = path
        self.default = default
        self.lock = threading.Lock()
        if not self.path.exists():
            self.write(default)

    def read(self) -> Any:
        with self.lock:
            if not self.path.exists():
                return self.default
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)

    def write(self, payload: Any) -> None:
        with self.lock:
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)


channels_store = JsonStore(CHANNELS_FILE, {"channels": []})
users_store = JsonStore(USERS_FILE, {"users": []})
socket_store = JsonStore(SOCKET_FILE, {"url": "", "updated_at": 0.0})
events_store = JsonStore(EVENTS_FILE, {"items": []})
SESSIONS: Dict[str, str] = {}
SESSIONS_LOCK = threading.Lock()


@dataclass
class Channel:
    id: str
    name: str
    format: str
    description: str
    archived: bool
    version: int
    created_by: str
    created_at: float
    updated_at: float
    playlist: List[Dict[str, Any]]
    settings: Dict[str, Any]
    tags: List[str]
    ad_banner: str


def _now() -> float:
    return time.time()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    correlation = handler.headers.get("X-Request-Correlation", "")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    if correlation:
        handler.send_header("X-Request-Correlation", correlation)
    handler.end_headers()
    handler.wfile.write(encoded)


def _parse_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(content_length) if content_length > 0 else b"{}"
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _session_user(handler: BaseHTTPRequestHandler) -> Optional[str]:
    cookie = handler.headers.get("Cookie")
    if not cookie:
        return None
    jar = SimpleCookie()
    jar.load(cookie)
    token = jar.get("metve_session")
    if not token:
        return None
    with SESSIONS_LOCK:
        return SESSIONS.get(token.value)


def _set_session(handler: BaseHTTPRequestHandler, username: str) -> None:
    token = str(uuid.uuid4())
    with SESSIONS_LOCK:
        SESSIONS[token] = username
    handler.send_header("Set-Cookie", f"metve_session={token}; Path=/; HttpOnly; SameSite=Lax")


def _clear_session(handler: BaseHTTPRequestHandler) -> None:
    cookie = handler.headers.get("Cookie")
    if cookie:
        jar = SimpleCookie()
        jar.load(cookie)
        token = jar.get("metve_session")
        if token:
            with SESSIONS_LOCK:
                SESSIONS.pop(token.value, None)
    handler.send_header("Set-Cookie", "metve_session=deleted; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")


def _append_event(kind: str, message: str, actor: str = "system") -> None:
    data = events_store.read()
    items = data.get("items", [])
    items.insert(
        0,
        {
            "id": f"evt-{uuid.uuid4().hex[:10]}",
            "kind": kind,
            "message": message,
            "actor": actor,
            "ts": _now(),
        },
    )
    data["items"] = items[:500]
    events_store.write(data)


def _load_channels() -> List[Dict[str, Any]]:
    return channels_store.read().get("channels", [])


def _save_channels(channels: List[Dict[str, Any]]) -> None:
    channels_store.write({"channels": channels})


def _find_channel(channels: List[Dict[str, Any]], channel_id: str) -> Optional[Dict[str, Any]]:
    return next((c for c in channels if c["id"] == channel_id), None)


class MeTVeHandler(BaseHTTPRequestHandler):
    server_version = "MeTVeMega/2.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve_index(self) -> None:
        body = INDEX_HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> Optional[str]:
        username = _session_user(self)
        if not username:
            _json_response(self, 401, {"error": "Authentication required"})
            return None
        return username

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in {"/", "/public", "/v2"}:
            self._serve_index()
            return

        if parsed.path == "/api/me":
            user = _session_user(self)
            _json_response(self, 200, {"authenticated": bool(user), "username": user})
            return

        if parsed.path == "/api/health":
            sock = socket_store.read()
            pending = 0
            _json_response(
                self,
                200,
                {
                    "api": "online",
                    "socketConfigured": bool(sock.get("url")),
                    "socketUrl": sock.get("url", ""),
                    "pendingLocalFallback": pending,
                    "timestamp": _now(),
                },
            )
            return

        if parsed.path == "/api/socket/config":
            _json_response(self, 200, socket_store.read())
            return

        if parsed.path == "/api/events":
            data = events_store.read().get("items", [])
            _json_response(self, 200, {"items": data[:200], "count": len(data)})
            return

        if parsed.path == "/api/channels":
            user = self._require_auth()
            if not user:
                return
            qs = parse_qs(parsed.query)
            include_archived = qs.get("includeArchived", ["false"])[0].lower() == "true"
            format_filter = qs.get("format", [""])[0].strip().lower()
            query = qs.get("q", [""])[0].strip().lower()
            channels = _load_channels()

            if not include_archived:
                channels = [c for c in channels if not c.get("archived")]
            if format_filter and format_filter != "all":
                channels = [c for c in channels if c.get("format", "").lower() == format_filter]
            if query:
                channels = [
                    c
                    for c in channels
                    if query in c.get("name", "").lower()
                    or query in c.get("description", "").lower()
                    or query in " ".join(c.get("tags", [])).lower()
                ]

            _json_response(self, 200, {"channels": channels, "count": len(channels)})
            return

        _json_response(self, 404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = _parse_json(self)

        if parsed.path == "/api/signup":
            username = (payload.get("username") or "").strip()
            password = (payload.get("password") or "").strip()
            if not username or not password:
                _json_response(self, 400, {"error": "username and password required"})
                return
            users = users_store.read()
            if any(u["username"] == username for u in users["users"]):
                _json_response(self, 409, {"error": "username already exists"})
                return
            users["users"].append({"username": username, "password": password})
            users_store.write(users)
            _append_event("auth", f"{username} signed up", username)

            body = json.dumps({"ok": True, "username": username}).encode("utf-8")
            self.send_response(201)
            _set_session(self, username)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/signin":
            username = (payload.get("username") or "").strip()
            password = (payload.get("password") or "").strip()
            users = users_store.read().get("users", [])
            ok = any(u["username"] == username and u["password"] == password for u in users)
            if not ok:
                _json_response(self, 401, {"error": "invalid credentials"})
                return
            _append_event("auth", f"{username} signed in", username)

            body = json.dumps({"ok": True, "username": username}).encode("utf-8")
            self.send_response(200)
            _set_session(self, username)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/signout":
            user = _session_user(self) or "unknown"
            _append_event("auth", f"{user} signed out", user)
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            _clear_session(self)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        user = self._require_auth()
        if not user:
            return

        if parsed.path == "/api/socket/config":
            socket_url = (payload.get("url") or "").strip()
            socket_store.write({"url": socket_url, "updated_at": _now()})
            status = "configured" if socket_url else "cleared"
            _append_event("socket", f"Socket URL {status}", user)
            _json_response(self, 200, {"ok": True, "url": socket_url})
            return

        if parsed.path == "/api/channels":
            idem = self.headers.get("X-Idempotency-Key", "")
            if not idem:
                _json_response(self, 400, {"error": "X-Idempotency-Key required"})
                return

            channels = _load_channels()
            prior = next((c for c in channels if c.get("last_idempotency_key") == idem), None)
            if prior:
                _json_response(self, 200, {"channel": prior, "replayed": True})
                return

            now = _now()
            channel = Channel(
                id=f"ch-{uuid.uuid4().hex[:12]}",
                name=(payload.get("name") or "Untitled Channel").strip(),
                format=(payload.get("format") or "All").strip(),
                description=(payload.get("description") or "").strip(),
                archived=False,
                version=1,
                created_by=user,
                created_at=now,
                updated_at=now,
                playlist=payload.get("playlist", []),
                settings=payload.get("settings", {}),
                tags=payload.get("tags", []),
                ad_banner=payload.get("ad_banner", ""),
            )
            data = asdict(channel)
            data["last_idempotency_key"] = idem
            channels.append(data)
            _save_channels(channels)
            _append_event("channel", f"Channel created: {data['name']}", user)
            _json_response(self, 201, {"channel": data, "replayed": False})
            return

        if parsed.path.startswith("/api/channels/") and parsed.path.endswith("/clone"):
            channel_id = parsed.path.split("/")[3]
            channels = _load_channels()
            found = _find_channel(channels, channel_id)
            if not found:
                _json_response(self, 404, {"error": "channel not found"})
                return
            clone = dict(found)
            clone["id"] = f"ch-{uuid.uuid4().hex[:12]}"
            clone["name"] = f"{found['name']} (Clone)"
            clone["version"] = 1
            clone["created_at"] = _now()
            clone["updated_at"] = _now()
            clone["last_idempotency_key"] = self.headers.get("X-Idempotency-Key", str(uuid.uuid4()))
            channels.append(clone)
            _save_channels(channels)
            _append_event("channel", f"Channel cloned: {clone['name']}", user)
            _json_response(self, 201, {"channel": clone})
            return

        _json_response(self, 404, {"error": "Not found"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        payload = _parse_json(self)
        user = self._require_auth()
        if not user:
            return

        if parsed.path.startswith("/api/channels/"):
            channel_id = parsed.path.split("/")[3]
            channels = _load_channels()
            found = _find_channel(channels, channel_id)
            if not found:
                _json_response(self, 404, {"error": "channel not found"})
                return

            incoming_version = payload.get("version")
            if incoming_version is None or int(incoming_version) != int(found.get("version", 0)):
                _json_response(self, 409, {"error": "version conflict", "channel": found})
                return

            found["name"] = payload.get("name", found["name"])
            found["format"] = payload.get("format", found["format"])
            found["description"] = payload.get("description", found.get("description", ""))
            found["playlist"] = payload.get("playlist", found.get("playlist", []))
            found["settings"] = payload.get("settings", found.get("settings", {}))
            found["tags"] = payload.get("tags", found.get("tags", []))
            found["ad_banner"] = payload.get("ad_banner", found.get("ad_banner", ""))
            found["updated_at"] = _now()
            found["version"] = int(found.get("version", 1)) + 1
            _save_channels(channels)
            _append_event("channel", f"Channel saved: {found['name']} v{found['version']}", user)
            _json_response(self, 200, {"channel": found})
            return

        _json_response(self, 404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        user = self._require_auth()
        if not user:
            return

        if parsed.path.startswith("/api/channels/"):
            channel_id = parsed.path.split("/")[3]
            channels = _load_channels()
            found = _find_channel(channels, channel_id)
            if not found:
                _json_response(self, 404, {"error": "channel not found"})
                return
            found["archived"] = True
            found["updated_at"] = _now()
            found["version"] = int(found.get("version", 1)) + 1
            _save_channels(channels)
            _append_event("channel", f"Channel archived: {found['name']}", user)
            _json_response(self, 200, {"channel": found})
            return

        _json_response(self, 404, {"error": "Not found"})


INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeTVe V2 | Online Television (Expanded Features Edition)</title>
<style>
:root{--bg:#070c19;--panel:#111a36;--panel2:#1b2750;--line:#334b8f;--text:#dce9ff;--muted:#9cb0df;--ok:#1ed184;--warn:#fbbf24;--bad:#f43f5e;--acc:#54a8ff}
*{box-sizing:border-box}body{margin:0;color:var(--text);background:radial-gradient(circle at top,#152851,#050913 60%);font-family:Tahoma,Verdana,Arial,sans-serif}
a{color:#9fd3ff}.top{position:sticky;top:0;z-index:10;background:#0d1530;border-bottom:1px solid var(--line);padding:8px}
.top .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.title{font-size:13px;font-weight:bold}.btn{background:#244083;border:1px solid #4f73da;color:#fff;padding:5px 8px;border-radius:4px;cursor:pointer;font-size:12px}
.btn:hover{filter:brightness(1.1)}.ok{background:#0f5132;border-color:#22c55e}.warn{background:#6b4a00;border-color:#f59e0b}.bad{background:#5f1232;border-color:#fb7185}
input,select,textarea{width:100%;background:var(--panel2);color:#fff;border:1px solid #4960a8;border-radius:4px;padding:6px;font-size:12px}
textarea{min-height:72px}.layout{display:grid;grid-template-columns:300px 1fr;gap:10px;padding:10px}.panel{background:var(--panel);border:1px solid #2a3f78;border-radius:8px;padding:10px}
.panel h3{font-size:14px;margin:0 0 8px;padding-bottom:6px;border-bottom:1px solid #2e447f}.small{font-size:11px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}.menu{display:flex;gap:6px;flex-wrap:wrap}
.pill{font-size:10px;background:#25386d;padding:2px 6px;border-radius:999px}.status-ok{color:var(--ok)}.status-warn{color:var(--warn)}.status-bad{color:var(--bad)}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.card{background:#192854;border:1px solid #3a58ad;border-radius:6px;padding:8px}
.table{width:100%;border-collapse:collapse;font-size:12px}.table th,.table td{border:1px solid #355092;padding:4px;vertical-align:top}.mono{font-family:Consolas,monospace;font-size:11px}
.navtabs{display:flex;gap:6px;flex-wrap:wrap}.tab{padding:5px 7px;border:1px solid #4b67b6;border-radius:4px;background:#1b2d5e;cursor:pointer;font-size:12px}
.tab.active{background:#3257b3}.page{display:none}.page.active{display:block}.ticker{background:#0a132a;border:1px solid #334;overflow:hidden;white-space:nowrap;padding:5px;margin-top:6px}.ticker span{display:inline-block;padding-left:100%;animation:ticker 18s linear infinite}
@keyframes ticker{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}.drop{border:1px dashed #8cb1ff;border-radius:8px;padding:12px;text-align:center;background:#0f1834}
.progress{height:10px;background:#1a2a55;border-radius:999px;overflow:hidden}.progress>div{height:100%;width:0%;background:#2eb6ff}
</style>
</head>
<body>
<div class="top">
  <div class="row">
    <div class="title">MeTVe Mega V2 — Public Online Television (Legacy 2011 Mode)</div>
    <button class="btn" onclick="quick('create')">Quick: Create</button>
    <button class="btn" onclick="quick('go-live')">Quick: Go Live</button>
    <button class="btn" onclick="quick('upload')">Quick: Upload</button>
    <button class="btn" onclick="quick('promote')">Quick: Promote</button>
    <button class="btn" onclick="quick('monitor')">Quick: Monitor</button>
    <button class="btn" onclick="resyncLocal()">Quick: Local Sync</button>
    <select id="uiLang" onchange="setLanguage()"><option value="en">English</option><option value="ar">Arabic (RTL)</option></select>
    <select id="userRole" onchange="setRole()"><option>Operator</option><option>Producer</option><option>Viewer</option><option>Admin</option></select>
    <select id="nostalgiaPreset" onchange="applyNostalgia()"><option>CRT Glow</option><option>Blue Neon</option><option>Studio Amber</option></select>
  </div>
  <div class="ticker"><span>MeTVe Mega Capabilities: playout • lower-thirds • SMS moderation • IVR queue • UDP/RTMP/SRT/NDI outputs • auto-EPG • SCTE simulation • PAL/NTSC safe-area.</span></div>
</div>

<div class="layout">
  <aside>
    <section class="panel">
      <h3>Account Controls</h3>
      <label>Username <input id="username" placeholder="creator"></label>
      <label>Password <input id="password" type="password" placeholder="password"></label>
      <div class="menu" style="margin-top:6px">
        <button class="btn" onclick="signup()">Sign Up</button>
        <button class="btn" onclick="signin()">Sign In</button>
        <button class="btn bad" onclick="signout()">Sign Out</button>
      </div>
      <div id="authState" class="small" style="margin-top:6px">Not authenticated</div>
    </section>

    <section class="panel">
      <h3>Reliability & Sync Center</h3>
      <table class="table">
        <tr><td>API</td><td id="apiHealth" class="status-ok">online</td></tr>
        <tr><td>Socket</td><td id="socketHealth" class="status-warn">offline (url missing)</td></tr>
        <tr><td>Pending Local</td><td id="pendingCount">0</td></tr>
        <tr><td>Correlation</td><td id="corr" class="mono">-</td></tr>
      </table>
      <label class="small">Socket URL</label>
      <input id="socketUrlInput" placeholder="wss://socket.metve.local/realtime">
      <div class="menu" style="margin-top:6px">
        <button class="btn" onclick="saveSocketConfig()">Save Socket URL</button>
        <button class="btn" onclick="manualReconnect()">Manual Reconnect</button>
        <button class="btn warn" onclick="toggleApiFailure()">Toggle API Fail Sim</button>
      </div>
    </section>

    <section class="panel">
      <h3>Creator Freedom Hub</h3>
      <div class="small">Create channels, promote projects, entertain viewers, and operate cable-style experiences.</div>
      <textarea id="promoteText" placeholder="Promote your projects/channels"></textarea>
      <button class="btn" onclick="publishPromotion()">Publish Promotion</button>
    </section>

    <section class="panel">
      <h3>MeTVe Mega Operations</h3>
      <ul class="small">
        <li>Playout: Slide TV / Video TV / Radio TV / VIP / Ringtone TV / Chat / Games / Other / All</li>
        <li>Graphics: OSD, lower-thirds, emergency text override</li>
        <li>Interactive: SMS moderation, poll winners, IVR/call queue</li>
        <li>Distribution: UDP, RTMP, SRT, NDI + SDI/Composite placeholders</li>
        <li>Legacy broadcast: PAL/NTSC + safe-area + 4:3 controls</li>
      </ul>
    </section>
  </aside>

  <main style="display:flex;flex-direction:column;gap:10px">
    <section class="panel">
      <div class="navtabs">
        <button class="tab active" data-page="home" onclick="showPage('home',this)">V2 Home</button>
        <button class="tab" data-page="channels" onclick="showPage('channels',this)">Channels</button>
        <button class="tab" data-page="library" onclick="showPage('library',this)">Library</button>
        <button class="tab" data-page="reliability" onclick="showPage('reliability',this)">Reliability</button>
        <button class="tab" data-page="automation" onclick="showPage('automation',this)">Automation</button>
      </div>

      <div id="page-home" class="page active">
        <h3>Professional Cable-Style Operator Dashboard</h3>
        <div class="grid3">
          <div>
            <label>Channel Name <input id="chName" placeholder="Galaxy Retro TV"></label>
            <label>Channel Format
              <select id="chFormat"><option>Slide TV</option><option>Video TV</option><option>Radio TV</option><option>VIP</option><option>Ringtone TV</option><option>Chat</option><option>Games</option><option>Other</option><option>All</option></select>
            </label>
            <label>Description <textarea id="chDesc"></textarea></label>
            <label>Tags (comma separated) <input id="chTags" placeholder="retro,music,night"></label>
            <label>Ad Banner Text <input id="chAd" placeholder="Tonight 9PM: Retro blockbuster"></label>
            <div class="menu" style="margin-top:6px">
              <button class="btn ok" onclick="createChannel()">Create</button>
              <button class="btn" onclick="saveChannel()">Save</button>
              <button class="btn" onclick="cloneChannel()">Clone</button>
              <button class="btn bad" onclick="archiveChannel()">Archive</button>
            </div>
          </div>
          <div>
            <div class="card"><b>TV Preview</b><div class="small">Virtual confidence screen / lower-third safe-area</div></div>
            <div class="card"><b>Radio Preview</b><div class="small">Audio-only playout monitor</div></div>
            <div class="card"><b>Hotkeys</b><div class="small">Alt+1 chat, Alt+2 clip, Alt+3 ad</div></div>
            <div class="card"><b>Quiz Round</b><button class="btn" onclick="log('Quiz round triggered')">Trigger</button></div>
          </div>
          <div>
            <div class="cards">
              <div class="card"><b>SMS</b><div id="revSms">$0</div></div>
              <div class="card"><b>Ads</b><div id="revAds">$0</div></div>
              <div class="card"><b>Votes</b><div id="revVotes">0</div></div>
              <div class="card"><b>Subs</b><div id="revSubs">0</div></div>
            </div>
            <div class="menu" style="margin-top:8px">
              <button class="btn" onclick="simulateRevenue()">Revenue Tick</button>
              <button class="btn" onclick="playlistAction('auto')">Auto-Schedule</button>
              <button class="btn warn" onclick="playlistAction('emergency')">Emergency Action</button>
            </div>
          </div>
        </div>
        <div class="grid2" style="margin-top:8px">
          <div class="panel">
            <h3>Featured Channels + Cable Watch Preview</h3>
            <table id="featuredTable" class="table"></table>
            <div class="small">Click “Watch” to load channel preview and details.</div>
          </div>
          <div class="panel">
            <h3>Channel Watch Page</h3>
            <video id="channelPlayer" controls muted style="width:100%;background:#000;min-height:180px"></video>
            <div id="watchMeta" class="small" style="margin-top:6px">Select a channel to watch content.</div>
          </div>
        </div>
      </div>

      <div id="page-channels" class="page">
        <h3>MeTVe V2 Channel Home</h3>
        <div class="grid2">
          <div>
            <label>Search Channels <input id="chSearch" oninput="loadChannels()" placeholder="name, tags, description"></label>
          </div>
          <div>
            <label>Format Filter
              <select id="formatFilter" onchange="loadChannels()"><option>All</option><option>Slide TV</option><option>Video TV</option><option>Radio TV</option><option>VIP</option><option>Ringtone TV</option><option>Chat</option><option>Games</option><option>Other</option></select>
            </label>
          </div>
        </div>
        <table id="channelsTable" class="table" style="margin-top:8px"></table>
      </div>

      <div id="page-library" class="page">
        <h3>Media Creator Studio + Library Folders</h3>
        <div class="small">Shows, Movies, Commercials, Bumpers, Songs, Idents, Promos, Graphics</div>
        <div class="grid3">
          <label>Asset Name <input id="assetName" placeholder="Evening Show Episode 1"></label>
          <label>Asset Type
            <select id="assetType">
              <option>Video</option><option>Audio</option><option>Music</option><option>Image</option><option>GIF</option><option>Live Event</option>
            </select>
          </label>
          <label>Asset URL / Stream / YouTube / VidLii / Archive <input id="assetUrl" placeholder="https://..."></label>
        </div>
        <div class="menu" style="margin-top:6px">
          <button class="btn" onclick="addAssetToFolder('Shows')">Add Shows (Folder)</button>
          <button class="btn" onclick="addAssetToFolder('Movies')">Add Movies (Folder)</button>
          <button class="btn" onclick="addAssetToFolder('Commercials')">Add Commercials (Folder)</button>
          <button class="btn" onclick="addAssetToFolder('Bumpers')">Add Bumpers (Multi)</button>
          <button class="btn" onclick="addAssetToFolder('Songs')">Add Songs (Folder/Multi)</button>
          <button class="btn" onclick="addAssetToFolder('Idents')">Add Idents (Multi)</button>
          <button class="btn" onclick="addAssetToFolder('Promos')">Add Promos (Folder)</button>
        </div>
        <label>Library Search <input id="librarySearch" oninput="renderLibrary()"></label>
        <div class="menu" style="margin-top:6px">
          <button class="btn" onclick="batchFolder('Shows')">Batch Shows</button>
          <button class="btn" onclick="batchFolder('Movies')">Batch Movies</button>
          <button class="btn" onclick="batchFolder('Commercials')">Batch Commercials</button>
          <button class="btn" onclick="batchFolder('Bumpers')">Batch Bumpers</button>
          <button class="btn" onclick="batchFolder('Songs')">Batch Songs</button>
          <button class="btn" onclick="batchFolder('Idents')">Batch Idents</button>
          <button class="btn" onclick="batchFolder('Promos')">Batch Promos</button>
        </div>
        <div class="drop" id="dropzone">Drag/drop upload simulation with chunked progress
          <div class="progress" style="margin-top:8px"><div id="uploadBar"></div></div>
        </div>
        <table id="libraryTable" class="table" style="margin-top:8px"></table>
      </div>

      <div id="page-reliability" class="page">
        <h3>Reliability Monitor + Sync Actions</h3>
        <label>Broadcaster Time Zone
          <select id="tzSelect" onchange="updateTimezone()">
            <option value="UTC">UTC</option>
            <option value="America/New_York">America/New_York</option>
            <option value="Europe/London">Europe/London</option>
            <option value="Asia/Dubai">Asia/Dubai</option>
            <option value="Asia/Kolkata">Asia/Kolkata</option>
          </select>
        </label>
        <div id="tzNow" class="small">Current scheduler time: -</div>
        <div class="grid2">
          <div class="panel">
            <h3>Live Stream Engine Panel</h3>
            <label>Latency <select id="latency"><option>Ultra Low</option><option>Balanced</option><option>Compatibility</option></select></label>
            <label>Bitrate Ladder <input id="ladder" value="360p:800k,720p:2500k,1080p:6000k"></label>
            <label>Codec <select id="codec"><option>H264 Main</option><option>H264 High</option><option>HEVC Main10</option></select></label>
            <label>Aspect/Scaling <select id="aspect"><option>4:3 Legacy</option><option>16:9 Letterbox</option><option>Smart Center Cut</option></select></label>
            <label>Audio Channels <input id="audioChannels" type="number" min="1" max="16" value="8"></label>
            <label>Caption Mode <select id="caption"><option>CEA-608</option><option>CEA-708</option><option>Burn-In</option></select></label>
            <label>RTMP <input id="rtmp" value="rtmp://uplink/metve/channel"></label>
            <label>HLS <input id="hls" value="https://cdn/metve/live.m3u8"></label>
            <label>WebRTC <input id="webrtc" value="wss://rtc/metve"></label>
            <label>SRT/UDP/NDI <input id="networkOutputs" value="srt://core:9000,udp://239.0.0.1:5000,ndi://metve"></label>
            <div class="menu" style="margin-top:6px"><button class="btn" onclick="goLive()">Go Live</button><button class="btn" onclick="snapshotAnalytics()">Snapshot</button><button class="btn" onclick="simulateScte()">SCTE Trigger</button></div>
            <pre id="analyticsBox" class="mono"></pre>
          </div>
          <div class="panel">
            <h3>Local Sync + Events</h3>
            <button class="btn" onclick="resyncLocal()">One-click Resync Local Queue</button>
            <button class="btn" onclick="refreshEvents()">Refresh Events</button>
            <pre id="eventsBox" class="mono" style="max-height:260px;overflow:auto"></pre>
          </div>
        </div>
        <div class="panel" style="margin-top:8px">
          <h3>Smart Alerts + AI Moderation</h3>
          <div class="menu">
            <button class="btn" onclick="scanSmartAlerts()">Scan Schedule Alerts</button>
            <button class="btn" onclick="seedModeration()">Seed SMS/Chat Queue</button>
            <button class="btn" onclick="approveModeration()">Approve Top</button>
            <button class="btn bad" onclick="rejectModeration()">Reject Top</button>
          </div>
          <div class="grid2" style="margin-top:8px">
            <pre id="alertsBox" class="mono"></pre>
            <pre id="moderationBox" class="mono"></pre>
          </div>
        </div>
      </div>

      <div id="page-automation" class="page">
        <h3>Broadcast Automation & Legacy Modules</h3>
        <div class="grid2">
          <div>
            <h4>TV Guide Planner + Auto-EPG</h4>
            <textarea id="guideInput">08:00 Morning Cartoons\n10:00 Movie Block\n12:30 Ad Break\n12:35 News\n13:00 Music Jam</textarea>
            <div class="menu"><button class="btn" onclick="generateEPG()">Generate EPG</button><button class="btn" onclick="playlistAction('filler')">Insert Filler</button><button class="btn" onclick="playlistAction('auto')">Auto Schedule</button></div>
            <pre id="epgBox" class="mono"></pre>
          </div>
          <div>
            <h4>Playlist + Legacy Actions</h4>
            <table id="playlistTable" class="table"></table>
            <div class="menu" style="margin-top:6px">
              <button class="btn" onclick="log('OSD lower third dispatched')">OSD Lower Third</button>
              <button class="btn" onclick="log('SMS moderation workflow run')">SMS Moderation</button>
              <button class="btn" onclick="log('Poll winner workflow run')">Poll Winner</button>
              <button class="btn" onclick="log('IVR call queue simulation advanced')">IVR Queue</button>
              <button class="btn warn" onclick="log('Emergency text override enabled')">Emergency Text</button>
            </div>
          </div>
        </div>
        <div class="panel" style="margin-top:8px">
          <h3>24-Hour Mega Playout Engine</h3>
          <div class="grid3">
            <label>Playout Date <input id="playoutDate" type="date"></label>
            <label>Mode
              <select id="playoutMode">
                <option>Auto 24/7</option>
                <option>Live Assist</option>
                <option>Emergency Override</option>
              </select>
            </label>
            <label>Filler Rule
              <select id="fillerRule">
                <option>Auto Filler on Gaps</option>
                <option>Loop Last Block</option>
                <option>Break News Priority</option>
              </select>
            </label>
          </div>
          <div class="menu" style="margin-top:6px">
            <button class="btn" onclick="build24hPlayout()">Build 24H Grid</button>
            <button class="btn ok" onclick="startPlayout()">Start Playout</button>
            <button class="btn warn" onclick="nextPlayoutItem()">Next Item</button>
            <button class="btn bad" onclick="stopPlayout()">Stop Playout</button>
            <button class="btn" onclick="exportAsRun()">Export As-Run JSON</button>
          </div>
          <div class="grid2" style="margin-top:8px">
            <table id="playoutTable" class="table"></table>
            <pre id="asRunBox" class="mono" style="max-height:220px;overflow:auto"></pre>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <h3>Control Room Logs</h3>
      <pre id="logs" class="mono" style="max-height:180px;overflow:auto"></pre>
    </section>
  </main>
</div>

<script>
const state = {
  me: null,
  channels: [],
  selected: null,
  socketUrl: '',
  apiFailSim: false,
  library: [
    {folder:'Shows',name:'Retro Morning Show',type:'Video',url:''}, {folder:'Movies',name:'Prime Thriller',type:'Video',url:''},
    {folder:'Commercials',name:'Soda Spot 2011',type:'Video',url:''}, {folder:'Bumpers',name:'Bumper A',type:'GIF',url:''},
    {folder:'Songs',name:'Top Pop',type:'Music',url:''}, {folder:'Idents',name:'MeTVe Blue Ident',type:'Image',url:''},
    {folder:'Promos',name:'Weekend Promo',type:'Video',url:''}, {folder:'Graphics',name:'Lower Third Pack',type:'Image',url:''}
  ],
  playlist: [
    {slot:'08:00',asset:'Retro Intro',kind:'Ident'},
    {slot:'08:01',asset:'Morning Show',kind:'Show'},
    {slot:'08:30',asset:'Ad Cluster',kind:'Commercial'}
  ],
  playoutGrid: [],
  playoutIndex: 0,
  playoutTimer: null,
  asRun: [],
  moderationQueue: [],
  role: 'Operator',
  lang: 'en',
  pending: JSON.parse(localStorage.getItem('metve_pending_queue') || '[]'),
  localChannels: JSON.parse(localStorage.getItem('metve_local_channels') || '[]')
};
state.library = JSON.parse(localStorage.getItem('metve_library_assets') || JSON.stringify(state.library));

function log(msg){
  const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
  const box = document.getElementById('logs');
  box.textContent = line + '\n' + box.textContent;
}

function persistLocal(){
  localStorage.setItem('metve_pending_queue', JSON.stringify(state.pending));
  localStorage.setItem('metve_local_channels', JSON.stringify(state.localChannels));
  localStorage.setItem('metve_library_assets', JSON.stringify(state.library));
  pendingCount.textContent = state.pending.length;
}

async function apiFetch(url, opts={}){
  const retries = opts.retries ?? 2;
  const timeoutMs = opts.timeoutMs ?? 2200;
  const corrId = crypto.randomUUID();
  document.getElementById('corr').textContent = corrId;

  if (state.apiFailSim) throw new Error('Simulated API outage');

  for (let i = 0; i <= retries; i++) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs + (i * 400));
    try {
      const res = await fetch(url, {
        ...opts,
        signal: ctl.signal,
        headers: {
          'Content-Type': 'application/json',
          'X-Request-Correlation': corrId,
          ...(opts.idempotencyKey ? {'X-Idempotency-Key': opts.idempotencyKey} : {}),
          ...(opts.headers || {})
        }
      });
      clearTimeout(timer);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    } catch (e) {
      clearTimeout(timer);
      if (i === retries) throw e;
      await new Promise(r => setTimeout(r, 400 * (i + 1)));
    }
  }
}

async function refreshMe(){
  try {
    const me = await apiFetch('/api/me', {retries:0, timeoutMs:1500});
    state.me = me.username || null;
    authState.textContent = state.me ? `Authenticated as ${state.me}` : 'Not authenticated';
    await loadChannels();
  } catch {
    authState.textContent = 'Auth API unavailable';
  }
}

async function signup(){
  try {
    const d = await apiFetch('/api/signup', {method:'POST', body: JSON.stringify({username: username.value, password: password.value})});
    state.me = d.username;
    log('Signed up ' + d.username);
    await refreshMe();
  } catch (e) { log('Signup failed: ' + e.message); }
}

async function signin(){
  try {
    const d = await apiFetch('/api/signin', {method:'POST', body: JSON.stringify({username: username.value, password: password.value})});
    state.me = d.username;
    log('Signed in ' + d.username);
    await refreshMe();
  } catch (e) { log('Signin failed: ' + e.message); }
}

async function signout(){
  try {
    await apiFetch('/api/signout', {method:'POST', body:'{}'});
    state.me = null;
    authState.textContent = 'Not authenticated';
    log('Signed out');
  } catch (e) { log('Signout failed: ' + e.message); }
}

function showPage(name, el){
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  el.classList.add('active');
}

function quick(action){ log('Quick menu -> ' + action); }

function setLanguage(){
  state.lang = uiLang.value;
  const rtl = state.lang === 'ar';
  document.documentElement.dir = rtl ? 'rtl' : 'ltr';
  log('UI language mode set to ' + (rtl ? 'Arabic RTL' : 'English LTR'));
}

function setRole(){
  state.role = userRole.value;
  log('Role set to ' + state.role);
}

function canManageChannels(){
  if (state.role === 'Viewer') {
    log('Viewer role cannot create/save channels. Switch role to Operator/Producer/Admin.');
    return false;
  }
  return true;
}

async function loadChannels(){
  const q = encodeURIComponent((chSearch.value || '').trim());
  const f = encodeURIComponent((formatFilter.value || 'All'));
  try {
    const r = await apiFetch(`/api/channels?includeArchived=true&q=${q}&format=${f}`);
    state.channels = r.channels;
    state.localChannels = r.channels;
    persistLocal();
    apiHealth.textContent = 'online';
    apiHealth.className = 'status-ok';
    renderChannels();
  } catch (e) {
    state.channels = [...state.localChannels];
    apiHealth.textContent = 'degraded (local fallback)';
    apiHealth.className = 'status-warn';
    renderChannels();
    log('Loaded channels from local fallback');
  }
}

function channelPayload(){
  return {
    name: (chName.value || 'Untitled Channel').trim(),
    format: chFormat.value,
    description: (chDesc.value || '').trim(),
    tags: (chTags.value || '').split(',').map(x => x.trim()).filter(Boolean),
    ad_banner: (chAd.value || '').trim(),
    playlist: [...state.playlist],
    settings: {
      latency: latency.value,
      ladder: ladder.value,
      codec: codec.value,
      aspect: aspect.value,
      audioChannels: Number(audioChannels.value || 8),
      caption: caption.value,
      outputs: {rtmp: rtmp.value, hls: hls.value, webrtc: webrtc.value, extra: networkOutputs.value}
    }
  };
}

async function createChannel(){
  if (!canManageChannels()) return;
  const payload = channelPayload();
  try {
    const res = await apiFetch('/api/channels', {
      method:'POST',
      body: JSON.stringify(payload),
      idempotencyKey: crypto.randomUUID()
    });
    log('Channel created via API: ' + res.channel.name);
    await loadChannels();
  } catch (e) {
    const local = { ...payload, id: 'local-' + Date.now(), version: 1, archived: false, localOnly: true, created_by: state.me || 'local' };
    state.localChannels.push(local);
    state.pending.push({type:'create', payload:local});
    persistLocal();
    state.channels = [...state.localChannels];
    renderChannels();
    apiHealth.textContent = 'offline (queued local)';
    apiHealth.className = 'status-bad';
    log('Create queued locally due to API outage');
  }
}

function selectChannel(id){
  state.selected = state.channels.find(c => c.id === id) || null;
  if (!state.selected) return;
  chName.value = state.selected.name || '';
  chDesc.value = state.selected.description || '';
  chFormat.value = state.selected.format || 'All';
  chTags.value = (state.selected.tags || []).join(', ');
  chAd.value = state.selected.ad_banner || '';
  if (Array.isArray(state.selected.playlist)) state.playlist = state.selected.playlist;
  renderPlaylist();
  watchChannel(id);
  log('Selected channel ' + state.selected.name);
}

async function saveChannel(){
  if (!canManageChannels()) return;
  if (!state.selected) { log('No selected channel'); return; }
  const payload = {...state.selected, ...channelPayload()};
  try {
    const res = await apiFetch('/api/channels/' + state.selected.id, {method:'PUT', body: JSON.stringify(payload)});
    log('Saved channel version ' + res.channel.version);
    await loadChannels();
  } catch (e) {
    if ((e.message || '').includes('version conflict')) {
      log('Optimistic concurrency conflict; reload then retry');
      return;
    }
    state.pending.push({type:'save', payload});
    const ix = state.localChannels.findIndex(c => c.id === payload.id);
    if (ix >= 0) state.localChannels[ix] = payload;
    persistLocal();
    renderChannels();
    log('Save queued to local fallback queue');
  }
}

async function cloneChannel(){
  if (!canManageChannels()) return;
  if (!state.selected) { log('No selected channel'); return; }
  try {
    await apiFetch('/api/channels/' + state.selected.id + '/clone', {
      method: 'POST',
      body: '{}',
      idempotencyKey: crypto.randomUUID()
    });
    log('Channel cloned');
    await loadChannels();
  } catch (e) { log('Clone failed: ' + e.message); }
}

async function archiveChannel(){
  if (!canManageChannels()) return;
  if (!state.selected) { log('No selected channel'); return; }
  try {
    await apiFetch('/api/channels/' + state.selected.id, {method:'DELETE'});
    log('Channel archived');
    await loadChannels();
  } catch (e) { log('Archive failed: ' + e.message); }
}

function renderChannels(){
  const rows = state.channels.map(c => `
    <tr>
      <td>${c.name}</td><td>${c.format}</td><td>${c.version || 1}</td>
      <td>${(c.tags || []).join(', ')}</td>
      <td>${c.archived ? 'Archived' : 'Active'} ${c.localOnly ? '<span class="pill">LOCAL</span>' : ''}</td>
      <td><button class="btn" onclick="selectChannel('${c.id}')">Select</button></td>
    </tr>`).join('');
  channelsTable.innerHTML = '<tr><th>Name</th><th>Format</th><th>Version</th><th>Tags</th><th>Status</th><th>Action</th></tr>' + rows;
  renderFeaturedChannels();
}

function renderFeaturedChannels(){
  const top = [...state.channels].filter(c => !c.archived).slice(0, 8);
  featuredTable.innerHTML = '<tr><th>Featured</th><th>Format</th><th>Watch</th></tr>' + top.map(c => `
    <tr><td>${c.name}</td><td>${c.format}</td><td><button class="btn" onclick="watchChannel('${c.id}')">Watch</button></td></tr>
  `).join('');
}

function watchChannel(id){
  const ch = state.channels.find(c => c.id === id);
  if (!ch) return;
  const firstPlayable = (ch.playlist || []).find(i => i.url) || null;
  channelPlayer.src = firstPlayable?.url || '';
  watchMeta.textContent = `${ch.name} • ${ch.format} • ${ch.description || 'No description'} • ${firstPlayable ? ('Now Playing: '+firstPlayable.asset) : 'No playable URL in playlist yet'}`;
  log('Watch mode opened for channel ' + ch.name);
}

function renderLibrary(){
  const q = (librarySearch.value || '').toLowerCase();
  const items = state.library.filter(i => i.name.toLowerCase().includes(q) || i.folder.toLowerCase().includes(q) || (i.type || '').toLowerCase().includes(q));
  libraryTable.innerHTML = '<tr><th>Folder</th><th>Name</th><th>Type</th><th>Source</th><th>Action</th></tr>' + items.map(i => `<tr><td>${i.folder}</td><td>${i.name}</td><td>${i.type || '-'}</td><td class="small">${i.url || '-'}</td><td><button class="btn" onclick="pushToPlaylist('${i.name.replace(/'/g, \"&#39;\")}', '${i.url || ''}')">Use</button></td></tr>`).join('');
}

function batchFolder(folder){
  for (let i=0;i<3;i++) state.library.push({folder, name:`${folder} Asset ${Math.floor(Math.random()*9999)}`, type:'Video', url:''});
  renderLibrary();
  persistLocal();
  log('Batch folder action: ' + folder);
}

function addAssetToFolder(folder){
  const name = (assetName.value || `${folder} Asset ${Date.now()}`).trim();
  const type = assetType.value;
  const url = (assetUrl.value || '').trim();
  const item = {folder, name, type, url};
  state.library.push(item);
  renderLibrary();
  persistLocal();
  log(`Added ${type} asset to ${folder}: ${name}`);
}

function pushToPlaylist(name, url){
  const d = new Date();
  const slot = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  state.playlist.push({slot, asset:name, kind:'Asset', url});
  renderPlaylist();
  log('Asset pushed to playlist: ' + name);
}

function renderPlaylist(){
  playlistTable.innerHTML = '<tr><th>Slot</th><th>Asset</th><th>Kind</th><th>URL</th></tr>' + state.playlist.map((p,i)=>
    `<tr><td>${p.slot}</td><td contenteditable onblur="editPlaylist(${i},'asset',this.textContent)">${p.asset}</td><td>${p.kind}</td><td contenteditable onblur="editPlaylist(${i},'url',this.textContent)">${p.url || ''}</td></tr>`
  ).join('');
}

function editPlaylist(i,key,value){ state.playlist[i][key] = value; }

function playlistAction(kind){
  const d = new Date();
  const slot = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  if (kind === 'filler') state.playlist.push({slot, asset:'Auto Filler', kind:'Filler'});
  if (kind === 'auto') state.playlist.push({slot, asset:'Smart Auto Block', kind:'Promo'});
  if (kind === 'emergency') state.playlist.unshift({slot, asset:'Emergency Text Override', kind:'Emergency'});
  renderPlaylist();
  log(`Playlist action: ${kind}`);
}

function goLive(){
  log(`Go Live: ${latency.value}, ladder=${ladder.value}, outputs=${rtmp.value}|${hls.value}|${webrtc.value}`);
}

function snapshotAnalytics(){
  const snap = {
    viewers: 1000 + Math.floor(Math.random()*3000),
    drops: Math.floor(Math.random()*4),
    qos: ['Good','Great','Excellent'][Math.floor(Math.random()*3)],
    outputs: {udp:'ready',rtmp:'ready',srt:'ready',ndi:'ready'},
    codec: codec.value,
    captions: caption.value,
    audioChannels: Number(audioChannels.value || 8)
  };
  analyticsBox.textContent = JSON.stringify(snap, null, 2);
}

function simulateScte(){ log('SCTE trigger simulation sent (splice_insert)'); }

function simulateRevenue(){
  revSms.textContent = '$' + (Math.random()*120).toFixed(2);
  revAds.textContent = '$' + (Math.random()*350).toFixed(2);
  revVotes.textContent = Math.floor(Math.random()*9000);
  revSubs.textContent = Math.floor(Math.random()*4000);
}

function generateEPG(){
  const lines = guideInput.value.split('\n').map(x => x.trim()).filter(Boolean);
  epgBox.textContent = lines.map((line,i) => `${i+1}. ${line} [SCTE:${i%2===0?'YES':'NO'}] [Traffic:${i%3===0?'HOOK':'PASS'}]`).join('\n');
  log('Auto-EPG generated (frame-accurate snap schedule simulation)');
}

function build24hPlayout(){
  const base = state.playlist.length ? state.playlist : [{slot:'00:00',asset:'Default Filler',kind:'Filler',url:''}];
  const date = playoutDate.value || new Date().toISOString().slice(0,10);
  const grid = [];
  for (let h = 0; h < 24; h++) {
    const source = base[h % base.length];
    grid.push({
      time: `${String(h).padStart(2,'0')}:00`,
      asset: source.asset || `Auto Block ${h}`,
      kind: source.kind || 'Show',
      url: source.url || '',
      status: 'queued',
      date,
      mode: playoutMode.value,
      fillerRule: fillerRule.value
    });
  }
  state.playoutGrid = grid;
  state.playoutIndex = 0;
  renderPlayoutGrid();
  log(`24H playout grid built for ${date} (${playoutMode.value})`);
}

function renderPlayoutGrid(){
  playoutTable.innerHTML = '<tr><th>Time</th><th>Asset</th><th>Kind</th><th>Status</th></tr>' +
    state.playoutGrid.map((it, idx) => `<tr><td>${it.time}</td><td>${it.asset}</td><td>${it.kind}</td><td>${idx===state.playoutIndex?'<span class=\"pill\">LIVE</span> ':''}${it.status}</td></tr>`).join('');
}

function startPlayout(){
  if (!state.playoutGrid.length) build24hPlayout();
  if (state.playoutTimer) clearInterval(state.playoutTimer);
  state.playoutTimer = setInterval(nextPlayoutItem, 3500);
  log('24H playout engine started');
}

function stopPlayout(){
  if (state.playoutTimer) clearInterval(state.playoutTimer);
  state.playoutTimer = null;
  log('24H playout engine stopped');
}

function nextPlayoutItem(){
  if (!state.playoutGrid.length) return;
  if (state.playoutIndex >= state.playoutGrid.length) {
    state.playoutIndex = 0;
  }
  state.playoutGrid.forEach((row, i) => {
    if (i < state.playoutIndex) row.status = 'done';
    if (i > state.playoutIndex) row.status = 'queued';
  });
  const current = state.playoutGrid[state.playoutIndex];
  current.status = 'on-air';
  state.asRun.unshift({
    ts: new Date().toISOString(),
    item: current.asset,
    time: current.time,
    kind: current.kind,
    mode: current.mode
  });
  state.asRun = state.asRun.slice(0, 1200);
  asRunBox.textContent = state.asRun.map(x => `${x.ts} | ${x.time} | ${x.item} | ${x.kind} | ${x.mode}`).join('\\n');
  if (current.url) {
    channelPlayer.src = current.url;
    watchMeta.textContent = `On Air: ${current.asset} (${current.kind})`;
  }
  log(`On-air switched to ${current.time} ${current.asset}`);
  state.playoutIndex += 1;
  renderPlayoutGrid();
}

function exportAsRun(){
  const payload = JSON.stringify(state.asRun, null, 2);
  const blob = new Blob([payload], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `metve-asrun-${new Date().toISOString().slice(0,10)}.json`;
  a.click();
  URL.revokeObjectURL(url);
  log('As-run export generated');
}

function scanSmartAlerts(){
  const alerts = [];
  if (!state.playoutGrid.length) alerts.push('No playout grid built.');
  const seen = new Set();
  for (const item of state.playoutGrid) {
    if (seen.has(item.time)) alerts.push(`Clash detected at ${item.time}`);
    seen.add(item.time);
    if (!item.asset || item.asset.trim() === '') alerts.push(`Free slot at ${item.time}`);
  }
  if (!alerts.length) alerts.push('No clashes/free-slots detected. Schedule healthy.');
  alerts.push(`Role=${state.role}, Lang=${state.lang}, Socket=${state.socketUrl ? 'configured' : 'offline mode'}`);
  alertsBox.textContent = alerts.join('\\n');
  log('Smart alert scan completed');
}

function seedModeration(){
  state.moderationQueue.unshift(
    {type:'SMS', user:'+12025550101', text:'Play my song please!', ai:'safe'},
    {type:'CHAT', user:'viewer_neo', text:'This channel is HOT LIVE', ai:'safe'},
    {type:'SMS', user:'+447700900123', text:'spam $$$ link', ai:'flagged'}
  );
  renderModeration();
  log('Moderation queue seeded');
}

function approveModeration(){
  const item = state.moderationQueue.shift();
  if (!item) { log('No moderation items to approve'); return; }
  log(`Approved ${item.type} from ${item.user}`);
  renderModeration();
}

function rejectModeration(){
  const item = state.moderationQueue.shift();
  if (!item) { log('No moderation items to reject'); return; }
  log(`Rejected ${item.type} from ${item.user}`);
  renderModeration();
}

function renderModeration(){
  moderationBox.textContent = state.moderationQueue.map((m, i) => `#${i+1} ${m.type} ${m.user} | ${m.text} | AI=${m.ai}`).join('\\n') || 'Moderation queue empty';
}

function toggleApiFailure(){
  state.apiFailSim = !state.apiFailSim;
  log('API failure simulation ' + (state.apiFailSim ? 'enabled' : 'disabled'));
}

async function resyncLocal(){
  if (!state.pending.length) { log('No local pending operations'); return; }
  const pendingCopy = [...state.pending];
  const keep = [];
  for (const item of pendingCopy) {
    try {
      if (item.type === 'create') {
        await apiFetch('/api/channels', {method:'POST', body: JSON.stringify(item.payload), idempotencyKey: crypto.randomUUID()});
      } else if (item.type === 'save') {
        await apiFetch('/api/channels/' + item.payload.id, {method:'PUT', body: JSON.stringify(item.payload)});
      }
    } catch {
      keep.push(item);
    }
  }
  state.pending = keep;
  persistLocal();
  await loadChannels();
  log('Local resync complete. Remaining=' + state.pending.length);
}

async function saveSocketConfig(){
  try {
    const url = socketUrlInput.value.trim();
    const d = await apiFetch('/api/socket/config', {method:'POST', body: JSON.stringify({url})});
    state.socketUrl = d.url || '';
    socketHealth.textContent = state.socketUrl ? 'configured' : 'offline (url missing)';
    socketHealth.className = state.socketUrl ? 'status-ok' : 'status-warn';
    log('Socket config saved');
  } catch (e) {
    log('Socket config failed: ' + e.message);
  }
}

function manualReconnect(){
  if (!state.socketUrl) {
    socketHealth.textContent = 'offline (url missing)';
    socketHealth.className = 'status-warn';
    log('Explicit socket offline mode (URL not configured)');
    return;
  }
  socketHealth.textContent = 'reconnecting...';
  socketHealth.className = 'status-warn';
  setTimeout(() => {
    socketHealth.textContent = 'online (resubscribed)';
    socketHealth.className = 'status-ok';
    log('Socket reconnect + resubscribe complete');
  }, 1000);
}

async function refreshSocketConfig(){
  try {
    const s = await apiFetch('/api/socket/config', {retries:0});
    state.socketUrl = s.url || '';
    socketUrlInput.value = state.socketUrl;
    socketHealth.textContent = state.socketUrl ? 'configured' : 'offline (url missing)';
    socketHealth.className = state.socketUrl ? 'status-ok' : 'status-warn';
  } catch {
    socketHealth.textContent = 'unreachable';
    socketHealth.className = 'status-bad';
  }
}

async function refreshEvents(){
  try {
    const r = await apiFetch('/api/events', {retries:0});
    eventsBox.textContent = r.items.map(e => `[${new Date(e.ts*1000).toLocaleString()}] (${e.kind}) ${e.message} @${e.actor}`).join('\n');
  } catch (e) {
    eventsBox.textContent = 'Events unavailable: ' + e.message;
  }
}

function publishPromotion(){
  const text = (promoteText.value || '').trim();
  if (!text) { log('Promotion text required'); return; }
  log('Promotion published: ' + text);
}

function applyNostalgia(){
  const map = {'CRT Glow':'#54a8ff','Blue Neon':'#67b9ff','Studio Amber':'#ffb347'};
  document.documentElement.style.setProperty('--acc', map[nostalgiaPreset.value] || '#54a8ff');
  log('Nostalgia FX -> ' + nostalgiaPreset.value);
}

function updateTimezone(){
  const tz = tzSelect.value || 'UTC';
  const now = new Date();
  try {
    tzNow.textContent = 'Current scheduler time: ' + now.toLocaleString('en-US', {timeZone: tz}) + ` (${tz})`;
  } catch {
    tzNow.textContent = 'Current scheduler time: ' + now.toISOString() + ' (UTC fallback)';
  }
  log('Scheduler timezone set to ' + tz);
}

(function initDnD(){
  const zone = document.getElementById('dropzone');
  const runUpload = () => {
    let p = 0;
    const bar = document.getElementById('uploadBar');
    const timer = setInterval(() => {
      p += 6 + Math.random()*14;
      bar.style.width = Math.min(100, p) + '%';
      if (p >= 100) {
        clearInterval(timer);
        state.library.push({folder:'Shows', name:'Upload_' + Date.now() + '.mp4'});
        renderLibrary();
        log('Chunked upload simulation complete');
        setTimeout(() => bar.style.width = '0%', 500);
      }
    }, 170);
  };
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.style.borderColor = '#1ed184'; });
  zone.addEventListener('dragleave', () => zone.style.borderColor = '#8cb1ff');
  zone.addEventListener('drop', e => { e.preventDefault(); zone.style.borderColor = '#8cb1ff'; runUpload(); });
  zone.addEventListener('click', runUpload);
})();

window.addEventListener('keydown', e => {
  if (!e.altKey) return;
  if (e.key === '1') log('Hotkey Alt+1 -> Chat preset');
  if (e.key === '2') log('Hotkey Alt+2 -> Clip preset');
  if (e.key === '3') log('Hotkey Alt+3 -> Ad preset');
});

renderLibrary();
renderPlaylist();
simulateRevenue();
snapshotAnalytics();
playoutDate.value = new Date().toISOString().slice(0,10);
build24hPlayout();
updateTimezone();
setLanguage();
setRole();
renderModeration();
refreshMe();
refreshSocketConfig();
refreshEvents();
persistLocal();
</script>
</body>
</html>
"""


def run() -> None:
    host = "0.0.0.0"
    port = 8080
    server = ThreadingHTTPServer((host, port), MeTVeHandler)
    print(f"MeTVe Mega V2 running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
