"""Fill the prototype with something to look at.

The Bayeux Tapestry article is real — it is read straight out of the uploaded
Flexible payload, so the join keys, headline and revision number are the actual
ones. The other articles are invented stubs, and a few days of marking history
are written so the log has a past worth browsing.
"""

import json
import os
import sys
from datetime import date, timedelta

import store
from ingest import from_payload

UPLOADS = "/mnt/user-data/uploads"

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


def event(conn, content_id, day, action, actor, at, note=None, revision=None):
    """Seed-only insert so past events can carry a believable timestamp."""
    if revision is None:
        revision = store.get_article(conn, content_id)["revision"]
    conn.execute(
        """INSERT INTO mark_events (content_id, mark_date, action, actor, occurred_at, revision, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content_id, day.isoformat(), action, actor, f"{day.isoformat()}T{at}:00Z", revision, note),
    )


def main():
    if os.path.exists(store.DB_PATH):
        os.remove(store.DB_PATH)
    store.init_db()

    today = date.today()
    d1, d2, d3 = today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=3)

    with store.connect() as conn:
        # The real one, read from the Flexible payload.
        flexible_path = os.path.join(UPLOADS, "flexibleapi.json")
        if os.path.exists(flexible_path):
            with open(flexible_path) as fh:
                bayeux = from_payload(json.load(fh))
        else:
            print("Flexible payload not found — using a stub for the Bayeux piece.", file=sys.stderr)
            bayeux = {
                "content_id": "6aa2bb838f08f44a1b8d91a8",
                "path": "tv-and-radio/2026/sep/14/tv-tonight-inside-the-bayeux-tapestrys-treacherous-journey-to-london",
                "page_id": "17554801", "headline": "TV tonight: inside the Bayeux Tapestry's journey to London",
                "section": "Television & radio", "web_url": None, "revision": 1424, "last_modified": None,
            }
        store.upsert_article(conn, bayeux)

        for cid, path, headline, section, rev in STUBS:
            store.upsert_article(conn, {
                "content_id": cid, "path": path, "page_id": None, "headline": headline,
                "section": section, "web_url": f"https://www.theguardian.com/{path}",
                "revision": rev, "last_modified": None,
            })

        leak, cable, water, transfers, retail = [s[0] for s in STUBS]

        # Three days back: a straightforward day.
        event(conn, water, d3, "mark", "claire.burke@guardian.co.uk", "07:12", revision=61)
        event(conn, retail, d3, "mark", "halima.ali@guardian.co.uk", "09:40", revision=31)

        # Two days back: one call reversed as the story moved on.
        event(conn, cable, d2, "mark", "claire.burke@guardian.co.uk", "06:55",
              "Leading the world file", revision=120)
        event(conn, retail, d2, "mark", "halima.ali@guardian.co.uk", "08:02", revision=38)
        event(conn, retail, d2, "retract", "claire.burke@guardian.co.uk", "14:20",
              "Overtaken by the cable story", revision=41)

        # Yesterday: marked, dropped, then marked again — the case a switch can't hold.
        event(conn, leak, d1, "mark", "halima.ali@guardian.co.uk", "06:30", revision=380)
        event(conn, leak, d1, "retract", "halima.ali@guardian.co.uk", "11:05",
              "Treasury pushed back, holding it", revision=391)
        event(conn, leak, d1, "mark", "claire.burke@guardian.co.uk", "16:48",
              "Second source stands it up", revision=404)
        event(conn, cable, d1, "mark", "claire.burke@guardian.co.uk", "07:15", revision=151)

        # Today: one clean mark, one that has since been heavily rewritten.
        event(conn, bayeux["content_id"], today, "mark", "hollie.richardson@guardian.co.uk",
              "05:41", "Pick of the day, leading the G2 front", revision=bayeux["revision"])
        event(conn, leak, today, "mark", "halima.ali@guardian.co.uk", "06:18",
              "Running as the splash", revision=340)

    # The leak story has been rewritten hard since this morning's call.
    with store.connect() as conn:
        row = dict(store.get_article(conn, leak))
        row["revision"] = 412
        store.upsert_article(conn, row)

    print(f"Seeded {store.DB_PATH}")


if __name__ == "__main__":
    main()
