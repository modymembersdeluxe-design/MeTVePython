# MeTVe Mega V2 (Python Public Website Edition)

MeTVe Mega V2 is a Python-powered public website + API that simulates a **WebForms-era interactive television platform** with creator studio controls, channel lifecycle management, live stream tooling, media library operations, and reliability-first fallback behavior.

## Expanded Highlights

- Professional cable-style operator dashboard (TV/Radio preview + channel format targeting)
- User account flow: sign up, sign in, sign out
- Channel lifecycle: create/save/list/clone/archive with optimistic concurrency
- Reliability hardening:
  - API timeout + retry/backoff
  - idempotency and correlation headers
  - local fallback queue for create/save when API is unavailable
- Socket stability controls:
  - explicit offline mode when URL missing
  - reconnect/resubscribe simulation when configured
- Live Stream Engine panel:
  - low-latency mode, ladder/codec/aspect profiles
  - RTMP/HLS/WebRTC plus UDP/SRT/NDI profile field
  - analytics snapshot widget
- Media Composer + Library:
  - folders (Shows, Movies, Commercials, Bumpers, Songs, Idents, Promos, Graphics)
  - local search + drag/drop chunk upload simulation
- Creator Freedom Hub + Project Advertising page
- Reliability and event monitor with one-click local resync
- Featured-channel homepage watch preview + per-channel watch panel
- Channel creation/save reliability fix path:
  - create/save continue through local queue even if API/socket is unavailable
  - manual local resync pushes queued create/save actions back to API
- Broadcaster timezone selector for schedule context
- Automation modules:
  - frame-accurate snap schedule simulation
  - auto-EPG generation
  - SCTE trigger simulation
  - traffic hook placeholders
- Legacy modules:
  - OSD/lower thirds, SMS moderation, polls, IVR queue simulation
  - emergency text override
  - PAL/NTSC and 4:3 compatibility placeholders

## Run

```bash
python3 app.py
```

Open:
- `http://localhost:8080/`
- `http://localhost:8080/public`
- `http://localhost:8080/v2`

## Data persistence

Runtime data is stored in:
- `data/users.json`
- `data/channels.json`
- `data/socket.json`
- `data/events.json`
- client local fallback in browser `localStorage` (`metve_pending_queue`, `metve_local_channels`, `metve_library_assets`)
