# Nestling

Nestling is a self-hosted baby monitor that streams Google Nest camera feed directly to the browser over WebRTC.

## Tech stack

- Python 3.12
- [aiohttp](https://docs.aiohttp.org/) — async HTTP server
- [aiortc](https://github.com/aiortc/aiortc) — WebRTC implementation (both the inbound SDM session and outbound browser stream)
- [Google Smart Device Management API](https://developers.google.com/nest/device-access)

---

## Setup

### Prerequisites

- Python 3.12
- A Google Nest camera on your Google account
- A Google Cloud project with the **Device Access API** enabled
- A **$5 one-time Device Access registration fee** paid to Google ([register here](https://developers.google.com/nest/device-access/registration))

### 1. Create a Google Cloud project and OAuth client

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. Enable the **Smart Device Management API** for the project.
3. Under **APIs & Services → Credentials**, create an **OAuth 2.0 Client ID** (application type: **Web application**).
4. Add `https://oauth2.googleapis.com/token` as an authorized redirect URI (or use `http://localhost` for local flows).
5. Note your **Client ID** and **Client Secret**.

### 2. Create a Device Access project and link your account

1. Go to the [Device Access Console](https://console.nest.google.com/u/0/device-access) and create a project, entering your OAuth Client ID when prompted.
2. Note your **Project ID** (shown on the project page).
3. Follow the [authorization guide](https://developers.google.com/nest/device-access/authorize) to complete the OAuth flow and obtain a **refresh token** for your Google account.

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```
cp .env.example .env
```

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_REFRESH_TOKEN` | Refresh token obtained from the authorization flow |
| `GOOGLE_PROJECT_ID` | Device Access project ID |

### 4. Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Load your .env and start the server
export $(grep -v '^#' .env | xargs)
nestling
```

Open [http://localhost:8080](http://localhost:8080) in your browser. Select your camera from the dropdown, click **Start**, and the live WebRTC stream will begin.

---

## Architecture

Nestling establishes two independent WebRTC peer connections and bridges them through a frame pipeline:

```
Google SDM API
      ↕  (WebRTC offer/answer via HTTPS)
aiortc inbound PeerConnection   ← receives H.264/VP8 from the camera
      ↓
  raw frame pipeline            ← asyncio fan-out queue (BGR frames)
      ↓
aiortc outbound PeerConnection  ← re-encodes frames as VP8 for the browser
      ↕  (WebRTC offer/answer via /webrtc/offer)
    Browser
```

**Inbound session** (`camera.py`): `CameraManager` opens a WebRTC session with the SDM API using `aiortc`. Because SDM fingerprints the offer, the SDP is munged before sending (fake Chrome-like SSRCs, codec fixes, candidate normalization) to coax a stable VP8 answer out of the camera.

**Frame pipeline** (`camera.py`): Decoded BGR frames are broadcast to all subscribers via `Pipeline`, a simple asyncio fan-out queue. Slow consumers drop frames rather than back-pressuring the producer.

**Outbound session** (`stream/webrtc.py`): Each browser tab gets its own `BrowserPeerConnection`. `RawVideoTrack` subscribes to the pipeline, converts BGR→RGB, stamps wall-clock pts, and feeds frames to aiortc's VP8 encoder, which handles the browser-facing WebRTC session.

**HTTP server** (`main.py`): aiohttp routes. `/camera/start` and `/camera/stop` manage the inbound session lifecycle. `/webrtc/offer` handles browser offer/answer negotiation. The frontend is a single static HTML file (`static/index.html`) with no build step.
