#!/usr/bin/env python3
import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import seed
import server


CONTENT_TABLES = (
    ("categories", ("key", "name", "description", "sort"), ""),
    ("faqs", ("id", "q", "a", "sort"), ""),
    (
        "videos",
        (
            "id", "title", "kind", "media_type", "youtube_id", "media_url",
            "media_path", "category", "thumb", "home_section", "program_key",
            "description", "sort", "published", "created_at",
        ),
        "published=1",
    ),
    (
        "lesson_catalog",
        ("video_id", "title", "playlist_id", "updated_at"),
        "",
    ),
    (
        "lesson_audio_playlists",
        ("id", "title", "description", "created_at"),
        "",
    ),
    (
        "lesson_resources",
        (
            "id", "video_id", "kind", "title", "body", "path",
            "audio_playlist_id", "created_at",
        ),
        "",
    ),
    (
        "benefits",
        (
            "id", "title", "description", "kind", "path", "youtube_id",
            "original_name", "size_bytes", "published", "sort", "created_at",
        ),
        "published=1",
    ),
    (
        "books",
        (
            "id", "title", "author", "description", "media_url", "media_path",
            "sort", "published", "created_at",
        ),
        "published=1",
    ),
    (
        "sermons",
        (
            "id", "title", "speaker", "description", "media_type", "media_url",
            "media_path", "sort", "published", "created_at",
        ),
        "published=1",
    ),
    (
        "words",
        (
            "id", "title", "body", "image_path", "image_url", "youtube_url",
            "sort", "published", "created_at",
        ),
        "published=1",
    ),
    (
        "announcements",
        ("id", "word_id", "title", "body", "image_path", "active", "created_at"),
        "active=1",
    ),
)

CONTENT_SETTINGS = (*seed.SETTINGS, "contact_text")
RESET_TABLES = (
    "announcements",
    "lesson_resources",
    "lesson_catalog",
    "lesson_audio_playlists",
    "books",
    "sermons",
    "benefits",
    "words",
    "faqs",
    "videos",
    "categories",
)
USER_DATA_TABLES = (
    "sessions",
    "telegram_link_tokens",
    "password_recovery_codes",
    "enrollments",
    "saved_videos",
    "lesson_progress",
    "lesson_questions",
    "chat_messages",
    "notifications",
    "messages",
)


def source_table(source, table, columns, condition):
    fields = ",".join(columns)
    where = " WHERE " + condition if condition else ""
    return [
        tuple(row)
        for row in source.execute("SELECT %s FROM %s%s" % (fields, table, where)).fetchall()
    ]


def ensure_target_is_safe(target):
    tables = {
        row["table_name"]
        for row in target.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )
    }
    if "users" not in tables:
        return
    counts = {
        table: target.execute("SELECT COUNT(*) AS count FROM %s" % table).fetchone()["count"]
        for table in USER_DATA_TABLES
        if table in tables
    }
    user_count = target.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if user_count > 1 or any(counts.values()):
        raise RuntimeError(
            "Refusing to initialize or replace content in a database that already contains user data."
        )


def main():
    parser = argparse.ArgumentParser(
        description="Copy published site content from the local SQLite database to Supabase PostgreSQL."
    )
    parser.add_argument("--source", type=Path, default=BASE / "mahad.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the target schema and replace its public content with the local published content.",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        raise SystemExit("Source SQLite database not found: %s" % args.source)
    if args.apply and not server.DATABASE_URL:
        server.DATABASE_URL = getpass.getpass("Supabase PostgreSQL connection string (input hidden): ").strip()
        if not server.DATABASE_URL:
            raise SystemExit("A Supabase PostgreSQL connection string is required.")

    source = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        imported = {
            table: source_table(source, table, columns, condition)
            for table, columns, condition in CONTENT_TABLES
        }
        settings = {
            key: row[0]
            for key in CONTENT_SETTINGS
            if (row := source.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone())
        }
        print("Published content ready to copy:")
        for table, records in imported.items():
            print("  %s: %d" % (table, len(records)))
        print("  public settings: %d" % len(settings))
        if not args.apply:
            print("Dry run only. Add --apply after configuring DATABASE_URL to write to Supabase.")
            return

        target = server.db()
        try:
            ensure_target_is_safe(target)
        finally:
            target.close()
        server.init_db()
        target = server.db()
        try:
            user_data = {
                table: target.execute("SELECT COUNT(*) AS count FROM %s" % table).fetchone()["count"]
                for table in USER_DATA_TABLES
            }
            populated = [table for table, count in user_data.items() if count]
            users = target.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
            if populated or users > 1:
                raise RuntimeError(
                    "Refusing to replace content in a database that already contains user data."
                )

            for table in RESET_TABLES:
                target.execute("DELETE FROM %s" % table)

            for table, columns, _ in CONTENT_TABLES:
                if not imported[table]:
                    continue
                fields = ",".join(columns)
                placeholders = ",".join("?" for _ in columns)
                target.executemany(
                    "INSERT INTO %s (%s) VALUES (%s)" % (table, fields, placeholders),
                    imported[table],
                )

            for key, value in settings.items():
                target.execute(
                    "INSERT INTO settings (key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

            for table in (
                "announcements", "faqs", "lesson_audio_playlists", "lesson_resources",
                "benefits", "books", "sermons", "videos", "words",
            ):
                target.execute(
                    "SELECT setval(pg_get_serial_sequence(?, 'id'), "
                    "COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM %s" % table,
                    (table,),
                )
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.close()
        print("Published content migration completed. User accounts and private data were not copied.")
    finally:
        source.close()


if __name__ == "__main__":
    main()
