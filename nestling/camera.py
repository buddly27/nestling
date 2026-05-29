import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription

log = logging.getLogger(__name__)

_VIDEO_SSRC = "1933910976"
_RTX_SSRC = "504479091"
_CNAME = "DFsz7BwXusRJ+YWb"

_RENEW_INTERVAL = 4 * 60  # seconds — SDM tokens expire at 5 min
_FIRST_FRAME_TIMEOUT = 3.0  # warn if no frame within this many seconds
_ICE_GATHER_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Frame and Pipeline
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    at: datetime
    img: np.ndarray  # BGR uint8, shape (H, W, 3)


class Pipeline:
    """Fan-out frame broadcaster. Each subscriber gets its own asyncio.Queue."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    def subscribe(self, maxsize: int = 2) -> "asyncio.Queue[Frame | None]":
        q: asyncio.Queue[Frame | None] = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Frame | None]") -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def broadcast(self, frame: Frame) -> None:
        for q in self._queues:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # slow consumer — drop frame, never block the producer

    def close(self) -> None:
        """Signal all subscribers to exit by pushing a None sentinel."""
        for q in list(self._queues):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # Make room for the sentinel so handlers can exit cleanly.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        self._queues.clear()


# ---------------------------------------------------------------------------
# SDP munge functions
# ---------------------------------------------------------------------------

def _lines(sdp: str) -> list[str]:
    return sdp.replace("\r\n", "\n").split("\n")


def _join(lines: list[str]) -> str:
    return "\r\n".join(lines)


def add_fake_ssrc(sdp: str) -> str:
    """Inject fixed Chrome-like SSRCs into the video m-section of the offer.

    SDM fingerprints offer SSRCs and returns stable PT=96 VP8 only when the
    offer looks like a real browser sender. Random SSRCs produce PT=0 + random
    SSRCs in the answer. aiortc's generated SSRCs are replaced here.
    """
    lines = _lines(sdp)
    result: list[str] = []
    in_video = False
    injected = False

    def _ssrc_block() -> list[str]:
        return [
            f"a=ssrc-group:FID {_VIDEO_SSRC} {_RTX_SSRC}",
            f"a=ssrc:{_VIDEO_SSRC} cname:{_CNAME}",
            f"a=ssrc:{_RTX_SSRC} cname:{_CNAME}",
        ]

    for line in lines:
        if line.startswith("m="):
            if in_video and not injected:
                result.extend(_ssrc_block())
            in_video = line.startswith("m=video")
            injected = False

        if in_video and (line.startswith("a=ssrc:") or line.startswith("a=ssrc-group:")):
            if not injected:
                result.extend(_ssrc_block())
                injected = True
            continue  # drop aiortc's random ssrc lines

        result.append(line)

    if in_video and not injected:
        result.extend(_ssrc_block())

    return _join(result)


def fix_video_codec(sdp: str) -> str:
    """Safety net: rewrite PT=0 → PT=96 and inject a=rtpmap:96 VP8/90000 if missing.

    With fake SSRCs in the offer, SDM should return PT=96, but older cameras
    may still send PT=0 with no rtpmap. Only injects rtpmap when PT=0 was
    actually rewritten — avoids spurious injection for cameras using PT=97.
    """
    lines = _lines(sdp)
    result: list[str] = []
    in_video = False
    has_rtpmap96 = False
    changed_pt = False

    for line in lines:
        if line == "" or line.startswith("m="):
            if in_video and changed_pt and not has_rtpmap96:
                result.append("a=rtpmap:96 VP8/90000")
            in_video = False
            has_rtpmap96 = False
            changed_pt = False

        if line.startswith("m=video"):
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "0":
                parts[3] = "96"
                line = " ".join(parts)
                changed_pt = True
            in_video = True

        if in_video and line.startswith("a=rtpmap:96 "):
            has_rtpmap96 = True

        result.append(line)

    if in_video and changed_pt and not has_rtpmap96:
        result.append("a=rtpmap:96 VP8/90000")

    return _join(result)


def fix_candidates(sdp: str) -> str:
    """Fix SDM's malformed a=candidate lines — the foundation field is missing.

    SDM sends: a=candidate: 1 udp <priority> <ip> <port> typ host ...
    Standard:  a=candidate:<foundation> <component> <transport> ...

    The leading space after the colon means bits[0]='1' (component), bits[1]='udp'
    (transport), so aiortc's candidate_from_sdp crashes on int(bits[1]).
    Injecting a placeholder foundation restores the expected field positions.
    """
    lines = _lines(sdp)
    result: list[str] = []
    for line in lines:
        if line.startswith("a=candidate:"):
            value = line[len("a=candidate:"):]
            if value.startswith(" "):
                value = "sdm" + value
                line = "a=candidate:" + value
        result.append(line)
    return _join(result)


def fix_bundle_order(sdp: str) -> str:
    """Rewrite SDM's a=group:BUNDLE 0 2 1 to match our offer ordering 0 1 2."""
    lines = _lines(sdp)
    for i, line in enumerate(lines):
        if line == "a=group:BUNDLE 0 2 1":
            lines[i] = "a=group:BUNDLE 0 1 2"
            break
    return _join(lines)



def _munge_answer(sdp: str) -> str:
    return fix_candidates(fix_bundle_order(fix_video_codec(sdp)))


# ---------------------------------------------------------------------------
# CameraManager
# ---------------------------------------------------------------------------

class CameraManager:
    def __init__(self, sdm_client: Any, device_name: str, pipeline: Pipeline) -> None:
        self._sdm = sdm_client
        self._device_name = device_name
        self._pipeline = pipeline
        self._pc: RTCPeerConnection | None = None
        self._media_session_id: str = ""
        self._consume_task: asyncio.Task | None = None
        self._renew_task: asyncio.Task | None = None

    async def start(self) -> None:
        pc = RTCPeerConnection()
        self._pc = pc

        pc.addTransceiver("audio", direction="recvonly")
        # sendrecv forces SSRC lines into the offer so SDM returns stable PT=96 VP8.
        # add_fake_ssrc then replaces aiortc's random SSRCs with Chrome-like values.
        pc.addTransceiver("video", direction="sendrecv")
        # SDM requires m=audio, m=video, m=application in that order.
        pc.createDataChannel("dataSendChannel")

        @pc.on("connectionstatechange")
        async def on_connection_state() -> None:
            log.info("camera %s: connection state → %s", self._device_name, pc.connectionState)

        @pc.on("track")
        def on_track(track: Any) -> None:
            log.info("camera %s: track received kind=%s", self._device_name, track.kind)
            if track.kind == "video":
                self._consume_task = asyncio.create_task(self._consume_video(track))

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for ICE gathering to complete before reading localDescription.sdp.
        gather_done = asyncio.Event()
        if pc.iceGatheringState == "complete":
            gather_done.set()
        else:
            @pc.on("icegatheringstatechange")
            def on_gather_state() -> None:
                if pc.iceGatheringState == "complete":
                    gather_done.set()

        await asyncio.wait_for(gather_done.wait(), timeout=_ICE_GATHER_TIMEOUT)

        offer_sdp = add_fake_ssrc(pc.localDescription.sdp)
        log.debug("camera %s: offer SDP\n%s", self._device_name, offer_sdp)

        answer_sdp, media_session_id = await self._sdm.generate_webrtc_stream(
            self._device_name, offer_sdp
        )
        self._media_session_id = media_session_id
        log.debug("camera %s: answer SDP (raw)\n%s", self._device_name, answer_sdp)

        munged = _munge_answer(answer_sdp)
        log.debug("camera %s: answer SDP (munged)\n%s", self._device_name, munged)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=munged, type="answer"))

        self._renew_task = asyncio.create_task(self._renew_loop())
        log.info("camera %s: WebRTC session established", self._device_name)

    async def stop(self) -> None:
        for task in (self._consume_task, self._renew_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._pc:
            await self._pc.close()
            self._pc = None

    async def _consume_video(self, track: Any) -> None:
        log.info("camera %s: video consumer started", self._device_name)
        target_wh: tuple[int, int] | None = None  # (width, height) for cv2.resize
        try:
            # First frame with timeout — log a diagnostic warning if stalled.
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=_FIRST_FRAME_TIMEOUT)
                log.info("camera %s: first video frame received", self._device_name)
            except asyncio.TimeoutError:
                log.warning(
                    "camera %s: no video frame within %.1fs — still waiting",
                    self._device_name, _FIRST_FRAME_TIMEOUT,
                )
                frame = await track.recv()

            img = frame.to_ndarray(format="bgr24")
            target_wh = (img.shape[1], img.shape[0])
            self._pipeline.broadcast(Frame(at=datetime.now(), img=img))

            while True:
                frame = await track.recv()
                img = frame.to_ndarray(format="bgr24")
                # VP8 adaptive bitrate can change resolution mid-stream.
                # Normalise to the first frame's dimensions so all pipeline
                # subscribers see consistent frame sizes.
                if (img.shape[1], img.shape[0]) != target_wh:
                    img = cv2.resize(img, target_wh)
                self._pipeline.broadcast(Frame(at=datetime.now(), img=img))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("camera %s: video consumer error: %s", self._device_name, exc)

    async def _renew_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_RENEW_INTERVAL)
                if not self._media_session_id:
                    continue
                try:
                    new_id = await self._sdm.extend_webrtc_stream(
                        self._device_name, self._media_session_id
                    )
                    self._media_session_id = new_id
                    log.info("camera %s: stream token renewed", self._device_name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("camera %s: stream renewal failed: %s", self._device_name, exc)
        except asyncio.CancelledError:
            raise
