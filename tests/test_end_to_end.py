"""Run the whole tool against the fake portal."""

import csv
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from fkepull.cli import run
from fkepull.errors import DataError, FKEError, LoginError, SchemaError
from tests.fake_portal import PASSWORD, USERNAME, FakePortal, PortalState

ROOT = Path(__file__).resolve().parent.parent
CONFIG = str(ROOT / "config.toml")


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("FKE_USER", USERNAME)
    monkeypatch.setenv("FKE_PASS", PASSWORD)
    monkeypatch.delenv("FKE_BASE_URL", raising=False)


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def invoke(portal: FakePortal, tmp_path: Path, *extra: str) -> int:
    return run(
        [
            "--base-url", portal.base_url,
            "--config", CONFIG,
            "--env-file", str(tmp_path / "nonexistent.env"),
            "--out-dir", str(tmp_path),
            "--cache-dir", str(tmp_path / "cache"),
            *extra,
        ]
    )


def test_full_run(tmp_path):
    with FakePortal() as portal:
        assert invoke(portal, tmp_path) == 0

        invoice = read_csv(tmp_path / "invoice.csv")
        details = read_csv(tmp_path / "details.csv")

        # --- the search was sent in US format ---------------------------
        sent = portal.state.date_ranges[0]
        assert sent == f"01/01/2000 - 12/31/{date.today().year + 2}"

        # --- never more than 5 requests at once -------------------------
        assert portal.state.max_concurrent <= 5
        assert portal.state.max_concurrent > 1, "detail pages should be fetched in parallel"

    header, *rows = invoice
    assert header == ["#", "Booking Ref", "Date", "Theme", "Fee", "20%"]

    *lines, total = rows
    assert len(lines) == 8  # the 8 completed bookings; the 2027 one is upcoming

    # --- sorted by event date ascending, dates shown UK style -----------
    assert [line[2] for line in lines] == [
        "14/06/2025", "03/07/2025", "21/11/2025", "05/12/2025",
        "09/01/2026", "14/02/2026", "02/03/2026", "28/03/2026",
    ]
    assert [line[0] for line in lines] == [str(n) for n in range(1, 9)]

    # --- themes come from the config mapping ----------------------------
    assert [line[3] for line in lines] == [
        "party", "party", "school disco", "glow", "spiderman", "batman",
        "fun toddler sensory session",  # unmapped: full name, and a warning
        "party",
    ]

    # --- the money --------------------------------------------------------
    fees_and_cuts = [(line[4], line[5]) for line in lines]
    assert fees_and_cuts == [
        ("150.00", "30"),
        ("120.50", "24"),
        ("240.00", "48"),
        ("197.50", "40"),   # 39.50 rounds up
        ("175.00", "35"),
        ("175.00", "35"),
        ("99.99", "20"),
        ("162.50", "33"),   # 32.50 rounds up, not to even
    ]
    assert total == ["TOTAL", "", "", "", "1320.49", "265"]

    # --- details.csv ------------------------------------------------------
    dheader, *drows = details
    assert dheader == [
        "Booking Ref", "Booking Confirmed Date", "Customer Name", "Phone",
        "Package", "Venue", "Party Occasion", "Duration",
    ]
    assert len(drows) == 8
    assert drows[0] == [
        "100-26501", "01/02/2026", "Jane Smith", "07700 900111",
        "FUN Kids Party (Indoor Use)", "12 Example Street", "Birthday", "1h 30m",
    ]
    assert drows[-1][0] == "100-26528"
    assert drows[-1][-1] == "2h"          # 2:00 PM to 4:00 PM
    assert drows[1][-1] == "1h"           # 11:00 to 12:00
    assert drows[6][3] == ""              # a missing phone is simply blank


def test_since_filters_and_narrows_the_search(tmp_path):
    with FakePortal() as portal:
        assert invoke(portal, tmp_path, "--since", "01/01/2026") == 0
        assert portal.state.date_ranges[0].startswith("01/01/2026 - ")

    rows = read_csv(tmp_path / "invoice.csv")[1:-1]
    assert [row[1] for row in rows] == ["100-26505", "100-26506", "100-26507", "100-26528"]


def test_upcoming_source(tmp_path):
    """The one future booking is dated 12/12/2027 — ambiguous on its own, so
    this also exercises the fallback that fetches a wider range to settle it."""
    with FakePortal() as portal:
        assert invoke(portal, tmp_path, "--source", "upcoming") == 0
        assert portal.state.date_ranges, "should have asked for more dates to disambiguate"
    rows = read_csv(tmp_path / "invoice.csv")[1:-1]
    assert [row[1] for row in rows] == ["100-26610"]
    assert rows[0][2] == "12/12/2027"


def test_both_sources_dedupe(tmp_path):
    with FakePortal() as portal:
        assert invoke(portal, tmp_path, "--source", "both") == 0
    rows = read_csv(tmp_path / "invoice.csv")[1:-1]
    assert len(rows) == 9
    assert len({row[1] for row in rows}) == 9


def test_already_invoiced_refs_are_excluded(tmp_path):
    refs = tmp_path / "invoiced.txt"
    refs.write_text("# already billed in the March invoice\n100-26501\n100-26502\n\n")
    with FakePortal() as portal:
        assert invoke(portal, tmp_path, "--invoiced-refs", str(refs)) == 0

    rows = read_csv(tmp_path / "invoice.csv")[1:]
    listed = [row[1] for row in rows[:-1]]
    assert "100-26501" not in listed and "100-26502" not in listed
    assert len(listed) == 6
    assert rows[-1] == ["TOTAL", "", "", "", "1049.99", "211"]


def test_already_invoiced_refs_can_be_marked_instead(tmp_path):
    refs = tmp_path / "invoiced.txt"
    refs.write_text("100-26501\n")
    with FakePortal() as portal:
        assert invoke(portal, tmp_path, "--invoiced-refs", str(refs), "--invoiced-mode", "mark") == 0

    header, *rows = read_csv(tmp_path / "invoice.csv")
    assert header[-1] == "Already Invoiced"
    assert rows[0][1] == "100-26501" and rows[0][-1] == "YES"
    assert rows[1][-1] == ""
    assert rows[-2][0] == "TOTAL (not yet invoiced)"
    assert rows[-2][4] == "1170.49"    # 1320.49 less the 150.00 already billed
    assert rows[-1][0] == "TOTAL (all rows)"
    assert rows[-1][4] == "1320.49"


def test_detail_pages_are_cached_between_runs(tmp_path):
    state = PortalState()
    with FakePortal(state) as portal:
        assert invoke(portal, tmp_path) == 0
        first = dict(state.detail_hits)
        assert set(first.values()) == {1}

        assert invoke(portal, tmp_path) == 0
        assert state.detail_hits == first, "second run should not re-fetch past parties"

        assert invoke(portal, tmp_path, "--refresh-cache") == 0
        assert set(state.detail_hits.values()) == {2}, "--refresh-cache should re-fetch"


def test_upcoming_bookings_are_not_cached_forever(tmp_path):
    state = PortalState()
    with FakePortal(state) as portal:
        assert invoke(portal, tmp_path, "--source", "upcoming") == 0
        assert state.detail_hits["100-26610"] == 1
        # cache.future_ttl_hours is 6, so within the same minute it is reused
        assert invoke(portal, tmp_path, "--source", "upcoming") == 0
        assert state.detail_hits["100-26610"] == 1

    cache_file = tmp_path / "cache" / "details" / "100-26610.json"
    assert cache_file.exists()
    import json
    payload = json.loads(cache_file.read_text())
    payload["fetched_at"] = "2020-01-01T00:00:00"
    cache_file.write_text(json.dumps(payload))

    with FakePortal(state) as portal:
        assert invoke(portal, tmp_path, "--source", "upcoming") == 0
        assert state.detail_hits["100-26610"] == 2, "a stale future booking should be re-fetched"


def test_no_cache_flag(tmp_path):
    state = PortalState()
    with FakePortal(state) as portal:
        assert invoke(portal, tmp_path, "--no-cache") == 0
        assert invoke(portal, tmp_path, "--no-cache") == 0
    assert set(state.detail_hits.values()) == {2}


# --- the failure modes that matter ------------------------------------------

def test_bad_password_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("FKE_PASS", "wrong")
    with FakePortal() as portal:
        with pytest.raises(LoginError) as exc:
            invoke(portal, tmp_path)
    assert "Invalid login attempt" in str(exc.value)
    assert not (tmp_path / "invoice.csv").exists()


def test_changed_detail_layout_stops_the_run(tmp_path):
    state = PortalState(break_detail_layout=True)
    with FakePortal(state) as portal:
        with pytest.raises(SchemaError) as exc:
            invoke(portal, tmp_path)
    assert "balance" in str(exc.value)
    assert not (tmp_path / "invoice.csv").exists(), "no CSV should be written on a schema break"


def test_detail_page_for_the_wrong_booking_is_caught(tmp_path):
    """The ref -> detail id rule is an assumption. If it stops holding, stop."""
    state = PortalState(mislabel_details=True)
    with FakePortal(state) as portal:
        with pytest.raises(DataError) as exc:
            invoke(portal, tmp_path)
    assert "no longer holds" in str(exc.value)
    assert not (tmp_path / "invoice.csv").exists()


def test_wrong_detail_id_offset_is_caught(tmp_path):
    """A bad offset points at ids that don't exist — that must be loud too."""
    config = tmp_path / "offset.toml"
    config.write_text(
        (ROOT / "config.toml").read_text().replace(
            "detail_id_offset = 10000", "detail_id_offset = 9999"
        )
    )
    with FakePortal() as portal:
        with pytest.raises(FKEError) as exc:
            run([
                "--base-url", portal.base_url,
                "--config", str(config),
                "--env-file", str(tmp_path / "none.env"),
                "--out-dir", str(tmp_path),
                "--cache-dir", str(tmp_path / "cache"),
            ])
    assert "/Bookings/Details/" in str(exc.value)
    assert not (tmp_path / "invoice.csv").exists()


def test_blank_balance_stops_the_run_unless_allowed(tmp_path):
    state = PortalState()
    state.bookings = state.bookings[:3]
    state.bookings[1] = replace(state.bookings[1], balance="")
    with FakePortal(state) as portal:
        with pytest.raises(DataError) as exc:
            invoke(portal, tmp_path)
        assert "100-26502" in str(exc.value)
        assert "--allow-blank-fees" in str(exc.value)

        assert invoke(portal, tmp_path, "--allow-blank-fees") == 0
        rows = read_csv(tmp_path / "invoice.csv")[1:-1]
        assert rows[1][4] == "" and rows[1][5] == ""


def test_ambiguous_dates_stop_the_run(tmp_path):
    """Every date in the batch readable both ways -> refuse to guess."""
    state = PortalState()
    state.bookings = [
        b for b in state.bookings if int(b.event_date.split("/")[0]) <= 12
    ][:2]
    with FakePortal(state) as portal:
        with pytest.raises(DataError) as exc:
            invoke(portal, tmp_path)
    assert "day/month or month/day" in str(exc.value)
    assert "display_date_format" in str(exc.value)


def test_ambiguous_dates_are_fine_once_configured(tmp_path):
    config = tmp_path / "dmy.toml"
    config.write_text(
        (ROOT / "config.toml").read_text().replace(
            'display_date_format = "auto"', 'display_date_format = "DMY"'
        )
    )
    state = PortalState()
    state.bookings = [
        b for b in state.bookings if int(b.event_date.split("/")[0]) <= 12
    ][:2]
    with FakePortal(state) as portal:
        assert run([
            "--base-url", portal.base_url,
            "--config", str(config),
            "--env-file", str(tmp_path / "none.env"),
            "--out-dir", str(tmp_path),
            "--cache-dir", str(tmp_path / "cache"),
        ]) == 0
    rows = read_csv(tmp_path / "invoice.csv")[1:-1]
    assert rows[0][2] == "03/07/2025"


def test_concurrency_is_capped_at_five(tmp_path):
    state = PortalState(detail_delay=0.1)
    with FakePortal(state) as portal:
        assert invoke(portal, tmp_path, "--max-concurrency", "50") == 0
    assert state.max_concurrent <= 5
