# Day log — daily importance tracker

A prototype for marking articles as important on a given day, keeping the
history, and staying out of the CMS.

```bash
pip install flask
python seed.py     # builds importance.db, reads the real article from the uploaded Flexible payload
python app.py      # http://localhost:5000
```

## The shape of the data

One table does the work:

```
mark_events
  content_id   Flexible document id, e.g. 6aa2bb838f08f44a1b8d91a8
  mark_date    the day the article matters for, YYYY-MM-DD
  action       'mark' or 'retract'
  actor        who decided
  occurred_at  when they decided
  revision     the article's revision at that moment
  note         optional reason
```

It is append-only. Retracting writes a row rather than clearing one, and the
current state of any day is the latest event per `(content_id, mark_date)`.

That is the whole argument for this design. A boolean field on the article can
say "important, yes or no" but it cannot say that the budget leak was marked at
06:30, dropped at 11:05 when the Treasury pushed back, and marked again at 16:48
once a second source stood it up. The seed data contains exactly that sequence,
on 13 September, so you can see what a switch would have thrown away.

`articles` is a second table holding a cached copy of headline, section and
revision. It is not a source of truth. Nothing in this app writes to the CMS.

## Why marks live here and not in Flexible

`toolSettings` in the Flexible document would have worked mechanically —
arbitrary keys, per-document PATCH, invisible to CAPI. It was rejected for three
reasons:

1. **Write churn.** Writing to the article document is an edit. It moves
   `lastModified` and can put a curation click on the content update stream,
   where it looks like a journalist changed the piece.
2. **It would spoil its own safeguard.** We snapshot `contentChangeDetails.revision`
   when a mark is made, so a large gap between that and the current revision
   means the article has been rewritten since the call. If our writes bumped the
   revision, we would be corrupting the signal we depend on.
3. **Flat strings, one per key.** No room for an actor, a timestamp or a second
   marking on a different day.

The join back to the CMS is `content_id` plus `identifiers.path`, both stable.

## What the drift warning does

Mark an article at revision 340, come back at teatime and find it at 412, and
the board says so: *"Rewritten since marking — 72 revisions on. Worth a second
look."* Nobody has to remember to check. The threshold is `DRIFT_THRESHOLD` in
`app.py` and is a guess — it wants calibrating against real revision rates
before it means anything.

## Reading it from other tools

```
GET  /api/day/2026-09-14      marks for a day, with revision at mark and now
GET  /api/article/<id>        every event for one article, newest first
POST /api/articles            feed the cache a Flexible document or CAPI response
```

`POST /api/articles` accepts either payload and detects which is which. Both
resolve to the same `content_id`, since CAPI's `internalComposerCode` is the
Flexible document id.

## What a real version needs

- **Auth.** `CURRENT_USER` in `app.py` is hardcoded. The actor field is the
  point of the design, so this is the first thing to replace.
- **A real article source.** The cache is seeded from files. It wants a feed or
  a search against Flexible so editors can find today's pieces.
- **Postgres.** SQLite is fine for one desk and not for concurrent editors.
- **A retention decision.** The log grows forever by design. Somebody should
  decide how long "forever" is.
- **Timezone care.** `mark_date` is a newsroom day, not a UTC day. The Flexible
  document carries `timeZone: Europe/London`; an editor marking something at
  00:30 BST means that day, not the previous UTC one.
