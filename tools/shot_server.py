"""Local-only screenshot harness: serves the real app with auth bypassed.

Used to eyeball /people and /database in a headless browser while working on
CSS. Never deploy this. It binds 127.0.0.1 and only exists so a screenshot tool
can reach pages that normally require a login cookie.

    SCOUT_DB_PATH=tests/.shot.db python tools/shot_server.py 5010
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("SCOUT_DB_PATH"):
    sys.exit("shot_server: set SCOUT_DB_PATH to a throwaway DB copy")
if Path(os.environ["SCOUT_DB_PATH"]).resolve() == (REPO_ROOT / "scout.db").resolve():
    sys.exit("shot_server: refusing to run against the real scout.db")

import lib.job_watchdog as job_watchdog

job_watchdog.start = lambda *a, **k: None      # no background sweeper

from lib import auth, db

_admin = auth.get_user_by_username("admin")
if not _admin:
    sys.exit("shot_server: no admin user in that DB")

# Every request is the admin. login_required/admin_required both go through
# auth.current_user, so this is enough.
auth.current_user = lambda: _admin


class _FakeSettings:
    """Avoid the app touching SMTP/AI keys while rendering pages."""


import app as app_module   # noqa: E402  (import after the patch)

app_module.app.config.update(TESTING=True)
# Templates are cached when debug is off; this harness exists to eyeball edits.
app_module.app.jinja_env.auto_reload = True
app_module.app.config["TEMPLATES_AUTO_RELOAD"] = True
port = int(sys.argv[1]) if len(sys.argv) > 1 else 5010
app_module.app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
