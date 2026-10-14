"""Review Focus: replay must not double-publish an event whose status
already moved past PENDING (e.g. a concurrent replayer run, or a row
that actually succeeded before the outbox write happened)."""
import asyncio
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestReplayPendingEvents(IsolatedAsyncioTestCase):
    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_replays_pending_rows_and_marks_completed(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events
        import json

        row = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                         payload={"event_id": "evt-1"}, status="PENDING", retry_count=0)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row]))
        mock_outbox_manager.update = AsyncMock()
        mock_session = AsyncMock()

        count = await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        self.assertEqual(count, 1)
        mock_publish.assert_awaited_once()
        published_topic, published_payload = mock_publish.call_args.args[0], mock_publish.call_args.args[1]
        self.assertEqual(published_topic, "crop_guide_completed")
        self.assertEqual(json.loads(published_payload)["event_id"], "evt-1")
        update_calls = mock_outbox_manager.update.call_args_list
        # First call flips PENDING -> PROCESSING (claim), second flips to COMPLETED.
        self.assertEqual(update_calls[0].kwargs["updates"]["status"], "PROCESSING")
        self.assertEqual(update_calls[-1].kwargs["updates"]["status"], "COMPLETED")

    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_skips_row_not_pending_after_claim(self, mock_publish):
        # Review Focus: simulates a concurrent claim winning the race — the
        # update() that flips PENDING->PROCESSING reports 0 rows affected,
        # so this replayer must not publish it again.
        from workers.event_outbox_replayer import replay_pending_events

        row = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                         payload={"event_id": "evt-1"}, status="PENDING", retry_count=0)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row]))
        # update() returning a falsy/zero-rows result signals the claim lost the race.
        mock_outbox_manager.update = AsyncMock(return_value=None)
        mock_outbox_manager.claim_pending = AsyncMock(return_value=False)
        mock_session = AsyncMock()

        count = await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        self.assertEqual(count, 0)
        mock_publish.assert_not_called()

    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_publish_failure_increments_retry_count_not_completed(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        row = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                         payload={"event_id": "evt-1"}, status="PENDING", retry_count=2)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row]))
        mock_outbox_manager.update = AsyncMock()
        mock_outbox_manager.claim_pending = AsyncMock(return_value=True)
        mock_publish.side_effect = RuntimeError("still unreachable")
        mock_session = AsyncMock()

        count = await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        self.assertEqual(count, 0)
        last_update = mock_outbox_manager.update.call_args_list[-1]
        self.assertEqual(last_update.kwargs["updates"]["status"], "PENDING")
        self.assertEqual(last_update.kwargs["updates"]["retry_count"], 3)

    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_commits_each_row_before_a_later_row_in_the_batch_fails(self, mock_publish):
        # Review Focus: pins the fix for a double-publish bug. The pre-fix code
        # opened one session per batch (up to 50 rows) and called
        # session.commit() exactly once, after the whole batch finished, in
        # run_replay_loop -- not inside replay_pending_events. Every per-row
        # update() (including the COMPLETED write after a successful publish)
        # only flushes; it does not commit. So if asyncio.CancelledError (an
        # ordinary SIGTERM shutdown via app.py's `finally: replay_task.cancel()`,
        # not just a rare crash) arrived while row 2 of a 2-row batch was still
        # awaiting publish_event, it would propagate straight out of
        # replay_pending_events *before* any commit ever ran -- discarding row
        # 1's already-flushed COMPLETED write along with it. On the next
        # process start, row 1 (already genuinely published to Kafka) would be
        # re-fetched as PENDING and published again: a real double-publish with
        # exactly one process and zero concurrency.
        #
        # This test simulates exactly that: row 1 publishes successfully, then
        # row 2's publish raises CancelledError mid-batch. It asserts that by
        # the time CancelledError propagates out, row 1's COMPLETED state was
        # already committed -- not merely flushed -- by checking session.commit
        # was awaited once (for row 1) before the exception surfaces. Against
        # the pre-fix code (commit only in run_replay_loop, once per batch,
        # never reached here because the exception propagates first), this
        # assertion fails: mock_session.commit is never awaited at all.
        from workers.event_outbox_replayer import replay_pending_events

        row1 = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                          payload={"event_id": "evt-1"}, status="PENDING", retry_count=0)
        row2 = MagicMock(uid="outbox-2", topic="crop_guide_completed", event_key="k2",
                          payload={"event_id": "evt-2"}, status="PENDING", retry_count=0)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row1, row2]))
        mock_outbox_manager.update = AsyncMock()
        mock_publish.side_effect = [None, asyncio.CancelledError()]
        mock_session = AsyncMock()

        with self.assertRaises(asyncio.CancelledError):
            await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        # Row 1's terminal COMPLETED update must already be committed -- not
        # just flushed -- before row 2's CancelledError propagates out of this
        # function. One commit so far, for row 1; row 2 never reaches its own
        # terminal update (CancelledError is a BaseException, not caught by
        # the `except Exception` branch, so row 2 gets no update call at all).
        self.assertEqual(mock_session.commit.await_count, 1)
        update_calls = mock_outbox_manager.update.call_args_list
        # Row 1: claim (PROCESSING) then COMPLETED. Row 2: claim only (PROCESSING) --
        # its publish raised before any terminal update for row 2 was made.
        completed_updates = [c for c in update_calls if c.kwargs["updates"].get("status") == "COMPLETED"]
        self.assertEqual(len(completed_updates), 1)
        self.assertEqual(completed_updates[0].kwargs["uid"], "outbox-1")
