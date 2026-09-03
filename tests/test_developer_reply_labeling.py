"""Verifies the mislabeling bug is actually fixed: a nested reply from a real
customer (not the business) must go through real AI sentiment analysis and
get a real label - not the hardcoded "Neutral / Developer Reply / Outbound
response by developer" that used to apply to ANY nested reply just because
it had a parent_id, regardless of who actually wrote it."""

import collector
from models import Mention


def test_customer_reply_to_another_customer_gets_real_sentiment_not_developer_label(db_session, monkeypatch):
    """This is the exact scenario from the session: "John Smith" (a random
    commenter, not the business) replies to someone else's comment. Before
    the fix, this got mislabeled purely because parent_id was set."""
    parent = Mention(id="parent1", source="Facebook", author="Original Commenter", content="original", sentiment="Neutral")
    collector_db = db_session
    collector_db.add(parent)
    collector_db.commit()

    monkeypatch.setattr(collector, "analyze_sentiment", lambda content: ("Negative", "Bug/Performance", "Real AI analysis ran"))

    collector.save_mention(
        mention_id="reply1",
        source="Facebook",
        author="John Smith",  # NOT "Developer" - a real, unrelated commenter
        content="this app is broken",
        link="https://example.com",
        pub_date=None,
        platform_id="fb_reply_123",
        parent_id="parent1",  # nested - this alone used to trigger the mislabel
        db=collector_db,
    )

    saved = collector_db.query(Mention).filter(Mention.id == "reply1").first()
    assert saved.author == "John Smith"
    assert saved.sentiment == "Negative", "a real customer reply must get real AI sentiment, not a hardcoded 'Neutral'"
    assert saved.label == "Bug/Performance", "must get the real AI label, not 'Developer Reply'"
    assert saved.explanation == "Real AI analysis ran"


def test_genuine_developer_reply_still_gets_the_developer_label(db_session):
    """The fix must not break the correct, intended case: a reply actually
    authored by "Developer" should still skip AI analysis and get labeled
    as a developer reply."""
    parent = Mention(id="parent2", source="Facebook", author="Customer", content="question", sentiment="Neutral")
    db_session.add(parent)
    db_session.commit()

    collector.save_mention(
        mention_id="reply2",
        source="Facebook",
        author="Developer",
        content="Thanks for reaching out!",
        link="https://example.com",
        pub_date=None,
        platform_id="fb_reply_456",
        parent_id="parent2",
        db=db_session,
    )

    saved = db_session.query(Mention).filter(Mention.id == "reply2").first()
    assert saved.sentiment == "Neutral"
    assert saved.label == "Developer Reply"
    assert saved.explanation == "Outbound response by developer."
