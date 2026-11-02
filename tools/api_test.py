"""End-to-end backend smoke test — exercises every API the frontend calls.

Run this against a live `python app.py` server. It will WIPE the DB if --reset.

Usage:
    .venv/bin/python tools/api_test.py            # run against http://127.0.0.1:5001
    .venv/bin/python tools/api_test.py --reset    # drop DB before running
    .venv/bin/python tools/api_test.py --base http://localhost:5001
"""
from __future__ import annotations
import argparse
import json
import sys
import time

import requests

DEFAULT_BASE = "http://127.0.0.1:5001"


class TestRig:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        # Bypass any local SOCKS/HTTP proxy — we hit localhost only.
        self.session = requests.Session()
        self.session.trust_env = False

    def _req(self, method: str, path: str, **kw):
        url = self.base + path
        r = self.session.request(method, url, timeout=240, **kw)
        return r

    def step(self, label: str, ok: bool, detail: str = ""):
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if ok:
            self.passed.append(label)
        else:
            self.failed.append((label, detail))

    # ---- API helpers ----
    def get_json(self, path, params=None):
        r = self._req("GET", path, params=params)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        try:
            return r.json(), None
        except Exception as e:
            return None, f"json decode: {e}"

    def post_json(self, path, body=None, expect_status=200):
        r = self._req("POST", path, json=body or {})
        if r.status_code != expect_status:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        try:
            return r.json(), None
        except Exception as e:
            return None, f"json decode: {e}"

    def patch_json(self, path, body):
        r = self._req("PATCH", path, json=body)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        return r.json(), None

    def delete(self, path):
        r = self._req("DELETE", path)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        return r.json(), None


def run(rig: TestRig, *, reset: bool):
    print(f"\n=== Smoke test against {rig.base} ===\n")

    # ----- 1. server reachable -----
    print("[1] connectivity")
    d, err = rig.get_json("/api/db/stats")
    rig.step("GET /api/db/stats", err is None and d is not None, err or f"stats={d}")
    if err:
        return  # nothing else will work

    d, err = rig.get_json("/api/platforms")
    rig.step("GET /api/platforms", err is None and "instagram" in (d or {}).get("info_capable", []),
             err or f"caps={d}")

    # ----- 2. clear DB if requested -----
    if reset:
        print("\n[2] reset DB")
        d, err = rig.post_json("/api/db/clear", {"confirm": True})
        rig.step("POST /api/db/clear", err is None, err or f"after={d.get('stats') if d else '?'}")

    # ----- 3. raw platform calls (no DB writes) -----
    print("\n[3] raw platform calls")
    for plat, handle in [("instagram", "natgeo"), ("tiktok", "cookingwithayeh"), ("x", "elonmusk")]:
        d, err = rig.get_json(f"/api/platform/{plat}/info", params={"handle": handle})
        ok = err is None and (d or {}).get("normalized", {}).get("username") == handle.lower() or \
             (d or {}).get("normalized", {}).get("username") == handle
        rig.step(f"GET /api/platform/{plat}/info?handle={handle}", ok,
                 err or f"got @{(d or {}).get('normalized', {}).get('username')}")

    for plat, kw in [("instagram", "fitness"), ("tiktok", "vegan"), ("x", "indie hacker")]:
        d, err = rig.get_json(f"/api/platform/{plat}/search", params={"keyword": kw, "count": 3})
        ok = err is None and isinstance((d or {}).get("results"), list)
        rig.step(f"GET /api/platform/{plat}/search?keyword={kw}", ok,
                 err or f"{len(d['results']) if d else 0} results")

    # ----- 4. ai endpoints -----
    print("\n[4] AI endpoints")
    d, err = rig.post_json("/api/ai/embed", {"text": "plant-based food"})
    ok = err is None and len((d or {}).get("vector", [])) == 1024
    rig.step("POST /api/ai/embed (single)", ok, err or f"dim={len((d or {}).get('vector', []))}")

    d, err = rig.post_json("/api/ai/embed", {"texts": ["a", "b"]})
    ok = err is None and len((d or {}).get("vectors", [])) == 2
    rig.step("POST /api/ai/embed (batch)", ok, err or f"got {len((d or {}).get('vectors', []))} vectors")

    d, err = rig.post_json("/api/ai/queries", {"description": "Plant-based food bloggers", "n": 4})
    ok = err is None and len((d or {}).get("queries", [])) >= 1
    rig.step("POST /api/ai/queries", ok, err or f"queries={(d or {}).get('queries')}")

    # ----- 5. import seeds -----
    print("\n[5] import seeds")
    nat_id = None
    for plat, handle in [("instagram", "natgeo"), ("tiktok", "cookingwithayeh")]:
        d, err = rig.post_json("/api/db/import_handle", {"platform": plat, "handle": handle})
        ok = err is None and (d or {}).get("account", {}).get("id")
        if ok and plat == "instagram":
            nat_id = d["account"]["id"]
        rig.step(f"POST /api/db/import_handle {plat}/{handle}", ok,
                 err or f"id={(d or {}).get('account', {}).get('id')}")

    # Re-import same handle should NOT create duplicate
    d, err = rig.post_json("/api/db/import_handle", {"platform": "instagram", "handle": "natgeo"})
    ok = err is None and (d or {}).get("created") is False
    rig.step("POST /api/db/import_handle (dedup re-import)", ok, err or f"created={d.get('created') if d else '?'}")

    # ----- 6. db list / filter / rerank -----
    print("\n[6] DB browse")
    d, err = rig.get_json("/api/db/list", params={"limit": 50})
    ok = err is None and len((d or {}).get("accounts", [])) >= 2
    rig.step("GET /api/db/list", ok, err or f"{len((d or {}).get('accounts', []))} accounts")

    d, err = rig.get_json("/api/db/list", params={"platforms": "instagram"})
    ok = err is None and all(a["platform"] == "instagram" for a in (d or {}).get("accounts", []))
    rig.step("GET /api/db/list?platforms=instagram", ok, err or f"{len((d or {}).get('accounts', []))} IG accts")

    d, err = rig.get_json("/api/db/list", params={"text": "nat"})
    ok = err is None and any("nat" in a["username"].lower() for a in (d or {}).get("accounts", []))
    rig.step("GET /api/db/list?text=nat", ok, err or f"{len((d or {}).get('accounts', []))} matches")

    # rerank by description
    d, err = rig.get_json("/api/db/list", params={"description": "wildlife photography"})
    accounts = (d or {}).get("accounts", [])
    ok = err is None and (len(accounts) == 0 or "score" in accounts[0])
    rig.step("GET /api/db/list?description=...", ok, err or f"top score={accounts[0].get('score') if accounts else '-'}")

    # ----- 7. account detail + person CRM -----
    print("\n[7] account detail + person CRM")
    if nat_id:
        d, err = rig.get_json(f"/api/db/account/{nat_id}")
        ok = err is None and (d or {}).get("account", {}).get("id") == nat_id
        rig.step(f"GET /api/db/account/{nat_id}", ok,
                 err or f"siblings={len((d or {}).get('other_accounts_for_person', []))}")

        person_id = (d or {}).get("person", {}).get("id")
        if person_id:
            d, err = rig.patch_json(f"/api/db/person/{person_id}",
                                    {"status": "contacted", "notes": "DM'd 2026-04-24",
                                     "tags": ["wildlife", "tier-1"]})
            ok = err is None and (d or {}).get("person", {}).get("status") == "contacted"
            rig.step(f"PATCH /api/db/person/{person_id}", ok, err or f"status={d.get('person', {}).get('status')}")

    # ----- 8. similarity (DB seed → similar API) -----
    print("\n[8] similarity discovery")
    if nat_id:
        d, err = rig.post_json("/api/similarity/run",
                               {"seed_account_ids": [nat_id], "count": 8})
        ok = err is None and (d or {}).get("considered", 0) > 0
        rig.step("POST /api/similarity/run (natgeo seed)", ok,
                 err or f"considered={d.get('considered')} new={len(d.get('new_accounts', []))} "
                        f"cross={len(d.get('cross_platform', []))} skip_pre={d.get('skipped_pre')} "
                        f"skip_email={d.get('skipped_email')}")
        # Re-run: TikHub's similar feed isn't idempotent, but the previously-saved
        # accounts MUST appear in skipped_pre.
        prev_new = len(d.get("new_accounts", []))
        d2, err2 = rig.post_json("/api/similarity/run",
                                 {"seed_account_ids": [nat_id], "count": 8})
        ok2 = err2 is None and d2.get("skipped_pre", 0) >= prev_new
        rig.step("POST /api/similarity/run (re-run respects DB dedup)", ok2,
                 err2 or f"prev_new={prev_new} skip_pre={d2.get('skipped_pre')} new={len(d2.get('new_accounts', []))}")

    # ----- 9. query (AI → search → ingest) -----
    print("\n[9] query discovery")
    d, err = rig.post_json("/api/query/run", {
        "queries": ["fitness", "yoga"],   # bypass AI for deterministic test
        "count": 6,
        "platforms": ["instagram", "tiktok"],
        "num_queries": 2,
    })
    ok = err is None and (d or {}).get("considered", 0) > 0
    quota = (d or {}).get("quota", {})
    rig.step("POST /api/query/run (preset queries)", ok,
             err or f"considered={d.get('considered')} new={len(d.get('new_accounts', []))} "
                    f"quota={quota}")

    # ----- 10. embedding fill -----
    print("\n[10] embedding")
    d, err = rig.post_json("/api/db/embed_missing")
    rig.step("POST /api/db/embed_missing", err is None, err or f"updated={(d or {}).get('updated')}")

    # ----- 11. excel export -----
    print("\n[11] excel export")
    r = rig._req("POST", "/api/db/export", json={})
    ok = r.status_code == 200 and len(r.content) > 1000 and r.content[:2] == b"PK"
    rig.step("POST /api/db/export", ok, f"HTTP {r.status_code}, {len(r.content)} bytes")

    # ----- 12. delete an account -----
    print("\n[12] delete")
    d, err = rig.get_json("/api/db/list", params={"limit": 1})
    if d and d.get("accounts"):
        del_id = d["accounts"][0]["id"]
        d2, err2 = rig.delete(f"/api/db/account/{del_id}")
        rig.step(f"DELETE /api/db/account/{del_id}", err2 is None and d2.get("deleted") is True,
                 err2 or f"deleted={d2.get('deleted')}")

    # ----- summary -----
    print(f"\n=== {len(rig.passed)} passed, {len(rig.failed)} failed ===")
    for name, why in rig.failed:
        print(f"  FAIL: {name}\n     {why}")
    return len(rig.failed) == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--reset", action="store_true", help="wipe DB before running")
    args = ap.parse_args()
    rig = TestRig(args.base)
    ok = run(rig, reset=args.reset)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
