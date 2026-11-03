"""Crash-safe background processing queue.

Design notes
------------
* Every finished image is committed immediately (version row + image status), so
  an interruption never loses completed edits.
* Items claimed by a worker move to ``running``; on startup those are recovered
  and requeued (see ``Database.recover_interrupted``).
* One worker process by default: image rendering is memory-bound, and saturating
  RAM is how batch tools fail.  Concurrency is configurable.
* Priority, pause/resume, failed-item retry and ETA estimation are all first-class.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from ..core import NotFound, human_seconds, utcnow
from ..models import JobRecord

log = logging.getLogger("kestrel.queue")


class JobQueue:
    def __init__(self, engine, start_worker: bool = True):
        self.engine = engine
        self.db = engine.db
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._current_job: Optional[str] = None
        self._lock = threading.RLock()
        self._timings: List[float] = []
        if start_worker:
            self.start()

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kestrel-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ submit
    def submit(self, kind: str, image_ids: List[str], *, collection_id: Optional[str] = None,
               priority: int = 5, options: Optional[Dict[str, Any]] = None) -> JobRecord:
        if not image_ids:
            raise NotFound("There are no photographs selected for this job.")
        rec = JobRecord(kind=kind, collection_id=collection_id, priority=priority,
                        options=options or {}, message="Queued")
        rec = self.db.create_job(rec, image_ids)
        self.db.audit("job.create", {"id": rec.id, "kind": kind, "images": len(image_ids)})
        self.wake()
        return self.db.get_job(rec.id)

    def submit_collection(self, collection_id: str, *, kind: str = "edit",
                          priority: int = 5, options: Optional[Dict[str, Any]] = None,
                          limit: int = 20000) -> JobRecord:
        images, _ = self.db.list_images(collection_id=collection_id, limit=limit)
        return self.submit(kind, [i.id for i in images], collection_id=collection_id,
                           priority=priority, options=options)

    def pause(self, job_id: str) -> Dict[str, Any]:
        self.db.pause_job(job_id)
        if self._current_job == job_id:
            self.db.update_job(job_id, message="Pausing after the current photograph")
        return self.db.get_job(job_id).to_dict()

    def resume(self, job_id: str) -> Dict[str, Any]:
        job = self.db.get_job(job_id)
        if job.status in ("done", "cancelled"):
            raise NotFound("That job has already finished.")
        self.db.resume_job(job_id)
        self.db.update_job(job_id, status="queued", message="Resumed")
        self.wake()
        return self.db.get_job(job_id).to_dict()

    def cancel(self, job_id: str) -> Dict[str, Any]:
        self.db.cancel_job(job_id)
        return self.db.get_job(job_id).to_dict()

    def retry_failed(self, job_id: str) -> Dict[str, Any]:
        n = self.db.requeue_failed(job_id)
        if n:
            self.db.update_job(job_id, status="queued", message=f"Retrying {n} failed photographs")
            self.wake()
        return {"requeued": n, "job": self.db.get_job(job_id).to_dict()}

    # ------------------------------------------------------------------ worker
    def _next_job(self) -> Optional[JobRecord]:
        rows = [r for r in self.db.list_jobs(limit=200) if r["status"] == "queued"]
        rows.sort(key=lambda r: (-int(r["priority"] or 5), r["created"] or ""))
        for r in rows:
            job = self.db.get_job(r["id"])
            items = self.db.job_items(job.id)
            if any(i["status"] in ("queued", "running") for i in items):
                return job
            # nothing left to do
            self.db.update_job(job.id, status="done", finished=utcnow(), progress=1.0,
                               message="Finished")
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._next_job()
                if job is None:
                    self._wake.wait(timeout=1.0)
                    self._wake.clear()
                    continue
                self._process(job)
            except Exception:
                log.exception("worker loop error")
                time.sleep(1.0)

    def _process(self, job: JobRecord) -> None:
        self._current_job = job.id
        self.db.update_job(job.id, status="running", started=job.started or utcnow(),
                           worker_pid=None, message="Processing")
        group_overrides: Dict[str, Dict[str, float]] = {}
        if job.kind == "edit" and job.options.get("consistency", True):
            try:
                group_overrides = self._prepare_consistency(job)
            except Exception:
                log.exception("consistency preparation failed; continuing individually")

        while not self._stop.is_set():
            current = self.db.get_job(job.id)
            if current.status in ("cancelled", "paused"):
                self.db.update_job(job.id, finished=utcnow() if current.status == "cancelled"
                                   else None)
                break
            item = self.db.next_job_item(job.id)
            if item is None:
                items = self.db.job_items(job.id)
                failed = sum(1 for i in items if i["status"] == "failed")
                self.db.update_job(job.id, status="done", finished=utcnow(), progress=1.0,
                                   failed=failed, message="Finished"
                                   + (f" with {failed} failed" if failed else ""))
                self._current_job = None
                break
            self.db.set_job_item(item["id"], "running")
            t0 = time.perf_counter()
            try:
                version_id = self._process_item(job, item["image_id"], group_overrides)
                elapsed = time.perf_counter() - t0
                self._timings = (self._timings + [elapsed])[-20:]
                self.db.set_job_item(item["id"], "done", version_id=version_id,
                                     attempts=int(item["attempts"]) + 1)
            except Exception as exc:
                log.exception("job item failed: %s", item["image_id"])
                attempts = int(item["attempts"]) + 1
                self.db.set_job_item(item["id"], "failed", error=str(exc)[:500],
                                     attempts=attempts)
                errs = current.errors + [{"image_id": item["image_id"],
                                          "error": str(exc)[:300], "at": utcnow()}]
                self.db.update_job(job.id, errors_json=__import__("json").dumps(errs[-50:]))
                try:
                    self.db.update_image(item["image_id"], status="failed", error=str(exc)[:500])
                except Exception:
                    pass
            items = self.db.job_items(job.id)
            done = sum(1 for i in items if i["status"] in ("done", "failed"))
            failed = sum(1 for i in items if i["status"] == "failed")
            progress = done / max(len(items), 1)
            avg = sum(self._timings) / len(self._timings) if self._timings else None
            eta = (avg * (len(items) - done)) if avg else None
            self.db.update_job(job.id, completed=done, failed=failed, progress=round(progress, 4),
                               eta_seconds=round(eta, 1) if eta else None,
                               message=(f"{done}/{len(items)} photographs"
                                        + (f" · about {human_seconds(eta)} left" if eta else "")))
        self._current_job = None

    def _process_item(self, job: JobRecord, image_id: str,
                      overrides: Dict[str, Dict[str, float]]) -> Optional[str]:
        opts = job.options or {}
        engine = self.engine
        intro = opts.get("intent", "automatic")
        quality = opts.get("quality", engine.settings.default_quality)
        strength = float(opts.get("strength", 1.0))
        profile_id = opts.get("profile_id")
        if job.kind == "edit":
            manual = dict(overrides.get(image_id, {}))
            result = engine.preview(image_id, intent=intro, strength=strength,
                                    quality=quality, profile_id=profile_id,
                                    manual=manual or None, save_version=True,
                                    genre=opts.get("genre"))
            return result.version.id
        if job.kind == "export":
            path, _ = engine.export_version(image_id, quality=quality,
                                            fmt=opts.get("format", "jpeg"))
            return str(path)
        if job.kind == "analyse":
            engine.analyse(image_id, force=bool(opts.get("force", False)),
                           quality=quality)
            return None
        raise NotFound(f"Unknown job kind '{job.kind}'")

    # ------------------------------------------------------------------ consistency
    def _prepare_consistency(self, job: JobRecord) -> Dict[str, Dict[str, float]]:
        from .batch import build_groups, consistency_overrides, group_map
        images, _ = self.db.list_images(collection_id=job.collection_id, limit=20000) \
            if job.collection_id else ([], 0)
        if len(images) < 4:
            return {}
        analyses = {}
        for rec in images:
            analysis = rec.analysis
            if analysis is None:
                try:
                    analysis, _, _ = self.engine.analyse(rec.id, quality=job.options.get("quality"))
                except Exception:
                    continue
            analyses[rec.id] = analysis
        groups = build_groups(analyses)
        self.db.set_pref(f"consistency.{job.collection_id or job.id}",
                         {"built": utcnow(), "groups": [g.to_dict() for g in groups]})
        overrides: Dict[str, Dict[str, float]] = {}
        for g in groups:
            if len(g.members) < 2:
                continue
            for m in g.members:
                a = analyses.get(m)
                if a is None:
                    continue
                ov = consistency_overrides(g, a)
                if ov:
                    overrides[m] = ov
        self.db.update_job(job.id, message=f"Grouped into {len(groups)} lighting groups")
        return overrides

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        jobs = self.db.list_jobs(limit=30)
        active = next((j for j in jobs if j["status"] in ("running", "queued", "paused")), None)
        items = self.db.job_items(active["id"]) if active else []
        return {
            "jobs": jobs,
            "active_job_id": active["id"] if active else None,
            "progress": active["progress"] if active else 0.0,
            "eta_seconds": active["eta_seconds"] if active else None,
            "queued": sum(1 for j in jobs if j["status"] == "queued"),
            "paused": sum(1 for j in jobs if j["status"] == "paused"),
            "items": items[-200:],
            "worker_alive": bool(self._thread and self._thread.is_alive()),
        }
