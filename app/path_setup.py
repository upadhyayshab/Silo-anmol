"""
Path setup for SharedBackend imports - MUST BE IMPORTED FIRST
"""
import sys
import os
from pathlib import Path

# Get the project root directory
project_root = Path(__file__).parent.parent

# Use the local SharedBackend
local_shared_backend = project_root / "SharedBackend" / "src"
if local_shared_backend.exists():
    if str(local_shared_backend) not in sys.path:
        sys.path.insert(0, str(local_shared_backend))
    print(f"✅ Local SharedBackend path added: {local_shared_backend}")
else:
    print(f"❌ Local SharedBackend not found at: {local_shared_backend}")

# Debug: Print current Python path
print("🔍 Current Python path includes:")
for i, path in enumerate(sys.path[:5]):  # Show first 5 paths
    print(f"  {i}: {path}")
