"""Screenshot the drawers and every page for a quick layout review.

    python tools/shots.py drawers   # .zcode/shots/*.png
    python tools/shots.py pages
    python tools/shots.py           # both

Needs Playwright and a local Chrome; it drives the auth-bypassed harness in
tools/shot_server.py, so start that first:

    SCOUT_DB_PATH=tests/.shot.db python tools/shot_server.py 5010
    pip install playwright          # channel="chrome" uses the installed browser
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / ".zcode" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:5010"

PAGES = ["/stats", "/people", "/database", "/similarity", "/query",
         "/settings", "/admin/users", "/change_password", "/me"]

# Each page needs something concrete on screen before the shot is meaningful.
WAIT = {
    "/stats": "#kpiGrid .kpi-value",
    "/people": "tr[data-pid]",
    "/database": "#dbList .acct-row",
    "/similarity": ".card",
    "/query": "input",
    "/settings": "input",
    "/admin/users": "table tbody tr, .empty",
    "/change_password": "#old",
    "/me": "body",
}


def shots_pages(pg, errors):
    for path in PAGES:
        pg.goto(BASE + path, wait_until="domcontentloaded")
        try:
            pg.wait_for_selector(WAIT[path], timeout=15000)
        except Exception as e:
            errors.append(f"{path}: wait failed {type(e).__name__}")
        pg.wait_for_timeout(700)
        name = path.strip("/").replace("/", "_") or "root"
        pg.screenshot(path=str(OUT / f"page_{name}.png"), full_page=False)
        print("shot", path)


def shots_drawers(pg, _errors):
    pg.goto(f"{BASE}/database", wait_until="domcontentloaded")
    pg.wait_for_selector("#dbList .acct-row", timeout=15000)
    pg.locator("#dbList .acct-row").first.click()
    pg.wait_for_selector("#drawer .dsec", timeout=15000)
    pg.wait_for_timeout(700)
    pg.screenshot(path=str(OUT / "db_drawer.png"))

    pg.goto(f"{BASE}/people", wait_until="domcontentloaded")
    pg.wait_for_selector("tr[data-pid]", timeout=20000)
    pg.locator("tr[data-pid] .btn-handles").first.click()
    pg.wait_for_selector("#handlesDrawer .dsec", timeout=15000)
    pg.wait_for_timeout(900)
    pg.screenshot(path=str(OUT / "people_drawer.png"))
    print("shots drawers")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
              if m.type == "error" else None)
        if mode in ("all", "drawers"):
            shots_drawers(pg, errors)
        if mode in ("all", "pages"):
            shots_pages(pg, errors)
        b.close()
    if errors:
        print("JS errors:")
        for e in errors:
            print("  " + e)
    print("shots ->", OUT)


if __name__ == "__main__":
    sys.exit(main())
