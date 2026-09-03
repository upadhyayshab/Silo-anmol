"""
Facebook/Instagram (Meta) integration: fetching comments/DMs and posting replies.

The reply-posting side (post_*_reply()) is deliberately plain Python with no
FastAPI dependency: each function either returns a success value or raises
MetaReplyError with the HTTP status code and detail message the route should
return. main.py is responsible for translating that into an actual
HTTPException/JSON response.
"""

import os
import uuid
import logging
from datetime import datetime, timezone

import requests

import models
from collector import save_mention

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v26.0"
META_USER_TOKEN = os.getenv("META_USER_TOKEN")

# System strings generated automatically by Meta when users reply to posts/stories
META_SYSTEM_PATTERNS = [
    "ने एक पोस्ट पर जवाब दिया है",
    "ने आपकी स्टोरी का जवाब दिया",
    "replied to a post",
    "replied to your story",
    "story.php?story_fbid=",
    "You missed a call",
    "You can call",
    "call within the next",
    "started a call",
    "Missed audio call"
]


class MetaReplyError(Exception):
    """Raised when a reply/DM cannot be posted to Facebook or Instagram.

    Carries the HTTP status code and detail message the calling route should return.
    """

    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def get_meta_pages(user_token):
    """Fetch the Facebook Pages (with their own page access tokens) linked to this Meta user token."""
    accounts_url = f"{GRAPH_API_BASE}/me/accounts"
    accounts_response = requests.get(accounts_url, params={"access_token": user_token})
    return accounts_response.json().get("data", []) if accounts_response.status_code == 200 else []


def resolve_facebook_page(mention, pages, user_token):
    """Figure out which Facebook Page owns the given mention.

    First tries a direct platform_id/link match against known pages, then falls back to
    walking the post/photo/video/parent object chain via the Graph API.
    Returns (matching_page_or_None, target_page_id_or_None).
    """
    page_map = {str(p["id"]): p for p in pages}
    target_page_id = None

    search_strings = [str(mention.platform_id), str(mention.link)]
    for p_id in page_map.keys():
        if any(p_id in s for s in search_strings):
            target_page_id = p_id
            break

    if not target_page_id:
        for token in [user_token] + [p["access_token"] for p in pages]:
            try:
                res = requests.get(
                    f"{GRAPH_API_BASE}/{mention.platform_id}",
                    params={
                        "fields": "post,photo,video,parent,permalink_url",
                        "access_token": token
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    permalink = data.get("permalink_url", "")
                    for p_id in page_map.keys():
                        if p_id in permalink:
                            target_page_id = p_id
                            break

                    if target_page_id:
                        break

                    if "post" in data:
                        post_id_str = str(data["post"].get("id", ""))
                        if "_" in post_id_str:
                            pot_page = post_id_str.split("_")[0]
                            if pot_page in page_map:
                                target_page_id = pot_page
                                break

                        p_res = requests.get(f"{GRAPH_API_BASE}/{post_id_str}", params={"fields": "from", "access_token": token})
                        if p_res.status_code == 200:
                            pot_page = str(p_res.json().get("from", {}).get("id", ""))
                            if pot_page in page_map:
                                target_page_id = pot_page
                                break

                    if target_page_id:
                        break

                    parent_obj_id = None
                    if "photo" in data:
                        parent_obj_id = data["photo"].get("id")
                    elif "video" in data:
                        parent_obj_id = data["video"].get("id")
                    elif "parent" in data:
                        parent_obj_id = data["parent"].get("id")

                    if parent_obj_id:
                        p_res = requests.get(f"{GRAPH_API_BASE}/{parent_obj_id}", params={"fields": "from", "access_token": token})
                        if p_res.status_code == 200:
                            pot_page = str(p_res.json().get("from", {}).get("id", ""))
                            if pot_page in page_map:
                                target_page_id = pot_page
                                break

            except Exception:
                continue

            if target_page_id:
                break

    return page_map.get(target_page_id), target_page_id


def post_facebook_reply(mention, reply_text, user_token, db=None):
    """Posts a reply to a Facebook comment. Returns the matching page's name on
    success, and immediately saves the reply as a mention too (instead of
    waiting for the next fetch cycle), using the real comment ID Facebook's
    API returns - so when the cycle later re-scans and finds the same
    comment, save_mention()'s dedup check recognizes it and skips creating a
    duplicate card."""
    if not user_token:
        raise MetaReplyError(400, "Facebook token is missing from .env.")

    pages = get_meta_pages(user_token)
    if not pages:
        raise MetaReplyError(400, "No Facebook Pages found for this account.")

    matching_page, target_page_id = resolve_facebook_page(mention, pages, user_token)

    if not matching_page:
        available_pages = ", ".join([f"{p['name']} ({p['id']})" for p in pages])
        raise MetaReplyError(
            400,
            f"Cannot determine matching Facebook Page. Detected Page ID: {target_page_id}. Available Pages: {available_pages}"
        )

    target_ids = [mention.platform_id]
    if "_" in mention.platform_id:
        target_ids.append(mention.platform_id.split("_")[-1])

    last_error = ""
    for t_id in target_ids:
        reply_url = f"{GRAPH_API_BASE}/{t_id}/comments"
        payload_data = {
            "message": reply_text,
            "access_token": matching_page["access_token"]
        }
        response = requests.post(reply_url, data=payload_data)

        if response.status_code == 200:
            reply_platform_id = response.json().get("id")
            if reply_platform_id:
                save_mention(
                    mention_id=f"fb_{reply_platform_id}",
                    source=models.MentionSource.FACEBOOK,
                    author="Developer",
                    content=reply_text,
                    link=mention.link,
                    pub_date=datetime.now(timezone.utc).replace(tzinfo=None),
                    platform_id=reply_platform_id,
                    parent_id=mention.id,
                    db=db,
                )
            return matching_page["name"]
        else:
            last_error = response.text

    logger.error(f"Meta API Error when posting as {matching_page['name']}: {last_error}")
    raise MetaReplyError(500, "Couldn't post the reply to Facebook. Check the server logs for details.")


def post_instagram_reply(mention, reply_text, user_token, db=None):
    """Posts a reply to an Instagram comment. Immediately saves the reply as a
    mention on success too (instead of waiting for the next fetch cycle),
    using the real reply ID Instagram's API returns - so the fetch cycle's
    dedup check recognizes it later instead of creating a duplicate card."""
    if not user_token:
        raise MetaReplyError(400, "Meta token missing.")

    target_ids = [mention.platform_id]
    if "_" in mention.platform_id:
        target_ids.append(mention.platform_id.split("_")[1])

    pages = get_meta_pages(user_token)
    last_error = ""

    for page in pages:
        for t_id in target_ids:
            reply_url = f"{GRAPH_API_BASE}/{t_id}/replies"
            response = requests.post(
                reply_url,
                params={"access_token": page["access_token"]},
                json={"message": reply_text}
            )

            if response.status_code == 200:
                reply_platform_id = response.json().get("id")
                if reply_platform_id:
                    save_mention(
                        mention_id=f"ig_{reply_platform_id}",
                        source=models.MentionSource.INSTAGRAM,
                        author="Developer",
                        content=reply_text,
                        link=mention.link,
                        pub_date=datetime.now(timezone.utc).replace(tzinfo=None),
                        platform_id=reply_platform_id,
                        parent_id=mention.id,
                        db=db,
                    )
                return
            else:
                last_error = response.text

    logger.error(f"Failed to post reply to Instagram: {last_error}")
    raise MetaReplyError(500, "Couldn't post the reply to Instagram. Check the server logs for details.")


def post_meta_dm_reply(mention, reply_text, user_token):
    """Sends a Facebook or Instagram DM reply. Returns 'Instagram' or 'Facebook' on success."""
    if not user_token:
        raise MetaReplyError(400, "Meta user token missing.")

    pages = get_meta_pages(user_token)
    last_error = ""
    is_ig_dm = (mention.source.lower() == models.MentionSource.INSTAGRAM_DM.lower())

    for page in pages:
        page_token = page["access_token"]
        page_id = page["id"]

        msg_info_url = f"{GRAPH_API_BASE}/{mention.platform_id}"
        msg_res = requests.get(msg_info_url, params={"fields": "from", "access_token": page_token})

        if msg_res.status_code != 200:
            last_error = msg_res.text
            continue

        sender_id = msg_res.json().get("from", {}).get("id")
        if not sender_id:
            continue

        send_url = ""
        if is_ig_dm:
            ig_setup_url = f"{GRAPH_API_BASE}/{page_id}?fields=instagram_business_account"
            ig_res = requests.get(ig_setup_url, params={"access_token": page_token})
            ig_account_id = ig_res.json().get("instagram_business_account", {}).get("id")

            if not ig_account_id:
                continue

            send_url = f"{GRAPH_API_BASE}/{ig_account_id}/messages"
        else:
            send_url = f"{GRAPH_API_BASE}/me/messages"

        send_payload = {
            "recipient": {"id": sender_id},
            "message": {"text": reply_text}
        }

        response = requests.post(send_url, params={"access_token": page_token}, json=send_payload)

        if response.status_code == 200:
            return "Instagram" if is_ig_dm else "Facebook"
        else:
            last_error = response.text

    logger.error(f"Meta Send API Error: {last_error}")
    raise MetaReplyError(500, "Couldn't send the direct message. Check the server logs for details.")


def fetch_facebook_comments(db=None):
    if not META_USER_TOKEN:
        logger.warning("--> [Facebook] META_USER_TOKEN not found in .env. Skipping...")
        return

    logger.info("\n--> [Facebook] Fetching recent posts & nested comments (Max 15 posts/page)...")

    accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
    accounts_response = requests.get(accounts_url, params={"access_token": META_USER_TOKEN})

    if accounts_response.status_code != 200:
        logger.error(f"    [Facebook API Error]: Failed to fetch accounts. {accounts_response.text}")
        return

    pages = accounts_response.json().get("data", [])

    for page in pages:
        page_name = page["name"]
        page_id = page["id"]
        page_token = page["access_token"]

        logger.info(f"\n    [Facebook] Scanning recent posts for {page_name}...")

        posts_url = f"https://graph.facebook.com/v26.0/{page_id}/posts"
        posts_params = {"access_token": page_token, "limit": 15}

        total_posts_scanned = 0
        total_comments_processed = 0

        while posts_url and total_posts_scanned < 15:
            posts_response = requests.get(posts_url, params=posts_params)

            if posts_response.status_code != 200:
                logger.error(f"    [Facebook API Error for {page_name}]: {posts_response.text}")
                break

            posts_data = posts_response.json()
            posts = posts_data.get("data", [])

            if not posts:
                break

            logger.info(f"      -> Scanning batch of {len(posts)} posts (Total so far: {total_posts_scanned + len(posts)})")

            for post in posts:
                if total_posts_scanned >= 15:
                    break

                post_id = post["id"]

                comments_url = f"https://graph.facebook.com/v26.0/{post_id}/comments"
                # FIX: Set nested comments limit to 100 to pull hidden developer replies
                comments_params = {
                    "access_token": page_token,
                    "fields": "id,message,from,created_time,attachment{media,target,type},comments.limit(100){id,message,from,created_time,attachment{media,target,type}}",
                    "limit": 200,
                }

                while comments_url:
                    comments_response = requests.get(comments_url, params=comments_params)

                    if comments_response.status_code != 200:
                        break

                    comments_data = comments_response.json()
                    comments = comments_data.get("data", [])

                    if not comments:
                        break

                    for comment in comments:
                        message_text = (comment.get("message") or "").strip()
                        has_attachment = bool(comment.get("attachment"))
                        if not message_text and not has_attachment:
                            continue

                        if message_text and has_attachment:
                            content_text = f"{message_text} [Photo Attached]"
                        elif has_attachment:
                            content_text = "[Media/Attachment Shared]"
                        else:
                            content_text = message_text

                        raw_time = comment["created_time"]
                        date_obj = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)

                        comment_id = comment["id"]
                        stable_mention_id = f"fb_{comment_id}"
                        fb_link = f"https://www.facebook.com/{post_id}?comment_id={comment_id}"

                        # FIX: Handle "from": null securely to prevent AttributeError crashes
                        from_data = comment.get("from") or {}
                        commenter_id = from_data.get("id", "")
                        commenter_name = from_data.get("name", "Facebook User")
                        author_name = "Developer" if commenter_id == page_id else commenter_name

                        save_mention(
                            mention_id=stable_mention_id,
                            source=models.MentionSource.FACEBOOK,
                            author=author_name,
                            content=content_text,
                            link=fb_link,
                            pub_date=date_obj,
                            platform_id=comment_id,
                            parent_id=None,
                            db=db
                        )
                        total_comments_processed += 1

                        nested_comments = (comment.get("comments") or {}).get("data", [])
                        for reply in nested_comments:
                            r_message_text = (reply.get("message") or "").strip()
                            r_has_attachment = bool(reply.get("attachment"))
                            if not r_message_text and not r_has_attachment:
                                continue

                            if r_message_text and r_has_attachment:
                                r_content_text = f"{r_message_text} [Photo Attached]"
                            elif r_has_attachment:
                                r_content_text = "[Media/Attachment Shared]"
                            else:
                                r_content_text = r_message_text

                            r_raw_time = reply["created_time"]
                            r_date_obj = datetime.strptime(r_raw_time, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)

                            reply_id = reply["id"]
                            r_stable_mention_id = f"fb_{reply_id}"

                            r_from_data = reply.get("from") or {}
                            r_commenter_id = r_from_data.get("id", "")
                            r_commenter_name = r_from_data.get("name", "Facebook User")
                            r_author_name = "Developer" if r_commenter_id == page_id else r_commenter_name

                            save_mention(
                                mention_id=r_stable_mention_id,
                                source=models.MentionSource.FACEBOOK,
                                author=r_author_name,
                                content=r_content_text,
                                link=fb_link,
                                pub_date=r_date_obj,
                                platform_id=reply_id,
                                parent_id=stable_mention_id,
                                db=db
                            )
                            total_comments_processed += 1

                    # FIX: Clear params when using the paging 'next' URL to prevent parameter corruption
                    comments_url = comments_data.get("paging", {}).get("next")
                    comments_params = {}

                total_posts_scanned += 1

            if total_posts_scanned >= 15:
                logger.info(f"      [INFO] Cap of 15 recent posts reached for {page_name}.")
                break

            posts_url = posts_data.get("paging", {}).get("next")
            posts_params = {}

        logger.info(f"    [INFO] Finished {page_name}. Scanned {total_posts_scanned} posts and checked {total_comments_processed} comments.")


def fetch_instagram_comments(db=None):
    if not META_USER_TOKEN:
        logger.warning("--> [Instagram] META_USER_TOKEN not found. Skipping...")
        return

    logger.info("\n--> [Instagram] Fetching recent posts & comments (Max 15 posts/page)...")

    accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
    accounts_response = requests.get(accounts_url, params={"access_token": META_USER_TOKEN})

    if accounts_response.status_code != 200:
        logger.error(f"    [Instagram API Error]: Failed to fetch accounts. {accounts_response.text}")
        return

    pages = accounts_response.json().get("data", [])

    for page in pages:
        page_token = page["access_token"]
        page_id = page["id"]

        ig_setup_url = f"https://graph.facebook.com/v26.0/{page_id}?fields=instagram_business_account"
        ig_setup_res = requests.get(ig_setup_url, params={"access_token": page_token})

        if ig_setup_res.status_code != 200:
            continue

        # FIX: Handle pages with no Instagram account safely
        ig_account_data = ig_setup_res.json().get("instagram_business_account") or {}
        ig_account_id = ig_account_data.get("id")

        if not ig_account_id:
            continue

        # FIX: We query the actual username in a dedicated request to ensure Developer matching works
        ig_profile_res = requests.get(f"https://graph.facebook.com/v26.0/{ig_account_id}?fields=username", params={"access_token": page_token})
        ig_username = ig_profile_res.json().get("username", "")

        logger.info(f"\n    [Instagram] Scanning recent media for IG Account: @{ig_username or ig_account_id}...")

        media_url = f"https://graph.facebook.com/v26.0/{ig_account_id}/media"
        media_params = {"access_token": page_token, "fields": "id,shortcode", "limit": 15}

        media_response = requests.get(media_url, params=media_params)
        if media_response.status_code != 200:
            logger.error(f"    [Instagram API Error for {ig_username}]: {media_response.text}")
            continue

        media_items = media_response.json().get("data", [])[:15]
        total_comments_processed = 0

        for media in media_items:
            media_id = media["id"]
            shortcode = media.get("shortcode", "")
            ig_post_link = f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""

            post_comments_count = 0

            comments_url = f"https://graph.facebook.com/v26.0/{media_id}/comments"
            comments_params = {
                "access_token": page_token,
                "fields": "id,text,username,timestamp,replies.limit(100){id,text,username,timestamp}",
                "limit": 200
            }

            comments_res = requests.get(comments_url, params=comments_params)
            if comments_res.status_code != 200:
                continue

            comments_data = comments_res.json().get("data", [])

            for comment in comments_data:
                if post_comments_count >= 50:
                    break

                c_text = comment.get("text", "")
                if not c_text or not c_text.strip():
                    continue

                c_id = comment["id"]
                c_time = datetime.strptime(comment["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)
                c_username = comment.get("username", "IG User")

                author = "Developer" if (ig_username and c_username.lower() == ig_username.lower()) else c_username

                save_mention(f"ig_{c_id}", models.MentionSource.INSTAGRAM, author, c_text, ig_post_link, c_time, platform_id=c_id, parent_id=None, db=db)
                total_comments_processed += 1
                post_comments_count += 1

                # FIX: Handle missing replies gracefully
                replies_data = (comment.get("replies") or {}).get("data", [])
                for reply in replies_data:
                    if post_comments_count >= 50:
                        break

                    r_text = reply.get("text", "")
                    if not r_text or not r_text.strip():
                        continue

                    r_id = reply["id"]
                    r_time = datetime.strptime(reply["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)
                    r_username = reply.get("username", "IG User")

                    r_author = "Developer" if (ig_username and r_username.lower() == ig_username.lower()) else r_username

                    save_mention(f"ig_{r_id}", models.MentionSource.INSTAGRAM, r_author, r_text, ig_post_link, r_time, platform_id=r_id, parent_id=f"ig_{c_id}", db=db)
                    total_comments_processed += 1
                    post_comments_count += 1

        logger.info(f"    [INFO] Finished @{ig_username or ig_account_id}. Checked {total_comments_processed} comments across {len(media_items)} posts.")


def fetch_facebook_dms(db=None):
    if not META_USER_TOKEN:
        logger.warning("--> [Facebook DMs] SKIPPED: META_USER_TOKEN is missing from .env")
        return

    logger.info("\n--> [Facebook DMs] Fetching Messenger conversations...")

    accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
    accounts_res = requests.get(accounts_url, params={"access_token": META_USER_TOKEN})
    if accounts_res.status_code != 200:
        logger.error(f"--> [Facebook DMs] ERROR fetching pages: {accounts_res.text}")
        return

    pages = accounts_res.json().get("data", [])
    if not pages:
        logger.warning("--> [Facebook DMs] No Facebook Pages found under this user token.")
        return

    for page in pages:
        page_id = page["id"]
        page_token = page["access_token"]
        page_name = page.get("name", page_id)

        logger.info(f"  > Checking Page: {page_name} (ID: {page_id})...")

        conv_url = f"https://graph.facebook.com/v26.0/{page_id}/conversations"
        conv_params = {
            "access_token": page_token,
            "limit": 100,
            "fields": "id,updated_time,participants{id,name},messages.limit(20){id,message,from,created_time}"
        }

        conv_res = requests.get(conv_url, params=conv_params)

        if conv_res.status_code != 200:
            err_msg = conv_res.json().get("error", {}).get("message", conv_res.text)
            logger.warning(f"    ⚠️ Meta API Error fetching Facebook conversations for {page_name}: {err_msg}")
            continue

        conversations = conv_res.json().get("data", [])
        logger.info(f"    📩 Total Active Facebook DM Threads Checked: {len(conversations)}")

        for conv in conversations:
            conv_id = conv.get("id")
            messages = conv.get("messages", {}).get("data", [])

            # Track the very first message to act as the "Root Card" for the UI
            thread_root_id = None

            # Process messages chronologically (oldest to newest)
            for msg in reversed(messages):
                raw_time = msg.get("created_time")
                if not raw_time:
                    continue

                try:
                    msg_date_tz = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S%z")
                except Exception:
                    msg_date_tz = datetime.now(timezone.utc)

                date_obj = msg_date_tz.replace(tzinfo=None)
                msg_text = msg.get("message", "").strip() or "[Media/Attachment Shared]"

                # Filter out Meta system notifications
                if any(pattern in msg_text for pattern in META_SYSTEM_PATTERNS):
                    continue

                msg_id = msg["id"]
                sender_id = msg.get("from", {}).get("id", "")
                sender_name = msg.get("from", {}).get("name", "FB User")

                author = "Developer" if sender_id == page_id else sender_name
                stable_id = f"fb_dm_{msg_id}"

                # --- THE NEW PARENTING LOGIC ---
                if thread_root_id is None:
                    # The first message becomes the main card on the dashboard
                    thread_root_id = stable_id
                    assign_parent = None
                else:
                    # Every other message nests inside that main card
                    assign_parent = thread_root_id

                business_id = os.getenv("META_BUSINESS_ID", "")
                direct_inbox_link = (
                    "https://business.facebook.com/latest/inbox/messenger"
                    f"?asset_id={page_id}"
                    f"&business_id={business_id}"
                )

                save_mention(
                    mention_id=stable_id,
                    source=models.MentionSource.FACEBOOK_DM,
                    author=author,
                    content=msg_text,
                    link=direct_inbox_link,
                    pub_date=date_obj,
                    platform_id=msg_id,
                    parent_id=assign_parent,
                    thread_id=conv_id,
                    db=db
                )


def fetch_instagram_dms(db=None):
    if not META_USER_TOKEN:
        logger.warning("--> [Instagram DMs] SKIPPED: META_USER_TOKEN is missing from .env")
        return

    logger.info("\n--> [Instagram DMs] Fetching IG Direct conversations...")
    accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
    accounts_res = requests.get(accounts_url, params={"access_token": META_USER_TOKEN})

    if accounts_res.status_code != 200:
        logger.error(f"--> [Instagram DMs] ERROR fetching pages: {accounts_res.text}")
        return

    pages = accounts_res.json().get("data", [])
    if not pages:
        logger.warning("--> [Instagram DMs] No Facebook Pages found under this user token.")
        return

    for page in pages:
        page_token = page["access_token"]
        page_id = page["id"]
        page_name = page.get("name", page_id)

        # Check connected IG Account
        ig_setup_url = (
            f"https://graph.facebook.com/v26.0/{page_id}"
            "?fields=instagram_business_account{id,username}"
        )
        ig_res = requests.get(ig_setup_url, params={"access_token": page_token})
        ig_data = ig_res.json().get("instagram_business_account", {})
        ig_account_id = ig_data.get("id")
        ig_username = ig_data.get("username", "")

        if not ig_account_id:
            logger.warning(f"    ❌ No Instagram Business/Creator Account linked to Facebook Page '{page_name}'")
            continue

        logger.info(f"  > Checking Page: @{ig_username} (ID: {ig_account_id})...")

        # 1. Fetch conversation IDs
        conv_url = f"https://graph.facebook.com/v26.0/{page_id}/conversations"
        conv_params = {
            "access_token": page_token,
            "platform": "instagram",
            "limit": 5,
            "fields": "id"
        }

        conv_res = requests.get(conv_url, params=conv_params)

        if conv_res.status_code != 200:
            logger.warning(f"    ⚠️ Meta API Error fetching conversations for @{ig_username}: {conv_res.text}")
            continue

        conversations = conv_res.json().get("data", [])
        logger.info(f"    📩 Total Active IG DM Threads Found: {len(conversations)}")

        # 2. Iterate through each conversation thread and fetch its messages
        for conv in conversations:
            conv_id = conv["id"]
            direct_inbox_link = f"https://business.facebook.com/latest/inbox/all?asset_id={page_id}&thread_id={conv_id}"

            msg_url = f"https://graph.facebook.com/v26.0/{conv_id}/messages"
            msg_params = {
                "access_token": page_token,
                "limit": 10,
                "fields": "id,message,from,created_time"
            }

            msg_res = requests.get(msg_url, params=msg_params)
            if msg_res.status_code != 200:
                logger.warning(f"    ⚠️ Error fetching messages for thread {conv_id}: {msg_res.text}")
                continue

            messages = msg_res.json().get("data", [])
            last_user_msg_id = None

            for msg in reversed(messages):
                msg_text = msg.get("message", "").strip()

                # Catch photos, reels, audio notes, and attachments
                if not msg_text:
                    msg_text = "[Media/Attachment Shared]"

                # Filter out Meta system notifications
                if any(pattern in msg_text for pattern in META_SYSTEM_PATTERNS):
                    logger.info(f"    [Skipped System Notification]: {msg_text[:40]}...")
                    continue

                msg_id = msg["id"]
                sender_username = msg.get("from", {}).get("username", "IG User")
                raw_time = msg["created_time"]

                try:
                    date_obj = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)
                except Exception:
                    date_obj = datetime.now(timezone.utc).replace(tzinfo=None)

                author = "Developer" if (ig_username and sender_username.lower() == ig_username.lower()) else sender_username
                stable_id = f"ig_dm_{msg_id}"

                if author != "Developer":
                    last_user_msg_id = stable_id

                save_mention(
                    mention_id=stable_id,
                    source=models.MentionSource.INSTAGRAM_DM,
                    author=author,
                    content=msg_text,
                    link=direct_inbox_link,
                    pub_date=date_obj,
                    platform_id=msg_id,
                    parent_id=None if author != "Developer" else last_user_msg_id,
                    thread_id=conv_id,
                    db=db
                )
