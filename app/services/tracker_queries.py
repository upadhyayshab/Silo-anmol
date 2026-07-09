"""Raw SQL behind the Business Daily Tracker.

Deliberately does NOT reuse reports.py's daily-order-summary:
  * its "marketing" view buckets DELIVERED by the booking cohort; the tracker dates
    deliveries by actual_delivery_date (verified 28/30 days against June).
  * its FILTER_CLASSIFICATION_SQL puts Organic Search / Referral / Direct under
    Lead Gen; the tracker puts them under Organic (design doc §4).
Both classifiers are correct for their own consumers. Do not merge them.
"""
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy import ARRAY, String, bindparam, text

REGIONS: Dict[str, Optional[List[str]]] = {
    "KN": ["karnataka"],
    "AP_TG": ["andhra pradesh", "telangana"],
    "PN": ["punjab"],
    "ALL": None,
}

# Paid / direct-response sources. Everything else falls through to Organic --
# including 'Organic Search', 'Referral Sites', 'Direct Traffic', unattributed rows,
# outlet-manager orders, and 'Telecaller' (auto-set when a telecaller creates a lead
# with no source given, i.e. origin unknown -- the same bucket as unattributed).
#
# This ONE constant feeds BOTH the order and the lead classifier. Do not append a
# source to one side only: a lead in Lead Gen whose order lands in Organic silently
# inflates Lead Gen's conversion rate and moves revenue between sections.
_LEADGEN_SOURCES = (
    "'fb lead ads','pay per click ads','social media','whatsapp inbound',"
    "'inbound phone call','outbound phone call','inbound email'"
)
# CAUTION -- the two D2C branches are keyed on DIFFERENT columns and are KNOWN to disagree:
#   orders: uid LIKE 'order%'            (placed through the Medusa storefront)
#   leads : source IN (_D2C_LEAD_SOURCES) (originated on the storefront)
# A 'medusa'-sourced lead is D2C, but if a telecaller places its order through the CRM the
# order's uid is not 'order%', so the order lands in Organic -- 111 such orders in June.
# This is a recorded, deferred business decision (design doc §9.2), not an oversight.
# It is the same failure shape as the 'telecaller' bug: leads in one section, orders in another.
_D2C_LEAD_SOURCES = "'medusa','add to cart','browsed 3 pages','web visit/login','app sign up'"

_ORDER_SQL = f"""
WITH days AS (
    SELECT generate_series(CAST(:start AS DATE), CAST(:end AS DATE), '1 day'::interval)::date AS day
),
sections AS (SELECT unnest(ARRAY['leadgen','d2c','organic']) AS section),
-- ponytail: `o` has no date filter and is referenced twice, so PG materializes it and
-- every query scans the whole table (~15k rows today, ~4ms). If customer_orders reaches
-- six figures, push `created_at >= :start - interval '90 days'` into `o`, or split the
-- booked-cohort and delivered scans into two independently-filtered CTEs.
o AS (
    SELECT co.uid,
           (co.created_at AT TIME ZONE 'Asia/Kolkata')::date AS booked_day,
           -- actual_delivery_date is a bare DATE (verified in information_schema), so it
           -- needs no AT TIME ZONE. reports.py:1500/1633/1690 shift it to IST; that is a
           -- bug in those reports, not a pattern to copy here.
           co.actual_delivery_date                            AS delivered_day,
           co.order_status,
           co.prepaid_amount,
           co.gross_amount - co.discount_applied              AS net_amount,
           COALESCE(oi.total_qty, 0)                          AS qty,
           CASE
               WHEN co.uid LIKE 'order%' THEN 'd2c'
               WHEN LOWER(lsq.lead_source) IN ({_LEADGEN_SOURCES}) THEN 'leadgen'
               ELSE 'organic'
           END AS section
    FROM customer_orders co
    LEFT JOIN (SELECT order_id, SUM(quantity) AS total_qty FROM order_items GROUP BY order_id) oi
           ON oi.order_id = co.uid
    LEFT JOIN order_attribution lsq ON lsq.order_id = co.uid
    WHERE co.deleted_at IS NULL
      AND (CAST(:states AS VARCHAR[]) IS NULL
           OR LOWER(TRIM(co.state)) = ANY(CAST(:states AS VARCHAR[])))
),
cohort AS (
    SELECT booked_day AS day, section,
           COUNT(*)                                                                   AS orders_booked,
           COALESCE(SUM(qty), 0)                                                      AS units_booked,
           COALESCE(SUM(net_amount), 0)                                               AS revenue_booked,
           COUNT(*) FILTER (WHERE prepaid_amount > 0)                                 AS prepaid_booked_orders,
           COUNT(*) FILTER (WHERE order_status = 'CANCELLED')                         AS orders_cancelled,
           COALESCE(SUM(qty) FILTER (WHERE order_status = 'CANCELLED'), 0)            AS units_cancelled,
           COALESCE(SUM(net_amount) FILTER (WHERE order_status = 'CANCELLED'), 0)     AS units_cancelled_amount,
           COUNT(*) FILTER (WHERE order_status NOT IN ('DELIVERED','CANCELLED'))      AS orders_pending,
           COALESCE(SUM(qty) FILTER (WHERE order_status NOT IN ('DELIVERED','CANCELLED')), 0)        AS units_pending,
           COALESCE(SUM(net_amount) FILTER (WHERE order_status NOT IN ('DELIVERED','CANCELLED')), 0) AS units_pending_amount
    FROM o
    WHERE booked_day BETWEEN CAST(:start AS DATE) AND CAST(:end AS DATE)
    GROUP BY 1, 2
),
deliv AS (
    SELECT delivered_day AS day, section,
           COUNT(*)                     AS orders_delivered,
           COALESCE(SUM(qty), 0)        AS units_delivered,
           COALESCE(SUM(net_amount), 0) AS revenue_received
    FROM o
    WHERE order_status = 'DELIVERED'
      AND delivered_day BETWEEN CAST(:start AS DATE) AND CAST(:end AS DATE)
    GROUP BY 1, 2
)
SELECT d.day, s.section,
       COALESCE(c.orders_booked, 0)          AS orders_booked,
       COALESCE(c.units_booked, 0)           AS units_booked,
       COALESCE(c.revenue_booked, 0)         AS revenue_booked,
       COALESCE(c.prepaid_booked_orders, 0)  AS prepaid_booked_orders,
       COALESCE(c.orders_cancelled, 0)       AS orders_cancelled,
       COALESCE(c.units_cancelled, 0)        AS units_cancelled,
       COALESCE(c.units_cancelled_amount, 0) AS units_cancelled_amount,
       COALESCE(c.orders_pending, 0)         AS orders_pending,
       COALESCE(c.units_pending, 0)          AS units_pending,
       COALESCE(c.units_pending_amount, 0)   AS units_pending_amount,
       COALESCE(dl.orders_delivered, 0)      AS orders_delivered,
       COALESCE(dl.units_delivered, 0)       AS units_delivered,
       COALESCE(dl.revenue_received, 0)      AS revenue_received
FROM days d
CROSS JOIN sections s
LEFT JOIN cohort c  ON c.day  = d.day AND c.section  = s.section
LEFT JOIN deliv  dl ON dl.day = d.day AND dl.section = s.section
ORDER BY d.day, s.section;
"""

_LEAD_SQL = f"""
WITH days AS (
    SELECT generate_series(CAST(:start AS DATE), CAST(:end AS DATE), '1 day'::interval)::date AS day
),
sections AS (SELECT unnest(ARRAY['leadgen','d2c','organic']) AS section),
l AS (
    SELECT ld.uid,
           (ld.created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
           ld.stage::text AS stage,
           -- Both classifiers MUST share _LEADGEN_SOURCES verbatim. Adding a source to one
           -- side only (e.g. 'telecaller') files a lead under Lead Gen while its order lands
           -- in Organic: July had 178 such leads and 127 such orders, 6% of the month.
           CASE
               WHEN LOWER(ld.source::text) IN ({_D2C_LEAD_SOURCES}) THEN 'd2c'
               WHEN LOWER(ld.source::text) IN ({_LEADGEN_SOURCES}) THEN 'leadgen'
               ELSE 'organic'
           END AS section
    FROM leads ld
    WHERE ld.deleted_at IS NULL
      AND (ld.created_at AT TIME ZONE 'Asia/Kolkata')::date
          BETWEEN CAST(:start AS DATE) AND CAST(:end AS DATE)
      AND (CAST(:states AS VARCHAR[]) IS NULL
           OR LOWER(TRIM(ld.state)) = ANY(CAST(:states AS VARCHAR[])))
)
SELECT d.day, s.section,
       COUNT(l.uid)                                                AS leads_total,
       COUNT(l.uid) FILTER (WHERE l.stage = 'New Lead')            AS unprocessed_leads,
       COUNT(l.uid) FILTER (WHERE l.stage = 'Not Reachable')       AS not_connected_rnr,
       COUNT(l.uid) FILTER (WHERE l.stage = 'Engaged')             AS qualified_leads,
       COUNT(l.uid) FILTER (WHERE l.stage = 'Not Qualified')       AS not_qualified_leads
FROM days d
CROSS JOIN sections s
LEFT JOIN l ON l.day = d.day AND l.section = s.section
GROUP BY d.day, s.section
ORDER BY d.day, s.section;
"""

_ORDER_COLS = [
    "orders_booked", "units_booked", "revenue_booked", "prepaid_booked_orders",
    "orders_cancelled", "units_cancelled", "units_cancelled_amount",
    "orders_pending", "units_pending", "units_pending_amount",
    "orders_delivered", "units_delivered", "revenue_received",
]
_LEAD_COLS = ["leads_total", "unprocessed_leads", "not_connected_rnr", "qualified_leads", "not_qualified_leads"]


def _day_index(start: date, end: date) -> Dict[date, int]:
    n = (end - start).days + 1
    return {start + timedelta(days=i): i for i in range(n)}


async def _run(conn, sql: str, start: date, end: date, states, cols: List[str], rename=None):
    stmt = text(sql).bindparams(bindparam("states", type_=ARRAY(String())))
    result = await conn.execute(stmt, {"start": start, "end": end, "states": states})
    idx = _day_index(start, end)
    n = len(idx)
    out: Dict[str, List[float]] = {}
    for row in result.mappings():
        i = idx[row["day"]]
        for c in cols:
            key = f"{row['section']}.{(rename or {}).get(c, c)}"
            out.setdefault(key, [0.0] * n)[i] = float(row[c] or 0)
    return out


async def fetch_order_facts(conn, start: date, end: date, states):
    return await _run(conn, _ORDER_SQL, start, end, states, _ORDER_COLS)


async def fetch_lead_facts(conn, start: date, end: date, states):
    # Lead Gen names its leads row `leads_lsq_total`; D2C and Organic call it `leads_total`.
    facts = await _run(conn, _LEAD_SQL, start, end, states, _LEAD_COLS)
    if "leadgen.leads_total" in facts:
        facts["leadgen.leads_lsq_total"] = facts.pop("leadgen.leads_total")
    return facts
