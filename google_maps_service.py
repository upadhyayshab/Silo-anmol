"""
Google Business Profile integration (store listings, ratings, reviews).

IMPORTANT - this module has NOT been tested against live Google APIs. Unlike
every other integration in this codebase, it was written without real Business
Profile API access (see the SL-ORM session that built this: access was still
pending Google's approval). Google's Business Profile API surface has been
restructured/renamed multiple times over the years - verify the exact endpoint
paths below against https://developers.google.com/my-business/reference/rest
once real credentials exist, before trusting this in production.

Two separate, currently-documented API families are used:
  - Account Management API + Business Information API (the modern, actively
    maintained REST APIs) for listing accounts/locations and creating new ones.
  - The older v4 "My Business API" (mybusiness.googleapis.com) for reviews -
    Google's newer API family does not appear to have a replacement for review
    read/reply as of this writing, so reviews still go through the legacy v4
    surface. This is the single most likely thing to have changed/moved by the
    time this actually runs - check Google's docs first if review calls fail.

Follows the same lazy, non-interactive-by-default OAuth pattern as
get_youtube_client() in main.py: never blocks app startup waiting for a
browser login, and every public function fails gracefully (logs + returns
None/[]) rather than crashing the caller when credentials aren't set up yet.

Two ways to authenticate, tried in this order by get_business_credentials():
  1. A saved login (business_token.pickle), refreshed automatically once it
     expires - created either by an interactive browser login, or the first
     time option 2 below successfully runs.
  2. A manually-generated refresh token (GOOGLE_MAPS_REFRESH_TOKEN env var),
     for headless setups where no one can run an interactive browser login
     on the machine this backend runs on - e.g. a refresh token pulled from
     Google's OAuth 2.0 Playground. Requires maps_client_secrets.json (the
     OAuth client's own downloaded credentials file, renamed) to also be
     present, since the refresh token alone isn't enough - Google also needs
     the matching client_id/client_secret to exchange it for an access token.
  3. (only when allow_interactive=True is explicitly passed) An interactive
     browser login via maps_client_secrets.json, same as get_youtube_client().
"""

import os
import json
import uuid
import pickle
import logging
from datetime import datetime, timezone

import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

import models
from collector import save_mention

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/business.manage"]
TOKEN_FILE = "business_token.pickle"
# Its own OAuth client, separate from YouTube's client_secret.json - Business
# Profile access was set up as its own Cloud Console project/client.
CLIENT_SECRET_FILE = "maps_client_secrets.json"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Optional: a refresh token generated manually (e.g. via Google's OAuth 2.0
# Playground) for headless setups where no one can run an interactive browser
# login on the machine this backend runs on. Requires CLIENT_SECRET_FILE to
# also be present, to supply the matching client_id/client_secret.
MAPS_REFRESH_TOKEN = os.getenv("GOOGLE_MAPS_REFRESH_TOKEN")

ACCOUNT_MGMT_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
REVIEWS_BASE = "https://mybusiness.googleapis.com/v4"  # legacy API - see module docstring


def _build_credentials_from_refresh_token():
    """Builds credentials directly from a manually-supplied refresh token
    (GOOGLE_MAPS_REFRESH_TOKEN) plus the client_id/client_secret in
    CLIENT_SECRET_FILE, instead of running an interactive browser login.
    For headless setups - see the module docstring. Returns None (never
    raises) if the refresh token isn't configured or the exchange fails."""
    if not MAPS_REFRESH_TOKEN:
        return None
    if not os.path.exists(CLIENT_SECRET_FILE):
        logger.warning(
            f"[Google Business] GOOGLE_MAPS_REFRESH_TOKEN is set but {CLIENT_SECRET_FILE} "
            "is missing - need it for the client_id/client_secret."
        )
        return None

    try:
        with open(CLIENT_SECRET_FILE, "r") as f:
            client_config = json.load(f)
        # Google's downloaded OAuth client JSON nests everything under
        # "installed" (Desktop app) or "web", depending on the client type.
        client_info = client_config.get("installed") or client_config.get("web") or {}
        client_id = client_info.get("client_id")
        client_secret = client_info.get("client_secret")
        if not client_id or not client_secret:
            logger.warning(f"[Google Business] {CLIENT_SECRET_FILE} is missing client_id/client_secret.")
            return None

        credentials = Credentials(
            token=None,
            refresh_token=MAPS_REFRESH_TOKEN,
            token_uri=TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        credentials.refresh(GoogleRequest())
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(credentials, token)
        return credentials
    except Exception as e:
        logger.warning(f"[Google Business] Failed to exchange GOOGLE_MAPS_REFRESH_TOKEN for an access token: {e}")
        return None


def get_business_credentials(allow_interactive: bool = False):
    """Loads saved Business Profile OAuth credentials. Returns None (never
    raises) if nothing usable is available - callers must handle that case,
    same as get_youtube_client() does elsewhere in this project."""
    credentials = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            credentials = pickle.load(token)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(GoogleRequest())
            with open(TOKEN_FILE, "wb") as token:
                pickle.dump(credentials, token)
            return credentials
        except Exception as e:
            logger.warning(f"[Google Business] Failed to refresh saved token: {e}")

    credentials = _build_credentials_from_refresh_token()
    if credentials:
        return credentials

    if allow_interactive:
        if not os.path.exists(CLIENT_SECRET_FILE):
            logger.warning(f"[Google Business] {CLIENT_SECRET_FILE} not found - cannot start interactive login.")
            return None
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
        credentials = flow.run_local_server(port=8081)  # different port than the YouTube flow (8080)
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(credentials, token)
        return credentials

    logger.warning(
        "[Google Business] No valid saved login (business_token.pickle) and no "
        "GOOGLE_MAPS_REFRESH_TOKEN configured. Either run a one-time interactive login, "
        "or set GOOGLE_MAPS_REFRESH_TOKEN + maps_client_secrets.json for headless setups."
    )
    return None


def _auth_headers():
    credentials = get_business_credentials()
    if not credentials:
        return None
    return {"Authorization": f"Bearer {credentials.token}"}


def list_accounts():
    """Returns the Business Profile accounts this login has access to."""
    headers = _auth_headers()
    if not headers:
        return []

    res = requests.get(f"{ACCOUNT_MGMT_BASE}/accounts", headers=headers)
    if res.status_code != 200:
        logger.error(f"[Google Business] Failed to list accounts: {res.text}")
        return []
    return res.json().get("accounts", [])


def list_locations(account_name: str):
    """account_name is the resource name Google returns, e.g. 'accounts/123456'."""
    headers = _auth_headers()
    if not headers:
        return []

    locations = []
    url = f"{BUSINESS_INFO_BASE}/{account_name}/locations"
    params = {
        "readMask": "name,title,storefrontAddress,phoneNumbers,categories,metadata"
    }
    while url:
        res = requests.get(url, headers=headers, params=params)
        if res.status_code != 200:
            logger.error(f"[Google Business] Failed to list locations for {account_name}: {res.text}")
            break
        data = res.json()
        locations.extend(data.get("locations", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token

    return locations


def fetch_location_reviews(account_name: str, location_name: str):
    """location_name is the resource name, e.g. 'accounts/123/locations/456'.
    Returns the raw review objects from Google (id, reviewer, starRating,
    comment, createTime, reviewReply if any)."""
    headers = _auth_headers()
    if not headers:
        return []

    reviews = []
    url = f"{REVIEWS_BASE}/{location_name}/reviews"
    params = {"pageSize": 50}
    while url:
        res = requests.get(url, headers=headers, params=params)
        if res.status_code != 200:
            logger.error(f"[Google Business] Failed to fetch reviews for {location_name}: {res.text}")
            break
        data = res.json()
        reviews.extend(data.get("reviews", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token

    return reviews


def reply_to_review(mention, reply_text, db=None) -> bool:
    """mention.platform_id holds the review's full resource name, e.g.
    'accounts/123/locations/456/reviews/789'. Returns True on success.

    On success, immediately saves the reply as a mention too (instead of
    waiting for the next fetch cycle). Google Maps replies don't get their own
    separate resource - fetch_google_business_reviews() always derives a
    deterministic platform_id as f"reply_{review_id}" (review_id being the
    last path segment of the resource name), so we compute the exact same
    value here to match, letting save_mention()'s dedup check recognize it
    later instead of creating a duplicate card."""
    review_name = mention.platform_id
    headers = _auth_headers()
    if not headers:
        return False

    url = f"{REVIEWS_BASE}/{review_name}/reply"
    res = requests.put(url, headers=headers, json={"comment": reply_text})
    if res.status_code != 200:
        logger.error(f"[Google Business] Failed to reply to review {review_name}: {res.text}")
        return False

    review_id = review_name.rsplit("/", 1)[-1]
    save_mention(
        mention_id=f"gmb_reply_{review_id}",
        source=models.MentionSource.GOOGLE_MAPS,
        author="Developer",
        content=reply_text,
        link=mention.link,
        pub_date=datetime.now(timezone.utc).replace(tzinfo=None),
        platform_id=f"reply_{review_id}",
        parent_id=mention.id,
        db=db,
    )
    return True


def create_location(account_name: str, name: str, address: str, phone: str, category: str, latitude: float = None, longitude: float = None):
    """Creates a new store listing under account_name. `address` is a single
    free-text line here for simplicity (this is what the "Add a new store
    listing" form on the dashboard sends) - Google's API actually wants a
    structured addressLines/locality/etc. object; this does a best-effort
    single-line split and should be reviewed against real store data once
    this is actually run.

    latitude/longitude are optional. If omitted, Google auto-geocodes the pin
    location from the address - which only works well if the address is clean
    and unambiguous. Since our address field above is NOT the structured
    format Google actually wants, auto-geocoding may place the pin wrong or
    fail; passing latitude/longitude explicitly guarantees the pin lands in
    the right place regardless of how well the address parses.

    New locations typically still require Google's own verification (postcard/
    phone/email) before they appear live on Maps - creating it via the API
    does not skip that step."""
    headers = _auth_headers()
    if not headers:
        return None

    body = {
        "title": name,
        "storefrontAddress": {
            "addressLines": [address],
        },
        "phoneNumbers": {"primaryPhone": phone} if phone else {},
        "categories": {"primaryCategory": {"displayName": category}} if category else {},
    }

    if latitude is not None and longitude is not None:
        body["latlng"] = {"latitude": latitude, "longitude": longitude}

    res = requests.post(f"{BUSINESS_INFO_BASE}/{account_name}/locations", headers=headers, json=body)
    if res.status_code not in (200, 201):
        logger.error(f"[Google Business] Failed to create location '{name}': {res.text}")
        return None

    logger.info(f"[Google Business] Created new location listing: {name}")
    return res.json()


STAR_RATING_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def fetch_google_business_reviews(db=None):
    account_id = os.getenv("GOOGLE_BUSINESS_ACCOUNT_ID", "").strip()
    if not account_id:
        logger.warning("--> [Google Maps] GOOGLE_BUSINESS_ACCOUNT_ID not set in .env. Skipping...")
        return

    account_name = f"accounts/{account_id}"
    logger.info(f"\n--> [Google Maps] Fetching store reviews for account {account_id}...")

    locations = list_locations(account_name)
    if not locations:
        logger.info("    [INFO] No locations found (or Business Profile login not set up yet).")
        return

    for location in locations:
        location_name = location.get("name")  # e.g. "accounts/123/locations/456"
        store_title = location.get("title", location_name)
        place_id = location.get("metadata", {}).get("placeId", "")
        link = f"https://search.google.com/local/reviews?placeid={place_id}" if place_id else ""

        reviews = fetch_location_reviews(account_name, location_name)
        logger.info(f"    [INFO] {store_title}: found {len(reviews)} reviews.")

        for review in reviews:
            review_id = review.get("reviewId")
            review_resource_name = review.get("name")  # full path, needed later to reply
            if not review_id or not review_resource_name:
                continue

            stable_id = f"gmb_{review_id}"
            rating = STAR_RATING_MAP.get(review.get("starRating", ""), 0)
            text = (review.get("comment") or "").strip()
            content = f"{'⭐' * rating} - {text}" if text else f"{'⭐' * rating} rating (no written review)"
            author = review.get("reviewer", {}).get("displayName", "Google User")

            try:
                pub_date = (
                    datetime.strptime(review.get("createTime"), "%Y-%m-%dT%H:%M:%S%z")
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                )
            except Exception:
                pub_date = datetime.now(timezone.utc).replace(tzinfo=None)

            save_mention(
                mention_id=stable_id,
                source=models.MentionSource.GOOGLE_MAPS,
                author=author,
                content=content,
                link=link,
                pub_date=pub_date,
                platform_id=review_resource_name,
                parent_id=None,
                db=db,
            )

            reply = review.get("reviewReply")
            if reply:
                reply_stable_id = f"gmb_reply_{review_id}"
                try:
                    reply_date = (
                        datetime.strptime(reply.get("updateTime"), "%Y-%m-%dT%H:%M:%S%z")
                        .astimezone(timezone.utc)
                        .replace(tzinfo=None)
                    )
                except Exception:
                    reply_date = datetime.now(timezone.utc).replace(tzinfo=None)

                save_mention(
                    mention_id=reply_stable_id,
                    source=models.MentionSource.GOOGLE_MAPS,
                    author="Developer",
                    content=reply.get("comment", ""),
                    link=link,
                    pub_date=reply_date,
                    platform_id=f"reply_{review_id}",
                    parent_id=stable_id,
                    db=db,
                )
