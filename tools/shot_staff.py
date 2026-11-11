"""Staff-identity screenshot server (throwaway DB only).

Same shape as tools/shot_server.py, but every request is a staff user, so the
admin-only nav/api differences can be exercised.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("SCOUT_DB_PATH"):
    sys.exit("shot_staff: set SCOUT_DB_PATH to a throwaway DB copy")
if Path(os.environ["SCOUT_DB_PATH"]).resolve() == (REPO_ROOT / "scout.db").resolve():
    sys.exit("shot_staff: refusing to run against the real scout.db")

import lib.job_watchdog as job_watchdog

job_watchdog.start = lambda *a, **k: None

from lib import auth

_staff = auth.get_user_by_username(os.environ.get("SCOUT_STAFF", "Echo"))
if not _staff or _staff["role"] != "staff":
    sys.exit("shot_staff: no active staff user in that DB")

auth.current_user = lambda: _staff

import app as app_module

app_module.app.config.update(TESTING=True)
app_module.app.jinja_env.auto_reload = True
app_module.app.config["TEMPLATES_AUTO_RELOAD"] = True
port = int(sys.argv[1]) if len(sys.argv) > 1 else 5011
app_module.app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
