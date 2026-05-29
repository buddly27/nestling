import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_MIN_INTERVAL = 60.0
_RETRY_INTERVAL = 30.0


class TokenManager:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token: str = ""
        self._task: asyncio.Task | None = None

    @property
    def token(self) -> str:
        return self._token

    async def start(self) -> None:
        """Perform the initial token refresh (fail-fast) then launch the background loop."""
        async with aiohttp.ClientSession() as session:
            expires_in = await self._refresh(session)
        self._task = asyncio.create_task(self._refresh_loop(expires_in))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _refresh(self, session: aiohttp.ClientSession) -> float:
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }
        async with session.post(_TOKEN_URL, data=data) as resp:
            resp.raise_for_status()
            result = await resp.json()
        token = result.get("access_token", "")
        if not token:
            raise ValueError("empty access_token in response")
        self._token = token
        expires_in = float(result.get("expires_in", 3600))
        log.info("auth: token refreshed, expires_in=%.0fs", expires_in)
        return expires_in

    async def _refresh_loop(self, initial_expires_in: float) -> None:
        expires_in = initial_expires_in
        async with aiohttp.ClientSession() as session:
            while True:
                interval = max(expires_in * 0.9, _MIN_INTERVAL)
                await asyncio.sleep(interval)
                try:
                    expires_in = await self._refresh(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("auth: token refresh failed: %s; retrying in %.0fs", exc, _RETRY_INTERVAL)
                    await asyncio.sleep(_RETRY_INTERVAL)
