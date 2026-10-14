import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestV2ReportsMount(TestCase):
    def test_reports_routes_mounted_under_api_v2(self):
        import app as app_module
        paths = [r.path for r in app_module.app.routes]
        self.assertIn("/api/v2/reports/users/{user_id}/reports", paths)
        self.assertIn("/api/v2/reports/recommendations/{recommendation_id}/report", paths)
        self.assertIn("/api/v2/reports/crops/{crop_id}/report", paths)
