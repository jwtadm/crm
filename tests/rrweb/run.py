"""Run one rrweb regression test (or all of them).

Each test is a directory containing:
  - `seed.py`       — module exposing `seed()` (and optionally `teardown()`)
  - `steps.txt`     — rrweb step file consumed by record.py
  - `FRONTMATTER`   — first line of steps.txt, format:
                      `# COVERAGE: <page or feature>; INTRODUCED: <commit-ish>`

Usage:
  python tests/rrweb/run.py                  # run every test under tests/rrweb/
  python tests/rrweb/run.py NAME [NAME ...]  # run only those tests
  python tests/rrweb/run.py --list           # list tests + their FRONTMATTER

The runner:
  1. Boots the dev Flask app via tools/dev_with_ig_stub.py if nothing is on
     localhost:5001 yet.
  2. Calls `_lib.seed.wipe_fixtures()` + `_lib.seed.ensure_admin()` + each
     test's `seed()` before recording.
  3. Drives record.py with the eval flag enabled (caption banners) + --share.
  4. Reads steps.jsonl to decide pass/fail (NEVER greps transcript.md).
  5. Calls teardown() if present.

Exit code: 0 if every requested test passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# CRITICAL: point at the harness DB BEFORE importing anything that touches
# lib.db. Without this, the seed helpers' _assert_test_db guard fires, and
# more importantly running the suite on a server that holds prod scout.db
# could otherwise scramble admin's password via ensure_admin().
from tests.rrweb._lib import config  # noqa: E402
os.environ["SCOUT_DB_PATH"] = str(config.TEST_DB_PATH)
config.TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

from tests.rrweb._lib import seed as seedlib  # noqa: E402

RECORD_SCRIPT = Path.home() / ".claude/skills/fingertap:rrweb-record/scripts/record.py"

# record.py needs playwright. .venv may or may not have it; system python3 does
# on this box. Prefer whichever interpreter actually imports playwright.
def _record_python() -> str:
    import shutil
    for candidate in ("python3", sys.executable, "/usr/bin/python3"):
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if not path or not os.path.exists(path):
            continue
        rc = subprocess.run(
            [path, "-c", "import playwright"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode
        if rc == 0:
            return path
    raise RuntimeError("no python on PATH has the `playwright` module installed")


def _list_tests() -> list[Path]:
    return sorted(
        d for d in TESTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_")
        and (d / "steps.txt").exists() and (d / "seed.py").exists()
    )


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _start_app() -> subprocess.Popen | None:
    if _port_open("127.0.0.1", config.APP_PORT):
        return None
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools/dev_with_ig_stub.py")],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _port_open("127.0.0.1", config.APP_PORT):
            return proc
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("dev_with_ig_stub failed to bind localhost:5001")


def _load_seed_module(test_dir: Path):
    spec = importlib.util.spec_from_file_location(
        f"tests.rrweb.{test_dir.name}.seed", test_dir / "seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _frontmatter(steps_file: Path) -> str:
    with steps_file.open() as f:
        return f.readline().strip().lstrip("# ").strip()


def _run_one(test_dir: Path, *, share: bool) -> dict:
    name = test_dir.name
    out_dir = Path(f"/tmp/rrweb-{name}")
    seedlib.wipe_fixtures()
    seedlib.ensure_admin()
    mod = _load_seed_module(test_dir)
    if hasattr(mod, "seed"):
        mod.seed()

    cmd = [
        _record_python(), str(RECORD_SCRIPT), "run",
        "--output", str(out_dir),
        "--title", f"Scout regression · {name}",
        "--intent", _frontmatter(test_dir / "steps.txt"),
        "--steps-file", str(test_dir / "steps.txt"),
        "--allow-eval",
    ]
    if share:
        cmd.append("--share")
    print(f"\n▶ {name}")
    rc = subprocess.run(cmd, cwd=str(ROOT))

    summary = {"name": name, "status": "fail", "share_url": None,
               "ok": 0, "fail": 0, "assertions_pass": 0, "assertions_fail": 0}
    steps_path = out_dir / "steps.jsonl"
    if steps_path.exists():
        ok = fail = a_pass = a_fail = 0
        for line in steps_path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            is_assert = row["verb"].startswith("assert")
            if row["status"] == "fail":
                (a_fail := a_fail + 1) if is_assert else (fail := fail + 1)  # type: ignore[misc]
            else:
                (a_pass := a_pass + 1) if is_assert else (ok := ok + 1)  # type: ignore[misc]
        summary.update(ok=ok, fail=fail, assertions_pass=a_pass, assertions_fail=a_fail,
                       status=("pass" if fail == 0 and a_fail == 0 else "fail"))
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            summary["share_url"] = (meta.get("share") or {}).get("share_url")
        except Exception:
            pass

    if hasattr(mod, "teardown"):
        try:
            mod.teardown()
        except Exception as e:
            print(f"  teardown error: {e}")
    return summary


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="test directory names; default = all")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-share", action="store_true", help="skip cloudflared tunnel")
    args = ap.parse_args(argv)

    available = _list_tests()
    if args.list:
        for d in available:
            print(f"  {d.name:36s}  {_frontmatter(d / 'steps.txt')}")
        return 0

    targets = available if not args.names else [d for d in available if d.name in args.names]
    missing = set(args.names) - {d.name for d in available}
    if missing:
        print("no such test(s):", ", ".join(sorted(missing)), file=sys.stderr)
        return 2

    proc = _start_app()
    try:
        results = [_run_one(d, share=not args.no_share) for d in targets]
    finally:
        if proc is not None:
            proc.terminate()

    print("\n=== summary ===")
    all_pass = True
    for r in results:
        mark = "✓" if r["status"] == "pass" else "✗"
        steps = f"{r['ok']}/{r['ok'] + r['fail']}"
        asserts = f"{r['assertions_pass']}/{r['assertions_pass'] + r['assertions_fail']}"
        url = f"  {r['share_url']}" if r.get("share_url") else ""
        print(f"  {mark} {r['name']:36s} steps {steps:>7}  asserts {asserts:>5}{url}")
        if r["status"] != "pass":
            all_pass = False
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
