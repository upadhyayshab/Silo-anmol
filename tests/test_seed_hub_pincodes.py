import os, sys, csv, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "seed"))
from seed_hub_pincodes import read_rows, match_hub, apply_overrides, PINCODE_OVERRIDES


def _csv(rows):
    fd, path = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["District", "State", "pincode", "Final Hub"])
        w.writerows(rows)
    return path


def test_read_rows_filters_dedupes_lowercases():
    path = _csv([
        ["Bapatla", "Andhra Pradesh", "523201", "Addanki"],
        ["Bapatla", "Andhra Pradesh", "523201", "Addanki"],   # dup pin -> first wins
        ["Nalgonda", "Telangana", "508248", "Uncovered"],      # kept here; main skips
        ["Bad", "X", "12", "Foo"],                             # not 6-digit -> dropped
        ["Krishna", "Andhra Pradesh", " 521301 ", "Gudivada"], # spaces stripped
    ])
    rows = read_rows(path); os.remove(path)
    assert [r["pincode"] for r in rows] == ["523201", "508248", "521301"]
    assert rows[0]["state"] == "andhra pradesh"
    assert rows[0]["district"] == "bapatla"
    assert rows[0]["hub"] == "Addanki"          # hub preserved raw for matching
    assert rows[1]["hub"] == "Uncovered"


def test_match_hub_strict_no_false_positive():
    outlets = [
        {"uid": "1", "outlet_name": "Belagavi Outlet"},
        {"uid": "2", "outlet_name": "Addanki Outlet"},
        {"uid": "3", "outlet_name": "Samalkota Outlet"},
        {"uid": "4", "outlet_name": "Chittor Rural Outlet"},
        {"uid": "5", "outlet_name": "Tadepalligudem"},  # no suffix
    ]
    assert match_hub(outlets, "Addanki")["uid"] == "2"        # exact base
    assert match_hub(outlets, "Tadepalligudem")["uid"] == "5" # no-suffix outlet
    assert match_hub(outlets, "Samalkot")["uid"] == "3"       # alias
    assert match_hub(outlets, "Chittoor Rural")["uid"] == "4" # alias
    assert match_hub(outlets, "Bela") is None                 # NOT Belagavi (the bug)
    assert match_hub(outlets, "Uncovered") is None


def test_apply_overrides_rewrites_and_appends():
    rows = [
        {"pincode": "503111", "state": "telangana", "district": "kamareddy", "hub": "Bodhan"},
        {"pincode": "509301", "state": "telangana", "district": "x", "hub": "Uncovered"},
        {"pincode": "999999", "state": "telangana", "district": "y", "hub": "Gajwel"},
    ]
    out = apply_overrides(rows)
    by = {r["pincode"]: r["hub"] for r in out}
    assert by["503111"] == "Kamareddy"       # rewritten off Bodhan
    assert by["509301"] == "Jadcherla"       # rewritten off Uncovered
    assert by["999999"] == "Gajwel"          # untouched
    assert by["516712"] == "Mydukur"         # appended (absent from sheet)
    assert len(out) == 3 + 2                 # 516712 + 505467 appended
    assert set(PINCODE_OVERRIDES) == {"503111", "505467", "509301", "516712"}


if __name__ == "__main__":
    test_read_rows_filters_dedupes_lowercases()
    test_match_hub_strict_no_false_positive()
    test_apply_overrides_rewrites_and_appends()
    print("PASS")
