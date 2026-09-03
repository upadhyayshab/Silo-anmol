"""Google News integration: fetching articles. No reply capability - news
articles can't be replied to, so there's only a fetch function here."""

import hashlib
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime

import requests

import models
from collector import save_mention

logger = logging.getLogger(__name__)


def fetch_google_news(keywords_input: str, db=None):
    """
    Fetches news articles directly from Google News RSS strictly matching
    the exact phrases. Supports single keywords or comma-separated lists.
    """
    if not keywords_input or not keywords_input.strip():
        return

    # Split by comma to process each exact brand/product term individually
    keyword_list = [k.strip() for k in keywords_input.split(",") if k.strip()]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for clean_keyword in keyword_list:
        logger.info(f"\n--> [Google News] Searching articles for: '{clean_keyword}'...")

        # 1. Wrap in double quotes for Google's exact-match engine
        exact_query = f'"{clean_keyword}"'
        encoded_query = urllib.parse.quote(exact_query)

        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"

        try:
            response = requests.get(rss_url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"--> [Google News] Error: Received status code {response.status_code}")
                continue

            root = ET.fromstring(response.content)
            articles = root.findall(".//item")
            saved_count = 0

            for item in articles:
                title = item.findtext("title", "").strip()
                description = item.findtext("description", "").strip()
                article_url = item.findtext("link", "").strip()
                pub_date_str = item.findtext("pubDate", "")

                source_elem = item.find("source")
                author = source_elem.text.strip() if (source_elem is not None and source_elem.text) else "Google News"

                if not title or not article_url:
                    continue

                # 2. Strict Filter: Discard if the exact phrase is not in the title or description
                target_phrase = clean_keyword.lower()
                combined_text = f"{title} {description}".lower()

                if target_phrase not in combined_text:
                    continue

                # Parse publication date
                try:
                    pub_date = parsedate_to_datetime(pub_date_str).replace(tzinfo=None)
                except Exception:
                    pub_date = datetime.utcnow()

                # Generate stable ID from URL hash
                url_hash = hashlib.md5(article_url.encode("utf-8")).hexdigest()
                stable_mention_id = f"news_g_{url_hash}"

                save_mention(
                    mention_id=stable_mention_id,
                    source=models.MentionSource.NEWS,
                    author=author,
                    content=title,
                    link=article_url,
                    pub_date=pub_date,
                    db=db
                )
                saved_count += 1

            logger.info(f"    ✅ Successfully saved {saved_count} verified article(s) for '{clean_keyword}'")

        except Exception as e:
            logger.error(f"--> [Google News] Error fetching news for '{clean_keyword}': {e}")
