import base64
import csv
import io
import re
import sqlite3

import pytest

from app import create_app, get_db

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(app, username):
    client = app.test_client()
    response = client.post(
        "/login", data={"username": username, "password": "demo1234"}
    )
    assert response.status_code in (302, 303)
    return client


def card_id(app, card_no):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM cards WHERE card_no = ?", (card_no,)
        ).fetchone()
        return row["id"]


def get_export(app):
    reviewer = login(app, "reviewer")
    response = reviewer.get("/reviewer/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8"))))
    return response, rows


def review_form(app, card_no):
    reviewer = login(app, "reviewer")
    cid = card_id(app, card_no)
    page = reviewer.get(f"/reviewer/cards/{cid}").data.decode("utf-8")
    fields = dict(
        re.findall(
            r'name="(entry_[ab]_(?:id|revision)|image_version)" value="(\d+)"',
            page,
        )
    )
    assert len(fields) == 5
    return reviewer, cid, fields, page


def test_demo_login_and_roles(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/clerk").status_code == 200
    assert clerk.get("/reviewer").status_code == 403

    reviewer = login(app, "reviewer")
    assert reviewer.get("/reviewer").status_code == 200
    assert reviewer.get("/clerk").status_code == 403

    anonymous = app.test_client()
    assert anonymous.get("/clerk").status_code == 302


def test_clerk_cannot_see_peer_entry(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0715")
    page = clerk.get(f"/clerk/cards/{cid}").data.decode("utf-8")
    # The clerk sees their own 31.0 reading, but not clerk2's 13.0 reading.
    assert "31" in page
    assert ">13<" not in page
    assert "13 mm" not in page
    assert "录入员乙" not in page


def test_two_agreeing_entries_are_auto_approved(app):
    c1 = login(app, "clerk1")
    c2 = login(app, "clerk2")
    cid = card_id(app, "YC-1986-0714")
    for client, value in ((c1, "42.5"), (c2, "42.50")):
        response = client.post(
            f"/clerk/cards/{cid}",
            data={
                "image_version": "1",
                "station_no": "30501200",
                "obs_date": "1986-07-14",
                "rainfall_mm": value,
            },
        )
        assert response.status_code == 302

    _, rows = get_export(app)
    matching = [r for r in rows if r["card_no"] == "YC-1986-0714"]
    assert len(matching) == 1
    assert matching[0]["station_no"] == "30501200"
    assert matching[0]["observation_date"] == "1986-07-14"
    assert matching[0]["rainfall_mm"] == "42.5"
    assert matching[0]["verification"] == "double_entry_agreement"
    # One row per approved card.
    assert len({r["card_no"] for r in rows}) == len(rows)


def test_conflict_requires_review_and_uses_selected_version(app):
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    reviewer, cid, fields, page = review_form(app, "YC-1986-0715")
    assert "31" in page and "13" in page
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 302

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "31"
    assert match[0]["verification"] == "reviewer_adjudication"

    # Any later save (even textually identical) revokes the award.
    c2 = login(app, "clerk2")
    response = c2.post(
        f"/clerk/cards/{cid}",
        data={
            "image_version": "1",
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "13.0",
        },
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ?", (cid,)
        ).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["voided_at"] is not None
    assert decisions[0]["voided_reason"] == "entry_edited"


def test_stale_adjudication_is_refused(app):
    reviewer, cid, old_fields, _ = review_form(app, "YC-1986-0715")
    clerk2 = login(app, "clerk2")
    clerk2.post(
        f"/clerk/cards/{cid}",
        data={
            "image_version": "1",
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "14",
        },
    )

    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**old_fields, "selected_entry_id": old_fields["entry_a_id"]},
    )
    assert response.status_code == 409
    assert "旧裁定已拒绝" in response.data.decode("utf-8")

    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,)
        ).fetchone()[0]
    assert count == 0

    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}


def test_resolution_can_be_redone_after_edit(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data={
            "image_version": "1",
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "12.5",
        },
    )

    reviewer2, cid2, fresh, _ = review_form(app, "YC-1986-0715")
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    response = reviewer2.post(
        f"/reviewer/cards/{cid2}/decide",
        data={**fresh, "selected_entry_id": fresh["entry_b_id"]},
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "12.5"


def test_agreement_after_edit_wins_and_needs_no_adjudication(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data={
            "image_version": "1",
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31.0",
        },
    )

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["verification"] == "double_entry_agreement"
    assert match[0]["rainfall_mm"] == "31"
    # A decision against the pre-edit versions cannot be added now either.
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 409


def test_upload_creates_card_and_rejects_non_image(app):
    reviewer = login(app, "reviewer")
    response = reviewer.post(
        "/reviewer/upload",
        data={
            "card_no": "UPLOAD-1",
            "clerk_a_id": "1",
            "clerk_b_id": "2",
            "scan": (io.BytesIO(PNG_1PX), "card.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    cid = card_id(app, "UPLOAD-1")
    assert reviewer.get(f"/cards/{cid}/image").status_code == 200
    assert reviewer.get(f"/reviewer/cards/{cid}").status_code == 200

    response = reviewer.post(
        "/reviewer/upload",
        data={
            "card_no": "BAD-1",
            "clerk_a_id": "1",
            "clerk_b_id": "2",
            "scan": (io.BytesIO(b"<svg><script>alert(1)</script></svg>"), "bad.svg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM cards WHERE card_no = 'BAD-1'")
            .fetchone()[0]
            == 0
        )


def test_rainfall_and_date_validation(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0714")
    base = {"image_version": "1", "station_no": "30501200", "obs_date": "1986-07-14"}
    for value in ("-1", "abc", "", "NaN", "100001"):
        response = clerk.post(
            f"/clerk/cards/{cid}",
            data={**base, "rainfall_mm": value},
        )
        assert response.status_code == 200
        assert "雨量" in response.data.decode("utf-8")
    response = clerk.post(
        f"/clerk/cards/{cid}",
        data={"image_version": "1", "station_no": "30501200", "obs_date": "not-a-date", "rainfall_mm": "4"},
    )
    assert response.status_code == 200
    with app.app_context():
        entry = get_db().execute(
            "SELECT * FROM entries WHERE card_id = ? AND clerk_id = 1", (cid,)
        ).fetchone()
    assert entry is None


def test_csv_is_reviewer_only(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/reviewer/export.csv").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/reviewer/export.csv").status_code == 302


def test_decision_requires_both_entries_and_current_versions(app):
    reviewer = login(app, "reviewer")
    cid = card_id(app, "YC-1986-0717")  # only clerk1 has entered
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={
            "image_version": "1",
            "entry_a_id": "1",
            "entry_a_revision": "1",
            "entry_b_id": "2",
            "entry_b_revision": "1",
            "selected_entry_id": "1",
        },
    )
    assert response.status_code == 409
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,))
            .fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------------------
# Scan swap workflow (wrongly mounted rain cards)
# ---------------------------------------------------------------------------


def _card_row(app, card_no):
    with app.app_context():
        return dict(
            get_db()
            .execute("SELECT * FROM cards WHERE card_no = ?", (card_no,))
            .fetchone()
        )


def test_seed_data_has_the_two_scans_mounted_on_wrong_cards(app):
    c715 = _card_row(app, "YC-1986-0715")
    c716 = _card_row(app, "YC-1986-0716")
    assert c715["image_filename"] == "sample:rain-card-003.svg"
    assert c716["image_filename"] == "sample:rain-card-002.svg"
    assert c715["image_version"] == 1 and c716["image_version"] == 1


def test_swap_requires_exactly_two_distinct_cards(app):
    reviewer = login(app, "reviewer")
    c1 = card_id(app, "YC-1986-0715")
    c2 = card_id(app, "YC-1986-0716")

    # GET confirm page renders both current scans.
    page = reviewer.get(f"/reviewer/swap?card_id={c1}&card_id={c2}")
    assert page.status_code == 200
    body = page.data.decode("utf-8")
    assert "YC-1986-0715" in body and "YC-1986-0716" in body

    # One card selected -> bounced back to the desk, nothing written.
    resp = reviewer.get(f"/reviewer/swap?card_id={c1}", follow_redirects=True)
    assert resp.status_code == 200
    assert "请勾选两张卡片" in resp.data.decode("utf-8")

    # Same card twice -> rejected.
    resp = reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(c1),
            "card_b_id": str(c1),
            "card_a_version": "1",
            "card_b_version": "1",
        },
        follow_redirects=True,
    )
    assert "两张不同的卡片" in resp.data.decode("utf-8")
    assert _card_row(app, "YC-1986-0715")["image_version"] == 1


def test_swap_exchanges_images_retires_entries_and_voids_decisions(app):
    reviewer = login(app, "reviewer")
    cid_715 = card_id(app, "YC-1986-0715")
    cid_716 = card_id(app, "YC-1986-0716")
    before_715 = _card_row(app, "YC-1986-0715")
    before_716 = _card_row(app, "YC-1986-0716")

    # First adjudicate 0715 so the swap also has a live decision to void.
    _, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    resp = reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(cid_715),
            "card_b_id": str(cid_716),
            "card_a_version": "1",
            "card_b_version": "1",
        },
    )
    assert resp.status_code == 302

    after_715 = _card_row(app, "YC-1986-0715")
    after_716 = _card_row(app, "YC-1986-0716")

    # Images exchanged; card numbers untouched.
    assert after_715["image_filename"] == before_716["image_filename"]
    assert after_716["image_filename"] == before_715["image_filename"]
    assert after_715["image_version"] == 2
    assert after_716["image_version"] == 2
    assert after_715["image_updated_at"] is not None

    with app.app_context():
        db = get_db()
        # Old entries are retained but none belong to the new image version.
        for cid_ in (cid_715, cid_716):
            live = db.execute(
                "SELECT COUNT(*) FROM entries WHERE card_id = ? AND image_version = 2",
                (cid_,),
            ).fetchone()[0]
            retired = db.execute(
                "SELECT COUNT(*) FROM entries WHERE card_id = ? AND image_version = 1",
                (cid_,),
            ).fetchone()[0]
            assert live == 0
            assert retired == 2
            # Every previously live decision is now void with the swap reason.
            voided = db.execute(
                """SELECT COUNT(*) FROM decisions
                   WHERE card_id = ? AND voided_reason = 'image_swapped'
                     AND voided_at IS NOT NULL""",
                (cid_,),
            ).fetchone()[0]
            assert voided == (1 if cid_ == cid_715 else 0)
            active = db.execute(
                "SELECT COUNT(*) FROM decisions WHERE card_id = ? AND voided_at IS NULL",
                (cid_,),
            ).fetchone()[0]
            assert active == 0
        # The audit row records the exact exchange.
        swap = db.execute("SELECT * FROM image_swaps ORDER BY id DESC LIMIT 1").fetchone()
        assert swap["card_a_id"] == cid_715 and swap["card_b_id"] == cid_716
        assert swap["card_a_version_after"] == 2

    # Both cards dropped out of the verified CSV.
    _, rows = get_export(app)
    exported = {r["card_no"] for r in rows}
    assert "YC-1986-0715" not in exported
    assert "YC-1986-0716" not in exported

    # The image endpoint now serves the exchanged scan byte-for-byte.
    img = reviewer.get(f"/cards/{cid_715}/image")
    assert img.status_code == 200
    assert b"rain-card-002" in img.data or b"YC-1986-0715" in img.data


def test_entry_form_is_blank_after_swap_and_refuses_stale_submission(app):
    reviewer = login(app, "reviewer")
    cid_715 = card_id(app, "YC-1986-0715")
    cid_716 = card_id(app, "YC-1986-0716")
    reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(cid_715),
            "card_b_id": str(cid_716),
            "card_a_version": "1",
            "card_b_version": "1",
        },
    )

    clerk1 = login(app, "clerk1")
    page = clerk1.get(f"/clerk/cards/{cid_715}").data.decode("utf-8")
    # No pre-fill of the retired reading (31) and the page binds version 2.
    assert 'name="image_version" value="2"' in page
    assert 'value="31"' not in page
    assert "退回待录" in page

    # A stale tab still holding version 1 is rejected without writing.
    stale = clerk1.post(
        f"/clerk/cards/{cid_715}",
        data={
            "image_version": "1",
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "999",
        },
    )
    assert stale.status_code == 409
    stale_page = stale.data.decode("utf-8")
    assert "刷新" in stale_page
    # The rejected page already renders the new image version, so refreshing
    # (or simply re-submitting this rendered form) cannot write to v1.
    assert 'name="image_version" value="2"' in stale_page
    with app.app_context():
        assert (
            get_db()
            .execute(
                "SELECT COUNT(*) FROM entries WHERE card_id = ? AND image_version = 2",
                (cid_715,),
            )
            .fetchone()[0]
            == 0
        )

    # Re-entering against the new image version works and starts revision 1.
    ok = clerk1.post(
        f"/clerk/cards/{cid_715}",
        data={
            "image_version": "2",
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31.0",
        },
    )
    assert ok.status_code == 302
    with app.app_context():
        row = get_db().execute(
            "SELECT * FROM entries WHERE card_id = ? AND image_version = 2",
            (cid_715,),
        ).fetchone()
    assert row["revision"] == 1 and row["rainfall_mm"] == "31"


def test_stale_adjudication_after_swap_is_refused(app):
    reviewer = login(app, "reviewer")
    cid_715 = card_id(app, "YC-1986-0715")
    cid_716 = card_id(app, "YC-1986-0716")

    # Open the conflict page, then swap the scans while the tab stays open.
    old_page = reviewer.get(f"/reviewer/cards/{cid_715}").data.decode("utf-8")
    old_fields = dict(
        re.findall(
            r'name="(entry_[ab]_(?:id|revision)|image_version)" value="(\d+)"',
            old_page,
        )
    )
    reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(cid_715),
            "card_b_id": str(cid_716),
            "card_a_version": "1",
            "card_b_version": "1",
        },
    )

    stale = reviewer.post(
        f"/reviewer/cards/{cid_715}/decide",
        data={**old_fields, "selected_entry_id": old_fields["entry_a_id"]},
    )
    assert stale.status_code == 409
    stale_page = stale.data.decode("utf-8")
    assert "刷新" in stale_page
    assert "第 2 版" in stale_page
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM decisions WHERE card_id = ? AND voided_at IS NULL", (cid_715,))
            .fetchone()[0]
            == 0
        )


def test_full_re_entry_and_re_adjudication_returns_card_to_csv(app):
    reviewer = login(app, "reviewer")
    cid_715 = card_id(app, "YC-1986-0715")
    cid_716 = card_id(app, "YC-1986-0716")
    reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(cid_715),
            "card_b_id": str(cid_716),
            "card_a_version": "1",
            "card_b_version": "1",
        },
    )

    # Both clerks re-transcribe the exchanged card 002 (rainfall 31.0).
    c1 = login(app, "clerk1")
    c2 = login(app, "clerk2")
    payload_a = {
        "image_version": "2",
        "station_no": "30501210",
        "obs_date": "1986-07-15",
        "rainfall_mm": "31",
    }
    payload_b = {**payload_a, "rainfall_mm": "13"}
    assert c1.post(f"/clerk/cards/{cid_715}", data=payload_a).status_code == 302
    assert c2.post(f"/clerk/cards/{cid_715}", data=payload_b).status_code == 302

    # Conflict under the new image version; the reviewer re-adjudicates.
    reviewer2, cid2, fields, page = review_form(app, "YC-1986-0715")
    assert fields["image_version"] == "2"
    assert "31" in page and "13" in page
    resp = reviewer2.post(
        f"/reviewer/cards/{cid2}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert resp.status_code == 302

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "31"
    assert match[0]["verification"] == "reviewer_adjudication"

    # The audit trail only holds the new decision bound to the swapped image.
    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ? ORDER BY id", (cid_715,)
        ).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["voided_at"] is None
    assert decisions[0]["image_version"] == 2


def test_stale_swap_confirmation_is_refused(app):
    reviewer = login(app, "reviewer")
    cid_715 = card_id(app, "YC-1986-0715")
    cid_716 = card_id(app, "YC-1986-0716")

    # A second swap pair changes 0716's version first... simulate by swapping
    # 0716 with 0714, then trying to confirm the stale 0715/0716 form.
    cid_714 = card_id(app, "YC-1986-0714")
    reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(cid_716),
            "card_b_id": str(cid_714),
            "card_a_version": "1",
            "card_b_version": "1",
        },
    )
    resp = reviewer.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(cid_715),
            "card_b_id": str(cid_716),
            "card_a_version": "1",  # stale for 0716, which is now version 2
            "card_b_version": "1",
        },
    )
    assert resp.status_code == 409
    assert "刷新" in resp.data.decode("utf-8")
    # The stale swap did not touch 0715.
    assert _card_row(app, "YC-1986-0715")["image_version"] == 1


def test_clerk_cannot_swap(app):
    clerk = login(app, "clerk1")
    c1 = card_id(app, "YC-1986-0715")
    c2 = card_id(app, "YC-1986-0716")
    assert clerk.get("/reviewer/swap").status_code == 403
    resp = clerk.post(
        "/reviewer/swap",
        data={
            "card_a_id": str(c1),
            "card_b_id": str(c2),
            "card_a_version": "1",
            "card_b_version": "1",
        },
    )
    assert resp.status_code == 403


def test_legacy_database_is_migrated_and_keeps_its_history(tmp_path):
    """A volume created by the pre-swap schema upgrades without losing data."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
            role TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, card_no TEXT NOT NULL UNIQUE,
            image_filename TEXT NOT NULL,
            clerk_a_id INTEGER NOT NULL, clerk_b_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL, clerk_id INTEGER NOT NULL,
            station_no TEXT NOT NULL, obs_date TEXT NOT NULL,
            rainfall_mm TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (card_id, clerk_id));
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL, entry_a_id INTEGER NOT NULL,
            entry_a_revision INTEGER NOT NULL, entry_b_id INTEGER NOT NULL,
            entry_b_revision INTEGER NOT NULL, selected_entry_id INTEGER NOT NULL,
            selected_entry_revision INTEGER NOT NULL,
            verified_station_no TEXT NOT NULL, verified_obs_date TEXT NOT NULL,
            verified_rainfall_mm TEXT NOT NULL, created_at TEXT NOT NULL,
            voided_at TEXT, voided_reason TEXT, voided_by_entry_id INTEGER);
        INSERT INTO users VALUES (1,'r','h','复核员','reviewer','t'),
                                 (2,'a','h','录入员甲','clerk','t'),
                                 (3,'b','h','录入员乙','clerk','t');
        INSERT INTO cards VALUES (1,'OLD-1','sample:x.svg',2,3,1,'t');
        INSERT INTO entries VALUES (1,1,2,'S','1986-07-15','31',1,'t','t'),
                                   (2,1,3,'S','1986-07-15','13',1,'t','t');
        """
    )
    legacy.commit()
    legacy.close()

    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(db_path),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    with application.app_context():
        db = get_db()
        card = db.execute("SELECT * FROM cards WHERE id = 1").fetchone()
        assert card["image_version"] == 1
        rows = db.execute(
            "SELECT image_version, rainfall_mm FROM entries ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [(1, "31"), (1, "13")]
        # The version-scoped unique index allows a second entry round per clerk.
        db.execute(
            """INSERT INTO entries
               (card_id, clerk_id, image_version, station_no, obs_date,
                rainfall_mm, revision, created_at, updated_at)
               VALUES (1, 2, 2, 'S', '1986-07-15', '31', 1, 't', 't')"""
        )
        db.commit()
        assert (
            db.execute(
                "SELECT COUNT(*) FROM entries WHERE card_id = 1"
            ).fetchone()[0]
            == 3
        )
        # Foreign-key enforcement is back on after the table rebuild.
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        import sqlite3 as _sqlite3

        with pytest.raises(_sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO entries (card_id, clerk_id, image_version, "
                "station_no, obs_date, rainfall_mm, revision, created_at, updated_at) "
                "VALUES (999, 2, 1, 'S', '1986-07-15', '1', 1, 't', 't')"
            )
