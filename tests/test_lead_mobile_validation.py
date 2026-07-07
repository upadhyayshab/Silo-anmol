"""Lead mobile must normalize to exactly 10 digits so telecallers can dial it."""
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from models.leadModels import LeadCreateRequest, LeadUpdateRequest


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "9876543210"),
    ("+91 98765-43210", "9876543210"),
    ("098765 43210", "9876543210"),
])
def test_accepts_and_normalizes(raw, expected):
    assert LeadCreateRequest(first_name="A", mobile=raw).mobile == expected


@pytest.mark.parametrize("bad", ["12345", "12345678901", "98765abcde", ""])
def test_rejects_non_10_digit(bad):
    with pytest.raises(ValidationError):
        LeadCreateRequest(first_name="A", mobile=bad)


def test_update_optional_but_validated():
    assert LeadUpdateRequest().mobile is None
    assert LeadUpdateRequest(mobile="+919876543210").mobile == "9876543210"
    with pytest.raises(ValidationError):
        LeadUpdateRequest(mobile="123")
