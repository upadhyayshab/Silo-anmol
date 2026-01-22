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