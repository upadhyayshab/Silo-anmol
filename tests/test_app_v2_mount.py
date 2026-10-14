"""Confirms the v2 wizard router is mounted at /api/v2."""
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestV2Mount(TestCase):
    def test_wizard_routes_mounted_with_correct_prefix(self):
        import app as app_module

        paths = [r.path for r in app_module.app.routes]
        self.assertIn("/api/v2/wizard/questions", paths)
        self.assertIn("/api/v2/wizard/submit", paths)
