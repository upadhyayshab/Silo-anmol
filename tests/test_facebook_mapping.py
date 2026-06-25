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


def test_relabel_custom_fields_falls_back_to_raw_key():
    # Kannada question key gets its English label; untranslated one keeps its key.
    labels = {"budget_q": "Budget"}
    out = fbm.relabel_custom_fields({"budget_q": "50000", "cattle_q": "12"}, labels)
    assert out == {"Budget": "50000", "cattle_q": "12"}
    # No labels / empty custom_fields -> unchanged (nothing vanishes).
    assert fbm.relabel_custom_fields({"a": "1"}, {}) == {"a": "1"}
    assert fbm.relabel_custom_fields(None, labels) is None


def test_custom_bound_only_for_non_column_questions():
    resolved = {
        ("question", "phone_number"): {"target": "mobile", "target_kind": "lead", "is_active": True},
        ("question", "budget_q"): {"target": "budget_q", "target_kind": "custom", "is_active": True},
    }
    assert fbm.custom_bound(resolved, "phone_number") is False   # real column -> no label needed
    assert fbm.custom_bound(resolved, "budget_q") is True        # custom -> needs a label
    assert fbm.custom_bound(resolved, "never_seen") is True      # unmapped -> custom by default


def test_merge_questions_flags_untranslated_as_pending():
    form_questions = [
        {"name": "budget_q", "type": "CUSTOM", "label": "ನಿಮ್ಮ ಬಜೆಟ್?"},
        {"name": "cattle_q", "type": "CUSTOM", "label": "ಎಷ್ಟು ಹಸುಗಳಿವೆ?"},
    ]
    mapped = {"budget_q": {"meta_field": "budget_q", "field_kind": "question",
                           "label": "Budget", "target": "budget_q",
                           "target_kind": "custom", "is_active": True}}
    out = {q["meta_field"]: q for q in fbm.merge_questions(form_questions, mapped)}
    assert out["budget_q"]["pending"] is False                   # has English label
    assert out["budget_q"]["label_raw"] == "ನಿಮ್ಮ ಬಜೆಟ್?"
    assert out["cattle_q"]["pending"] is True                    # no label yet -> flagged
    assert out["cattle_q"]["label"] is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} fb-mapping unit tests passed")


if __name__ == "__main__":
    _run_all()
