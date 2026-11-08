"""
The booking servers. Each backend server runs this same code.

A server can be switched on/off, and put into a "mode" for testing:
    normal  - works properly
    slow    - waits 1.5 seconds before answering every request
    errors  - fails about half of all requests, and fails its health check
"""

import base64
import binascii
import json
import os
import random
import re
import secrets
import string
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import database as dbm
from database import (AISLES_AFTER, CANCEL_HOURS_BEFORE, FEE_PER_TICKET, HOLD_MINUTES, MAX_SEATS, PRICE_BY_ROW,
                      SECTION_BY_ROW, SECTIONS, SEATS_PER_ROW, SESSION_DAYS)

STATIC_DIR = os.path.join(dbm.BASE_DIR, "static")
SLOW_DELAY = 1.5
ERROR_RATE = 0.5
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon",
                 ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
MAX_BODY = 5 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class Transaction:
    """with Transaction(conn): ...  -> everything inside is saved together, or not at all."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


class Ctx:
    """Everything a request handler needs."""
    def __init__(self, conn, user, query, body, server_name):
        self.conn, self.user, self.q, self.body, self.server = conn, user, query, body, server_name
        self.headers = []

    def param(self, name, default=""):
        return self.q.get(name, [default])[0]


# =====================================================================
# Small helpers
# =====================================================================
def nice_time(hhmm):
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def as_int(value, what):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"Bad {what}")


def text(body, key, max_len, required=True):
    value = str(body.get(key) or "").strip()
    if required and not value:
        raise ApiError(400, f"Please fill in {key.replace('_', ' ')}")
    return value[:max_len]


def presold(showtime_id):
    """Pretend the box office already sold some seats (same answer every time)."""
    rnd = random.Random(f"show-{showtime_id}")
    return {f"{r}{n}" for r in PRICE_BY_ROW for n in range(1, SEATS_PER_ROW + 1) if rnd.random() < 0.12}


def booked(conn, sid):
    return {r[0] for r in conn.execute("SELECT seat FROM booked_seats WHERE showtime_id = ?", (sid,))}


def active_holds(conn, sid):
    rows = conn.execute("SELECT seat, user_id, expires FROM holds WHERE showtime_id = ? AND expires > ?",
                        (sid, dbm.now_str()))
    return {r["seat"]: r for r in rows}


def valid_seat(seat):
    return (isinstance(seat, str) and len(seat) >= 2 and seat[0] in PRICE_BY_ROW and seat[1:].isdigit()
            and 1 <= int(seat[1:]) <= SEATS_PER_ROW)


def load_show(conn, sid):
    row = conn.execute("""SELECT s.id, s.day, s.time, s.format, s.movie_id, s.cinema_id, c.name AS cinema
                          FROM showtimes s JOIN cinemas c ON c.id = s.cinema_id WHERE s.id = ?""", (sid,)).fetchone()
    if not row:
        raise ApiError(404, "Show not found")
    show = dict(row)
    show["movie"] = dict(conn.execute("SELECT * FROM movies WHERE id = ?", (row["movie_id"],)).fetchone())
    show["start"] = datetime.fromisoformat(f"{row['day']}T{row['time']}")
    return show


def show_public(show):
    return {"id": show["id"], "movie": public_movie(show["movie"]), "cinema": show["cinema"], "date": show["day"],
            "time": nice_time(show["time"]), "format": show["format"]}


def public_movie(m):
    m = dict(m)
    m.pop("auto_schedule", None)
    return m


def need_user(ctx):
    if not ctx.user:
        raise ApiError(401, "Please log in first")
    return ctx.user


def need_owner(ctx):
    if not ctx.user or not ctx.user["is_owner"]:
        raise ApiError(403, "Only the cinema owner can do this")
    return ctx.user


def session_cookie(token, max_age):
    return ("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")


def new_booking_id():
    return "RS-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# =====================================================================
# Accounts
# =====================================================================
def api_me(ctx):
    return {"user": ctx.user}


def api_signup(ctx):
    name = text(ctx.body, "name", 80)
    email = text(ctx.body, "email", 120).lower()
    password = str(ctx.body.get("password") or "")
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Please enter a valid email")
    if len(password) < 8:
        raise ApiError(400, "Password must be at least 8 characters")
    pw_hash, salt = dbm.hash_password(password)
    with Transaction(ctx.conn) as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ApiError(409, "An account with this email already exists. Try logging in.")
        user_id = conn.execute("INSERT INTO users (name, email, pw_hash, salt, created) VALUES (?,?,?,?,?)",
                               (name, email, pw_hash, salt, dbm.now_str())).lastrowid
        token = dbm.create_session(conn, user_id)
    ctx.headers.append(session_cookie(token, SESSION_DAYS * 86400))
    return {"user": {"id": user_id, "name": name, "email": email, "is_owner": 0}}


def api_login(ctx):
    email = str(ctx.body.get("email") or "").strip().lower()
    password = str(ctx.body.get("password") or "")
    row = ctx.conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not dbm.check_password(password, row["pw_hash"], row["salt"]):
        raise ApiError(401, "Email or password is wrong")
    with Transaction(ctx.conn) as conn:
        conn.execute("DELETE FROM sessions WHERE expires <= ?", (dbm.now_str(),))
        token = dbm.create_session(conn, row["id"])
    ctx.headers.append(session_cookie(token, SESSION_DAYS * 86400))
    return {"user": {"id": row["id"], "name": row["name"], "email": row["email"], "is_owner": row["is_owner"]}}


def api_logout(ctx):
    if ctx.user:
        ctx.conn.execute("DELETE FROM sessions WHERE token = ?", (ctx.token,))
    ctx.headers.append(session_cookie("", 0))
    return {"ok": True}


# =====================================================================
# Movies, showtimes and seats
# =====================================================================
def api_movies(ctx):
    rows = ctx.conn.execute("SELECT * FROM movies WHERE status IN ('now', 'soon') ORDER BY id").fetchall()
    return {"movies": [public_movie(r) for r in rows if r["status"] == "now"],
            "coming_soon": [public_movie(r) for r in rows if r["status"] == "soon"]}


def api_shows(ctx):
    dbm.ensure_schedule(ctx.conn)
    movie_id = as_int(ctx.param("movie"), "movie")
    day = ctx.param("date", date.today().isoformat())
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ApiError(400, "Bad date")
    movie = ctx.conn.execute("SELECT * FROM movies WHERE id = ? AND status = 'now'", (movie_id,)).fetchone()
    if not movie:
        raise ApiError(404, "Movie not found")
    rows = ctx.conn.execute("""SELECT s.id, s.time, s.format, c.id AS cid, c.name, c.area, c.extras
                               FROM showtimes s JOIN cinemas c ON c.id = s.cinema_id
                               WHERE s.movie_id = ? AND s.day = ? ORDER BY c.id, s.time""", (movie_id, day)).fetchall()
    now = datetime.now()
    cinemas = {}
    for r in rows:
        c = cinemas.setdefault(r["cid"], {"id": r["cid"], "name": r["name"], "area": r["area"],
                                          "extras": r["extras"], "times": []})
        started = datetime.fromisoformat(f"{day}T{r['time']}") <= now
        c["times"].append({"show_id": r["id"], "time": r["time"], "label": nice_time(r["time"]),
                           "format": r["format"], "started": started})
    return {"movie": public_movie(movie), "date": day, "cinemas": list(cinemas.values())}


def api_seats(ctx):
    sid = as_int(ctx.param("show"), "show")
    show = load_show(ctx.conn, sid)
    sold = presold(sid) | booked(ctx.conn, sid)
    holds = active_holds(ctx.conn, sid)
    me = ctx.user["id"] if ctx.user else None
    now = datetime.now()
    my_left = None
    sections = []
    for s in SECTIONS:
        rows = []
        for r in s["rows"]:
            seats = []
            for n in range(1, SEATS_PER_ROW + 1):
                sid_ = f"{r}{n}"
                if sid_ in sold:
                    state = "sold"
                elif sid_ in holds:
                    if holds[sid_]["user_id"] == me:
                        state = "mine"
                        left = (datetime.fromisoformat(holds[sid_]["expires"]) - now).total_seconds()
                        my_left = left if my_left is None else min(my_left, left)
                    else:
                        state = "held"
                else:
                    state = "free"
                seats.append({"id": sid_, "num": n, "state": state})
            rows.append({"label": r, "seats": seats})
        sections.append({"name": s["name"], "price": s["price"], "rows": rows})
    return {"show": show_public(show), "started": show["start"] <= now, "sections": sections,
            "aisles_after": AISLES_AFTER, "fee": FEE_PER_TICKET, "max": MAX_SEATS, "hold_minutes": HOLD_MINUTES,
            "my_hold_seconds": max(0, int(my_left)) if my_left is not None else None}


def api_hold(ctx):
    """Hold (or release) one seat while the user is choosing, so other people see it as taken."""
    user = need_user(ctx)
    sid = as_int(ctx.body.get("show"), "show")
    seat = ctx.body.get("seat")
    action = ctx.body.get("action", "hold")
    if not valid_seat(seat):
        raise ApiError(400, "That seat does not exist")
    show = load_show(ctx.conn, sid)
    if show["start"] <= datetime.now():
        raise ApiError(400, "This show has already started")
    with Transaction(ctx.conn) as conn:
        conn.execute("DELETE FROM holds WHERE showtime_id = ? AND expires <= ?", (sid, dbm.now_str()))
        if action == "release":
            conn.execute("DELETE FROM holds WHERE showtime_id = ? AND seat = ? AND user_id = ?", (sid, seat, user["id"]))
            return {"ok": True}
        if seat in presold(sid) or seat in booked(conn, sid):
            raise ApiError(409, f"Seat {seat} has just been booked by someone else")
        current = conn.execute("SELECT user_id FROM holds WHERE showtime_id = ? AND seat = ?", (sid, seat)).fetchone()
        if current and current["user_id"] != user["id"]:
            raise ApiError(409, f"Someone else is choosing seat {seat} right now")
        mine = conn.execute("SELECT COUNT(*) FROM holds WHERE showtime_id = ? AND user_id = ?", (sid, user["id"])).fetchone()[0]
        if not current and mine >= MAX_SEATS:
            raise ApiError(400, f"You can pick up to {MAX_SEATS} seats")
        expires = (datetime.now() + timedelta(minutes=HOLD_MINUTES)).isoformat(timespec="seconds")
        conn.execute("INSERT OR REPLACE INTO holds VALUES (?,?,?,?)", (sid, seat, user["id"], expires))
        conn.execute("UPDATE holds SET expires = ? WHERE showtime_id = ? AND user_id = ?", (expires, sid, user["id"]))
    return {"ok": True, "seconds": HOLD_MINUTES * 60}


# =====================================================================
# Bookings
# =====================================================================
def api_book(ctx):
    user = need_user(ctx)
    sid = as_int(ctx.body.get("show"), "show")
    seats = ctx.body.get("seats")
    if not isinstance(seats, list) or not 1 <= len(seats) <= MAX_SEATS or len(set(seats)) != len(seats):
        raise ApiError(400, f"Pick between 1 and {MAX_SEATS} seats")
    if not all(valid_seat(s) for s in seats):
        raise ApiError(400, "One of those seats does not exist")
    show = load_show(ctx.conn, sid)
    if show["start"] <= datetime.now():
        raise ApiError(400, "This show has already started")

    with Transaction(ctx.conn) as conn:     # locked: two people can never get the same seat
        conn.execute("DELETE FROM holds WHERE showtime_id = ? AND expires <= ?", (sid, dbm.now_str()))
        taken = sorted(set(seats) & (presold(sid) | booked(conn, sid)))
        held = sorted(s for s, h in active_holds(conn, sid).items() if s in seats and h["user_id"] != user["id"])
        clash = sorted(set(taken) | set(held))
        if clash:
            raise ApiError(409, "Sorry, these seats were just taken: " + ", ".join(clash), taken=clash)
        booking_id = new_booking_id()
        tickets = sum(PRICE_BY_ROW[s[0]] for s in seats)
        fee = FEE_PER_TICKET * len(seats)
        conn.execute("INSERT INTO bookings VALUES (?,?,?,?,?,?,?, 'confirmed', ?, ?)",
                     (booking_id, user["id"], sid, ",".join(sorted(seats, key=lambda s: (s[0], int(s[1:])))),
                      tickets, fee, tickets + fee, dbm.now_str(), ctx.server))
        conn.executemany("INSERT INTO booked_seats VALUES (?,?,?)", [(sid, s, booking_id) for s in seats])
        conn.execute("DELETE FROM holds WHERE showtime_id = ? AND user_id = ?", (sid, user["id"]))
    return {"booking_id": booking_id, "total": tickets + fee}


def booking_public(conn, row):
    show = load_show(conn, row["showtime_id"])
    seats = row["seats"].split(",")
    can_cancel = row["status"] == "confirmed" and show["start"] - datetime.now() >= timedelta(hours=CANCEL_HOURS_BEFORE)
    data = show_public(show)
    data["show_id"] = data.pop("id")
    data.update({"id": row["id"], "seats": seats, "classes": sorted({SECTION_BY_ROW[s[0]] for s in seats}),
                 "tickets": row["tickets"], "fee": row["fee"], "total": row["total"], "status": row["status"],
                 "created": row["created"], "booked_on": row["served_by"], "can_cancel": can_cancel,
                 "upcoming": show["start"] > datetime.now()})
    return data


def api_booking(ctx):
    user = need_user(ctx)
    row = ctx.conn.execute("""SELECT b.*, u.name, u.email FROM bookings b JOIN users u ON u.id = b.user_id
                              WHERE b.id = ?""", (ctx.param("id"),)).fetchone()
    if not row or (row["user_id"] != user["id"] and not user["is_owner"]):
        raise ApiError(404, "Booking not found")
    data = booking_public(ctx.conn, row)
    data.update(name=row["name"], email=row["email"])
    return data


def api_my_bookings(ctx):
    user = need_user(ctx)
    rows = ctx.conn.execute("SELECT * FROM bookings WHERE user_id = ? ORDER BY created DESC", (user["id"],)).fetchall()
    return {"bookings": [booking_public(ctx.conn, r) for r in rows]}


def api_cancel(ctx):
    user = need_user(ctx)
    booking_id = str(ctx.body.get("id") or "")
    with Transaction(ctx.conn) as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not row or (row["user_id"] != user["id"] and not user["is_owner"]):
            raise ApiError(404, "Booking not found")
        if row["status"] != "confirmed":
            raise ApiError(400, "This booking is already cancelled")
        show = load_show(conn, row["showtime_id"])
        if show["start"] - datetime.now() < timedelta(hours=CANCEL_HOURS_BEFORE):
            raise ApiError(400, f"Bookings can only be cancelled up to {CANCEL_HOURS_BEFORE} hours before the show")
        conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        conn.execute("DELETE FROM booked_seats WHERE booking_id = ?", (booking_id,))
    return {"ok": True, "refund": row["total"]}


# =====================================================================
# Cinema owner tools
# =====================================================================
def api_owner_summary(ctx):
    need_owner(ctx)
    conn = ctx.conn
    today = date.today().isoformat()
    rows = conn.execute("""SELECT b.*, u.name AS customer, u.email, m.title, s.day, s.time, c.name AS cinema
                           FROM bookings b JOIN users u ON u.id = b.user_id JOIN showtimes s ON s.id = b.showtime_id
                           JOIN movies m ON m.id = s.movie_id JOIN cinemas c ON c.id = s.cinema_id
                           ORDER BY b.created DESC""").fetchall()
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    by_movie, by_server = {}, {}
    for r in confirmed:
        m = by_movie.setdefault(r["title"], {"title": r["title"], "tickets": 0, "revenue": 0})
        m["tickets"] += len(r["seats"].split(","))
        m["revenue"] += r["total"]
    for r in rows:
        by_server[r["served_by"] or "?"] = by_server.get(r["served_by"] or "?", 0) + 1
    return {
        "totals": {
            "bookings": len(confirmed),
            "tickets": sum(len(r["seats"].split(",")) for r in confirmed),
            "revenue": sum(r["total"] for r in confirmed),
            "revenue_today": sum(r["total"] for r in confirmed if r["created"].startswith(today)),
            "cancelled": len(rows) - len(confirmed),
            "customers": conn.execute("SELECT COUNT(*) FROM users WHERE is_owner = 0").fetchone()[0],
        },
        "by_movie": sorted(by_movie.values(), key=lambda m: -m["revenue"]),
        "by_server": [{"server": k, "bookings": v} for k, v in sorted(by_server.items())],
        "recent": [{"id": r["id"], "customer": r["customer"], "email": r["email"], "movie": r["title"],
                    "cinema": r["cinema"], "date": r["day"], "time": nice_time(r["time"]), "seats": r["seats"],
                    "total": r["total"], "status": r["status"], "server": r["served_by"], "created": r["created"]}
                   for r in rows[:25]],
    }


def api_owner_movies(ctx):
    need_owner(ctx)
    today = date.today().isoformat()
    rows = ctx.conn.execute("""SELECT m.*, (SELECT COUNT(*) FROM showtimes s WHERE s.movie_id = m.id AND s.day >= ?) AS upcoming
                               FROM movies m ORDER BY m.status, m.id""", (today,)).fetchall()
    cinemas = ctx.conn.execute("SELECT id, name FROM cinemas ORDER BY id").fetchall()
    return {"movies": [dict(r) for r in rows], "cinemas": [dict(c) for c in cinemas]}


def api_owner_save_movie(ctx):
    need_owner(ctx)
    b = ctx.body
    fields = {
        "title": text(b, "title", 80), "genre": text(b, "genre", 40), "lang": text(b, "lang", 30),
        "length": text(b, "length", 20, required=False), "cert": text(b, "cert", 5, required=False),
        "synopsis": text(b, "synopsis", 400), "director": text(b, "director", 120, required=False),
        "starring": text(b, "starring", 200, required=False),
        "color": text(b, "color", 7), "status": text(b, "status", 10), "release": text(b, "release", 30, required=False),
    }
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", fields["color"]):
        raise ApiError(400, "Colour must look like #3B4A6B")
    if fields["status"] not in ("now", "soon", "hidden"):
        raise ApiError(400, "Status must be now, soon or hidden")
    if fields["cert"] not in ("", "U", "U/A", "A", "S"):
        raise ApiError(400, "Certificate must be U, U/A, A or S")
    cols = list(fields)
    if b.get("id"):
        movie_id = as_int(b["id"], "movie")
        if not ctx.conn.execute("SELECT 1 FROM movies WHERE id = ?", (movie_id,)).fetchone():
            raise ApiError(404, "Movie not found")
        ctx.conn.execute(f"UPDATE movies SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                         [fields[c] for c in cols] + [movie_id])
    else:
        movie_id = ctx.conn.execute(f"INSERT INTO movies ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                    [fields[c] for c in cols]).lastrowid
    return {"ok": True, "id": movie_id}


IMAGE_TYPES = {  # what the file must start with -> file extension
    "png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "jpeg": (b"\xff\xd8\xff", ".jpg"),
    "webp": (b"RIFF", ".webp"),
}
MAX_POSTER_BYTES = 3 * 1024 * 1024


def remove_poster_file(url):
    """Delete an uploaded poster file (only ever inside the uploads folder)."""
    if url and url.startswith("/uploads/posters/"):
        path = os.path.normpath(os.path.join(dbm.UPLOAD_DIR, os.path.basename(url)))
        if path.startswith(dbm.UPLOAD_DIR + os.sep) and os.path.isfile(path):
            os.remove(path)


def api_owner_poster(ctx):
    """Upload (or remove) a movie's poster. The image arrives as a data URL from the browser."""
    need_owner(ctx)
    movie_id = as_int(ctx.body.get("id"), "movie")
    row = ctx.conn.execute("SELECT poster FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if not row:
        raise ApiError(404, "Movie not found")
    if ctx.body.get("remove"):
        remove_poster_file(row["poster"])
        ctx.conn.execute("UPDATE movies SET poster = NULL WHERE id = ?", (movie_id,))
        return {"ok": True, "poster": None}

    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)", str(ctx.body.get("image") or ""))
    if not match:
        raise ApiError(400, "Please choose a PNG, JPG or WEBP image")
    try:
        data = base64.b64decode(match.group(2), validate=False)
    except (ValueError, binascii.Error):
        raise ApiError(400, "That image could not be read")
    if len(data) > MAX_POSTER_BYTES:
        raise ApiError(400, "Image is too big. Please use one under 3 MB.")
    magic, ext = IMAGE_TYPES[match.group(1)]
    if not data.startswith(magic) or (ext == ".webp" and data[8:12] != b"WEBP"):
        raise ApiError(400, "That file is not a real image")

    name = f"{movie_id}-{secrets.token_hex(6)}{ext}"
    with open(os.path.join(dbm.UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    remove_poster_file(row["poster"])
    url = "/uploads/posters/" + name
    ctx.conn.execute("UPDATE movies SET poster = ? WHERE id = ?", (url, movie_id))
    return {"ok": True, "poster": url}


def api_owner_showtimes(ctx):
    need_owner(ctx)
    dbm.ensure_schedule(ctx.conn)
    day = ctx.param("date", date.today().isoformat())
    rows = ctx.conn.execute("""SELECT s.id, s.time, s.format, m.title, c.name AS cinema,
                                 (SELECT COUNT(*) FROM booked_seats bs WHERE bs.showtime_id = s.id) AS online_seats
                               FROM showtimes s JOIN movies m ON m.id = s.movie_id JOIN cinemas c ON c.id = s.cinema_id
                               WHERE s.day = ? ORDER BY c.id, s.time, m.title""", (day,)).fetchall()
    return {"date": day, "showtimes": [dict(r, label=nice_time(r["time"])) for r in rows]}


def api_owner_add_showtime(ctx):
    need_owner(ctx)
    b = ctx.body
    movie_id = as_int(b.get("movie_id"), "movie")
    cinema_id = as_int(b.get("cinema_id"), "cinema")
    day, hhmm, fmt = str(b.get("date") or ""), str(b.get("time") or ""), str(b.get("format") or "")
    try:
        if date.fromisoformat(day) < date.today():
            raise ApiError(400, "Pick today or a future date")
    except ValueError:
        raise ApiError(400, "Bad date")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm):
        raise ApiError(400, "Time must look like 18:45")
    if fmt not in ("2D", "3D", "IMAX", "4DX"):
        raise ApiError(400, "Format must be 2D, 3D, IMAX or 4DX")
    if not ctx.conn.execute("SELECT 1 FROM movies WHERE id = ?", (movie_id,)).fetchone():
        raise ApiError(404, "Movie not found")
    if not ctx.conn.execute("SELECT 1 FROM cinemas WHERE id = ?", (cinema_id,)).fetchone():
        raise ApiError(404, "Cinema not found")
    try:
        new_id = ctx.conn.execute("INSERT INTO showtimes (movie_id, cinema_id, day, time, format) VALUES (?,?,?,?,?)",
                                  (movie_id, cinema_id, day, hhmm, fmt)).lastrowid
    except dbm.sqlite3.IntegrityError:
        raise ApiError(409, "That showtime already exists")
    return {"ok": True, "id": new_id}


def api_owner_delete_showtime(ctx):
    need_owner(ctx)
    sid = as_int(ctx.body.get("id"), "showtime")
    with Transaction(ctx.conn) as conn:
        if conn.execute("SELECT 1 FROM bookings WHERE showtime_id = ? AND status = 'confirmed'", (sid,)).fetchone():
            raise ApiError(409, "People have booked this show. Cancel their bookings first.")
        conn.execute("DELETE FROM holds WHERE showtime_id = ?", (sid,))
        conn.execute("DELETE FROM bookings WHERE showtime_id = ?", (sid,))
        if not conn.execute("DELETE FROM showtimes WHERE id = ?", (sid,)).rowcount:
            raise ApiError(404, "Showtime not found")
    return {"ok": True}


ROUTES = {
    ("GET", "/api/me"): api_me,
    ("POST", "/api/signup"): api_signup,
    ("POST", "/api/login"): api_login,
    ("POST", "/api/logout"): api_logout,
    ("GET", "/api/movies"): api_movies,
    ("GET", "/api/shows"): api_shows,
    ("GET", "/api/seats"): api_seats,
    ("POST", "/api/hold"): api_hold,
    ("POST", "/api/book"): api_book,
    ("GET", "/api/booking"): api_booking,
    ("GET", "/api/my-bookings"): api_my_bookings,
    ("POST", "/api/cancel"): api_cancel,
    ("GET", "/api/owner/summary"): api_owner_summary,
    ("GET", "/api/owner/movies"): api_owner_movies,
    ("POST", "/api/owner/movie"): api_owner_save_movie,
    ("POST", "/api/owner/poster"): api_owner_poster,
    ("GET", "/api/owner/showtimes"): api_owner_showtimes,
    ("POST", "/api/owner/showtime"): api_owner_add_showtime,
    ("POST", "/api/owner/showtime/delete"): api_owner_delete_showtime,
}


# =====================================================================
# The server itself
# =====================================================================
class Backend:
    def __init__(self, number, port):
        self.number, self.port = number, port
        self.server = None
        self.mode = "normal"        # normal | slow | errors
        self.requests = 0
        self._lock = threading.Lock()

    @property
    def name(self):
        return f"Server {self.number}"

    @property
    def running(self):
        return self.server is not None

    def start(self):
        with self._lock:
            if self.server:
                return
            srv = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.server = srv

    def stop(self):
        with self._lock:
            srv, self.server = self.server, None
        if srv:
            srv.shutdown()
            srv.server_close()


def make_handler(backend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Served-By", f"{backend.name} (port {backend.port})")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, obj, headers=()):
            self.send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", headers)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def handle_any(self, method):
            url = urlparse(self.path)
            # --- test modes ---
            if backend.mode == "slow":
                time.sleep(SLOW_DELAY)
            if backend.mode == "errors" and (url.path == "/health" or random.random() < ERROR_RATE):
                return self.send_json(500, {"error": f"{backend.name} crashed while handling this (error mode)"})
            if url.path == "/health":
                return self.send_json(200, {"ok": True})

            backend.requests += 1
            if not url.path.startswith("/api/"):
                return self.serve_file(url.path) if method == "GET" else self.send_json(404, {"error": "Not found"})

            conn = dbm.connect()
            try:
                body = {}
                if method == "POST":
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        raise ApiError(415, "Send JSON")
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "That upload is too big")
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        raise ApiError(400, "Bad request")
                    if not isinstance(body, dict):
                        raise ApiError(400, "Bad request")
                cookie = SimpleCookie(self.headers.get("Cookie") or "")
                token = cookie["session"].value if "session" in cookie else ""
                ctx = Ctx(conn, dbm.user_for_token(conn, token), parse_qs(url.query), body, backend.name)
                ctx.token = token
                route = ROUTES.get((method, url.path))
                if not route:
                    raise ApiError(404, "Not found")
                self.send_json(200, route(ctx), ctx.headers)
            except ApiError as e:
                self.send_json(e.status, {"error": e.message, **e.extra})
            except Exception:
                traceback.print_exc()
                self.send_json(500, {"error": "Something went wrong on the server"})
            finally:
                conn.close()

        def serve_file(self, path):
            if path in ("", "/"):
                path = "/index.html"
            if path.startswith("/uploads/posters/"):
                base, rel = dbm.UPLOAD_DIR, os.path.basename(path)
            else:
                base, rel = STATIC_DIR, path.lstrip("/")
            full = os.path.normpath(os.path.join(base, rel))
            ext = os.path.splitext(full)[1].lower()
            if not full.startswith(base + os.sep) or not os.path.isfile(full) or ext not in CONTENT_TYPES:
                return self.send_json(404, {"error": "Not found"})
            if base == dbm.UPLOAD_DIR and ext not in (".png", ".jpg", ".webp"):
                return self.send_json(404, {"error": "Not found"})
            with open(full, "rb") as f:
                body = f.read()
            self.send(200, body, CONTENT_TYPES[ext], [("X-Content-Type-Options", "nosniff")])

    return Handler
