import os
import json
import time
import logging
import requests
import urllib.parse
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from collections import defaultdict
from dotenv import load_dotenv

from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, Request, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

import models
import meta_service
import auth
import google_maps_service
import youtube_service
import playstore_service
import news_service
from database import engine, get_db
from collector import analyze_sentiment

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

META_USER_TOKEN = os.getenv("META_USER_TOKEN")

models.Base.metadata.create_all(bind=engine)

# ==========================================
# BACKGROUND SCHEDULER
# ==========================================
def run_automation_cycle():
    db = Session(bind=engine)
    
    try:
        logger.info("--- Background Automation Cycle Started ---")

        raw_channel_ids = os.getenv("TARGET_CHANNEL_IDS", "")
        target_channel_ids = [cid.strip() for cid in raw_channel_ids.split(",") if cid.strip()]
        
        for channel_id in target_channel_ids:
            youtube_service.fetch_channel_comments(channel_id, db)

        keywords_string = os.getenv("TARGET_KEYWORDS", "")
        target_keywords = [kw.strip() for kw in keywords_string.split(",") if kw.strip()]

        for kw in target_keywords:
            youtube_service.fetch_and_store_youtube_data(kw, db)
            news_service.fetch_google_news(kw, db)

        raw_packages = os.getenv("PLAYSTORE_PACKAGE_NAMES", "")
        package_names = [pkg.strip() for pkg in raw_packages.split(",") if pkg.strip()]

        for pkg in package_names:
            playstore_service.fetch_playstore_reviews(pkg, db)

        meta_service.fetch_facebook_comments(db)
        meta_service.fetch_instagram_comments(db)
        meta_service.fetch_facebook_dms(db)
        meta_service.fetch_instagram_dms(db)
        google_maps_service.fetch_google_business_reviews(db)

        logger.info("--- Background Automation Cycle Complete ---")

    except Exception as e:
        logger.error(f"Error during automation cycle: {e}")

    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_automation_cycle, 'interval', minutes=30, id='listening_job')
    scheduler.start()
    run_automation_cycle()
    yield  
    scheduler.shutdown()

# ==========================================
# FASTAPI APP SETUP
# ==========================================
app = FastAPI(title="Social Listening API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# LOGIN
# ==========================================

class LoginPayload(BaseModel):
    password: str

@app.post("/api/login")
def login(payload: LoginPayload):
    if not auth.verify_password(payload.password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"token": auth.create_token()}

# ==========================================
# DASHBOARD API ROUTES
# ==========================================

@app.get("/api/feed", dependencies=[Depends(auth.require_auth)])
def get_feed(db: Session = Depends(get_db)):
    """Returns only the most recent message for each unique thread_id."""
    subquery = (
        db.query(models.Mention.thread_id, func.max(models.Mention.date).label("max_date"))
        .filter(models.Mention.thread_id.isnot(None))
        .group_by(models.Mention.thread_id)
        .subquery()
    )
    
    latest_threads = (
        db.query(models.Mention)
        .join(subquery, (models.Mention.thread_id == subquery.c.thread_id) & (models.Mention.date == subquery.c.max_date))
        .order_by(models.Mention.date.desc())
        .all()
    )
    
    standalone_mentions = db.query(models.Mention).filter(models.Mention.thread_id.is_(None)).all()
    
    combined_feed = latest_threads + standalone_mentions
    combined_feed.sort(key=lambda x: x.date, reverse=True)
    
    return combined_feed

@app.get("/api/threads/{thread_id}/messages", dependencies=[Depends(auth.require_auth)])
def get_thread_messages(thread_id: str, db: Session = Depends(get_db)):
    """Returns the complete chat history for a specific conversation."""
    messages = (
        db.query(models.Mention)
        .filter(models.Mention.thread_id == thread_id)
        .order_by(models.Mention.date.asc())
        .all()
    )
    return messages

@app.get("/api/mentions", dependencies=[Depends(auth.require_auth)])
def read_all_mentions(db: Session = Depends(get_db)):
    return db.query(models.Mention).order_by(models.Mention.date.desc()).all()

class MentionStatusUpdate(BaseModel):
    status: str

@app.patch("/api/mentions/{mention_id}/status", dependencies=[Depends(auth.require_auth)])
def update_mention_status(mention_id: str, payload: MentionStatusUpdate, db: Session = Depends(get_db)):
    db_mention = db.query(models.Mention).filter(models.Mention.id == mention_id).first()
    if not db_mention: raise HTTPException(status_code=404, detail="Mention not found")
    db_mention.status = payload.status
    db.commit()
    db.refresh(db_mention)
    return db_mention

@app.get("/api/analytics/sentiment-trend", dependencies=[Depends(auth.require_auth)])
def get_sentiment_trend(time_range: str = "7d", db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if time_range == "All": start_time = datetime.min
    elif time_range == "1h": start_time = now - timedelta(hours=1)
    elif time_range == "6h": start_time = now - timedelta(hours=6)
    elif time_range == "12h": start_time = now - timedelta(hours=12)
    elif time_range == "1d": start_time = now - timedelta(days=1)
    elif time_range == "2d": start_time = now - timedelta(days=2)
    elif time_range == "4d": start_time = now - timedelta(days=4)
    elif time_range == "14d": start_time = now - timedelta(days=14)
    elif time_range == "30d": start_time = now - timedelta(days=30)
    else: start_time = now - timedelta(days=7) 

    mentions = db.query(models.Mention).filter(models.Mention.date >= start_time).all()
    is_hourly = time_range in ["1h", "6h", "12h"]
    trend_data = defaultdict(lambda: {"Positive": 0, "Negative": 0, "Neutral": 0})
    
    for m in mentions:
        label = m.date.strftime("%H:%M" if time_range == "1h" else "%b %d, %H:00") if is_hourly else m.date.strftime("%Y-%m-%d")
        if m.sentiment and m.sentiment in trend_data[label]:
            trend_data[label][m.sentiment] += 1

    result = [{"date": label, "Positive": counts["Positive"], "Negative": counts["Negative"], "Neutral": counts["Neutral"]} for label, counts in trend_data.items()]
    result.sort(key=lambda x: x["date"])
    return result

@app.get("/api/mentions/recent", dependencies=[Depends(auth.require_auth)])
def get_recent_mentions(
    time_range: str = "All", 
    sentiment: str = "All",
    status: str = "All",
    source: str = "All", 
    limit: int = 100, 
    db: Session = Depends(get_db)
):
    query = db.query(models.Mention).filter(
        models.Mention.parent_id == None,
        models.Mention.author != "Developer"
    )
    
    # 1. SOURCE FILTER
    if source != "All":
        s = source.lower()
        matched_source = next((src for src in models.MentionSource.ALL if src.lower() == s), None)
        query = query.filter(models.Mention.source.ilike(matched_source or source))

    # 2. TIME RANGE FILTER
    if time_range != "All":
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if time_range == "1h": start_time = now - timedelta(hours=1)
        elif time_range == "6h": start_time = now - timedelta(hours=6)
        elif time_range == "12h": start_time = now - timedelta(hours=12)
        elif time_range in ["1d", "24h"]: start_time = now - timedelta(days=1)
        elif time_range == "2d": start_time = now - timedelta(days=2)
        elif time_range == "4d": start_time = now - timedelta(days=4)
        elif time_range == "7d": start_time = now - timedelta(days=7)
        elif time_range == "14d": start_time = now - timedelta(days=14)
        elif time_range == "30d": start_time = now - timedelta(days=30)
        else: start_time = now - timedelta(days=7) 
        
        query = query.filter(models.Mention.date >= start_time)

    # 3. SENTIMENT FILTER
    if sentiment != "All":
        query = query.filter(models.Mention.sentiment.ilike(sentiment))
        
    # 4. STATUS FILTER (Fixed to ensure only Developer replies count as Responded)
    if status != "All":
        replied_parents_subquery = db.query(models.Mention.parent_id).filter(
            models.Mention.parent_id.isnot(None),
            models.Mention.parent_id != "",
            models.Mention.author == "Developer"
        ).subquery()
        
        if status.lower() == "responded":
            query = query.filter(
                models.Mention.status.ilike("Responded") | 
                models.Mention.id.in_(replied_parents_subquery)
            )
        elif status.lower() in ["unanswered", "new"]:
            query = query.filter(
                models.Mention.status.ilike("Unanswered") & 
                (~models.Mention.id.in_(replied_parents_subquery)) &
                (~models.Mention.status.ilike("Responded"))
            )

    parents = query.order_by(models.Mention.date.desc()).limit(limit).all()
    
    parent_ids = [p.id for p in parents]
    if parent_ids:
        replies = db.query(models.Mention).filter(models.Mention.parent_id.in_(parent_ids)).all()
    else:
        replies = []
        
    return parents + replies

@app.get("/api/analytics/kpis", dependencies=[Depends(auth.require_auth)])
def get_kpis(time_range: str = "7d", db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    time_deltas = {
        "All": timedelta(days=365), "1h": timedelta(hours=1), "6h": timedelta(hours=6), 
        "12h": timedelta(hours=12), "1d": timedelta(days=1), "2d": timedelta(days=2), 
        "4d": timedelta(days=4), "7d": timedelta(days=7), "14d": timedelta(days=14), 
        "30d": timedelta(days=30)
    }
    delta = time_deltas.get(time_range, timedelta(days=7))
    start_time = now - delta
    past_start_time = start_time - delta

    current_mentions = db.query(models.Mention).filter(models.Mention.date >= start_time).all()
    past_mentions = db.query(models.Mention).filter(models.Mention.date >= past_start_time, models.Mention.date < start_time).all()

    total_mentions = len(current_mentions)
    past_total = len(past_mentions)
    positive_count = sum(1 for m in current_mentions if m.sentiment == "Positive")
    critical_count = sum(1 for m in current_mentions if m.sentiment == "Negative")
    
    net_sentiment = round((positive_count / total_mentions) * 100) if total_mentions > 0 else 0
    trend_pct = round(((total_mentions - past_total) / past_total) * 100, 1) if past_total > 0 else 0

    return {
        "total_mentions": total_mentions, 
        "net_sentiment": net_sentiment, 
        "critical_alerts": critical_count, 
        "mention_trend_pct": trend_pct, 
        "time_label": time_range
    }

# ==========================================
# MANUAL FETCH / SYNC ENDPOINTS
# ==========================================
# These all run in the background: the endpoint schedules the job and responds
# immediately instead of making the caller wait however long the actual scrape
# takes. Each background job opens its own DB session (the one FastAPI injects
# via Depends(get_db) gets closed the moment the response is sent, so it can't
# be reused once the request has already returned).

def _run_youtube_fetch_job(keyword: str):
    db = Session(bind=engine)
    try:
        youtube_service.fetch_and_store_youtube_data(keyword, db)
    except Exception as e:
        logger.error(f"Background YouTube fetch for '{keyword}' failed: {e}")
    finally:
        db.close()

def _run_facebook_fetch_job():
    db = Session(bind=engine)
    try:
        meta_service.fetch_facebook_comments(db)
    except Exception as e:
        logger.error(f"Background Facebook fetch failed: {e}")
    finally:
        db.close()

def _run_instagram_fetch_job():
    db = Session(bind=engine)
    try:
        meta_service.fetch_instagram_comments(db)
    except Exception as e:
        logger.error(f"Background Instagram fetch failed: {e}")
    finally:
        db.close()

def _run_playstore_fetch_job():
    db = Session(bind=engine)
    try:
        raw_packages = os.getenv("PLAYSTORE_PACKAGE_NAMES", "")
        package_names = [pkg.strip() for pkg in raw_packages.split(",") if pkg.strip()]
        for pkg in package_names:
            playstore_service.fetch_playstore_reviews(pkg, db)
    except Exception as e:
        logger.error(f"Background Play Store fetch failed: {e}")
    finally:
        db.close()

def _run_news_fetch_job():
    db = Session(bind=engine)
    try:
        keywords_string = os.getenv("TARGET_KEYWORDS", "")
        target_keywords = [kw.strip() for kw in keywords_string.split(",") if kw.strip()]
        for kw in target_keywords:
            news_service.fetch_google_news(kw, db)
    except Exception as e:
        logger.error(f"Background News fetch failed: {e}")
    finally:
        db.close()

def _run_google_business_fetch_job():
    db = Session(bind=engine)
    try:
        google_maps_service.fetch_google_business_reviews(db)
    except Exception as e:
        logger.error(f"Background Google Maps fetch failed: {e}")
    finally:
        db.close()

@app.post("/api/fetch/youtube", dependencies=[Depends(auth.require_auth)])
def trigger_youtube_fetch(keyword: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_youtube_fetch_job, keyword)
    return {"status": "started", "message": f"YouTube fetch for '{keyword}' started in the background."}

@app.post("/api/fetch/facebook", dependencies=[Depends(auth.require_auth)])
def trigger_facebook_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_facebook_fetch_job)
    return {"status": "started", "message": "Facebook fetch started in the background."}

@app.post("/api/fetch/instagram", dependencies=[Depends(auth.require_auth)])
def trigger_instagram_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_instagram_fetch_job)
    return {"status": "started", "message": "Instagram fetch started in the background."}

@app.post("/api/fetch/playstore", dependencies=[Depends(auth.require_auth)])
def trigger_playstore_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_playstore_fetch_job)
    return {"status": "started", "message": "Play Store fetch started in the background."}

@app.post("/api/fetch/news", dependencies=[Depends(auth.require_auth)])
def trigger_news_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_news_fetch_job)
    return {"status": "started", "message": "News fetch started in the background."}

@app.post("/api/fetch/all", dependencies=[Depends(auth.require_auth)])
def trigger_all_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_automation_cycle)
    return {"status": "started", "message": "Full synchronization started in the background."}

@app.post("/api/fetch/google-maps", dependencies=[Depends(auth.require_auth)])
def trigger_google_business_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_google_business_fetch_job)
    return {"status": "started", "message": "Google Maps review fetch started in the background."}

class CreateStorePayload(BaseModel):
    name: str
    address: str
    phone: str = ""
    category: str = ""
    latitude: float | None = None
    longitude: float | None = None

@app.post("/api/google-maps/stores", dependencies=[Depends(auth.require_auth)])
def create_store_listing(payload: CreateStorePayload):
    """Creates a single new Google Maps store listing from the dashboard's
    "Add a new store listing" form. Runs synchronously (unlike the fetch
    endpoints) since creating one listing is a quick single API call, and the
    dashboard needs an immediate success/failure result to show the user."""
    account_id = os.getenv("GOOGLE_BUSINESS_ACCOUNT_ID", "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="GOOGLE_BUSINESS_ACCOUNT_ID is not set in .env.")

    name = payload.name.strip()
    address = payload.address.strip()
    if not name or not address:
        raise HTTPException(status_code=400, detail="Store name and address are required.")

    if (payload.latitude is None) != (payload.longitude is None):
        raise HTTPException(status_code=400, detail="Provide both latitude and longitude, or neither.")

    account_name = f"accounts/{account_id}"
    result = google_maps_service.create_location(
        account_name, name, address, payload.phone.strip(), payload.category.strip(),
        latitude=payload.latitude, longitude=payload.longitude
    )

    if result is None:
        raise HTTPException(status_code=500, detail="Couldn't create the store listing. Check the server logs for details.")

    return {
        "status": "success",
        "message": f"Store listing '{name}' created. It may still need Google's own verification (postcard/phone/email) before it appears live on Maps."
    }

# ==========================================
# THE OMNICHANNEL REPLY ENDPOINT
# ==========================================
class ReplyPayload(BaseModel):
    reply_text: str

@app.post("/api/mentions/{mention_id:path}/reply", dependencies=[Depends(auth.require_auth)])
def reply_to_mention(mention_id: str, payload: ReplyPayload, db: Session = Depends(get_db)): 
    mention = db.query(models.Mention).filter(models.Mention.id == mention_id).first()
    
    if not mention:
        raise HTTPException(status_code=404, detail="Mention not found in database")
    if not mention.platform_id:
        raise HTTPException(status_code=400, detail="Cannot reply: Missing platform ID")

    # ==========================================
    # YOUTUBE REPLY LOGIC
    # ==========================================
    if mention.source.lower() == models.MentionSource.YOUTUBE.lower():
        try:
            youtube_service.reply_to_comment(mention, payload.reply_text, db=db)

            mention.status = "Responded"
            db.commit()

            return {"status": "success", "message": "Reply posted successfully to YouTube!"}

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to post to YouTube: {str(e)}")

    # ==========================================
    # GOOGLE PLAY STORE REPLY LOGIC
    # ==========================================
    elif mention.source.lower() == models.MentionSource.PLAYSTORE.lower():
        try:
            if len(payload.reply_text) > 350:
                raise HTTPException(status_code=400, detail="Google Play limits replies to 350 characters.")

            package_name = playstore_service.reply_to_review(mention, payload.reply_text, db=db)

            mention.status = "Responded"
            db.commit()
            return {"status": "success", "message": f"Reply posted to Google Play for {package_name}!"}

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to post to Play Store: {str(e)}")

    # ==========================================
    # GOOGLE MAPS (BUSINESS PROFILE) REPLY LOGIC
    # ==========================================
    elif mention.source.lower() == models.MentionSource.GOOGLE_MAPS.lower():
        try:
            success = google_maps_service.reply_to_review(mention, payload.reply_text, db=db)
            if not success:
                raise HTTPException(status_code=500, detail="Couldn't post the reply to Google Maps. Check the server logs for details.")

            mention.status = "Responded"
            db.commit()
            return {"status": "success", "message": "Reply posted to Google Maps!"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")

    # ==========================================
    # FACEBOOK REPLY LOGIC
    # ==========================================
    elif mention.source.lower() == models.MentionSource.FACEBOOK.lower():
        try:
            page_name = meta_service.post_facebook_reply(mention, payload.reply_text, META_USER_TOKEN, db=db)
            mention.status = "Responded"
            db.commit()
            return {"status": "success", "message": f"Reply posted directly as {page_name}"}
        except meta_service.MetaReplyError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")

    # ==========================================
    # INSTAGRAM REPLY LOGIC
    # ==========================================
    elif mention.source.lower() == models.MentionSource.INSTAGRAM.lower():
        try:
            meta_service.post_instagram_reply(mention, payload.reply_text, META_USER_TOKEN, db=db)
            mention.status = "Responded"
            db.commit()
            return {"status": "success", "message": "Reply posted to Instagram!"}
        except meta_service.MetaReplyError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")

    # ==========================================
    # DIRECT MESSAGES (DM) REPLY LOGIC
    # ==========================================
    elif mention.source.lower() in [models.MentionSource.FACEBOOK_DM.lower(), models.MentionSource.INSTAGRAM_DM.lower()]:
        try:
            platform_name = meta_service.post_meta_dm_reply(mention, payload.reply_text, META_USER_TOKEN)
            mention.status = "Responded"
            db.commit()
            return {"status": "success", "message": f"{platform_name} direct message reply sent successfully!"}
        except meta_service.MetaReplyError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")

    # ==========================================
    # FALLBACK
    # ==========================================
    else:
        raise HTTPException(status_code=400, detail="Auto-reply not supported for this platform")

# ==========================================
# STRICT LIVE DELETE ENDPOINT
# ==========================================
@app.delete("/api/mentions/{mention_id:path}", dependencies=[Depends(auth.require_auth)])
def delete_mention(mention_id: str, db: Session = Depends(get_db)):
    mention = db.query(models.Mention).filter(models.Mention.id == mention_id).first()
    
    if not mention:
        raise HTTPException(status_code=404, detail="Mention not found in database")

    # ==========================================
    # 1. LIVE DELETE FROM META (Facebook / Instagram)
    # ==========================================
    if mention.source.lower() in [models.MentionSource.FACEBOOK.lower(), models.MentionSource.INSTAGRAM.lower()]:
        if not META_USER_TOKEN:
            raise HTTPException(status_code=400, detail="Meta user token missing from .env.")

        deleted_on_meta = False
        last_meta_error = ""

        try:
            accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
            accounts_res = requests.get(accounts_url, params={"access_token": META_USER_TOKEN})
            pages = accounts_res.json().get("data", []) if accounts_res.status_code == 200 else []

            ids_to_try = [mention.platform_id]
            if "_" in mention.platform_id:
                ids_to_try.append(mention.platform_id.split("_")[1])

            for page in pages:
                for target_id in ids_to_try:
                    del_url = f"https://graph.facebook.com/v26.0/{target_id}"
                    res = requests.delete(del_url, params={"access_token": page["access_token"]})
                    
                    if res.status_code == 200 and res.json().get("success", True):
                        logger.info(f"Successfully deleted {target_id} on Meta via page: {page['name']}")
                        deleted_on_meta = True
                        break
                    else:
                        last_meta_error = res.text
                if deleted_on_meta:
                    break

            if not deleted_on_meta:
                for target_id in ids_to_try:
                    del_url = f"https://graph.facebook.com/v26.0/{target_id}"
                    res = requests.delete(del_url, params={"access_token": META_USER_TOKEN})
                    
                    if res.status_code == 200 and res.json().get("success", True):
                        logger.info("Successfully deleted comment on Meta via User Token")
                        deleted_on_meta = True
                        break
                    else:
                        last_meta_error = res.text
                        
        except Exception as err:
            last_meta_error = str(err)

        if not deleted_on_meta:
            if '"code":100' in last_meta_error and ('"error_subcode":33' in last_meta_error or 'does not exist' in last_meta_error):
                logger.info("Notice: Meta says comment doesn't exist. It was likely already deleted natively. Cleaning up local database.")
            else:
                logger.error(f"Failed to delete live comment on Meta. Meta Error: {last_meta_error}")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to delete the live comment on Meta. Check the server logs for details."
                )

    # ==========================================
    # 2. LOCAL DATABASE DELETION
    # ==========================================
    try:
        db.query(models.Mention).filter(models.Mention.parent_id == mention.id).delete()
        db.delete(mention)
        db.commit()
        return {"status": "success", "message": "Mention deleted and removed from dashboard!"}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during deletion: {str(e)}")

# Load the webhook token from .env
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")

# ==========================================
# 1. META WEBHOOK VERIFICATION (GET)
# ==========================================
@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta sends a GET request here when you first configure the App Dashboard 
    to verify that your server is online and you know the secret token.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("✅ Webhook Verified successfully by Meta!")
        return PlainTextResponse(content=str(challenge), status_code=200)
    
    raise HTTPException(status_code=403, detail="Verification token mismatch")

# ==========================================
# 2. RECEIVE REAL-TIME MESSAGES (POST)
# ==========================================
@app.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives real-time Direct Messages for both Instagram and Facebook Messenger,
    processes them through the Groq AI engine, and nests all events inside their conversation threads.
    """
    payload = await request.json()
    webhook_object = payload.get("object")

    if webhook_object in ["instagram", "page"]:
        entries = payload.get("entry", [])
        
        for entry in entries:
            entry_id = entry.get("id", "")
            messaging_events = entry.get("messaging", [])
            
            for event in messaging_events:
                message_data = event.get("message", {})
                
                # Only process events containing a message payload
                if not message_data:
                    continue
                    
                msg_text = message_data.get("text", "").strip() or "[Media/Attachment Shared]"
                msg_id = message_data.get("mid")
                if not msg_id:
                    continue

                sender_id = event.get("sender", {}).get("id", "")
                
                # 1. Determine platform and generate stable ID
                is_ig = (webhook_object == "instagram")
                source = "Instagram DM" if is_ig else "Facebook DM"
                prefix = "ig_dm" if is_ig else "fb_dm"
                stable_id = f"{prefix}_{msg_id}"
                author = f"IG User ({sender_id})" if is_ig else f"FB User ({sender_id})"

                # 2. Duplicate check
                existing_msg = db.query(models.Mention).filter(
                    (models.Mention.id == stable_id) | (models.Mention.platform_id == msg_id)
                ).first()
                
                if existing_msg:
                    continue

                # 3. Parse timestamp
                timestamp = event.get("timestamp", 0)
                try:
                    pub_date = datetime.fromtimestamp(timestamp / 1000.0) if timestamp else datetime.utcnow()
                except Exception:
                    pub_date = datetime.utcnow()

                # 4. Direct Meta Inbox Link
                business_id = os.getenv("META_BUSINESS_ID", "")
                if is_ig:
                    direct_inbox_link = f"https://business.facebook.com/latest/inbox/all?asset_id={entry_id}&business_id={business_id}"
                else:
                    direct_inbox_link = (
                        f"https://business.facebook.com/latest/inbox/messenger"
                        f"?asset_id={entry_id}&business_id={business_id}"
                    )

                # 5. Conversation Parenting Logic
                # Check if a root card already exists for this sender
                existing_root = db.query(models.Mention).filter(
                    models.Mention.source == source,
                    models.Mention.author == author,
                    models.Mention.parent_id == None
                ).order_by(models.Mention.date.asc()).first()

                if existing_root:
                    assign_parent = existing_root.id
                    thread_id = existing_root.thread_id or f"thread_{sender_id}"
                    # Reopen the thread so the team knows a new customer interaction arrived
                    existing_root.status = "Unanswered"
                else:
                    # The first message or interaction becomes the root card
                    assign_parent = None
                    thread_id = f"thread_{sender_id}"

                # 6. AI Sentiment, Translation & Classification
                ai_sentiment, ai_label, ai_explanation = analyze_sentiment(msg_text)

                # 7. Save to Database
                new_dm = models.Mention(
                    id=stable_id,
                    source=source,
                    author=author,
                    content=msg_text,
                    sentiment=ai_sentiment,
                    label=ai_label,
                    explanation=ai_explanation,
                    link=direct_inbox_link,
                    date=pub_date,
                    platform_id=msg_id,
                    parent_id=assign_parent,
                    thread_id=thread_id,
                    status="Unanswered"
                )
                db.add(new_dm)
                db.commit()
                logger.info(f"📩 Real-time {source} saved & analyzed! [{sender_id}]: {msg_text}")

    return Response(content="EVENT_RECEIVED", status_code=200)