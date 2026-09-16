"""Pull content from the Guardian Content API (CAPI).

This is the missing half of the tool: `ingest.py` can already *parse* a CAPI
response, but something has to *fetch* one first. That is what lives here.

Your key
--------
Set it in the environment before running:

    export GUARDIAN_CAPI_KEY=your-key-here

or, for a throwaway prototype, drop it into the API_KEY fallback below. Get a
key at https://open-platform.theguardian.com/access/. Do not commit a real key.

A note on the internal fields
-----------------------------
The tool is happiest with CAPI's internal fields — `internalComposerCode`
(the Flexible document id we use as content_id), `internalPageCode` and
`internalRevision`. Those are only returned to internal Guardian keys. With an
open-platform key they are absent and `ingest.from_capi` falls back to the
public content id and webTitle, which still works — the revision-drift signal
just won't be populated until a key that carries `internalRevision` is used.

Read-only: this only ever GETs from CAPI. It never writes back to the CMS.
"""

import os

import requests

# --- your key goes here -----------------------------------------------------
# Prefer the environment variable; the empty-string fallback is the "space"
# left for you to paste a key into for a quick local run. Leave it empty and
# the app will tell you the key is missing rather than failing obscurely.
API_KEY = os.environ.get("GUARDIAN_CAPI_KEY", "")
# ----------------------------------------------------------------------------

CAPI_BASE = os.environ.get("GUARDIAN_CAPI_BASE", "https://content.guardianapis.com")

# `all` is the simplest thing to ask for; ingest picks out the handful of
# fields it needs (internalComposerCode / internalPageCode / internalRevision /
# headline / lastModified) and ignores the rest.
SHOW_FIELDS = "all"

TIMEOUT = 10  # seconds


class CapiError(RuntimeError):
    """Anything that stops us getting a usable response back from CAPI."""


def has_key():
    return bool(API_KEY)


def _require_key():
    if not API_KEY:
        raise CapiError(
            "No Guardian CAPI key. Set GUARDIAN_CAPI_KEY in the environment, "
            "or paste one into API_KEY in capi.py. "
            "Get a key at https://open-platform.theguardian.com/access/."
        )


def _get(path, params):
    _require_key()
    url = f"{CAPI_BASE}/{path.lstrip('/')}"
    params = {**params, "api-key": API_KEY, "format": "json"}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CapiError(f"Could not reach CAPI: {exc}") from exc

    if resp.status_code in (401, 403):
        raise CapiError("CAPI rejected the key (HTTP %s). Check GUARDIAN_CAPI_KEY." % resp.status_code)
    if resp.status_code == 429:
        raise CapiError("CAPI rate limit reached (HTTP 429). Try again shortly.")
    if not resp.ok:
        raise CapiError(f"CAPI returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise CapiError("CAPI returned something that wasn't JSON.") from exc


def search(query, page_size=20, section=None, order_by="newest"):
    """Search CAPI and return the raw content items (response.results).

    Each item is shaped like CAPI's `content` object, so wrap it as
    {"response": {"content": item}} before handing it to ingest.from_payload
    (see `as_payload` below).
    """
    params = {
        "q": query,
        "page-size": page_size,
        "show-fields": SHOW_FIELDS,
        "order-by": order_by,
    }
    if section:
        params["section"] = section
    payload = _get("search", params)
    return payload.get("response", {}).get("results", [])


def get_item(item_id):
    """Fetch one content item by its CAPI id / path.

    Returns the full payload ({"response": {"content": {...}}}), ready to pass
    straight to ingest.from_payload.
    """
    return _get(item_id, {"show-fields": SHOW_FIELDS})


def as_payload(item):
    """Wrap a single search result so ingest.from_payload recognises it."""
    return {"response": {"content": item}}
