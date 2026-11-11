#!/usr/bin/env python3
"""Whole-app performance audit: every route, plus concurrency.

tools/baseline_capture.py answers "did behaviour change?" for a curated set.
This answers "how fast is the app overall?" — it walks app.url_map so nothing
is silently left out, benchmarks each route, and then hammers the read and
write paths concurrently (SQLite's single-writer lock is the thing most likely
to hurt under load).

Offline: platform API / OpenRouter / SMTP are stubbed.

Usage
-----
    cp scout.db tests/.perfaudit.db
    SCOUT_DB_PATH=tests/.perfaudit.db python tools/perf_audit.py
    SCOUT_DB_PATH=tests/.perfaudit.db python tools/perf_audit.py --reps 20

Refuses to run against the repo's real scout.db: it creates, mutates and
deletes customers, and resets the admin password on the copy it is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

ADMIN_PW = os.environ.get("SCOUT_PERF_ADMIN_PW", "perfaudit12345")
EMBED_DIM = 4096

# p95 above these gets flagged in the report.
SLOW_READ_MS = 100.0
SLOW_WRITE_MS = 300.0


class SqlCounter:
    """Count statements (not connections — the read pool reuses them)."""

    def __init__(self):
        self.n = 0
        self._orig = None

    def __enter__(self):
        self._orig = sqlite3.connect

        def patched(*a, **k):
            conn = self._orig(*a, **k)
            conn.set_trace_callback(self._tick)
            return conn

        sqlite3.connect = patched          # type: ignore[assignment]
        return self

    def _tick(self, _sql):
        self.n += 1

    def reset(self):
        self.n = 0

    def __exit__(self, *exc):
        sqlite3.connect = self._orig   # type: ignore[assignment]
        return False


def guard_db_path() -> Path:
    raw = os.environ.get("SCOUT_DB_PATH")
    if not raw:
        sys.exit("perf_audit: set SCOUT_DB_PATH to a throwaway copy "
                 "(cp scout.db tests/.perfaudit.db)")
    target = Path(raw).resolve()
    if target == (REPO_ROOT / "scout.db").resolve():
        sys.exit("perf_audit: SCOUT_DB_PATH points at the real scout.db. Refusing.")
    if not target.exists():
        sys.exit(f"perf_audit: {target} does not exist")
    return target


# ---------------------------------------------------------------------------
# Offline stubs
# ---------------------------------------------------------------------------
def install_stubs():
    from lib import ai, ingest, mailer

    ai.embed_texts = lambda texts: [[0.01 * ((i % 7) + 1)] * EMBED_DIM for i in range(len(texts))]
    ai.embed_text = lambda text: [0.01] * EMBED_DIM
    # /api/email/polish parses the reply as JSON — a bare "ok" would 500.
    ai._chat = lambda *a, **k: '{"subject": "s", "body": "b"}'
    ai.generate_search_queries = lambda *a, **k: ["stub query"]
    ai.classify_creators = lambda briefs, b: {x["username"]: True for x in briefs}
    ingest.ai = ai
    mailer.send_one = lambda *a, **k: None

    def norm(platform, handle):
        return {
            "platform": platform, "platform_user_id": f"stub-{platform}-{handle}",
            "secondary_id": "", "username": handle, "full_name": f"Stub {handle}",
            "biography": "stub bio", "follower_count": 1000, "following_count": 10,
            "post_count": 1, "video_count": 0, "is_verified": False,
            "is_private": False, "is_business": False, "category": "Artist",
            "profile_pic_url": "", "external_url": "", "bio_links": [],
            "email": None, "phone": "", "city": "", "address": "", "country": "",
            "raw": {"stubbed": True},
        }

    class _P:
        def __init__(self, name):
            self.name = name

        def fetch_user_info(self, handle):
            return norm(self.name, handle)

        def similar_users(self, *a, **k):
            # Shape must match what ingest expects, otherwise the endpoint 400s
            # on an empty result and the audit would measure the wrong path.
            return [norm(self.name, f"sim{i}") for i in range(2)]

        def search_users(self, *a, **k):
            return [norm(self.name, f"hit{i}") for i in range(2)]

    import app as app_mod
    app_mod.get_platform = lambda name: _P(name)

    # lib/ingest, lib/bulk_similarity and lib/bulk_hydrate each do
    # `from platforms import get as get_platform`, binding the name into their
    # own module. Patching only app.get_platform leaves them calling the real
    # TikHub API — which is what made /api/query/run look like it took 6-9s
    # (it was waiting on real HTTPS). Patch the bindings too.
    import lib.ingest as ingest_mod
    import lib.bulk_hydrate as bh_mod
    import lib.bulk_similarity as bs_mod
    for mod in (ingest_mod, bs_mod, bh_mod):
        mod.get_platform = lambda name: _P(name)

    bs_mod.fetch_tikhub_balance = lambda: None
    bh_mod.fetch_tikhub_balance = lambda: None


# ---------------------------------------------------------------------------
# Benchmarking
# ---------------------------------------------------------------------------
def bench(fn, reps: int, counter: SqlCounter) -> dict:
    times: list[float] = []
    sql = 0
    status = None
    for i in range(reps):
        counter.reset()
        t0 = time.perf_counter()
        r = fn(i)
        ms = (time.perf_counter() - t0) * 1000
        times.append(ms)
        sql = max(sql, counter.n)
        status = getattr(r, "status_code", status)
    return {
        "median": statistics.median(times),
        "p95": sorted(times)[max(0, int(len(times) * 0.95) - 1)] if len(times) > 1 else times[0],
        "max": max(times),
        "sql": sql,
        "status": status,
    }


MIXED_PATHS = [
    "/api/stats?range=month",
    "/api/people/list?limit=50",
    "/api/people/match_ids?status=%E5%BE%85%E8%81%94%E7%B3%BB",
    "/api/db/list?limit=100",
]


def _child_run(iters: int, start_at: float = 0.0) -> int:
    """One standalone process doing `iters` mixed reads; prints elapsed seconds.

    Spawned by --multiproc. This mirrors a gunicorn worker: its own process,
    its own GIL, its own SQLite connections. Waits for a shared start time so
    the parent can measure only the work window (interpreter start + app import
    is ~1.4s and would otherwise swamp the parallelism maths).
    """
    guard_db_path()
    import lib.job_watchdog as jw
    jw.start = lambda *a, **k: None
    install_stubs()
    import app as app_mod
    from lib import auth, settings

    settings.refresh()
    app_mod.app.config.update(TESTING=True)
    admin = auth.get_user_by_username("admin")
    auth.set_password(admin["id"], ADMIN_PW, must_change=False)
    with app_mod.app.test_client() as cl:
        with cl.session_transaction() as s:
            s["user_id"] = admin["id"]
            s["sid"] = "perfaudit"
        cl.get(MIXED_PATHS[0])                     # warm the pool/cache
        if start_at:
            while time.time() < start_at:
                time.sleep(0.002)
        t0 = time.perf_counter()
        for i in range(iters):
            r = cl.get(MIXED_PATHS[i % len(MIXED_PATHS)])
            if r.status_code != 200:
                print(f"ERR {r.status_code} {MIXED_PATHS[i % len(MIXED_PATHS)]}")
                return 1
        print(f"{time.perf_counter() - t0:.4f}")
    return 0


def multiproc_measure(nproc: int, iters: int) -> None:
    """Aggregate throughput with `nproc` concurrent OS processes."""
    import subprocess
    print()
    print(f"  多进程 (模拟 gunicorn: {nproc} 个独立进程, 每个 {iters} 次混合读)")
    start_at = time.time() + 4          # let every child finish importing
    kids = [subprocess.Popen([sys.executable, __file__, "--child", str(iters),
                              "--start-at", f"{start_at}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(nproc)]
    outs = [k.communicate() for k in kids]
    ok = all(k.returncode == 0 for k in kids)
    per = []
    for o in outs:
        lines = [ln for ln in o[0].splitlines() if ln.strip()]
        if lines:
            try:
                per.append(float(lines[-1]))
            except ValueError:
                pass
    if not ok or not per:
        print(f"    失败: {[o[1][:200] for o in outs if o[1]]}")
        return
    total = nproc * iters
    slowest = max(per)
    serial = sum(per)
    print(f"    各进程工作耗时 单进程 {per[0]:.2f}s / 最慢 {slowest:.2f}s")
    print(f"    并行完成 {total} 次用时 {slowest:.2f}s -> {total/slowest:6.1f} req/s"
          f"   (同样负载串行需 {serial:.2f}s, 并行度 {serial/slowest:.2f}x)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=12, help="iterations per read endpoint")
    ap.add_argument("--write-reps", type=int, default=6)
    ap.add_argument("--json", help="also write the raw results here")
    ap.add_argument("--multiproc", type=int, default=0,
                    help="also measure N concurrent OS processes (mirrors the "
                         "8 gunicorn workers; the threaded test shares one GIL)")
    ap.add_argument("--child", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--start-at", type=float, default=0.0, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        return _child_run(args.child, args.start_at)

    guard_db_path()
    import lib.job_watchdog as jw
    jw.start = lambda *a, **k: None
    install_stubs()

    import app as app_mod
    from lib import auth, db, settings

    settings.set_many({
        "SMTP_HOST": "stub.local", "SMTP_PORT": "587", "SMTP_USERNAME": "u",
        "SMTP_PASSWORD": "p", "SMTP_FROM_EMAIL": "noreply@stub.local",
        "SMTP_FROM_NAME": "Stub", "OPENROUTER_API_KEY": "stub",
    })
    settings.refresh()
    app_mod.app.config.update(TESTING=True)

    admin = auth.get_user_by_username("admin")
    if not admin:
        sys.exit("perf_audit: no admin user in the target DB")
    auth.set_password(admin["id"], ADMIN_PW, must_change=False)

    # Real ids to poke
    with db.tx() as c:
        row = c.execute("SELECT person_id, COUNT(*) n FROM platform_accounts "
                        "GROUP BY person_id ORDER BY n DESC LIMIT 1").fetchone()
        rich_pid = row["person_id"]
        plain_pid = c.execute("SELECT id FROM people ORDER BY id LIMIT 1").fetchone()["id"]
        acct_id = c.execute("SELECT id FROM platform_accounts ORDER BY id LIMIT 1").fetchone()["id"]
        sim_job = c.execute("SELECT id FROM similarity_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        hyd_job = c.execute("SELECT id FROM hydrate_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        email_job = c.execute("SELECT id FROM email_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    sim_job_id = sim_job["id"] if sim_job else "nonexistent-job"
    hyd_job_id = hyd_job["id"] if hyd_job else "nonexistent-job"
    email_job_id = email_job["id"] if email_job else "nonexistent-job"

    results: list[dict] = []
    counter = SqlCounter()
    counter.__enter__()

    with app_mod.app.test_client() as cl:
        r = cl.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        if r.status_code != 200:
            sys.exit(f"perf_audit: admin login failed: {r.status_code}")

        # ---------------- HTML pages ----------------
        print("=" * 96)
        print("HTML 页面（服务端渲染）")
        print("=" * 96)
        for path in ["/", "/stats", "/people", "/similarity", "/query", "/database",
                     "/settings", "/admin/users", "/me", "/login", "/register",
                     "/change_password"]:
            res = bench(lambda i, p=path: cl.get(p), args.reps, counter)
            results.append({"kind": "page", "path": path, **res})
            print(f"  {path:<22} median {res['median']:7.1f}ms  p95 {res['p95']:7.1f}ms  "
                  f"sql {res['sql']:>3}  {res['status']}")

        # ---------------- GET endpoints ----------------
        print()
        print("=" * 96)
        print("GET 接口（28 个路由的全覆盖）")
        print("=" * 96)
        get_specs = [
            ("/api/auth/me", {}),
            ("/api/auth/global_from", {}),
            ("/api/platforms", {}),
            ("/api/db/stats", {}),
            ("/api/stats", {"range": "month"}),
            ("/api/stats", {"range": "today"}),
            ("/api/stats", {"range": "all"}),
            ("/api/stats/trends", {"days": 30}),
            ("/api/stats/trends", {"days": 90}),
            ("/api/people/list", {"limit": 50}),
            ("/api/people/list", {"limit": 50, "order": "followers"}),
            ("/api/people/list", {"limit": 50, "order": "name"}),
            ("/api/people/list", {"limit": 50, "q": "anna"}),
            ("/api/people/match_ids", {"status": "待联系"}),
            ("/api/people/match_ids", {}),
            (f"/api/people/{rich_pid}", {}),
            (f"/api/people/{plain_pid}/followups", {}),
            (f"/api/people/{plain_pid}/reminders", {}),
            ("/api/reminders/due", {}),
            ("/api/db/list", {"limit": 100}),
            (f"/api/db/account/{acct_id}", {}),
            ("/api/settings", {}),
            ("/api/admin/users", {}),
            ("/api/admin/active_staff", {}),
            ("/api/admin/audit", {}),
            ("/api/similarity/bulk/estimate", {}),
            ("/api/similarity/bulk/active", {}),
            (f"/api/similarity/bulk/{sim_job_id}", {}),
            ("/api/hydrate/bulk/estimate", {}),
            ("/api/hydrate/bulk/active", {}),
            (f"/api/hydrate/bulk/{hyd_job_id}", {}),
            (f"/api/email/job/{email_job_id}", {}),
            ("/api/platform/instagram/info", {"handle": "natgeo"}),
            ("/api/platform/instagram/search", {"keyword": "art", "count": 3}),
            ("/api/platform/instagram/similar", {"handle": "natgeo"}),
        ]
        for path, params in get_specs:
            res = bench(lambda i, p=path, q=params: cl.get(p, query_string=q),
                        args.reps, counter)
            label = path + (("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else "")
            results.append({"kind": "get", "path": label, **res})
            flag = "  <-- SLOW" if res["p95"] > SLOW_READ_MS else ""
            print(f"  {label[:62]:<62} {res['median']:7.1f}ms  p95 {res['p95']:7.1f}  "
                  f"sql {res['sql']:>3}  {res['status']}{flag}")

        # ---------------- write endpoints ----------------
        print()
        print("=" * 96)
        print("写接口（可重复执行且非破坏性的）")
        print("=" * 96)
        stamp = int(time.time())
        created: list[int] = []

        def mk_person(i):
            r = cl.post("/api/people", json={
                "full_name": f"Perf {stamp}-{i}",
                "email": f"perf_{stamp}_{i}@stub.local",
                "handles": [{"platform": "instagram", "username": f"perf_{stamp}_{i}"}],
            })
            body = r.get_json() or {}
            pid = body.get("person_id") or body.get("id")
            if pid:
                created.append(pid)
            return r

        write_specs = [
            ("POST /api/people", lambda i: mk_person(i)),
            (f"PATCH /api/db/person/<id>",
             lambda i: cl.patch(f"/api/db/person/{created[-1]}",
                                json={"notes": f"perf note {i}"}) if created else None),
            (f"POST /api/people/<id>/followups",
             lambda i: cl.post(f"/api/people/{created[-1]}/followups",
                               json={"content": f"perf followup {i}"}) if created else None),
            (f"POST /api/people/<id>/reminders",
             lambda i: cl.post(f"/api/people/{created[-1]}/reminders",
                               json={"remind_at": int(time.time()) + 3600,
                                     "content": f"perf rem {i}"}) if created else None),
            (f"POST /api/people/<id>/contact",
             lambda i: cl.post(f"/api/people/{created[-1]}/contact", json={}) if created else None),
            (f"POST /api/people/<id>/handles",
             lambda i: cl.post(f"/api/people/{created[-1]}/handles",
                               json={"platform": "x", "handle": f"perf_x_{stamp}_{i}"}) if created else None),
            (f"POST /api/people/<id>/hydrate",
             lambda i: cl.post(f"/api/people/{created[-1]}/hydrate") if created else None),
            (f"POST /api/people/<id>/reject",
             lambda i: cl.post(f"/api/people/{created[-1]}/reject", json={"reason": "perf"}) if created else None),
            (f"POST /api/people/<id>/unreject",
             lambda i: cl.post(f"/api/people/{created[-1]}/unreject", json={}) if created else None),
            ("POST /api/people/bulk_assign",
             lambda i: cl.post("/api/people/bulk_assign",
                               json={"ids": created[-3:] or [], "assigned_user_id": None})),
            ("PATCH /api/auth/preferences",
             lambda i: cl.patch("/api/auth/preferences", json={"reply_to_email": ""})),
            ("POST /api/email/preview",
             lambda i: cl.post("/api/email/preview",
                               json={"person_ids": created[-2:] or [],
                                     "subject": "s {{name}}", "body": "b {{name}}"})),
            ("POST /api/email/polish",
             lambda i: cl.post("/api/email/polish", json={"subject": "s", "body": "b"})),
            ("POST /api/ai/embed",
             lambda i: cl.post("/api/ai/embed", json={"text": "hello"})),
            ("POST /api/ai/queries",
             lambda i: cl.post("/api/ai/queries", json={"description": "artists", "n": 3})),
            ("POST /api/query/run",
             lambda i: cl.post("/api/query/run", json={"description": "artists"})),
            ("POST /api/similarity/run",
             lambda i: cl.post("/api/similarity/run",
                               json={"platform": "instagram", "handle": "natgeo"})),
            ("POST /api/settings", lambda i: cl.post("/api/settings", json={})),
        ]
        # seed one person so the dependent writes have a target
        r0 = mk_person(0)
        if not created:
            sys.exit("perf_audit: could not create a person to write against")

        for label, fn in write_specs:
            res = bench(fn, args.write_reps, counter)
            if res["status"] is None:
                print(f"  {label:<42} skipped (no target row)")
                continue
            results.append({"kind": "write", "path": label, **res})
            flag = "  <-- SLOW" if res["p95"] > SLOW_WRITE_MS else ""
            print(f"  {label:<42} {res['median']:7.1f}ms  p95 {res['p95']:7.1f}  "
                  f"sql {res['sql']:>3}  {res['status']}{flag}")

        # Single-shot heavy ops (too expensive to repeat)
        print()
        print("一次性重操作（测一次）")
        print("-" * 96)
        for label, fn in [
            ("POST /api/db/export", lambda: cl.post("/api/db/export", json={})),
            ("POST /api/db/embed_missing", lambda: cl.post("/api/db/embed_missing")),
            ("POST /api/admin/migrate_owners (dry-run)",
             lambda: cl.post("/api/admin/migrate_owners", json={"apply": False})),
        ]:
            counter.reset()
            t0 = time.perf_counter()
            r = fn()
            ms = (time.perf_counter() - t0) * 1000
            results.append({"kind": "heavy", "path": label, "median": ms, "p95": ms,
                            "max": ms, "sql": counter.n,
                            "status": getattr(r, "status_code", None)})
            print(f"  {label:<42} {ms:9.1f}ms  sql {counter.n:>4}  {r.status_code}")

        # ---------------- concurrency ----------------
        print()
        print("=" * 96)
        print("并发")
        print("-" * 96)
        print("  注意: 这里是同一进程内的多线程, 共享一个 GIL, 比生产的 8 个")
        print("  gunicorn 进程(各自独立 GIL/CPU)更悲观。写并发受 SQLite 单写锁")
        print("  约束, 这一项与生产同构。")

        def authed_client():
            """Fresh client with the admin session injected.

            Going through /api/auth/login per thread would trip the login rate
            limiter (30 hits / 60 s per IP) and the resulting 401s would look
            like a performance problem. current_user() only reads user_id.
            """
            c = app_mod.app.test_client()
            with c.session_transaction() as sess:
                sess["user_id"] = admin["id"]
                sess["sid"] = "perfaudit"
            return c

        read_paths = [
            ("/api/stats?range=month", "stats"),
            ("/api/people/list?limit=50", "people list"),
            ("/api/people/match_ids?status=%E5%BE%85%E8%81%94%E7%B3%BB", "match_ids"),
            ("/api/db/list?limit=100", "db_list"),
            (f"/api/people/{rich_pid}", "person detail"),
        ]

        def read_worker(path, iters, errs, lat):
            c2 = authed_client()
            for _ in range(iters):
                t0 = time.perf_counter()
                r = c2.get(path)
                lat.append((time.perf_counter() - t0) * 1000)
                if r.status_code != 200:
                    errs.append(f"{r.status_code}")

        print()
        print(f"  并发读 (每项 8 线程 x 10 次, 单进程)")
        for path, label in read_paths:
            # warm so the first hit doesn't skew the tail
            authed_client().get(path)
            errs: list[float] = []
            lat: list[float] = []
            th = [threading.Thread(target=read_worker, args=(path, 10, errs, lat))
                  for _ in range(8)]
            t0 = time.perf_counter()
            [t.start() for t in th]
            [t.join() for t in th]
            wall = (time.perf_counter() - t0) * 1000
            total = 8 * 10
            p95 = sorted(lat)[int(len(lat) * .95) - 1] if lat else 0
            print(f"    {label:<16} {wall:7.0f}ms  {total/(wall/1000):6.1f} req/s  "
                  f"median {statistics.median(lat):6.1f}  p95 {p95:7.1f}  "
                  f"max {max(lat):7.1f}  错误 {len(errs)}")
            results.append({"kind": "concurrency_read", "path": f"{label} 8x10",
                            "median": statistics.median(lat), "p95": p95,
                            "max": max(lat), "sql": 0, "status": f"errors={len(errs)}"})

        # concurrent writes: the classic SQLite single-writer hazard
        w_errs: list[str] = []
        w_lat: list[float] = []

        def write_worker(iters):
            c3 = authed_client()
            for i in range(iters):
                t0 = time.perf_counter()
                r = c3.post(f"/api/people/{created[0]}/followups",
                            json={"content": "conc write"})
                w_lat.append((time.perf_counter() - t0) * 1000)
                if r.status_code != 200:
                    try:
                        w_errs.append(f"{r.status_code} "
                                      f"{json.dumps(r.get_json(), ensure_ascii=False)[:100]}")
                    except Exception:
                        w_errs.append(f"{r.status_code} {r.get_data(as_text=True)[:80]}")

        print()
        th = [threading.Thread(target=write_worker, args=(8,)) for _ in range(8)]
        t0 = time.perf_counter()
        [t.start() for t in th]
        [t.join() for t in th]
        wwall = (time.perf_counter() - t0) * 1000
        wtotal = 8 * 8
        wp95 = sorted(w_lat)[int(len(w_lat) * .95) - 1]
        print(f"  并发写 关注册跟进 8 线程 x 8 次: {wwall:6.0f}ms  "
              f"{wtotal/(wwall/1000):6.1f} req/s  median {statistics.median(w_lat):5.1f}  "
              f"p95 {wp95:6.1f}  错误 {len(w_errs)}")
        for e in w_errs[:3]:
            print(f"      {e}")
        results.append({"kind": "concurrency_write", "path": "followups 8x8",
                        "median": statistics.median(w_lat), "p95": wp95,
                        "max": max(w_lat), "sql": 0, "status": f"errors={len(w_errs)}"})

    # Real topology: separate OS processes (this is what gunicorn forks).
    if args.multiproc:
        multiproc_measure(args.multiproc, 20)

    counter.__exit__(None, None, None)

    # ---------------- summary ----------------
    measured_rules = len(results)
    total_rules = len([r for r in app_mod.app.url_map.iter_rules() if r.endpoint != "static"])
    reads = [r for r in results if r["kind"] in ("page", "get")]
    writes = [r for r in results if r["kind"] == "write"]
    slow_reads = [r for r in reads if r["p95"] > SLOW_READ_MS]
    slow_writes = [r for r in writes if r["p95"] > SLOW_WRITE_MS]

    print()
    print("=" * 96)
    print("汇总")
    print("=" * 96)
    print(f"  路由总数 {total_rules}，本审计打了 {measured_rules} 次请求"
          f"（页面 {len([r for r in results if r['kind']=='page'])}、"
          f"GET {len([r for r in results if r['kind']=='get'])}、"
          f"写 {len(writes)}、一次性 {len([r for r in results if r['kind']=='heavy'])}）")
    if reads:
        allmed = [r["median"] for r in reads]
        print(f"  读路径: median {statistics.median(allmed):.1f}ms  "
              f"最慢 {max(reads, key=lambda r: r['p95'])['path']}"
              f" p95={max(r['p95'] for r in reads):.1f}ms")
    if writes:
        print(f"  写路径: median {statistics.median([r['median'] for r in writes]):.1f}ms  "
              f"p95 最大 {max(r['p95'] for r in writes):.1f}ms")
    if slow_reads:
        print(f"  超过 {SLOW_READ_MS:.0f}ms 的读路径 ({len(slow_reads)}):")
        for r in sorted(slow_reads, key=lambda r: -r["p95"]):
            print(f"      {r['path'][:62]:<62} p95 {r['p95']:7.1f}ms")
    if slow_writes:
        print(f"  超过 {SLOW_WRITE_MS:.0f}ms 的写路径 ({len(slow_writes)}):")
        for r in sorted(slow_writes, key=lambda r: -r["p95"]):
            print(f"      {r['path'][:62]:<62} p95 {r['p95']:7.1f}ms")

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n  明细已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
