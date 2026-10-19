"""event_outbox retries: back off instead of giving up after ~5 minutes, and
replay only where Kafka is reachable.

On 2026-10-19 a wizard run's crop_guide_completed event timed out (the
service was running on a laptop that can't reach the private MSK brokers).
The replay loop retried it 5 times in 5 minutes, marked it FAILED and never
tried again, so the farmer's farm was never created in Agro. The laptop's own
replay loop, pointed at the shared dev database, was what spent those tries.
Mocks only, never a real broker or database.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

NOW = datetime.now(timezone.utc)


def _row(uid="outbox-1", status="PENDING", retry_count=0, created_ago=timedelta(minutes=1), tried_ago=None):
    created = NOW - created_ago
    return MagicMock(uid=uid, topic="crop_guide_completed", event_key="k", payload={"event_id": uid},
                     status=status, retry_count=retry_count, created_at=created,
                     updated_at=(NOW - tried_ago) if tried_ago is not None else created)


def _manager(pending=(), failed=()):
    manager = MagicMock()

    async def fetch_all(filters=None, **_kwargs):
        return MagicMock(items=list(failed if filters.get("status") == "FAILED" else pending))

    manager.fetch_all = AsyncMock(side_effect=fetch_all)
    manager.update = AsyncMock(return_value=True)
    return manager


def _last_update(manager):
    return manager.update.call_args_list[-1].kwargs["updates"]


class TestBackoff(TestCase):
    def test_retry_gaps_grow_then_cap_at_15_minutes(self):
        from workers.event_outbox_replayer import _retry_delay

        self.assertEqual([_retry_delay(n).total_seconds() / 60 for n in range(0, 8)], [0, 1, 2, 4, 8, 15, 15, 15])


@patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
class TestReplay(IsolatedAsyncioTestCase):
    async def test_failure_keeps_the_event_pending_past_the_old_5_try_limit(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        mock_publish.side_effect = RuntimeError("Local: Message timed out")
        manager = _manager(pending=[_row(retry_count=4, created_ago=timedelta(minutes=30), tried_ago=timedelta(minutes=9))])

        await replay_pending_events(manager, AsyncMock())

        # The old rule marked this FAILED (5th attempt) and never retried it.
        self.assertEqual(_last_update(manager), {"status": "PENDING", "retry_count": 5, "last_error": "Local: Message timed out"})

    async def test_gives_up_only_after_7_days(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        mock_publish.side_effect = RuntimeError("still unreachable")
        manager = _manager(pending=[_row(retry_count=700, created_ago=timedelta(days=7, minutes=1), tried_ago=timedelta(minutes=16))])

        await replay_pending_events(manager, AsyncMock())

        self.assertEqual(_last_update(manager)["status"], "FAILED")

    async def test_a_row_inside_its_backoff_is_not_tried(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        # 3 failures so far -> next try 4 minutes after the last; only 1 minute passed.
        manager = _manager(pending=[_row(retry_count=3, created_ago=timedelta(minutes=20), tried_ago=timedelta(minutes=1))])

        replayed = await replay_pending_events(manager, AsyncMock())

        self.assertEqual(replayed, 0)
        mock_publish.assert_not_called()
        manager.update.assert_not_called()

    async def test_a_row_past_its_backoff_is_delivered(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        manager = _manager(pending=[_row(retry_count=3, created_ago=timedelta(minutes=20), tried_ago=timedelta(minutes=5))])

        replayed = await replay_pending_events(manager, AsyncMock())

        self.assertEqual(replayed, 1)
        self.assertEqual(_last_update(manager), {"status": "COMPLETED"})

    async def test_a_row_failed_under_the_old_rule_is_delivered_after_all(self, mock_publish):
        """FAILED by the 5-try rule, still inside 7 days: replayed (Agro drops
        duplicates by event_id), so the missing farm gets created."""
        from workers.event_outbox_replayer import replay_pending_events

        manager = _manager(failed=[_row(status="FAILED", retry_count=5, created_ago=timedelta(hours=3), tried_ago=timedelta(hours=3))])

        replayed = await replay_pending_events(manager, AsyncMock())

        self.assertEqual(replayed, 1)
        mock_publish.assert_awaited_once()
        failed_query = [c.kwargs["filters"] for c in manager.fetch_all.call_args_list if c.kwargs["filters"]["status"] == "FAILED"][0]
        self.assertGreaterEqual(failed_query["created_at"]["$gte"], NOW - timedelta(days=7, minutes=1))

    async def test_a_row_in_both_queries_is_sent_once(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        row = _row()
        manager = _manager(pending=[row], failed=[row])

        self.assertEqual(await replay_pending_events(manager, AsyncMock()), 1)
        mock_publish.assert_awaited_once()


class TestWhereReplayRuns(TestCase):
    def _enabled(self, broker="kafka", flag="", ecs=False):
        import app as app_module

        with patch.object(app_module.settings, "broker_type", broker), \
             patch.object(app_module.settings, "outbox_replay_enabled", flag), \
             patch("app._on_server", return_value=ecs):
            return app_module._outbox_replay_enabled()

    def test_server_detection_reads_the_ecs_variable(self):
        import app as app_module

        with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x"}):
            self.assertTrue(app_module._on_server())
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(app_module._on_server())

    def test_on_ecs_with_kafka(self):
        self.assertTrue(self._enabled(ecs=True))

    def test_not_on_a_laptop_even_with_kafka_configured(self):
        """The case that spent dev's retries: a local service against dev's database."""
        self.assertFalse(self._enabled(ecs=False))

    def test_never_without_kafka(self):
        self.assertFalse(self._enabled(broker="memory", ecs=True))
        self.assertFalse(self._enabled(broker="memory", flag="1"))

    def test_the_setting_forces_it(self):
        self.assertTrue(self._enabled(flag="1"))           # e.g. a local Kafka test
        self.assertFalse(self._enabled(flag="0", ecs=True))  # switched off on a server


class TestLifespanWhenOff(IsolatedAsyncioTestCase):
    @patch("app._outbox_replay_enabled", return_value=False)
    @patch("app.run_replay_loop")
    async def test_no_replay_task_when_off(self, mock_run_replay_loop, _enabled):
        import app as app_module

        async with app_module.lifespan(app_module.app):
            pass

        mock_run_replay_loop.assert_not_called()
