from sqlmodel import Session

from app.models import AuditLog


def log_action(
    session: Session,
    actor_email: str,
    entity_type: str,
    entity_id: int,
    action: str,
    detail: str | None = None,
) -> None:
    """Records an audit entry on the caller's session without committing,
    so it lands in the same transaction as the change it describes."""
    session.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            actor_email=actor_email,
            action=action,
            detail=detail,
        )
    )
