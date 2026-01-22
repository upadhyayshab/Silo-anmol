import path_setup  # Setup SharedBackend path
import sys
import os
from pathlib import Path
from contextlib import asynccontextmanager

# Add SharedBackend to Python path
current_dir = Path(__file__).parent
shared_backend_path = current_dir.parent / "SharedBackend" / "src"
if str(shared_backend_path) not in sys.path:
    sys.path.insert(0, str(shared_backend_path))

import sqlalchemy as db
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# SharedBackend imports with proper path resolution
import sys
from pathlib import Path

# Add SharedBackend/src to Python path
current_dir = Path(__file__).parent.parent
shared_backend_src = current_dir / "SharedBackend" / "src"

if shared_backend_src.exists():
    sys.path.insert(0, str(shared_backend_src))
    print(f"✅ Added SharedBackend path: {shared_backend_src}")
else:
    print(f"⚠️ SharedBackend path not found: {shared_backend_src}")

# Import SharedBackend modules
from SharedBackend.managers import ApiKeyManager, BaseSchema, EntityManager, GenericManager
from SharedBackend.middlewares import SDKMiddleware, EntityMiddleware

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
    print(f"📊 Database: {settings.database_url}")
    print(f"🔧 Environment: {settings.environment}")
    
    yield
    
    # Shutdown
    print(f"🛑 Shutting down {settings.name} API Server")

# Create FastAPI app
app = FastAPI(
    title=f"{settings.name} API",
    description="ERP System API with comprehensive business management features",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url="/redoc" if settings.environment != "production" else None,
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
app.add_middleware(SDKMiddleware)
app.add_middleware(EntityMiddleware)

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
        "environment": settings.environment,
        "docs": "/docs" if settings.environment != "production" else "disabled",
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": settings.name,
        "version": settings.version,
        "environment": settings.environment,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "development",
    )