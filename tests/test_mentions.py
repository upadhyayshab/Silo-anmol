"""The consolidated MentionSource matching, and the platform_id unique constraint."""

import pytest
from sqlalchemy.exc import IntegrityError

import models
from models import Mention, MentionSource


def _make_mention(id, source, platform_id=None, parent_id=None, author="Someone"):
    return Mention(
        id=id,
        source=source,
        author=author,
        content="test content",
        sentiment="Neutral",
        platform_id=platform_id,
        parent_id=parent_id,
    )


def test_source_filter_matches_regardless_of_casing(client, db_session, auth_headers):
    db_session.add(_make_mention("m1", MentionSource.FACEBOOK))
    db_session.add(_make_mention("m2", MentionSource.YOUTUBE))
    db_session.commit()

    # The stored value is "Facebook", but the query param is lowercase - the
    # consolidated filter in main.py should still match it via MentionSource.ALL.
    res = client.get("/api/mentions/recent", params={"source": "facebook"}, headers=auth_headers)
    assert res.status_code == 200
    sources = {m["source"] for m in res.json()}
    assert sources == {"Facebook"}


def test_source_filter_all_returns_everything(client, db_session, auth_headers):
    db_session.add(_make_mention("m1", MentionSource.FACEBOOK))
    db_session.add(_make_mention("m2", MentionSource.YOUTUBE))
    db_session.commit()

    res = client.get("/api/mentions/recent", params={"source": "All"}, headers=auth_headers)
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_duplicate_platform_id_is_rejected_at_the_database_level(db_session):
    """This is the exact race-condition protection added earlier this session:
    the scheduler and the live webhook could otherwise both save the same
    comment. The unique constraint must make a duplicate impossible, not just
    unlikely."""
    db_session.add(_make_mention("m1", MentionSource.FACEBOOK, platform_id="fb_comment_123"))
    db_session.commit()

    db_session.add(_make_mention("m2", MentionSource.FACEBOOK, platform_id="fb_comment_123"))
    with pytest.raises(IntegrityError):
        db_session.commit()
