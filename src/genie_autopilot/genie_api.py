"""Thin REST wrapper over the Genie Conversation + Space Management APIs.

Uses raw endpoint paths through the SDK's ApiClient so behavior is stable
regardless of SDK version surface. All question-producing POSTs are paced to
respect Free Edition's ~5 questions/minute soft limit; polling GETs are exempt.

Endpoints (GA 2026):
  POST /api/2.0/genie/spaces/{space}/start-conversation
  POST /api/2.0/genie/spaces/{space}/conversations/{conv}/messages
  GET  /api/2.0/genie/spaces/{space}/conversations/{conv}/messages/{msg}
  GET  .../messages/{msg}/query-result/{attachment_id}
  POST .../messages/{msg}/feedback
  GET  /api/2.0/genie/spaces/{space}/conversations
  GET  /api/2.0/genie/spaces/{space}?include_serialized_space=true
  PATCH/POST space update via SDK genie.update_space
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}


class RateLimiter:
    """Enforce a minimum interval between question POSTs (default 12.5s ≈ 4.8/min)."""

    def __init__(self, min_interval_s: float = 12.5):
        self.min_interval_s = min_interval_s
        self._last: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)
        self._last = time.monotonic()


@dataclass
class GenieAnswer:
    conversation_id: str
    message_id: str
    status: str
    text: str = ""
    sql: str = ""
    attachment_id: str = ""
    raw: dict = field(default_factory=dict)


class GenieAPI:
    def __init__(self, workspace_client, space_id: str, limiter: RateLimiter | None = None):
        self.w = workspace_client
        self.space_id = space_id
        self.limiter = limiter or RateLimiter()

    # -- internals ---------------------------------------------------------
    def _do(self, method: str, path: str, body: dict | None = None) -> dict:
        return self.w.api_client.do(method, path, body=body) or {}

    def _base(self) -> str:
        return f"/api/2.0/genie/spaces/{self.space_id}"

    # -- conversations -----------------------------------------------------
    def ask(self, question: str, conversation_id: str | None = None) -> GenieAnswer:
        """Start or continue a conversation, then poll to a terminal state."""
        self.limiter.wait()
        if conversation_id:
            resp = self._do(
                "POST",
                f"{self._base()}/conversations/{conversation_id}/messages",
                {"content": question},
            )
        else:
            resp = self._do("POST", f"{self._base()}/start-conversation", {"content": question})
        conv_id = resp.get("conversation_id") or resp.get("conversation", {}).get("id")
        msg = resp.get("message", {})
        msg_id = resp.get("message_id") or msg.get("id") or msg.get("message_id")
        return self.poll(conv_id, msg_id)

    def poll(self, conversation_id: str, message_id: str, timeout_s: int = 300) -> GenieAnswer:
        deadline = time.monotonic() + timeout_s
        path = f"{self._base()}/conversations/{conversation_id}/messages/{message_id}"
        raw: dict = {}
        while time.monotonic() < deadline:
            raw = self._do("GET", path)
            if raw.get("status") in TERMINAL_STATES:
                break
            time.sleep(2.5)
        text, sql, attachment_id = "", "", ""
        for att in raw.get("attachments") or []:
            if att.get("text"):
                text = att["text"].get("content", "") if isinstance(att["text"], dict) else str(att["text"])
            if att.get("query"):
                sql = att["query"].get("query", "")
                attachment_id = att.get("attachment_id", "")
        return GenieAnswer(
            conversation_id=conversation_id,
            message_id=message_id,
            status=raw.get("status", "UNKNOWN"),
            text=text,
            sql=sql,
            attachment_id=attachment_id,
            raw=raw,
        )

    def query_result(self, conversation_id: str, message_id: str, attachment_id: str) -> dict:
        return self._do(
            "GET",
            f"{self._base()}/conversations/{conversation_id}/messages/{message_id}"
            f"/query-result/{attachment_id}",
        )

    def send_feedback(
        self, conversation_id: str, message_id: str, rating: str, comment: str | None = None
    ) -> dict:
        """rating: POSITIVE | NEGATIVE (GenieFeedbackRating)."""
        body: dict[str, Any] = {"rating": rating}
        if comment:
            body["comment"] = comment
        return self._do(
            "POST",
            f"{self._base()}/conversations/{conversation_id}/messages/{message_id}/feedback",
            body,
        )

    # -- telemetry ---------------------------------------------------------
    def list_conversations(self, include_all: bool = True, page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            path = f"{self._base()}/conversations?page_size={page_size}"
            if include_all:
                path += "&include_all=true"
            if token:
                path += f"&page_token={token}"
            resp = self._do("GET", path)
            out.extend(resp.get("conversations", []))
            token = resp.get("next_page_token")
            if not token:
                break
        return out

    def list_messages(self, conversation_id: str, page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            path = f"{self._base()}/conversations/{conversation_id}/messages?page_size={page_size}"
            if token:
                path += f"&page_token={token}"
            resp = self._do("GET", path)
            out.extend(resp.get("messages", []))
            token = resp.get("next_page_token")
            if not token:
                break
        return out

    # -- space management ----------------------------------------------------
    def get_space(self) -> dict:
        return self._do("GET", f"{self._base()}?include_serialized_space=true")

    def update_space(self, serialized_space: str, etag: str | None = None) -> dict:
        body: dict[str, Any] = {"serialized_space": serialized_space}
        if etag:
            body["etag"] = etag
        return self._do("PATCH", self._base(), body)
