"""HITL operational store on Databricks Lakebase (project-based, Neon-derived Postgres).

Free Edition allows one Lakebase project with scale-to-zero compute; the autopilot
uses it as the human-in-the-loop review queue and healing audit store:

  hitl_queue      — drift proposals awaiting a human approve/reject decision
  healing_history — applied/rolled-back healing actions (mirrors healing.HealingRecord)

Provisioning goes through the project-based Postgres REST surface:
  POST /api/2.0/postgres/projects?project_id={id}     create (already-exists -> GET)
  GET  /api/2.0/postgres/projects/{id}                status incl. branch/endpoint hosts
  GET  /api/2.0/postgres/projects/{id}/branches/{b}/endpoints   endpoint discovery

Credential minting prefers the typed SDK method
  w.postgres.generate_database_credential(endpoint="projects/{p}/branches/{b}/endpoints/{e}")
and falls back to raw REST (POST .../branches/{b}/credentials) because the SDK
surface varies by version. Tokens are OAuth, expiring after 1 hour — enforced at
login only: an established psycopg connection outlives its token, but any
reconnect needs a freshly minted one.

All data-plane helpers take an injected psycopg connection (tests pass a fake) and
use parameterized queries throughout.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_PROJECT_ID = "genie-autopilot"
DEFAULT_BRANCH = "production"
DEFAULT_DBNAME = "databricks_postgres"


# -- provisioning / connection ------------------------------------------------


def _find_first(obj: Any, key: str) -> Any:
    """Depth-first search for the first truthy value of `key` in nested dicts/lists."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for value in obj.values():
            found = _find_first(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


def _is_already_exists(exc: Exception) -> bool:
    text = str(exc).lower()
    code = str(getattr(exc, "error_code", "") or "").lower()
    return (
        "already_exists" in code
        or "resource_conflict" in code
        or "already exists" in text
        or "conflict" in text
        or "409" in text
    )


def ensure_project(
    w: Any,
    project_id: str = DEFAULT_PROJECT_ID,
    display_name: str = "Genie Autopilot HITL",
    pg_version: int = 17,
) -> dict:
    """Create the Lakebase project if missing; return its created or existing state."""
    try:
        return (
            w.api_client.do(
                "POST",
                f"/api/2.0/postgres/projects?project_id={project_id}",
                body={"spec": {"display_name": display_name, "pg_version": pg_version}},
            )
            or {}
        )
    except Exception as exc:  # SDK error classes vary by version; match on content
        if not _is_already_exists(exc):
            raise
        return w.api_client.do("GET", f"/api/2.0/postgres/projects/{project_id}") or {}


def get_credential(w: Any, project_id: str, branch: str = DEFAULT_BRANCH) -> tuple[str, str]:
    """Return (endpoint_host, oauth_token) for the branch's compute endpoint.

    Discovery: recent API versions embed branch/endpoint state (host, endpoint id)
    in the project GET response; older ones require listing the branch's endpoints.
    Both are tried. The returned token expires in 1 hour (enforced at login only).
    """
    project = w.api_client.do("GET", f"/api/2.0/postgres/projects/{project_id}") or {}
    host = _find_first(project, "host")
    endpoint_id = _find_first(project, "endpoint_id")
    if not host or not endpoint_id:
        listing = (
            w.api_client.do(
                "GET", f"/api/2.0/postgres/projects/{project_id}/branches/{branch}/endpoints"
            )
            or {}
        )
        endpoints = listing.get("endpoints") or []
        if endpoints:
            ep = endpoints[0]
            host = host or ep.get("host")
            endpoint_id = endpoint_id or ep.get("id") or ep.get("endpoint_id")
            name = ep.get("name", "")
            if not endpoint_id and "/endpoints/" in name:
                endpoint_id = name.rsplit("/", 1)[-1]
    if not host or not endpoint_id:
        raise RuntimeError(f"no endpoint found for Lakebase project {project_id!r}/{branch!r}")

    endpoint_name = f"projects/{project_id}/branches/{branch}/endpoints/{endpoint_id}"
    return str(host), _generate_token(w, project_id, branch, endpoint_name)


def _generate_token(w: Any, project_id: str, branch: str, endpoint_name: str) -> str:
    """Mint a 1h OAuth token: typed SDK method first, raw REST fallback."""
    postgres_api = getattr(w, "postgres", None)
    generate = getattr(postgres_api, "generate_database_credential", None)
    if callable(generate):
        cred = generate(endpoint=endpoint_name)
        token = getattr(cred, "token", None)
        if token is None and isinstance(cred, dict):
            token = cred.get("token")
        if token:
            return str(token)
    resp = (
        w.api_client.do(
            "POST",
            f"/api/2.0/postgres/projects/{project_id}/branches/{branch}/credentials",
            body={"endpoint": endpoint_name},
        )
        or {}
    )
    token = resp.get("token") or _find_first(resp, "token")
    if not token:
        raise RuntimeError("Lakebase credential response contained no token")
    return str(token)


def connect(host: str, token: str, user_email: str, dbname: str = DEFAULT_DBNAME) -> Any:
    """psycopg connection to the endpoint; the OAuth token is the password, TLS required."""
    import psycopg  # lazy import keeps this module pure-python for tests

    return psycopg.connect(
        host=host, user=user_email, password=token, dbname=dbname, sslmode="require"
    )


# -- schema --------------------------------------------------------------------

HITL_DDL = """
CREATE TABLE IF NOT EXISTS hitl_queue (
    id serial PRIMARY KEY,
    proposal_key text,
    term text,
    entity text,
    confidence double precision,
    distinct_users int,
    kind text,
    status text DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at timestamptz DEFAULT now(),
    decided_at timestamptz,
    decided_by text,
    evidence jsonb
);

CREATE TABLE IF NOT EXISTS healing_history (
    id serial PRIMARY KEY,
    ts timestamptz DEFAULT now(),
    action text,
    target text,
    proposal_key text,
    payload text,
    status text,
    approver text
);
"""


def ensure_schema(conn: Any) -> None:
    """Create the HITL tables if absent (idempotent)."""
    with conn.cursor() as cur:
        for statement in HITL_DDL.split(";"):
            if statement.strip():
                cur.execute(statement)
    conn.commit()


# -- queue operations ------------------------------------------------------------

_ENQUEUE_SQL = (
    "INSERT INTO hitl_queue "
    "(proposal_key, term, entity, confidence, distinct_users, kind, evidence) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id"
)

_PENDING_COLUMNS = (
    "id",
    "proposal_key",
    "term",
    "entity",
    "confidence",
    "distinct_users",
    "kind",
    "status",
    "created_at",
    "evidence",
)

_PENDING_SQL = (
    "SELECT id, proposal_key, term, entity, confidence, distinct_users, kind, status, "
    "created_at, evidence FROM hitl_queue WHERE status = %s "
    "ORDER BY confidence DESC, created_at"
)

_DECIDE_SQL = (
    "UPDATE hitl_queue SET status = %s, decided_at = now(), decided_by = %s "
    "WHERE id = %s AND status = 'pending'"
)

_RECORD_HEALING_SQL = (
    "INSERT INTO healing_history (ts, action, target, proposal_key, payload, status, approver) "
    "VALUES (COALESCE(to_timestamp(%s), now()), %s, %s, %s, %s, %s, %s) RETURNING id"
)


def enqueue(conn: Any, proposal: dict) -> int:
    """Insert a pending proposal (dict from drift.Proposal); return the queue row id."""
    with conn.cursor() as cur:
        cur.execute(
            _ENQUEUE_SQL,
            (
                proposal.get("proposal_key") or proposal.get("key"),
                proposal.get("term"),
                proposal.get("entity"),
                proposal.get("confidence"),
                proposal.get("distinct_users"),
                proposal.get("kind", "synonym"),
                json.dumps(proposal.get("evidence") or []),
            ),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return int(row_id)


def pending(conn: Any) -> list[dict]:
    """All proposals still awaiting a decision, highest confidence first."""
    with conn.cursor() as cur:
        cur.execute(_PENDING_SQL, ("pending",))
        rows = cur.fetchall()
    return [dict(zip(_PENDING_COLUMNS, row, strict=True)) for row in rows]


def decide(conn: Any, queue_id: int, approved: bool, decided_by: str) -> str:
    """Record a human decision on one queue row; returns the status written."""
    status = "approved" if approved else "rejected"
    with conn.cursor() as cur:
        cur.execute(_DECIDE_SQL, (status, decided_by, queue_id))
    conn.commit()
    return status


def record_healing(conn: Any, record: dict) -> int:
    """Append one healing action (dict from healing.HealingRecord); return the row id."""
    with conn.cursor() as cur:
        cur.execute(
            _RECORD_HEALING_SQL,
            (
                record.get("ts"),
                record.get("action"),
                record.get("target"),
                record.get("proposal_key"),
                record.get("payload"),
                record.get("status"),
                record.get("approver"),
            ),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return int(row_id)
