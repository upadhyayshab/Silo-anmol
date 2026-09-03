"""Verifies _get_all_replies() actually fetches every reply on a comment,
not just the 5 YouTube embeds inline in commentThreads.list() - and that it
only makes the extra API call when genuinely needed, to avoid burning quota
on comments that already have every reply embedded."""

from unittest.mock import MagicMock

from youtube_service import _get_all_replies


def _comment_thread(comment_id, total_reply_count, embedded_reply_ids):
    return {
        "id": comment_id,
        "snippet": {"totalReplyCount": total_reply_count},
        "replies": {"comments": [{"id": rid, "snippet": {}} for rid in embedded_reply_ids]},
    }


def test_uses_embedded_replies_when_nothing_is_missing():
    """A comment with 3 replies: all 3 are embedded already (under the cap of
    5), so no extra API call should happen at all."""
    comment_item = _comment_thread("c1", total_reply_count=3, embedded_reply_ids=["r1", "r2", "r3"])
    youtube_client = MagicMock()

    replies = _get_all_replies(youtube_client, comment_item)

    assert [r["id"] for r in replies] == ["r1", "r2", "r3"]
    youtube_client.comments().list.assert_not_called()


def test_paginates_through_every_reply_when_more_exist_than_embedded():
    """A comment with 12 real replies: YouTube only embeds 5. This must fetch
    ALL 12 via pagination, not stop at 5 or any other arbitrary number."""
    comment_item = _comment_thread("c2", total_reply_count=12, embedded_reply_ids=["r1", "r2", "r3", "r4", "r5"])

    youtube_client = MagicMock()
    page_1 = {"items": [{"id": f"r{i}", "snippet": {}} for i in range(1, 11)], "nextPageToken": "page2"}
    page_2 = {"items": [{"id": f"r{i}", "snippet": {}} for i in range(11, 13)]}  # no nextPageToken -> last page
    youtube_client.comments().list().execute.side_effect = [page_1, page_2]

    replies = _get_all_replies(youtube_client, comment_item)

    assert len(replies) == 12, "must fetch every real reply, not cap at 5 or any fixed number"
    assert [r["id"] for r in replies] == [f"r{i}" for i in range(1, 13)]

    # Confirm it actually paginated (called list() for each page) rather than
    # just grabbing one page and stopping.
    assert youtube_client.comments().list().execute.call_count == 2
