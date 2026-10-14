"""Review Focus: replay must not double-publish an event whose status
already moved past PENDING (e.g. a concurrent replayer run, or a row
that actually succeeded before the outbox write happened)."""
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
