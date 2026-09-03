from sqlalchemy import Column, String, Text, DateTime
from database import Base
from datetime import datetime, timezone


class MentionSource:
    """Canonical values for Mention.source. Every place that writes or compares
    against a mention's source should use these instead of retyping the string -
    that way a typo is an ImportError/AttributeError, not a silent mismatch."""
    YOUTUBE = "YouTube"
    PLAYSTORE = "PlayStore"
    FACEBOOK = "Facebook"
    FACEBOOK_DM = "Facebook DM"
    INSTAGRAM = "Instagram"
    INSTAGRAM_DM = "Instagram DM"
    NEWS = "News"
    GOOGLE_MAPS = "Google Maps"

    ALL = [YOUTUBE, PLAYSTORE, FACEBOOK, FACEBOOK_DM, INSTAGRAM, INSTAGRAM_DM, NEWS, GOOGLE_MAPS]
    # Sources a reply can actually be posted back to (News has no reply capability).
    REPLYABLE = [YOUTUBE, PLAYSTORE, FACEBOOK, FACEBOOK_DM, INSTAGRAM, INSTAGRAM_DM, GOOGLE_MAPS]


class Mention(Base):
    __tablename__ = "mentions"

    id = Column(String, primary_key=True, index=True) 
    source = Column(String, index=True)
    
    # --- NEW COLUMN FOR YOUTUBE REPLIES ---
    platform_id = Column(String, unique=True, index=True, nullable=True)
    
    author = Column(String)
    content = Column(Text)
    sentiment = Column(String, index=True)
    
    # --- QWEN COLUMNS ---
    label = Column(String, default="General Inquiry")
    explanation = Column(Text)
    
    link = Column(String)
    date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    parent_id = Column(String, nullable=True)
    status = Column(String, default="Unanswered")
    thread_id = Column(String, index=True, nullable=True)