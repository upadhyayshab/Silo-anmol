import os
from pathlib import Path
from functools import lru_cache
from typing import Optional
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pydantic.v1 import BaseSettings
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine

basedir = Path(__file__).parents[1]
load_dotenv(basedir / ".env")


class Settings(BaseSettings):
    name: str = os.getenv("SERVICE_NAME", "template-backend-service")
    version: str = "0.1.0"
    commit: str = os.getenv("COMMIT", "unknown")
    branch: str = os.getenv("BRANCH", "unknown")
    build_time: str = os.getenv("BUILD_TIME", "unknown")
    build_number: str = os.getenv("BUILD_NUMBER", "unknown")
    build_tags: tuple = tuple(os.getenv("BUILD_TAGS", "").split())

    env: str = os.getenv("ENV", "dev")
    debug: bool = bool(int(os.getenv("DEBUG", "1")))
    allowed_origins: tuple[str] = tuple(os.getenv("CORS_ORIGINS", "*").split(","))

    _pw = quote_plus(os.getenv("DB_PASSWORD", ""))  # encode @ $ # etc.
    engine_url = f"{os.getenv('DB_USER')}:{_pw}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    engine_str: str = f"{os.getenv('DB_DIALECT')}+{os.getenv('DB_DRIVER')}://{engine_url}" \
        if os.getenv("DB_HOST") else "sqlite+aiosqlite:///:memory:"
    supports_schema = bool(int(os.getenv("DB_SUPPORTS_SCHEMA", "0")))
    master_api_key: str = os.getenv("MASTER_API_KEY", "12345678-unsafe-master-key")
    google_maps_api_key: Optional[str] = os.getenv("GOOGLE_MAPS_API_KEY")
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    subscription_base_url: str = os.getenv("SUBSCRIPTION_BASE_URL", "")

    msk_bootstrap_servers: str = os.getenv("MSK_BOOTSTRAP_SERVERS", "")
    msk_region: str = os.getenv("MSK_REGION", "") or os.getenv("REGION_NAME", "ap-south-1")
    msk_role_arn: str = os.getenv("MSK_ROLE_ARN", "")
    crop_guide_completed_topic: str = os.getenv("CROP_GUIDE_COMPLETED_TOPIC", "crop_guide_completed")
    agro_internal_base_url: str = os.getenv("AGRO_INTERNAL_BASE_URL", "")
    internal_service_secret: str = os.getenv("INTERNAL_SERVICE_SECRET", "")
    broker_type: str = os.getenv("BROKER_TYPE", "memory")  # memory | kafka | sqs

    class subservices:  # noqa
        pass


@lru_cache()
def get_settings():
    return Settings()


@lru_cache()
def get_engine(schema: str):
    settings = get_settings()
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_size": 20,
        "max_overflow": 10,
    } if not settings.engine_str.startswith("sqlite") else {}
    if settings.engine_str.endswith(":memory:"):
        # Tests: one shared connection, or every connection gets its own
        # empty in-memory database.
        engine_kwargs = {"poolclass": StaticPool}

    if settings.supports_schema:
        engine = create_async_engine(
            settings.engine_str,
            execution_options={"schema_translate_map": {None: schema}},
            **engine_kwargs
        )
    else:
        engine = create_async_engine(settings.engine_str, **engine_kwargs)
    return engine
