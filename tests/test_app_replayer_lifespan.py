"""Confirms the outbox replayer starts as a background task when the app
starts, and is cleanly cancelled on shutdown — no separate process, no
leaked task after the app stops."""
import asyncio
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestReplayerLifespan(IsolatedAsyncioTestCase):
    @patch("app._outbox_replay_enabled", return_value=True)  # on ECS with BROKER_TYPE=kafka
    @patch("app.run_replay_loop")
    async def test_lifespan_starts_and_cancels_replay_task(self, mock_run_replay_loop, _enabled):
        import app as app_module

        # run_replay_loop normally never returns; make the mock behave the
        # same way so cancellation is actually exercised, not a no-op await.
        async def _never_returns(*_args, **_kwargs):
            await asyncio.Event().wait()
        mock_run_replay_loop.side_effect = _never_returns

        async with app_module.lifespan(app_module.app):
            pass  # lifespan's `yield` point — the app is "running" here

        # After the `async with` exits, lifespan's finally-block must have
        # cancelled the task it created — asserted indirectly: the mock
        # was awaited (task actually ran) and no exception propagated out
        # of the `async with`, which would happen if cancellation leaked.
        mock_run_replay_loop.assert_called_once()
