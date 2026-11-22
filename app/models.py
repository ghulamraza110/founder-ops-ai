"""SQLAlchemy models and database setup."""
import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./founder_ops.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StartupCase(Base):
    __tablename__ = "startup_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    entity_type: Mapped[str] = mapped_column(String(50), default="Delaware C-Corp")
    state: Mapped[str] = mapped_column(String(50), default="Delaware")
    incorporation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="intake")

    founders: Mapped[list["Founder"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class Founder(Base):
    __tablename__ = "founders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("startup_cases.id"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    equity_percent: Mapped[float] = mapped_column(Float)
    vesting_months: Mapped[int] = mapped_column(Integer, default=48)
    cliff_months: Mapped[int] = mapped_column(Integer, default=12)

    case: Mapped[StartupCase] = relationship(back_populates="founders")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("startup_cases.id"))
    doc_type: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_md: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="draft")
    approvals_json: Mapped[str] = mapped_column(Text, default="[]")

    case: Mapped[StartupCase] = relationship(back_populates="documents")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("startup_cases.id"))
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[StartupCase] = relationship(back_populates="audit_logs")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_action(db, case_id: int, actor: str, action: str, details: str = "") -> None:
    db.add(AuditLog(case_id=case_id, actor=actor, action=action, details=details))
