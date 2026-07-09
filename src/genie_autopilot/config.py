"""Workspace configuration and credential resolution.

Token resolution order:
1. DATABRICKS_TOKEN environment variable
2. macOS Keychain item (service: databricks-fe)

The PAT never lives in the repo or in .databrickscfg committed files.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

HOST = os.environ.get(
    "DATABRICKS_HOST", "https://dbc-c00424a1-8d76.cloud.databricks.com"
)

KEYCHAIN_SERVICE = "databricks-fe"

# Filled in during Week 1 bootstrap; overridable via env for forks of this project.
CATALOG = os.environ.get("GA_CATALOG", "workspace")
SCHEMA = os.environ.get("GA_SCHEMA", "banking_gold")
GENIE_SPACE_ID = os.environ.get("GA_GENIE_SPACE_ID", "")
WAREHOUSE_ID = os.environ.get("GA_WAREHOUSE_ID", "")


def resolve_token() -> str:
    """Return the workspace PAT from env or macOS Keychain."""
    token = os.environ.get("DATABRICKS_TOKEN")
    if token:
        return token
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "No Databricks token found. Set DATABRICKS_TOKEN or add a Keychain item: "
            f"security add-generic-password -s {KEYCHAIN_SERVICE} -a <you> -w <token>"
        ) from exc


@lru_cache(maxsize=1)
def workspace_client():
    """Authenticated databricks-sdk WorkspaceClient (lazy import keeps tests light)."""
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(host=HOST, token=resolve_token())


def fq(table: str) -> str:
    """Fully qualified table name in the project catalog/schema."""
    return f"{CATALOG}.{SCHEMA}.{table}"
