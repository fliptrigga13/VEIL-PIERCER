"""
nexus_notion_bridge.py — Single-responsibility Notion sync module for NEXUS ULTRA.

All Notion writes for the swarm flow through this file.
Never silently swallows errors — callers decide how to handle failures.

Public API:
    push_entry(entry)          -> str  # auto-dispatch by type, returns page_id
    push_cycle(entry)          -> str  # Type 1: regular swarm cycles
    push_intel(entry)          -> str  # Type 2: INTEL_SEEKER step entries
    push_system(entry)         -> str  # Type 3: system events
    upsert_by_raw_id(id, props) -> str  # query-first upsert, used by all above
"""

import json
import logging
import re
import time
from pathlib import Path

import requests

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("NOTION_BRIDGE")

# ── Config — manual .env parser (no python-dotenv) ───────────────────────────
_env: dict = {}
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _env[_k.strip()] = _v.strip().strip('"')

TOKEN = _env.get("NOTION_TOKEN", "")
DB_ID = _env.get("NOTION_DATABASE_ID", "1d7f17fe54c6820b91ba0158dd5fdea3")
BASE  = "https://api.notion.com/v1"

# ── Shared session — headers set once, reused for all requests ────────────────
_session = requests.Session()
_session.headers.update({
    "Authorization":  f"Bearer {TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28",
})


# ── Notion payload helpers ────────────────────────────────────────────────────
def _rt(text: str) -> list:
    """Rich text block. Required format for all rich_text properties."""
    return [{"type": "text", "text": {"content": str(text or "")[:2000]}}]


def _title(text: str) -> dict:
    """Title property. Required format — missing 'type':'text' causes blank titles."""
    return {"title": [{"type": "text", "text": {"content": str(text or "")[:2000]}}]}


# ── Core upsert ───────────────────────────────────────────────────────────────
def upsert_by_raw_id(cycle_id: str, properties: dict) -> str:
    """
    Query Notion DB for a page with Raw ID == cycle_id.
    PATCH it if found (idempotent), CREATE it if not.
    Returns page_id. Raises RuntimeError on Notion API failures.

    Rate-limited: sleeps 0.35s after every write to avoid 429s on batch syncs.
    """
    r = _session.post(
        f"{BASE}/databases/{DB_ID}/query",
        json={
            "filter": {"property": "Raw ID", "rich_text": {"equals": cycle_id}},
            "page_size": 1,
        },
    )
    if not r.ok:
        raise RuntimeError(f"Notion query failed [{r.status_code}]: {r.text}")

    results = r.json().get("results", [])

    if results:
        page_id = results[0]["id"]
        rp = _session.patch(f"{BASE}/pages/{page_id}", json={"properties": properties})
        if not rp.ok:
            raise RuntimeError(f"Notion PATCH failed [{rp.status_code}]: {rp.text}")
        log.info(f"UPDATED | {cycle_id[:40]} | page_id={page_id}")
    else:
        rc = _session.post(
            f"{BASE}/pages",
            json={"parent": {"database_id": DB_ID}, "properties": properties},
        )
        if not rc.ok:
            raise RuntimeError(f"Notion CREATE failed [{rc.status_code}]: {rc.text}")
        page_id = rc.json()["id"]
        log.info(f"CREATED | {cycle_id[:40]} | page_id={page_id}")

    time.sleep(0.35)
    return page_id


# ── Type 1: Regular swarm cycles ─────────────────────────────────────────────
def push_cycle(entry: dict) -> str:
    """Handle Type 1 feed entries — regular nexus_swarm_loop.py cycles."""
    cycle_id = entry.get("cycle") or ""
    score    = entry.get("score") or 0.0
    mvp      = entry.get("mvp") or "?"
    lesson   = entry.get("lesson") or ""
    ts       = entry.get("ts") or ""
    scout    = entry.get("scout_ctx") or entry.get("scout") or ""

    # Title: CYCLE | 68% | cycle_1774811038 | MVP: EXECUTIONER
    title = f"CYCLE | {int(float(score) * 100)}% | {cycle_id} | MVP: {mvp}"

    # Parse top signal from [PLATFORM: ...] [CONTEXT: ...]
    top_signal = ""
    pm = re.search(r'\[PLATFORM:\s*([^\]]+)\]', scout)
    cm = re.search(r'\[CONTEXT:\s*([^\]]+)\]', scout)
    if pm:
        top_signal = pm.group(1).strip()
        if cm:
            top_signal += f" — {cm.group(1).strip()[:120]}"

    # Parse per-agent scores from [AGENT_SCORES: COMMANDER=0.8 RESEARCHER=0.7 ...]
    agent_props: dict = {}
    am = re.search(r'\[AGENT_SCORES:\s*([^\]]+)\]', lesson)
    if am:
        for pair in am.group(1).split():
            if "=" in pair:
                name, val = pair.split("=", 1)
                try:
                    agent_props[name.strip()] = {"number": float(val.strip())}
                except ValueError:
                    pass

    properties: dict = {
        "Cycle ID":      _title(title),
        "Raw ID":        {"rich_text": _rt(cycle_id)},
        "Score":         {"number": round(float(score), 4)},
        "MVP Agent":     {"select": {"name": str(mvp)[:100]}},
        "Type":          {"select": {"name": "CYCLE"}},
        "Lesson":        {"rich_text": _rt(lesson)},
        "Scout Context": {"rich_text": _rt(scout)},
        "Top Signal":    {"rich_text": _rt(top_signal)},
    }
    if ts:
        properties["Cycle Date"] = {"date": {"start": ts}}
    properties.update(agent_props)

    return upsert_by_raw_id(cycle_id, properties)


# ── Type 2: INTEL_SEEKER step entries ────────────────────────────────────────
def push_intel(entry: dict) -> str:
    """Handle Type 2 feed entries — nexus_intel_seeker.py phase steps."""
    cycle_id = entry.get("cycle") or ""
    phase    = (entry.get("phase") or "?").upper()
    ts       = entry.get("ts") or ""
    lesson   = entry.get("lesson") or ""
    scout    = entry.get("scout") or entry.get("scout_ctx") or ""

    # Extract topic from scout JSON payload
    topic = "?"
    if scout:
        try:
            scout_data = json.loads(scout) if isinstance(scout, str) else scout
            topic = str(scout_data.get("topic", "?")).upper()
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    # Title: INTEL | LEARNED | LANGCHAIN — Distilled 2 facts.
    summary = (lesson or "")[:80].strip()
    title = f"INTEL | {phase} | {topic} — {summary}"

    properties: dict = {
        "Cycle ID":      _title(title),
        "Raw ID":        {"rich_text": _rt(cycle_id)},
        "MVP Agent":     {"select": {"name": "INTEL_SEEKER"}},
        "Type":          {"select": {"name": f"INTEL:{phase}"}},
        "Lesson":        {"rich_text": _rt(lesson)},
        "Scout Context": {"rich_text": _rt(str(scout)[:2000])},
    }
    if ts:
        properties["Cycle Date"] = {"date": {"start": ts}}

    return upsert_by_raw_id(cycle_id, properties)


# ── Type 3: System events ─────────────────────────────────────────────────────
def push_system(entry: dict) -> str:
    """Handle Type 3 feed entries — system events, FIX_* entries."""
    cycle_id = entry.get("cycle") or ""
    lesson   = entry.get("lesson") or entry.get("body") or cycle_id
    ts       = entry.get("ts") or ""

    properties: dict = {
        "Cycle ID": _title(cycle_id),
        "Raw ID":   {"rich_text": _rt(cycle_id)},
        "Type":     {"select": {"name": "SYSTEM:FIX"}},
        "Lesson":   {"rich_text": _rt(lesson)},
    }
    if ts:
        properties["Cycle Date"] = {"date": {"start": ts}}

    return upsert_by_raw_id(cycle_id, properties)


# ── Auto-dispatch entrypoint ──────────────────────────────────────────────────
def push_entry(entry: dict) -> str:
    """
    Main entrypoint. Auto-detects entry type and dispatches to the correct handler.
    Returns page_id. Raises on failure — do not catch here, let callers decide.

    Detection rules:
        INTEL  — entry["type"] == "INTEL" or entry["phase"] is set
        SYSTEM — entry["type"] starts with "SYSTEM"
        CYCLE  — everything else
    """
    is_intel  = entry.get("type") == "INTEL" or bool(entry.get("phase"))
    is_system = str(entry.get("type", "")).startswith("SYSTEM")

    if is_intel:
        return push_intel(entry)
    elif is_system:
        return push_system(entry)
    else:
        return push_cycle(entry)
