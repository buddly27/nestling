import asyncio
import logging
import time

import av
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import (
    MediaStreamError,
    MediaStreamTrack,
    VIDEO_CLOCK_RATE,
    VIDEO_TIME_BASE,
)

from nestling.camera import Frame, Pipeline

log = logging.getLogger(__name__)

_ICE_GATHER_TIMEOUT = 10.0


class RawVideoTrack(MediaStreamTrack):
    """Pulls raw BGR frames from the pipeline and delivers them as VP8."""

    kind = "video"

    def __init__(self, pipeline: Pipeline) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._queue = pipeline.subscribe(maxsize=2)
        self._start: float | None = None
        self._last_pts: int = -1

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError

        frame: Frame | None = await self._queue.get()
        if frame is None:
            # Pipeline closed — signal end-of-stream.
            self.stop()
            raise MediaStreamError

        # Compute pts from wall-clock time (90000 Hz clock).
        # Using real time rather than next_timestamp() so the pts reflects
        # the camera's actual frame rate rather than an artificial 30 fps pace.
        now = time.time()
        if self._start is None:
            self._start = now
        pts = max(self._last_pts + 1, int((now - self._start) * VIDEO_CLOCK_RATE))
        self._last_pts = pts

        img_rgb = frame.img[:, :, ::-1]  # BGR → RGB
        av_frame = av.VideoFrame.from_ndarray(img_rgb, format="rgb24")
        av_frame.pts = pts
        av_frame.time_base = VIDEO_TIME_BASE
        return av_frame

    def stop(self) -> None:
        self._pipeline.unsubscribe(self._queue)
        super().stop()


class BrowserPeerConnection:
    """Outbound WebRTC connection to one browser tab."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pc = RTCPeerConnection()
        self._track = RawVideoTrack(pipeline)
        self._pc.addTrack(self._track)

    async def handle_offer(self, sdp: str, type_: str) -> tuple[str, str]:
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        gather_done = asyncio.Event()
        if self._pc.iceGatheringState == "complete":
            gather_done.set()
        else:
            @self._pc.on("icegatheringstatechange")
            def _on_gather() -> None:
                if self._pc.iceGatheringState == "complete":
                    gather_done.set()

        await asyncio.wait_for(gather_done.wait(), timeout=_ICE_GATHER_TIMEOUT)
        log.info("webrtc: outbound ICE gathering complete")
        return self._pc.localDescription.sdp, self._pc.localDescription.type

    async def close(self) -> None:
        self._track.stop()
        await self._pc.close()
