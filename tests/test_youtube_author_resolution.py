"""Verifies YouTube now actually detects when a comment/reply is from one of
our own channels - previously it never checked at all, so even the channel's
own replies showed up under the channel's real display name instead of
"Developer", and got run through AI sentiment analysis unnecessarily."""

import youtube_service
from youtube_service import _resolve_author


def test_returns_developer_when_author_channel_matches_target_channel_ids(monkeypatch):
    monkeypatch.setattr(youtube_service, "TARGET_CHANNEL_IDS", {"UCourChannel123"})

    result = _resolve_author("Gau Sampurna Official", "UCourChannel123")

    assert result == "Developer"


def test_returns_developer_when_author_channel_matches_known_channel_id(monkeypatch):
    """fetch_channel_comments() passes known_channel_id explicitly, since it
    already knows exactly which channel it's scanning - should match even if
    TARGET_CHANNEL_IDS happens to be empty/different."""
    monkeypatch.setattr(youtube_service, "TARGET_CHANNEL_IDS", set())

    result = _resolve_author("Gau Sampurna Official", "UCourChannel123", known_channel_id="UCourChannel123")

    assert result == "Developer"


def test_returns_real_name_for_an_unrelated_commenter(monkeypatch):
    monkeypatch.setattr(youtube_service, "TARGET_CHANNEL_IDS", {"UCourChannel123"})

    result = _resolve_author("Random Viewer", "UCsomeoneElse456")

    assert result == "Random Viewer"


def test_returns_real_name_when_author_channel_id_is_missing():
    """Some comments don't expose an authorChannelId at all - must not crash,
    and must not accidentally treat a missing ID as a match."""
    result = _resolve_author("Anonymous Commenter", "")

    assert result == "Anonymous Commenter"
