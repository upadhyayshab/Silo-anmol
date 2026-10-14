"""Unit tests for the Kafka producer wrapper — mocks confluent_kafka.Producer
entirely, never touches a real broker."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestPublishEvent(IsolatedAsyncioTestCase):
    @patch("services.kafka_client.make_producer")
    async def test_publish_event_produces_and_flushes(self, mock_make_producer):
        from services.kafka_client import publish_event

        mock_producer = MagicMock()
        mock_producer.flush.return_value = 0  # 0 = nothing left un-flushed
        mock_make_producer.return_value = mock_producer

        await publish_event("crop_guide_completed", '{"event_id": "evt-1"}', key="usr_1")

        mock_producer.produce.assert_called_once()
        call_kwargs = mock_producer.produce.call_args.kwargs
        self.assertEqual(call_kwargs["topic"], "crop_guide_completed")
        self.assertEqual(call_kwargs["value"], b'{"event_id": "evt-1"}')
        self.assertEqual(call_kwargs["key"], b"usr_1")

    @patch("services.kafka_client.make_producer")
    async def test_publish_event_raises_on_flush_timeout(self, mock_make_producer):
        from services.kafka_client import publish_event
        from confluent_kafka import KafkaException

        mock_producer = MagicMock()
        mock_producer.flush.return_value = 1  # 1 message still un-flushed = timeout
        mock_make_producer.return_value = mock_producer

        with self.assertRaises(KafkaException):
            await publish_event("crop_guide_completed", "{}", key=None)
