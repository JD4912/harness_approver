from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(200), nullable=False)
    org_id: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    pipeline_id: Mapped[str] = mapped_column(String(200), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(200), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    api_response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
