import path_setup  # Must be first to set up Python path

import sys
import os
from pathlib import Path
from contextlib import asynccontextmanager
import sqlalchemy as db
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import SharedBackend modules
from SharedBackend.managers import ApiKeyManager, BaseSchema, EntityManager, GenericManager

# Safe import of middlewares with fallback
try:
    from SharedBackend.middlewares import SDKMiddleware, EntityMiddleware
    MIDDLEWARES_AVAILABLE = True
    print("✅ SharedBackend middlewares imported successfully")
except Exception as e:
    print(f"⚠️  SharedBackend middlewares import failed: {e}")
    MIDDLEWARES_AVAILABLE = False
    
    # Create fallback middleware classes
    from starlette.middleware.base import BaseHTTPMiddleware
    
    class SDKMiddleware(BaseHTTPMiddleware):
        def __init__(self, app, key_manager=None):
            super().__init__(app)
            self.key_manager = key_manager
        
        async def dispatch(self, request, call_next):
            return await call_next(request)
    
    class EntityMiddleware(BaseHTTPMiddleware):
        def __init__(self, app, entity_manager=None):
            super().__init__(app)
            self.entity_manager = entity_manager
        
        async def dispatch(self, request, call_next):
            return await call_next(request)

# Import local modules
from config import get_settings, get_engine
from routers import v1_router, auth_router, admin_router

# Get settings
settings = get_settings()
engine = get_engine(settings.name)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    print(f"🚀 Starting {settings.name} API Server")
    print(f"📊 Database: {settings.engine_str}")
    print(f"🔧 Environment: {settings.env}")
    
    yield
    
    # Shutdown
    print(f"🛑 Shutting down {settings.name} API Server")

# Create FastAPI app
app = FastAPI(
    title=f"{settings.name} API",
    description="ERP System API with comprehensive business management features",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add SharedBackend middlewares
key_manager = ApiKeyManager(engine)
entity_manager = EntityManager(engine)

# Add middlewares with error handling
try:
    app.add_middleware(SDKMiddleware, key_manager=key_manager)
    app.add_middleware(EntityMiddleware, entity_manager=entity_manager)
    print("✅ Middlewares added successfully")
except Exception as e:
    print(f"⚠️  Middleware setup failed: {e}")
    print("🔄 Continuing without middlewares...")

# Include routers
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(admin_router, prefix="/admin", tags=["Admin"])
app.include_router(v1_router, prefix="/api/v1", tags=["API v1"])

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": f"Welcome to {settings.name} API",
        "version": settings.version,
        "environment": settings.env,
        "docs": "/docs",
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": settings.name,
        "version": settings.version,
        "environment": settings.env,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.env == "development",
    )