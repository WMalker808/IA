"""Daily importance tracker — prototype.

Marks live here, not in the CMS. Writing to the article document in Flexible
would bump its revision and put a curation click on the content update stream,
which would make an editorial judgement look like an edit — and would spoil the
revision number we rely on to spot rewrites.

Run:  python app.py       (seed first with:  python seed.py)
"""

from datetime import date, datetime, timedelta

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import store
from ingest import from_payload

app = Flask(__name__)
store.init_db()  # safe to call repeatedly; CREATE TABLE IF NOT EXISTS

# Stand-in for real auth. In the newsroom this would be the signed-in editor.
CURRENT_USER = "hollie.richardson@guardian.co.uk"

# A piece rewritten by this many revisions since marking gets flagged for review.
DRIFT_THRESHOLD = 25


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
        marked = store.marked_on(conn, day_str)
        retracted = store.retracted_on(conn, day_str)
        candidates = store.unmarked_candidates(conn, day_str)
        recent = store.days_with_marks(conn)
        cached = store.article_count(conn)
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
        db_path=store.DB_PATH,
    )


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
    return render_template("article.html", article=row, history=history)


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
