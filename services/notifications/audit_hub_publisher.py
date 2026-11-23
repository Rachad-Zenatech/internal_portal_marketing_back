"""Publish local audit log records to the CEO Dashboard via the RabbitMQ Message Hub."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

SOURCE_SERVICE = "marketing"
SOURCE_LABEL = "Marketing"


async def _publish_audit_log_async(payload: Dict[str, Any]) -> None:
    try:
        from services.notifications.rabbitmq_service import EVENTS_EXCHANGE_NAME, rabbitmq_manager

        if not rabbitmq_manager.is_connected:
            return
        routing_key = f"{getattr(rabbitmq_manager, 'service_key', SOURCE_SERVICE) or SOURCE_SERVICE}.audit.logged"
        await rabbitmq_manager.publish_message(EVENTS_EXCHANGE_NAME, routing_key, payload)
    except Exception as exc:
        logger.debug(f"Audit log Message Hub publish skipped: {exc}")


def publish_audit_log_to_hub(
    *,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    actor_user_id: Any = None,
    actor_name: Optional[str] = None,
    actor_email: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Fire-and-forget dispatch of an audit log entry to the CEO Dashboard over Message Hub."""
    payload: Dict[str, Any] = {
        "message_type": "audit_log",
        "event_type": "AUDIT_LOG_CREATED",
        "event_id": str(uuid.uuid4()),
        "source_service": SOURCE_SERVICE,
        "source_label": SOURCE_LABEL,
        "action": str(action or "UNKNOWN"),
        "entity_type": str(entity_type or "System"),
        "entity_id": str(entity_id) if entity_id is not None else "",
        "actor_user_id": str(actor_user_id) if actor_user_id is not None else None,
        "actor_name": actor_name,
        "actor_email": actor_email,
        "old_value": old_value,
        "new_value": new_value,
        "ip_address": str(ip_address) if ip_address is not None else None,
        "user_agent": user_agent,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_publish_audit_log_async(payload))
    except RuntimeError:
        pass
    except Exception as exc:
        logger.debug(f"Could not schedule audit log hub publish: {exc}")


async def log_audit(
    conn,
    actor_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    old_value: dict = None,
    new_value: dict = None,
    ip: str = None,
    ua: str = None,
):
    sql = """
        INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, ip_address, user_agent, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
    """
    old_json = json.dumps(old_value, default=str) if old_value else None
    new_json = json.dumps(new_value, default=str) if new_value else None
    await conn.execute(sql, actor_id, action, entity_type, str(entity_id) if entity_id is not None else None, old_json, new_json, ip, ua)

    actor_name, actor_email = None, None
    if actor_id:
        try:
            u = await conn.fetchrow("SELECT full_name, email FROM users WHERE id = $1", actor_id)
            if u:
                actor_name, actor_email = u["full_name"], u["email"]
        except Exception:
            pass
    publish_audit_log_to_hub(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_id,
        actor_name=actor_name,
        actor_email=actor_email,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip,
        user_agent=ua,
    )


async def audit_many(conn, actor_id: UUID | None, entity_type: str, entries: list[tuple]) -> None:
    """Write many audit entries in one statement: (action, entity id, old value, new value) each."""
    if not entries:
        return
    await conn.execute(
        """
        INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
        SELECT $1, a.action, $2, a.entity_id, CAST(a.old_value AS jsonb), CAST(a.new_value AS jsonb), now()
        FROM unnest($3::text[], $4::text[], $5::text[], $6::text[]) AS a(action, entity_id, old_value, new_value)
        """,
        actor_id,
        entity_type,
        [e[0] for e in entries],
        [str(e[1]) for e in entries],
        [json.dumps(e[2], default=str) if e[2] is not None else None for e in entries],
        [json.dumps(e[3], default=str) if e[3] is not None else None for e in entries],
    )
    for e in entries:
        publish_audit_log_to_hub(
            action=e[0],
            entity_type=entity_type,
            entity_id=str(e[1]),
            actor_user_id=actor_id,
            old_value=e[2],
            new_value=e[3],
        )


async def write_navan_event_audit(conn, entity_type: str, initiative_id: Any, values: dict) -> None:
    await conn.execute(
        """
        INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
        VALUES (NULL, 'CREATE', $1, $2, NULL, CAST($3 AS jsonb), now())
        """,
        entity_type,
        str(initiative_id),
        json.dumps(values, default=str),
    )
    publish_audit_log_to_hub(
        action="CREATE",
        entity_type=entity_type,
        entity_id=str(initiative_id),
        actor_name="Navan Sync",
        new_value=values,
    )


async def write_navan_audits(conn, audits) -> None:
    for entity_type, action, entity_id, old, new in audits:
        await conn.execute(
            """
            INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, created_at)
            VALUES (NULL, $1, $2, $3, CAST($4 AS jsonb), CAST($5 AS jsonb), now())
            """,
            action,
            entity_type,
            str(entity_id),
            json.dumps(old, default=str) if old is not None else None,
            json.dumps(new, default=str) if new is not None else None,
        )
        publish_audit_log_to_hub(
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor_name="Navan Sync",
            old_value=old,
            new_value=new,
        )


def publish_budget_copy_audit(row, from_month, to_month, actor_id: UUID, entity_type: str) -> None:
    if row and row["created"]:
        publish_audit_log_to_hub(
            action="CREATE",
            entity_type=entity_type,
            entity_id=str(to_month),
            actor_user_id=actor_id,
            new_value={"copied_from": str(from_month), "to_month": str(to_month), "created": row["created"]},
        )
