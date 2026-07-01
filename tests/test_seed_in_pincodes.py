import os, sys, csv, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "seed"))
from seed_in_pincodes import read_rows


def _write_csv(rows):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Pincode", "State", "District", "Taluk"])  # skipped by read_rows; names irrelevant
        w.writerows(rows)
    return path


def test_read_rows_parses_and_filters():
    path = _write_csv([
        ["744301", "Andaman & Nicobar Islands", "Nicobar", "Carnicobar"],
        ["744301", "Andaman & Nicobar Islands", "Nicobar", ""],   # empty taluk -> None
        ["12", "Bad", "Row", "Skip"],                              # too short -> skipped
        ["abcdef", "Bad", "Row", "Skip"],                          # non-digit -> skipped
    ])
    rows = read_rows(path)
    os.remove(path)
    assert len(rows) == 2
    assert rows[0]["pincode"] == "744301"
    # Stored lowercase-canonical (display layers title-case it back).
    assert rows[0]["state"] == "andaman & nicobar islands"
    assert rows[0]["district"] == "nicobar"
    assert rows[0]["taluk"] == "carnicobar"
    assert rows[1]["taluk"] is None
    assert rows[0]["uid"].startswith("pincodes_")


if __name__ == "__main__":
    test_read_rows_parses_and_filters()
    print("PASS")
