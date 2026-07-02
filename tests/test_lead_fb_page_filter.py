"""FB Page filter on LeadManager.search_leads (Task 9).

FB leads store their originating page inside `leads.campaign_data` JSON as
`page_id` (see app/services/facebook_leads.py:87) -- there is no plain column,
so search_leads needs a dedicated `fb_page_id` kwarg that filters on the JSON
path. Must work on Postgres (`campaign_data->>'page_id'`) AND sqlite (what the
test suite runs against, since DB_HOST is unset in the test environment) --
this uses SQLAlchemy's cross-dialect `Schema.campaign_data['page_id'].as_string()`
comparator rather than a Postgres-only `.astext`/`->>'` construct.

Run::
    pytest tests/test_lead_fb_page_filter.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from managers import LeadManager  # noqa: E402
from managers.crmManagers import LeadSchema  # noqa: E402


def _lead(**overrides):
    fields = dict(
        first_name="Test", last_name="Lead", mobile="9000000000",
        lead_number=overrides.pop("lead_number", "L-TEST"),
    )
    fields.update(overrides)
    return LeadSchema(**fields)


class TestSearchLeadsFbPageFilter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Fresh in-memory sqlite engine per test (a shared :memory: engine
        # across tests would accumulate rows from earlier tests) -- same
        # engine flavor the rest of the suite runs against (DB_HOST unset
        # => sqlite+aiosqlite per app/config.py).
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.manager = LeadManager(self.engine)
        await self.manager.init_db()

    async def test_matches_lead_with_page_id_in_campaign_data(self):
        await self.manager.create(_lead(
            lead_number="L-FB-1", mobile="9111111111",
            campaign_data={"page_id": "111"},
        ))
        await self.manager.create(_lead(
            lead_number="L-FB-2", mobile="9222222222",
            campaign_data={"page_id": "222"},
        ))

        items, total = await self.manager.search_leads(fb_page_id="111")

        self.assertEqual(total, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].lead_number, "L-FB-1")

    async def test_excludes_lead_with_no_campaign_data(self):
        await self.manager.create(_lead(
            lead_number="L-FB-3", mobile="9333333333",
            campaign_data={"page_id": "111"},
        ))
        await self.manager.create(_lead(
            lead_number="L-NO-CD", mobile="9444444444",
            campaign_data=None,
        ))

        items, total = await self.manager.search_leads(fb_page_id="111")

        # NULL campaign_data must be excluded quietly, not raise.
        self.assertEqual(total, 1)
        self.assertEqual(items[0].lead_number, "L-FB-3")

    async def test_no_fb_page_id_does_not_filter(self):
        await self.manager.create(_lead(lead_number="L-A", mobile="9555555555", campaign_data={"page_id": "111"}))
        await self.manager.create(_lead(lead_number="L-B", mobile="9666666666", campaign_data=None))

        items, total = await self.manager.search_leads()

        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
