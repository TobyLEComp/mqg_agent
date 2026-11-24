"""Source & Target (spec 2026-09-19 source-target adapters, §8). Plain
functions over recorded reads (coding_agent.adapters.reads) and the adapter
interface; nothing here computes migration state - the strip reads the GA1
projection. The routes in app.py are thin wrappers. Domain-aware on purpose:
the design rebuild for the unit map lives here, not in generic core.

A connection is found by its `role` and `unit` in connections.toml, never by
its section name, so renaming a connection is configuration only. A source's
unit map (which of its columns the unit uses, and where each goes) is resolved
when it is VIEWED, from the newest read of the unit's target, so a target
refreshed after the source never leaves the source view stale."""
from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from coding_agent.adapters import config, reads, registry
from coding_agent.adapters.base import sanitise
from coding_agent.adapters.unit_map import build_unit_map, source_key

STALE_HOURS = 24
_LOCKS = defaultdict(threading.Lock)

# The read cache. Every endpoint reads the newest recorded read, which against
# Azure Blob is a list() and a get() per request. Two layers: parsed read docs
# by sha256 (content-addressed, so never stale - bounded LRU), and which read
# is newest per connection (a TTL; refresh() drops its connection's entry the
# moment its read is recorded). A failure - no read, a fetch that returned
# nothing - is never stored, so the next request retries. FastAPI runs sync
# handlers in a threadpool: every mutation holds _CACHE_LOCK; fetches do not.
LATEST_TTL_SECONDS = 30
DOC_CACHE_SIZE = 8
_CACHE_LOCK = threading.Lock()
_DOCS: OrderedDict = OrderedDict()      # sha -> parsed read doc (callers must not mutate it)
_LATEST: dict = {}                      # connection -> (looked_up_at, sha, note)
_clock = time.monotonic


def clear_cache() -> None:
    with _CACHE_LOCK:
        _DOCS.clear()
        _LATEST.clear()


def _cached_doc(sha):
    with _CACHE_LOCK:
        doc = _DOCS.get(sha)
        if doc is not None:
            _DOCS.move_to_end(sha)
        return doc


def _remember_doc(sha, doc):
    with _CACHE_LOCK:
        _DOCS[sha] = doc
        _DOCS.move_to_end(sha)
        while len(_DOCS) > DOC_CACHE_SIZE:
            _DOCS.popitem(last=False)


def _load_cached(archive, sha) -> tuple:
    """reads.load_snapshot, served from the doc LRU when it holds `sha`. Only
    an adapter-read/1 doc is stored: a failed fetch or a foreign document is
    looked at again next time."""
    doc = _cached_doc(sha)
    if doc is not None:
        return True, doc
    fetched, doc = reads.load_snapshot(archive, sha)
    if doc is not None:
        _remember_doc(sha, doc)
    return fetched, doc


def _latest_read(archive, name) -> tuple:
    """reads.latest_read through the cache: (doc, note)."""
    with _CACHE_LOCK:
        hit = _LATEST.get(name)
        fresh = hit is not None and _clock() - hit[0] < LATEST_TTL_SECONDS
    if fresh:
        doc = _cached_doc(hit[1])
        if doc is not None:
            return doc, hit[2]
    seen = {}

    def load(arch, sha):
        fetched, doc = _load_cached(arch, sha)
        if doc is not None:
            seen[sha] = doc
        return fetched, doc
    doc, note = reads.latest_read(archive, name, load=load)
    if doc is not None:
        sha = next(s for s, d in seen.items() if d is doc)
        with _CACHE_LOCK:
            _LATEST[name] = (_clock(), sha, note)
    return doc, note


def _forget_latest(name) -> None:
    with _CACHE_LOCK:
        _LATEST.pop(name, None)


def pack_root() -> Path:
    """The domain pack's directory: wherever `macquarie` imports from, the same
    place target_designer.node reads its governed records. agent/macquarie in
    a checkout, /agent/macquarie in the slim backend image, /agent-src/macquarie
    (the editable install) in the combined image - never a guessed path."""
    import macquarie
    return Path(next(iter(macquarie.__path__))).resolve()


def connections_file() -> Path:
    return Path(os.environ.get("CONNECTIONS_CONFIG") or pack_root() / "connections.toml")


def ledger_file() -> Path:
    return Path(os.environ.get("MIGRATION_LEDGER")
                or pack_root() / "migration_portfolios" / "iris.jsonl")


def migration_run_root() -> Path:
    """Where a migration run's evidence is published (WS-G GA17). Cwd-relative
    like `output/` everywhere else, and env-overridable because the backend and
    the worker need not share a working directory."""
    return Path(os.environ.get("MIGRATION_RUN_ROOT") or "output/migration-runs")


def design_promotions_file() -> Path:
    return pack_root() / "target_design" / "promotions.jsonl"


def load() -> tuple:
    try:
        return config.load_connections(connections_file().read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, (f"the connections file cannot be read: {exc}",)


def context(archive) -> dict:
    import tempfile
    return {"archive": archive, "ledger_path": ledger_file(),
            "workdir": Path(tempfile.gettempdir()) / "source-target"}


def _open_adapter(name, entries, environ, ctx):
    return registry.open(name, entries, environ=environ, context=ctx)


def _read_summary(doc, note, now) -> dict | None:
    if doc is None:
        return None
    read_at = datetime.fromisoformat(doc["read_at"])
    return {"read_at": doc["read_at"], "tables": len(doc["tables"]),
            "columns": sum(len(t["columns"]) for t in doc["tables"]),
            "stale": (now - read_at).total_seconds() > STALE_HOURS * 3600,
            "identity": doc.get("identity", {}), "note": note}


def _units(entries) -> list:
    return list(dict.fromkeys(e["unit"] for e in entries.values() if e.get("unit")))


def list_connections(archive, *, environ=os.environ, now=None) -> dict:
    """Every configured connection with its role, unit, describe() and newest
    read; `problems` names every reason the file did not load (the screen
    shows them - an empty rail must never be silent); `units` are the
    migration units the connections serve, in file order."""
    now = now or datetime.now(timezone.utc)
    entries, problems = load()
    out = []
    for name, entry in entries.items():
        adapter = _open_adapter(name, entries, environ, context(archive))
        doc, note = _latest_read(archive, name)
        out.append({"name": name, "role": entry["role"], "unit": entry.get("unit", ""),
                    "describe": adapter.describe(), "read": _read_summary(doc, note, now)})
    return {"connections": out, "problems": list(problems), "units": _units(entries)}


def test(name, archive, *, environ=os.environ) -> dict:
    entries, problems = load()
    if name not in entries:
        return {"state": "not_configured", "reason": "; ".join(problems) or f"no connection {name!r}"}
    status = _open_adapter(name, entries, environ, context(archive)).test_connection()
    return {"state": status.state, "reason": status.reason, "identity": status.identity}


def _live_unit_map(design_builder, approved_reader, unit_map_builder=build_unit_map):
    """(unit map, "") when the design rebuilt from the verified governed
    records matches the promoted graph's fingerprint, else (None, why). A
    failure while rebuilding is a reason, never an exception."""
    try:
        design, approved = design_builder(), approved_reader()
        if design is None or approved is None:
            return None, "the target design or its promoted graph did not verify"
        umap = unit_map_builder(design)
    except Exception as exc:                           # noqa: BLE001 - a named reason, not a 500
        return None, "the target design could not be rebuilt: " + sanitise(
            f"{type(exc).__name__}: {exc}", None)
    if umap["design_fingerprint"] != approved:
        return None, (f"the rebuilt design is {umap['design_fingerprint']}, the promoted graph "
                      f"records {approved}")
    return umap, ""


def _target_unit_map(archive, entries, unit) -> tuple:
    """(unit map, why, found) from the newest read of `unit`'s target
    connection. `found` is False when no target read was obtained, and `why`
    then says why: nothing recorded, or the archive could not be read."""
    notes = []
    for target in (config.by_role(entries, "target", unit) if unit else ()):
        doc, note = _latest_read(archive, target)
        if doc is not None:
            umap = doc.get("unit_map")
            return umap, "" if umap is not None else (
                doc.get("unit_map_unavailable") or f"the newest read of {target} has no unit map"), True
        notes.append(note)
    if not notes:
        return None, "the unit has no target connection configured", False
    return None, "the unit's target has no usable read: " + "; ".join(filter(None, notes)), False


def _view_unit_map(archive, entries, name, doc) -> tuple:
    """(role, unit map, why) for viewing `doc`, the newest read of `name`."""
    entry = entries.get(name) or {}
    role = entry.get("role") or doc.get("role")
    if role == "target":
        umap = doc.get("unit_map")
        return role, umap, "" if umap is not None else doc.get("unit_map_unavailable", "")
    if role != "source":
        return role, None, f"{name} is not a configured connection"
    umap, why, found = _target_unit_map(archive, entries, entry.get("unit"))
    if found:
        return role, umap, why
    # No target read at all: the map recorded with the source read is the only one.
    umap = doc.get("unit_map")
    return role, umap, "" if umap is not None else why


def _used(doc, role, umap) -> set:
    """The (database, schema, name) the unit uses, under the view-time map;
    the flags recorded with the read when there is no map to judge by."""
    if umap is None:
        return {(t["database"], t["schema"], t["name"]) for t in doc["tables"] if t["used_by_unit"]}
    tables = [SimpleNamespace(database=t["database"], schema=t["schema"], name=t["name"])
              for t in doc["tables"]]
    return set(reads.used_tables(tables, umap, role))


def refresh(name, archive, *, environ=os.environ, now=None, design_builder, approved_reader,
            unit_map_builder=build_unit_map, requested_by=None) -> dict:
    now = now or datetime.now(timezone.utc)
    lock = _LOCKS[name]
    if not lock.acquire(blocking=False):
        return {"state": "busy", "reason": f"a refresh of {name} is already running"}
    try:
        entries, problems = load()
        if name not in entries:
            return {"state": "not_configured", "reason": "; ".join(problems) or f"no connection {name!r}"}
        entry = entries[name]
        adapter = _open_adapter(name, entries, environ, context(archive))
        if entry["role"] == "target":
            umap, why = _live_unit_map(design_builder, lambda: approved_reader(entry),
                                       unit_map_builder)
        else:
            umap, why, _ = _target_unit_map(archive, entries, entry.get("unit"))
        result = reads.record_read(name, adapter, archive, now=now, role=entry["role"],
                                   unit_map=umap, requested_by=requested_by)
        if why and result.get("state") == "recorded":
            _annotate(archive, result, why)
        _forget_latest(name)            # the next request sees this read, not the cached one
        return result
    finally:
        lock.release()


def _annotate(archive, result, why):
    """Record why the unit map is missing inside the read itself, as a new
    snapshot superseding the one just written (reads are immutable). A failed
    put is reported like the read's own: never a sha nobody stored."""
    import json
    raw = archive.get(result["sha256"].removeprefix("sha256:"))
    doc = json.loads(raw)
    doc["unit_map_unavailable"] = why
    name = result["name"].replace(".json", "-annotated.json")
    sha = archive.put(name, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())
    if getattr(archive, "last_error", None):
        result.update(state="archive_failed", reason=archive.last_error)
        return
    result.update(name=name, sha256=f"sha256:{sha}")


def _view(name, archive):
    """(doc, role, unit map, why, used set) for viewing the newest read of
    `name`, or ({"state": "no_read", ...},) when there is none."""
    doc, note = _latest_read(archive, name)
    if doc is None:
        return {"state": "no_read", "reason": note}, None, None, None, None
    entries, _ = load()
    role, umap, why = _view_unit_map(archive, entries, name, doc)
    return doc, role, umap, why, _used(doc, role, umap)


def _key(t) -> tuple:
    return t["database"], t["schema"], t["name"]


def _qualified(t) -> str:
    return f"{t['database']}.{t['schema']}.{t['name']}"


def tree(name, archive) -> dict:
    """Databases -> schemas with table and used counts, never the tables
    themselves: a real database has thousands, fetched a page at a time by
    schema_tables()."""
    doc, note = _latest_read(archive, name)
    if doc is None:
        return {"state": "no_read", "reason": note}
    entries, _ = load()
    role, umap, why = _view_unit_map(archive, entries, name, doc)
    used = _used(doc, role, umap)
    dbs: dict = {}
    for t in doc["tables"]:
        counts = dbs.setdefault(t["database"], {}).setdefault(t["schema"], [0, 0])
        counts[0] += 1
        counts[1] += _key(t) in used
    not_built = (umap or {}).get("unprovisionable_tables", []) if role == "target" else []
    return {"read_at": doc["read_at"], "role": role, "not_built": not_built, "note": note,
            "mapping_available": umap is not None, "mapping_reason": why,
            "databases": [{"name": db, "schemas": [{"name": s, "tables": n, "used": u}
                                                  for s, (n, u) in sorted(schemas.items())]}
                          for db, schemas in sorted(dbs.items())]}


def schema_tables(name, qualified_schema, archive, *, offset=0, limit=100, only_used=False,
                  q="") -> dict:
    """One page of a schema's tables, sorted by name. `qualified_schema` is
    `<database>.<schema>`, matched whole, so a dot in either name is safe;
    `q` is a case-insensitive substring of the table name."""
    doc, role, umap, why, used = _view(name, archive)
    if role is None:
        return doc
    in_schema = [t for t in doc["tables"] if f"{t['database']}.{t['schema']}" == qualified_schema]
    if not in_schema:
        return {"state": "not_found", "reason": f"{qualified_schema} is not in the newest read"}
    needle = (q or "").lower()
    rows = sorted((t for t in in_schema if (not only_used or _key(t) in used)
                   and needle in t["name"].lower()), key=lambda t: t["name"])
    offset, limit = max(0, int(offset)), min(500, max(1, int(limit)))
    return {"database": in_schema[0]["database"], "schema": in_schema[0]["schema"],
            "total": len(rows), "offset": offset, "limit": limit,
            "tables": [{"name": t["name"], "type": t["type"], "columns": len(t["columns"]),
                        "row_count": t["row_count"], "used_by_unit": _key(t) in used}
                       for t in rows[offset:offset + limit]]}


NEIGHBOURHOOD_CAP = 40
UNUSED_SCHEMA_SEED = 12


def _fk_links(doc) -> list:
    """The recorded read's foreign keys as links between qualified tables.
    A harvest names each end `schema.table` and stamps the `database` (the
    SQL Server adapter does); without the stamp, the end is looked up by
    `schema.table` alone. An end that is not in the read is dropped."""
    by_db = {(t["database"], f"{t['schema']}.{t['name']}"): _qualified(t) for t in doc["tables"]}
    by_name: dict = {}
    for t in doc["tables"]:
        by_name.setdefault(f"{t['schema']}.{t['name']}", _qualified(t))

    def resolve(fk, end):
        db = fk.get("database")
        return by_db.get((db, fk[end])) if db else by_name.get(fk[end])
    links = []
    for fk in doc.get("foreign_keys", []):
        child, parent = resolve(fk, "from_table"), resolve(fk, "to_table")
        if child and parent:
            links.append({"from_table": child, "from_column": fk["from_column"],
                          "to_table": parent, "to_column": fk["to_column"],
                          "constraint": fk.get("name", "")})
    return links


def neighbourhood(name, qualified, archive) -> dict:
    """The cards the schema canvas starts from. `qualified` names a table
    (`db.schema.table`: it and every table it joins to by a foreign key, in
    either direction) or a schema (`db.schema`: the unit's tables there and
    their neighbours, or the first 12 tables by name when the unit uses none).
    Seed tables first, then neighbours by name; at most 40 cards."""
    doc, role, umap, why, used = _view(name, archive)
    if role is None:
        return doc
    tables = {_qualified(t): t for t in doc["tables"]}
    links = _fk_links(doc)
    if qualified in tables:
        seed, fallback = [qualified], False
    else:
        in_schema = sorted(q for q, t in tables.items()
                           if f"{t['database']}.{t['schema']}" == qualified)
        if not in_schema:
            return {"state": "not_found", "reason": f"{qualified} is not in the newest read"}
        seed = [q for q in in_schema if _key(tables[q]) in used]
        fallback = not seed
        seed = seed or in_schema[:UNUSED_SCHEMA_SEED]
    seeded = set(seed)
    neighbours = [] if fallback else sorted(
        {end for link in links for a, end in ((link["from_table"], link["to_table"]),
                                              (link["to_table"], link["from_table"]))
         if a in seeded and end not in seeded})
    shown = (seed + neighbours)[:NEIGHBOURHOOD_CAP]
    keep = set(shown)
    fk_cols = {(link["from_table"], link["from_column"]) for link in links}
    return {"role": role, "read_at": doc["read_at"],
            "truncated": len(seed) + len(neighbours) > NEIGHBOURHOOD_CAP,
            "total_neighbours": len(neighbours),
            "tables": [{"qualified": q, "database": tables[q]["database"],
                        "schema": tables[q]["schema"], "name": tables[q]["name"],
                        "row_count": tables[q]["row_count"],
                        "used_by_unit": _key(tables[q]) in used,
                        "columns": [{"name": c["name"], "type": c["type"],
                                     "nullable": c["nullable"], "pk": c["pk"],
                                     "fk": (q, c["name"]) in fk_cols}
                                    for c in tables[q]["columns"]]} for q in shown],
            "links": [link for link in links
                      if link["from_table"] in keep and link["to_table"] in keep]}


def _target_origin(umap) -> dict:
    origin: dict = {}
    for skey, targets in (umap or {}).get("goes_to", {}).items():
        for g in targets:
            origin.setdefault(g["target"], []).append({"source": skey, "via": g["via"]})
    return origin


def table(name, qualified, archive) -> dict:
    doc, reason = _latest_read(archive, name)
    if doc is None:
        return {"state": "no_read", "reason": reason}
    # matched whole, like the rail and the canvas: a dot in any part is safe
    t = next((x for x in doc["tables"] if _qualified(x) == qualified
              or (not x["database"] and f"{x['schema']}.{x['name']}" == qualified)), None)
    if t is None:
        return {"state": "not_found", "reason": f"{qualified} is not in the newest read"}
    database, schema, tname = t["database"], t["schema"], t["name"]
    entries, _ = load()
    role, umap, why = _view_unit_map(archive, entries, name, doc)
    origin = _target_origin(umap)
    cols = []
    for c in t["columns"]:
        if role == "source":
            goes = (umap or {}).get("goes_to", {}).get(source_key(database, schema, tname, c["name"]), [])
            cols.append({**c, "goes_to": goes, "used": bool(goes)})
        else:
            came = origin.get(f"{schema}.{tname}.{c['name']}", [])
            cols.append({**c, "from": came, "used": bool(came)})
    return {"qualified": qualified, "role": role, "read_at": doc["read_at"],
            "row_count": t["row_count"], "columns": cols,
            "mapping_available": umap is not None, "mapping_reason": why}


def erd(name, schema, archive) -> dict:
    from coding_agent.sql.erd import erd_mermaid
    doc, reason = _latest_read(archive, name)
    if doc is None:
        return {"state": "no_read", "reason": reason}
    # `schema` is `<database>.<schema>` matched whole (a dot in either name is
    # safe), or a bare schema name in any database.
    picked = [t for t in doc["tables"] if f"{t['database']}.{t['schema']}" == schema] \
        or [t for t in doc["tables"] if t["schema"] == schema]
    tables = [{"schema": t["schema"], "name": t["name"], "type": t["type"],
               "columns": t["columns"]} for t in picked]
    children = {(t["database"], f"{t['schema']}.{t['name']}") for t in picked}
    fks = [fk for fk in doc.get("foreign_keys", [])
           if (fk.get("database"), fk["from_table"]) in children
           or ("database" not in fk and fk["from_table"] in {c for _, c in children})]
    text, counts = erd_mermaid({"tables": tables, "foreign_keys": fks})
    # erd_mermaid returns markdown (heading, ```mermaid fence, footer); the
    # renderer parses only the diagram, and a schema with no tables has none.
    fenced = re.search(r"```mermaid\n(.*?)\n```", text, re.S)
    return {"mermaid": fenced.group(1) if fenced else "", "counts": counts}


def _provisioned_counts(lines, event_id) -> dict:
    """The table and column counts the provisioning run recorded in its
    ledger event - read from the record, never recomputed."""
    event = next((line.event for line in lines if line.event is not None
                  and line.event.event_id == event_id), None)
    return dict((event.payload or {}).get("counts") or {}) if event else {}


def migration(unit, archive) -> dict:
    from coding_agent.migration.ledger import read_events
    from coding_agent.migration.projector import project
    lines = list(read_events(ledger_file()))
    item = project(lines).items.get(unit)
    entries, _ = load()
    sources = config.by_role(entries, "source", unit)
    read_tables, read_names = 0, []
    for name in sources:
        doc, _ = _latest_read(archive, name)
        if doc is not None:
            role, umap, _ = _view_unit_map(archive, entries, name, doc)
            read_tables += len(_used(doc, role, umap))
            read_names.append(name)
    provisioning = item.dimensions["provisioning"] if item else None
    done = bool(provisioning and provisioning.state == "provisioned")
    counts = _provisioned_counts(lines, provisioning.event_id) if done else {}
    if not sources:
        source_figure = "no source connection for this unit"
    elif read_tables:
        source_figure = f"{read_tables} tables read"
    else:
        source_figure = "not read yet" if not read_names else "no unit tables in the read"
    if provisioning is None:
        provisioned_figure = "unit not in the ledger"
    elif done and counts.get("tables") is not None:
        provisioned_figure = f"{provisioning.state} · {counts['tables']} tables"
    else:
        provisioned_figure = provisioning.state
    boxes = [
        {"key": "source", "label": "Source", "state": "done" if read_tables else "pending",
         "figure": source_figure},
        {"key": "provisioned", "label": "Provisioned", "state": "done" if done else "pending",
         "figure": provisioned_figure, "tables": counts.get("tables"),
         "columns": counts.get("columns")},
        {"key": "loaded", "label": "Loaded", "state": "missing", "figure": "—"},
        {"key": "reconciled", "label": "Reconciled", "state": "missing", "figure": "—"},
    ]
    return {"unit": unit, "boxes": boxes,
            "contract": provisioning.detail.get("contract") if provisioning else None}
