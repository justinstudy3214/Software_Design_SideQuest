"""
Backend: database layer.
Owns the SQLite schema, the connection helper, and every read/write of
users, games, genres and per-user actions (like/pass/save).
"""
import json
import sqlite3
from contextlib import contextmanager

from config import DB_PATH, GAMES_JSON

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id           TEXT PRIMARY KEY,
    title        TEXT    NOT NULL,
    year         INTEGER NOT NULL,
    price        TEXT    NOT NULL,
    trend        INTEGER NOT NULL DEFAULT 0,
    how_it_plays TEXT    NOT NULL,
    mechanics    TEXT    NOT NULL,
    objective    TEXT    NOT NULL,
    style        TEXT    NOT NULL,
    image_url    TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS game_genres (
    game_id  TEXT    NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    genre    TEXT    NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (game_id, genre)
);

CREATE TABLE IF NOT EXISTS genres (
    name TEXT PRIMARY KEY COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_genres (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    genre   TEXT    NOT NULL,
    PRIMARY KEY (user_id, genre)
);

CREATE TABLE IF NOT EXISTS user_game_actions (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id    TEXT    NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    action     TEXT    NOT NULL CHECK (action IN ('like', 'pass', 'save')),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, game_id, action)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# One-time seed for the master genre list (used only the first time the
# `genres` table is empty, e.g. a fresh database or an upgrade from before
# this table existed). After that, the admin owns the list via
# create_genre()/delete_genre().
DEFAULT_GENRES = [
    'Action', 'Adventure', 'RPG', 'JRPG', 'Strategy', 'Tactical', 'Turn-Based Strategy', 'RTS',
    'Simulation', 'City Builder', 'Management', 'Tycoon', 'Sports', 'Racing', 'Arcade Racing',
    'Puzzle', 'Puzzle-Platformer', 'Sudoku', 'Match-3', 'Platformer', 'Metroidvania', '2D Platformer', '3D Platformer',
    'Shooter', 'FPS', 'TPS', 'Roguelike', 'Roguelite', 'Dungeon Crawler', 'Hack and Slash', 'Souls-like',
    'Fighting', 'Beat-em-up', 'Brawler', 'Horror', 'Psychological Horror', 'Survival Horror', 'Survival', 'Stealth',
    'Rhythm', 'Music', 'Dance', 'Card Game', 'Deck Building', 'Board Game', 'Indie', 'Casual', 'Arcade',
    'MOBA', 'Battle Royale', 'Tower Defense', 'Auto-Battler', 'Visual Novel', 'Dating Sim', 'Interactive Fiction',
    'Exploration', 'Open World', 'Sandbox', 'Story-Driven', 'Narrative', 'Point-and-Click', 'Adventure Game',
    'Walking Simulator', 'Pixel Art', 'Retro', 'Indie Game', 'Co-op', 'Multiplayer', 'Online', 'Local Multiplayer',
]


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        existing_tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
        if "image_url" not in columns:
            conn.execute("ALTER TABLE games ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        seed_games(conn)
        seed_genres(conn, genre_table_existed="genres" in existing_tables)


def seed_games(conn):
    """Make the games table match data/games.json (add, update, remove)."""
    games = json.loads(GAMES_JSON.read_text(encoding="utf-8"))
    for g in games:
        conn.execute(
            """INSERT INTO games (
                   id, title, year, price, trend, how_it_plays, mechanics, objective, style, image_url
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title = excluded.title, year = excluded.year, price = excluded.price,
                 trend = excluded.trend, how_it_plays = excluded.how_it_plays,
                 mechanics = excluded.mechanics, objective = excluded.objective,
                 style = excluded.style, image_url = excluded.image_url""",
            (g["id"], g["title"], g["year"], g["price"], g.get("trend", 0),
             g["how"], g["mechanics"], g["objective"], g["style"], g.get("image_url", "")),
        )
        conn.execute("DELETE FROM game_genres WHERE game_id = ?", (g["id"],))
        for pos, genre in enumerate(g["genres"]):
            conn.execute(
                "INSERT INTO game_genres (game_id, genre, position) VALUES (?, ?, ?)",
                (g["id"], genre, pos),
            )
    ids = [g["id"] for g in games]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM games WHERE id NOT IN ({marks})", ids)
    else:
        conn.execute("DELETE FROM games")


def seed_genres(conn, genre_table_existed=True):
    """Seed the master genres once, without reviving admin-deleted genres."""
    seeded = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'genres_seeded'"
    ).fetchone()
    if seeded:
        return

    # Existing databases predate the seed marker; preserve their current list,
    # including an intentionally empty list after admin removal.
    if genre_table_existed:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('genres_seeded', '1')")
        return

    used = {r["genre"] for r in conn.execute("SELECT DISTINCT genre FROM game_genres")}
    names = sorted(set(DEFAULT_GENRES) | used, key=str.casefold)
    conn.executemany("INSERT OR IGNORE INTO genres (name) VALUES (?)", [(n,) for n in names])
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('genres_seeded', '1')")


def load_genre_names(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM genres ORDER BY name COLLATE NOCASE")]


def create_genre(conn, name):
    from api import ApiError  # local import avoids a circular import at module load time

    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 40:
        raise ApiError(400, "Enter a genre name (up to 40 characters).")
    try:
        conn.execute("INSERT INTO genres (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        raise ApiError(409, "That genre already exists.")
    return load_genre_names(conn)


def delete_genre(conn, name):
    from api import ApiError  # local import avoids a circular import at module load time

    cur = conn.execute("DELETE FROM genres WHERE name = ?", (name,))
    if cur.rowcount == 0:
        raise ApiError(404, "That genre doesn't exist.")
    conn.execute("DELETE FROM user_genres WHERE genre = ?", (name,))
    return load_genre_names(conn)


def load_games(conn):
    genres = {}
    for r in conn.execute("SELECT game_id, genre FROM game_genres ORDER BY game_id, position"):
        genres.setdefault(r["game_id"], []).append(r["genre"])
    out = []
    for r in conn.execute("SELECT * FROM games ORDER BY title"):
        out.append({
            "id": r["id"],
            "title": r["title"],
            "genres": genres.get(r["id"], []),
            "year": r["year"],
            "price": r["price"],
            "trend": r["trend"],
            "how": r["how_it_plays"],
            "mechanics": r["mechanics"],
            "objective": r["objective"],
            "style": r["style"],
            "image_url": r["image_url"],
        })
    return out


def load_profile(conn, user_id):
    picked = [r["genre"] for r in conn.execute(
        "SELECT genre FROM user_genres WHERE user_id = ? ORDER BY rowid", (user_id,))]
    profile = {"picked": picked, "likes": [], "passes": [], "saved": []}
    key = {"like": "likes", "pass": "passes", "save": "saved"}
    for r in conn.execute(
            "SELECT game_id, action FROM user_game_actions WHERE user_id = ? ORDER BY rowid", (user_id,)):
        profile[key[r["action"]]].append(r["game_id"])
    return profile


def save_profile(conn, user_id, data):
    from api import ApiError  # local import avoids a circular import at module load time

    genres = {r["name"] for r in conn.execute("SELECT name FROM genres")}
    game_ids = {r["id"] for r in conn.execute("SELECT id FROM games")}

    def clean(value, allowed, field):
        if not isinstance(value, list) or len(value) > 500:
            raise ApiError(400, f"'{field}' must be a list.")
        out = []
        for v in value:
            if isinstance(v, str) and v in allowed and v not in out:  # unknown values are ignored
                out.append(v)
        return out

    picked = clean(data.get("picked", []), genres, "picked")
    lists = {
        "like": clean(data.get("likes", []), game_ids, "likes"),
        "pass": clean(data.get("passes", []), game_ids, "passes"),
        "save": clean(data.get("saved", []), game_ids, "saved"),
    }
    conn.execute("DELETE FROM user_genres WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_game_actions WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO user_genres (user_id, genre) VALUES (?, ?)",
                     [(user_id, g) for g in picked])
    for action, ids in lists.items():
        conn.executemany(
            "INSERT INTO user_game_actions (user_id, game_id, action) VALUES (?, ?, ?)",
            [(user_id, gid, action) for gid in ids],
        )
