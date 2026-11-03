"""Packaging invariants.

Two things silently break an install and are invisible in normal use:

  * the web UI living only in ``web/`` while the wheel serves ``kestrel/api/static``;
  * brand assets referenced by the UI but never shipped in the package.

These tests fail loudly instead, and they are cheap enough to run on every commit.
"""
from __future__ import annotations

import filecmp
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
BRAND = ROOT / "assets" / "brand"
STATIC = ROOT / "src" / "kestrel" / "api" / "static"

UI_FILES = ("index.html", "styles.css", "app.js", "views.js", "editor.js",
            "brand-mark.svg", "favicon.svg", "social-preview.png")
BRAND_FILES = ("kestrel-mark.svg", "kestrel-logo.svg", "kestrel-icon-32.png",
               "kestrel-icon-64.png", "kestrel-icon-256.png", "social-preview.png")


def test_web_assets_exist():
    for name in UI_FILES:
        assert (WEB / name).is_file(), f"missing web asset: {name}"


def test_brand_assets_exist():
    for name in BRAND_FILES:
        path = BRAND / name
        assert path.is_file(), f"missing brand asset: {name}"
        assert path.stat().st_size > 200, f"brand asset looks empty: {name}"


def test_packaged_ui_matches_source():
    assert (STATIC / "index.html").is_file(), (
        "src/kestrel/api/static is missing — run `python scripts/sync_web.py`"
    )
    for name in UI_FILES:
        source, copy = WEB / name, STATIC / name
        assert copy.is_file(), f"packaged copy missing: {name} (run scripts/sync_web.py)"
        assert filecmp.cmp(source, copy, shallow=False), (
            f"packaged copy is stale: {name} (run scripts/sync_web.py)"
        )


def test_packaged_brand_matches_source():
    for name in BRAND_FILES:
        source, copy = BRAND / name, STATIC / "brand" / name
        assert copy.is_file(), f"packaged brand missing: {name}"
        assert filecmp.cmp(source, copy, shallow=False), f"packaged brand stale: {name}"


def test_brand_sources_match_committed_assets():
    """`python scripts/brand.py --check` must pass on a clean tree.

    The check compares the generated SVG sources, the file manifest and the
    pixel dimensions of every bitmap — the parts that are identical on every
    machine. PNG bytes are not compared: they depend on the Pillow/FreeType/zlib
    build, which is why the old byte-for-byte CI gate could never be portable.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "brand.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, (result.stderr or result.stdout).strip()


def test_index_references_only_shipped_scripts():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for script in ("app.js", "views.js", "editor.js"):
        assert f'src="/{script}"' in html, f"index.html does not load {script}"
        assert (STATIC / script).is_file(), f"{script} not packaged"


def test_api_serves_packaged_ui_when_installed():
    from kestrel.api import app as api_app

    resolved = Path(api_app.WEB_DIR)
    assert (resolved / "index.html").is_file(), f"WEB_DIR does not contain the UI: {resolved}"
    assert Path(api_app.BRAND_DIR).is_dir(), "BRAND_DIR does not exist"


def test_pyproject_declares_package_data():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "api/static/*.js" in text, "JavaScript is not packaged"
    assert "api/static/brand/*.svg" in text, "brand SVG files are not packaged"
    assert 'readme = "README.md"' in text


def test_readme_is_substantial_and_linked():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert len(readme) > 12_000, "the README is the primary deliverable; keep it extensive"
    for section in ("## Installation", "## Lightroom Classic setup", "## Troubleshooting",
                    "## Honest limits", "## Configuration reference"):
        assert section in readme, f"README lost the {section!r} section"
    for target in ("docs/PRD.md", "docs/ARCHITECTURE.md", "CONTRIBUTING.md", "LICENSE"):
        assert (ROOT / target).is_file(), f"README links {target} but it does not exist"


def test_documented_files_exist():
    for target in ("CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE"):
        assert (ROOT / target).is_file(), f"missing {target}"


def test_plugin_folder_is_self_contained():
    plugin = ROOT / "lightroom" / "Kestrel.lrplugin"
    assert (plugin / "Info.lua").is_file()
    info = (plugin / "Info.lua").read_text(encoding="utf-8")
    for module in ("KestrelCore", "KestrelApply", "KestrelWorkflow", "KestrelJson",
                   "KestrelDialogs", "KestrelMenus", "KestrelBatch"):
        assert (plugin / f"{module}.lua").is_file(), f"plug-in module missing: {module}"
        assert module in info or module in "".join(
            p.name for p in plugin.glob("*.lua")), f"{module} is never referenced"
    assert "require(" not in info, "the Lightroom sandbox has no require()"
