"""Publish local audit log records to the CEO Dashboard via the RabbitMQ Message Hub."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
