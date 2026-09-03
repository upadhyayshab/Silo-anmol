"""Google Play Store integration: fetching reviews and posting replies."""

import uuid
import logging
import traceback
from datetime import datetime, timezone

from google_play_scraper import Sort, reviews_all
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import models
from collector import save_mention

logger = logging.getLogger(__name__)

# google_play_scraper only returns reviews matching the requested language, so a
# single call with lang='en' silently misses everything written in another language.
# We pull each of these separately and merge the results (deduplicated by review ID)
# to actually cover the languages our audience reviews in.
PLAYSTORE_REVIEW_LANGUAGES = ['en', 'hi', 'kn', 'te', 'pa']


def fetch_playstore_reviews(package_name: str, db=None):
    if not package_name: return
    logger.info(f"\n--> [Play Store] Fetching ALL historical reviews for: {package_name}")

    try:
        reviews_by_id = {}
        for lang in PLAYSTORE_REVIEW_LANGUAGES:
            try:
                lang_reviews = reviews_all(
                    package_name,
                    sleep_milliseconds=0,
                    lang=lang,
                    country='in',
                    sort=Sort.NEWEST
                )
                for review in lang_reviews:
                    reviews_by_id[review['reviewId']] = review
            except Exception as e:
                logger.warning(f"    [Play Store] Failed fetching '{lang}' reviews for {package_name}: {e}")

        all_reviews = list(reviews_by_id.values())

        logger.info(f"    [INFO] Found {len(all_reviews)} total reviews on the public store.")

        for review in all_reviews:
            review_id = review['reviewId']
            stable_mention_id = f"play_{review_id}"

            author = review.get('userName', 'Anonymous User')
            text = review.get('content', '').strip()
            rating = review.get('score', 0)

            if not text: continue

            content = f"{'⭐' * rating} - {text}"
            link = f"https://play.google.com/store/apps/details?id={package_name}&reviewId={review_id}"

            # Use timezone-aware UTC datetime
            pub_date = review.get('at') or datetime.now(timezone.utc)

            save_mention(
                mention_id=stable_mention_id,
                source=models.MentionSource.PLAYSTORE,
                author=author,
                content=content,
                link=link,
                pub_date=pub_date,
                platform_id=review_id,
                parent_id=None,
                db=db
            )

            reply_text = review.get('replyContent')
            if reply_text:
                reply_date = review.get('repliedAt') or datetime.now(timezone.utc)
                reply_stable_id = f"play_reply_{review_id}"

                save_mention(
                    mention_id=reply_stable_id,
                    source=models.MentionSource.PLAYSTORE,
                    author="Developer",
                    content=reply_text,
                    link=link,
                    pub_date=reply_date,
                    platform_id=f"reply_{review_id}",
                    parent_id=stable_mention_id,
                    db=db
                )

    except Exception as e:
        logger.error(f"\n[CRITICAL ERROR] Play Store Scraper crashed:\n{traceback.format_exc()}")


def reply_to_review(mention, reply_text, db=None):
    """Posts a reply to a Play Store review. Raises on failure - the caller
    (main.py) is responsible for turning that into an HTTP error response.
    Google Play limits replies to 350 characters; the caller checks that
    before calling this.

    On success, immediately saves the reply as a mention too (instead of
    waiting for the next fetch cycle to discover it). Play Store replies don't
    get their own platform-assigned ID - fetch_playstore_reviews() always
    derives one deterministically as f"reply_{review_id}", so we compute the
    exact same value here to match, letting save_mention()'s dedup check
    recognize it later instead of creating a duplicate card."""
    package_name = mention.link.split("id=")[1].split("&")[0]

    credentials = Credentials.from_service_account_file(
        "playstore_key.json",
        scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    play_service = build('androidpublisher', 'v3', credentials=credentials)

    play_service.reviews().reply(
        packageName=package_name,
        reviewId=mention.platform_id,
        body={"replyText": reply_text}
    ).execute()

    save_mention(
        mention_id=str(uuid.uuid4()),
        source=models.MentionSource.PLAYSTORE,
        author="Developer",
        content=reply_text,
        link=mention.link,
        pub_date=datetime.now(timezone.utc).replace(tzinfo=None),
        platform_id=f"reply_{mention.platform_id}",
        parent_id=mention.id,
        db=db,
    )

    return package_name
