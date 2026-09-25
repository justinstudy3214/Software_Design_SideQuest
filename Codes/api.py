"""
Backend: API route handlers.
Every /api/... endpoint's business logic lives here: signup, login,
logout, session check, game catalog, and profile read/write. Nothing in
this module knows about static files or HTML - that's the frontend's job.
"""
import base64
import binascii
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from config import (
    EMAIL_RE, GAME_IMAGES_DIR, GAMES_JSON, MAX_GAME_BODY_BYTES,
    MAX_IMAGE_BYTES, MAX_PASSWORD, MIN_PASSWORD,
)
from db import db, load_games, load_profile, save_profile, seed_games
from auth import (
    DUMMY_HASH, LOGIN_LIMITER, SIGNUP_LIMITER,
    create_session, hash_password, session_cookie, clear_cookie,
    sha256, verify_password,
)


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


def api_signup(handler):
    data = handler.read_json()
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    remember = bool(data.get("remember"))

    if not name or len(name) > 60:
        raise ApiError(400, "Enter your name (up to 60 characters).")
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ApiError(400, "That email address doesn't look right.")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ApiError(400, f"Choose a password with at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise ApiError(400, f"Passwords can be up to {MAX_PASSWORD} characters.")

    ip = handler.client_ip()
    wait = SIGNUP_LIMITER.blocked_for(ip)
    if wait:
        raise ApiError(429, "Too many new accounts from this address. Try again later.", {"retry_after": wait})
    SIGNUP_LIMITER.hit(ip)

    pw_hash = hash_password(password)
    try:
        with db() as conn:
            cur = conn.execute("INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
                               (email, name, pw_hash))
            user_id = cur.lastrowid
            token, ttl = create_session(conn, user_id, remember)
    except sqlite3.IntegrityError:
        raise ApiError(409, "An account with that email already exists. Log in instead.")
    handler.send_json(201, {"user": {"id": user_id, "email": email, "name": name}},
                       cookies=[session_cookie(token, ttl if remember else None)])


def api_login(handler):
    data = handler.read_json()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    remember = bool(data.get("remember"))
    if not email or not isinstance(password, str) or not password or len(password) > MAX_PASSWORD:
        raise ApiError(400, "Enter your email and password.")

    key = f"{handler.client_ip()}|{email}"
    wait = LOGIN_LIMITER.blocked_for(key)
    if wait:
        mins = -(-wait // 60)
        raise ApiError(429, f"Too many wrong tries. Try again in {mins} minute{'s' if mins != 1 else ''}.",
                       {"retry_after": wait})

    with db() as conn:
        row = conn.execute("SELECT id, email, name, password_hash FROM users WHERE email = ?",
                           (email,)).fetchone()
    good = verify_password(password, row["password_hash"] if row else DUMMY_HASH)
    if not row or not good:
        LOGIN_LIMITER.hit(key)
        raise ApiError(401, "The email or password is incorrect.")

    LOGIN_LIMITER.reset(key)
    with db() as conn:
        token, ttl = create_session(conn, row["id"], remember)
    handler.send_json(200, {"user": {"id": row["id"], "email": row["email"], "name": row["name"]}},
                       cookies=[session_cookie(token, ttl if remember else None)])


def api_logout(handler):
    handler.read_json()
    token = handler.session_token()
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256(token),))
    handler.send_json(200, {"ok": True}, cookies=[clear_cookie()])


def api_me(handler):
    handler.send_json(200, {"user": handler.require_user()})


def api_games(handler):
    handler.require_user()
    with db() as conn:
        games = load_games(conn)
    handler.send_json(200, {"games": games})


def api_get_profile(handler):
    user = handler.require_user()
    with db() as conn:
        profile = load_profile(conn, user["id"])
    handler.send_json(200, {"profile": profile})


def api_put_profile(handler):
    user = handler.require_user()
    data = handler.read_json()
    with db() as conn:
        save_profile(conn, user["id"], data)
    handler.send_json(200, {"ok": True})


def _load_games_json():
    """Load games from JSON file."""
    if GAMES_JSON.exists():
        with open(GAMES_JSON, 'r') as f:
            return json.load(f)
    return []


def _save_games_json(games):
    """Save games to JSON file."""
    GAMES_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(GAMES_JSON, 'w', encoding='utf-8') as f:
        json.dump(games, f, indent=2)


def _sync_games_database():
    """Make API reads reflect the updated JSON catalog immediately."""
    with db() as conn:
        seed_games(conn)


_IMAGE_DATA_URL = re.compile(
    r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})$",
    re.IGNORECASE,
)
_IMAGE_SIGNATURES = {
    "png": ("png", b"\x89PNG\r\n\x1a\n"),
    "jpeg": ("jpg", b"\xff\xd8\xff"),
    "webp": ("webp", b"RIFF"),
}


def _normalize_image_url(value):
    """Persist uploaded image data and validate externally supplied URLs."""
    if not isinstance(value, str):
        raise ApiError(400, "Choose a valid image or enter an image URL.")
    image_url = value.strip()
    if not image_url:
        return ""
    if len(image_url) > 2048 + (MAX_IMAGE_BYTES * 4 // 3) + 128:
        raise ApiError(413, "The image or image URL is too large.")

    match = _IMAGE_DATA_URL.fullmatch(image_url)
    if match:
        mime = match.group(1).lower()
        try:
            image_data = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "The uploaded image is invalid.")
        if not image_data or len(image_data) > MAX_IMAGE_BYTES:
            raise ApiError(413, "Images must be smaller than 4 MB.")

        extension, signature = _IMAGE_SIGNATURES[mime]
        if not image_data.startswith(signature) or (mime == "webp" and image_data[8:12] != b"WEBP"):
            raise ApiError(400, "Upload a valid PNG, JPG, or WebP image.")

        GAME_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid4().hex}.{extension}"
        (GAME_IMAGES_DIR / filename).write_bytes(image_data)
        return f"/uploads/{filename}"

    if any(ord(char) < 32 for char in image_url):
        raise ApiError(400, "Enter a valid image URL.")
    parsed = urlsplit(image_url)
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ApiError(400, "Image URLs must use HTTPS.")
    elif image_url.startswith("//") or not parsed.path:
        raise ApiError(400, "Enter a valid image URL.")
    return image_url


def api_create_game(handler):
    """Create a new game."""
    handler.require_user()
    data = handler.read_json(max_bytes=MAX_GAME_BODY_BYTES)

    games = _load_games_json()
    
    new_game = {
        "id": str(uuid4()),
        "title": data.get("title", "").strip(),
        "genres": data.get("genres", []),
        "year": data.get("year", 2024),
        "price": data.get("price", "Free to play"),
        "trend": 0,
        "how": data.get("how", ""),
        "mechanics": data.get("mechanics", ""),
        "objective": data.get("objective", ""),
        "style": data.get("style", ""),
    }
    
    new_game["image_url"] = _normalize_image_url(data.get("image_url", ""))

    games.append(new_game)
    _save_games_json(games)
    _sync_games_database()

    handler.send_json(201, {"success": True, "game": new_game})


def api_update_game(handler, game_id):
    """Update an existing game."""
    handler.require_user()
    data = handler.read_json(max_bytes=MAX_GAME_BODY_BYTES)

    games = _load_games_json()
    game_index = next((i for i, g in enumerate(games) if g["id"] == game_id), None)
    
    if game_index is None:
        raise ApiError(404, "Game not found")
    
    game = games[game_index]
    
    # Update fields
    if "title" in data:
        game["title"] = str(data["title"]).strip()
    if "genres" in data:
        game["genres"] = data["genres"]
    if "year" in data:
        game["year"] = int(data["year"])
    if "price" in data:
        game["price"] = str(data["price"]).strip()
    if "how" in data:
        game["how"] = str(data["how"]).strip()
    if "mechanics" in data:
        game["mechanics"] = str(data["mechanics"]).strip()
    if "objective" in data:
        game["objective"] = str(data["objective"]).strip()
    if "style" in data:
        game["style"] = str(data["style"]).strip()
    if "image_url" in data:
        image_url = _normalize_image_url(data["image_url"])
        if image_url:
            game["image_url"] = image_url
        else:
            game.pop("image_url", None)

    games[game_index] = game
    _save_games_json(games)
    _sync_games_database()

    handler.send_json(200, {"success": True, "game": game})


def api_delete_game(handler, game_id):
    """Delete a game."""
    handler.require_user()
    
    games = _load_games_json()
    games = [g for g in games if g["id"] != game_id]
    _save_games_json(games)
    _sync_games_database()

    handler.send_json(200, {"success": True})


# Maps (HTTP method, path) -> handler function. Imported by server.py's dispatcher.
ROUTES = {
    ("POST", "/api/signup"): api_signup,
    ("POST", "/api/login"): api_login,
    ("POST", "/api/logout"): api_logout,
    ("GET", "/api/me"): api_me,
    ("GET", "/api/games"): api_games,
    ("GET", "/api/profile"): api_get_profile,
    ("PUT", "/api/profile"): api_put_profile,
    ("POST", "/api/create-game"): api_create_game,
}
