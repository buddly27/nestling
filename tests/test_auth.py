"""Unit tests for TokenManager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nestling.auth import TokenManager


def _make_tm() -> TokenManager:
    return TokenManager(
        client_id="test-client-id",
        client_secret="test-secret",
        refresh_token="test-refresh-token",
    )


def _mock_session(access_token: str = "tok-abc", expires_in: int = 3600):
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value={"access_token": access_token, "expires_in": expires_in})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestTokenManager:
    @pytest.mark.asyncio
    async def test_start_sets_token(self):
        tm = _make_tm()
        session = _mock_session("my-token")
        with patch("nestling.auth.aiohttp.ClientSession", return_value=session):
            await tm.start()
            await tm.stop()
        assert tm.token == "my-token"

    @pytest.mark.asyncio
    async def test_start_raises_on_empty_token(self):
        tm = _make_tm()
        session = _mock_session(access_token="")
        with patch("nestling.auth.aiohttp.ClientSession", return_value=session):
            with pytest.raises(ValueError, match="empty access_token"):
                await tm.start()

    @pytest.mark.asyncio
    async def test_stop_cancels_background_task(self):
        tm = _make_tm()
        session = _mock_session()
        with patch("nestling.auth.aiohttp.ClientSession", return_value=session):
            await tm.start()
            assert tm._task is not None
            await tm.stop()
            assert tm._task is None

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        tm = _make_tm()
        session = _mock_session()
        with patch("nestling.auth.aiohttp.ClientSession", return_value=session):
            await tm.start()
            await tm.stop()
            await tm.stop()  # must not raise
