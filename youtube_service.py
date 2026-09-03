"""
YouTube integration: fetching comments and posting replies.

Two separate YouTube clients are used, for two different reasons:
  - `youtube` (this module's OAuth-authenticated client, set up via
    get_youtube_client()) can post replies as the channel owner - needed for
    both fetch_and_store_youtube_data() (which also reads comments) and
    reply_to_comment().
  - fetch_channel_comments() builds its own simple API-key client instead,
    since it only ever reads public data and doesn't need reply capability -
    it works even if the OAuth login below hasn't been set up.
"""

import os
import uuid
import pickle
import logging
import traceback
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest

import models
from collector import analyze_sentiment, save_mention

logger = logging.getLogger(__name__)

# Used to detect when a comment/reply is actually from one of our own channels,
# so it gets correctly labeled "Developer" instead of showing up as if a
# random viewer posted it. Same TARGET_CHANNEL_IDS env var fetch_channel_comments()
# is called with elsewhere - re-read here since keyword-search-based fetching
# doesn't otherwise know which channel(s) are "ours".
TARGET_CHANNEL_IDS = {cid.strip() for cid in os.getenv("TARGET_CHANNEL_IDS", "").split(",") if cid.strip()}


def _resolve_author(display_name, author_channel_id, known_channel_id=None):
    """Returns "Developer" if author_channel_id belongs to one of our own
    channels (TARGET_CHANNEL_IDS), or matches known_channel_id when the
    caller already knows which specific channel is being scanned - otherwise
    returns the commenter's real display name, same as Facebook/Instagram
    already do for their own comments."""
    if author_channel_id and (author_channel_id == known_channel_id or author_channel_id in TARGET_CHANNEL_IDS):
        return "Developer"
    return display_name

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def get_youtube_client(allow_interactive: bool = True):
    credentials = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            credentials = pickle.load(token)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleRequest())
        elif allow_interactive:
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            credentials = flow.run_local_server(port=8080)
        else:
            raise RuntimeError("No valid token.pickle found and interactive login is disabled at startup.")

        with open("token.pickle", "wb") as token:
            pickle.dump(credentials, token)

    return build("youtube", "v3", credentials=credentials)


try:
    youtube = get_youtube_client(allow_interactive=False)
except Exception as e:
    logger.warning(f"[WARNING] YouTube client not initialized: {e}. YouTube fetch/reply features will be unavailable until token.pickle is restored or refreshed.")
    youtube = None


def _get_all_replies(youtube_client, comment_item):
    """Returns the complete list of replies for a comment thread - never
    capped, unlike the `replies` field commentThreads.list() embeds directly,
    which YouTube always truncates to at most 5 regardless of how many
    replies actually exist. Only makes the extra comments.list() call (and
    paginates through ALL of it, no artificial limit) when the thread's own
    totalReplyCount proves the embedded list is incomplete - avoids burning
    YouTube API quota on comments that already have every reply embedded."""
    total_reply_count = comment_item.get("snippet", {}).get("totalReplyCount", 0)
    embedded_replies = comment_item.get("replies", {}).get("comments", [])

    if total_reply_count <= len(embedded_replies):
        return embedded_replies

    comment_id = comment_item["id"]
    all_replies = []
    next_token = None
    while True:
        response = youtube_client.comments().list(
            parentId=comment_id,
            part="snippet",
            maxResults=100,
            pageToken=next_token,
        ).execute()
        all_replies.extend(response.get("items", []))
        next_token = response.get("nextPageToken")
        if not next_token:
            break

    logger.info(f"    [INFO] Comment {comment_id} has {total_reply_count} replies (more than the 5 YouTube embeds inline) - fetched all {len(all_replies)} via pagination.")
    return all_replies


def fetch_and_store_youtube_data(keyword: str, db):
    if youtube is None:
        logger.warning(f"Skipping YouTube fetch for '{keyword}': YouTube client not authenticated.")
        return
    try:
        search_response = youtube.search().list(
            q=keyword, part='snippet', type='video', maxResults=5, order='date'
        ).execute()
    except Exception as e:
        logger.error(f"YouTube API connection dropped for '{keyword}'. Skipping until next cycle. Error: {e}")
        return

    for item in search_response.get('items', []):
        video_id = item['id']['videoId']
        video_snippet = item['snippet']
        video_title = video_snippet['title']

        existing_video = db.query(models.Mention).filter(models.Mention.platform_id == video_id).first()

        if not existing_video:
            video_mention = models.Mention(
                id=str(uuid.uuid4()),
                source=models.MentionSource.YOUTUBE,
                platform_id=video_id,
                author=video_snippet['channelTitle'],
                content=f"Video Title: {video_title}",
                sentiment="Neutral",
                label="Video Upload",
                explanation="A new YouTube video mentioning the brand was uploaded.",
                link=f"https://www.youtube.com/watch?v={video_id}",
                date=datetime.strptime(video_snippet['publishedAt'], "%Y-%m-%dT%H:%M:%SZ")
            )
            db.add(video_mention)

        try:
            comments_response = youtube.commentThreads().list(
                videoId=video_id,
                part='snippet,replies,id',
                maxResults=10
            ).execute()

            for comment_item in comments_response.get('items', []):
                c_id = comment_item['id']

                existing_comment = db.query(models.Mention).filter(models.Mention.platform_id == c_id).first()

                if not existing_comment:
                    c_snippet = comment_item['snippet']['topLevelComment']['snippet']
                    c_text = c_snippet['textDisplay']
                    c_author = _resolve_author(c_snippet['authorDisplayName'], c_snippet.get('authorChannelId', {}).get('value', ''))

                    if c_author == "Developer":
                        ai_sentiment, ai_label, ai_explanation = "Neutral", "Developer Reply", "Outbound response by developer."
                    else:
                        ai_sentiment, ai_label, ai_explanation = analyze_sentiment(c_text)

                    comment_mention = models.Mention(
                        id=str(uuid.uuid4()),
                        source=models.MentionSource.YOUTUBE,
                        platform_id=c_id,
                        author=c_author,
                        content=c_text,
                        sentiment=ai_sentiment,
                        label=ai_label,
                        explanation=ai_explanation,
                        link=f"https://www.youtube.com/watch?v={video_id}",
                        date=datetime.strptime(c_snippet['publishedAt'], "%Y-%m-%dT%H:%M:%SZ")
                    )
                    db.add(comment_mention)

                for reply in _get_all_replies(youtube, comment_item):
                    reply_snippet = reply['snippet']
                    reply_id = reply['id']

                    parent_platform_id = reply_snippet.get('parentId', c_id)

                    existing_reply = db.query(models.Mention).filter(models.Mention.platform_id == reply_id).first()

                    if not existing_reply:
                        reply_text = f"↳ [Reply]: {reply_snippet['textDisplay']}"
                        r_author = _resolve_author(reply_snippet['authorDisplayName'], reply_snippet.get('authorChannelId', {}).get('value', ''))

                        if r_author == "Developer":
                            ai_r_sentiment, ai_r_label, ai_r_explanation = "Neutral", "Developer Reply", "Outbound response by developer."
                        else:
                            ai_r_sentiment, ai_r_label, ai_r_explanation = analyze_sentiment(reply_text)

                        # Before saving reply_mention, find the parent comment's DB ID:
                        parent_comment_in_db = db.query(models.Mention).filter(models.Mention.platform_id == parent_platform_id).first()

                        if parent_comment_in_db:
                            reply_mention = models.Mention(
                                id=str(uuid.uuid4()),
                                source=models.MentionSource.YOUTUBE,
                                platform_id=reply_id,
                                parent_id=parent_comment_in_db.id,  # Use the DB primary key UUID, NOT the YouTube c_id
                                author=r_author,
                                content=reply_text,
                                sentiment=ai_r_sentiment,
                                label=ai_r_label,
                                explanation=ai_r_explanation,
                                link=f"https://www.youtube.com/watch?v={video_id}",
                                date=datetime.strptime(reply_snippet['publishedAt'], "%Y-%m-%dT%H:%M:%SZ")
                            )
                            db.add(reply_mention)

        except HttpError as e:
            error_msg = e.content.decode() if hasattr(e, 'content') else str(e)
            if e.resp.status == 403 and "commentsDisabled" in error_msg:
                logger.info(f"  > Skipping {video_id}: Comments are disabled.")
            else:
                logger.error(f"  > API Error on {video_id}: {e}")
        except Exception as e:
            logger.error(f"  > Skipping comments for {video_id}. Unknown Error: {e}")

    db.commit()


def fetch_channel_comments(channel_id: str, db=None):
    logger.info(f"\n--> [YouTube] Fetching deep historical comments for channel: {channel_id}")

    api_key = os.getenv("YOUTUBE_API_KEY")
    yt_service = build('youtube', 'v3', developerKey=api_key)

    try:
        channel_res = yt_service.channels().list(id=channel_id, part='contentDetails').execute()
        if not channel_res.get('items'):
            logger.warning("Channel not found!")
            return

        uploads_playlist_id = channel_res['items'][0]['contentDetails']['relatedPlaylists']['uploads']

        video_ids = []
        next_video_token = None

        while len(video_ids) < 50:
            playlist_res = yt_service.playlistItems().list(
                playlistId=uploads_playlist_id,
                part='snippet',
                maxResults=50,
                pageToken=next_video_token
            ).execute()

            for item in playlist_res.get('items', []):
                video_ids.append(item['snippet']['resourceId']['videoId'])

            next_video_token = playlist_res.get('nextPageToken')
            if not next_video_token:
                break

        logger.info(f"    [INFO] Found {len(video_ids)} videos. Checking comments...\n")

        for video_id in video_ids:
            try:
                next_comment_token = None

                while True:
                    comments_res = yt_service.commentThreads().list(
                        videoId=video_id,
                        part="snippet,replies",
                        maxResults=100,
                        order="time",
                        pageToken=next_comment_token
                    ).execute()

                    stop_scanning = False

                    for item in comments_res.get("items", []):
                        comment = item["snippet"]["topLevelComment"]["snippet"]
                        comment_id = item["id"]
                        stable_mention_id = f"yt_{comment_id}"

                        content = comment["textOriginal"]
                        author = _resolve_author(
                            comment["authorDisplayName"],
                            comment.get("authorChannelId", {}).get("value", ""),
                            known_channel_id=channel_id,
                        )
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        pub_date = datetime.strptime(comment["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")

                        save_mention(
                            stable_mention_id,
                            models.MentionSource.YOUTUBE,
                            author,
                            content,
                            video_url,
                            pub_date,
                            platform_id=comment_id,
                            parent_id=None,
                            db=db
                        )

                        for reply in _get_all_replies(yt_service, item):
                            reply_snippet = reply["snippet"]
                            reply_id = reply["id"]
                            reply_stable_id = f"yt_{reply_id}"
                            parent_id = reply_snippet.get("parentId", comment_id)

                            reply_content = reply_snippet['textOriginal']
                            reply_author = _resolve_author(
                                reply_snippet["authorDisplayName"],
                                reply_snippet.get("authorChannelId", {}).get("value", ""),
                                known_channel_id=channel_id,
                            )
                            reply_pub_date = datetime.strptime(reply_snippet["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")

                            save_mention(
                                reply_stable_id,
                                models.MentionSource.YOUTUBE,
                                reply_author,
                                reply_content,
                                video_url,
                                reply_pub_date,
                                platform_id=reply_id,
                                parent_id=parent_id,
                                db=db
                            )

                    if stop_scanning:
                        break

                    next_comment_token = comments_res.get("nextPageToken")
                    if not next_comment_token:
                        break

            except HttpError as e:
                error_msg = e.content.decode() if hasattr(e, 'content') else str(e)

                # 1. Check if we ran out of API requests for the day
                if "quotaExceeded" in error_msg:
                    logger.error(f"\n[🚨 CRITICAL] YOUTUBE API QUOTA EXCEEDED! Stopping YouTube fetch.")
                    return  # Safely exit the function so it doesn't crash

                # 2. Your clean catch for disabled comments
                elif e.resp.status == 403 and "commentsDisabled" in error_msg:
                    logger.info(f"  > Skipping {video_id}: Comments are disabled.")

                # 3. Any other random API error
                else:
                    logger.error(f"  > API Error on {video_id}: {error_msg}")

    except Exception as e:
        logger.error(f"\n[CRITICAL ERROR] YouTube Scraper crashed:\n{traceback.format_exc()}")


def reply_to_comment(mention, reply_text, db=None):
    """Posts a reply to a YouTube video (as a new top-level comment) or to an
    existing comment. Raises on failure - the caller (main.py) is responsible
    for turning that into an HTTP error response.

    On success, immediately saves the reply as a mention too (instead of
    waiting for the next fetch cycle to discover it), using the real comment
    ID YouTube's API returns - so when the cycle later re-scans and finds the
    same comment, save_mention()'s existing platform_id dedup check recognizes
    it and skips creating a duplicate card."""
    fresh_youtube = get_youtube_client()

    if mention.label == "Video Upload":
        result = fresh_youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": mention.platform_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": reply_text
                        }
                    }
                }
            }
        ).execute()
        reply_platform_id = result.get("snippet", {}).get("topLevelComment", {}).get("id") or result.get("id")
    else:
        result = fresh_youtube.comments().insert(
            part="snippet",
            body={
                "snippet": {
                    "parentId": mention.platform_id,
                    "textOriginal": reply_text
                }
            }
        ).execute()
        reply_platform_id = result.get("id")

    if reply_platform_id:
        save_mention(
            mention_id=str(uuid.uuid4()),
            source=models.MentionSource.YOUTUBE,
            author="Developer",
            content=reply_text,
            link=mention.link,
            pub_date=datetime.now(timezone.utc).replace(tzinfo=None),
            platform_id=reply_platform_id,
            parent_id=mention.id,
            db=db,
        )
