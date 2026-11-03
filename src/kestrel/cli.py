"""Kestrel command line.

    kestrel serve                  start the local API + web app
    kestrel doctor                 environment and capability report
    kestrel import <paths…>        add photographs to the library
    kestrel collections            list collections
    kestrel preview <image>        analyse + plan + render, print the reasoning
    kestrel batch <collection>     queue a collection edit
    kestrel jobs [--watch]         watch the processing queue
    kestrel export <image>         render the latest edit to disk
    kestrel eval <collection>      run the evaluation harness
    kestrel profiles               list profiles
    kestrel licence-key <email>    (vendor) issue a licence key
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from .core import (APP_VERSION, BRAND, KestrelError, Settings, engine, human_seconds,
                   setup_logging)

log = logging.getLogger("kestrel.cli")


def _eng():
    return engine()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api.app import create_app
    settings = Settings.load()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.quality:
        settings.default_quality = args.quality
    eng = engine(settings)
    app = create_app(eng)
    print(f"\n  {BRAND['name']} {APP_VERSION} — {BRAND['tagline']}")
    print(f"  Local web app:  http://{'127.0.0.1' if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}/")
    print(f"  API:            http://{'127.0.0.1' if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}/api/docs")
    print(f"  Library home:   {settings.home}")
    print(f"  Photos stay on this machine. No upload happens without your action.\n")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning",
                workers=1, access_log=False)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .core import HardwareProfile
    from .lightroom.native import capability_report
    settings = Settings.load()
    hw = HardwareProfile.detect()
    eng = engine(settings)
    print(f"{BRAND['name']} {APP_VERSION}  ({BRAND['channel']})")
    print(f"home:      {settings.home}")
    print(f"python:    {sys.version.split()[0]}")
    print(f"cpu:       {hw.cpu_name} × {hw.cpu_count} logical cores")
    print(f"memory:    {hw.total_memory_gb} GB")
    if hw.onnx_runtime:
        print(f"gpu/onnx:  {', '.join(hw.onnx_providers) or 'CPU only'}")
    else:
        print("gpu/onnx:  onnxruntime not installed — classical CPU backends in use")
    print(f"faces:     {eng.analyser.backends.get('faces', 'unknown')}")
    print(f"profile db:{len(eng.db.list_profiles())} profiles, "
          f"{eng.db.stats()['images']} photographs")
    print(f"licence:   {__import__('kestrel.license', fromlist=['x']).verify_licence(settings)['status']}")
    print("\ncapabilities")
    for k, v in capability_report().items():
        print(f"  {k:24s} {v}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    eng = _eng()
    cid = args.collection
    if args.collection_name:
        cid = eng.db.create_collection(args.collection_name).id
    res = eng.import_files(args.paths, collection_id=cid)
    print(json.dumps({k: v for k, v in res.items() if k != "skipped"}, indent=2))
    if res["skipped"]:
        print(f"{len(res['skipped'])} already imported")
    return 0


def cmd_collections(args: argparse.Namespace) -> int:
    for c in _eng().db.list_collections():
        print(f"{c['id']}  {c['count']:>5} images  {c['edited']:>5} edited  {c['name']}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    eng = _eng()
    result = eng.preview(args.image, intent=args.intent, strength=args.strength,
                         quality=args.quality, profile_id=args.profile,
                         genre=getattr(args, "genre", None))
    print(f"\n{result.image.filename}  →  {result.plan.genre} / {result.plan.intent}")
    print(f"profile: {result.plan.detail.get('profile_name')}")
    print(f"quality: {result.quality}   render: {result.report.get('ms_total', 0):.0f} ms"
          f"   passes: {result.plan.iterations}")
    if result.plan.metrics:
        m = result.plan.metrics
        print(f"score:   {m.score:.2f}   naturalness: {m.naturalness:.2f}   "
              f"skin ΔE: {m.skin_delta_e:.1f}   clipping: {m.clipping_high * 100:.2f}%")
    print("\nsettings")
    for s in result.plan.settings_summary():
        print(f"  {s['label']:16s} {s['display']:>8s} {s['unit']}")
    print("\nwhy")
    for e in result.plan.explanation:
        print(f"  • [{e.area}] {e.action} — {e.reason}" + (f"  ({e.amount})" if e.amount else ""))
    if result.plan.masks:
        print("\nlocal adjustments")
        for m in result.plan.masks:
            print(f"  • {m.name} ({m.kind}, conf {m.confidence:.2f}) {m.notes}")
    if result.plan.warnings:
        print("\nwarnings")
        for w in result.plan.warnings:
            print(f"  ! {w}")
    print(f"\npreview: {result.version.thumb_path}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    eng = _eng()
    job = eng.queue.submit_collection(
        args.collection, kind="edit", priority=args.priority,
        options={"intent": args.intent, "quality": args.quality,
                 "profile_id": args.profile, "strength": args.strength,
                 "consistency": not args.no_consistency})
    print(f"queued {job.id} with {job.total} photographs")
    if args.watch:
        return _watch(eng, job.id, args.quality)
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    eng = _eng()
    if args.watch:
        jobs = eng.db.list_jobs(limit=1)
        if jobs:
            return _watch(eng, jobs[0]["id"], args.quality)
    status = eng.queue.status()
    for j in status["jobs"]:
        eta = f" eta {human_seconds(j['eta_seconds'])}" if j["eta_seconds"] else ""
        print(f"{j['id']}  {j['status']:<9} {j['completed']:>4}/{j['total']:<4}"
              f" {j['kind']:<7} {j['message'] or ''}{eta}")
    return 0


def _watch(eng, job_id: str, quality: Optional[str]) -> int:
    last = None
    try:
        while True:
            job = eng.db.get_job(job_id)
            line = f"{job.status:<9} {job.completed}/{job.total}  {job.message}"
            if line != last:
                print(line, flush=True)
                last = line
            if job.status in ("done", "failed", "cancelled"):
                failed = eng.db.job_items(job_id)
                bad = [i for i in failed if i["status"] == "failed"]
                if bad:
                    print(f"{len(bad)} photographs failed:")
                    for b in bad[:10]:
                        print(f"  {b['image_id']}: {b['error']}")
                return 0 if not bad else 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        eng.queue.pause(job_id)
        print("paused (safe to resume later)")
        return 130


def cmd_export(args: argparse.Namespace) -> int:
    eng = _eng()
    path, report = eng.export_version(args.image, quality=args.quality, fmt=args.format)
    print(f"written: {path}")
    print(f"render:  {report.get('ms_total', 0):.0f} ms at {report.get('width')}×{report.get('height')}")
    for w in report.get("warnings", []):
        print(f"  ! {w}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval.harness import evaluate_collection
    eng = _eng()
    summary = evaluate_collection(eng, args.collection, limit=args.limit,
                                  intent=args.intent, quality=args.quality)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"evaluated {summary['images_evaluated']} photographs")
        for k in ("median_score", "median_exposure_error", "median_clipping_high",
                  "median_skin_delta_e", "median_skin_consistency", "median_naturalness",
                  "median_haloing", "set_skin_consistency", "contrast_spread"):
            if k in summary:
                print(f"  {k:28s} {summary[k]}")
        for f in summary.get("failures", []):
            print(f"  ! {f['filename']}: {', '.join(f['reasons'])}")
        print(f"report: {summary['report_path']}")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    for p in _eng().db.list_profiles():
        mark = "built-in" if p.builtin else "custom  "
        print(f"{mark} {p.genre:<10} {p.name}")
    return 0


def cmd_licence_key(args: argparse.Namespace) -> int:
    from .license import issue_licence
    print(issue_licence(args.key_id or f"KST-{int(time.time())}", args.email,
                        seats=args.seats, tier=args.tier, expires=args.expires))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kestrel", description=f"{BRAND['name']} — {BRAND['description']}")
    p.add_argument("--version", action="version", version=f"{BRAND['name']} {APP_VERSION}")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="start the local API and web app")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--quality", choices=["standard", "high", "maximum"], default=None)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("doctor", help="environment and capability report")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("import", help="import photographs")
    s.add_argument("paths", nargs="+")
    s.add_argument("--collection")
    s.add_argument("--collection-name")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("collections", help="list collections")
    s.set_defaults(func=cmd_collections)

    s = sub.add_parser("preview", help="edit one photograph and explain the result")
    s.add_argument("image")
    s.add_argument("--intent", default="automatic")
    s.add_argument("--strength", type=float, default=1.0)
    s.add_argument("--quality", default="high")
    s.add_argument("--profile", default=None)
    s.add_argument("--genre", default=None,
                   choices=["sports", "wedding", "portrait", "event", "landscape",
                            "wildlife", "commercial", "general"])
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("batch", help="queue a collection for editing")
    s.add_argument("collection")
    s.add_argument("--intent", default="automatic")
    s.add_argument("--quality", default="high")
    s.add_argument("--profile", default=None)
    s.add_argument("--genre", default=None,
                   choices=["sports", "wedding", "portrait", "event", "landscape",
                            "wildlife", "commercial", "general"])
    s.add_argument("--strength", type=float, default=1.0)
    s.add_argument("--priority", type=int, default=5)
    s.add_argument("--no-consistency", action="store_true")
    s.add_argument("--watch", action="store_true")
    s.set_defaults(func=cmd_batch)

    s = sub.add_parser("jobs", help="processing queue status")
    s.add_argument("--watch", action="store_true")
    s.add_argument("--quality", default=None)
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("export", help="render the latest edit to disk")
    s.add_argument("image")
    s.add_argument("--quality", default="maximum",
                   choices=["standard", "high", "maximum"])
    s.add_argument("--format", default="jpeg", choices=["jpeg", "tiff", "png"])
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("eval", help="run the quality evaluation harness")
    s.add_argument("collection")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--intent", default="automatic")
    s.add_argument("--quality", default="high")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("profiles", help="list editing profiles")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("licence-key", help="(vendor) issue a licence key")
    s.add_argument("email")
    s.add_argument("--key-id")
    s.add_argument("--seats", type=int, default=1)
    s.add_argument("--tier", default="pro")
    s.add_argument("--expires")
    s.set_defaults(func=cmd_licence_key)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.load()
    setup_logging(settings, args.log_level)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KestrelError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint:  {exc.hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
