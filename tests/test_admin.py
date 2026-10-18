import unittest

from fastapi.testclient import TestClient
from app import app


class TestAdminEndpoints(unittest.TestCase):
    # Entering the client runs the app's startup (create_all on the in-memory
    # SQLite engine); without it every request failed with "no such table".
    @classmethod
    def setUpClass(cls):
        cls.client_cm = TestClient(app)
        global client
        client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def test_health_check(self):
        response = client.get(f"/admin/health-check", headers={"X-API-Key": "12345678-unsafe-master-key"})
        self.assertEqual(200, response.status_code)

    def test_generate_api_key(self):
        payload = {
            "scopes": ["sdk:true"]
        }
        response = client.post(f"/admin/generate-api-key",
                               headers={"X-API-Key": "12345678-unsafe-master-key", "Content-Type": "application/json"},
                               json=payload)
        self.assertEqual(200, response.status_code)
        self.assertIn("uid", response.json())
        self.assertIn("key", response.json())
        self.assertIn("scopes", response.json())
        return response.json()

    # Returned 500 on SharedBackend 843ac1d (fetch_one IndexError); fixed by
    # pinning 1e6bd7f, the commit Agro uses.
    def test_revoke_api_key(self):
        r1 = self.test_generate_api_key()
        response = client.delete(f"/admin/revoke-api-key/{r1['uid']}",
                                 headers={"X-API-Key": "12345678-unsafe-master-key"})
        self.assertEqual(200, response.status_code)


if __name__ == '__main__':
    unittest.main()
