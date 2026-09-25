#!/usr/bin/env python3
"""
Sidequest server
================
Serves the website AND runs the accounts + database, using only Python's
standard library (no pip install needed).

    python server.py                      start the site at http://localhost:8000
    python server.py --port 3000          use another port
    python server.py set-password EMAIL   set a new password for an account

Data is stored in sidequest.db (SQLite) next to this file.
The game catalog is read from data/games.json every time the server starts.

This file is just the entry point and HTTP plumbing. The real logic lives in:
  config.py        shared settings
  db.py            database (backend)
  auth.py          passwords, sessions, rate limiting (backend)
  api.py           /api/... route handlers (backend)
  static_files.py  serves public/ (frontend)
"""
import argparse
import getpass
import json
import os
import sys
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from config import COOKIE_NAME, CSP, DB_PATH, MAX_BODY, MAX_PASSWORD, MIN_PASSWORD
from db import db, init_db
from auth import hash_password, sha256
from api import ApiError, ROUTES
from static_files import serve_static


class Handler(BaseHTTPRequestHandler):
    server_version = "Sidequest/1.0"

    # ---- plumbing -----------------------------------------------------
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path = urlsplit(self.path).path
        try:
            if path.startswith("/api/"):
                self.handle_api(method, path)
            elif method == "GET":
                serve_static(self, path)
            else:
                raise ApiError(405, "Method not allowed.")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message, **e.extra})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json(500, {"error": "Something went wrong on the server."})
            except Exception:
                pass

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)

    def send_json(self, status, data, cookies=None):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()

    def read_json(self, max_bytes=MAX_BODY):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Requests must be sent as JSON.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Bad Content-Length.")
        if length <= 0:
            raise ApiError(400, "Missing request body.")
        if length > max_bytes:
            raise ApiError(413, "Request is too large.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Expected a JSON object.")
        return data

    def session_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            morsel = SimpleCookie(raw).get(COOKIE_NAME)
        except Exception:
            return None
        return morsel.value if morsel else None

    def current_user(self):
        token = self.session_token()
        if not token:
            return None
        with db() as conn:
            row = conn.execute(
                """SELECT u.id, u.email, u.name FROM sessions s
                   JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at > ?""",
                (sha256(token), int(time.time())),
            ).fetchone()
        return dict(row) if row else None

    def require_user(self):
        user = self.current_user()
        if not user:
            raise ApiError(401, "You need to log in.")
        return user

    # ---- API ----------------------------------------------------------
    def handle_api(self, method, path):
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise ApiError(403, "Cross-site requests are blocked.")
        
        # Try exact match first
        handler_fn = ROUTES.get((method, path))
        if handler_fn:
            handler_fn(self)
            return
        
        # Try dynamic routes with parameters
        parts = path.split('/')
        
        # Handle /api/update-game/{gameId}
        if method == "POST" and len(parts) == 4 and parts[1] == "api" and parts[2] == "update-game":
            from api import api_update_game
            api_update_game(self, parts[3])
            return
        
        # Handle /api/delete-game/{gameId}
        if method == "POST" and len(parts) == 4 and parts[1] == "api" and parts[2] == "delete-game":
            from api import api_delete_game
            api_delete_game(self, parts[3])
            return
        
        raise ApiError(404, "Not found.")

    def client_ip(self):
        return self.client_address[0]


# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------
def cmd_set_password(email):
    email = email.strip().lower()
    init_db()
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            sys.exit(f"No account found for {email}.")
        pw = getpass.getpass("New password: ")
        if len(pw) < MIN_PASSWORD or len(pw) > MAX_PASSWORD:
            sys.exit(f"Passwords must be {MIN_PASSWORD} to {MAX_PASSWORD} characters.")
        if pw != getpass.getpass("Repeat new password: "):
            sys.exit("The passwords don't match. Nothing was changed.")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(pw), row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
    print("Password updated. Any existing logins for this account were ended.")


def cmd_run(host, port):
    init_db()
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        sys.exit(f"Could not start on {host}:{port} ({e}). Try another port, e.g. --port {port + 1}")
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Sidequest is running at http://{shown}:{port}")
    print(f"Database: {DB_PATH}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Sidequest game discovery site")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "set-password"])
    parser.add_argument("email", nargs="?", help="account email (for set-password)")
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (this computer only)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()

    if args.command == "set-password":
        if not args.email:
            parser.error("set-password needs an email: python server.py set-password you@example.com")
        cmd_set_password(args.email)
    else:
        cmd_run(args.host, args.port)


if __name__ == "__main__":
    main()
