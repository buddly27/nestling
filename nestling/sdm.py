import logging
import re
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_BASE_URL = "https://smartdevicemanagement.googleapis.com/v1"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SDMClient:
    def __init__(self, token_manager: Any, project_id: str) -> None:
        self._tm = token_manager
        self._project_id = project_id
        self._device_re = re.compile(
            r"^enterprises/" + re.escape(project_id) + r"/devices/[A-Za-z0-9_-]+$"
        )
        self._session = aiohttp.ClientSession(timeout=_TIMEOUT)

    def validate_device(self, device: str) -> bool:
        return bool(self._device_re.match(device))

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tm.token}"}

    async def list_devices(self) -> Any:
        url = f"{_BASE_URL}/enterprises/{self._project_id}/devices"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def generate_webrtc_stream(
        self, device_name: str, offer_sdp: str
    ) -> tuple[str, str]:
        url = f"{_BASE_URL}/{device_name}:executeCommand"
        payload = {
            "command": "sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream",
            "params": {"offerSdp": offer_sdp},
        }
        async with self._session.post(url, json=payload, headers=self._auth_headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        results = data.get("results", {})
        answer_sdp = results.get("answerSdp", "")
        media_session_id = results.get("mediaSessionId", "")
        if not answer_sdp:
            raise ValueError("SDM returned empty answerSdp")
        if not media_session_id:
            log.warning("sdm: GenerateWebRtcStream returned no mediaSessionId; renewal unavailable")
        return answer_sdp, media_session_id

    async def extend_webrtc_stream(
        self, device_name: str, media_session_id: str
    ) -> str:
        url = f"{_BASE_URL}/{device_name}:executeCommand"
        payload = {
            "command": "sdm.devices.commands.CameraLiveStream.ExtendWebRtcStream",
            "params": {"mediaSessionId": media_session_id},
        }
        async with self._session.post(url, json=payload, headers=self._auth_headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        new_id = data.get("results", {}).get("mediaSessionId", "")
        if not new_id:
            raise ValueError("SDM returned empty mediaSessionId from ExtendWebRtcStream")
        return new_id

    async def close(self) -> None:
        await self._session.close()
