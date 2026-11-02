"""Add an extra admin user (the app already auto-bootstraps `admin/admin` on
first start with must_change_pw=1; use this only when you want a *second*
admin, or want to script-create one with a known username/email).

    python -m tools.create_admin <username> <email>

Prompts for password (no echo). Idempotent on (username): refuses if taken.
"""
import getpass
import sys

from lib import auth, db


def main() -> int:
    db.init_db()
    if len(sys.argv) < 3:
        print("usage: python -m tools.create_admin <username> <email>", file=sys.stderr)
        return 2
    username, email = sys.argv[1].strip(), sys.argv[2].strip()
    if auth.get_user_by_username(username):
        print(f"username already taken: {username}", file=sys.stderr)
        return 1
    if email and auth.get_user_by_email(email):
        print(f"email already taken: {email}", file=sys.stderr)
        return 1
    pw1 = getpass.getpass("password (min 8 chars): ")
    pw2 = getpass.getpass("repeat password: ")
    if pw1 != pw2:
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        uid = auth.create_user(
            username=username,
            email=email or None,
            display_name=username,
            password=pw1,
            role="admin",
            is_active=1,
            must_change_pw=0,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"created admin user id={uid} username={username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
