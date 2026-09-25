"""
Backend: authentication.
Password hashing/verification, session tokens, session cookies, and the
in-memory rate limiter used to slow down brute-force login/signup attempts.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

from config import COOKIE_NAME, COOKIE_SECURE, SESSION_DAYS_REMEMBER, SESSION_HOURS_DEFAULT

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password("not-a-real-password")  # used so unknown emails take equally long


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_session(conn, user_id, remember):
    token = secrets.token_urlsafe(32)
    ttl = SESSION_DAYS_REMEMBER * 86400 if remember else SESSION_HOURS_DEFAULT * 3600
    now = int(time.time())
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (sha256(token), user_id, now + ttl))
    return token, ttl


def session_cookie(token, max_age=None):
    parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie():
    parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


class Limiter:
    """Allows `limit` hits per `window` seconds for each key (kept in memory)."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = {}
        self.lock = threading.Lock()

    def _prune(self, key, now):
        stamps = [t for t in self.hits.get(key, []) if now - t < self.window]
        if stamps:
            self.hits[key] = stamps
        else:
            self.hits.pop(key, None)
        return stamps

    def blocked_for(self, key):
        now = time.time()
        with self.lock:
            stamps = self._prune(key, now)
            if len(stamps) >= self.limit:
                return int(stamps[0] + self.window - now) + 1
        return 0

    def hit(self, key):
        with self.lock:
            self.hits.setdefault(key, []).append(time.time())

    def reset(self, key):
        with self.lock:
            self.hits.pop(key, None)


LOGIN_LIMITER = Limiter(limit=5, window=15 * 60)     # 5 wrong passwords per email+IP per 15 min
SIGNUP_LIMITER = Limiter(limit=10, window=60 * 60)   # 10 new accounts per IP per hour
