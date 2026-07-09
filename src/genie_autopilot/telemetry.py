"""Telemetry ingestion: normalize Genie interaction history into correction records.

Primary source: Conversation API (list_conversations + list_messages) — richest and
confirmed available. Secondary sources, verified per-workspace during Phase 0:
  system.access.audit  WHERE service_name = 'aibiGenie'   (feedback action events)
  system.query.history WHERE client_application = 'Databricks SQL Genie Space'
"""

from __future__ import annotations

from .drift import Correction, parse_correction
from .genie_api import GenieAPI


def harvest_corrections(api: GenieAPI, roles_by_user: dict[str, str] | None = None) -> list[Correction]:
    """Walk all conversations in the space and mine structured corrections.

    A correction is any message whose text parses as 'term → entity' feedback —
    typically the follow-up a user types after a thumbs-down.
    """
    roles_by_user = roles_by_user or {}
    corrections: list[Correction] = []
    for conv in api.list_conversations():
        conv_id = conv.get("conversation_id") or conv.get("id", "")
        user = str(conv.get("user_id", "unknown"))
        for msg in api.list_messages(conv_id):
            content = msg.get("content", "")
            parsed = parse_correction(content)
            if not parsed:
                continue
            term, entity = parsed
            corrections.append(
                Correction(
                    term=term,
                    entity=entity,
                    user=user,
                    role=roles_by_user.get(user, "unknown"),
                    ts=float(msg.get("created_timestamp", 0)) / 1000.0
                    if msg.get("created_timestamp")
                    else 0.0,
                    source_message_id=msg.get("message_id") or msg.get("id", ""),
                )
            )
    return corrections


AUDIT_FEEDBACK_SQL = """
SELECT event_time, user_identity.email AS user, action_name,
       request_params.space_id       AS space_id,
       request_params.feedback_rating AS rating
FROM system.access.audit
WHERE service_name = 'aibiGenie'
  AND action_name IN ('updateConversationMessageFeedback', 'createConversationMessageComment')
  AND request_params.space_id = :space_id
ORDER BY event_time DESC
"""

GENIE_QUERY_HISTORY_SQL = """
SELECT statement_text, executed_by, start_time, total_duration_ms
FROM system.query.history
WHERE client_application = 'Databricks SQL Genie Space'
ORDER BY start_time DESC
LIMIT 500
"""
