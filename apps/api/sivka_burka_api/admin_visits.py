"""Owner-only, consistent read projections for the visit calendar."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from .admin_inquiries import _cash_summary


class InvalidVisitCursor(ValueError):
    """The cursor is malformed or belongs to a different calendar interval."""


@dataclass(frozen=True)
class AdminVisitReadRuntime:
    """Explicit database dependency; production composition remains external."""

    engine: Engine


def _cursor_encode(value: dict[str, Any]) -> str:
    return urlsafe_b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()


def _cursor_decode(cursor: str, filters: dict[str, str]) -> tuple[datetime, UUID]:
    try:
        raw = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if payload["filters"] != filters:
            raise InvalidVisitCursor
        return datetime.fromisoformat(payload["start_at"]), UUID(payload["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidVisitCursor from error


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "version": row["version"], "status": row["status"],
        "service_title": row["service_title"], "start_at": row["start_at"].isoformat(),
        "duration_minutes": row["duration_minutes"],
        "end_at": row["end_at"].isoformat() if row["end_at"] else None,
        "inquiry_count": int(row["inquiry_count"]), "participants_count": int(row["participants_count"]),
        "has_unresolved_inquiries": bool(row["has_unresolved_inquiries"]),
    }


class AdminVisitReader:
    def __init__(self, runtime: AdminVisitReadRuntime):
        self.engine = runtime.engine

    def list(self, *, from_at: datetime, to_at: datetime, cursor: str | None, limit: int) -> dict[str, Any]:
        filters = {"from": from_at.isoformat(), "to": to_at.isoformat()}
        cursor_values = _cursor_decode(cursor, filters) if cursor else None
        clauses = ["((v.duration_minutes IS NULL AND v.start_at >= :from_at AND v.start_at < :to_at) OR (v.duration_minutes IS NOT NULL AND v.start_at < :to_at AND v.start_at + v.duration_minutes * interval '1 minute' > :from_at))"]
        params: dict[str, Any] = {"from_at": from_at, "to_at": to_at, "limit": limit + 1}
        if cursor_values:
            clauses.append("(v.start_at, v.id) > (:cursor_start_at, :cursor_id)")
            params.update(cursor_start_at=cursor_values[0], cursor_id=cursor_values[1])
        query = text("""
            SELECT v.id, v.version, v.status, s.title AS service_title, v.start_at, v.duration_minutes,
                   CASE WHEN v.duration_minutes IS NULL THEN NULL
                        ELSE v.start_at + v.duration_minutes * interval '1 minute' END AS end_at,
                   count(p.id) AS inquiry_count,
                   coalesce(sum(t.participants_count), 0) AS participants_count,
                   coalesce(bool_or(i.status NOT IN ('COMPLETED', 'CANCELLED')), false) AS has_unresolved_inquiries
            FROM visits v JOIN services s ON s.id = v.service_id
            LEFT JOIN visit_participations p ON p.visit_id = v.id AND p.closed_at IS NULL
            LEFT JOIN inquiries i ON i.id = p.inquiry_id
            LEFT JOIN inquiry_terms t ON t.inquiry_id = i.id
            WHERE """ + " AND ".join(clauses) + """
            GROUP BY v.id, s.title
            ORDER BY v.start_at ASC, v.id ASC
            LIMIT :limit
        """)
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                rows = [dict(row) for row in connection.execute(query, params).mappings()]
                page, overflow = rows[:limit], len(rows) > limit
                result = [_summary(row) for row in page]
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
        next_cursor = None
        if overflow and page:
            last = page[-1]
            next_cursor = _cursor_encode({"filters": filters, "start_at": last["start_at"].isoformat(), "id": str(last["id"])})
        return {"items": result, "next_cursor": next_cursor}

    def detail(self, visit_id: UUID) -> dict[str, Any] | None:
        visit_query = text("""
            SELECT v.id, v.version, v.status, s.title AS service_title, v.start_at, v.duration_minutes,
                   CASE WHEN v.duration_minutes IS NULL THEN NULL
                        ELSE v.start_at + v.duration_minutes * interval '1 minute' END AS end_at
            FROM visits v JOIN services s ON s.id = v.service_id WHERE v.id = :id
        """)
        participations_query = text("""
            SELECT p.id, i.id AS inquiry_id, i.version AS inquiry_version, i.status,
                   i.requester_name, t.participants_count, t.total_minor
            FROM visit_participations p JOIN inquiries i ON i.id = p.inquiry_id
            JOIN inquiry_terms t ON t.inquiry_id = i.id
            WHERE p.visit_id = :id AND p.closed_at IS NULL
            ORDER BY p.joined_at ASC, p.id ASC
        """)
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                visit = connection.execute(visit_query, {"id": visit_id}).mappings().one_or_none()
                if visit is None:
                    transaction.commit()
                    return None
                participations = []
                for row in connection.execute(participations_query, {"id": visit_id}).mappings():
                    row = dict(row)
                    participations.append({
                        "id": str(row["id"]), "inquiry_id": str(row["inquiry_id"]),
                        "inquiry_version": row["inquiry_version"], "status": row["status"],
                        "participants_count": row["participants_count"], "requester_name": row["requester_name"],
                        "cash_summary": _cash_summary(connection, row["inquiry_id"], row["total_minor"]),
                    })
                summary = _summary({**dict(visit), "inquiry_count": len(participations),
                                    "participants_count": sum(item["participants_count"] for item in participations),
                                    "has_unresolved_inquiries": any(item["status"] not in {"COMPLETED", "CANCELLED"} for item in participations)})
                transaction.commit()
                return {**summary, "participations": participations, "allowed_commands": []}
            except Exception:
                transaction.rollback()
                raise
