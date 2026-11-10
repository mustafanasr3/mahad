#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
خادم مكتبة د. مالك بن رضا المحمدي
------------------------
بدون أي مكتبات خارجية: Python 3.8+ فقط (http.server + sqlite3).

تشغيل:        python server.py
إعادة تعيين مؤقتة: python server.py resetpass USERNAME
تغيير كلمة مرور:  python server.py setpass USERNAME NEWPASSWORD
"""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from postgres import (
    DB_ERRORS,
    DB_INTEGRITY_ERRORS,
    connect as connect_postgres,
    postgres_schema,
)

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import seed

STATIC = BASE / "static"
UPLOADS = STATIC / "uploads"
UPLOAD_TMP = BASE / ".upload_tmp"
DB_PATH = os.environ.get("DB_PATH", str(BASE / "mahad.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
SECURE_COOKIE = os.environ.get("SECURE_COOKIE", "") == "1"   # فعّله عند النشر خلف HTTPS
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"       # فعّله إن كان خلف nginx ونحوه
SESSION_DAYS = 14
MAX_JSON = 1 * 1024 * 1024
MAX_UPLOAD = 3 * 1024 * 1024
MAX_MEDIA_UPLOAD = 25 * 1024 * 1024
MAX_CHAT_UPLOAD = 25 * 1024 * 1024
MAX_BENEFIT_UPLOAD = 8 * 1024 * 1024 * 1024
BENEFIT_UPLOAD_CHUNK = 8 * 1024 * 1024
MAX_PLAYLIST_VIDEOS = 500
PLAYLIST_CACHE_TTL = 15 * 60
PLAYLIST_CACHE = {}
PLAYLIST_CACHE_LOCK = threading.Lock()

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


# ───────────────────────── قاعدة البيانات ─────────────────────────
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations (key TEXT PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  gender TEXT NOT NULL DEFAULT '' CHECK (gender IN ('','male','female')),
  password_hash TEXT NOT NULL,
  password_reset_required INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL CHECK (role IN ('supervisor','student')),
  city TEXT NOT NULL DEFAULT '',
  bio TEXT NOT NULL DEFAULT '',
  avatar_path TEXT NOT NULL DEFAULT '',
  telegram_user_id TEXT,
  telegram_chat_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_link_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS password_recovery_codes (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS programs (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  headline TEXT NOT NULL DEFAULT '',
  audience TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  cover TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS program_axes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_key TEXT NOT NULL REFERENCES programs(key) ON DELETE CASCADE ON UPDATE CASCADE,
  title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS program_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_key TEXT NOT NULL REFERENCES programs(key) ON DELETE CASCADE ON UPDATE CASCADE,
  stage TEXT NOT NULL, activity TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS faqs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  q TEXT NOT NULL, a TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'video' CHECK (kind IN ('video','playlist')),
  media_type TEXT NOT NULL DEFAULT '',
  youtube_id TEXT NOT NULL DEFAULT '',
  media_url TEXT NOT NULL DEFAULT '',
  media_path TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  thumb TEXT NOT NULL DEFAULT '',
  home_section TEXT NOT NULL DEFAULT '' CHECK (home_section IN ('','lectures','benefit')),
  program_key TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  published INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  path TEXT NOT NULL,
  original_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  program_key TEXT NOT NULL REFERENCES programs(key) ON DELETE CASCADE ON UPDATE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active')),
  created_at INTEGER NOT NULL,
  sent_at INTEGER,
  UNIQUE (user_id, program_key)
);
CREATE TABLE IF NOT EXISTS supervisor_programs (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  program_key TEXT NOT NULL REFERENCES programs(key) ON DELETE CASCADE ON UPDATE CASCADE,
  PRIMARY KEY (user_id, program_key)
);
CREATE TABLE IF NOT EXISTS saved_videos (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, video_id)
);
CREATE TABLE IF NOT EXISTS lesson_catalog (
  video_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  playlist_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lesson_audio_playlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lesson_resources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL REFERENCES lesson_catalog(video_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('file','note','audio')),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  audio_playlist_id INTEGER REFERENCES lesson_audio_playlists(id) ON DELETE SET NULL,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS benefits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('video','audio')),
  path TEXT NOT NULL,
  youtube_id TEXT NOT NULL DEFAULT '',
  original_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  published INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  media_url TEXT NOT NULL DEFAULT '',
  media_path TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  published INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sermons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  speaker TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  media_type TEXT NOT NULL CHECK (media_type IN ('video','audio')),
  media_url TEXT NOT NULL DEFAULT '',
  media_path TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  published INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  image_path TEXT NOT NULL DEFAULT '',
  image_url TEXT NOT NULL DEFAULT '',
  youtube_url TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  published INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS benefit_uploads (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('video','audio')),
  original_name TEXT NOT NULL,
  extension TEXT NOT NULL,
  total_size INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS benefit_upload_chunks (
  token TEXT NOT NULL REFERENCES benefit_uploads(token) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL,
  PRIMARY KEY (token,chunk_index)
);
CREATE TABLE IF NOT EXISTS lesson_progress (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL REFERENCES lesson_catalog(video_id) ON DELETE CASCADE,
  position_seconds INTEGER NOT NULL DEFAULT 0,
  duration_seconds INTEGER NOT NULL DEFAULT 0,
  completed INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, video_id)
);
CREATE TABLE IF NOT EXISTS lesson_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL REFERENCES lesson_catalog(video_id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  answer TEXT NOT NULL DEFAULT '',
  answered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL,
  answered_at INTEGER
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recipient_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  gender TEXT NOT NULL CHECK (gender IN ('male','female','both')),
  body TEXT NOT NULL,
  attachment_path TEXT NOT NULL DEFAULT '',
  attachment_name TEXT NOT NULL DEFAULT '',
  attachment_type TEXT NOT NULL DEFAULT '',
  read_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('chat','question','answer')),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '',
  related_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL,
  read_at INTEGER
);
CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  word_id INTEGER REFERENCES words(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  image_path TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS live_rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  event_url TEXT NOT NULL,
  audience TEXT NOT NULL DEFAULT 'both' CHECK (audience IN ('both','male','female')),
  status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','live','ended')),
  starts_at INTEGER,
  published INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_key TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  username TEXT NOT NULL,
  gender TEXT NOT NULL DEFAULT '' CHECK (gender IN ('','male','female')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered')),
  created_at INTEGER NOT NULL,
  delivered_at INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, telegram TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  reply_text TEXT NOT NULL DEFAULT '',
  replied_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_enroll_prog ON enrollments(program_key);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_telegram_link_expiry ON telegram_link_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_lesson_resources_video ON lesson_resources(video_id, kind, id);
CREATE INDEX IF NOT EXISTS idx_lesson_questions_video ON lesson_questions(video_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_group ON chat_messages(gender, recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,read_at,id);
CREATE INDEX IF NOT EXISTS idx_announcements_active ON announcements(active,id);
CREATE INDEX IF NOT EXISTS idx_live_rooms_schedule ON live_rooms(published, starts_at);
CREATE INDEX IF NOT EXISTS idx_benefits_published ON benefits(published,sort,id);
CREATE INDEX IF NOT EXISTS idx_books_published ON books(published,sort,id);
CREATE INDEX IF NOT EXISTS idx_sermons_published ON sermons(published,sort,id);
CREATE INDEX IF NOT EXISTS idx_words_published ON words(published,sort,id);
"""

_PG_ID_TABLES = frozenset(
    match.group(1).lower()
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\);", SCHEMA, re.S)
    if re.search(r"\bid INTEGER PRIMARY KEY AUTOINCREMENT\b", match.group(2), re.I)
)


def db():
    if DATABASE_URL:
        return connect_postgres(DATABASE_URL, _PG_ID_TABLES)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn, table):
    if DATABASE_URL:
        return {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=?",
                (table,),
            )
        }
    return {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def now():
    return int(time.time())


def add_notification(conn, user_id, kind, title, body="", target="", related_user_id=None, created_at=None):
    conn.execute(
        "INSERT INTO notifications(user_id,kind,title,body,target,related_user_id,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, kind, title, body, target, related_user_id, created_at or now()))


def init_db():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
    conn = db()
    if DATABASE_URL:
        conn.executescript(postgres_schema(SCHEMA))
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    cutoff = now() - 24 * 60 * 60
    for upload in rows(conn, "SELECT token FROM benefit_uploads WHERE created_at<?", (cutoff,)):
        staged_path = UPLOAD_TMP / (upload["token"] + ".part")
        if staged_path.exists():
            staged_path.unlink()
    conn.execute("DELETE FROM benefit_uploads WHERE created_at<?", (cutoff,))
    resource_columns = table_columns(conn, "lesson_resources")
    if "audio_playlist_id" not in resource_columns:
        conn.execute("ALTER TABLE lesson_resources ADD COLUMN audio_playlist_id INTEGER REFERENCES lesson_audio_playlists(id) ON DELETE SET NULL")
    chat_columns = table_columns(conn, "chat_messages")
    for column, definition in (("attachment_path", "TEXT NOT NULL DEFAULT ''"),
                               ("attachment_name", "TEXT NOT NULL DEFAULT ''"),
                               ("attachment_type", "TEXT NOT NULL DEFAULT ''"),
                               ("read_at", "INTEGER")):
        if column not in chat_columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN %s %s" % (column, definition))
    message_columns = table_columns(conn, "messages")
    for column, definition in (("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
                               ("reply_text", "TEXT NOT NULL DEFAULT ''"),
                               ("replied_at", "INTEGER")):
        if column not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN %s %s" % (column, definition))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_inbox ON chat_messages(recipient_id,read_at,id)")
    user_columns = table_columns(conn, "users")
    if "gender" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN gender TEXT NOT NULL DEFAULT ''")
    if "avatar_path" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT NOT NULL DEFAULT ''")
    if "password_reset_required" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_reset_required INTEGER NOT NULL DEFAULT 0")
    if "telegram_user_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN telegram_user_id TEXT")
    if "telegram_chat_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_user ON users(telegram_user_id) "
                 "WHERE telegram_user_id IS NOT NULL")
    outbox_columns = table_columns(conn, "outbox")
    if "gender" not in outbox_columns:
        conn.execute("ALTER TABLE outbox ADD COLUMN gender TEXT NOT NULL DEFAULT ''")
    video_columns = table_columns(conn, "videos")
    if "category" not in video_columns:
        conn.execute("ALTER TABLE videos ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    if "media_type" not in video_columns:
        conn.execute("ALTER TABLE videos ADD COLUMN media_type TEXT NOT NULL DEFAULT ''")
    if "media_url" not in video_columns:
        conn.execute("ALTER TABLE videos ADD COLUMN media_url TEXT NOT NULL DEFAULT ''")
    if "media_path" not in video_columns:
        conn.execute("ALTER TABLE videos ADD COLUMN media_path TEXT NOT NULL DEFAULT ''")
    benefit_columns = table_columns(conn, "benefits")
    if "youtube_id" not in benefit_columns:
        conn.execute("ALTER TABLE benefits ADD COLUMN youtube_id TEXT NOT NULL DEFAULT ''")
    word_columns = table_columns(conn, "words")
    if "image_url" not in word_columns:
        conn.execute("ALTER TABLE words ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
    if "youtube_url" not in word_columns:
        conn.execute("ALTER TABLE words ADD COLUMN youtube_url TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT OR IGNORE INTO supervisor_programs (user_id,program_key) "
                 "SELECT u.id,p.key FROM users u CROSS JOIN programs p WHERE u.role='admin'")
    conn.execute("UPDATE users SET role='supervisor', name=CASE WHEN name='مدير الموقع' "
                 "THEN 'مشرف الموقع' ELSE name END WHERE role='admin'")
    if not one(conn, "SELECT 1 AS x FROM schema_migrations WHERE key='malik_library_v1'"):
        conn.execute("DELETE FROM outbox")
        conn.execute("DELETE FROM enrollments")
        conn.execute("DELETE FROM supervisor_programs")
        conn.execute("DELETE FROM saved_videos")
        conn.execute("DELETE FROM videos")
        conn.execute("DELETE FROM program_axes")
        conn.execute("DELETE FROM program_plan")
        conn.execute("DELETE FROM programs")
        conn.execute("DELETE FROM faqs")
        conn.execute("DELETE FROM settings")
        conn.executemany("INSERT INTO settings (key,value) VALUES (?,?)", seed.SETTINGS.items())
        t = now()
        for i, (q, a) in enumerate(seed.FAQS):
            conn.execute("INSERT INTO faqs (q,a,sort) VALUES (?,?,?)", (q, a, i))
        for title, playlist_id, category, thumb_id, sort in seed.VIDEOS:
            thumb = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % thumb_id
            conn.execute(
                "INSERT INTO videos (title,kind,youtube_id,category,thumb,description,sort,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (title, "playlist", playlist_id, category, thumb, "قائمة تشغيل من القناة الرسمية.", sort, t))
        conn.execute("INSERT INTO schema_migrations (key,applied_at) VALUES (?,?)",
                     ("malik_library_v1", t))
    conn.executemany("INSERT OR IGNORE INTO categories (key,name,description,sort) VALUES (?,?,?,?)",
                     [(key, name, description, index)
                      for index, (key, name, description) in enumerate(seed.CATEGORIES)])
    if not one(conn, "SELECT 1 AS x FROM schema_migrations WHERE key='library_description_v2'"):
        conn.execute("UPDATE settings SET value=? WHERE key='lectures_sub' AND value=?",
                     ("هنا يعرض البرامج و التعليقات والشروح المرتبطة بمعهد الدكتور -وفقه الله-",
                      "اختر التخصص، ثم افتح قائمة التشغيل لاستعراض دروسها ومتابعتها."))
        conn.execute("INSERT INTO schema_migrations (key,applied_at) VALUES (?,?)",
                     ("library_description_v2", now()))
    conn.executemany("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", seed.SOCIAL_LINKS.items())
    conn.execute("UPDATE settings SET value=REPLACE(value,'المالكي','المحمدي') "
                 "WHERE value LIKE '%المالكي%'")
    conn.execute("UPDATE faqs SET q=REPLACE(q,'المالكي','المحمدي'),"
                 "a=REPLACE(a,'المالكي','المحمدي') WHERE q LIKE '%المالكي%' OR a LIKE '%المالكي%'")
    library_description = "قناة علمية تنشر فيها برامج ودروس وفوائد الشيخ د. مالك المحمدي - وفَّقه الله -"
    requested_settings = {
        "site_name": "مالك بن رضا المحمدي",
        "hero_tag": "مكتبة العلوم الشرعية",
        "hero_title": "مالك بن رضا المحمدي",
        "hero_subtitle": "المعهد الرسمي للدكتور",
        "hero_text": library_description,
        "footer_about": library_description,
        "copyright": "© 2026 مالك بن رضا المحمدي · جميع الحقوق محفوظة",
    }
    conn.executemany("INSERT INTO settings (key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     requested_settings.items())
    # حساب المشرف الأول، بصلاحيات كاملة على الموقع.
    if not one(conn, "SELECT 1 AS x FROM users WHERE role='supervisor' LIMIT 1"):
        user = norm_username(os.environ.get("ADMIN_USER", "admin"))
        pw = secrets.token_urlsafe(18)
        cur = conn.execute("INSERT INTO users "
                           "(username,name,password_hash,password_reset_required,role,created_at) "
                           "VALUES (?,?,?,?,?,?)",
                           (user, "مشرف الموقع", hash_pw(pw), 1, "supervisor", now()))
        conn.executemany("INSERT OR IGNORE INTO supervisor_programs (user_id,program_key) VALUES (?,?)",
                         [(cur.lastrowid, r["key"]) for r in rows(conn, "SELECT key FROM programs")])
        print("=" * 60)
        print(" تم إنشاء حساب المشرف (يظهر هذا مرة واحدة فقط):")
        print("   معرّف الدخول :", user)
        print(" كلمة المرور المؤقتة:", pw)
        print(" يجب تغييرها بعد تسجيل الدخول الأول.")
        print("=" * 60)
    conn.execute("INSERT OR IGNORE INTO supervisor_programs (user_id,program_key) "
                 "SELECT u.id,p.key FROM users u CROSS JOIN programs p WHERE u.role='supervisor'")
    conn.execute("UPDATE videos SET media_type=CASE kind WHEN 'playlist' THEN 'playlist' ELSE 'video' END "
                 "WHERE media_type=''")
    terms = one(conn, "SELECT value FROM settings WHERE key='terms_text'")
    notice = "٦) يُسجّل تقدم الطالب في الدروس وآخر موضع للمشاهدة، ويطّلع عليه المشرفون للمتابعة التعليمية."
    if terms:
        terms_text = terms["value"]
        terms_text = terms_text.replace(
            "تُخزّن رسائل التواصل والأسئلة، وتظهر أسئلة الدروس وإجاباتها للطالب صاحب السؤال والمشرفين. ", "")
        terms_text = terms_text.replace(
            "تقتصر المحادثات والأسئلة ومتابعة الطلاب على مشرفي الجنس نفسه، مع بقاء صلاحيات إدارة الموقع كاملة.", "")
        terms_text = terms_text.replace(
            "٧) تقتصر المحادثات والأسئلة ومتابعة الطلاب على مشرفي الجنس نفسه، مع بقاء صلاحيات إدارة الموقع كاملة.", "")
        if "تقدم الطالب في الدروس وآخر موضع" not in terms_text:
            terms_text = terms_text.rstrip() + "\n" + notice
        if terms_text != terms["value"]:
            conn.execute("UPDATE settings SET value=? WHERE key='terms_text'", (terms_text,))
    conn.commit()
    conn.close()


# ───────────────────────── كلمات المرور والجلسات ─────────────────────────
def hash_pw(pw, iters=240_000):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters)
    return "pbkdf2$%d$%s$%s" % (iters, salt.hex(), dk.hex())


def verify_pw(pw, stored):
    try:
        _, iters, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


DUMMY_HASH = hash_pw("dummy-password-for-timing")


def norm_username(s):
    return (s or "").strip().lower().lstrip("@")


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    t = now()
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                 (hashlib.sha256(token.encode()).hexdigest(), user_id, t, t + SESSION_DAYS * 86400))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (t,))
    return token


def session_user(conn, token):
    if not token:
        return None
    return one(conn, """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                        WHERE s.token_hash = ? AND s.expires_at > ?""",
               (hashlib.sha256(token.encode()).hexdigest(), now()))


_rl, _rl_lock = {}, threading.Lock()


def rl_count(key, window):
    t = time.time()
    with _rl_lock:
        arr = [x for x in _rl.get(key, []) if t - x < window]
        _rl[key] = arr
        return len(arr)


def rl_add(key):
    with _rl_lock:
        _rl.setdefault(key, []).append(time.time())


# ───────────────────────── أدوات مساعدة ─────────────────────────
class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


class Ctx:
    def __init__(self, conn, user, body, query, ip, raw, ctype):
        self.conn, self.user, self.body, self.query = conn, user, body, query
        self.ip, self.raw, self.ctype = ip, raw, ctype
        self.cookies = []

    @property
    def has_full_access(self):
        return bool(self.user) and self.user["role"] in ("admin", "supervisor")

    def sup_programs(self):
        return [r["program_key"] for r in rows(self.conn, "SELECT program_key FROM supervisor_programs WHERE user_id=?", (self.user["id"],))]

    def allowed_programs(self):
        if self.has_full_access:
            return [r["key"] for r in rows(self.conn, "SELECT key FROM programs")]
        return self.sup_programs()


def lesson_video_id(value):
    kind, video_id = parse_youtube(value)
    if kind != "video":
        raise ApiError(400, "أدخل رابط فيديو يوتيوب أو معرّفه")
    return video_id


def streamyard_url(value):
    value = text({"url": value}, "url", "رابط بث StreamYard", 1, 1000)
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not (host == "streamyard.com" or host.endswith(".streamyard.com"))
            or parsed.username or parsed.password):
        raise ApiError(400, "استخدم رابط مشاهدة StreamYard يبدأ بـ https://streamyard.com")
    return value


def text(body, key, label, lo=0, hi=200, required=True):
    v = body.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ApiError(400, "قيمة غير صالحة: " + label)
    v = v.strip()
    if (required and not v) or len(v) < lo:
        raise ApiError(400, "من فضلك أكمل: " + label)
    if len(v) > hi:
        raise ApiError(400, "«%s» أطول من المسموح (%d حرفًا)" % (label, hi))
    return v


def integer(body, key, default=0):
    try:
        return int(body.get(key, default))
    except (TypeError, ValueError):
        return default


def valid_program_keys(conn, keys):
    if not isinstance(keys, list):
        return []
    have = {r["key"] for r in rows(conn, "SELECT key FROM programs")}
    return [k for k in dict.fromkeys(keys) if isinstance(k, str) and k in have]


def clean_path(p, label="المسار"):
    p = (p or "").strip()
    if not p:
        return ""
    if len(p) > 300 or not (p.startswith("/") or p.startswith("https://")) or p.startswith("//") or any(c in p for c in "\"'<>\\ "):
        raise ApiError(400, label + " غير صالح")
    return p


def youtube_post_url(value):
    value = text({"url": value}, "url", "رابط منشور يوتيوب", 0, 2000, required=False)
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if (parsed.scheme != "https" or not (host == "youtube.com" or host.endswith(".youtube.com"))
            or parsed.username or parsed.password or len(parts) < 2 or parts[-2] != "post"
            or not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", parts[-1])):
        raise ApiError(400, "أدخل رابط منشور يوتيوب كاملًا من نوع /post/...")
    return "https://www.youtube.com/post/" + parts[-1]


def external_https_url(value, label):
    value = text({"url": value}, "url", label, 0, 2000, required=False)
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError(400, label + " يجب أن يبدأ برابط HTTPS صالح")
    return value


def uploaded_media_extension(path, label):
    match = re.fullmatch(r"/uploads/[a-f0-9]{32}\.([a-z0-9]{2,5})", path or "")
    if not match:
        raise ApiError(400, label + " غير صالح")
    if not (UPLOADS / Path(path).name).is_file():
        raise ApiError(400, label + " غير موجود")
    return match.group(1)


def delete_uploaded_media(path):
    if re.fullmatch(r"/uploads/[a-f0-9]{32}\.[a-z0-9]{2,5}", path or ""):
        target = UPLOADS / Path(path).name
        if target.is_file():
            target.unlink()


def parse_youtube(raw):
    """يُرجع (kind, id) من رابط يوتيوب أو معرّف مجرّد. ('', '') إن كان الحقل فارغًا."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    kind = vid = None
    looks_url = re.match(r"^(https?://|www\.|m\.|music\.|youtu)", raw, re.I)
    if looks_url:
        if not raw.lower().startswith("http"):
            raw = "https://" + raw
        u = urlparse(raw)
        host = (u.hostname or "").lower()
        if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
                or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")):
            raise ApiError(400, "الرابط ليس رابط يوتيوب")
        qs, parts = parse_qs(u.query), [p for p in u.path.split("/") if p]
        if host == "youtu.be" and parts:
            kind, vid = "video", parts[0]
        elif parts[:1] == ["playlist"] and "list" in qs:
            kind, vid = "playlist", qs["list"][0]
        elif parts[:1] in (["embed"], ["shorts"], ["live"], ["v"]) and len(parts) > 1:
            if parts[1] == "videoseries" and "list" in qs:
                kind, vid = "playlist", qs["list"][0]
            else:
                kind, vid = "video", parts[1]
        elif "v" in qs:
            kind, vid = "video", qs["v"][0]
        elif "list" in qs:
            kind, vid = "playlist", qs["list"][0]
    else:
        vid = raw
        kind = "playlist" if re.match(r"^(PL|UU|OL|FL|RD|LL)", raw) and len(raw) >= 13 else "video"
    if not vid or not YT_ID_RE.match(vid):
        raise ApiError(400, "تعذّر فهم رابط يوتيوب؛ الصق رابط الفيديو أو قائمة التشغيل كاملًا")
    return kind, vid


def video_url(kind, yid):
    if not yid:
        return ""
    if kind == "playlist":
        return "https://www.youtube.com/playlist?list=" + yid
    return "https://www.youtube.com/watch?v=" + yid


def video_public(v, category_names=None):
    v = dict(v)
    media_type = v.get("media_type") or ("playlist" if v["kind"] == "playlist" else "video")
    thumb = v["thumb"]
    if not thumb and media_type == "video" and YT_ID_RE.fullmatch(v["youtube_id"] or ""):
        thumb = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % v["youtube_id"]
    v["thumb_url"] = thumb
    v["media_type"] = media_type
    v["url"] = v.get("media_url") or video_url(v["kind"], v["youtube_id"])
    v["category_name"] = (category_names or {}).get(v["category"], "")
    v["published"] = bool(v["published"])
    return v


def playlist_video_items(payload):
    videos, seen = [], set()

    def walk(node):
        if isinstance(node, dict):
            lockup = node.get("lockupViewModel")
            renderer = node.get("playlistVideoRenderer")
            if isinstance(lockup, dict):
                video_id = lockup.get("contentId", "")
                metadata = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
                title = metadata.get("title", {}).get("content", "")
            elif isinstance(renderer, dict):
                video_id = renderer.get("videoId", "")
                title_data = renderer.get("title", {})
                title = title_data.get("simpleText", "")
                if not title:
                    title = "".join(part.get("text", "") for part in title_data.get("runs", []))
            else:
                video_id, title = "", ""
            if YT_ID_RE.match(video_id) and video_id not in seen and title:
                seen.add(video_id)
                videos.append({
                    "id": video_id,
                    "title": title,
                    "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % video_id,
                })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return videos


def playlist_continuation(payload):
    tokens = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("continuationItemViewModel", "continuationItemRenderer"):
                item = node.get(key)
                if not isinstance(item, dict):
                    continue
                command = item.get("continuationCommand", item.get("continuationEndpoint", {}))
                if isinstance(command, dict):
                    command = command.get("innertubeCommand", command)
                    continuation = command.get("continuationCommand", {})
                    token = continuation.get("token") if isinstance(continuation, dict) else ""
                    if token:
                        tokens.append(token)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return tokens[0] if tokens else ""


def fetch_playlist_videos(playlist_id):
    with PLAYLIST_CACHE_LOCK:
        cached = PLAYLIST_CACHE.get(playlist_id)
        if cached and cached[0] > time.time():
            return cached[1]

    videos, seen_tokens = [], set()
    continuation = ""
    for _ in range(25):
        body = {
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00"}},
            "browseId": "VL" + playlist_id,
        }
        if continuation:
            body = {"context": body["context"], "continuation": continuation}
        request = Request(
            "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.youtube.com",
                "Referer": "https://www.youtube.com/",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=12) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ApiError(502, "استجابة قائمة التشغيل من يوتيوب كبيرة جدًا")
            payload = json.loads(raw.decode("utf-8"))
        except (URLError, TimeoutError, OSError):
            raise ApiError(502, "تعذّر تحميل فيديوهات قائمة التشغيل من يوتيوب")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(502, "تعذّر قراءة قائمة التشغيل من يوتيوب")
        if not isinstance(payload, dict) or "error" in payload:
            raise ApiError(502, "لم يتمكن يوتيوب من عرض فيديوهات قائمة التشغيل")

        videos.extend(playlist_video_items(payload))
        if len(videos) >= MAX_PLAYLIST_VIDEOS:
            videos = videos[:MAX_PLAYLIST_VIDEOS]
            break
        continuation = playlist_continuation(payload)
        if not continuation or continuation in seen_tokens:
            break
        seen_tokens.add(continuation)

    with PLAYLIST_CACHE_LOCK:
        PLAYLIST_CACHE[playlist_id] = (time.time() + PLAYLIST_CACHE_TTL, videos)
    return videos


def user_public(u):
    out = {k: u[k] for k in ("id", "name", "username", "gender", "role", "city", "bio",
                             "avatar_path", "created_at")}
    out["password_reset_required"] = bool(u["password_reset_required"])
    out["telegram_linked"] = bool(u["telegram_user_id"])
    return out


def telegram_api(method, payload, timeout=15):
    if not TELEGRAM_BOT_TOKEN:
        raise ApiError(503, "استعادة كلمة المرور عبر تيليجرام غير مفعّلة بعد")
    url = "https://api.telegram.org/bot%s/%s" % (TELEGRAM_BOT_TOKEN, method)
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except HTTPError:
        raise ApiError(502, "تعذّر الاتصال بخدمة تيليجرام")
    except (URLError, TimeoutError, OSError):
        raise ApiError(502, "تعذّر الاتصال بخدمة تيليجرام")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(502, "استجابة تيليجرام غير صالحة")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ApiError(502, "تعذّر إكمال طلب تيليجرام")
    return result.get("result")


def telegram_send(chat_id, message):
    return telegram_api("sendMessage", {"chat_id": chat_id, "text": message}, timeout=10)


def telegram_link_update(message):
    if not isinstance(message, dict):
        return
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    text_value = message.get("text") or ""
    match = re.fullmatch(r"/start(?:@\w+)?(?:\s+([A-Za-z0-9_-]{32,64}))?", text_value.strip())
    if chat.get("type") != "private" or not sender.get("id"):
        return
    chat_id = str(chat.get("id", ""))
    sender_id = str(sender["id"])
    if match and match.group(1):
        token_hash = hashlib.sha256(match.group(1).encode()).hexdigest()
        conn = db()
        try:
            link = one(conn, "SELECT user_id,expires_at FROM telegram_link_tokens WHERE token_hash=?",
                       (token_hash,))
            if not link or link["expires_at"] <= now():
                conn.execute("DELETE FROM telegram_link_tokens WHERE token_hash=?", (token_hash,))
                conn.commit()
                telegram_send(chat_id, "انتهت صلاحية رابط الربط. سجّل الدخول إلى الموقع واطلب رابطًا جديدًا.")
                return
            other = one(conn, "SELECT id FROM users WHERE telegram_user_id=? AND id<>?",
                        (sender_id, link["user_id"]))
            if other:
                conn.execute("DELETE FROM telegram_link_tokens WHERE token_hash=?", (token_hash,))
                conn.commit()
                telegram_send(chat_id, "حساب تيليجرام هذا مرتبط بحساب آخر في الموقع.")
                return
            conn.execute("UPDATE users SET telegram_user_id=?,telegram_chat_id=? WHERE id=?",
                         (sender_id, chat_id, link["user_id"]))
            conn.execute("DELETE FROM telegram_link_tokens WHERE user_id=?", (link["user_id"],))
            conn.commit()
            telegram_send(chat_id, "تم ربط حساب تيليجرام بنجاح. يمكنك الآن استعادة كلمة المرور من الموقع.")
            return
        finally:
            conn.close()
    telegram_send(chat_id, "لربط حسابك، سجّل الدخول إلى الموقع ثم افتح حسابي واختر ربط تيليجرام.")


def telegram_poll_loop():
    global TELEGRAM_BOT_USERNAME
    offset = 0
    while True:
        try:
            if not TELEGRAM_BOT_USERNAME:
                bot = telegram_api("getMe", {}, timeout=10)
                if not isinstance(bot, dict) or not bot.get("username"):
                    raise ApiError(502, "لم يتمكن تيليجرام من تحديد اسم البوت")
                TELEGRAM_BOT_USERNAME = bot.get("username", "")
            updates = telegram_api("getUpdates", {
                "offset": offset, "timeout": 25, "allowed_updates": ["message"]
            }, timeout=35)
            if not isinstance(updates, list):
                raise ApiError(502, "استجابة تحديثات تيليجرام غير صالحة")
            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                telegram_link_update(update.get("message"))
        except ApiError as exc:
            print("تعذّر تحديث بوت تيليجرام: %s" % exc.msg, file=sys.stderr)
            time.sleep(5)
        except (AttributeError, TypeError, ValueError):
            print("تجاهل تحديث تيليجرام غير صالح", file=sys.stderr)
            time.sleep(1)


def recovery_hash(code):
    return hmac.new(TELEGRAM_BOT_TOKEN.encode("utf-8"), code.encode("ascii"),
                    hashlib.sha256).hexdigest()


def me_payload(conn, user):
    if not user:
        return {"user": None}
    out = {"user": user_public(user)}
    out["enrollments"] = []
    out["saved"] = [r["video_id"] for r in rows(conn, "SELECT video_id FROM saved_videos WHERE user_id=? ORDER BY created_at DESC", (user["id"],))]
    if user["role"] == "supervisor":
        out["programs"] = [r["program_key"] for r in rows(conn, "SELECT program_key FROM supervisor_programs WHERE user_id=?", (user["id"],))]
    return out


def set_cookie(ctx, token, max_age):
    c = "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (token, max_age)
    if SECURE_COOKIE:
        c += "; Secure"
    ctx.cookies.append(c)


def check_password_rules(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        raise ApiError(400, "كلمة المرور يجب ألا تقل عن ٨ أحرف")
    if len(pw) > 128:
        raise ApiError(400, "كلمة المرور طويلة جدًا")


# ───────────────────────── المسارات ─────────────────────────
ROUTES = []


def route(method, pattern, auth=None, raw=False):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, auth, raw, fn))
        return fn
    return deco


# ---- عام ----
@route("GET", r"/api/site")
def api_site(ctx):
    c = ctx.conn
    settings = {r["key"]: r["value"] for r in rows(c, "SELECT * FROM settings")}
    category_rows = rows(c, "SELECT key,name,description FROM categories ORDER BY sort,name")
    category_names = {category["key"]: category["name"] for category in category_rows}
    videos = [video_public(v, category_names)
              for v in rows(c, "SELECT * FROM videos WHERE published=1 ORDER BY sort, id")]
    for v in videos:
        if v["kind"] == "video" and v["youtube_id"]:
            c.execute("INSERT INTO lesson_catalog (video_id,title,updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at",
                      (v["youtube_id"], v["title"], now()))
    playlist_files = rows(c, "SELECT f.id,f.video_id,f.title,f.path,f.original_name,f.size_bytes,"
                            "v.title AS playlist_title FROM playlist_files f "
                            "JOIN videos v ON v.id=f.video_id "
                            "WHERE v.kind='playlist' AND v.published=1 ORDER BY v.sort,v.id,f.id")
    books = rows(c, "SELECT id,title,author,description,media_url,media_path,sort "
                    "FROM books WHERE published=1 ORDER BY sort,id DESC LIMIT 500")
    sermons = rows(c, "SELECT id,title,speaker,description,media_type,media_url,media_path,sort "
                      "FROM sermons WHERE published=1 ORDER BY sort,id DESC LIMIT 500")
    announcement = one(c, "SELECT id,title,body,image_path,created_at FROM announcements "
                           "WHERE active=1 ORDER BY id DESC LIMIT 1")
    return {"settings": settings, "categories": category_rows, "programs": [],
            "faqs": rows(c, "SELECT id,q,a FROM faqs ORDER BY sort, id"),
            "videos": videos, "playlist_files": playlist_files, "books": books, "sermons": sermons,
            "words": rows(c, "SELECT id,title,body,image_path,image_url,youtube_url,sort,created_at "
                             "FROM words WHERE published=1 ORDER BY sort,id DESC LIMIT 500"),
            "benefits": rows(c, "SELECT id,title,description,kind,path,youtube_id,original_name,size_bytes,sort "
                                 "FROM benefits WHERE published=1 AND kind='video' "
                                 "ORDER BY sort,id DESC LIMIT 500"),
            "announcement": announcement}


@route("GET", r"/api/playlists/([A-Za-z0-9_-]{6,64})")
def api_playlist_videos(ctx, playlist_id):
    playlist = one(ctx.conn, "SELECT id FROM videos WHERE kind='playlist' AND youtube_id=? AND published=1",
                   (playlist_id,))
    if not playlist:
        raise ApiError(404, "قائمة التشغيل غير موجودة")
    videos = fetch_playlist_videos(playlist_id)
    for item in videos:
        ctx.conn.execute("INSERT INTO lesson_catalog (video_id,title,playlist_id,updated_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,"
                         "playlist_id=COALESCE(lesson_catalog.playlist_id,excluded.playlist_id),"
                         "updated_at=excluded.updated_at",
                         (item["id"], item["title"], playlist["id"], now()))
    return {"videos": videos}


@route("GET", r"/api/me")
def api_me(ctx):
    return me_payload(ctx.conn, ctx.user)


@route("POST", r"/api/me/telegram/link", "user")
def api_telegram_link(ctx):
    global TELEGRAM_BOT_USERNAME
    if ctx.user["telegram_user_id"]:
        return {"linked": True}
    if not TELEGRAM_BOT_TOKEN:
        raise ApiError(503, "ربط تيليجرام غير متاح حاليًا؛ لم يُعدّ المشرف البوت بعد")
    if not TELEGRAM_BOT_USERNAME:
        bot = telegram_api("getMe", {}, timeout=10)
        TELEGRAM_BOT_USERNAME = bot.get("username", "") if isinstance(bot, dict) else ""
    if not isinstance(TELEGRAM_BOT_USERNAME, str) or not re.fullmatch(
            r"[A-Za-z0-9_]{5,32}", TELEGRAM_BOT_USERNAME):
        raise ApiError(503, "تعذّر تحديد اسم بوت تيليجرام")
    token = secrets.token_urlsafe(32)
    ctx.conn.execute("DELETE FROM telegram_link_tokens WHERE user_id=?", (ctx.user["id"],))
    ctx.conn.execute("INSERT INTO telegram_link_tokens(token_hash,user_id,created_at,expires_at) "
                     "VALUES (?,?,?,?)",
                     (hashlib.sha256(token.encode()).hexdigest(), ctx.user["id"], now(), now() + 600))
    return {"linked": False, "url": "https://t.me/%s?start=%s" % (TELEGRAM_BOT_USERNAME, token)}


@route("POST", r"/api/password/recovery")
def api_password_recovery(ctx):
    if not TELEGRAM_BOT_TOKEN:
        raise ApiError(503, "استعادة كلمة المرور عبر تيليجرام غير مفعّلة بعد")
    if rl_count("recovery-ip:" + ctx.ip, 3600) >= 20:
        raise ApiError(429, "محاولات كثيرة؛ حاول لاحقًا")
    raw_username = ctx.body.get("username")
    username = norm_username(raw_username if isinstance(raw_username, str) else "")
    if not USERNAME_RE.fullmatch(username):
        raise ApiError(400, "أدخل معرّف الحساب")
    rl_add("recovery-ip:" + ctx.ip)
    user = one(ctx.conn, "SELECT id,telegram_chat_id FROM users WHERE username=?", (username,))
    account_key = "recovery-user:" + str(user["id"]) if user else ""
    if user and user["telegram_chat_id"] and rl_count(account_key, 3600) < 3:
        rl_add(account_key)
        code = "%08d" % secrets.randbelow(100000000)
        created_at = now()
        ctx.conn.execute(
            "INSERT INTO password_recovery_codes(user_id,code_hash,created_at,expires_at,attempts) "
            "VALUES (?,?,?,?,0) ON CONFLICT(user_id) DO UPDATE SET "
            "code_hash=excluded.code_hash,created_at=excluded.created_at,"
            "expires_at=excluded.expires_at,attempts=0",
            (user["id"], recovery_hash(code), created_at, created_at + 600))
        try:
            telegram_send(user["telegram_chat_id"],
                          "رمز استعادة كلمة المرور: %s\nينتهي خلال ١٠ دقائق. لا تشاركه مع أحد." % code)
        except ApiError as exc:
            ctx.conn.execute("DELETE FROM password_recovery_codes WHERE user_id=?", (user["id"],))
            print("تعذّر إرسال رمز استعادة تيليجرام: %s" % exc.msg, file=sys.stderr)
    return {"ok": True, "message": "إذا كان الحساب مرتبطًا بتيليجرام، فسيصل رمز الاستعادة قريبًا."}


@route("POST", r"/api/password/recovery/verify")
def api_password_recovery_verify(ctx):
    if not TELEGRAM_BOT_TOKEN:
        raise ApiError(503, "استعادة كلمة المرور عبر تيليجرام غير مفعّلة بعد")
    if rl_count("recovery-verify-ip:" + ctx.ip, 3600) >= 30:
        raise ApiError(429, "محاولات كثيرة؛ حاول لاحقًا")
    raw_username = ctx.body.get("username")
    username = norm_username(raw_username if isinstance(raw_username, str) else "")
    code = ctx.body.get("code") if isinstance(ctx.body.get("code"), str) else ""
    password = ctx.body.get("password")
    if not USERNAME_RE.fullmatch(username):
        raise ApiError(400, "معرّف الحساب غير صالح")
    check_password_rules(password)
    if len(password) < 12:
        raise ApiError(400, "كلمة المرور الجديدة يجب ألا تقل عن ١٢ حرفًا")
    if ctx.body.get("password2") != password:
        raise ApiError(400, "كلمتا المرور غير متطابقتين")
    rl_add("recovery-verify-ip:" + ctx.ip)
    user = one(ctx.conn, "SELECT id FROM users WHERE username=?", (username,))
    record = (one(ctx.conn, "SELECT code_hash,expires_at,attempts FROM password_recovery_codes WHERE user_id=?",
                  (user["id"],)) if user else None)
    if not record or record["expires_at"] <= now() or record["attempts"] >= 5:
        raise ApiError(400, "رمز الاستعادة غير صالح أو منتهي الصلاحية")
    ctx.conn.execute("UPDATE password_recovery_codes SET attempts=attempts+1 WHERE user_id=?",
                     (user["id"],))
    ctx.conn.commit()
    valid_code = bool(re.fullmatch(r"[0-9]{8}", code))
    if not valid_code or not hmac.compare_digest(record["code_hash"], recovery_hash(code)):
        raise ApiError(400, "رمز الاستعادة غير صالح أو منتهي الصلاحية")
    ctx.conn.execute("UPDATE users SET password_hash=?,password_reset_required=0 WHERE id=?",
                     (hash_pw(password), user["id"]))
    ctx.conn.execute("DELETE FROM password_recovery_codes WHERE user_id=?", (user["id"],))
    ctx.conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    return {"ok": True}


@route("POST", r"/api/signup")
def api_signup(ctx):
    b, c = ctx.body, ctx.conn
    if rl_count("signup:" + ctx.ip, 3600) >= 10:
        raise ApiError(429, "محاولات تسجيل كثيرة، حاول لاحقًا")
    name = text(b, "name", "الاسم الكامل", 2, 80)
    username = norm_username(b.get("username"))
    if not USERNAME_RE.match(username):
        raise ApiError(400, "معرّف تيليجرام: ٣ إلى ٣٢ حرفًا إنجليزيًا أو أرقامًا أو _ فقط")
    gender = b.get("gender")
    if gender not in ("male", "female"):
        raise ApiError(400, "اختر النوع")
    check_password_rules(b.get("password"))
    if b.get("password") != b.get("password2"):
        raise ApiError(400, "كلمتا المرور غير متطابقتين")
    if b.get("terms") is not True:
        raise ApiError(400, "يلزم الموافقة على شروط الاستخدام وسياسة الخصوصية")
    try:
        cur = c.execute("INSERT INTO users (username,name,gender,password_hash,role,created_at) VALUES (?,?,?,?,?,?)",
                        (username, name, gender, hash_pw(b["password"]), "student", now()))
    except DB_INTEGRITY_ERRORS:
        raise ApiError(409, "معرّف تيليجرام هذا مسجّل من قبل")
    uid = cur.lastrowid
    rl_add("signup:" + ctx.ip)
    set_cookie(ctx, create_session(c, uid), SESSION_DAYS * 86400)
    return me_payload(c, one(c, "SELECT * FROM users WHERE id=?", (uid,)))


@route("POST", r"/api/login")
def api_login(ctx):
    b, c = ctx.body, ctx.conn
    username = norm_username(b.get("username"))
    pw = b.get("password") if isinstance(b.get("password"), str) else ""
    if rl_count("login-ip:" + ctx.ip, 600) >= 30 or rl_count("login-user:" + username, 600) >= 8:
        raise ApiError(429, "محاولات دخول كثيرة، انتظر بضع دقائق ثم أعد المحاولة")
    u = one(c, "SELECT * FROM users WHERE username=?", (username,))
    ok = verify_pw(pw, u["password_hash"] if u else DUMMY_HASH)
    if not (u and ok):
        rl_add("login-ip:" + ctx.ip)
        rl_add("login-user:" + username)
        raise ApiError(401, "معرّف تيليجرام أو كلمة المرور غير صحيحة")
    set_cookie(ctx, create_session(c, u["id"]), SESSION_DAYS * 86400)
    return me_payload(c, u)


@route("POST", r"/api/logout")
def api_logout(ctx):
    sid = ctx.query.get("_sid")
    if sid:
        ctx.conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(sid.encode()).hexdigest(),))
    set_cookie(ctx, "", 0)
    return {"ok": True}


@route("POST", r"/api/contact")
def api_contact(ctx):
    b = ctx.body
    if rl_count("contact:" + ctx.ip, 3600) >= 5:
        raise ApiError(429, "أرسلت رسائل كثيرة، حاول بعد قليل")
    name = text(b, "name", "الاسم", 2, 80)
    tg = text(b, "telegram", "معرّف التيليجرام", 0, 64, required=False)
    body = text(b, "message", "الرسالة", 5, 3000)
    ctx.conn.execute("INSERT INTO messages (name,telegram,body,created_at,user_id) VALUES (?,?,?,?,?)",
                     (name, tg, body, now(), ctx.user["id"] if ctx.user else None))
    rl_add("contact:" + ctx.ip)
    return {"ok": True}


@route("GET", r"/api/my/contact-messages", "user")
def api_my_contact_messages(ctx):
    return {"messages": rows(ctx.conn, "SELECT id,body,reply_text,created_at,replied_at FROM messages "
                                       "WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                             (ctx.user["id"],))}


# ---- الطالب ----
@route("PUT", r"/api/me", "user")
def api_me_update(ctx):
    b = ctx.body
    name = text(b, "name", "الاسم", 2, 80)
    city = text(b, "city", "المدينة", 0, 80, False)
    bio = text(b, "bio", "النبذة", 0, 300, False)
    if ctx.user["role"] == "supervisor" and not ctx.user["gender"]:
        gender = b.get("gender")
        if gender not in ("male", "female"):
            raise ApiError(400, "اختر النوع لتفعيل التواصل ومتابعة الطلاب")
        ctx.conn.execute("UPDATE users SET name=?,city=?,bio=?,gender=? WHERE id=?",
                         (name, city, bio, gender, ctx.user["id"]))
    else:
        if ctx.user["role"] == "supervisor" and "gender" in b and b["gender"] != ctx.user["gender"]:
            raise ApiError(400, "لا يمكن تغيير النوع بعد تفعيله")
        ctx.conn.execute("UPDATE users SET name=?, city=?, bio=? WHERE id=?",
                         (name, city, bio, ctx.user["id"]))
    return me_payload(ctx.conn, one(ctx.conn, "SELECT * FROM users WHERE id=?", (ctx.user["id"],)))


def remove_avatar_file(path):
    match = re.fullmatch(r"/uploads/([a-f0-9]{24})\.(?:jpg|png|webp)", path or "")
    if match:
        target = UPLOADS / (match.group(1) + Path(path).suffix)
        if target.is_file():
            target.unlink()


@route("POST", r"/api/me/avatar", "user", raw=True)
def api_me_avatar_upload(ctx):
    data = ctx.raw
    if not data or len(data) > MAX_UPLOAD:
        raise ApiError(400, "حجم الصورة غير مقبول (الحد الأقصى ٣ ميجابايت)")
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise ApiError(400, "الصورة يجب أن تكون JPG أو PNG أو WEBP")
    name = "%s.%s" % (secrets.token_hex(12), ext)
    path = "/uploads/" + name
    (UPLOADS / name).write_bytes(data)
    previous = ctx.user.get("avatar_path", "")
    ctx.conn.execute("UPDATE users SET avatar_path=? WHERE id=?", (path, ctx.user["id"]))
    remove_avatar_file(previous)
    user = one(ctx.conn, "SELECT * FROM users WHERE id=?", (ctx.user["id"],))
    return me_payload(ctx.conn, user)


@route("DELETE", r"/api/me/avatar", "user")
def api_me_avatar_delete(ctx):
    previous = ctx.user.get("avatar_path", "")
    ctx.conn.execute("UPDATE users SET avatar_path='' WHERE id=?", (ctx.user["id"],))
    remove_avatar_file(previous)
    user = one(ctx.conn, "SELECT * FROM users WHERE id=?", (ctx.user["id"],))
    return me_payload(ctx.conn, user)


@route("PUT", r"/api/me/password", "user")
def api_me_password(ctx):
    b = ctx.body
    if not verify_pw(b.get("old") or "", ctx.user["password_hash"]):
        raise ApiError(400, "كلمة المرور الحالية غير صحيحة")
    if "new2" in b and b.get("new") != b.get("new2"):
        raise ApiError(400, "كلمتا المرور الجديدتان غير متطابقتين")
    check_password_rules(b.get("new"))
    if ctx.user["password_reset_required"] and len(b["new"]) < 12:
        raise ApiError(400, "كلمة المرور الجديدة يجب ألا تقل عن ١٢ حرفًا")
    if ctx.user["password_reset_required"] and verify_pw(b["new"], ctx.user["password_hash"]):
        raise ApiError(400, "اختر كلمة مرور مختلفة عن كلمة المرور المؤقتة")
    ctx.conn.execute("UPDATE users SET password_hash=?,password_reset_required=0 WHERE id=?",
                     (hash_pw(b["new"]), ctx.user["id"]))
    ctx.conn.execute("DELETE FROM password_recovery_codes WHERE user_id=?", (ctx.user["id"],))
    return {"ok": True}


@route("POST", r"/api/enroll", "user")
def api_enroll(ctx):
    keys = valid_program_keys(ctx.conn, [ctx.body.get("program")])
    if not keys:
        raise ApiError(404, "البرنامج غير موجود")
    if ctx.user["role"] != "student":
        raise ApiError(400, "التسجيل في البرامج للطلاب فقط")
    ctx.conn.execute("INSERT OR IGNORE INTO enrollments (user_id,program_key,status,created_at) VALUES (?,?,?,?)",
                     (ctx.user["id"], keys[0], "pending", now()))
    return me_payload(ctx.conn, ctx.user)


@route("DELETE", r"/api/enroll/([a-z0-9_]+)", "user")
def api_unenroll(ctx, key):
    ctx.conn.execute("DELETE FROM enrollments WHERE user_id=? AND program_key=? AND status='pending'", (ctx.user["id"], key))
    return me_payload(ctx.conn, ctx.user)


@route("POST", r"/api/saved/(\d+)", "user")
def api_save(ctx, vid):
    if not one(ctx.conn, "SELECT 1 AS x FROM videos WHERE id=? AND published=1", (vid,)):
        raise ApiError(404, "الدرس غير موجود")
    ctx.conn.execute("INSERT OR IGNORE INTO saved_videos VALUES (?,?,?)", (ctx.user["id"], int(vid), now()))
    return me_payload(ctx.conn, ctx.user)


@route("DELETE", r"/api/saved/(\d+)", "user")
def api_unsave(ctx, vid):
    ctx.conn.execute("DELETE FROM saved_videos WHERE user_id=? AND video_id=?", (ctx.user["id"], int(vid)))
    return me_payload(ctx.conn, ctx.user)


def chat_gender(ctx, value):
    gender = value if value in ("male", "female") else ""
    if ctx.user["role"] in ("student", "supervisor"):
        if not ctx.user["gender"]:
            raise ApiError(403, "حدّد النوع في ملفك قبل استخدام التواصل")
        if gender and gender != ctx.user["gender"]:
            raise ApiError(403, "هذه المساحة غير متاحة لحسابك")
        return ctx.user["gender"]
    if not gender:
        raise ApiError(400, "اختر مساحة التواصل")
    return gender


def chat_contact(ctx, other_id):
    other = one(ctx.conn, "SELECT id,name,gender,role,avatar_path FROM users WHERE id=?", (other_id,))
    if not other or other["id"] == ctx.user["id"]:
        raise ApiError(404, "الحساب غير موجود")
    user = ctx.user
    if user["role"] not in ("student", "supervisor") or user["gender"] not in ("male", "female"):
        raise ApiError(403, "التواصل متاح بين المشرف والطالب بعد تحديد النوع")
    expected_role = "supervisor" if user["role"] == "student" else "student"
    if other["role"] != expected_role or other["gender"] != user["gender"]:
        raise ApiError(403, "لا يمكن فتح محادثة لهذا الحساب")
    return other


@route("GET", r"/api/chat/contacts", "user")
def api_chat_contacts(ctx):
    if ctx.user["role"] not in ("student", "supervisor") or ctx.user["gender"] not in ("male", "female"):
        raise ApiError(403, "حدّث النوع في ملفك قبل استخدام التواصل")
    other_role = "supervisor" if ctx.user["role"] == "student" else "student"
    search = ctx.query.get("q", "").strip()[:80]
    query = "SELECT u.id,u.name,u.username,u.gender,u.role,u.avatar_path," \
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.sender_id=u.id AND m.recipient_id=? " \
            "AND m.read_at IS NULL) AS unread_count " \
            "FROM users u WHERE u.role=? AND u.gender=? AND u.id<>?"
    args = [ctx.user["id"], other_role, ctx.user["gender"], ctx.user["id"]]
    if search:
        query += " AND (u.name LIKE ? OR u.username LIKE ?)"
        args.extend(["%" + search + "%", "%" + search + "%"])
    query += " ORDER BY CASE WHEN u.role='supervisor' THEN 0 ELSE 1 END,unread_count DESC,u.name LIMIT 100"
    contacts = rows(ctx.conn, query, tuple(args))
    return {"contacts": contacts}


@route("GET", r"/api/chat/notifications", "user")
def api_chat_notifications(ctx):
    user = ctx.user
    if user["role"] not in ("student", "supervisor"):
        raise ApiError(403, "الإشعارات متاحة لحسابات الطلاب والمشرفين فقط")
    try:
        after = max(0, int(ctx.query.get("after", "0")))
    except ValueError:
        raise ApiError(400, "معرّف الإشعار غير صالح")
    unread_count = one(ctx.conn, "SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND read_at IS NULL",
                       (user["id"],))["n"]
    latest = one(ctx.conn, "SELECT COALESCE(MAX(id),0) AS id FROM notifications WHERE user_id=?",
                 (user["id"],))["id"]
    new_items = rows(ctx.conn, "SELECT id,kind,title,body,target,related_user_id,created_at,read_at "
                               "FROM notifications WHERE user_id=? AND id>? ORDER BY id LIMIT 100",
                     (user["id"], after))
    items = rows(ctx.conn, "SELECT id,kind,title,body,target,related_user_id,created_at,read_at "
                           "FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",
                 (user["id"],))
    return {"unread_count": unread_count, "latest_id": latest,
            "new_items": new_items, "items": items}


@route("POST", r"/api/notifications/([0-9]+)/read", "user")
def api_notification_read(ctx, notification_id):
    cursor = ctx.conn.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL",
                              (now(), int(notification_id), ctx.user["id"]))
    if not cursor.rowcount:
        if not one(ctx.conn, "SELECT 1 AS x FROM notifications WHERE id=? AND user_id=?",
                   (int(notification_id), ctx.user["id"])):
            raise ApiError(404, "الإشعار غير موجود")
    return {"ok": True}


@route("POST", r"/api/notifications/read-all", "user")
def api_notifications_read_all(ctx):
    ctx.conn.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                     (now(), ctx.user["id"]))
    return {"ok": True}


@route("GET", r"/api/chat/messages", "user")
def api_chat_messages(ctx):
    recipient = ctx.query.get("recipient", "")
    if not recipient:
        raise ApiError(400, "اختر محادثة بين مشرف وطالب")
    try:
        other_id = int(recipient)
    except ValueError:
        raise ApiError(400, "المحادثة غير صالحة")
    chat_contact(ctx, other_id)
    ctx.conn.execute("UPDATE chat_messages SET read_at=? WHERE recipient_id=? AND sender_id=? AND read_at IS NULL",
                     (now(), ctx.user["id"], other_id))
    messages = rows(ctx.conn, "SELECT m.id,m.sender_id,m.recipient_id,m.body,m.attachment_path,"
                              "m.attachment_name,m.attachment_type,m.read_at,m.created_at,"
                              "u.name AS sender_name,u.role AS sender_role,u.gender AS sender_gender,"
                              "u.avatar_path AS sender_avatar_path "
                              "FROM chat_messages m JOIN users u ON u.id=m.sender_id "
                              "WHERE (m.sender_id=? AND m.recipient_id=?) OR "
                              "(m.sender_id=? AND m.recipient_id=?) ORDER BY m.id DESC LIMIT 100",
                    (ctx.user["id"], other_id, other_id, ctx.user["id"]))
    messages.reverse()
    return {"messages": messages}


@route("POST", r"/api/chat/messages", "user")
def api_chat_send(ctx):
    if rl_count("chat:" + str(ctx.user["id"]), 60) >= 30:
        raise ApiError(429, "أرسلت رسائل كثيرة، انتظر قليلًا")
    recipient = ctx.body.get("recipient")
    try:
        other_id = int(recipient)
    except (TypeError, ValueError):
        raise ApiError(400, "اختر الطالب أو المشرف الذي تريد مراسلته")
    chat_contact(ctx, other_id)
    body = text(ctx.body, "body", "الرسالة", 0, 2000, required=False)
    attachment_path = ctx.body.get("attachment_path", "")
    attachment_name = text(ctx.body, "attachment_name", "اسم الملف", 0, 180, required=False)
    attachment_type = ctx.body.get("attachment_type", "")
    if not body and not attachment_path:
        raise ApiError(400, "اكتب رسالة أو أرفق ملفًا")
    if attachment_path:
        if not re.fullmatch(r"/uploads/[a-f0-9]{32}\.(?:pdf|jpg|png|webp|mp3|m4a|wav|ogg|webm|mp4|mov)", attachment_path):
            raise ApiError(400, "المرفق غير صالح")
        if not (UPLOADS / Path(attachment_path).name).is_file():
            raise ApiError(400, "ارفع المرفق مرة أخرى قبل إرسال الرسالة")
        allowed_types = {"file", "image", "audio", "video"}
        if attachment_type not in allowed_types or not attachment_name:
            raise ApiError(400, "بيانات المرفق غير صالحة")
    else:
        attachment_path = attachment_name = attachment_type = ""
    ctx.conn.execute("INSERT INTO chat_messages "
                     "(sender_id,recipient_id,gender,body,attachment_path,attachment_name,attachment_type,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (ctx.user["id"], other_id, ctx.user["gender"], body, attachment_path,
                      attachment_name, attachment_type, now()))
    sender_name = ctx.user["name"]
    add_notification(ctx.conn, other_id, "chat", "رسالة جديدة من " + sender_name,
                     body or ("أرسل " + attachment_name), "#chat", ctx.user["id"])
    rl_add("chat:" + str(ctx.user["id"]))
    return {"ok": True}


@route("POST", r"/api/chat/upload", "user", raw=True)
def api_chat_upload(ctx):
    if ctx.user["role"] not in ("student", "supervisor") or ctx.user["gender"] not in ("male", "female"):
        raise ApiError(403, "التواصل متاح بين المشرف والطالب بعد تحديد النوع")
    key = "chat-upload:" + str(ctx.user["id"])
    if rl_count(key, 3600) >= 30:
        raise ApiError(429, "وصلت إلى حد رفع المرفقات لهذا الوقت")
    data = ctx.raw
    if not data or len(data) > MAX_CHAT_UPLOAD:
        raise ApiError(400, "حجم المرفق غير مقبول (الحد الأقصى ٢٥ ميجابايت)")
    media_types = {
        "application/pdf": ("pdf", "file"),
        "image/jpeg": ("jpg", "image"),
        "image/png": ("png", "image"),
        "image/webp": ("webp", "image"),
        "audio/mpeg": ("mp3", "audio"),
        "audio/mp3": ("mp3", "audio"),
        "audio/mp4": ("m4a", "audio"),
        "audio/wav": ("wav", "audio"),
        "audio/x-wav": ("wav", "audio"),
        "audio/ogg": ("ogg", "audio"),
        "audio/webm": ("webm", "audio"),
        "video/mp4": ("mp4", "video"),
        "video/quicktime": ("mov", "video"),
        "video/webm": ("webm", "video"),
    }
    media = media_types.get(ctx.ctype)
    if not media:
        raise ApiError(415, "المرفقات المسموحة: PDF أو صورة أو صوت أو فيديو")
    ext, kind = media
    name = "%s.%s" % (secrets.token_hex(16), ext)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / name).write_bytes(data)
    rl_add(key)
    return {"path": "/uploads/" + name, "kind": kind}


@route("GET", r"/api/lessons/([A-Za-z0-9_-]{6,64})")
def api_lesson_data(ctx, video_id):
    lesson = one(ctx.conn, "SELECT video_id,title FROM lesson_catalog WHERE video_id=?", (video_id,))
    if not lesson:
        raise ApiError(404, "الدرس غير موجود بعد؛ افتح قائمته أولًا")
    result = {
        "lesson": lesson,
        "resources": rows(ctx.conn, "SELECT id,kind,title,body,path FROM lesson_resources "
                                    "WHERE video_id=? ORDER BY kind,id", (video_id,)),
        "progress": None,
    }
    if ctx.user:
        result["progress"] = one(ctx.conn, "SELECT position_seconds,duration_seconds,completed,updated_at "
                                           "FROM lesson_progress WHERE user_id=? AND video_id=?",
                                 (ctx.user["id"], video_id))
    return result


@route("GET", r"/api/lessons/([A-Za-z0-9_-]{6,64})/questions")
def api_lesson_questions(ctx, video_id):
    lesson = one(ctx.conn, "SELECT video_id FROM lesson_catalog WHERE video_id=?", (video_id,))
    if not lesson:
        raise ApiError(404, "الدرس غير موجود بعد؛ افتح قائمته أولًا")
    return {"questions": rows(ctx.conn, "SELECT id,body,answer,created_at,answered_at "
                                       "FROM lesson_questions WHERE video_id=? ORDER BY created_at DESC,id DESC",
                              (video_id,))}


@route("POST", r"/api/lessons/([A-Za-z0-9_-]{6,64})/questions", "user")
def api_lesson_question_create(ctx, video_id):
    lesson = one(ctx.conn, "SELECT l.video_id,l.title,l.playlist_id,"
                           "(SELECT id FROM videos WHERE youtube_id=l.video_id ORDER BY id LIMIT 1) AS content_id "
                           "FROM lesson_catalog l WHERE l.video_id=?", (video_id,))
    if not lesson:
        raise ApiError(404, "الدرس غير موجود")
    key = "lesson-question:%s:%s" % (ctx.user["id"], ctx.ip)
    if rl_count(key, 300) >= 5:
        raise ApiError(429, "أرسلت أسئلة كثيرة مؤخرًا؛ حاول مرة أخرى بعد قليل")
    body = text(ctx.body, "body", "السؤال", 4, 2000)
    created_at = now()
    cursor = ctx.conn.execute("INSERT INTO lesson_questions(user_id,video_id,body,created_at) "
                              "VALUES (?,?,?,?)", (ctx.user["id"], video_id, body, created_at))
    content_id = lesson["playlist_id"] or lesson["content_id"]
    target = "#watch/" + str(content_id) + "/" + video_id if content_id else "#lessons"
    supervisors = rows(ctx.conn, "SELECT id FROM users WHERE role='supervisor'")
    for supervisor in supervisors:
        add_notification(ctx.conn, supervisor["id"], "question",
                         "سؤال جديد من " + ctx.user["name"], body, target,
                         ctx.user["id"], created_at)
    rl_add(key)
    return {"id": cursor.lastrowid, "created_at": created_at}


@route("POST", r"/api/admin/lesson-questions/([0-9]+)/answer", "staff")
def api_lesson_question_answer(ctx, question_id):
    question = one(ctx.conn, "SELECT id,user_id,video_id FROM lesson_questions WHERE id=?", (int(question_id),))
    if not question:
        raise ApiError(404, "السؤال غير موجود")
    answer = text(ctx.body, "answer", "الإجابة", 2, 2000)
    answered_at = now()
    ctx.conn.execute("UPDATE lesson_questions SET answer=?,answered_by=?,answered_at=? WHERE id=?",
                     (answer, ctx.user["id"], answered_at, question["id"]))
    lesson = one(ctx.conn, "SELECT l.playlist_id,"
                           "(SELECT id FROM videos WHERE youtube_id=l.video_id ORDER BY id LIMIT 1) AS content_id "
                           "FROM lesson_catalog l WHERE l.video_id=?", (question["video_id"],))
    content_id = (lesson["playlist_id"] or lesson["content_id"]) if lesson else None
    target = "#watch/" + str(content_id) + "/" + question["video_id"] if content_id else "#lessons"
    add_notification(ctx.conn, question["user_id"], "answer",
                     "أجاب المشرف عن سؤالك", answer, target, ctx.user["id"], answered_at)
    return {"ok": True, "answered_at": answered_at}


@route("POST", r"/api/lessons/([A-Za-z0-9_-]{6,64})/progress", "user")
def api_lesson_progress(ctx, video_id):
    if ctx.user["role"] != "student":
        raise ApiError(403, "متابعة الدروس متاحة لحسابات الطلاب")
    lesson = one(ctx.conn, "SELECT title FROM lesson_catalog WHERE video_id=?", (video_id,))
    if not lesson:
        raise ApiError(404, "الدرس غير موجود")
    try:
        duration = int(ctx.body.get("duration_seconds", 0))
        position = int(ctx.body.get("position_seconds", 0))
    except (TypeError, ValueError):
        raise ApiError(400, "بيانات التقدم غير صالحة")
    if duration < 1 or duration > 86400 or position < 0:
        raise ApiError(400, "مدة الدرس أو موضع التشغيل غير صالح")
    position = min(position, duration)
    completed = bool(ctx.body.get("completed")) and position >= duration * 0.95
    t = now()
    ctx.conn.execute("INSERT INTO lesson_catalog (video_id,title,updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at",
                     (video_id, lesson["title"], t))
    ctx.conn.execute("INSERT INTO lesson_progress "
                     "(user_id,video_id,position_seconds,duration_seconds,completed,updated_at) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(user_id,video_id) DO UPDATE SET position_seconds=excluded.position_seconds,"
                     "duration_seconds=excluded.duration_seconds,"
                     "completed=MAX(lesson_progress.completed,excluded.completed),updated_at=excluded.updated_at",
                     (ctx.user["id"], video_id, position, duration, int(completed), t))
    return {"ok": True, "completed": completed, "position_seconds": position}


@route("GET", r"/api/live-rooms", "user")
def api_live_rooms(ctx):
    if ctx.user["role"] in ("student", "supervisor"):
        if ctx.user["gender"] not in ("male", "female"):
            raise ApiError(403, "حدّث النوع في ملفك قبل مشاهدة الغرف")
        live = rows(ctx.conn, "SELECT id,title,description,event_url,audience,status,starts_at "
                              "FROM live_rooms WHERE published=1 AND status<>'ended' AND audience IN ('both',?) "
                              "ORDER BY CASE status WHEN 'live' THEN 0 WHEN 'scheduled' THEN 1 ELSE 2 END,"
                              "starts_at DESC LIMIT 100", (ctx.user["gender"],))
    else:
        live = rows(ctx.conn, "SELECT id,title,description,event_url,audience,status,starts_at "
                              "FROM live_rooms WHERE published=1 ORDER BY starts_at DESC LIMIT 100")
    return {"rooms": live}


def lesson_resource_fields(ctx):
    b = ctx.body
    video_id = lesson_video_id(b.get("video_id", ""))
    kind = b.get("kind")
    if kind not in ("file", "note", "audio"):
        raise ApiError(400, "اختر نوع المادة")
    title = text(b, "title", "عنوان المادة", 2, 200)
    body = text(b, "body", "نص الملاحظة", 0, 5000, required=False) if kind == "note" else ""
    path = clean_path(b.get("path"), "رابط المادة")
    if kind != "note" and not path:
        raise ApiError(400, "ارفع الملف أو أدخل رابطه")
    if kind == "audio" and not (path.startswith("/uploads/") or
                               re.match(r"^https://", path, re.I)):
        raise ApiError(400, "رابط الصوت غير صالح")
    return video_id, title, kind, body, path


@route("GET", r"/api/admin/lesson-resources", "staff")
def adm_lesson_resources(ctx):
    return {"resources": rows(ctx.conn, "SELECT r.*,c.title AS lesson_title,p.title AS audio_playlist_title,"
                                        "p.description AS audio_playlist_description "
                                        "FROM lesson_resources r JOIN lesson_catalog c ON c.video_id=r.video_id "
                                        "LEFT JOIN lesson_audio_playlists p ON p.id=r.audio_playlist_id "
                                        "ORDER BY r.created_at DESC,r.id DESC LIMIT 500")}


def audio_playlist_id(ctx, kind):
    if kind != "audio" or not ctx.body.get("audio_playlist"):
        return None
    title = text(ctx.body, "playlist_title", "اسم قائمة الصوتيات", 2, 200)
    description = text(ctx.body, "playlist_description", "وصف قائمة الصوتيات", 0, 1000, required=False)
    existing = one(ctx.conn, "SELECT id FROM lesson_audio_playlists WHERE title=?", (title,))
    if existing:
        ctx.conn.execute("UPDATE lesson_audio_playlists SET description=? WHERE id=?",
                         (description, existing["id"]))
        return existing["id"]
    cur = ctx.conn.execute("INSERT INTO lesson_audio_playlists (title,description,created_by,created_at) "
                           "VALUES (?,?,?,?)", (title, description, ctx.user["id"], now()))
    return cur.lastrowid


@route("POST", r"/api/admin/lesson-resources", "staff")
def adm_lesson_resource_add(ctx):
    video_id, title, kind, body, path = lesson_resource_fields(ctx)
    playlist_id = audio_playlist_id(ctx, kind)
    t = now()
    ctx.conn.execute("INSERT INTO lesson_catalog (video_id,title,updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(video_id) DO UPDATE SET updated_at=excluded.updated_at",
                     (video_id, text(ctx.body, "lesson_title", "عنوان الدرس", 2, 200), t))
    cur = ctx.conn.execute("INSERT INTO lesson_resources "
                           "(video_id,kind,title,body,path,audio_playlist_id,created_by,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (video_id, kind, title, body, path, playlist_id, ctx.user["id"], t))
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/lesson-resources/(\d+)", "staff")
def adm_lesson_resource_edit(ctx, resource_id):
    video_id, title, kind, body, path = lesson_resource_fields(ctx)
    playlist_id = audio_playlist_id(ctx, kind)
    ctx.conn.execute("INSERT INTO lesson_catalog (video_id,title,updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(video_id) DO UPDATE SET updated_at=excluded.updated_at",
                     (video_id, text(ctx.body, "lesson_title", "عنوان الدرس", 2, 200), now()))
    cur = ctx.conn.execute("UPDATE lesson_resources SET video_id=?,kind=?,title=?,body=?,path=?,"
                           "audio_playlist_id=? WHERE id=?",
                           (video_id, kind, title, body, path, playlist_id, resource_id))
    if not cur.rowcount:
        raise ApiError(404, "المادة غير موجودة")
    return {"ok": True}


@route("DELETE", r"/api/admin/lesson-resources/(\d+)", "staff")
def adm_lesson_resource_delete(ctx, resource_id):
    ctx.conn.execute("DELETE FROM lesson_resources WHERE id=?", (resource_id,))
    return {"ok": True}


@route("GET", r"/api/admin/lesson-progress", "staff")
def adm_lesson_progress(ctx):
    if ctx.user["role"] == "supervisor" and ctx.user["gender"] not in ("male", "female"):
        raise ApiError(403, "حدّد النوع في ملفك قبل متابعة تقدم الطلاب")
    progress_join = "LEFT JOIN lesson_progress p ON p.video_id=c.video_id"
    user_join = "LEFT JOIN users u ON u.id=p.user_id"
    args = ()
    if ctx.user["role"] == "supervisor":
        progress_join += " AND p.user_id IN (SELECT id FROM users WHERE gender=?)"
        args = (ctx.user["gender"],)
    return {"progress": rows(ctx.conn, "SELECT c.video_id,c.title AS lesson_title,u.id AS user_id,"
                                       "u.name AS student_name,u.gender,p.position_seconds,p.duration_seconds,"
                                       "p.completed,p.updated_at FROM lesson_catalog c "
                                       + progress_join + " " + user_join +
                                       " ORDER BY c.title,u.name LIMIT 5000", args)}


def live_room_fields(body):
    title = text(body, "title", "عنوان الغرفة", 2, 200)
    description = text(body, "description", "الوصف", 0, 1000, required=False)
    event_url = streamyard_url(body.get("event_url"))
    audience = body.get("audience", "both")
    status = body.get("status", "scheduled")
    if audience not in ("both", "male", "female"):
        raise ApiError(400, "اختر الفئة المستهدفة")
    if status not in ("scheduled", "live", "ended"):
        raise ApiError(400, "حالة البث غير صالحة")
    starts_at = body.get("starts_at")
    if starts_at in (None, ""):
        starts_at = None
    else:
        try:
            starts_at = int(starts_at)
        except (TypeError, ValueError):
            raise ApiError(400, "موعد البث غير صالح")
        if starts_at < 0:
            raise ApiError(400, "موعد البث غير صالح")
    return title, description, event_url, audience, status, starts_at, int(bool(body.get("published", True)))


@route("GET", r"/api/admin/live-rooms", "staff")
def adm_live_rooms(ctx):
    return {"rooms": rows(ctx.conn, "SELECT * FROM live_rooms ORDER BY starts_at DESC,id DESC")}


@route("POST", r"/api/admin/live-rooms", "staff")
def adm_live_room_add(ctx):
    fields = live_room_fields(ctx.body)
    cur = ctx.conn.execute("INSERT INTO live_rooms "
                           "(title,description,event_url,audience,status,starts_at,published,created_by,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?)", fields + (ctx.user["id"], now()))
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/live-rooms/(\d+)", "staff")
def adm_live_room_edit(ctx, room_id):
    fields = live_room_fields(ctx.body)
    cur = ctx.conn.execute("UPDATE live_rooms SET title=?,description=?,event_url=?,audience=?,status=?,"
                           "starts_at=?,published=? WHERE id=?", fields + (room_id,))
    if not cur.rowcount:
        raise ApiError(404, "غرفة البث غير موجودة")
    return {"ok": True}


@route("DELETE", r"/api/admin/live-rooms/(\d+)", "staff")
def adm_live_room_delete(ctx, room_id):
    ctx.conn.execute("DELETE FROM live_rooms WHERE id=?", (room_id,))
    return {"ok": True}


BENEFIT_EXTENSIONS = {
    "video": {"mp4", "m4v", "mov", "webm", "mkv", "avi"},
    "audio": {"mp3", "m4a", "aac", "wav", "ogg", "flac", "webm"},
}


def benefit_upload_type(kind, extension, header):
    if kind == "video":
        if extension in ("mp4", "m4v", "mov") and len(header) >= 8 and header[4:8] == b"ftyp":
            return True
        if extension == "avi" and header.startswith(b"RIFF") and header[8:12] == b"AVI ":
            return True
        if extension in ("webm", "mkv") and header.startswith(b"\x1a\x45\xdf\xa3"):
            return True
        return False
    if extension == "mp3":
        return header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
    if extension == "m4a" and len(header) >= 8 and header[4:8] == b"ftyp":
        return True
    if extension == "aac":
        return len(header) >= 2 and header[0] == 0xff and header[1] & 0xf6 == 0xf0
    if extension == "wav":
        return header.startswith(b"RIFF") and header[8:12] == b"WAVE"
    if extension == "ogg":
        return header.startswith(b"OggS")
    if extension == "flac":
        return header.startswith(b"fLaC")
    if extension == "webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    return False


def get_benefit_upload(ctx, token):
    upload = one(ctx.conn, "SELECT * FROM benefit_uploads WHERE token=? AND user_id=?",
                 (token, ctx.user["id"]))
    if not upload:
        raise ApiError(404, "طلب الرفع غير موجود أو انتهت صلاحيته")
    return upload


def benefit_media_path(path):
    match = re.fullmatch(r"/uploads/([a-f0-9]{24})\.(mp4|m4v|mov|webm|mkv|avi|mp3|m4a|aac|wav|ogg|flac)", path or "")
    return UPLOADS / (match.group(1) + "." + match.group(2)) if match else None


@route("GET", r"/api/admin/benefits", "staff")
def adm_benefits(ctx):
    return {"benefits": rows(ctx.conn, "SELECT * FROM benefits WHERE kind='video' "
                                       "ORDER BY sort,id DESC LIMIT 500")}


@route("POST", r"/api/admin/benefits/youtube", "staff")
def adm_benefit_youtube_add(ctx):
    kind, youtube_id = parse_youtube(text(ctx.body, "youtube_url", "رابط فيديو يوتيوب", 1, 2000))
    if kind != "video":
        raise ApiError(400, "أدخل رابط فيديو أو Short، وليس قائمة تشغيل")
    title = text(ctx.body, "title", "العنوان", 1, 200)
    description = text(ctx.body, "description", "الوصف", 0, 1000, required=False)
    if one(ctx.conn, "SELECT 1 AS x FROM benefits WHERE youtube_id=? AND kind='video'", (youtube_id,)):
        raise ApiError(409, "هذا الفيديو مضاف بالفعل إلى الفوائد")
    sort = integer(ctx.body, "sort")
    published = int(bool(ctx.body.get("published", True)))
    cursor = ctx.conn.execute(
        "INSERT INTO benefits (title,description,kind,path,youtube_id,original_name,size_bytes,published,sort,"
        "created_by,created_at) VALUES (?,?, 'video','',?,'',0,?,?,?,?)",
        (title, description, youtube_id, published, sort, ctx.user["id"], now()))
    return {"id": cursor.lastrowid, "youtube_id": youtube_id}


@route("POST", r"/api/admin/benefits/uploads", "staff")
def adm_benefit_upload_start(ctx):
    body = ctx.body
    kind = body.get("kind")
    if kind != "video":
        raise ApiError(400, "الفوائد مخصصة لمقاطع الفيديو")
    original_name = text(body, "file_name", "اسم الملف", 1, 255)
    extension = Path(original_name.replace("\\", "/")).suffix.lower().lstrip(".")
    if extension not in BENEFIT_EXTENSIONS[kind]:
        allowed = "، ".join(sorted(BENEFIT_EXTENSIONS[kind]))
        raise ApiError(400, "صيغ الملفات المسموحة: " + allowed)
    try:
        total_size = int(body.get("size"))
    except (TypeError, ValueError):
        raise ApiError(400, "حجم الملف غير صالح")
    if total_size < 1 or total_size > MAX_BENEFIT_UPLOAD:
        raise ApiError(400, "الحد الأقصى لحجم الملف ٨ جيجابايت")
    title = text(body, "title", "العنوان", 2, 200)
    description = text(body, "description", "الوصف", 0, 1000, required=False)
    token = secrets.token_hex(16)
    ctx.conn.execute("INSERT INTO benefit_uploads "
                     "(token,user_id,title,description,kind,original_name,extension,total_size,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (token, ctx.user["id"], title, description, kind,
                      Path(original_name.replace("\\", "/")).name, extension, total_size, now()))
    with (UPLOAD_TMP / (token + ".part")).open("xb"):
        pass
    return {"upload_id": token, "chunk_size": BENEFIT_UPLOAD_CHUNK}


@route("PUT", r"/api/admin/benefits/uploads/([a-f0-9]{32})/([0-9]{1,4})", "staff", raw=True)
def adm_benefit_upload_chunk(ctx, token, chunk_number):
    upload = get_benefit_upload(ctx, token)
    index = int(chunk_number)
    chunk_count = (upload["total_size"] + BENEFIT_UPLOAD_CHUNK - 1) // BENEFIT_UPLOAD_CHUNK
    if index < 0 or index >= chunk_count:
        raise ApiError(400, "رقم جزء الرفع غير صالح")
    expected_size = min(BENEFIT_UPLOAD_CHUNK, upload["total_size"] - index * BENEFIT_UPLOAD_CHUNK)
    if len(ctx.raw) != expected_size:
        raise ApiError(400, "حجم جزء الرفع غير مطابق")
    if one(ctx.conn, "SELECT 1 AS x FROM benefit_upload_chunks WHERE token=? AND chunk_index=?",
           (token, index)):
        raise ApiError(409, "تم استلام هذا الجزء من قبل")
    staged_path = UPLOAD_TMP / (token + ".part")
    with staged_path.open("r+b") as staged_file:
        staged_file.seek(index * BENEFIT_UPLOAD_CHUNK)
        staged_file.write(ctx.raw)
        staged_file.flush()
    ctx.conn.execute("INSERT INTO benefit_upload_chunks (token,chunk_index,size_bytes) VALUES (?,?,?)",
                     (token, index, len(ctx.raw)))
    return {"received": index, "chunk_count": chunk_count}


@route("POST", r"/api/admin/benefits/uploads/([a-f0-9]{32})/complete", "staff")
def adm_benefit_upload_complete(ctx, token):
    upload = get_benefit_upload(ctx, token)
    chunk_count = (upload["total_size"] + BENEFIT_UPLOAD_CHUNK - 1) // BENEFIT_UPLOAD_CHUNK
    received = rows(ctx.conn, "SELECT chunk_index,size_bytes FROM benefit_upload_chunks "
                              "WHERE token=? ORDER BY chunk_index", (token,))
    expected_sizes = [min(BENEFIT_UPLOAD_CHUNK, upload["total_size"] - index * BENEFIT_UPLOAD_CHUNK)
                      for index in range(chunk_count)]
    if [chunk["chunk_index"] for chunk in received] != list(range(chunk_count)) or \
            [chunk["size_bytes"] for chunk in received] != expected_sizes:
        raise ApiError(400, "لم يكتمل رفع الملف")
    staged_path = UPLOAD_TMP / (token + ".part")
    if not staged_path.is_file() or staged_path.stat().st_size != upload["total_size"]:
        raise ApiError(400, "حجم الملف المرفوع غير مكتمل")
    with staged_path.open("rb") as uploaded_file:
        header = uploaded_file.read(32)
    if not benefit_upload_type(upload["kind"], upload["extension"], header):
        raise ApiError(400, "محتوى الملف لا يطابق صيغة الفيديو أو الصوت المختارة")
    name = secrets.token_hex(12) + "." + upload["extension"]
    path = "/uploads/" + name
    final_path = UPLOADS / name
    try:
        os.replace(str(staged_path), str(final_path))
    except OSError:
        raise
    try:
        cur = ctx.conn.execute("INSERT INTO benefits "
                               "(title,description,kind,path,original_name,size_bytes,created_by,created_at) "
                               "VALUES (?,?,?,?,?,?,?,?)",
                               (upload["title"], upload["description"], upload["kind"], path,
                                upload["original_name"], upload["total_size"], ctx.user["id"], now()))
    except DB_ERRORS:
        if final_path.exists():
            final_path.unlink()
        raise
    ctx.conn.execute("DELETE FROM benefit_uploads WHERE token=?", (token,))
    return {"id": cur.lastrowid}


@route("DELETE", r"/api/admin/benefits/uploads/([a-f0-9]{32})", "staff")
def adm_benefit_upload_cancel(ctx, token):
    get_benefit_upload(ctx, token)
    ctx.conn.execute("DELETE FROM benefit_uploads WHERE token=?", (token,))
    staged_path = UPLOAD_TMP / (token + ".part")
    if staged_path.exists():
        staged_path.unlink()
    return {"ok": True}


@route("PUT", r"/api/admin/benefits/(\d+)", "staff")
def adm_benefit_edit(ctx, benefit_id):
    title = text(ctx.body, "title", "العنوان", 1, 200)
    description = text(ctx.body, "description", "الوصف", 0, 1000, required=False)
    published = int(bool(ctx.body.get("published", True)))
    sort = ctx.body.get("sort", 0)
    try:
        sort = int(sort)
    except (TypeError, ValueError):
        raise ApiError(400, "ترتيب العرض غير صالح")
    youtube_id = None
    if "youtube_url" in ctx.body:
        raw_url = text(ctx.body, "youtube_url", "رابط فيديو يوتيوب", 0, 2000, required=False)
        if raw_url:
            media_type, youtube_id = parse_youtube(raw_url)
            if media_type != "video":
                raise ApiError(400, "أدخل رابط فيديو أو Short، وليس قائمة تشغيل")
        else:
            youtube_id = ""
    if youtube_id is None:
        cur = ctx.conn.execute("UPDATE benefits SET title=?,description=?,published=?,sort=? "
                               "WHERE id=? AND kind='video'",
                               (title, description, published, sort, benefit_id))
    else:
        duplicate = one(ctx.conn, "SELECT id FROM benefits WHERE youtube_id=? AND id<>? AND kind='video'",
                        (youtube_id, benefit_id)) if youtube_id else None
        if duplicate:
            raise ApiError(409, "هذا الفيديو مضاف بالفعل إلى الفوائد")
        cur = ctx.conn.execute("UPDATE benefits SET title=?,description=?,published=?,sort=?,youtube_id=? "
                               "WHERE id=? AND kind='video'",
                               (title, description, published, sort, youtube_id, benefit_id))
    if not cur.rowcount:
        raise ApiError(404, "المادة غير موجودة")
    return {"ok": True}


@route("DELETE", r"/api/admin/benefits/(\d+)", "staff")
def adm_benefit_delete(ctx, benefit_id):
    benefit = one(ctx.conn, "SELECT path FROM benefits WHERE id=?", (benefit_id,))
    if not benefit:
        raise ApiError(404, "المادة غير موجودة")
    ctx.conn.execute("DELETE FROM benefits WHERE id=?", (benefit_id,))
    target = benefit_media_path(benefit["path"])
    if target and target.is_file():
        target.unlink()
    return {"ok": True}


@route("POST", r"/api/admin/media/upload", "staff", raw=True)
def adm_media_upload(ctx):
    data = ctx.raw
    if not data or len(data) > MAX_MEDIA_UPLOAD:
        raise ApiError(400, "حجم الملف غير مقبول (الحد الأقصى ٢٥ ميجابايت)")
    requested_extension = Path(ctx.query.get("filename", "").replace("\\", "/")).suffix.lower().lstrip(".")
    if data.startswith(b"%PDF-"):
        ext = "pdf"
    elif len(data) > 2 and data[0] == 0xff and data[1] & 0xf6 == 0xf0:
        ext = "aac"
    elif data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
        ext = "mp3"
    elif data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        ext = "wav"
    elif data.startswith(b"OggS"):
        ext = "ogg"
    elif data.startswith(b"\x1a\x45\xdf\xa3"):
        ext = requested_extension if requested_extension in ("webm", "mkv") else "webm"
    elif data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        ext = "avi"
    elif len(data) > 12 and data[4:8] == b"ftyp":
        ext = requested_extension if requested_extension in ("mp4", "m4v", "mov", "m4a") else "m4a"
    elif data.startswith(b"fLaC"):
        ext = "flac"
    else:
        raise ApiError(400, "صيغة الملف غير مدعومة")
    allowed_extensions = {"pdf", "mp3", "m4a", "wav", "ogg", "webm", "mkv",
                          "mp4", "m4v", "mov", "avi", "aac", "flac"}
    if ext not in allowed_extensions or (requested_extension and requested_extension != ext and
                                         not (requested_extension in ("mp4", "m4v", "mov", "m4a") and ext in
                                              ("mp4", "m4v", "mov", "m4a"))):
        raise ApiError(400, "صيغة الملف لا تطابق محتواه")
    name = "%s.%s" % (secrets.token_hex(16), ext)
    (UPLOADS / name).write_bytes(data)
    return {"path": "/uploads/" + name}


# ---- لوحة التحكم (مشرفون بصلاحيات كاملة) ----
@route("GET", r"/api/admin/stats", "staff")
def adm_stats(ctx):
    c = ctx.conn
    return {
        "students": one(c, "SELECT COUNT(*) n FROM users WHERE role='student'")["n"],
        "supervisors": one(c, "SELECT COUNT(*) n FROM users WHERE role='supervisor'")["n"],
        "videos": one(c, "SELECT COUNT(*) n FROM videos WHERE published=1")["n"],
        "unlinked": one(c, "SELECT COUNT(*) n FROM videos WHERE published=1 AND kind='playlist' "
                           "AND youtube_id=''")["n"],
        "unread": one(c, "SELECT COUNT(*) n FROM messages WHERE is_read=0")["n"],
    }


@route("GET", r"/api/admin/students", "staff")
def adm_students(ctx):
    c = ctx.conn
    if ctx.has_full_access:
        users = rows(c, "SELECT id,name,username,gender,city,created_at FROM users WHERE role='student' ORDER BY created_at DESC")
    else:
        allowed = ctx.sup_programs()
        qm = ",".join("?" * len(allowed)) or "''"
        users = rows(c, "SELECT DISTINCT u.id,u.name,u.username,u.gender,u.city,u.created_at FROM users u "
                        "JOIN enrollments e ON e.user_id=u.id WHERE u.role='student' AND e.program_key IN (%s) "
                        "ORDER BY u.created_at DESC" % qm, allowed)
    return {"students": users}


def get_student(ctx, sid):
    s = one(ctx.conn, "SELECT * FROM users WHERE id=? AND role='student'", (sid,))
    if not s:
        raise ApiError(404, "الطالب غير موجود")
    return s


@route("PUT", r"/api/admin/students/(\d+)/programs", "staff")
def adm_set_programs(ctx, sid):
    c = ctx.conn
    get_student(ctx, sid)
    allowed = set(ctx.allowed_programs())
    desired = set(valid_program_keys(c, ctx.body.get("programs"))) & allowed
    existing = {e["program_key"]: e for e in rows(c, "SELECT * FROM enrollments WHERE user_id=?", (sid,))}
    for k in allowed:
        if k in desired:
            if k not in existing:
                c.execute("INSERT INTO enrollments (user_id,program_key,status,created_at) VALUES (?,?,?,?)", (sid, k, "active", now()))
            elif existing[k]["status"] != "active":
                c.execute("UPDATE enrollments SET status='active' WHERE id=?", (existing[k]["id"],))
        elif k in existing:
            c.execute("DELETE FROM enrollments WHERE id=?", (existing[k]["id"],))
    return {"ok": True}


@route("POST", r"/api/admin/students/(\d+)/send", "staff")
def adm_send(ctx, sid):
    c = ctx.conn
    s = get_student(ctx, sid)
    allowed = set(ctx.allowed_programs())
    todo = [e for e in rows(c, "SELECT * FROM enrollments WHERE user_id=? AND status='active' AND sent_at IS NULL", (sid,))
            if e["program_key"] in allowed]
    if not todo:
        raise ApiError(400, "لا توجد برامج جديدة للإرسال (اختر برنامجًا واحفظ أولًا)")
    for e in todo:
        c.execute("INSERT INTO outbox (program_key,user_id,name,username,gender,created_at) VALUES (?,?,?,?,?,?)",
                  (e["program_key"], sid, s["name"], s["username"], s["gender"], now()))
        c.execute("UPDATE enrollments SET sent_at=? WHERE id=?", (now(), e["id"]))
    return {"sent": [e["program_key"] for e in todo]}


@route("GET", r"/api/admin/supervisors", "admin")
def adm_sups(ctx):
    c = ctx.conn
    sups = rows(c, "SELECT id,name,username,gender,created_at FROM users WHERE role='supervisor' ORDER BY created_at DESC")
    for s in sups:
        s["programs"] = [r["program_key"] for r in rows(c, "SELECT program_key FROM supervisor_programs WHERE user_id=?", (s["id"],))]
    return {"supervisors": sups}


@route("POST", r"/api/admin/supervisors", "admin")
def adm_sup_add(ctx):
    b, c = ctx.body, ctx.conn
    name = text(b, "name", "اسم المشرف", 2, 80)
    gender = b.get("gender")
    if gender not in ("male", "female"):
        raise ApiError(400, "اختر نوع المشرف")
    username = norm_username(b.get("username"))
    if not USERNAME_RE.match(username):
        raise ApiError(400, "معرّف تيليجرام غير صالح")
    check_password_rules(b.get("password"))
    try:
        cur = c.execute("INSERT INTO users (username,name,gender,password_hash,role,created_at) VALUES (?,?,?,?,?,?)",
                        (username, name, gender, hash_pw(b["password"]), "supervisor", now()))
    except DB_INTEGRITY_ERRORS:
        raise ApiError(409, "هذا المعرّف مستخدم من قبل")
    c.executemany("INSERT OR IGNORE INTO supervisor_programs (user_id,program_key) VALUES (?,?)",
                  [(cur.lastrowid, r["key"]) for r in rows(c, "SELECT key FROM programs")])
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/supervisors/(\d+)/programs", "admin")
def adm_sup_programs(ctx, sid):
    c = ctx.conn
    if not one(c, "SELECT 1 AS x FROM users WHERE id=? AND role='supervisor'", (sid,)):
        raise ApiError(404, "المشرف غير موجود")
    c.execute("DELETE FROM supervisor_programs WHERE user_id=?", (sid,))
    for k in valid_program_keys(c, ctx.body.get("programs")):
        c.execute("INSERT INTO supervisor_programs VALUES (?,?)", (sid, k))
    return {"ok": True}


@route("DELETE", r"/api/admin/users/(\d+)", "admin")
def adm_user_delete(ctx, uid):
    u = one(ctx.conn, "SELECT * FROM users WHERE id=?", (uid,))
    if not u or u["role"] == "admin" or int(uid) == ctx.user["id"]:
        raise ApiError(404, "المستخدم غير موجود")
    ctx.conn.execute("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


@route("PUT", r"/api/admin/users/(\d+)/password", "admin")
def adm_user_password(ctx, uid):
    pw = ctx.body.get("password")
    check_password_rules(pw)
    cur = ctx.conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(pw), uid))
    if not cur.rowcount:
        raise ApiError(404, "المستخدم غير موجود")
    ctx.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    ctx.conn.execute("DELETE FROM password_recovery_codes WHERE user_id=?", (uid,))
    return {"ok": True}


@route("GET", r"/api/admin/messages", "staff")
def adm_messages(ctx):
    return {"messages": rows(ctx.conn, "SELECT * FROM messages ORDER BY created_at DESC LIMIT 500")}


@route("PUT", r"/api/admin/messages/(\d+)", "staff")
def adm_message_read(ctx, mid):
    ctx.conn.execute("UPDATE messages SET is_read=? WHERE id=?", (1 if ctx.body.get("read", True) else 0, mid))
    return {"ok": True}


@route("PUT", r"/api/admin/messages/(\d+)/reply", "staff")
def adm_message_reply(ctx, mid):
    reply = text(ctx.body, "reply", "الرد", 1, 3000)
    cur = ctx.conn.execute("UPDATE messages SET reply_text=?,replied_at=?,is_read=1 WHERE id=?",
                           (reply, now(), mid))
    if not cur.rowcount:
        raise ApiError(404, "الرسالة غير موجودة")
    return {"ok": True}


@route("DELETE", r"/api/admin/messages/(\d+)", "staff")
def adm_message_del(ctx, mid):
    ctx.conn.execute("DELETE FROM messages WHERE id=?", (mid,))
    return {"ok": True}


@route("GET", r"/api/admin/outbox", "admin")
def adm_outbox(ctx):
    return {"outbox": rows(ctx.conn, "SELECT o.*, p.name AS program_name FROM outbox o LEFT JOIN programs p ON p.key=o.program_key "
                                     "ORDER BY (o.status='pending') DESC, o.created_at DESC LIMIT 500")}


@route("PUT", r"/api/admin/outbox/(\d+)", "admin")
def adm_outbox_done(ctx, oid):
    ctx.conn.execute("UPDATE outbox SET status='delivered', delivered_at=? WHERE id=?", (now(), oid))
    return {"ok": True}


@route("DELETE", r"/api/admin/outbox/(\d+)", "admin")
def adm_outbox_del(ctx, oid):
    ctx.conn.execute("DELETE FROM outbox WHERE id=?", (oid,))
    return {"ok": True}


# ---- الفيديوهات ----
def video_fields(ctx):
    b = ctx.body
    title = text(b, "title", "العنوان", 2, 200)
    media_type = b.get("media_type", "playlist")
    if media_type not in ("playlist", "video", "audio"):
        raise ApiError(400, "اختر قائمة تشغيل أو فيديو أو صوتًا")
    raw_url = (b.get("url") or "").strip()
    kind, yid = "", ""
    media_url = ""
    if media_type == "playlist":
        kind, yid = parse_youtube(raw_url)
        if kind != "playlist":
            raise ApiError(400, "أدخل رابط قائمة تشغيل من يوتيوب")
    elif raw_url:
        if len(raw_url) > 500:
            raise ApiError(400, "رابط المادة طويل جدًا")
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ApiError(400, "رابط المادة يجب أن يبدأ بـ https://")
        host = (parsed.hostname or "").lower()
        if host == "youtu.be" or host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
            kind, yid = parse_youtube(raw_url)
            if kind != "video":
                raise ApiError(400, "أدخل رابط فيديو، وليس قائمة تشغيل")
        media_url = raw_url
    media_path = clean_path(b.get("media_path"), "ملف المادة")
    if media_path and not re.fullmatch(
            r"/uploads/[a-f0-9]{32}\.(?:mp4|m4v|mov|webm|mkv|avi|mp3|m4a|aac|wav|ogg|flac)", media_path):
        raise ApiError(400, "ملف المادة غير صالح")
    if media_type in ("video", "audio") and not media_path and not media_url:
        raise ApiError(400, "أضف رابط المادة أو ارفع ملفًا")
    category = (b.get("category") or "").strip()
    if category not in {item["key"] for item in rows(ctx.conn, "SELECT key FROM categories")}:
        raise ApiError(400, "اختر تصنيفًا صحيحًا")
    thumb = clean_path(b.get("thumb"), "الصورة")
    if thumb and not (thumb == "/img/malik-logo.jpg" or thumb.startswith("https://i.ytimg.com/vi/") or
                      re.fullmatch(r"/uploads/[a-f0-9]{16}\.(?:jpg|png|webp)", thumb)):
        raise ApiError(400, "الصورة غير صالحة")
    if thumb.startswith("/uploads/") and not (UPLOADS / Path(thumb).name).is_file():
        raise ApiError(400, "ارفع الصورة الاختيارية قبل حفظها")
    if media_path:
        extension = Path(media_path).suffix.lower().lstrip(".")
        allowed = ({"mp4", "m4v", "mov", "webm", "mkv", "avi"} if media_type == "video"
                   else {"mp3", "m4a", "aac", "wav", "ogg", "flac", "webm"})
        if extension not in allowed:
            raise ApiError(400, "صيغة الملف لا تطابق نوع المحتوى المختار")
        if not (UPLOADS / Path(media_path).name).is_file():
            raise ApiError(400, "ارفع ملف المادة قبل حفظها")
    return dict(title=title, kind="playlist" if media_type == "playlist" else "video",
                media_type=media_type, youtube_id=yid, media_url=media_url, media_path=media_path,
                category=category, thumb=thumb,
                home_section="", program_key="", description="",
                sort=integer(b, "sort"), published=1 if b.get("published", True) else 0)


def remove_unreferenced_upload(conn, path):
    match = re.fullmatch(r"/uploads/([a-f0-9]{16}|[a-f0-9]{32})\."
                         r"(?:jpg|png|webp|pdf|mp4|m4v|mov|webm|mkv|avi|mp3|m4a|aac|wav|ogg|flac)", path or "")
    if not match:
        return
    references = (
        ("videos", "media_path"),
        ("videos", "thumb"),
        ("words", "image_path"),
        ("playlist_files", "path"),
        ("lesson_resources", "path"),
        ("benefits", "path"),
        ("chat_messages", "attachment_path"),
        ("users", "avatar_path"),
    )
    for table, column in references:
        if one(conn, "SELECT 1 AS x FROM " + table + " WHERE " + column + "=? LIMIT 1", (path,)):
            return
    target = UPLOADS / Path(path).name
    if target.is_file():
        target.unlink()


@route("GET", r"/api/admin/videos", "admin")
def adm_videos(ctx):
    category_names = {item["key"]: item["name"] for item in rows(ctx.conn, "SELECT key,name FROM categories")}
    videos = [video_public(v, category_names) for v in rows(ctx.conn, "SELECT * FROM videos ORDER BY sort, id")]
    for video in videos:
        video["attachments"] = rows(ctx.conn, "SELECT id,title,path,original_name,size_bytes "
                                             "FROM playlist_files WHERE video_id=? ORDER BY id",
                                    (video["id"],))
    return {"videos": videos}


@route("POST", r"/api/admin/videos", "admin")
def adm_video_add(ctx):
    f = video_fields(ctx)
    cur = ctx.conn.execute("INSERT INTO videos (title,kind,media_type,youtube_id,media_url,media_path,category,thumb,"
                           "home_section,program_key,description,sort,published,created_at) "
                           "VALUES (:title,:kind,:media_type,:youtube_id,:media_url,:media_path,:category,:thumb,"
                           ":home_section,:program_key,:description,:sort,:published,:t)",
                           dict(f, t=now()))
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/videos/(\d+)", "admin")
def adm_video_edit(ctx, vid):
    f = video_fields(ctx)
    previous = one(ctx.conn, "SELECT media_path,thumb FROM videos WHERE id=?", (vid,))
    cur = ctx.conn.execute("UPDATE videos SET title=:title,kind=:kind,media_type=:media_type,youtube_id=:youtube_id,"
                           "media_url=:media_url,media_path=:media_path,category=:category,"
                           "thumb=:thumb,home_section=:home_section,program_key=:program_key,description=:description,"
                           "sort=:sort,published=:published WHERE id=:id",
                           dict(f, id=int(vid)))
    if not cur.rowcount:
        raise ApiError(404, "غير موجود")
    if previous:
        if previous["media_path"] != f["media_path"]:
            remove_unreferenced_upload(ctx.conn, previous["media_path"])
        if previous["thumb"] != f["thumb"]:
            remove_unreferenced_upload(ctx.conn, previous["thumb"])
    return {"ok": True}


@route("POST", r"/api/admin/videos/delete", "admin")
def adm_videos_delete(ctx):
    video_ids = ctx.body.get("ids")
    if (not isinstance(video_ids, list) or not video_ids or len(video_ids) > 500 or
            any(type(video_id) is not int or video_id < 1 for video_id in video_ids)):
        raise ApiError(400, "اختر محتوى صالحًا للحذف")
    video_ids = list(dict.fromkeys(video_ids))
    placeholders = ",".join("?" for _ in video_ids)
    videos = rows(ctx.conn, "SELECT id,media_path,thumb FROM videos WHERE id IN (%s)" % placeholders,
                  tuple(video_ids))
    if len(videos) != len(video_ids):
        raise ApiError(404, "بعض المحتوى المحدد غير موجود")
    attachments = rows(ctx.conn, "SELECT path FROM playlist_files WHERE video_id IN (%s)" % placeholders,
                       tuple(video_ids))
    ctx.conn.executemany("DELETE FROM videos WHERE id=?", [(video_id,) for video_id in video_ids])
    for video in videos:
        remove_unreferenced_upload(ctx.conn, video["media_path"])
        remove_unreferenced_upload(ctx.conn, video["thumb"])
    for attachment in attachments:
        remove_unreferenced_upload(ctx.conn, attachment["path"])
    return {"ok": True, "deleted": len(video_ids)}


@route("DELETE", r"/api/admin/videos/(\d+)", "admin")
def adm_video_del(ctx, vid):
    previous = one(ctx.conn, "SELECT media_path,thumb FROM videos WHERE id=?", (vid,))
    attachments = rows(ctx.conn, "SELECT path FROM playlist_files WHERE video_id=?", (vid,))
    cur = ctx.conn.execute("DELETE FROM videos WHERE id=?", (vid,))
    if not cur.rowcount:
        raise ApiError(404, "المادة غير موجودة")
    if previous:
        remove_unreferenced_upload(ctx.conn, previous["media_path"])
        remove_unreferenced_upload(ctx.conn, previous["thumb"])
    for attachment in attachments:
        remove_unreferenced_upload(ctx.conn, attachment["path"])
    return {"ok": True}


@route("POST", r"/api/admin/videos/(\d+)/files", "admin")
def adm_playlist_file_add(ctx, vid):
    playlist = one(ctx.conn, "SELECT id,kind FROM videos WHERE id=?", (vid,))
    if not playlist or playlist["kind"] != "playlist":
        raise ApiError(404, "قائمة التشغيل غير موجودة")
    path = ctx.body.get("path", "")
    match = re.fullmatch(r"/uploads/([a-f0-9]{32})\.(pdf|mp3|m4a|wav|ogg|webm)", path)
    if not match or not (UPLOADS / Path(path).name).is_file():
        raise ApiError(400, "ارفع ملفًا مرتبطًا صالحًا قبل حفظه")
    title = text(ctx.body, "title", "عنوان الملف", 2, 200)
    file_path = UPLOADS / Path(path).name
    cur = ctx.conn.execute("INSERT INTO playlist_files "
                           "(video_id,title,path,original_name,size_bytes,created_by,created_at) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (int(vid), title, path, Path(path).name, file_path.stat().st_size,
                            ctx.user["id"], now()))
    return {"id": cur.lastrowid}


@route("DELETE", r"/api/admin/playlist-files/(\d+)", "admin")
def adm_playlist_file_delete(ctx, file_id):
    attachment = one(ctx.conn, "SELECT path FROM playlist_files WHERE id=?", (file_id,))
    if not attachment:
        raise ApiError(404, "الملف المرتبط غير موجود")
    ctx.conn.execute("DELETE FROM playlist_files WHERE id=?", (file_id,))
    remove_unreferenced_upload(ctx.conn, attachment["path"])
    return {"ok": True}


# ---- الأسئلة الشائعة ----
@route("GET", r"/api/admin/faqs", "admin")
def adm_faqs(ctx):
    return {"faqs": rows(ctx.conn, "SELECT * FROM faqs ORDER BY sort, id")}


@route("POST", r"/api/admin/faqs", "admin")
def adm_faq_add(ctx):
    b = ctx.body
    cur = ctx.conn.execute("INSERT INTO faqs (q,a,sort) VALUES (?,?,?)",
                           (text(b, "q", "السؤال", 2, 300), text(b, "a", "الجواب", 2, 3000), integer(b, "sort")))
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/faqs/(\d+)", "admin")
def adm_faq_edit(ctx, fid):
    b = ctx.body
    ctx.conn.execute("UPDATE faqs SET q=?, a=?, sort=? WHERE id=?",
                     (text(b, "q", "السؤال", 2, 300), text(b, "a", "الجواب", 2, 3000), integer(b, "sort"), fid))
    return {"ok": True}


@route("DELETE", r"/api/admin/faqs/(\d+)", "admin")
def adm_faq_del(ctx, fid):
    ctx.conn.execute("DELETE FROM faqs WHERE id=?", (fid,))
    return {"ok": True}


# ---- البرامج ----
def pairs(v, label):
    out = []
    if not isinstance(v, list) or len(v) > 40:
        raise ApiError(400, "قائمة غير صالحة: " + label)
    for it in v:
        if not (isinstance(it, list) and len(it) == 2 and all(isinstance(x, str) for x in it)):
            raise ApiError(400, "قائمة غير صالحة: " + label)
        a, b = it[0].strip(), it[1].strip()
        if a:
            if len(a) > 200 or len(b) > 1000:
                raise ApiError(400, "نص طويل في: " + label)
            out.append((a, b))
    return out


def save_program_children(c, key, b):
    axes, plan = pairs(b.get("axes", []), "المحاور"), pairs(b.get("plan", []), "الخطة")
    c.execute("DELETE FROM program_axes WHERE program_key=?", (key,))
    c.execute("DELETE FROM program_plan WHERE program_key=?", (key,))
    for i, (x, y) in enumerate(axes):
        c.execute("INSERT INTO program_axes (program_key,title,body,sort) VALUES (?,?,?,?)", (key, x, y, i))
    for i, (x, y) in enumerate(plan):
        c.execute("INSERT INTO program_plan (program_key,stage,activity,sort) VALUES (?,?,?,?)", (key, x, y, i))


def program_fields(b):
    return (text(b, "name", "اسم البرنامج", 2, 80), text(b, "headline", "العنوان الفرعي", 0, 200, False),
            text(b, "audience", "الفئة المستهدفة", 0, 200, False), text(b, "description", "الوصف", 0, 1500, False),
            clean_path(b.get("cover"), "الصورة"), integer(b, "sort"), 1 if b.get("active", True) else 0)


@route("GET", r"/api/admin/programs", "admin")
def adm_programs(ctx):
    c = ctx.conn
    ps = rows(c, "SELECT * FROM programs ORDER BY sort, key")
    for p in ps:
        p["axes"] = rows(c, "SELECT title, body FROM program_axes WHERE program_key=? ORDER BY sort, id", (p["key"],))
        p["plan"] = rows(c, "SELECT stage, activity FROM program_plan WHERE program_key=? ORDER BY sort, id", (p["key"],))
        p["active"] = bool(p["active"])
    return {"programs": ps}


@route("POST", r"/api/admin/programs", "admin")
def adm_program_add(ctx):
    b, c = ctx.body, ctx.conn
    key = (b.get("key") or "").strip().lower()
    if not KEY_RE.match(key):
        raise ApiError(400, "المفتاح: حروف إنجليزية صغيرة وأرقام و _ (يبدأ بحرف)")
    if key in ("home", "lessons", "contact", "login", "signup", "account", "dashboard", "terms"):
        raise ApiError(400, "هذا المفتاح محجوز")
    if one(c, "SELECT 1 AS x FROM programs WHERE key=?", (key,)):
        raise ApiError(409, "المفتاح مستخدم من قبل")
    c.execute("INSERT INTO programs (key,name,headline,audience,description,cover,sort,active) VALUES (?,?,?,?,?,?,?,?)", (key,) + program_fields(b))
    save_program_children(c, key, b)
    return {"ok": True}


@route("PUT", r"/api/admin/programs/([a-z][a-z0-9_]*)", "admin")
def adm_program_edit(ctx, key):
    c = ctx.conn
    if not one(c, "SELECT 1 AS x FROM programs WHERE key=?", (key,)):
        raise ApiError(404, "البرنامج غير موجود")
    n, h, a, d, cv, s, ac = program_fields(ctx.body)
    c.execute("UPDATE programs SET name=?,headline=?,audience=?,description=?,cover=?,sort=?,active=? WHERE key=?", (n, h, a, d, cv, s, ac, key))
    save_program_children(c, key, ctx.body)
    return {"ok": True}


@route("DELETE", r"/api/admin/programs/([a-z][a-z0-9_]*)", "admin")
def adm_program_del(ctx, key):
    ctx.conn.execute("UPDATE videos SET program_key='' WHERE program_key=?", (key,))
    ctx.conn.execute("DELETE FROM programs WHERE key=?", (key,))
    return {"ok": True}


# ---- الإعدادات والرفع ----
@route("GET", r"/api/admin/settings", "admin")
def adm_settings(ctx):
    return {"settings": {r["key"]: r["value"] for r in rows(ctx.conn, "SELECT * FROM settings")}}


@route("PUT", r"/api/admin/settings", "admin")
def adm_settings_save(ctx):
    for k, v in ctx.body.items():
        if k not in seed.SETTINGS or not isinstance(v, str) or len(v) > 5000:
            continue
        if k.startswith(("tg_", "yt_")) and v.strip() and not re.match(r"^https://", v.strip()):
            raise ApiError(400, "الروابط يجب أن تبدأ بـ https://")
        ctx.conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v.strip() if k.startswith(("tg_", "yt_")) else v))
    return {"ok": True}


@route("GET", r"/api/admin/categories", "staff")
def adm_categories(ctx):
    return {"categories": rows(ctx.conn, "SELECT key,name,description,sort FROM categories ORDER BY sort,name")}


@route("POST", r"/api/admin/categories", "staff")
def adm_category_add(ctx):
    name = text(ctx.body, "name", "اسم العلم", 2, 100)
    description = text(ctx.body, "description", "وصف العلم", 0, 1000, required=False)
    key = "science_" + secrets.token_hex(4)
    try:
        ctx.conn.execute("INSERT INTO categories (key,name,description,sort) VALUES (?,?,?,?)",
                         (key, name, description, integer(ctx.body, "sort")))
    except DB_INTEGRITY_ERRORS:
        raise ApiError(409, "يوجد علم بهذا الاسم بالفعل")
    return {"key": key}


@route("PUT", r"/api/admin/categories/([a-z][a-z0-9_]{1,30})", "staff")
def adm_category_update(ctx, category_key):
    if not one(ctx.conn, "SELECT 1 AS x FROM categories WHERE key=?", (category_key,)):
        raise ApiError(404, "العلم غير موجود")
    name = text(ctx.body, "name", "اسم العلم", 2, 100)
    description = text(ctx.body, "description", "وصف العلم", 0, 1000, required=False)
    try:
        ctx.conn.execute("UPDATE categories SET name=?,description=?,sort=? WHERE key=?",
                         (name, description, integer(ctx.body, "sort"), category_key))
    except DB_INTEGRITY_ERRORS:
        raise ApiError(409, "يوجد علم بهذا الاسم بالفعل")
    return {"ok": True}


@route("DELETE", r"/api/admin/categories/([a-z][a-z0-9_]{1,30})", "staff")
def adm_category_delete(ctx, category_key):
    if not one(ctx.conn, "SELECT 1 AS x FROM categories WHERE key=?", (category_key,)):
        raise ApiError(404, "العلم غير موجود")
    if one(ctx.conn, "SELECT 1 AS x FROM videos WHERE category=? LIMIT 1", (category_key,)):
        raise ApiError(409, "انقل محتوى هذا العلم إلى علم آخر قبل حذفه")
    ctx.conn.execute("DELETE FROM categories WHERE key=?", (category_key,))
    return {"ok": True}


@route("POST", r"/api/admin/upload", "admin", raw=True)
def adm_upload(ctx):
    data = ctx.raw
    if not data or len(data) > MAX_UPLOAD:
        raise ApiError(400, "حجم الملف غير مقبول (الحد الأقصى ٣ ميجا)")
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise ApiError(400, "الصور المسموحة: JPG أو PNG أو WEBP")
    name = "%s.%s" % (secrets.token_hex(8), ext)
    (UPLOADS / name).write_bytes(data)
    return {"path": "/uploads/" + name}


def word_fields(ctx):
    title = text(ctx.body, "title", "عنوان الكلمة", 2, 200)
    body = text(ctx.body, "body", "وصف الكلمة", 2, 5000)
    image_path = clean_path(ctx.body.get("image_path"), "صورة الكلمة")
    if image_path and not re.fullmatch(r"/uploads/[a-f0-9]{16}\.(?:jpg|png|webp)", image_path):
        raise ApiError(400, "صورة الكلمة غير صالحة")
    if image_path and not (UPLOADS / Path(image_path).name).is_file():
        raise ApiError(400, "ارفع صورة الكلمة قبل حفظها")
    image_url = external_https_url(ctx.body.get("image_url", ""), "رابط صورة الكلمة")
    if image_path and image_url:
        raise ApiError(400, "استخدم ملف صورة مرفوعًا أو رابط الصورة، وليس الاثنين معًا")
    youtube_url = youtube_post_url(ctx.body.get("youtube_url", ""))
    return (title, body, image_path, image_url, youtube_url, integer(ctx.body, "sort"),
            int(bool(ctx.body.get("published", True))))


@route("GET", r"/api/admin/words", "staff")
def adm_words(ctx):
    return {"words": rows(ctx.conn, "SELECT id,title,body,image_path,image_url,youtube_url,sort,published,created_at "
                                    "FROM words ORDER BY sort,id DESC LIMIT 500")}


@route("POST", r"/api/admin/words", "staff")
def adm_word_add(ctx):
    title, body, image_path, image_url, youtube_url, sort, published = word_fields(ctx)
    if youtube_url and one(ctx.conn, "SELECT 1 AS x FROM words WHERE youtube_url=?", (youtube_url,)):
        raise ApiError(409, "هذا المنشور مضاف بالفعل إلى الكلمات")
    cur = ctx.conn.execute("INSERT INTO words (title,body,image_path,image_url,youtube_url,sort,published,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (title, body, image_path, image_url, youtube_url, sort, published, now()))
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/words/(\d+)", "staff")
def adm_word_update(ctx, word_id):
    word = one(ctx.conn, "SELECT image_path FROM words WHERE id=?", (word_id,))
    if not word:
        raise ApiError(404, "الكلمة غير موجودة")
    title, body, image_path, image_url, youtube_url, sort, published = word_fields(ctx)
    if youtube_url and one(ctx.conn, "SELECT 1 AS x FROM words WHERE youtube_url=? AND id<>?",
                           (youtube_url, word_id)):
        raise ApiError(409, "هذا المنشور مضاف بالفعل إلى الكلمات")
    ctx.conn.execute("UPDATE words SET title=?,body=?,image_path=?,image_url=?,youtube_url=?,sort=?,published=? "
                     "WHERE id=?",
                     (title, body, image_path, image_url, youtube_url, sort, published, word_id))
    if word["image_path"] != image_path:
        remove_unreferenced_upload(ctx.conn, word["image_path"])
    return {"ok": True}


@route("DELETE", r"/api/admin/words/(\d+)", "staff")
def adm_word_delete(ctx, word_id):
    word = one(ctx.conn, "SELECT image_path FROM words WHERE id=?", (word_id,))
    if not word:
        raise ApiError(404, "الكلمة غير موجودة")
    ctx.conn.execute("DELETE FROM words WHERE id=?", (word_id,))
    remove_unreferenced_upload(ctx.conn, word["image_path"])
    return {"ok": True}


@route("GET", r"/api/admin/announcements", "staff")
def adm_announcements(ctx):
    return {"announcements": rows(ctx.conn, "SELECT id,word_id,title,body,image_path,active,created_at "
                                           "FROM announcements ORDER BY id DESC LIMIT 100")}


@route("POST", r"/api/admin/announcements", "staff")
def adm_announcement_add(ctx):
    title = text(ctx.body, "title", "عنوان الإعلان", 2, 200)
    body = text(ctx.body, "body", "نص الإعلان", 2, 5000)
    image_path = clean_path(ctx.body.get("image_path"), "صورة الإعلان")
    if image_path and not re.fullmatch(r"/uploads/[a-f0-9]{16}\.(?:jpg|png|webp)", image_path):
        raise ApiError(400, "صورة الإعلان غير صالحة")
    if image_path and not (UPLOADS / Path(image_path).name).is_file():
        raise ApiError(400, "ارفع صورة الإعلان قبل حفظه")
    created_at = now()
    word_sort = one(ctx.conn, "SELECT COALESCE(MAX(sort),-1)+1 AS n FROM words")["n"]
    word = ctx.conn.execute(
        "INSERT INTO words(title,body,image_path,sort,published,created_at) VALUES (?,?,?,?,1,?)",
        (title, body, image_path, word_sort, created_at))
    ctx.conn.execute("UPDATE announcements SET active=0 WHERE active=1")
    announcement = ctx.conn.execute(
        "INSERT INTO announcements(word_id,title,body,image_path,active,created_by,created_at) "
        "VALUES (?,?,?,?,1,?,?)",
        (word.lastrowid, title, body, image_path, ctx.user["id"], created_at))
    return {"id": announcement.lastrowid, "word_id": word.lastrowid}


@route("GET", r"/api/admin/books", "staff")
def adm_books(ctx):
    return {"books": rows(ctx.conn, "SELECT id,title,author,description,media_url,media_path,sort,published "
                                    "FROM books ORDER BY sort,id DESC LIMIT 500")}


@route("POST", r"/api/admin/books", "staff")
def adm_book_add(ctx):
    title = text(ctx.body, "title", "عنوان الكتاب", 2, 200)
    author = text(ctx.body, "author", "المؤلف", 0, 200, required=False)
    description = text(ctx.body, "description", "وصف الكتاب", 0, 1000, required=False)
    media_url = external_https_url(ctx.body.get("media_url", ""), "رابط الكتاب")
    media_path = text(ctx.body, "media_path", "ملف الكتاب", 0, 300, required=False)
    if bool(media_url) == bool(media_path):
        raise ApiError(400, "أدخل رابط الكتاب أو ارفع ملف PDF، وليس الاثنين معًا")
    if media_path and uploaded_media_extension(media_path, "ملف الكتاب") != "pdf":
        raise ApiError(400, "يجب أن يكون ملف الكتاب بصيغة PDF")
    sort = integer(ctx.body, "sort")
    published = int(bool(ctx.body.get("published", True)))
    cur = ctx.conn.execute("INSERT INTO books "
                           "(title,author,description,media_url,media_path,sort,published,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (title, author, description, media_url, media_path, sort, published, now()))
    return {"id": cur.lastrowid}


@route("DELETE", r"/api/admin/books/(\d+)", "staff")
def adm_book_delete(ctx, book_id):
    book = one(ctx.conn, "SELECT media_path FROM books WHERE id=?", (book_id,))
    if not book:
        raise ApiError(404, "الكتاب غير موجود")
    ctx.conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    delete_uploaded_media(book["media_path"])
    return {"ok": True}


@route("GET", r"/api/admin/sermons", "staff")
def adm_sermons(ctx):
    return {"sermons": rows(ctx.conn, "SELECT id,title,speaker,description,media_type,media_url,media_path,"
                                      "sort,published FROM sermons ORDER BY sort,id DESC LIMIT 500")}


@route("POST", r"/api/admin/sermons", "staff")
def adm_sermon_add(ctx):
    title = text(ctx.body, "title", "عنوان الخطبة", 2, 200)
    speaker = text(ctx.body, "speaker", "الخطيب", 0, 200, required=False)
    description = text(ctx.body, "description", "وصف الخطبة", 0, 1000, required=False)
    media_type = ctx.body.get("media_type")
    if media_type not in ("video", "audio"):
        raise ApiError(400, "اختر نوع الخطبة: فيديو أو صوت")
    media_url = external_https_url(ctx.body.get("media_url", ""), "رابط الخطبة")
    media_path = text(ctx.body, "media_path", "ملف الخطبة", 0, 300, required=False)
    if bool(media_url) == bool(media_path):
        raise ApiError(400, "أدخل رابط الخطبة أو ارفع ملفًا، وليس الاثنين معًا")
    if media_path:
        extension = uploaded_media_extension(media_path, "ملف الخطبة")
        allowed = {"video": {"mp4", "m4v", "mov", "webm", "mkv", "avi"},
                   "audio": {"mp3", "m4a", "aac", "wav", "ogg", "flac", "webm"}}
        if extension not in allowed[media_type]:
            raise ApiError(400, "صيغة الملف لا تطابق نوع الخطبة المختار")
    sort = integer(ctx.body, "sort")
    published = int(bool(ctx.body.get("published", True)))
    cur = ctx.conn.execute("INSERT INTO sermons "
                           "(title,speaker,description,media_type,media_url,media_path,sort,published,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                           (title, speaker, description, media_type, media_url, media_path,
                            sort, published, now()))
    return {"id": cur.lastrowid}


@route("DELETE", r"/api/admin/sermons/(\d+)", "staff")
def adm_sermon_delete(ctx, sermon_id):
    sermon = one(ctx.conn, "SELECT media_path FROM sermons WHERE id=?", (sermon_id,))
    if not sermon:
        raise ApiError(404, "الخطبة غير موجودة")
    ctx.conn.execute("DELETE FROM sermons WHERE id=?", (sermon_id,))
    delete_uploaded_media(sermon["media_path"])
    return {"ok": True}


# ───────────────────────── معالج HTTP ─────────────────────────
SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",   # مطلوب لتشغيل مشغّل يوتيوب
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: https://i.ytimg.com https://yt3.ggpht.com; media-src 'self'; "
                                "frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
                                "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                "script-src 'self' https://www.youtube.com; connect-src 'self'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
}
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/webm", ".webm")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Mahad"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def client_ip(self):
        if TRUST_PROXY:
            xf = self.headers.get("X-Forwarded-For", "")
            if xf:
                return xf.split(",")[0].strip()
        return self.client_address[0]

    def send_bytes(self, status, data, ctype, extra=None, head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def send_json(self, status, obj, cookies=()):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, data, "application/json; charset=utf-8",
                        [("Cache-Control", "no-store")] + [("Set-Cookie", c) for c in cookies])

    def handle_any(self, method):
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            length = int(self.headers.get("Content-Length") or 0)
            limit = (BENEFIT_UPLOAD_CHUNK + 1024 if path.startswith("/api/admin/benefits/uploads/")
                     and re.fullmatch(r"/api/admin/benefits/uploads/[a-f0-9]{32}/[0-9]{1,4}", path)
                     else MAX_CHAT_UPLOAD + 1024 if path == "/api/chat/upload"
                     else MAX_MEDIA_UPLOAD + 1024 if path == "/api/admin/media/upload"
                     else MAX_UPLOAD + 1024 if path in ("/api/admin/upload", "/api/me/avatar") else MAX_JSON)
            if length > limit:
                self.close_connection = True
                return self.send_json(413, {"error": "الطلب كبير جدًا"})
            raw = self.rfile.read(length) if length else b""
            if path.startswith("/api/"):
                return self.api(method, path, parsed.query, raw)
            if method not in ("GET", "HEAD"):
                return self.send_json(405, {"error": "method not allowed"})
            return self.static(path, method == "HEAD")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json(500, {"error": "خطأ في الخادم"})
            except Exception:
                pass

    do_GET = lambda self: self.handle_any("GET")
    do_HEAD = lambda self: self.handle_any("HEAD")
    do_POST = lambda self: self.handle_any("POST")
    do_PUT = lambda self: self.handle_any("PUT")
    do_DELETE = lambda self: self.handle_any("DELETE")

    # ---- API ----
    def api(self, method, path, query, raw):
        conn = db()
        cookies = []
        try:
            token = None
            if "Cookie" in self.headers:
                try:
                    sc = SimpleCookie(self.headers["Cookie"])
                    token = sc["sid"].value if "sid" in sc else None
                except Exception:
                    token = None
            user = session_user(conn, token)
            if user and user["password_reset_required"]:
                allowed_reset_routes = {
                    ("GET", "/api/me"),
                    ("PUT", "/api/me/password"),
                    ("POST", "/api/logout"),
                }
                if (method, path) not in allowed_reset_routes:
                    raise ApiError(403, "غيّر كلمة المرور المؤقتة قبل متابعة استخدام الموقع")
            matched = False
            for m, rx, auth, is_raw, fn in ROUTES:
                mt = rx.match(path)
                if not mt:
                    continue
                matched = True
                if m != method:
                    continue
                if method != "GET":
                    origin = self.headers.get("Origin")
                    if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                        raise ApiError(403, "طلب مرفوض (مصدر غير مطابق)")
                if auth and not user:
                    raise ApiError(401, "سجّل الدخول أولًا")
                if auth == "staff" and user["role"] not in ("admin", "supervisor"):
                    raise ApiError(403, "غير مصرح لك")
                if auth == "admin" and user["role"] not in ("admin", "supervisor"):
                    raise ApiError(403, "غير مصرح لك")
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                body = {}
                if not is_raw and raw:
                    if ctype != "application/json":
                        raise ApiError(415, "نوع المحتوى غير مدعوم")
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        raise ApiError(400, "بيانات غير صالحة")
                    if not isinstance(body, dict):
                        raise ApiError(400, "بيانات غير صالحة")
                q = {k: v[0] for k, v in parse_qs(query).items()}
                q["_sid"] = token
                ctx = Ctx(conn, user, body, q, self.client_ip(), raw, ctype)
                result = fn(ctx, *mt.groups())
                conn.commit()
                return self.send_json(200, result, ctx.cookies)
            raise ApiError(405 if matched else 404, "غير موجود")
        except ApiError as e:
            conn.rollback()
            self.send_json(e.status, {"error": e.msg}, cookies)
        except Exception:
            conn.rollback()
            traceback.print_exc()
            self.send_json(500, {"error": "خطأ في الخادم"})
        finally:
            conn.close()

    # ---- ملفات ثابتة ----
    def static(self, path, head):
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            return self.send_json(404, {"error": "not found"})
        if not target.is_file():
            return self.send_json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith(("text/", "application/javascript")):
            ctype += "; charset=utf-8"
        cache = "public, max-age=86400" if target.suffix in (".jpg", ".png", ".webp") else "no-cache"
        size = target.stat().st_size
        start, end, status = 0, max(0, size - 1), 200
        extra = [("Cache-Control", cache), ("Accept-Ranges", "bytes")]
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or size == 0:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                for key, value in SEC_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                return
            left, right = match.groups()
            if not left:
                suffix = int(right or 0)
                if suffix <= 0:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    for key, value in SEC_HEADERS.items():
                        self.send_header(key, value)
                    self.end_headers()
                    return
                start = max(0, size - suffix)
            else:
                start = int(left)
                if right:
                    end = int(right)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                for key, value in SEC_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
            extra.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1 if size else 0))
        for key, value in SEC_HEADERS.items():
            self.send_header(key, value)
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if not head and size:
            with target.open("rb") as media_file:
                media_file.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = media_file.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)


def main():
    configure_stdout = getattr(sys.stdout, "reconfigure", None)
    if configure_stdout:
        configure_stdout(encoding="utf-8")
    if len(sys.argv) >= 2 and sys.argv[1] == "resetpass":
        if len(sys.argv) != 3:
            sys.exit("الاستخدام: python server.py resetpass USERNAME")
        init_db()
        conn = db()
        u = norm_username(sys.argv[2])
        pw = secrets.token_urlsafe(18)
        cur = conn.execute("UPDATE users SET password_hash=?,password_reset_required=1 WHERE username=?",
                           (hash_pw(pw), u))
        if cur.rowcount:
            conn.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=?)", (u,))
            conn.execute("DELETE FROM password_recovery_codes WHERE user_id IN "
                         "(SELECT id FROM users WHERE username=?)", (u,))
            conn.commit()
            print("تم إنشاء كلمة مرور مؤقتة لحساب %s:" % u)
            print(pw)
            print("لن يعمل الحساب حتى تغييرها عند تسجيل الدخول الأول.")
        conn.close()
        sys.exit(0 if cur.rowcount else "المستخدم غير موجود")
    if len(sys.argv) >= 2 and sys.argv[1] == "setpass":
        if len(sys.argv) != 4:
            sys.exit("الاستخدام: python server.py setpass USERNAME NEWPASSWORD")
        init_db()
        conn = db()
        u = norm_username(sys.argv[2])
        if len(sys.argv[3]) < 8:
            sys.exit("كلمة المرور يجب ألا تقل عن ٨ أحرف")
        cur = conn.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_pw(sys.argv[3]), u))
        conn.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=?)", (u,))
        conn.execute("DELETE FROM password_recovery_codes WHERE user_id IN "
                     "(SELECT id FROM users WHERE username=?)", (u,))
        conn.commit()
        sys.exit("تم التحديث" if cur.rowcount else "المستخدم غير موجود")
    init_db()
    if TELEGRAM_BOT_TOKEN:
        threading.Thread(target=telegram_poll_loop, name="telegram-bot-poller", daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("الموقع يعمل على http://localhost:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nتم الإيقاف")


if __name__ == "__main__":
    main()
