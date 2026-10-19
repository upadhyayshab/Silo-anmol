"""A publish that gives up must leave no Kafka client behind.

publish_event used to return (raise) after its deadline while the producer it
created stayed alive: the exception's traceback held it, so it kept trying to
connect in the background and ~30 s later logged "Connection setup timed out
in state CONNECT". During a Kafka outage every submit and every outbox replay
left one more of these clients (threads and a socket) behind. Mocks only,
never a real broker.
"""
import gc
import sys
import weakref
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class FakeProducer:
    """Stands in for confluent_kafka.Producer; weak-referenceable so the test
    can tell whether anything still holds it."""

    def __init__(self, unflushed: int):
        self.unflushed = unflushed
        self.purged = 0

    def produce(self, **kwargs):
        pass

    def flush(self, timeout=None):
        return self.unflushed

    def purge(self, *args, **kwargs):
        self.purged += 1


class TestProducerCleanup(IsolatedAsyncioTestCase):
    async def test_giving_up_purges_and_releases_the_producer(self):
        from confluent_kafka import KafkaException
        from services.kafka_client import publish_event

        handed_over = [FakeProducer(unflushed=1)]  # still queued at the deadline = gave up
        alive = weakref.ref(handed_over[0])
        # A plain function, not a MagicMock: mocks are themselves reference
        # cycles and would keep the fake alive with the GC off.
        make_producer = lambda extra=None: handed_over.pop()  # noqa: E731
        gc.collect()
        gc.disable()  # a GC pass would hide the leak: it frees the cycle the exception sits in
        try:
            raised = None
            with patch("services.kafka_client.make_producer", new=make_producer):
                # A plain except, as in EventBrokerService.publish: assertRaises
                # would clear the traceback's frames and hide the leak.
                try:
                    await publish_event("crop_guide_completed", "{}", key=None, timeout=3.0)
                except KafkaException as exc:
                    raised = exc

            # The exception and its full traceback are still held here, like the
            # caller holds them while writing the outbox row: they must not keep
            # the client alive until some later GC pass.
            self.assertIsNotNone(raised)
            self.assertIsNotNone(raised.__traceback__)
            self.assertIsNone(alive(), "the timed-out producer is still referenced and keeps reconnecting")
        finally:
            gc.enable()

    async def test_giving_up_drops_the_queued_message(self):
        from confluent_kafka import KafkaException
        from services.kafka_client import publish_event

        producer = FakeProducer(unflushed=1)
        with patch("services.kafka_client.make_producer", return_value=producer):
            with self.assertRaises(KafkaException):
                await publish_event("crop_guide_completed", "{}", key=None, timeout=3.0)

        # The caller keeps the event in event_outbox; the client must not also
        # try to send it (that would deliver it twice).
        self.assertEqual(producer.purged, 1)

    async def test_a_delivered_message_is_not_purged(self):
        from services.kafka_client import publish_event

        producer = FakeProducer(unflushed=0)
        with patch("services.kafka_client.make_producer", return_value=producer):
            await publish_event("crop_guide_completed", "{}", key=None, timeout=3.0)

        self.assertEqual(producer.purged, 0)

    async def test_connection_attempt_ends_at_the_publish_deadline(self):
        """librdkafka's default is 30 s; a hung connect (unreachable private
        broker) must give up when the publish does."""
        from services.kafka_client import publish_event

        for timeout, expected_ms in ((3.0, 3000), (0.2, 1000)):  # librdkafka's minimum is 1000 ms
            with patch("services.kafka_client.make_producer", return_value=FakeProducer(unflushed=0)) as make:
                await publish_event("crop_guide_completed", "{}", key=None, timeout=timeout)
            self.assertEqual(make.call_args.args[0]["socket.connection.setup.timeout.ms"], expected_ms)
