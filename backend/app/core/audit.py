"""Immutable financial and operational Audit Trail service."""

import logging
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import AuditLog

logger = logging.getLogger(__name__)


class AuditLogger:
    """Writes structured, immutable audit records for all financial and security operations."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        category: str,
        action: str,
        details: dict[str, Any] | None = None,
        severity: str = "INFO",
    ) -> AuditLog:
        """Persist an audit log entry asynchronously."""
        entry = AuditLog(
            category=category.upper().strip(),
            severity=severity.upper().strip(),
            action=action,
            details=details or {},
        )
        try:
            async with self._session_factory() as session:
                session.add(entry)
                await session.commit()
                await session.refresh(entry)
        except Exception as exc:
            logger.error("Failed to persist AuditLog (%s / %s): %s", category, action, exc)
        return entry
