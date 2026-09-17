"""Daily importance tracker — prototype.

Marks live here, not in the CMS. Writing to the article document in Flexible
would bump its revision and put a curation click on the content update stream,
which would make an editorial judgement look like an edit — and would spoil the
revision number we rely on to spot rewrites.

Run:  python app.py       (seed first with:  python seed.py)
"""

from collections import Counter
from datetime import date, datetime, timedelta

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import capi
import store
from ingest import from_payload

app = Flask(__name__)
store.init_db()  # safe to call repeatedly; CREATE TABLE IF NOT EXISTS

# Stand-in for real auth. In the newsroom this would be the signed-in editor.
CURRENT_USER = "hollie.richardson@guardian.co.uk"

# A piece rewritten by this many revisions since marking gets flagged for review.
DRIFT_THRESHOLD = 25

# Guardian pillars / sections offered in the CAPI search dropdown. The value is
# the CAPI section id passed as ?section=…; the empty value searches everything.
PILLARS = [
    ("", "All sections"),
    ("news", "News"),
    ("politics", "Politics"),
    ("world", "World"),
    ("environment", "Environment"),
    ("business", "Business"),
    ("technology", "Technology"),
    ("science", "Science"),
    ("society", "Society"),
    ("sport", "Sport"),
    ("football", "Football"),
    ("culture", "Culture"),
    ("lifestyle", "Lifestyle"),
    ("commentisfree", "Opinion"),
]
PILLAR_VALUES = {value for value, _ in PILLARS}

# The Guardian's five pillars, used to colour-code section chips. Only the
# section *name* is stored, so the pillar is derived from it here; anything
# unrecognised is treated as News, which is where most sections live.
PILLAR_OF_SECTION = {
    "opinion": "opinion", "comment is free": "opinion",
    "sport": "sport", "football": "sport", "cricket": "sport", "rugby": "sport",
    "culture": "culture", "film": "culture", "music": "culture", "books": "culture",
    "television & radio": "culture", "art and design": "culture", "stage": "culture",
    "games": "culture",
    "lifestyle": "lifestyle", "food": "lifestyle", "fashion": "lifestyle",
    "travel": "lifestyle", "life and style": "lifestyle", "health & wellbeing": "lifestyle",
}
PILLAR_LABEL = {"news": "News", "opinion": "Opinion", "sport": "Sport",
                "culture": "Culture", "lifestyle": "Lifestyle"}

# Past this many marks the day view switches to compact rows by default.
COMPACT_ABOVE = 8


def pillar_of(section):
    pid = PILLAR_OF_SECTION.get((section or "").strip().lower(), "news")
    return {"id": pid, "label": PILLAR_LABEL[pid]}


app.jinja_env.globals["pillar_of"] = pillar_of


@app.template_global()
def daylink(day_str, **overrides):
    """A link to the day page that keeps the current filters unless overridden.

    Pass a value of None/"" to drop a parameter. Transient banner args (q,
    pulled, capi_section, capi_error) are deliberately not carried over.
    """
    args = {k: v for k, v in request.args.items() if k in ("section", "attention", "view")}
    for k, v in overrides.items():
        if v in (None, "", False):
            args.pop(k, None)
        else:
            args[k] = v
    return url_for("day", day_str=day_str, **args)


def parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        abort(404)


@app.template_filter("clock")
def clock(iso):
    """05:41 — the time of day is what matters in a day log."""
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").strftime("%H:%M")


@app.template_filter("longdate")
def longdate(d):
    return d.strftime("%A %-d %B")


@app.template_filter("shortdate")
def shortdate(value):
    d = value if isinstance(value, date) else parse_day(value)
    return d.strftime("%-d %b")


@app.template_filter("person")
def person(email):
    name = email.split("@")[0].replace(".", " ")
    return name.title()


@app.route("/")
def home():
    return redirect(url_for("day", day_str=date.today().isoformat()))


@app.route("/day/<day_str>")
def day(day_str):
    day_obj = parse_day(day_str)
    today = date.today()
    with store.connect() as conn:
        marked_all = store.marked_on(conn, day_str)
        retracted = store.retracted_on(conn, day_str)
        candidates_all = [dict(c) for c in store.unmarked_candidates(conn, day_str)]
        recent = store.days_with_marks(conn)
        cached = store.article_count(conn)

    for m in marked_all:
        m["drifted"] = m["revision_drift"] > DRIFT_THRESHOLD
    drift_count = sum(1 for m in marked_all if m["drifted"])

    # The shape of the day: sections with marks, heaviest first.
    counts = Counter(m["section"] or "Uncategorised" for m in marked_all)
    section_counts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # Filters live in the URL so they survive a reload and can be shared.
    section_filter = request.args.get("section", "")
    attention = request.args.get("attention") == "1"
    view = request.args.get("view", "")

    marked = [
        m for m in marked_all
        if (not section_filter or (m["section"] or "Uncategorised") == section_filter)
        and (not attention or m["drifted"])
    ]
    candidates = [
        c for c in candidates_all
        if not section_filter or (c["section"] or "Uncategorised") == section_filter
    ]

    # Compact rows once the day is heavy, unless the editor asked otherwise.
    if view == "cards":
        compact = False
    elif view == "compact":
        compact = True
    else:
        compact = len(marked_all) > COMPACT_ABOVE
    candidates_compact = compact or len(candidates_all) > COMPACT_ABOVE

    capi_section = request.args.get("capi_section", "")
    pulled_section = dict(PILLARS).get(capi_section) if capi_section else None

    return render_template(
        "day.html",
        day=day_obj,
        day_str=day_str,
        is_today=day_obj == today,
        is_past=day_obj < today,
        marked=marked,
        marked_total=len(marked_all),
        retracted=retracted,
        candidates=candidates,
        to_mark=len(candidates_all),
        drift_count=drift_count,
        section_counts=section_counts,
        section_filter=section_filter,
        attention=attention,
        view=view,
        compact=compact,
        compact_above=COMPACT_ABOVE,
        candidates_compact=candidates_compact,
        filtered=bool(section_filter or attention),
        recent=[r for r in recent if r["mark_date"] != day_str],
        prev_day=(day_obj - timedelta(days=1)).isoformat(),
        next_day=(day_obj + timedelta(days=1)).isoformat() if day_obj < today else None,
        drift_threshold=DRIFT_THRESHOLD,
        cached=cached,
        db_path=store.DB_PATH,
        capi_ready=capi.has_key(),
        capi_query=request.args.get("q", ""),
        capi_error=request.args.get("capi_error"),
        pulled=request.args.get("pulled", type=int),
        pulled_section=pulled_section,
        pillars=PILLARS,
        capi_section=capi_section,
    )


@app.route("/day/<day_str>/pull", methods=["POST"])
def pull(day_str):
    """Search the Guardian Content API and cache the results as candidates.

    This is how today's real articles get in front of an editor. It only reads
    from CAPI; the results become rows in the local `articles` cache, ready to
    be marked. It never writes back to the CMS.
    """
    parse_day(day_str)
    query = (request.form.get("q") or "").strip()
    section = request.form.get("section") or ""
    if section not in PILLAR_VALUES:  # ignore anything not from our own dropdown
        section = ""
    if not query and not section:
        # Need at least a search term or a section to pull anything.
        return redirect(url_for("day", day_str=day_str))
    try:
        # With a section and no query, CAPI returns that section's latest.
        results = capi.search(query, section=section or None)
    except capi.CapiError as exc:
        return redirect(url_for("day", day_str=day_str, q=query, capi_section=section or None,
                                capi_error=str(exc), _anchor="pull"))

    added = 0
    with store.connect() as conn:
        for item in results:
            try:
                article = from_payload(capi.as_payload(item))
            except (ValueError, KeyError):
                continue  # skip anything CAPI hands back that we can't read
            store.upsert_article(conn, article)
            added += 1
    return redirect(url_for("day", day_str=day_str, q=query, capi_section=section or None,
                            pulled=added, _anchor="pull"))


@app.route("/day/<day_str>/mark", methods=["POST"])
def mark(day_str):
    parse_day(day_str)
    content_id = request.form["content_id"]
    note = (request.form.get("note") or "").strip() or None
    with store.connect() as conn:
        store.record(conn, content_id, day_str, "mark", CURRENT_USER, note)
    return redirect(url_for("day", day_str=day_str))


@app.route("/day/<day_str>/retract", methods=["POST"])
def retract(day_str):
    parse_day(day_str)
    content_id = request.form["content_id"]
    note = (request.form.get("note") or "").strip() or None
    with store.connect() as conn:
        store.record(conn, content_id, day_str, "retract", CURRENT_USER, note)
    return redirect(url_for("day", day_str=day_str))


@app.route("/article/<content_id>")
def article(content_id):
    with store.connect() as conn:
        row = store.get_article(conn, content_id)
        if row is None:
            abort(404)
        history = store.article_history(conn, content_id)
    # The newest event decides the piece's current status; history is newest-first.
    current = history[0] if history else None
    return render_template(
        "article.html",
        article=row,
        history=history,
        current_action=current["action"] if current else None,
        current_date=current["mark_date"] if current else None,
    )


# --- read API ---------------------------------------------------------------

@app.route("/api/day/<day_str>")
def api_day(day_str):
    parse_day(day_str)
    with store.connect() as conn:
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
                    for m in store.marked_on(conn, day_str)
                ],
            }
        )


@app.route("/api/article/<content_id>")
def api_article(content_id):
    with store.connect() as conn:
        if store.get_article(conn, content_id) is None:
            abort(404)
        return jsonify(
            {
                "content_id": content_id,
                "events": [dict(e) for e in store.article_history(conn, content_id)],
            }
        )


@app.route("/api/articles", methods=["POST"])
def api_ingest():
    """Feed the cache a Flexible document or a CAPI response."""
    try:
        article = from_payload(request.get_json(force=True))
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    with store.connect() as conn:
        store.upsert_article(conn, article)
    return jsonify(article), 201


if __name__ == "__main__":
    app.run(debug=True, port=5000)
