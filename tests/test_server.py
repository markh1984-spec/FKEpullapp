"""The web app, driven over HTTP against the fake portal."""

import csv
import io
import threading

import pytest
import requests

from fkepull.cli import build_parser, load_cfg, options_from
from fkepull.server import create_server
from tests.fake_portal import PASSWORD, USERNAME, FakePortal, PortalState

ROOT_CONFIG = "config.toml"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("FKE_USER", USERNAME)
    monkeypatch.setenv("FKE_PASS", PASSWORD)
    monkeypatch.delenv("FKE_BASE_URL", raising=False)


class RunningApp:
    def __init__(self, portal, tmp_path, extra=()):
        args = build_parser().parse_args([
            "--serve",
            "--base-url", portal.base_url,
            "--config", ROOT_CONFIG,
            "--env-file", str(tmp_path / "none.env"),
            "--cache-dir", str(tmp_path / "cache"),
            *extra,
        ])
        cfg = load_cfg(args)
        self.server, self.state, self.url = create_server(
            args, cfg, options_from(args), port=0
        )
        self.base = self.url.split("/?")[0]
        self.token = self.state.token

    def __enter__(self):
        self.state.refresh()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path, **kwargs):
        return requests.get(self.base + path, headers={"X-FKE-Token": self.token},
                            timeout=10, **kwargs)

    def post(self, path, **kwargs):
        return requests.post(self.base + path, headers={"X-FKE-Token": self.token},
                             timeout=30, **kwargs)


def test_page_and_data_load(tmp_path):
    with FakePortal() as portal, RunningApp(portal, tmp_path) as app:
        page = app.get("/")
        assert page.status_code == 200
        assert "FKE bookings" in page.text
        assert "__FKE_TOKEN__" not in page.text, "the token placeholder should be filled in"
        assert app.token in page.text

        data = app.get("/api/data").json()

    assert data["error"] is None
    assert data["percent"] == 20

    # --- two tabs, past and upcoming, each with its own total -------------
    assert data["past"]["count"] == 8
    assert data["past"]["total_fee"] == "1320.49"
    assert data["past"]["total_cut"] == "264.10"

    assert data["upcoming"]["count"] == 1
    assert data["upcoming"]["total_fee"] == "200.00"
    assert data["upcoming"]["total_cut"] == "40.00", "upcoming 20% ready for next time"

    first = data["past"]["rows"][0]
    assert first["ref"] == "100-26501"
    assert first["date"] == "14/06/2025"
    assert first["customer"] == "Jane Smith"
    assert first["theme"] == "party"
    assert first["venue"] == "12 Example Street"
    assert first["duration"] == "1h 30m"
    assert (first["fee"], first["cut"]) == ("150.00", "30.00")

    assert data["unknown_packages"] == ["FUN Toddler Sensory Session"]


def test_downloads(tmp_path):
    with FakePortal() as portal, RunningApp(portal, tmp_path) as app:
        invoice = app.get("/download/invoice.csv")
        upcoming = app.get("/download/upcoming.csv")
        details = app.get("/download/details.csv")

    assert invoice.headers["Content-Disposition"] == 'attachment; filename="invoice.csv"'
    rows = list(csv.reader(io.StringIO(invoice.content.decode("utf-8-sig"))))
    assert rows[0] == ["#", "Booking Ref", "Date", "Theme", "Fee", "20%"]
    assert rows[-1] == ["TOTAL", "", "", "", "1320.49", "264.10"]
    assert len(rows) == 10

    up_rows = list(csv.reader(io.StringIO(upcoming.content.decode("utf-8-sig"))))
    assert [r[1] for r in up_rows[1:-1]] == ["100-26610"]
    assert up_rows[-1] == ["TOTAL", "", "", "", "200.00", "40.00"]

    detail_rows = list(csv.reader(io.StringIO(details.content.decode("utf-8-sig"))))
    assert detail_rows[0][0] == "Booking Ref"
    assert len(detail_rows) == 10  # header + 8 past + 1 upcoming


def test_refresh_repulls(tmp_path):
    state = PortalState()
    with FakePortal(state) as portal, RunningApp(portal, tmp_path) as app:
        before = sum(1 for line in state.requests if line.startswith("POST /Account/Login"))
        data = app.post("/api/refresh").json()
        after = sum(1 for line in state.requests if line.startswith("POST /Account/Login"))

        assert after == before + 1, "refresh should log in and pull again"
        assert data["error"] is None, "a refresh from the browser must actually work"
        assert data["pulled_at"] is not None
        assert data["past"]["count"] == 8
        assert set(state.detail_hits.values()) == {1}, "cached detail pages are reused"

        hard = app.post("/api/refresh?hard=1").json()
        assert hard["error"] is None
        assert set(state.detail_hits.values()) == {2}, "full refresh re-reads detail pages"


def test_invoiced_refs_are_marked_in_the_app(tmp_path):
    refs = tmp_path / "invoiced.txt"
    refs.write_text("100-26501\n")
    with FakePortal() as portal, RunningApp(
        portal, tmp_path, ["--invoiced-refs", str(refs), "--invoiced-mode", "mark"]
    ) as app:
        data = app.get("/api/data").json()

    rows = {row["ref"]: row for row in data["past"]["rows"]}
    assert rows["100-26501"]["invoiced"] is True
    assert rows["100-26502"]["invoiced"] is False


def test_a_broken_portal_shows_an_error_instead_of_crashing(tmp_path):
    state = PortalState(break_detail_layout=True)
    with FakePortal(state) as portal, RunningApp(portal, tmp_path) as app:
        data = app.get("/api/data").json()
        assert data["error"] is not None
        assert "balance" in data["error"]
        assert data["past"]["count"] == 0
        # and the app is still up and answering
        assert app.get("/").status_code == 200


# --- it must not be reachable by anything else -------------------------------

def test_requests_without_the_token_are_refused(tmp_path):
    with FakePortal() as portal, RunningApp(portal, tmp_path) as app:
        assert requests.get(app.base + "/", timeout=10).status_code == 403
        assert requests.get(app.base + "/api/data", timeout=10).status_code == 403
        assert requests.get(app.base + "/api/data?t=guess", timeout=10).status_code == 403
        assert requests.post(app.base + "/api/refresh", timeout=10).status_code == 403
        assert requests.get(app.base + "/download/invoice.csv", timeout=10).status_code == 403


def test_requests_claiming_another_host_are_refused(tmp_path):
    """Blunts DNS rebinding: a domain pointed at 127.0.0.1 still can't get in."""
    with FakePortal() as portal, RunningApp(portal, tmp_path) as app:
        response = requests.get(
            app.base + "/api/data",
            headers={"X-FKE-Token": app.token, "Host": "evil.example.com"},
            timeout=10,
        )
    assert response.status_code == 403


def test_it_listens_on_loopback_only(tmp_path):
    with FakePortal() as portal:
        app = RunningApp(portal, tmp_path)
        try:
            assert app.server.server_address[0] == "127.0.0.1"
        finally:
            app.server.server_close()
