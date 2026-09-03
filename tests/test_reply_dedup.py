"""Verifies the actual mechanism behind "replies show up instantly instead of
waiting for the next fetch cycle": when a reply is posted, we immediately save
it as a mention using the exact same platform_id the fetch cycle would later
compute for that same reply - so when the cycle re-scans and tries to save it
again, save_mention()'s existing dedup check recognizes it and skips it,
instead of creating a duplicate card. This is the concern raised mid-session:
without matching platform_id exactly, you'd get two cards for one real reply."""

from unittest.mock import MagicMock

import meta_service
import playstore_service
from models import Mention, MentionSource


def _make_mention(id, source, platform_id, link="https://example.com"):
    return Mention(
        id=id,
        source=source,
        author="Someone",
        content="original comment",
        sentiment="Neutral",
        platform_id=platform_id,
        link=link,
    )


def test_facebook_reply_is_saved_instantly_and_cycle_reconciliation_does_not_duplicate(db_session, monkeypatch):
    parent = _make_mention("m1", MentionSource.FACEBOOK, platform_id="fb_original_comment_123")
    db_session.add(parent)
    db_session.commit()

    # Facebook's real API returns the new comment's own ID in the response.
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"id": "fb_new_reply_999"}
    monkeypatch.setattr(meta_service.requests, "post", lambda *a, **kw: fake_response)
    monkeypatch.setattr(meta_service, "get_meta_pages", lambda token: [{"id": "page1", "name": "My Page", "access_token": "tok"}])
    monkeypatch.setattr(meta_service, "resolve_facebook_page", lambda mention, pages, token: (pages[0], "page1"))

    # Step 1: post the reply - this should save it as a mention right away.
    meta_service.post_facebook_reply(parent, "Thanks for your comment!", user_token="fake-token", db=db_session)

    replies = db_session.query(Mention).filter(Mention.parent_id == "m1").all()
    assert len(replies) == 1
    assert replies[0].content == "Thanks for your comment!"
    assert replies[0].author == "Developer"
    assert replies[0].platform_id == "fb_new_reply_999"

    # Step 2: simulate the next fetch cycle re-scanning Facebook and finding
    # that same real comment again (same platform_id, since it's the same
    # real-world comment) - this must NOT create a second card.
    meta_service.save_mention(
        mention_id="fb_fb_new_reply_999",
        source=MentionSource.FACEBOOK,
        author="Developer",
        content="Thanks for your comment!",
        link="https://example.com",
        pub_date=None,
        platform_id="fb_new_reply_999",
        parent_id="m1",
        db=db_session,
    )

    replies_after_cycle = db_session.query(Mention).filter(Mention.parent_id == "m1").all()
    assert len(replies_after_cycle) == 1, "the fetch cycle re-discovering the same reply must not create a duplicate card"


def test_playstore_reply_is_saved_instantly_and_cycle_reconciliation_does_not_duplicate(db_session, monkeypatch):
    parent = _make_mention(
        "m2", MentionSource.PLAYSTORE, platform_id="review_abc",
        link="https://play.google.com/store/apps/details?id=com.example.app&reviewId=review_abc"
    )
    db_session.add(parent)
    db_session.commit()

    monkeypatch.setattr(playstore_service, "Credentials", MagicMock())
    fake_play_service = MagicMock()
    monkeypatch.setattr(playstore_service, "build", lambda *a, **kw: fake_play_service)

    # Step 1: post the reply.
    playstore_service.reply_to_review(parent, "Thanks for the review!", db=db_session)

    replies = db_session.query(Mention).filter(Mention.parent_id == "m2").all()
    assert len(replies) == 1
    assert replies[0].platform_id == "reply_review_abc"  # matches fetch_playstore_reviews()'s own convention

    # Step 2: simulate the cycle re-discovering this same reply (Play Store
    # reviews always compute this same deterministic ID) - must not duplicate.
    playstore_service.save_mention(
        mention_id="play_reply_review_abc",
        source=MentionSource.PLAYSTORE,
        author="Developer",
        content="Thanks for the review!",
        link=parent.link,
        pub_date=None,
        platform_id="reply_review_abc",
        parent_id="m2",
        db=db_session,
    )

    replies_after_cycle = db_session.query(Mention).filter(Mention.parent_id == "m2").all()
    assert len(replies_after_cycle) == 1, "the fetch cycle re-discovering the same reply must not create a duplicate card"
