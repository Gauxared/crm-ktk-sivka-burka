"""Owner-only, consistent read projections for CRM inquiries."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import Engine, text


class InvalidInquiryCursor(ValueError):
    """The caller supplied a cursor that does not belong to this query."""


@dataclass(frozen=True)
class AdminInquiryReadRuntime:
    """Explicit database dependency; production composition remains external."""

    engine: Engine


def _cursor_encode(value: dict[str, Any]) -> str:
    return urlsafe_b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()


def _cursor_decode(cursor: str, filters: dict[str, Any]) -> tuple[datetime, UUID]:
    try:
        raw = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if payload["filters"] != filters:
            raise InvalidInquiryCursor
        return datetime.fromisoformat(payload["received_at"]), UUID(payload["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidInquiryCursor from error


def _json(value: Any) -> Any:
    """Return JSONB values in a form FastAPI can safely serialize.

    psycopg normally decodes JSONB to Python objects, while alternate driver
    configurations can return its textual representation.  The read model
    accepts both without exposing an undecodable value as an internal error.
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value


def _cash_summary(connection: Any, inquiry_id: UUID, total_minor: int | None) -> dict[str, Any]:
    row = connection.execute(text("""
        SELECT COALESCE(sum(amount_minor) FILTER (WHERE kind = 'RECEIPT'), 0) AS received_minor,
               COALESCE(sum(amount_minor) FILTER (WHERE kind = 'REFUND_NOTE'), 0) AS returned_minor
        FROM cash_notes n
        WHERE n.inquiry_id = :id
          AND NOT EXISTS (SELECT 1 FROM cash_note_corrections c WHERE c.original_note_id = n.id)
    """), {"id": inquiry_id}).mappings().one()
    received, returned = int(row["received_minor"]), int(row["returned_minor"])
    net = received - returned
    return {
        "received_minor": received, "returned_minor": returned, "net_received_minor": net,
        "total_minor": total_minor, "balance_minor": total_minor - net if total_minor is not None else None,
        "calculation_warning": "DATA_REVIEW_REQUIRED" if returned > received else None,
    }


def _summary(connection: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "version": row["version"], "status": row["status"],
        "requester_name": row["requester_name"], "service_title": row["service_title"],
        "participants_count": row["participants_count"],
        "requested_time": {"date": row["requested_date"].isoformat() if row["requested_date"] else None,
                           "time_text": row["requested_time_text"], "timezone": row["requested_timezone"]},
        "visit_id": str(row["visit_id"]) if row["visit_id"] else None,
        "agreed_start_at": row["agreed_start_at"].isoformat() if row["agreed_start_at"] else None,
        "received_at": row["received_at"].isoformat(), "channel": row["channel"],
        "cash_summary": _cash_summary(connection, row["id"], row["total_minor"]),
    }


class AdminInquiryReader:
    def __init__(self, runtime: AdminInquiryReadRuntime):
        self.engine = runtime.engine

    def list(self, *, status: str | None, has_visit: bool | None, channel: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
        filters = {"status": status, "has_visit": has_visit, "channel": channel}
        cursor_values = _cursor_decode(cursor, filters) if cursor else None
        clauses, params = [], {"limit": limit + 1}
        if status is not None:
            clauses.append("i.status = :status"); params["status"] = status
        if channel is not None:
            clauses.append("i.source_kind = :channel"); params["channel"] = channel
        if has_visit is not None:
            clauses.append(("EXISTS" if has_visit else "NOT EXISTS") + " (SELECT 1 FROM visit_participations p WHERE p.inquiry_id = i.id AND p.closed_at IS NULL)")
        if cursor_values:
            clauses.append("(i.received_at, i.id) < (:cursor_received_at, :cursor_id)")
            params.update(cursor_received_at=cursor_values[0], cursor_id=cursor_values[1])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = text("""
            SELECT i.id, i.version, i.status, i.requester_name, i.requested_date, i.requested_time_text,
                   i.requested_timezone, i.received_at, i.source_kind AS channel, t.participants_count,
                   t.total_minor, s.title AS service_title, p.visit_id, v.start_at AS agreed_start_at
            FROM inquiries i JOIN inquiry_terms t ON t.inquiry_id = i.id
            JOIN service_options o ON o.id = t.service_option_id JOIN services s ON s.id = o.service_id
            LEFT JOIN visit_participations p ON p.inquiry_id = i.id AND p.closed_at IS NULL
            LEFT JOIN visits v ON v.id = p.visit_id""" + where + " ORDER BY i.received_at DESC, i.id DESC LIMIT :limit")
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                rows = [dict(row) for row in connection.execute(query, params).mappings()]
                page, overflow = rows[:limit], len(rows) > limit
                items = [_summary(connection, row) for row in page]
                transaction.commit()
            except Exception:
                transaction.rollback(); raise
        next_cursor = None
        if overflow and page:
            last = page[-1]
            next_cursor = _cursor_encode({"filters": filters, "received_at": last["received_at"].isoformat(), "id": str(last["id"])})
        return {"items": items, "next_cursor": next_cursor}

    def detail(self, inquiry_id: UUID) -> dict[str, Any] | None:
        query = text("""
            SELECT i.*, i.source_kind AS channel, s.title AS service_title, t.service_option_id, t.participants_count, t.duration_minutes, t.total_minor,
                   t.expected_prepayment_minor, t.currency, t.note AS terms_note, p.id AS participation_id,
                   p.visit_id, p.joined_at, v.status AS visit_status, v.start_at AS agreed_start_at
            FROM inquiries i JOIN inquiry_terms t ON t.inquiry_id=i.id
            JOIN service_options o ON o.id=t.service_option_id JOIN services s ON s.id=o.service_id
            LEFT JOIN visit_participations p ON p.inquiry_id=i.id AND p.closed_at IS NULL
            LEFT JOIN visits v ON v.id=p.visit_id WHERE i.id=:id
        """)
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                row = connection.execute(query, {"id": inquiry_id}).mappings().one_or_none()
                if row is None:
                    transaction.commit(); return None
                row = dict(row)
                notes = [dict(note) for note in connection.execute(text("""
                    SELECT n.*, c.id AS correction_id, c.replacement_note_id, c.reason AS correction_reason,
                           c.recorded_at AS correction_recorded_at
                    FROM cash_notes n LEFT JOIN cash_note_corrections c ON c.original_note_id=n.id
                    WHERE n.inquiry_id=:id ORDER BY n.recorded_at, n.id
                """), {"id": inquiry_id}).mappings()]
                jobs = [dict(job) for job in connection.execute(text("""
                    SELECT id, version, status, attempt_count, next_attempt_at, delivered_at, last_error_code
                    FROM notification_jobs WHERE inquiry_id=:id ORDER BY next_attempt_at, id
                """), {"id": inquiry_id}).mappings()]
                result = self._detail_value(connection, row, notes, jobs)
                transaction.commit(); return result
            except Exception:
                transaction.rollback(); raise

    @staticmethod
    def _detail_value(connection: Any, row: dict[str, Any], notes: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, Any]:
        cash_notes = [{"id": str(n["id"]), "kind": n["kind"], "amount_minor": n["amount_minor"], "currency": n["currency"],
                       "occurred_at": n["occurred_at"].isoformat(), "recorded_at": n["recorded_at"].isoformat(), "note": n["note"],
                       "correction": None if n["correction_id"] is None else {"id": str(n["correction_id"]), "replacement_note_id": str(n["replacement_note_id"]) if n["replacement_note_id"] else None, "reason": n["correction_reason"], "recorded_at": n["correction_recorded_at"].isoformat()},
                       "effective": n["correction_id"] is None} for n in notes]
        summary = _summary(connection, row)
        return {**summary, "contact_snapshot": _json(row["contact_snapshot"]),
                "current_contact": {"kind": row["contact_kind"], "value": row["contact_value"]},
                "selected_option_snapshot": _json(row["selection_snapshot"]),
                "agreed_terms": {"service_option_id": str(row["service_option_id"]), "participants_count": row["participants_count"], "duration_minutes": row["duration_minutes"], "total_minor": row["total_minor"], "expected_prepayment_minor": row["expected_prepayment_minor"], "currency": row["currency"], "note": row["terms_note"]},
                "comment": row["comment"], "owner_note": row["owner_note"], "acquisition": _json(row["acquisition"]),
                "participation": None if row["participation_id"] is None else {"id": str(row["participation_id"]), "visit_id": str(row["visit_id"]), "joined_at": row["joined_at"].isoformat(), "visit_status": row["visit_status"], "agreed_start_at": row["agreed_start_at"].isoformat()},
                "cash_notes": cash_notes, "notification_summary": [{"job_id": str(j["id"]), "version": j["version"], "status": j["status"], "attempt_count": j["attempt_count"], "next_attempt_at": j["next_attempt_at"].isoformat(), "delivered_at": j["delivered_at"].isoformat() if j["delivered_at"] else None, "safe_error_code": j["last_error_code"]} for j in jobs], "allowed_commands": []}
