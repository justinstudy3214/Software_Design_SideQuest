"""
Shared settings for the Sidequest server.
Both the backend (db/auth/api) and the static file server read from here.
"""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = (BASE_DIR / "public").resolve()
GAMES_JSON = BASE_DIR / "data" / "games.json"
DB_PATH = Path(os.environ.get("SIDEQUEST_DB", BASE_DIR / "sidequest.db"))

COOKIE_NAME = "sq_session"
COOKIE_SECURE = os.environ.get("SIDEQUEST_SECURE_COOKIES") == "1"  # set to 1 when served over HTTPS
SESSION_DAYS_REMEMBER = 30      # "Keep me logged in" ticked
SESSION_HOURS_DEFAULT = 24      # otherwise (cookie also ends when the browser closes)
MAX_BODY = 64 * 1024
MAX_GAME_BODY_BYTES = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
GAME_IMAGES_DIR = PUBLIC_DIR / "uploads"
MIN_PASSWORD = 8
MAX_PASSWORD = 200

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
