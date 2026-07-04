"""Unit test for the pure attribution mapper (leadService.attribution_values).

DB-free: exercises the campaign_data -> order_attribution column mapping with a
fake lead. Run with::

    python tests/test_attribution_values.py
    # or: pytest tests/test_attribution_values.py
"""
import os
import sys
from enum import Enum
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

try:  # path_setup prints emoji; keep it from crashing on Windows cp1252 stdout
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import path_setup  # noqa: F401,E402  (puts SharedBackend on the path)
from services.leadService import attribution_values  # noqa: E402


class _Src(str, Enum):
    FB = "FB Lead Ads"


def _lead(**kw):
    base = dict(uid="lead_1", source=None, stage=SimpleNamespace(value="ftu"),
                campaign_data=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_maps_fb_campaign_data():
    cd = {"ad_id": "A1", "adset_id": "S1", "campaign_id": "C1",
          "source_campaign": "Diwali"}
    v = attribution_values(_lead(source=_Src.FB, campaign_data=cd), "order_x")
    assert v["order_id"] == "order_x"
    assert v["lead_id"] == "lead_1"
    assert v["lead_source"] == "FB Lead Ads"      # enum -> .value
    assert v["ad_set_id"] == "S1"                 # adset_id -> ad_set_id
    assert v["ad_id"] == "A1"
    assert v["campaign_id"] == "C1"
    assert v["source_campaign"] == "Diwali"
    assert v["lead_stage"] == "ftu"
    assert v["utm_param"] == cd                    # whole blob preserved


def test_null_safe_when_no_campaign_data():
    v = attribution_values(_lead(), "order_y")
    assert v["order_id"] == "order_y"
    assert v["ad_id"] is None and v["ad_set_id"] is None
    assert v["campaign_id"] is None and v["source_campaign"] is None
    assert v["utm_param"] is None                  # empty {} -> None, not '{}'
    assert v["lead_source"] is None                # None source stays None


def test_ad_set_id_falls_back_to_ad_set_id_key():
    # LSQ imports may already use the ad_set_id spelling.
    v = attribution_values(_lead(campaign_data={"ad_set_id": "S2"}), "o")
    assert v["ad_set_id"] == "S2"


def test_last_touch_wins_over_first_touch():
    # Flat keys = first touch; touches[-1] = latest. Latest ad/campaign must win.
    cd = {
        "ad_id": "A1", "campaign_id": "C1", "source_campaign": "First",
        "touches": [
            {"ad_id": "A2", "campaign_id": "C2", "source_campaign": "Second"},
            {"ad_id": "A3", "adset_id": "S3", "campaign_id": "C3", "source_campaign": "Third"},
        ],
    }
    v = attribution_values(_lead(campaign_data=cd), "o")
    assert v["ad_id"] == "A3"
    assert v["campaign_id"] == "C3"
    assert v["ad_set_id"] == "S3"
    assert v["source_campaign"] == "Third"


def test_latest_touch_field_falls_back_to_first_touch():
    # Latest touch omits adset -> fall back to the flat first-touch adset_id.
    cd = {
        "ad_id": "A1", "adset_id": "S1",
        "touches": [{"ad_id": "A2", "campaign_id": "C2"}],  # no adset here
    }
    v = attribution_values(_lead(campaign_data=cd), "o")
    assert v["ad_id"] == "A2"          # from latest touch
    assert v["ad_set_id"] == "S1"      # fell back to first-touch flat key


def test_empty_touches_uses_flat_keys():
    cd = {"ad_id": "A1", "touches": []}
    v = attribution_values(_lead(campaign_data=cd), "o")
    assert v["ad_id"] == "A1"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
