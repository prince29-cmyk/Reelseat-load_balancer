"""
The shared database. All three servers read and write the same file (reelseat.db),
so a visitor can be handled by any server and still see the same bookings, login and seats.
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reelseat-v3.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads", "posters")
OWNER_FILE = os.path.join(BASE_DIR, "owner_login.txt")

# ---------------------------------------------------------------
# Fixed settings
# ---------------------------------------------------------------
SECTIONS = [
    {"name": "Recliner", "price": 350, "rows": ["J", "H"]},
    {"name": "Premium", "price": 220, "rows": ["G", "F", "E", "D"]},
    {"name": "Classic", "price": 150, "rows": ["C", "B", "A"]},
]
SEATS_PER_ROW = 12
AISLES_AFTER = [3, 9]
FEE_PER_TICKET = 30
MAX_SEATS = 10
HOLD_MINUTES = 5            # a seat you click is kept for you this long
CANCEL_HOURS_BEFORE = 2     # cancelling is allowed until this long before the show
SESSION_DAYS = 7
DAYS_AHEAD = 7

PRICE_BY_ROW = {row: s["price"] for s in SECTIONS for row in s["rows"]}
SECTION_BY_ROW = {row: s["name"] for s in SECTIONS for row in s["rows"]}

# ---------------------------------------------------------------
# Starting data: real films in cinemas around Deepavali 2026 (as of 7 Nov 2026).
# Running time and certificate are left blank where they weren't published;
# the owner can fill them in from the dashboard.
# Each: (title, genre, language, length, certificate, card colour, description, director, starring)
# ---------------------------------------------------------------
SEED_MOVIES = [
    ("Scene", "Action, crime, comedy", "Tamil", "", "", "#3B4A6B",
     "DSP Inbaraj and his unusual team investigate the kidnapping of a businessman's son in Bengaluru.",
     "Jithu Madhavan", "Suriya, Nazriya Nazim, Naslen"),
    ("Mookuthi Amman 2", "Mythological thriller", "Tamil", "", "", "#7A2E3B",
     "A story across two timelines, where a queen's sacrifice in the past collides with the present day.",
     "Sundar C", "Nayanthara, Regina Cassandra, Urvashi, Yogi Babu"),
    ("Moonwalk", "Musical fantasy comedy", "Tamil", "", "", "#2E5A7A",
     "Babootty, a young film choreographer, dances through a family comedy full of music and humour.",
     "Manoj NS", "Prabhu Deva, A.R. Rahman, Yogi Babu"),
    ("Appa Kutty", "Action drama", "Tamil", "", "", "#4A5A2F",
     "A wounded father faces danger to protect his young daughter.",
     "Mu. Maran", "Vijay Antony, Lithanya Sivabalan"),
    ("Ramayana: Rise of Legend (Part 1)", "Mythological epic", "Hindi", "", "", "#7A5A2F",
     "The first part of a two-part retelling of the Ramayana.",
     "Nitesh Tiwari", "Ranbir Kapoor, Sai Pallavi, Yash, Sunny Deol"),
    ("Disco", "Action crime thriller", "Malayalam", "", "", "#5B4A7A",
     "A stylish action-crime thriller set in a quirky underworld.",
     "Ullas Chemban", "Antony Varghese, Arjun Ashokan, Lukman Avaran"),
    ("The Cat in the Hat", "Animated comedy", "English", "", "", "#6B3B3B",
     "Based on Dr. Seuss: the mischievous Cat tries to cheer up two children struggling in a new town.",
     "Alessandro Carloni, Erica Rivinoja", ""),
    ("Wild Horse Nine", "Dark comedy thriller", "English", "", "", "#2F5D50",
     "A political dark comedy-thriller from writer-director Martin McDonagh.",
     "Martin McDonagh", ""),
]
# Release dates are announced dates and may change.
SEED_SOON = [
    ("Irandu Vaanam", "Drama", "Tamil", "#5A3E2B", "Drama starring Vishnu Vishal and Mamitha Baiju.", "Vishnu Vishal, Mamitha Baiju", "13 Nov"),
    ("John Doe", "Crime thriller", "Tamil", "#2B3E5A", "Crime thriller starring Arjun Das.", "Arjun Das, Sanchana Natarajan", "13 Nov"),
    ("Demonte Colony 3", "Horror thriller", "Tamil", "#3E2B5A", "The third film in the Demonte Colony horror series.", "Arulnidhi, Priya Bhavani Shankar", "20 Nov"),
    ("Love Bite", "Horror comedy", "Tamil", "#5A2B4A", "Horror comedy starring Santhanam.", "Santhanam, Aparna Das", "25 Nov"),
]
SEED_CINEMAS = [
    ("Starlight Cinemas: Marina Mall", "Marina", "Recliners, food court", [("10:15", "2D"), ("13:30", "2D"), ("18:45", "IMAX"), ("22:00", "2D")]),
    ("Grand Screens: Lakeview", "Lakeview", "Parking, wheelchair access", [("11:00", "2D"), ("15:15", "2D"), ("19:30", "2D")]),
    ("Arcadia Multiplex: City Centre", "City Centre", "Dolby Atmos", [("09:45", "2D"), ("12:50", "2D"), ("16:10", "IMAX"), ("21:20", "IMAX")]),
    ("Royal Talkies", "Old Town", "Single screen", [("14:30", "2D"), ("18:00", "2D")]),
]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL, salt TEXT NOT NULL, is_owner INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, genre TEXT, lang TEXT, length TEXT, cert TEXT, color TEXT,
    synopsis TEXT, status TEXT NOT NULL DEFAULT 'now', release TEXT, auto_schedule INTEGER NOT NULL DEFAULT 0,
    director TEXT, starring TEXT, poster TEXT);
CREATE TABLE IF NOT EXISTS cinemas (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, area TEXT, extras TEXT, default_times TEXT);
CREATE TABLE IF NOT EXISTS showtimes (
    id INTEGER PRIMARY KEY, movie_id INTEGER NOT NULL REFERENCES movies(id), cinema_id INTEGER NOT NULL REFERENCES cinemas(id),
    day TEXT NOT NULL, time TEXT NOT NULL, format TEXT NOT NULL, UNIQUE (movie_id, cinema_id, day, time));
CREATE INDEX IF NOT EXISTS showtimes_day ON showtimes (day);
CREATE TABLE IF NOT EXISTS seeded_days (day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), showtime_id INTEGER NOT NULL REFERENCES showtimes(id),
    seats TEXT NOT NULL, tickets INTEGER NOT NULL, fee INTEGER NOT NULL, total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed', created TEXT NOT NULL, served_by TEXT);
CREATE TABLE IF NOT EXISTS booked_seats (
    showtime_id INTEGER NOT NULL, seat TEXT NOT NULL, booking_id TEXT NOT NULL, PRIMARY KEY (showtime_id, seat));
CREATE TABLE IF NOT EXISTS holds (
    showtime_id INTEGER NOT NULL, seat TEXT NOT NULL, user_id INTEGER NOT NULL, expires TEXT NOT NULL,
    PRIMARY KEY (showtime_id, seat));
"""


# ---------------------------------------------------------------
# Passwords and sessions
# ---------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return digest.hex(), salt


def check_password(password, pw_hash, salt):
    return secrets.compare_digest(hash_password(password, salt)[0], pw_hash)


def now_str():
    return datetime.now().isoformat(timespec="seconds")


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, expires))
    return token


def user_for_token(conn, token):
    if not token:
        return None
    row = conn.execute("""SELECT u.id, u.name, u.email, u.is_owner FROM sessions s JOIN users u ON u.id = s.user_id
                          WHERE s.token = ? AND s.expires > ?""", (token, now_str())).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------
# Setting up the database
# ---------------------------------------------------------------
def init_db():
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")      # lets many servers read while one writes
    conn.executescript(SCHEMA)

    # Add newer columns to an older database
    have = {r["name"] for r in conn.execute("PRAGMA table_info(movies)")}
    for col in ("director", "starring", "poster"):
        if col not in have:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col} TEXT")
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    if conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 0:
        for m in SEED_MOVIES:
            conn.execute("INSERT INTO movies (title, genre, lang, length, cert, color, synopsis, director, starring, "
                         "status, auto_schedule) VALUES (?,?,?,?,?,?,?,?,?, 'now', 1)", m)
        for title, genre, lang, color, synopsis, starring, release in SEED_SOON:
            conn.execute("INSERT INTO movies (title, genre, lang, length, cert, color, synopsis, starring, status, release) "
                         "VALUES (?,?,?,'','',?,?,?, 'soon', ?)", (title, genre, lang, color, synopsis, starring, release))
        for name, area, extras, times in SEED_CINEMAS:
            conn.execute("INSERT INTO cinemas (name, area, extras, default_times) VALUES (?,?,?,?)",
                         (name, area, extras, ";".join(f"{t}|{f}" for t, f in times)))

    owner_password = None
    if not conn.execute("SELECT 1 FROM users WHERE is_owner = 1").fetchone():
        owner_password = secrets.token_urlsafe(9)
        pw_hash, salt = hash_password(owner_password)
        conn.execute("INSERT INTO users (name, email, pw_hash, salt, is_owner, created) VALUES (?,?,?,?,1,?)",
                     ("Cinema Owner", "owner@reelseat.local", pw_hash, salt, now_str()))
        with open(OWNER_FILE, "w", encoding="utf-8") as f:
            f.write("Owner dashboard login\n")
            f.write("Email:    owner@reelseat.local\n")
            f.write(f"Password: {owner_password}\n")

    ensure_schedule(conn)
    conn.close()
    return owner_password


def ensure_schedule(conn):
    """Keep the regular showtimes filled in for the next 7 days."""
    today = date.today()
    for i in range(DAYS_AHEAD):
        day = (today + timedelta(days=i)).isoformat()
        if conn.execute("SELECT 1 FROM seeded_days WHERE day = ?", (day,)).fetchone():
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("INSERT OR IGNORE INTO seeded_days VALUES (?)", (day,)).rowcount:
                movies = conn.execute("SELECT id FROM movies WHERE auto_schedule = 1 AND status = 'now'").fetchall()
                cinemas = conn.execute("SELECT id, default_times FROM cinemas").fetchall()
                for m in movies:
                    for c in cinemas:
                        for slot in (c["default_times"] or "").split(";"):
                            if slot:
                                t, f = slot.split("|")
                                conn.execute("INSERT OR IGNORE INTO showtimes (movie_id, cinema_id, day, time, format) "
                                             "VALUES (?,?,?,?,?)", (m["id"], c["id"], day, t, f))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
