"""Unit tests for the pure FB field-mapping logic (apply_resolved).

Loads facebook_mapping standalone (no services package / no DB). Run with::

    python tests/test_facebook_mapping.py
"""
import os
import sys
import importlib.util

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "SharedBackend", "src"))
sys.path.insert(0, os.path.join(ROOT, "app"))

_spec = importlib.util.spec_from_file_location(
    "fb_mapping_under_test", os.path.join(ROOT, "app", "services", "facebook_mapping.py"))
fbm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fbm)


def _default_resolved():
    resolved = {}
    for meta, target in fbm.DEFAULT_QUESTION_MAP:
        resolved[("question", meta)] = {"target": target, "target_kind": "lead", "is_active": True}
    for meta, target in fbm.DEFAULT_MARKETING_MAP:
        resolved[("marketing", meta)] = {"target": target, "target_kind": "campaign_data", "is_active": True}
    return resolved


def test_marketing_maps_to_campaign_data():
    resolved = _default_resolved()
    data, campaign, custom = fbm.apply_resolved(
        resolved,
        {"campaign_name": "Spring Sale", "ad_id": "123"},
        {},
    )
    assert campaign["source_campaign"] == "Spring Sale"   # campaign name -> source_campaign
    assert campaign["ad_id"] == "123"
    assert data == {} and custom == {}


def test_questions_map_to_lead_columns():
    resolved = _default_resolved()
    data, campaign, custom = fbm.apply_resolved(
        resolved, {},
        {"phone_number": "98765 43210", "email": "a@b.com"},
    )
    assert data["mobile"] == "98765 43210"   # normalization happens later, in build_lead_request
    assert data["email"] == "a@b.com"


def test_unmapped_fields_are_preserved():
    resolved = _default_resolved()
    data, campaign, custom = fbm.apply_resolved(
        resolved,
        {"weird_attr": "keepme"},          # unmapped marketing -> campaign_data by name
        {"fav_color": "blue"},             # unmapped question -> custom_fields
    )
    assert campaign["weird_attr"] == "keepme"
    assert custom["fav_color"] == "blue"


def test_ignored_marketing_dropped_but_ignored_question_kept():
    resolved = _default_resolved()
    resolved[("marketing", "ad_name")] = {"target": None, "target_kind": "campaign_data", "is_active": True}
    resolved[("question", "city")] = {"target": None, "target_kind": "lead", "is_active": True}
    data, campaign, custom = fbm.apply_resolved(
        resolved,
        {"ad_name": "noisy", "ad_id": "1"},
        {"city": "Hassan"},
    )
    assert "ad_name" not in campaign          # ignored marketing -> dropped
    assert campaign["ad_id"] == "1"
    assert custom["city"] == "Hassan"         # ignored question -> answer still kept
    assert "city" not in data


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} fb-mapping unit tests passed")


if __name__ == "__main__":
    _run_all()
