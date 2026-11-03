"""Library: SQLite persistence, versions, feedback, crash recovery, backups."""
from __future__ import annotations

import json

import pytest

from kestrel.library.db import Database
from kestrel.models import ImageRecord, ProfileSpec


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def _image(name="shot.jpg", path=None):
    return ImageRecord(path=path or f"/tmp/{name}", filename=name, width=100, height=80,
                       format="jpg", bytes=1234, sha256="deadbeef")


def test_collections_crud(db):
    c = db.create_collection("Sports Test", genre_hint="sports")
    assert c.id.startswith("col_")
    cols = db.list_collections()
    assert any(x["id"] == c.id for x in cols)
    db.update_collection(c.id, name="Renamed", notes="night match")
    assert db.get_collection(c.id).name == "Renamed"
    db.delete_collection(c.id)
    with pytest.raises(Exception):
        db.get_collection(c.id)


def test_images_are_unique_by_path(db):
    rec = db.add_image(_image())
    again = db.add_image(_image())
    assert again.id == rec.id, "importing the same file is idempotent"
    other = db.add_image(_image("second.jpg"))
    assert other.id != rec.id


def test_image_filtering_and_search(db):
    c = db.create_collection("Wedding")
    db.add_image(_image("bride.jpg"))
    db.add_image(_image("groom.jpg"))
    first, total = db.list_images(limit=10)
    assert total >= 2
    found, _ = db.list_images(search="bride")
    assert len(found) == 1 and found[0].filename == "bride.jpg"
    db.update_image(found[0].id, status="edited", genre="wedding")
    edited, _ = db.list_images(status="edited")
    assert [i.filename for i in edited] == ["bride.jpg"]


def test_versions_and_latest(db):
    from kestrel.models import EditPlan, Version

    rec = db.add_image(_image())
    v1 = db.save_version(Version(image_id=rec.id, plan=EditPlan(genre="sports"), quality="high"))
    v2 = db.save_version(Version(image_id=rec.id, plan=EditPlan(genre="sports"), quality="maximum"))
    versions = db.list_versions(rec.id)
    assert {v.id for v in versions} == {v1.id, v2.id}
    latest = db.latest_version(rec.id)
    assert latest.id in (v1.id, v2.id)
    db.mark_applied(v2.id)
    assert db.get_version(v2.id).applied_to_lightroom is True


def test_feedback_is_recorded(db):
    rec = db.add_image(_image())
    event = db.add_feedback(rec.id, None, "accept", rating=5, delta={"contrast": 4}, note="great")
    assert event["kind"] == "accept"
    listed = db.list_feedback()
    assert any(e["kind"] == "accept" for e in listed)


def test_prefs_round_trip(db):
    db.set_pref("settings.test", {"a": 1, "b": [1, 2, 3]})
    assert db.get_pref("settings.test") == {"a": 1, "b": [1, 2, 3]}
    assert db.get_pref("missing", "fallback") == "fallback"


def test_stats_report_counts(db):
    db.add_image(_image("a.jpg"))
    db.add_image(_image("b.jpg"))
    stats = db.stats()
    assert stats["images"] >= 2
    assert "by_status" in stats


def test_jobs_and_items_are_ordered(db):
    from kestrel.models import JobRecord

    ids = [db.add_image(_image(f"{i}.jpg")).id for i in range(3)]
    job = db.create_job(JobRecord(kind="edit", priority=7, options={"quality": "standard"}), ids)
    items = db.job_items(job.id)
    assert len(items) == 3
    assert all(item["status"] == "queued" for item in items)
    item = db.next_job_item(job.id)
    assert item is not None
    db.set_job_item(item["id"], "running")
    nxt = db.next_job_item(job.id)
    assert nxt["id"] != item["id"], "a running item is not handed out twice"


def test_recover_interrupted_requeues_running_items(db):
    from kestrel.models import JobRecord

    ids = [db.add_image(_image(f"r{i}.jpg")).id for i in range(2)]
    job = db.create_job(JobRecord(kind="edit"), ids)
    item = db.next_job_item(job.id)
    db.set_job_item(item["id"], "running")
    recovered = db.recover_interrupted()
    assert recovered >= 1
    statuses = {i["status"] for i in db.job_items(job.id)}
    assert "running" not in statuses
    assert "queued" in statuses


def test_requeue_failed_respects_attempt_limit(db):
    from kestrel.models import JobRecord

    ids = [db.add_image(_image("f.jpg")).id]
    job = db.create_job(JobRecord(kind="edit"), ids)
    item = db.next_job_item(job.id)
    db.set_job_item(item["id"], "failed", error="boom", attempts=1)
    assert db.requeue_failed(job.id, max_attempts=3) == 1, "first failure is retried"
    item = db.next_job_item(job.id)
    db.set_job_item(item["id"], "failed", error="boom", attempts=3)
    assert db.requeue_failed(job.id, max_attempts=3) == 0, "attempt limit is honoured"
    assert db.next_job_item(job.id) is None


def test_backup_copies_a_consistent_database(db, tmp_path):
    db.add_image(_image("backup.jpg"))
    dest = db.backup(tmp_path / "backups")
    assert dest.exists()
    clone = Database(dest)
    try:
        images, _ = clone.list_images(limit=10)
        assert any(i.filename == "backup.jpg" for i in images)
    finally:
        clone.close()


def test_audit_log_is_written(db):
    db.audit("test.event", {"value": 1})
    rows = db._rows("SELECT event FROM audit ORDER BY rowid DESC LIMIT 1")
    assert rows and rows[0]["event"] == "test.event"


def test_profiles_can_be_stored_and_deleted(db):
    spec = ProfileSpec(name="Custom Sports", genre="sports", description="test")
    stored = db.upsert_profile(spec)
    assert stored.id
    assert any(p.id == stored.id for p in db.list_profiles())
    db.delete_profile(stored.id)
    with pytest.raises(Exception):
        db.get_profile(stored.id)
