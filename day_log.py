"""Day log — daily importance tracker, single-file prototype.

Everything is in here: schema, CMS ingest, routes, templates and CSS. No
templates/ or static/ folders to get wrong.

    pip install flask
    python day_log.py --seed        # build the database with a few days of history
    python day_log.py               # http://localhost:5000

Put flexibleapi.json and capi.json next to this file and the seed will read the
real article out of them. Without them it falls back to a stub.

Design note
-----------
Nothing here is a flag on an article. `mark_events` is append-only: marking
writes a row, retracting writes another. Current state is the latest event per
(content_id, mark_date). A boolean on the article could only ever say whether
something is important now; the log can say what we thought on 3 September, who
decided, and when they changed their mind.

Marks deliberately do not live in the CMS. Writing to the Flexible document
would bump its revision and put a curation click on the content update stream,
making an editorial judgement look like an edit — and spoiling the revision
number we rely on to spot rewrites.
"""

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from jinja2 import DictLoader

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("IMPORTANCE_DB", os.path.join(HERE, "importance.db"))

# Stand-in for real auth. In the newsroom this would be the signed-in editor.
CURRENT_USER = "hollie.richardson@guardian.co.uk"

# A piece rewritten by this many revisions since marking gets flagged for review.
DRIFT_THRESHOLD = 25


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

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


def upsert_article(conn, article):
    payload = dict(article)
    payload["refreshed_at"] = now_iso()
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
        payload,
    )


def get_article(conn, content_id):
    return conn.execute("SELECT * FROM articles WHERE content_id = ?", (content_id,)).fetchone()


def record(conn, content_id, mark_date, action, actor, note=None):
    """Append one event. Never updates or deletes."""
    article = get_article(conn, content_id)
    if article is None:
        raise ValueError("unknown article " + content_id)
    conn.execute(
        """INSERT INTO mark_events (content_id, mark_date, action, actor, occurred_at, revision, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content_id, mark_date, action, actor, now_iso(), article["revision"], note),
    )


def marked_on(conn, mark_date):
    """Articles currently marked for a day, with staleness worked out.

    `revision_drift` is the gap between the article's revision now and its
    revision when marked. A large gap means it has been rewritten since somebody
    decided it mattered.
    """
    rows = conn.execute(
        """
        SELECT e.*, a.headline, a.section, a.path, a.web_url,
               a.revision AS current_revision
        FROM (%s) e
        JOIN articles a ON a.content_id = e.content_id
        WHERE e.action = 'mark'
        ORDER BY e.occurred_at
        """ % CURRENT_SQL,
        (mark_date,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["revision_drift"] = (r["current_revision"] or 0) - (r["revision"] or 0)
        out.append(d)
    return out


def retracted_on(conn, mark_date):
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT e.*, a.headline, a.section
            FROM (%s) e
            JOIN articles a ON a.content_id = e.content_id
            WHERE e.action = 'retract'
            ORDER BY e.occurred_at
            """ % CURRENT_SQL,
            (mark_date,),
        ).fetchall()
    ]


def unmarked_candidates(conn, mark_date):
    return conn.execute(
        """
        SELECT a.* FROM articles a
        WHERE a.content_id NOT IN (
            SELECT content_id FROM (%s) WHERE action = 'mark'
        )
        ORDER BY a.headline
        """ % CURRENT_SQL,
        (mark_date,),
    ).fetchall()


def article_history(conn, content_id):
    return conn.execute(
        "SELECT * FROM mark_events WHERE content_id = ? ORDER BY id DESC", (content_id,)
    ).fetchall()


def days_with_marks(conn, limit=14):
    return conn.execute(
        """SELECT mark_date, COUNT(DISTINCT content_id) AS touched
           FROM mark_events GROUP BY mark_date ORDER BY mark_date DESC LIMIT ?""",
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# CMS ingest
# ---------------------------------------------------------------------------
#
#   Flexible   data.id                             -> content_id
#              data.identifiers.path               -> path
#              data.contentChangeDetails.revision  -> revision
#
#   CAPI       fields.internalComposerCode         -> content_id  (same value)
#              content.id                          -> path        (same value)
#              fields.internalRevision             -> revision    (same value)
#
# Nothing here writes back. This tool only ever reads the CMS.


def _unwrap(node):
    """Flexible wraps most values as {uri, data, links}."""
    if isinstance(node, dict) and "data" in node and set(node) <= {"uri", "data", "links"}:
        return node["data"]
    return node


def from_flexible(payload):
    data = payload["data"]
    live = _unwrap(data.get("live")) or _unwrap(data.get("preview")) or {}
    fields = _unwrap(live.get("fields", {})) or {}
    taxonomy = _unwrap(live.get("taxonomy", {})) or {}
    change = _unwrap(data.get("contentChangeDetails", {})) or {}
    ids = _unwrap(data.get("identifiers", {})) or {}

    section = None
    for entry in _unwrap(taxonomy.get("tags", [])) or []:
        tag = entry.get("tag", {})
        if tag.get("section"):
            section = tag["section"].get("name")
            break

    path = _unwrap(ids.get("path"))
    return {
        "content_id": data["id"],
        "path": path,
        "page_id": _unwrap(ids.get("pageId")),
        "headline": _unwrap(fields.get("headline")) or _unwrap(fields.get("linkText")) or "(untitled)",
        "section": section,
        "web_url": ("https://www.theguardian.com/" + path) if path else None,
        "revision": change.get("revision"),
        "last_modified": (change.get("lastModified") or {}).get("date"),
    }


def from_capi(payload):
    content = payload["response"]["content"]
    fields = content.get("fields", {})
    return {
        "content_id": fields.get("internalComposerCode") or content["id"],
        "path": content["id"],
        "page_id": fields.get("internalPageCode"),
        "headline": fields.get("headline") or content.get("webTitle") or "(untitled)",
        "section": content.get("sectionName"),
        "web_url": content.get("webUrl"),
        "revision": int(fields["internalRevision"]) if fields.get("internalRevision") else None,
        "last_modified": fields.get("lastModified"),
    }


def from_payload(payload):
    """Detect which API a blob came from and parse it."""
    if isinstance(payload, dict) and "data" in payload and "id" in payload.get("data", {}):
        return from_flexible(payload)
    if isinstance(payload, dict) and "response" in payload:
        return from_capi(payload)
    raise ValueError("payload is neither a Flexible document nor a CAPI response")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BASE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Day log{% endblock %}</title>
<link rel="stylesheet" href="{{ url_for('stylesheet') }}">
</head>
<body>
<div class="wrap">
  <p class="tool"><a href="{{ url_for('home') }}">Day log</a> — what mattered, and when we said so</p>
  {% block content %}{% endblock %}
</div>
</body>
</html>
"""

DAY_HTML = """{% extends "base.html" %}
{% block title %}{{ day|longdate }} — Day log{% endblock %}

{% block content %}
<h1 class="daydate">{{ day|longdate }}</h1>
<p class="daysub">
  {% if marked|length == 0 %}
    Nothing marked yet{% if is_today %} today{% endif %}.
  {% elif marked|length == 1 %}
    One article marked{% if is_today %} so far today{% endif %}.
  {% else %}
    {{ marked|length }} articles marked{% if is_today %} so far today{% endif %}.
  {% endif %}
  {% if is_past %}This day is closed — you're looking at what was decided at the time.{% endif %}
</p>

<nav class="daynav">
  <a href="{{ url_for('day', day_str=prev_day) }}">← {{ prev_day|shortdate }}</a>
  {% if next_day %}<a href="{{ url_for('day', day_str=next_day) }}">{{ next_day|shortdate }} →</a>{% endif %}
  {% if not is_today %}<a href="{{ url_for('home') }}">Back to today</a>{% endif %}
</nav>

<h2>Marked</h2>
<ul class="log">
  {% for m in marked %}
  <li class="entry">
    <span class="time">{{ m.occurred_at|clock }}</span>
    <div>
      <p class="headline"><a href="{{ url_for('article', content_id=m.content_id) }}">{{ m.headline }}</a></p>
      <p class="meta">{{ m.actor|person }} · {{ m.section }} · revision {{ m.revision }}</p>
      {% if m.note %}<p class="note">{{ m.note }}</p>{% endif %}
      {% if m.revision_drift > drift_threshold %}
        <span class="drift">Rewritten since marking — {{ m.revision_drift }} revisions on. Worth a second look.</span>
      {% endif %}
      {% if not is_past %}
      <div class="actions">
        <form class="inline" method="post" action="{{ url_for('retract', day_str=day_str) }}">
          <input type="hidden" name="content_id" value="{{ m.content_id }}">
          <input type="text" name="note" placeholder="Why it's coming off (optional)">
          <button class="quiet" type="submit">Retract</button>
        </form>
      </div>
      {% endif %}
    </div>
  </li>
  {% else %}
  <li class="empty">Mark an article below and it appears here, stamped with the time and your name.</li>
  {% endfor %}
</ul>

{% if retracted %}
<h2>Retracted</h2>
<ul class="log">
  {% for r in retracted %}
  <li class="entry is-retracted">
    <span class="time">{{ r.occurred_at|clock }}</span>
    <div>
      <p class="headline">{{ r.headline }}</p>
      <p class="meta">Taken off by {{ r.actor|person }}</p>
      {% if r.note %}<p class="note">{{ r.note }}</p>{% endif %}
    </div>
  </li>
  {% endfor %}
</ul>
{% endif %}

{% if not is_past %}
<h2>Mark an article</h2>
<p class="hint">Marking records the article's current revision, so we can tell later if it changed after the call was made.</p>
<div class="log">
  {% for c in candidates %}
  <div class="candidate">
    <div>
      <p class="headline">{{ c.headline }}</p>
      <p class="meta">{{ c.section }} · revision {{ c.revision }}</p>
    </div>
    <form class="inline" method="post" action="{{ url_for('mark', day_str=day_str) }}">
      <input type="hidden" name="content_id" value="{{ c.content_id }}">
      <input type="text" name="note" placeholder="Note (optional)">
      <button type="submit">Mark</button>
    </form>
  </div>
  {% else %}
  {% if cached == 0 %}
  <p class="empty">No articles loaded yet. Run <code>python day_log.py --seed</code>, or POST a Flexible or CAPI payload to <code>/api/articles</code>. Reading from <code>{{ db_path }}</code>.</p>
  {% else %}
  <p class="empty">All {{ cached }} cached articles are already marked for this day.</p>
  {% endif %}
  {% endfor %}
</div>
{% endif %}

{% if recent %}
<h2>Earlier days</h2>
<ul class="earlier">
  {% for d in recent %}
  <li>
    <a href="{{ url_for('day', day_str=d.mark_date) }}">{{ d.mark_date|shortdate }}</a>
    <span class="count">— {{ d.touched }} article{{ '' if d.touched == 1 else 's' }} touched</span>
  </li>
  {% endfor %}
</ul>
{% endif %}
{% endblock %}
"""

ARTICLE_HTML = """{% extends "base.html" %}
{% block title %}{{ article.headline }} — Day log{% endblock %}

{% block content %}
<h1 class="daydate">{{ article.headline }}</h1>
<p class="daysub">
  {{ article.section }} · now at revision {{ article.revision }}
  {% if article.web_url %}· <a href="{{ article.web_url }}">Read on the site</a>{% endif %}
</p>

<h2>Every time this piece was marked or taken off</h2>
<ul class="history">
  {% for e in history %}
  <li class="{{ 'mark-row' if e.action == 'mark' else 'retract-row' }}">
    <span class="when">{{ e.mark_date|shortdate }}</span>
    <div>
      <p>{{ 'Marked' if e.action == 'mark' else 'Retracted' }} by {{ e.actor|person }} at {{ e.occurred_at|clock }}</p>
      <p class="meta">Article was at revision {{ e.revision }}</p>
      {% if e.note %}<p class="note">{{ e.note }}</p>{% endif %}
    </div>
  </li>
  {% else %}
  <li class="empty">This article has never been marked.</li>
  {% endfor %}
</ul>
{% endblock %}
"""

STYLESHEET = """
:root {
  --ground: #e3e6e9;
  --surface: #ffffff;
  --ink: #17212b;
  --muted: #667283;
  --line: #ccd2d8;
  --signal: #0c5a4c;
  --caution: #8a5a00;
  --retract: #8c2f26;
  --focus: #1a47b8;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  font-size: 16px;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
}

.wrap { max-width: 54rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }

a { color: inherit; }
a:focus-visible, button:focus-visible, input:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}

.tool { font-size: 0.8125rem; color: var(--muted); margin: 0 0 2rem; }
.tool a { text-decoration: none; }
.tool a:hover { text-decoration: underline; }

.daydate {
  font-size: clamp(2rem, 6vw, 3rem);
  font-weight: 500;
  letter-spacing: -0.025em;
  line-height: 1.05;
  margin: 0;
}

.daysub { color: var(--muted); margin: 0.5rem 0 0; max-width: 34rem; }

.daynav { display: flex; gap: 1rem; margin: 1.25rem 0 2.5rem; font-size: 0.875rem; }
.daynav a { color: var(--muted); text-decoration: none; }
.daynav a:hover { color: var(--ink); text-decoration: underline; }

h2 { font-size: 0.9375rem; font-weight: 600; margin: 2.5rem 0 0.75rem; }

.log { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }

.entry {
  display: grid;
  grid-template-columns: 4.5rem 1fr;
  gap: 0 1rem;
  padding: 1rem 0;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}
.entry > * { padding-right: 1rem; }
.entry .time { padding-left: 1rem; color: var(--signal); font-weight: 600; font-size: 0.9375rem; }
.entry.is-retracted .time { color: var(--retract); }

.entry .headline {
  font-size: 1.0625rem;
  font-weight: 500;
  letter-spacing: -0.01em;
  margin: 0 0 0.25rem;
}
.entry .headline a { text-decoration: none; }
.entry .headline a:hover { text-decoration: underline; }

.entry .meta { color: var(--muted); font-size: 0.875rem; margin: 0; }
.entry .note {
  margin: 0.4rem 0 0;
  font-size: 0.9375rem;
  border-left: 2px solid var(--line);
  padding-left: 0.75rem;
}
.entry .actions { margin-top: 0.6rem; }

.drift {
  display: block;
  margin-top: 0.4rem;
  color: var(--caution);
  font-size: 0.875rem;
  font-weight: 500;
}

.empty {
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  padding: 1.25rem 1rem;
  color: var(--muted);
  margin: 0;
}

form.inline { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }

input[type="text"] {
  font: inherit;
  padding: 0.4rem 0.6rem;
  border: 1px solid var(--line);
  border-radius: 2px;
  background: var(--surface);
  min-width: 14rem;
  flex: 1 1 14rem;
}

button {
  font: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  padding: 0.4rem 0.85rem;
  border-radius: 2px;
  border: 1px solid var(--signal);
  background: var(--signal);
  color: #fff;
  cursor: pointer;
}
button:hover { background: #0a4a3f; }
button.quiet { background: transparent; color: var(--retract); border-color: var(--line); }
button.quiet:hover { background: #f6eeed; border-color: var(--retract); }

.candidate {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.75rem 1rem;
  align-items: start;
  padding: 0.875rem 1rem;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}
.candidate p { margin: 0; }
.candidate .meta { color: var(--muted); font-size: 0.875rem; }

.hint { color: var(--muted); font-size: 0.875rem; margin: 0 0 0.75rem; }

.earlier { list-style: none; padding: 0; margin: 0.5rem 0 0; }
.earlier li { padding: 0.5rem 0; border-bottom: 1px solid var(--line); font-size: 0.9375rem; }
.earlier a { text-decoration: none; }
.earlier a:hover { text-decoration: underline; }
.earlier .count { color: var(--muted); }

.history { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
.history li {
  padding: 0.875rem 1rem;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  display: grid;
  grid-template-columns: 7rem 1fr;
  gap: 0 1rem;
}
.history .when { font-weight: 600; }
.history .retract-row .when { color: var(--retract); }
.history .mark-row .when { color: var(--signal); }
.history .meta { color: var(--muted); font-size: 0.875rem; margin: 0.15rem 0 0; }
.history .note {
  margin: 0.4rem 0 0;
  border-left: 2px solid var(--line);
  padding-left: 0.75rem;
}

@media (max-width: 34rem) {
  .entry, .history li { grid-template-columns: 1fr; }
  .entry .time, .history .when { padding-left: 1rem; margin-bottom: 0.25rem; }
  .entry > * { padding-left: 1rem; }
  .candidate { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.jinja_loader = DictLoader(
    {"base.html": BASE_HTML, "day.html": DAY_HTML, "article.html": ARTICLE_HTML}
)
init_db()  # safe to call repeatedly; CREATE TABLE IF NOT EXISTS


def parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        abort(404)


def _strip_zero(number):
    """%-d is not portable, so drop the leading zero by hand."""
    return str(int(number))


@app.template_filter("clock")
def clock(iso):
    """05:41 — the time of day is what matters in a day log."""
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").strftime("%H:%M")


@app.template_filter("longdate")
def longdate(d):
    return "%s %s %s" % (d.strftime("%A"), _strip_zero(d.day), d.strftime("%B"))


@app.template_filter("shortdate")
def shortdate(value):
    d = value if isinstance(value, date) else parse_day(value)
    return "%s %s" % (_strip_zero(d.day), d.strftime("%b"))


@app.template_filter("person")
def person(email):
    return email.split("@")[0].replace(".", " ").title()


@app.route("/app.css")
def stylesheet():
    return STYLESHEET, 200, {"Content-Type": "text/css; charset=utf-8"}


@app.route("/")
def home():
    return redirect(url_for("day", day_str=date.today().isoformat()))


@app.route("/day/<day_str>")
def day(day_str):
    day_obj = parse_day(day_str)
    today = date.today()
    with connect() as conn:
        marked = marked_on(conn, day_str)
        retracted = retracted_on(conn, day_str)
        candidates = unmarked_candidates(conn, day_str)
        recent = days_with_marks(conn)
        cached = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    return render_template(
        "day.html",
        day=day_obj,
        day_str=day_str,
        is_today=day_obj == today,
        is_past=day_obj < today,
        marked=marked,
        retracted=retracted,
        candidates=candidates,
        recent=[r for r in recent if r["mark_date"] != day_str],
        prev_day=(day_obj - timedelta(days=1)).isoformat(),
        next_day=(day_obj + timedelta(days=1)).isoformat() if day_obj < today else None,
        drift_threshold=DRIFT_THRESHOLD,
        cached=cached,
        db_path=DB_PATH,
    )


@app.route("/day/<day_str>/mark", methods=["POST"])
def mark(day_str):
    parse_day(day_str)
    note = (request.form.get("note") or "").strip() or None
    with connect() as conn:
        record(conn, request.form["content_id"], day_str, "mark", CURRENT_USER, note)
    return redirect(url_for("day", day_str=day_str))


@app.route("/day/<day_str>/retract", methods=["POST"])
def retract(day_str):
    parse_day(day_str)
    note = (request.form.get("note") or "").strip() or None
    with connect() as conn:
        record(conn, request.form["content_id"], day_str, "retract", CURRENT_USER, note)
    return redirect(url_for("day", day_str=day_str))


@app.route("/article/<content_id>")
def article(content_id):
    with connect() as conn:
        row = get_article(conn, content_id)
        if row is None:
            abort(404)
        history = article_history(conn, content_id)
    return render_template("article.html", article=row, history=history)


@app.route("/api/day/<day_str>")
def api_day(day_str):
    parse_day(day_str)
    with connect() as conn:
        return jsonify(
            {
                "date": day_str,
                "marked": [
                    {
                        "content_id": m["content_id"],
                        "path": m["path"],
                        "headline": m["headline"],
                        "marked_by": m["actor"],
                        "marked_at": m["occurred_at"],
                        "revision_at_mark": m["revision"],
                        "revision_now": m["current_revision"],
                        "note": m["note"],
                    }
                    for m in marked_on(conn, day_str)
                ],
            }
        )


@app.route("/api/article/<content_id>")
def api_article(content_id):
    with connect() as conn:
        if get_article(conn, content_id) is None:
            abort(404)
        return jsonify(
            {"content_id": content_id, "events": [dict(e) for e in article_history(conn, content_id)]}
        )


@app.route("/api/articles", methods=["POST"])
def api_ingest():
    """Feed the cache a Flexible document or a CAPI response."""
    try:
        parsed = from_payload(request.get_json(force=True))
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    with connect() as conn:
        upsert_article(conn, parsed)
    return jsonify(parsed), 201


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

STUBS = [
    ("5f21c9a41a77e30b4c118ad2", "politics/2026/sep/14/budget-briefing-leak",
     "Treasury briefing leak forces rethink on autumn budget", "Politics", 412),
    ("5f21c9a41a77e30b4c118ad3", "world/2026/sep/13/baltic-cable-inquiry",
     "Baltic cable inquiry widens to three more vessels", "World news", 168),
    ("5f21c9a41a77e30b4c118ad4", "environment/2026/sep/12/water-firms-ruling",
     "Water companies lose appeal over storm overflow penalties", "Environment", 93),
    ("5f21c9a41a77e30b4c118ad5", "football/2026/sep/14/transfer-window-review",
     "The transfer window in ten deals nobody saw coming", "Football", 57),
    ("5f21c9a41a77e30b4c118ad6", "business/2026/sep/11/retail-sales-figures",
     "Retail sales fall for a fourth straight month", "Business", 44),
]


def _event(conn, content_id, day_obj, action, actor, at, note=None, revision=None):
    """Seed-only insert so past events can carry a believable timestamp."""
    if revision is None:
        revision = get_article(conn, content_id)["revision"]
    conn.execute(
        """INSERT INTO mark_events (content_id, mark_date, action, actor, occurred_at, revision, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content_id, day_obj.isoformat(), action, actor,
         "%sT%s:00Z" % (day_obj.isoformat(), at), revision, note),
    )


def seed():
    """Build a database with four days of marking history."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()

    today = date.today()
    d1, d2, d3 = today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=3)

    with connect() as conn:
        flexible_path = os.path.join(HERE, "flexibleapi.json")
        if os.path.exists(flexible_path):
            with open(flexible_path) as fh:
                bayeux = from_payload(json.load(fh))
        else:
            print("No flexibleapi.json alongside this file — using a stub.", file=sys.stderr)
            bayeux = {
                "content_id": "6aa2bb838f08f44a1b8d91a8",
                "path": "tv-and-radio/2026/sep/14/tv-tonight-inside-the-bayeux-tapestrys-treacherous-journey-to-london",
                "page_id": "17554801",
                "headline": "TV tonight: inside the Bayeux Tapestry's journey to London",
                "section": "Television & radio",
                "web_url": None,
                "revision": 1423,
                "last_modified": None,
            }
        upsert_article(conn, bayeux)

        for cid, path, headline, section, rev in STUBS:
            upsert_article(conn, {
                "content_id": cid, "path": path, "page_id": None, "headline": headline,
                "section": section, "web_url": "https://www.theguardian.com/" + path,
                "revision": rev, "last_modified": None,
            })

        leak, cable, water, transfers, retail = [s[0] for s in STUBS]

        # Three days back: a straightforward day.
        _event(conn, water, d3, "mark", "claire.burke@guardian.co.uk", "07:12", revision=61)
        _event(conn, retail, d3, "mark", "halima.ali@guardian.co.uk", "09:40", revision=31)

        # Two days back: one call reversed as the story moved on.
        _event(conn, cable, d2, "mark", "claire.burke@guardian.co.uk", "06:55",
               "Leading the world file", revision=120)
        _event(conn, retail, d2, "mark", "halima.ali@guardian.co.uk", "08:02", revision=38)
        _event(conn, retail, d2, "retract", "claire.burke@guardian.co.uk", "14:20",
               "Overtaken by the cable story", revision=41)

        # Yesterday: marked, dropped, then marked again — the case a switch can't hold.
        _event(conn, leak, d1, "mark", "halima.ali@guardian.co.uk", "06:30", revision=380)
        _event(conn, leak, d1, "retract", "halima.ali@guardian.co.uk", "11:05",
               "Treasury pushed back, holding it", revision=391)
        _event(conn, leak, d1, "mark", "claire.burke@guardian.co.uk", "16:48",
               "Second source stands it up", revision=404)
        _event(conn, cable, d1, "mark", "claire.burke@guardian.co.uk", "07:15", revision=151)

        # Today: one clean mark, one that has since been heavily rewritten.
        _event(conn, bayeux["content_id"], today, "mark", "hollie.richardson@guardian.co.uk",
               "05:41", "Pick of the day, leading the G2 front", revision=bayeux["revision"])
        _event(conn, leak, today, "mark", "halima.ali@guardian.co.uk", "06:18",
               "Running as the splash", revision=340)

    # The leak story has been rewritten hard since this morning's call.
    with connect() as conn:
        row = dict(get_article(conn, STUBS[0][0]))
        row["revision"] = 412
        upsert_article(conn, row)

    print("Seeded " + DB_PATH)


if __name__ == "__main__":
    if "--seed" in sys.argv:
        seed()
    else:
        app.run(debug=True, port=5000)
