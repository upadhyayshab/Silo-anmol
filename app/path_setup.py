"""
Path setup for SharedBackend imports
"""
import sys
import os
from pathlib import Path

# Add SharedBackend/src to Python path
# Go up one level from app directory to find SharedBackend
current_dir = Path(__file__).parent.parent  # Go up from app to root
shared_backend_src = current_dir / "SharedBackend" / "src"

if shared_backend_src.exists():
    sys.path.insert(0, str(shared_backend_src))
    
    # Create temporary __init__.py file if it doesn't exist (for imports to work)
    # This is a local-only fix that doesn't affect the submodule
    init_file = shared_backend_src / "__init__.py"
    if not init_file.exists():
        try:
            with open(init_file, 'w') as f:
                f.write("# Temporary file for Python package imports - do not commit to SharedBackend\n")
            print(f"✅ Created temporary __init__.py for imports")
        except Exception as e:
            print(f"⚠️ Could not create temporary __init__.py: {e}")
    
    print(f"✅ SharedBackend path added: {shared_backend_src}")
else:
    print(f"❌ SharedBackend not found at: {shared_backend_src}")
