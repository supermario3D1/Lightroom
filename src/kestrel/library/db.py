"""SQLite project store.

Design: WAL journal, one connection per thread, every write inside a short
transaction.  Edits are *versioned rows* rather than mutable state, so an
interrupted batch can never lose completed work — a job's finished items are
committed one by one, and anything still marked ``running`` at startup is
recovered as ``queued``.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core import NotFound, new_id, utcnow
from ..models import (Collection, ImageRecord, JobRecord, ProfileSpec, Version)

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, genre_hint TEXT DEFAULT '',
  notes TEXT DEFAULT '', profile_id TEXT, created TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS images (
  id TEXT PRIMARY KEY, collection_id TEXT, path TEXT UNIQUE NOT NULL, filename TEXT,
  lr_uuid TEXT, width INTEGER, height INTEGER, bytes INTEGER, sha256 TEXT,
  format TEXT, imported TEXT, status TEXT DEFAULT 'new', genre TEXT DEFAULT '',
  camera TEXT DEFAULT '', lens TEXT DEFAULT '', iso INTEGER DEFAULT 0,
  rating INTEGER DEFAULT 0, accepted INTEGER, tags TEXT DEFAULT '[]',
  error TEXT DEFAULT '', analysis_json TEXT, plan_json TEXT,
  current_version_id TEXT, edition_count INTEGER DEFAULT 0,
  render_report_json TEXT DEFAULT '{}', analysis_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_images_collection ON images(collection_id);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY, image_id TEXT NOT NULL, parent_id TEXT, created TEXT,
  mode TEXT, intent TEXT, strength REAL, quality TEXT, profile_id TEXT,
  plan_json TEXT, render_path TEXT DEFAULT '', thumb_path TEXT DEFAULT '',
  original_thumb TEXT DEFAULT '', report_json TEXT DEFAULT '{}',
  applied_to_lightroom INTEGER DEFAULT 0, feedback_json TEXT, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_versions_image ON versions(image_id);
CREATE TABLE IF NOT EXISTS profiles (
  id TEXT PRIMARY KEY, builtin INTEGER DEFAULT 0, name TEXT, genre TEXT,
  spec_json TEXT, created TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT, collection_id TEXT, status TEXT,
  priority INTEGER DEFAULT 5, progress REAL DEFAULT 0, total INTEGER DEFAULT 0,
  completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, created TEXT,
  started TEXT, finished TEXT, eta_seconds REAL, message TEXT DEFAULT '',
  options_json TEXT DEFAULT '{}', errors_json TEXT DEFAULT '[]', worker_pid INTEGER
);
CREATE TABLE IF NOT EXISTS job_items (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, image_id TEXT NOT NULL,
  status TEXT DEFAULT 'queued', version_id TEXT, error TEXT DEFAULT '',
  attempts INTEGER DEFAULT 0, updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_job ON job_items(job_id);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, image_id TEXT, version_id TEXT, kind TEXT,
  rating INTEGER, delta_json TEXT DEFAULT '{}', note TEXT DEFAULT '', created TEXT
);
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value_json TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY, ts TEXT, event TEXT, detail_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def _json(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), default=str)


def _unjson(v: Optional[str], default: Any = None) -> Any:
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._write_lock:
            c = self.conn()
            cur = c.execute(sql, tuple(params))
            c.commit()
            return cur

    def _rows(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        return list(self.conn().execute(sql, tuple(params)).fetchall())

    def audit(self, event: str, detail: Optional[Dict[str, Any]] = None) -> None:
        self._exec("INSERT INTO audit (id, ts, event, detail_json) VALUES (?,?,?,?)",
                   (new_id("aud_"), utcnow(), event, _json(detail or {})))

    # ------------------------------------------------------------------ collections
    def create_collection(self, name: str, genre_hint: str = "", notes: str = "",
                          profile_id: Optional[str] = None) -> Collection:
        col = Collection(id=new_id("col_"), name=name.strip() or "Untitled collection",
                         genre_hint=genre_hint, notes=notes, profile_id=profile_id)
        self._exec("""INSERT INTO collections (id,name,genre_hint,notes,profile_id,created,updated)
                      VALUES (?,?,?,?,?,?,?)""",
                   (col.id, col.name, col.genre_hint, col.notes, col.profile_id,
                    col.created, col.created))
        return col

    def list_collections(self) -> List[Dict[str, Any]]:
        rows = self._rows("""SELECT c.*, (SELECT COUNT(*) FROM images i WHERE i.collection_id=c.id) AS count,
                             (SELECT COUNT(*) FROM images i WHERE i.collection_id=c.id AND i.status='edited') AS edited
                             FROM collections c ORDER BY c.created DESC""")
        return [dict(r) for r in rows]

    def get_collection(self, cid: str) -> Collection:
        rows = self._rows("SELECT * FROM collections WHERE id=?", (cid,))
        if not rows:
            raise NotFound("That collection no longer exists.")
        r = rows[0]
        return Collection(id=r["id"], name=r["name"], genre_hint=r["genre_hint"] or "",
                          notes=r["notes"] or "", profile_id=r["profile_id"],
                          created=r["created"] or "")

    def update_collection(self, cid: str, **fields: Any) -> None:
        allowed = {"name", "genre_hint", "notes", "profile_id"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        q = ", ".join(f"{k}=?" for k in sets)
        self._exec(f"UPDATE collections SET {q}, updated=? WHERE id=?",
                   list(sets.values()) + [utcnow(), cid])

    def delete_collection(self, cid: str, delete_images: bool = False) -> None:
        if delete_images:
            self._exec("DELETE FROM images WHERE collection_id=?", (cid,))
        else:
            self._exec("UPDATE images SET collection_id=NULL WHERE collection_id=?", (cid,))
        self._exec("DELETE FROM collections WHERE id=?", (cid,))
        self.audit("collection.delete", {"id": cid, "images_deleted": delete_images})

    # ------------------------------------------------------------------ images
    def add_image(self, rec: ImageRecord) -> ImageRecord:
        rec.id = rec.id or new_id("img_")
        rec.imported = rec.imported or utcnow()
        try:
            self._exec("""INSERT INTO images
              (id,collection_id,path,filename,lr_uuid,width,height,bytes,sha256,format,
               imported,status,genre,camera,lens,iso,rating,accepted,tags,error,
               current_version_id,edition_count)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (rec.id, rec.collection_id, rec.path, rec.filename, rec.lr_uuid,
                        rec.width, rec.height, rec.bytes, rec.sha256, rec.format,
                        rec.imported, rec.status, rec.genre, rec.camera, rec.lens,
                        rec.iso, rec.rating, rec.accepted, _json(rec.tags), rec.error,
                        rec.current_version_id, rec.edition_count))
        except sqlite3.IntegrityError:
            rows = self._rows("SELECT * FROM images WHERE path=?", (rec.path,))
            if rows:
                return self._image_from_row(rows[0])
            raise
        return rec

    def _image_from_row(self, r: sqlite3.Row, light: bool = False) -> ImageRecord:
        rec = ImageRecord(
            id=r["id"], path=r["path"], filename=r["filename"] or "",
            collection_id=r["collection_id"], lr_uuid=r["lr_uuid"],
            width=r["width"] or 0, height=r["height"] or 0, bytes=r["bytes"] or 0,
            sha256=r["sha256"] or "", format=r["format"] or "",
            imported=r["imported"] or "", status=r["status"] or "new",
            genre=r["genre"] or "", camera=r["camera"] or "", lens=r["lens"] or "",
            iso=r["iso"] or 0, rating=r["rating"] or 0,
            accepted=None if r["accepted"] is None else bool(r["accepted"]),
            tags=_unjson(r["tags"], []), error=r["error"] or "",
            current_version_id=r["current_version_id"], edition_count=r["edition_count"] or 0,
            render_report=_unjson(r["render_report_json"], {}),
        )
        if not light:
            a = _unjson(r["analysis_json"])
            if a:
                rec.analysis = ImageRecord.from_dict({"analysis": a}).analysis
            p = _unjson(r["plan_json"])
            if p:
                rec.plan = ImageRecord.from_dict({"plan": p}).plan
        return rec

    def get_image(self, iid: str, light: bool = False) -> ImageRecord:
        rows = self._rows("SELECT * FROM images WHERE id=?", (iid,))
        if not rows:
            raise NotFound("That photograph is no longer in the library.")
        return self._image_from_row(rows[0], light=light)

    def get_image_by_path(self, path: str, light: bool = False) -> Optional[ImageRecord]:
        rows = self._rows("SELECT * FROM images WHERE path=?", (str(path),))
        return self._image_from_row(rows[0], light=light) if rows else None

    def list_images(self, collection_id: Optional[str] = None, status: Optional[str] = None,
                    search: str = "", limit: int = 500, offset: int = 0,
                    light: bool = True) -> Tuple[List[ImageRecord], int]:
        where, params = [], []
        if collection_id:
            where.append("collection_id=?")
            params.append(collection_id)
        if status:
            where.append("status=?")
            params.append(status)
        if search:
            where.append("(filename LIKE ? OR genre LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._rows(f"SELECT COUNT(*) AS n FROM images {clause}", params)[0]["n"]
        rows = self._rows(f"SELECT * FROM images {clause} ORDER BY filename LIMIT ? OFFSET ?",
                          params + [limit, offset])
        return [self._image_from_row(r, light=light) for r in rows], int(total)

    def update_image(self, iid: str, **fields: Any) -> None:
        allowed = {"collection_id", "status", "genre", "rating", "accepted", "tags",
                   "error", "current_version_id", "edition_count", "analysis_json",
                   "plan_json", "analysis_hash", "render_report_json", "sha256",
                   "width", "height", "camera", "lens", "iso", "lr_uuid", "format",
                   "bytes"}
        sets = {k: (_json(v) if k in ("tags", "analysis_json", "plan_json",
                                      "render_report_json") else v)
                for k, v in fields.items() if k in allowed}
        if not sets:
            return
        q = ", ".join(f"{k}=?" for k in sets)
        self._exec(f"UPDATE images SET {q} WHERE id=?", list(sets.values()) + [iid])

    def save_analysis(self, iid: str, analysis_json: str, hash_: str) -> None:
        self._exec("UPDATE images SET analysis_json=?, analysis_hash=?, status="
                   "CASE WHEN status IN ('new','failed') THEN 'analysed' ELSE status END,"
                   " genre=? WHERE id=?",
                   (analysis_json, hash_, _unjson(analysis_json, {}).get("genre", ""), iid))

    def delete_image(self, iid: str, hard: bool = True) -> None:
        self._exec("DELETE FROM versions WHERE image_id=?", (iid,))
        self._exec("DELETE FROM job_items WHERE image_id=?", (iid,))
        if hard:
            self._exec("DELETE FROM images WHERE id=?", (iid,))

    # ------------------------------------------------------------------ versions
    def save_version(self, v: Version) -> Version:
        v.id = v.id or new_id("ver_")
        v.created = v.created or utcnow()
        self._exec("""INSERT INTO versions
          (id,image_id,parent_id,created,mode,intent,strength,quality,profile_id,
           plan_json,render_path,thumb_path,original_thumb,report_json,
           applied_to_lightroom,feedback_json,note)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (v.id, v.image_id, v.parent_id, v.created, v.mode, v.intent,
                    v.strength, v.quality, v.profile_id,
                    _json(v.plan.to_dict()) if v.plan else None,
                    v.render_path, v.thumb_path, v.original_thumb, _json(v.report),
                    1 if v.applied_to_lightroom else 0,
                    _json(v.feedback) if v.feedback else None, v.note))
        return v

    def _version_from_row(self, r: sqlite3.Row) -> Version:
        from ..models import EditPlan
        v = Version(id=r["id"], image_id=r["image_id"], parent_id=r["parent_id"],
                    created=r["created"] or "", mode=r["mode"] or "adaptive",
                    intent=r["intent"] or "automatic", strength=r["strength"] or 1.0,
                    quality=r["quality"] or "high", profile_id=r["profile_id"],
                    render_path=r["render_path"] or "", thumb_path=r["thumb_path"] or "",
                    original_thumb=r["original_thumb"] or "",
                    report=_unjson(r["report_json"], {}) or {},
                    applied_to_lightroom=bool(r["applied_to_lightroom"]),
                    feedback=_unjson(r["feedback_json"]), note=r["note"] or "")
        p = _unjson(r["plan_json"])
        if p:
            v.plan = EditPlan.from_dict(p)
        return v

    def get_version(self, vid: str) -> Version:
        rows = self._rows("SELECT * FROM versions WHERE id=?", (vid,))
        if not rows:
            raise NotFound("That edit version is no longer available.")
        return self._version_from_row(rows[0])

    def list_versions(self, image_id: str, limit: int = 50) -> List[Version]:
        rows = self._rows("SELECT * FROM versions WHERE image_id=? ORDER BY created DESC LIMIT ?",
                          (image_id, limit))
        return [self._version_from_row(r) for r in rows]

    def latest_version(self, image_id: str) -> Optional[Version]:
        vs = self.list_versions(image_id, 1)
        return vs[0] if vs else None

    def set_version_feedback(self, vid: str, feedback: Dict[str, Any],
                             note: Optional[str] = None) -> None:
        sets = "feedback_json=?"
        params: List[Any] = [_json(feedback)]
        if note is not None:
            sets += ", note=?"
            params.append(note)
        self._exec(f"UPDATE versions SET {sets} WHERE id=?", params + [vid])

    def mark_applied(self, vid: str) -> None:
        self._exec("UPDATE versions SET applied_to_lightroom=1 WHERE id=?", (vid,))

    # ------------------------------------------------------------------ profiles
    def upsert_profile(self, spec: ProfileSpec) -> ProfileSpec:
        # A profile without an id would silently overwrite the row keyed by "" —
        # so one is always minted here, whatever the caller did.
        if not spec.id:
            spec.id = new_id("prof_")
        spec.updated = utcnow()
        self._exec("""INSERT INTO profiles (id,builtin,name,genre,spec_json,created,updated)
                      VALUES (?,?,?,?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name, genre=excluded.genre,
                      spec_json=excluded.spec_json, updated=excluded.updated""",
                   (spec.id, 1 if spec.builtin else 0, spec.name, spec.genre,
                    _json(spec.to_dict()), spec.created, spec.updated))
        return spec

    def list_profiles(self) -> List[ProfileSpec]:
        rows = self._rows("SELECT * FROM profiles ORDER BY builtin DESC, genre, name")
        out = []
        for r in rows:
            data = _unjson(r["spec_json"], {}) or {}
            data.pop("id", None)
            known = set(ProfileSpec.__dataclass_fields__)
            try:
                out.append(ProfileSpec(**{k: v for k, v in data.items() if k in known}))
            except Exception:
                continue
        for p in out:
            p.id = p.id or ""
        # ids come from the spec payload
        for r, p in zip(rows, out):
            p.id = r["id"]
            p.builtin = bool(r["builtin"])
        return out

    def get_profile(self, pid: str) -> ProfileSpec:
        rows = self._rows("SELECT * FROM profiles WHERE id=?", (pid,))
        if not rows:
            raise NotFound("That profile no longer exists.")
        r = rows[0]
        data = _unjson(r["spec_json"], {}) or {}
        data.pop("id", None)
        known = set(ProfileSpec.__dataclass_fields__)
        spec = ProfileSpec(**{k: v for k, v in data.items() if k in known})
        spec.id = r["id"]
        spec.builtin = bool(r["builtin"])
        return spec

    def delete_profile(self, pid: str) -> None:
        row = self._rows("SELECT builtin FROM profiles WHERE id=?", (pid,))
        if not row:
            raise NotFound("That profile no longer exists.")
        if row[0]["builtin"]:
            raise NotFound("Built-in profiles cannot be deleted — duplicate it instead.")
        self._exec("DELETE FROM profiles WHERE id=?", (pid,))

    # ------------------------------------------------------------------ jobs
    def create_job(self, rec: JobRecord, image_ids: List[str]) -> JobRecord:
        rec.id = rec.id or new_id("job_")
        rec.created = utcnow()
        rec.total = len(image_ids)
        rec.priority = int(rec.priority)
        self._exec("""INSERT INTO jobs (id,kind,collection_id,status,priority,progress,
                      total,completed,failed,created,started,finished,eta_seconds,message,
                      options_json,errors_json,worker_pid)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rec.id, rec.kind, rec.collection_id, rec.status, rec.priority,
                    rec.progress, rec.total, rec.completed, rec.failed, rec.created,
                    rec.started, rec.finished, rec.eta_seconds, rec.message,
                    _json(rec.options), _json(rec.errors), rec.worker_pid))
        for iid in image_ids:
            self._exec("""INSERT INTO job_items (id,job_id,image_id,status,attempts,updated)
                          VALUES (?,?,?,?,0,?)""",
                       (new_id("itm_"), rec.id, iid, "queued", utcnow()))
        return rec

    def get_job(self, jid: str) -> JobRecord:
        rows = self._rows("SELECT * FROM jobs WHERE id=?", (jid,))
        if not rows:
            raise NotFound("That job no longer exists.")
        r = rows[0]
        return JobRecord(
            id=r["id"], kind=r["kind"], collection_id=r["collection_id"],
            status=r["status"], priority=r["priority"], progress=r["progress"],
            total=r["total"], completed=r["completed"], failed=r["failed"],
            created=r["created"] or "", started=r["started"], finished=r["finished"],
            eta_seconds=r["eta_seconds"], message=r["message"] or "",
            options=_unjson(r["options_json"], {}) or {},
            errors=_unjson(r["errors_json"], []) or [], worker_pid=r["worker_pid"])

    def list_jobs(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def update_job(self, jid: str, **fields: Any) -> None:
        allowed = {"status", "progress", "completed", "failed", "started", "finished",
                   "eta_seconds", "message", "errors_json", "worker_pid", "priority"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        q = ", ".join(f"{k}=?" for k in sets)
        self._exec(f"UPDATE jobs SET {q} WHERE id=?", list(sets.values()) + [jid])

    def job_items(self, jid: str) -> List[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM job_items WHERE job_id=? ORDER BY updated", (jid,))
        return [dict(r) for r in rows]

    def next_job_item(self, jid: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM job_items WHERE job_id=? AND status='queued' "
                          "ORDER BY updated LIMIT 1", (jid,))
        return dict(rows[0]) if rows else None

    def set_job_item(self, item_id: str, status: str, version_id: Optional[str] = None,
                     error: str = "", attempts: Optional[int] = None) -> None:
        if attempts is None:
            self._exec("UPDATE job_items SET status=?, version_id=?, error=?, updated=? "
                       "WHERE id=?", (status, version_id, error, utcnow(), item_id))
        else:
            self._exec("UPDATE job_items SET status=?, version_id=?, error=?, attempts=?, "
                       "updated=? WHERE id=?",
                       (status, version_id, error, attempts, utcnow(), item_id))

    def requeue_failed(self, jid: str, max_attempts: int = 3) -> int:
        cur = self._exec("UPDATE job_items SET status='queued', updated=? "
                         "WHERE job_id=? AND status='failed' AND attempts < ?",
                         (utcnow(), jid, max_attempts))
        return cur.rowcount

    def recover_interrupted(self) -> int:
        """Called at startup: unfinished work is requeued, never silently dropped."""
        cur = self._exec("UPDATE job_items SET status='queued', updated=? "
                         "WHERE status='running'", (utcnow(),))
        n = cur.rowcount
        self._exec("UPDATE jobs SET status='queued', worker_pid=NULL, message=? "
                   "WHERE status='running'", ("Recovered after restart",))
        if n:
            self.audit("job.recover", {"requeued_items": n})
        return n

    def cancel_job(self, jid: str) -> None:
        self._exec("UPDATE jobs SET status='cancelled', finished=? WHERE id=?",
                   (utcnow(), jid))
        self._exec("UPDATE job_items SET status='cancelled' WHERE job_id=? AND status='queued'",
                   (jid,))

    def pause_job(self, jid: str) -> None:
        self._exec("UPDATE jobs SET status='paused' WHERE id=? AND status='queued'", (jid,))

    def resume_job(self, jid: str) -> None:
        self._exec("UPDATE jobs SET status='queued' WHERE id=? AND status='paused'", (jid,))

    # ------------------------------------------------------------------ feedback & prefs
    def add_feedback(self, image_id: str, version_id: Optional[str], kind: str,
                     rating: int = 0, delta: Optional[Dict[str, Any]] = None,
                     note: str = "") -> Dict[str, Any]:
        rec = {"id": new_id("fb_"), "image_id": image_id, "version_id": version_id,
               "kind": kind, "rating": int(rating), "delta": delta or {},
               "note": note, "created": utcnow()}
        self._exec("""INSERT INTO feedback (id,image_id,version_id,kind,rating,delta_json,note,created)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (rec["id"], image_id, version_id, kind, rating, _json(rec["delta"]),
                    note, rec["created"]))
        self.audit("feedback", {"kind": kind, "image": image_id})
        return rec

    def list_feedback(self, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM feedback ORDER BY created DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["delta"] = _unjson(r["delta_json"], {})
            out.append(d)
        return out

    def set_pref(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO prefs (key,value_json) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                   (key, _json(value)))

    def get_pref(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value_json FROM prefs WHERE key=?", (key,))
        return _unjson(rows[0]["value_json"], default) if rows else default

    # ------------------------------------------------------------------ stats & maintenance
    def stats(self) -> Dict[str, Any]:
        q = self._rows("SELECT status, COUNT(*) n FROM images GROUP BY status")
        by_status = {r["status"]: r["n"] for r in q}
        total = sum(by_status.values())
        versions = self._rows("SELECT COUNT(*) n FROM versions")[0]["n"]
        accepted = self._rows("SELECT COUNT(*) n FROM images WHERE accepted=1")[0]["n"]
        rejected = self._rows("SELECT COUNT(*) n FROM images WHERE accepted=0")[0]["n"]
        genres = {r["genre"]: r["n"] for r in
                  self._rows("SELECT genre, COUNT(*) n FROM images GROUP BY genre")}
        fb = self._rows("SELECT kind, COUNT(*) n FROM feedback GROUP BY kind")
        return {"images": total, "by_status": by_status, "versions": versions,
                "accepted": accepted, "rejected": rejected, "genres": genres,
                "feedback": {r["kind"]: r["n"] for r in fb},
                "collections": self._rows("SELECT COUNT(*) n FROM collections")[0]["n"]}

    def backup(self, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"kestrel-{time.strftime('%Y%m%d-%H%M%S')}.db"
        with self._write_lock:
            self.conn().commit()
            src = sqlite3.connect(str(self.path))
            try:
                dst = sqlite3.connect(str(dest))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        return dest

    def prune_audit(self, keep_days: int = 90) -> int:
        cutoff = time.time() - keep_days * 86400
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
        cur = self._exec("DELETE FROM audit WHERE ts < ?", (iso,))
        return cur.rowcount
