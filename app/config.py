import os
from pathlib import Path
from functools import lru_cache
from typing import List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import create_async_engine

basedir = Path(__file__).parents[1]
load_dotenv(basedir / ".env")


class Settings(BaseSettings):
    # =============================================================================
    # HARDCODED NON-SENSITIVE CONFIGURATION
    # =============================================================================
    
    # Application Configuration (hardcoded)
    version: str = "1.0.0"
    commit: str = "production"
    branch: str = "main"
    build_time: str = "2024-01-20"
    build_number: str = "1"
    build_tags: List[str] = ["production", "erp", "backend"]
    
    # CORS Configuration (hardcoded)
    allowed_origins: List[str] = [
        "https://erp.gausampurna.com",
        "https://admin.gausampurna.com", 
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://localhost:5173",  # Vite default
        "http://localhost:5174",  # Vite alternative
        "http://localhost:8080",  # Common dev port
        "http://localhost:8081",  # Common dev port
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8080",
        "*"  # Allow all origins for development - remove in production
    ]
    
    # JWT Configuration (hardcoded non-sensitive parts)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7
    
    # Password Security (hardcoded)
    bcrypt_rounds: int = 12
    
    # File Upload Configuration (hardcoded)
    upload_dir: str = "/app/uploads"
    max_file_size: int = 5242880  # 5MB
    allowed_file_types: List[str] = ["image/png", "image/jpeg", "image/jpg"]
    
    # Logging Configuration (hardcoded)
    log_level: str = "INFO"
    log_format: str = "json"
    log_file: str = "/app/logs/silo-erp.log"
    
    # Health Check Configuration (hardcoded)
    health_check_timeout: int = 30
    db_health_check_timeout: int = 10
    
    # Rate Limiting Configuration (hardcoded)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 100
    rate_limit_window: int = 60
    
    # Database Pool Configuration (hardcoded)
    db_pool_size: int = 20
    db_max_overflow: int = 30
    db_pool_timeout: int = 30
    db_pool_recycle: int = 3600
    
    # =============================================================================
    # ENVIRONMENT VARIABLES (FROM AWS SSM PARAMETER STORE)
    # =============================================================================
    
    # Service Configuration (from environment)
    name: str = os.getenv("SERVICE_NAME", "silo-erp-backend")
    env: str = os.getenv("ENV", "production")
    debug: bool = bool(int(os.getenv("DEBUG", "0")))
    
    # Database Configuration (from environment)
    db_dialect: str = os.getenv("DB_DIALECT", "sqlite")
    db_driver: str = os.getenv("DB_DRIVER", "aiosqlite")
    db_host: str = os.getenv("DB_HOST", "")
    db_port: str = os.getenv("DB_PORT", "5432")
    db_name: str = os.getenv("DB_NAME", "")
    db_user: str = os.getenv("DB_USER", "")
    db_password: str = os.getenv("DB_PASSWORD", "")
    supports_schema: bool = bool(int(os.getenv("DB_SUPPORTS_SCHEMA", "0")))  # Default to False for SQLite
    
    # Security Configuration (from environment)
    master_api_key: str = os.getenv("MASTER_API_KEY", "")
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "")

    # CRM Configuration (from environment)
    crm_url: str = os.getenv("CRM_URL", "")
    crm_api_key: str = os.getenv("CRM_API_KEY", "")
    crm_secret_key: str = os.getenv("CRM_SECRET_KEY", "")

    # Medusa webhook
    store_url:str = os.getenv("STORE_URL","https://store-backend.gausampurna.co")
    # Shared secret Medusa must send in the X-Store-Webhook-Secret header.
    store_webhook_secret: str = os.getenv("STORE_WEBHOOK_SECRET", "")

    # Driver App Configuration (from environment)
    driver_url: str = os.getenv("DRIVER_URL", "")
    driver_api_key: str = os.getenv("DRIVER_API_KEY", "Delivery-secret-key")

    # Smartping Configuration (from environment)
    smartping_api_key: str = os.getenv("SMARTPING_API_KEY", "")

    # Exotel Telephony Configuration (from environment) — Feature 3.
    # All env keys use the DAILER_* prefix (ops-facing, vendor-agnostic name).
    exotel_api_key: str = os.getenv("DAILER_API_KEY", "")        # dialer key
    exotel_api_token: str = os.getenv("DAILER_TOKEN", "")        # dialer token
    exotel_sid: str = os.getenv("DAILER_SID", "")               # account SID
    exotel_subdomain: str = os.getenv("DAILER_SUBDOMAIN", "api.exotel.com")
    exotel_ccm_subdomain: str = os.getenv("DAILER_CCM_SUBDOMAIN", "ccm-api.exotel.com")  # Users/agent dir (Mumbai: ccm-api.in.exotel.com)
    exotel_crm_flow_id: str = os.getenv("DAILER_CRM_FLOW_ID", "")  # call-flow id of the CRM app; scopes the ExoPhone list (auto-map VirtualNumber)
    exotel_email_overrides: str = os.getenv("DAILER_EMAIL_OVERRIDES", "")  # crm_email:exotel_email pairs where they differ
    # exotel_email:sip pairs so inbound rings the agent's WebRTC softphone (Feature 3.2)
    # rather than their PSTN phone, e.g. "crm+1@silofortune.com:sip:naveenh37746fa6".
    exotel_sip_map: str = os.getenv("DAILER_SIP_MAP", "")
    # WebRTC CRM softphone SDK (IP-PSTN-intermix onboarding — app entity id+secret from Exotel)
    exotel_app_id: str = os.getenv("DAILER_APP_ID", "")
    exotel_app_secret: str = os.getenv("DAILER_APP_SECRET", "")
    exotel_app_entity: str = os.getenv("DAILER_APP_ENTITY", "app")  # token scope sent to Exotel as Entity
    exotel_integrations_host: str = os.getenv("DAILER_INTEGRATIONS_HOST", "integrationscore.mum1.exotel.com")

    # Facebook Lead Ads Configuration (from environment)
    fb_app_secret: str = os.getenv("FB_APP_SECRET", "")            # app secret -> webhook HMAC
    fb_verify_token: str = os.getenv("FB_VERIFY_TOKEN", "")        # our chosen subscription token
    fb_page_access_token: str = os.getenv("FB_PAGE_ACCESS_TOKEN") or os.getenv("FB_SYSTEM_USER_TOKEN", "")  # long-lived/system-user token
    fb_graph_version: str = os.getenv("FB_GRAPH_VERSION", "v21.0")
    # Multi-page (System User token in fb_page_access_token above mints page tokens on demand)
    fb_business_id: str = os.getenv("FB_BUSINESS_ID", "")
    # Conversions API — lead-stage events back to Meta (defaults to the live GauSampurna-PM dataset)
    fb_capi_dataset_id: str = os.getenv("FB_CAPI_DATASET_ID", "1426569352558576")
    fb_capi_access_token: str = (os.getenv("FB_CAPI_ACCESS_TOKEN")
                                 or os.getenv("FB_SYSTEM_USER_TOKEN")
                                 or os.getenv("FB_PAGE_ACCESS_TOKEN", ""))

    # =============================================================================
    # COMPUTED PROPERTIES
    # =============================================================================
    
    @property
    def engine_url(self) -> str:
        """Construct database URL from components"""
        return f"{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
    
    @property
    def engine_str(self) -> str:
        """Construct full database connection string"""
        if self.db_host:
            return f"{self.db_dialect}+{self.db_driver}://{self.engine_url}"
        else:
            # Use a proper SQLite file for development
            return "sqlite+aiosqlite:///./silo_erp.db"
    
    @property
    def database_config(self) -> dict:
        """Get database configuration for SQLAlchemy"""
        # Only apply pool settings for PostgreSQL, not SQLite
        if self.db_host and self.db_dialect == "postgresql":
            config = {
                "pool_size": self.db_pool_size,
                "max_overflow": self.db_max_overflow,
                "pool_timeout": self.db_pool_timeout,
                "pool_recycle": self.db_pool_recycle,
                "pool_pre_ping": True,
                "echo": self.debug
            }
        else:
            # For SQLite or when no DB host is configured
            config = {
                "echo": self.debug
            }
        return config

    class subservices:  # noqa
        pass


@lru_cache()
def get_settings():
    return Settings()


@lru_cache()
def get_engine(schema: str):
    settings = get_settings()
    
    # Create engine with proper configuration
    if settings.supports_schema:
        engine = create_async_engine(
            settings.engine_str,
            execution_options={"schema_translate_map": {None: schema}},
            **settings.database_config
        )
    else:
        engine = create_async_engine(
            settings.engine_str,
            **settings.database_config
        )
    return engine
