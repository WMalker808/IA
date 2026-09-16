"""Storage for daily importance marks.

Design note
-----------
Nothing here is a flag on an article. The table `mark_events` is append-only:
marking writes a row, retracting writes another row. Current state is derived
by taking the latest event for each (content_id, mark_date) pair.

That is deliberate. A boolean on the article can only answer "is this important
now"; the log can answer "what did we think was important on 3 September, who
decided, and when did they change their mind".

`articles` is a local cache of what we last read out of the Flexible Content
API. It is not a source of truth and nothing here ever writes back to the CMS.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("IMPORTANCE_DB", os.path.join(HERE, "importance.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    content_id      TEXT PRIMARY KEY,   -- Flexible document id, e.g. 6aa2bb838f08f44a1b8d91a8
    path            TEXT NOT NULL,      -- identifiers.path
    page_id         TEXT,               -- identifiers.pageId
    headline        TEXT NOT NULL,
    section         TEXT,
    web_url         TEXT,
    revision        INTEGER,            -- contentChangeDetails.revision, last seen
    last_modified   TEXT,
    refreshed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mark_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id  TEXT NOT NULL REFERENCES articles(content_id),
    mark_date   TEXT NOT NULL,          -- YYYY-MM-DD, the day the article matters for
    action      TEXT NOT NULL CHECK (action IN ('mark', 'retract')),
    actor       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    revision    INTEGER,                -- article revision at the moment of the event
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_date ON mark_events (mark_date, content_id, id);
"""

# Latest event per (article, day) wins. Everything else stays in the log.
CURRENT_SQL = """
SELECT e.*
FROM mark_events e
JOIN (
    SELECT content_id, mark_date, MAX(id) AS id
    FROM mark_events
    WHERE mark_date = ?
    GROUP BY content_id, mark_date
) latest ON latest.id = e.id
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- articles (cache of the CMS) -------------------------------------------

def upsert_article(conn, article):
    conn.execute(
        """
        INSERT INTO articles (content_id, path, page_id, headline, section, web_url,
                              revision, last_modified, refreshed_at)
        VALUES (:content_id, :path, :page_id, :headline, :section, :web_url,
                :revision, :last_modified, :refreshed_at)
        ON CONFLICT(content_id) DO UPDATE SET
            path = excluded.path,
            headline = excluded.headline,
            section = excluded.section,
            web_url = excluded.web_url,
            revision = excluded.revision,
            last_modified = excluded.last_modified,
            refreshed_at = excluded.refreshed_at
        """,
        {**article, "refreshed_at": now_iso()},
    )


def all_articles(conn):
    return conn.execute("SELECT * FROM articles ORDER BY headline").fetchall()


def get_article(conn, content_id):
    return conn.execute("SELECT * FROM articles WHERE content_id = ?", (content_id,)).fetchone()


# --- marks ------------------------------------------------------------------

def record(conn, content_id, mark_date, action, actor, note=None):
    """Append one event. Never updates or deletes."""
    article = get_article(conn, content_id)
    if article is None:
        raise ValueError(f"unknown article {content_id}")
    conn.execute(
        """INSERT INTO mark_events (content_id, mark_date, action, actor, occurred_at, revision, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content_id, mark_date, action, actor, now_iso(), article["revision"], note),
    )


def marked_on(conn, mark_date):
    """Articles currently marked for a given day, with staleness worked out.

    `revision_drift` is the gap between the article's revision now and its
    revision when it was marked. A large gap means the piece has been rewritten
    since somebody decided it mattered, which is worth a second look.
    """
    rows = conn.execute(
        f"""
        SELECT e.*, a.headline, a.section, a.path, a.web_url,
               a.revision AS current_revision
        FROM ({CURRENT_SQL}) e
        JOIN articles a ON a.content_id = e.content_id
        WHERE e.action = 'mark'
        ORDER BY e.occurred_at
        """,
        (mark_date,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        marked_at_rev = r["revision"] or 0
        d["revision_drift"] = (r["current_revision"] or 0) - marked_at_rev
        out.append(d)
    return out


def retracted_on(conn, mark_date):
    return [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT e.*, a.headline, a.section
            FROM ({CURRENT_SQL}) e
            JOIN articles a ON a.content_id = e.content_id
            WHERE e.action = 'retract'
            ORDER BY e.occurred_at
            """,
            (mark_date,),
        ).fetchall()
    ]


def unmarked_candidates(conn, mark_date):
    """Articles with no current mark for this day."""
    return conn.execute(
        f"""
        SELECT a.* FROM articles a
        WHERE a.content_id NOT IN (
            SELECT content_id FROM ({CURRENT_SQL}) WHERE action = 'mark'
        )
        ORDER BY a.headline
        """,
        (mark_date,),
    ).fetchall()


def article_history(conn, content_id):
    return conn.execute(
        """SELECT * FROM mark_events WHERE content_id = ? ORDER BY id DESC""",
        (content_id,),
    ).fetchall()


def days_with_marks(conn, limit=14):
    """Recent days that have any marking activity, newest first."""
    return conn.execute(
        """SELECT mark_date, COUNT(DISTINCT content_id) AS touched
           FROM mark_events GROUP BY mark_date ORDER BY mark_date DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def day_counts(conn, mark_date):
    marked = len(marked_on(conn, mark_date))
    events = conn.execute(
        "SELECT COUNT(*) c FROM mark_events WHERE mark_date = ?", (mark_date,)
    ).fetchone()["c"]
    return {"marked": marked, "events": events}


def article_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
