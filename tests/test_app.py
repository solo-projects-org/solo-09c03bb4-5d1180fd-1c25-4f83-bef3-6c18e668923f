import base64
import csv
import io
import re
import sqlite3

import pytest

from app import create_app, get_db, get_entry

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
            r'name="(entry_[ab]_(?:id|revision))" value="(\d+)"', page
        )
    )
    assert len(fields) == 4
    image_version = re.search(r'name="image_version" value="(\d+)"', page)
    assert image_version is not None
    fields["image_version"] = image_version.group(1)
    return reviewer, cid, fields, page


def swap_form(app, card_no_a, card_no_b):
    reviewer = login(app, "reviewer")
    cid_a = card_id(app, card_no_a)
    cid_b = card_id(app, card_no_b)
    page = reviewer.get(f"/reviewer/swap?card={cid_a}&card={cid_b}")
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    fields = dict(
        re.findall(r'name="(card_[ab]_id|image_version_[ab])" value="(\d+)"', html)
    )
    assert len(fields) == 4
    assert fields["card_a_id"] == str(cid_a)
    assert fields["card_b_id"] == str(cid_b)
    return reviewer, cid_a, cid_b, fields


def do_swap(app, card_no_a, card_no_b):
    reviewer, cid_a, cid_b, fields = swap_form(app, card_no_a, card_no_b)
    response = reviewer.post("/reviewer/swap", data=fields)
    assert response.status_code == 302
    return cid_a, cid_b


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
                "station_no": "30501200",
                "obs_date": "1986-07-14",
                "rainfall_mm": value,
                "image_version": "1",
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
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "13.0",
            "image_version": "1",
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
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "14",
            "image_version": "1",
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
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "12.5",
            "image_version": "1",
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
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31.0",
            "image_version": "1",
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
    base = {"station_no": "30501200", "obs_date": "1986-07-14"}
    for value in ("-1", "abc", "", "NaN", "100001"):
        response = clerk.post(
            f"/clerk/cards/{cid}",
            data={**base, "rainfall_mm": value},
        )
        assert response.status_code == 200
        assert "雨量" in response.data.decode("utf-8")
    response = clerk.post(
        f"/clerk/cards/{cid}",
        data={"station_no": "30501200", "obs_date": "not-a-date", "rainfall_mm": "4"},
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
# Image swap (换图)
# ---------------------------------------------------------------------------


def test_swap_requires_two_distinct_cards_and_reviewer_role(app):
    reviewer = login(app, "reviewer")
    cid_a = card_id(app, "YC-1986-0718")
    cid_b = card_id(app, "YC-1986-0719")

    # Exactly two distinct cards must be selected to open the confirmation.
    assert reviewer.get(f"/reviewer/swap?card={cid_a}").status_code == 302
    assert reviewer.get(f"/reviewer/swap?card={cid_a}&card={cid_a}").status_code == 302
    assert reviewer.get(f"/reviewer/swap?card={cid_a}&card={cid_b}").status_code == 200

    clerk = login(app, "clerk1")
    assert clerk.get(f"/reviewer/swap?card={cid_a}&card={cid_b}").status_code == 403
    assert clerk.post("/reviewer/swap", data={}).status_code == 403


def test_swap_exchanges_images_and_voids_work(app):
    # The seeded 0718/0719 pair carries each other's scans, and their
    # wrong-image readings already made it into the export.
    _, rows = get_export(app)
    exported = {r["card_no"]: r for r in rows}
    assert exported["YC-1986-0718"]["rainfall_mm"] == "120"
    assert exported["YC-1986-0719"]["rainfall_mm"] == "5.5"

    reviewer, cid_a, cid_b, fields = swap_form(app, "YC-1986-0718", "YC-1986-0719")
    response = reviewer.post("/reviewer/swap", data=fields)
    assert response.status_code == 302

    with app.app_context():
        db = get_db()
        card_a = db.execute("SELECT * FROM cards WHERE id = ?", (cid_a,)).fetchone()
        card_b = db.execute("SELECT * FROM cards WHERE id = ?", (cid_b,)).fetchone()
        # Scans exchanged and versions bumped...
        assert card_a["image_filename"] == "sample:rain-card-005.svg"
        assert card_b["image_filename"] == "sample:rain-card-006.svg"
        assert card_a["image_version"] == 2
        assert card_b["image_version"] == 2
        # ...while card numbers and assigned clerks stay put.
        assert card_a["card_no"] == "YC-1986-0718"
        assert card_b["card_no"] == "YC-1986-0719"
        assert (card_a["clerk_a_id"], card_a["clerk_b_id"]) == (1, 2)
        assert (card_b["clerk_a_id"], card_b["clerk_b_id"]) == (1, 2)

        # Old entries are revoked: they no longer count as live work.
        assert db.execute(
            "SELECT COUNT(*) FROM entries WHERE card_id IN (?, ?) AND voided_at IS NULL",
            (cid_a, cid_b),
        ).fetchone()[0] == 0
        voided = db.execute(
            "SELECT * FROM entries WHERE card_id IN (?, ?) AND voided_at IS NOT NULL",
            (cid_a, cid_b),
        ).fetchall()
        assert len(voided) == 4
        assert {row["voided_reason"] for row in voided} == {"image_swapped"}

        # The old adjudication is kept but marked void.
        decisions = db.execute(
            "SELECT * FROM decisions WHERE card_id = ?", (cid_b,)
        ).fetchall()
        assert len(decisions) == 1
        assert decisions[0]["voided_at"] is not None
        assert decisions[0]["voided_reason"] == "image_swapped"

        # The swap itself is audited from both cards' points of view.
        swaps = db.execute(
            "SELECT * FROM image_swaps WHERE card_id IN (?, ?)", (cid_a, cid_b)
        ).fetchall()
        assert len(swaps) == 2
        assert len({row["swap_group"] for row in swaps}) == 1

    # Both cards drop out of the export until re-entered and re-verified.
    _, rows = get_export(app)
    assert "YC-1986-0718" not in {r["card_no"] for r in rows}
    assert "YC-1986-0719" not in {r["card_no"] for r in rows}

    # The clerk's page no longer prefills the old readings, shows the
    # swapped-in scan, and explains the re-entry.
    clerk = login(app, "clerk1")
    html = clerk.get(f"/clerk/cards/{cid_a}").data.decode("utf-8")
    assert 'value="120"' not in html
    assert "已作废" in html
    image = clerk.get(f"/cards/{cid_a}/image")
    assert b"YC-1986-0718" in image.data  # the swapped-in scan
    assert b"30501240" in image.data


def test_reentry_after_swap_returns_card_to_csv(app):
    cid_a, cid_b = do_swap(app, "YC-1986-0718", "YC-1986-0719")

    c1 = login(app, "clerk1")
    c2 = login(app, "clerk2")
    # 0718's real scan shows 30501240 / 1986-07-18 / 5.5 mm.
    for client in (c1, c2):
        response = client.post(
            f"/clerk/cards/{cid_a}",
            data={
                "station_no": "30501240",
                "obs_date": "1986-07-18",
                "rainfall_mm": "5.5",
                "image_version": "2",
            },
        )
        assert response.status_code == 302
    # 0719's real scan shows 30501250 / 1986-07-19 / 120 mm; the second clerk
    # misreads again, so the reviewer adjudicates against the restored scan.
    c1.post(
        f"/clerk/cards/{cid_b}",
        data={
            "station_no": "30501250",
            "obs_date": "1986-07-19",
            "rainfall_mm": "120",
            "image_version": "2",
        },
    )
    c2.post(
        f"/clerk/cards/{cid_b}",
        data={
            "station_no": "30501250",
            "obs_date": "1986-07-19",
            "rainfall_mm": "12",
            "image_version": "2",
        },
    )

    _, rows = get_export(app)
    exported = {r["card_no"]: r for r in rows}
    assert exported["YC-1986-0718"]["rainfall_mm"] == "5.5"
    assert exported["YC-1986-0718"]["verification"] == "double_entry_agreement"
    assert "YC-1986-0719" not in exported  # conflict again, not yet re-verified

    reviewer, cid, fields, _ = review_form(app, "YC-1986-0719")
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 302

    _, rows = get_export(app)
    exported = {r["card_no"]: r for r in rows}
    assert exported["YC-1986-0719"]["rainfall_mm"] == "120"
    assert exported["YC-1986-0719"]["verification"] == "reviewer_adjudication"

    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ? ORDER BY id", (cid,)
        ).fetchall()
    # The pre-swap adjudication stays void; only the fresh one is active.
    assert len(decisions) == 2
    assert decisions[0]["voided_reason"] == "image_swapped"
    assert decisions[1]["voided_at"] is None


def test_swap_with_stale_image_version_is_refused(app):
    reviewer, cid_a, cid_b, fields = swap_form(app, "YC-1986-0718", "YC-1986-0719")

    stale = dict(fields)
    stale["image_version_a"] = "99"
    response = reviewer.post("/reviewer/swap", data=stale)
    assert response.status_code == 409
    assert "没有执行" in response.data.decode("utf-8")

    with app.app_context():
        db = get_db()
        card_a = db.execute("SELECT * FROM cards WHERE id = ?", (cid_a,)).fetchone()
        assert card_a["image_filename"] == "sample:rain-card-006.svg"
        assert card_a["image_version"] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM entries WHERE card_id = ? AND voided_at IS NULL",
            (cid_a,),
        ).fetchone()[0] == 2

    # A real swap invalidates the confirmation page rendered before it.
    response = reviewer.post("/reviewer/swap", data=fields)
    assert response.status_code == 302
    response = reviewer.post("/reviewer/swap", data=fields)
    assert response.status_code == 409

    with app.app_context():
        card_a = get_db().execute(
            "SELECT * FROM cards WHERE id = ?", (cid_a,)
        ).fetchone()
        # The refused second submit did not swap the images back.
        assert card_a["image_filename"] == "sample:rain-card-005.svg"
        assert card_a["image_version"] == 2


def test_clerk_entry_with_stale_image_version_is_refused(app):
    cid_a, _ = do_swap(app, "YC-1986-0718", "YC-1986-0719")

    clerk = login(app, "clerk1")
    response = clerk.post(
        f"/clerk/cards/{cid_a}",
        data={
            "station_no": "30501250",
            "obs_date": "1986-07-19",
            "rainfall_mm": "120",
            "image_version": "1",  # page was rendered before the swap
        },
    )
    assert response.status_code == 409
    html = response.data.decode("utf-8")
    assert "没有保存" in html
    assert "刷新" in html

    with app.app_context():
        assert get_entry(get_db(), cid_a, 1) is None

    # The refreshed form accepts the entry against the swapped-in scan.
    response = clerk.post(
        f"/clerk/cards/{cid_a}",
        data={
            "station_no": "30501240",
            "obs_date": "1986-07-18",
            "rainfall_mm": "5.5",
            "image_version": "2",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        entry = get_entry(get_db(), cid_a, 1)
        assert entry is not None
        assert entry["revision"] == 1
        assert entry["rainfall_mm"] == "5.5"


def test_decide_with_stale_image_version_is_refused(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    do_swap(app, "YC-1986-0715", "YC-1986-0717")

    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 409
    assert "原图已经更换" in response.data.decode("utf-8")

    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,))
            .fetchone()[0]
            == 0
        )


def test_swap_history_and_voided_decision_are_visible(app):
    _, cid_b = do_swap(app, "YC-1986-0718", "YC-1986-0719")

    reviewer = login(app, "reviewer")
    page = reviewer.get(f"/reviewer/cards/{cid_b}").data.decode("utf-8")
    assert "换图记录" in page
    assert "YC-1986-0718" in page
    assert "已失效" in page
    assert "原图互换" in page

    # The clerk dashboard flags the card for re-entry.
    clerk = login(app, "clerk1")
    dashboard = clerk.get("/clerk").data.decode("utf-8")
    assert "原图已换回" in dashboard
