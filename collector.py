"""
Shared, platform-agnostic pieces used by every platform-specific service module
(youtube_service.py, playstore_service.py, news_service.py, meta_service.py,
google_maps_service.py): the AI sentiment/label classifier and the single
database-saving function every one of them calls through.
"""

import os
import re
import time
import json
import logging
from dotenv import load_dotenv

from openai import OpenAI

from database import SessionLocal
from models import Mention

# 1. Load the secret API keys from the .env file
load_dotenv()

logger = logging.getLogger(__name__)

# 2. Initialize API clients
ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.getenv("GROQ_API_KEY"),
)

# 3. The Qwen / Llama AI Engine with Auto-Retry for Rate Limits
def analyze_sentiment(content: str):
    prompt = f"""
    You are an expert multilingual customer success analyst for an agricultural, dairy, and veterinary business. Follow these steps internally before responding:

    1. Detect the primary language. If it is EXCLUSIVELY emojis, use [Emoji]. If it contains any text, use the actual language (e.g., [Kannada], [English]).
       * DOMAIN CONTEXT: Words like "De Worming" (even if typed with a space), "dewormer", "cattle", "cow", and "livestock" are standard English veterinary terms. NEVER classify these agricultural terms as Dutch or any other foreign language.

    2. Accurately translate the text to English. Pay close attention to colloquialisms (e.g., asking for a "phone", "number", or "address").

    3. Determine the true intent and sentiment:
       * **Mixed Emojis & Text**: If a comment has emojis (e.g., ❤️❤️) AND text, YOU MUST PRIORITIZE THE TEXT to determine the label. Emojis are often just decorative.
       * **Contact Inquiries**: If the user is asking for a phone number, location, or contact details, it MUST be labeled "General Inquiry" with "Neutral" sentiment, even if they use positive emojis.
       * **Emoji-Only**: If and ONLY if the comment is purely emojis, classify positive emojis (❤️, 🔥, 👍) as "Praise" ("Positive").

    4. Return ONLY a valid JSON object with exactly three keys:
       1. "sentiment": strictly "Positive", "Negative", or "Neutral".
       2. "label": Categorize strictly as: ["Bug/Performance", "Pricing", "Feature Request", "Customer Service", "Praise", "General Inquiry"]
       3. "explanation": "[Language] 'English translation' — Brief reason."

    Text: "{content}"
    """

    max_retries = 3
    for attempt in range(max_retries):
        try:
            completion = ai_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )

            raw_response = completion.choices[0].message.content.strip()

            if raw_response.startswith("```json"):
                raw_response = raw_response[7:-3].strip()
            elif raw_response.startswith("```"):
                raw_response = raw_response[3:-3].strip()

            raw_response = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw_response)

            result = json.loads(raw_response, strict=False)

            sentiment = result.get('sentiment', 'Neutral').capitalize()
            label = result.get('label', 'General Inquiry')
            explanation = result.get('explanation', 'No explanation provided.')

            if isinstance(explanation, dict):
                lang = explanation.get("detected_language", explanation.get("language", ""))
                trans = explanation.get("english_translation", explanation.get("translation", ""))
                reason = explanation.get("reason", explanation.get("summary", str(explanation)))
                explanation = f"[{lang}] Translation: '{trans}' - {reason}" if lang else str(reason)
            else:
                explanation = str(explanation)

            if sentiment not in ["Positive", "Negative", "Neutral"]:
                sentiment = "Neutral"

            return sentiment, label, explanation

        except Exception as e:
            # If rate limit (429) hit, wait 2 seconds and retry automatically
            if "429" in str(e) or "rate_limit" in str(e).lower():
                wait_time = (attempt + 1) * 2
                logger.warning(f"    [Groq Rate Limit] Waiting {wait_time}s before retrying AI analysis...")
                time.sleep(wait_time)
                continue

            logger.error(f"AI Analysis Error: {e}")
            return "Neutral", "General Inquiry", "AI Analysis failed."

    return "Neutral", "General Inquiry", "AI Analysis skipped due to rate limits."

# 4. Universal Database Saver
def save_mention(mention_id, source, author, content, link, pub_date, platform_id=None, parent_id=None, thread_id=None, db=None):
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    if platform_id:
        existing = db.query(Mention).filter(Mention.platform_id == platform_id).first()
    else:
        existing = db.query(Mention).filter(Mention.id == mention_id).first()

    if existing:
        if close_db: db.close()
        return

    # SKIP AI ONLY FOR GENUINE DEVELOPER/BUSINESS REPLIES - not just because
    # something happens to be nested (a customer replying to another customer's
    # comment, or a customer's 2nd+ message in a DM thread, is still a real
    # customer message and needs real sentiment analysis, not this shortcut).
    if author == "Developer":
        sentiment = "Neutral"
        label = "Developer Reply"
        explanation = "Outbound response by developer."
    else:
        sentiment, label, explanation = analyze_sentiment(content)

    new_mention = Mention(
        id=mention_id,
        source=source,
        author=author,
        content=content,
        sentiment=sentiment,
        label=label,
        explanation=explanation,
        link=link,
        date=pub_date,
        platform_id=platform_id,
        parent_id=parent_id,
        thread_id=thread_id,
        status="Responded" if author == "Developer" or parent_id is not None else "Unanswered"
    )

    db.add(new_mention)

    if parent_id:
            parent_msg = db.query(Mention).filter(Mention.id == parent_id).first()
            if parent_msg:
                # ONLY mark as responded if the page owner is the one replying
                if author == "Developer":
                    parent_msg.status = "Responded"
                else:
                    # If customers are replying to each other, keep it Unanswered
                    parent_msg.status = "Unanswered"

    db.commit()

    if close_db:
        db.close()
