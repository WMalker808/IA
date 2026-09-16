"""Read the bits of a CMS payload this tool needs.

Both APIs are accepted because both carry stable join keys:

  Flexible   data.id                              -> content_id
             data.identifiers.path                -> path
             data.contentChangeDetails.revision   -> revision

  CAPI       fields.internalComposerCode          -> content_id  (same value)
             content.id                           -> path        (same value)
             fields.internalRevision              -> revision    (same value)

Flexible is the better source: it has a revision for unpublished pieces too,
and it is the system an editor is actually looking at. CAPI is supported so the
prototype can be fed from whichever payload is to hand.

Nothing in here writes back. This tool only ever reads the CMS.
"""


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
        "web_url": f"https://www.theguardian.com/{path}" if path else None,
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
