import sys
import json
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestEventBrokerServiceMemoryMode(IsolatedAsyncioTestCase):
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_memory_mode_never_calls_real_publish(self, mock_publish, mock_get_settings):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "memory"

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")

        mock_publish.assert_not_called()

    @patch("services.eventBrokerService.logger")
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_memory_mode_logs_at_warning_not_debug(self, mock_publish, mock_get_settings, mock_logger):
        # Regression for review finding Important #4: a misconfigured
        # production deployment (BROKER_TYPE left at its "memory" default)
        # must be loud about dropping every CropGuideCompleted event --
        # logger.debug sits below most production log levels and would make
        # the drop invisible.
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "memory"

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")

        mock_logger.warning.assert_called_once()
        mock_logger.debug.assert_not_called()


class TestEventBrokerServiceKafkaMode(IsolatedAsyncioTestCase):
    @patch("services.eventBrokerService.EventOutboxManager")
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_kafka_mode_publishes_directly_on_success(
        self, mock_publish, mock_get_settings, mock_outbox_manager_cls
    ):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "kafka"
        mock_outbox_instance = mock_outbox_manager_cls.return_value
        mock_outbox_instance.create = AsyncMock()

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")

        mock_publish.assert_awaited_once()
        topic_arg, payload_arg = mock_publish.call_args.args[0], mock_publish.call_args.args[1]
        self.assertEqual(topic_arg, "crop_guide_completed")
        self.assertEqual(json.loads(payload_arg)["event_id"], "evt-1")
        mock_outbox_instance.create.assert_not_called()

    @patch("services.eventBrokerService.EventOutboxManager")
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_publish_failure_falls_back_to_outbox_not_raise(
        self, mock_publish, mock_get_settings, mock_outbox_manager_cls
    ):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "kafka"
        mock_publish.side_effect = RuntimeError("MSK unreachable")
        mock_outbox_instance = mock_outbox_manager_cls.return_value
        mock_outbox_instance.create = AsyncMock()

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")  # must NOT raise

        mock_outbox_instance.create.assert_awaited_once()
        outbox_row = mock_outbox_instance.create.call_args.args[0]
        self.assertEqual(outbox_row.topic, "crop_guide_completed")
        self.assertEqual(outbox_row.payload["event_id"], "evt-1")
        self.assertEqual(outbox_row.status, "PENDING")

    @patch("services.eventBrokerService.get_settings")
    async def test_sqs_mode_raises_not_implemented(self, mock_get_settings):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "sqs"

        service = EventBrokerService()
        with self.assertRaises(NotImplementedError):
            await service.publish("crop_guide_completed", {"event_id": "evt-1"})
