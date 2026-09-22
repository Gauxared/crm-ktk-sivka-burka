"""FastAPI application entry point."""

import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .inquiries import (
    IdempotencyMismatch,
    InquiryCommandError,
    OptionUnavailable,
    PublicInquiryCommandService,
    SubmissionAlreadyUsed,
    SubmissionExpired,
)
from .public_submission import PublicSubmissionRuntime, SubmissionRateLimitError
from .public_catalog import PublicCatalogRuntime, PublicCatalogUnavailable
from .admin_sessions import AdminSessionRuntime, COOKIE_NAME, LoginRateLimited
from .admin_commands import AdminCommandRuntime
from .admin_inquiries import AdminInquiryReadRuntime, AdminInquiryReader, InvalidInquiryCursor
from .admin_visits import AdminVisitReadRuntime, AdminVisitReader, InvalidVisitCursor
from .owner_commands import IdempotencyMismatch as OwnerIdempotencyMismatch, OwnerCommandError
from .settings import load_settings
from apps.bot.sivka_burka_bot.telegram_runtime import TelegramWebhookError, TelegramWebhookRuntime
from apps.bot.sivka_burka_bot.telegram_transport import TelegramTransportError


_NO_STORE = {"Cache-Control": "no-store"}


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status_code, headers=_NO_STORE)


async def _json_object(request: Request, *, empty: bool = False) -> dict[str, object]:
    try:
        raw = await request.body()
        if len(raw) > 32 * 1024:
            raise InquiryCommandError("INVALID_REQUEST")
        body = json.loads(raw)
    except Exception:
        raise InquiryCommandError("INVALID_REQUEST") from None
    if not isinstance(body, dict) or (empty and body):
        raise InquiryCommandError("INVALID_REQUEST")
    return body


def _validate_public_inquiry_shape(payload: dict[str, object]) -> None:
    allowed = {"submission_token", "catalog_version", "service_option_id", "requester", "participants_count", "requested_time", "experience", "comment", "acquisition"}
    if set(payload) - allowed:
        raise InquiryCommandError("INVALID_REQUEST")
    requester = payload.get("requester")
    if isinstance(requester, dict) and set(requester) - {"name", "contact"}:
        raise InquiryCommandError("INVALID_REQUEST")
    contact = requester.get("contact") if isinstance(requester, dict) else None
    if isinstance(contact, dict) and set(contact) - {"kind", "value"}:
        raise InquiryCommandError("INVALID_REQUEST")
    requested_time = payload.get("requested_time")
    if isinstance(requested_time, dict) and set(requested_time) - {"date", "time_text"}:
        raise InquiryCommandError("INVALID_REQUEST")
    acquisition = payload.get("acquisition")
    if acquisition is not None and not isinstance(acquisition, dict):
        raise InquiryCommandError("INVALID_REQUEST")
    if isinstance(requester, dict):
        name = requester.get("name")
        if isinstance(name, str) and len(name.strip()) > 120:
            raise InquiryCommandError("INVALID_REQUEST")
    if isinstance(contact, dict):
        value = contact.get("value")
        if isinstance(value, str) and len(value.strip()) > 200:
            raise InquiryCommandError("INVALID_REQUEST")


def _runtime_or_error(runtime: PublicSubmissionRuntime | None) -> PublicSubmissionRuntime | JSONResponse:
    return runtime if runtime is not None else _error("PUBLIC_SUBMISSION_UNAVAILABLE", 503)


def _map_command_error(error: InquiryCommandError) -> JSONResponse:
    if isinstance(error, (IdempotencyMismatch, SubmissionAlreadyUsed, OptionUnavailable)):
        return _error(error.code, 409)
    if isinstance(error, SubmissionExpired):
        return _error(error.code, 410)
    if error.code == "RESULT_EXPIRED":
        return _error("RECEIPT_EXPIRED", 410)
    return _error("INVALID_REQUEST", 400)


def create_app(*, public_submission_runtime: PublicSubmissionRuntime | None = None, public_catalog_runtime: PublicCatalogRuntime | None = None, admin_session_runtime: AdminSessionRuntime | None = None, admin_command_runtime: AdminCommandRuntime | None = None, admin_inquiry_read_runtime: AdminInquiryReadRuntime | None = None, admin_visit_read_runtime: AdminVisitReadRuntime | None = None, telegram_webhook_runtime: TelegramWebhookRuntime | None = None) -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Sivka-Burka API", version="0.1.0")

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        """Report process liveness only; this endpoint intentionally does not touch PostgreSQL."""
        return {"status": "ok", "service": "sivka-burka-api", "environment": settings.environment}

    @app.post("/api/v1/integrations/telegram/webhook", tags=["integrations"])
    async def telegram_webhook(request: Request) -> JSONResponse:
        if telegram_webhook_runtime is None:
            return _error("TELEGRAM_WEBHOOK_UNAVAILABLE", 503)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("UNSUPPORTED_MEDIA_TYPE", 415)
        try:
            chunks, total = [], 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > 256 * 1024:
                    return _error("REQUEST_TOO_LARGE", 413)
                chunks.append(chunk)
            raw = b"".join(chunks)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return _error("INVALID_REQUEST", 400)
            delivered = await telegram_webhook_runtime.handle(
                request.headers.get("X-Telegram-Bot-Api-Secret-Token"), payload
            )
        except TelegramWebhookError as error:
            return _error("UNAUTHORIZED", 401) if error.code == "UNAUTHORIZED" else (_error("INVALID_REQUEST", 400) if error.code == "MALFORMED_UPDATE" else _error("TELEGRAM_PROCESSING_UNAVAILABLE", 503))
        except TelegramTransportError as error:
            return _error("RATE_LIMITED" if error.code == "RATE_LIMITED" else "TELEGRAM_TEMPORARY_FAILURE", 503)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error("INVALID_REQUEST", 400)
        return JSONResponse({"data": {"accepted": delivered}}, headers=_NO_STORE)

    @app.get("/api/v1/public/catalog", tags=["public"])
    def public_catalog() -> JSONResponse:
        if public_catalog_runtime is None:
            return _error("PUBLIC_CATALOG_UNAVAILABLE", 503)
        try:
            catalog = public_catalog_runtime.read()
        except PublicCatalogUnavailable:
            return _error("PUBLIC_CATALOG_UNAVAILABLE", 503)
        return JSONResponse({"data": catalog}, headers=_NO_STORE)

    @app.post("/api/v1/public/submission-tokens", status_code=201, tags=["public"])
    async def issue_submission_token(request: Request) -> JSONResponse:
        runtime = _runtime_or_error(public_submission_runtime)
        if isinstance(runtime, JSONResponse):
            return runtime
        try:
            await _json_object(request, empty=True)
            runtime.rate_limiter.check("submission-token", request)
            token, expires_at = runtime.issue_token()
        except SubmissionRateLimitError:
            return _error("RATE_LIMITED", 429)
        except InquiryCommandError as error:
            return _map_command_error(error)
        return JSONResponse(
            {"data": {"submission_token": token, "expires_at": expires_at.isoformat()}},
            status_code=201,
            headers=_NO_STORE,
        )

    @app.post("/api/v1/public/inquiries", status_code=201, tags=["public"])
    async def submit_public_inquiry(request: Request) -> JSONResponse:
        runtime = _runtime_or_error(public_submission_runtime)
        if isinstance(runtime, JSONResponse):
            return runtime
        try:
            payload = await _json_object(request)
            _validate_public_inquiry_shape(payload)
            idempotency_key = request.headers.get("Idempotency-Key")
            if not idempotency_key:
                raise InquiryCommandError("INVALID_REQUEST")
            runtime.rate_limiter.check("public-inquiry", request)
            result = PublicInquiryCommandService(
                runtime.engine,
                hmac_secret=runtime.hmac_secret,
                digest_key_version=runtime.digest_key_version,
            ).submit(payload, idempotency_key)
        except SubmissionRateLimitError:
            return _error("RATE_LIMITED", 429)
        except InquiryCommandError as error:
            return _map_command_error(error)
        return JSONResponse({"data": result.public_result()}, status_code=201, headers=_NO_STORE)

    def admin_runtime() -> AdminSessionRuntime | JSONResponse:
        return admin_session_runtime if admin_session_runtime is not None else _error("ADMIN_SESSION_UNAVAILABLE", 503)

    def trusted_origin(request: Request, runtime: AdminSessionRuntime) -> bool:
        return request.headers.get("Origin") in runtime.trusted_origins

    def private_unauthorized() -> JSONResponse:
        return _error("AUTH_REQUIRED", 401)

    def owner_command_runtime(request: Request) -> tuple[AdminCommandRuntime, UUID, dict[str, object]] | JSONResponse:
        session_runtime = admin_runtime()
        if isinstance(session_runtime, JSONResponse):
            return session_runtime
        token = request.cookies.get(COOKIE_NAME)
        try:
            session = session_runtime.active_session(token) if token else None
        except Exception:
            return private_unauthorized()
        if session is None:
            return private_unauthorized()
        if not trusted_origin(request, session_runtime):
            return _error("FORBIDDEN", 403)
        csrf_token = request.headers.get("X-CSRF-Token")
        try:
            csrf_valid = hmac.compare_digest(session["csrf_secret"], csrf_token.encode("ascii"))
        except (AttributeError, UnicodeEncodeError):
            csrf_valid = False
        if not csrf_valid:
            return _error("CSRF_FAILED", 403)
        if admin_command_runtime is None:
            return _error("ADMIN_COMMANDS_UNAVAILABLE", 503)
        return admin_command_runtime, UUID(str(session["owner_id"])), session

    def map_owner_command_error(error: OwnerCommandError) -> JSONResponse:
        if isinstance(error, OwnerIdempotencyMismatch) or error.code == "IDEMPOTENCY_MISMATCH":
            return _error("IDEMPOTENCY_MISMATCH", 409)
        if error.code == "RESULT_EXPIRED":
            return _error("RESULT_EXPIRED", 410)
        if error.code == "OPTION_UNAVAILABLE":
            return _error("OPTION_UNAVAILABLE", 422)
        return _error("VALIDATION_ERROR", 422)

    def map_inquiry_confirmation_error(error: OwnerCommandError) -> JSONResponse:
        if isinstance(error, OwnerIdempotencyMismatch) or error.code == "IDEMPOTENCY_MISMATCH":
            return _error("IDEMPOTENCY_MISMATCH", 409)
        if error.code == "NOT_FOUND":
            return _error("NOT_FOUND", 404)
        if error.code in {"VERSION_CONFLICT", "PLAN_CONFLICT", "PARTICIPATION_CONFLICT"}:
            return _error(error.code, 409)
        if error.code == "RESULT_EXPIRED":
            return _error("RESULT_EXPIRED", 410)
        if error.code in {"EXPECTED_VERSION_REQUIRED", "INVALID_TRANSITION", "VALIDATION_ERROR", "OPTION_UNAVAILABLE"}:
            return _error(error.code, 422)
        return _error("VALIDATION_ERROR", 422)

    def map_inquiry_negotiation_error(error: OwnerCommandError) -> JSONResponse:
        if isinstance(error, OwnerIdempotencyMismatch) or error.code == "IDEMPOTENCY_MISMATCH":
            return _error("IDEMPOTENCY_MISMATCH", 409)
        if error.code == "NOT_FOUND":
            return _error("NOT_FOUND", 404)
        if error.code == "VERSION_CONFLICT":
            return _error("VERSION_CONFLICT", 409)
        if error.code == "RESULT_EXPIRED":
            return _error("RESULT_EXPIRED", 410)
        if error.code in {"EXPECTED_VERSION_REQUIRED", "INVALID_TRANSITION", "VALIDATION_ERROR"}:
            return _error(error.code, 422)
        return _error("VALIDATION_ERROR", 422)

    def map_cash_note_error(error: OwnerCommandError) -> JSONResponse:
        if isinstance(error, OwnerIdempotencyMismatch) or error.code == "IDEMPOTENCY_MISMATCH":
            return _error("IDEMPOTENCY_MISMATCH", 409)
        if error.code == "NOT_FOUND":
            return _error("NOT_FOUND", 404)
        if error.code == "VERSION_CONFLICT":
            return _error("VERSION_CONFLICT", 409)
        if error.code == "RESULT_EXPIRED":
            return _error("RESULT_EXPIRED", 410)
        if error.code in {"EXPECTED_VERSION_REQUIRED", "VALIDATION_ERROR"}:
            return _error(error.code, 422)
        return _error("VALIDATION_ERROR", 422)

    def map_cash_note_correction_error(error: OwnerCommandError) -> JSONResponse:
        if isinstance(error, OwnerIdempotencyMismatch) or error.code == "IDEMPOTENCY_MISMATCH":
            return _error("IDEMPOTENCY_MISMATCH", 409)
        if error.code == "NOT_FOUND":
            return _error("NOT_FOUND", 404)
        if error.code in {"VERSION_CONFLICT", "CASH_NOTE_SUPERSEDED"}:
            return _error(error.code, 409)
        if error.code == "RESULT_EXPIRED":
            return _error("RESULT_EXPIRED", 410)
        if error.code in {"EXPECTED_VERSION_REQUIRED", "VALIDATION_ERROR"}:
            return _error(error.code, 422)
        return _error("VALIDATION_ERROR", 422)

    def owner_read_runtime(request: Request) -> AdminInquiryReader | JSONResponse:
        session_runtime = admin_runtime()
        if isinstance(session_runtime, JSONResponse):
            return session_runtime
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return private_unauthorized()
        try:
            session = session_runtime.active_session(token)
        except Exception:
            # Session validation is an authorization boundary.  An invalid
            # token or a failed validation must never fall through to a CRM
            # read response.
            return private_unauthorized()
        if session is None:
            return private_unauthorized()
        if admin_inquiry_read_runtime is None:
            return _error("ADMIN_INQUIRIES_UNAVAILABLE", 503)
        return AdminInquiryReader(admin_inquiry_read_runtime)

    def owner_visit_read_runtime(request: Request) -> AdminVisitReader | JSONResponse:
        session_runtime = admin_runtime()
        if isinstance(session_runtime, JSONResponse):
            return session_runtime
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return private_unauthorized()
        try:
            session = session_runtime.active_session(token)
        except Exception:
            return private_unauthorized()
        if session is None:
            return private_unauthorized()
        if admin_visit_read_runtime is None:
            return _error("ADMIN_VISITS_UNAVAILABLE", 503)
        return AdminVisitReader(admin_visit_read_runtime)

    def utc_moment(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            return None
        return parsed.astimezone(timezone.utc)

    @app.get("/api/v1/admin/inquiries", tags=["admin"])
    def list_admin_inquiries(request: Request, status: str | None = None, has_visit: bool | None = None, channel: str | None = None, cursor: str | None = None, limit: int = 50) -> JSONResponse:
        runtime = owner_read_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        if not 1 <= limit <= 100:
            return _error("INVALID_REQUEST", 400)
        try:
            result = runtime.list(status=status, has_visit=has_visit, channel=channel, cursor=cursor, limit=limit)
        except InvalidInquiryCursor:
            return _error("INVALID_REQUEST", 400)
        return JSONResponse({"data": result}, headers=_NO_STORE)

    @app.get("/api/v1/admin/inquiries/{inquiry_id}", tags=["admin"])
    def read_admin_inquiry(inquiry_id: str, request: Request) -> JSONResponse:
        runtime = owner_read_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        try:
            result = runtime.detail(UUID(inquiry_id))
        except ValueError:
            return _error("NOT_FOUND", 404)
        if result is None:
            return _error("NOT_FOUND", 404)
        return JSONResponse({"data": result}, headers=_NO_STORE)

    @app.get("/api/v1/admin/visits", tags=["admin"])
    def list_admin_visits(request: Request, from_at: str | None = Query(None, alias="from"), to_at: str | None = Query(None, alias="to"), cursor: str | None = None, limit: int = 50) -> JSONResponse:
        runtime = owner_visit_read_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        from_moment, to_moment = utc_moment(from_at), utc_moment(to_at)
        if from_moment is None or to_moment is None or from_moment >= to_moment:
            return _error("INVALID_REQUEST", 400)
        if to_moment - from_moment > timedelta(days=93):
            return _error("RANGE_TOO_LARGE", 422)
        if not 1 <= limit <= 100:
            return _error("INVALID_REQUEST", 400)
        try:
            result = runtime.list(from_at=from_moment, to_at=to_moment, cursor=cursor, limit=limit)
        except InvalidVisitCursor:
            return _error("INVALID_REQUEST", 400)
        return JSONResponse({"data": result}, headers=_NO_STORE)

    @app.get("/api/v1/admin/visits/{visit_id}", tags=["admin"])
    def read_admin_visit(visit_id: str, request: Request) -> JSONResponse:
        runtime = owner_visit_read_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        try:
            result = runtime.detail(UUID(visit_id))
        except ValueError:
            return _error("NOT_FOUND", 404)
        if result is None:
            return _error("NOT_FOUND", 404)
        return JSONResponse({"data": result}, headers=_NO_STORE)

    @app.post("/api/v1/admin/visits", tags=["admin"])
    async def create_admin_visit(request: Request) -> JSONResponse:
        runtime = owner_command_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("UNSUPPORTED_MEDIA_TYPE", 415)
        try:
            payload = await _json_object(request)
        except InquiryCommandError:
            return _error("VALIDATION_ERROR", 422)
        if set(payload) != {"service_id", "start_at", "duration_minutes"}:
            return _error("VALIDATION_ERROR", 422)
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return _error("VALIDATION_ERROR", 422)
        command_runtime, owner_id, _session = runtime
        try:
            result = command_runtime.owner_commands(owner_id).create_planned_visit(payload, idempotency_key)
        except OwnerCommandError as error:
            return map_owner_command_error(error)
        data = {"command_id": str(result.command_id), "visit": {"id": str(result.visit_id), "version": result.version, "status": result.status}}
        headers = {**_NO_STORE, **({"Idempotent-Replay": "true"} if result.replay else {})}
        return JSONResponse({"data": data}, status_code=200 if result.replay else 201, headers=headers)

    @app.post("/api/v1/admin/inquiries/{inquiry_id}/commands", tags=["admin"])
    async def confirm_admin_inquiry(inquiry_id: str, request: Request) -> JSONResponse:
        runtime = owner_command_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("UNSUPPORTED_MEDIA_TYPE", 415)
        try:
            envelope = await _json_object(request)
        except InquiryCommandError:
            return _error("VALIDATION_ERROR", 422)
        if envelope.get("type") in {"CONFIRM", "START_NEGOTIATION", "COMPLETE", "CANCEL"} and "expected_version" not in envelope:
            return _error("EXPECTED_VERSION_REQUIRED", 422)
        if set(envelope) != {"type", "expected_version", "expected_visit_versions", "payload"} or envelope.get("type") not in {"CONFIRM", "START_NEGOTIATION", "COMPLETE", "CANCEL"}:
            return _error("VALIDATION_ERROR", 422)
        command_type = envelope["type"]
        if command_type == "START_NEGOTIATION" and (envelope["expected_visit_versions"] != {} or envelope["payload"] != {}):
            return _error("VALIDATION_ERROR", 422)
        if command_type == "COMPLETE" and envelope["payload"] != {}:
            return _error("VALIDATION_ERROR", 422)
        if command_type == "CANCEL" and (not isinstance(envelope["payload"], dict) or set(envelope["payload"]) != {"reason"}):
            return _error("VALIDATION_ERROR", 422)
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return _error("VALIDATION_ERROR", 422)
        try:
            target_inquiry_id = UUID(inquiry_id)
        except ValueError:
            return _error("NOT_FOUND", 404)
        command_runtime, owner_id, _session = runtime
        payload = envelope if command_type in {"START_NEGOTIATION", "COMPLETE", "CANCEL"} else {
            "expected_version": envelope["expected_version"],
            "expected_visit_versions": envelope["expected_visit_versions"],
            "payload": envelope["payload"],
        }
        try:
            service = command_runtime.owner_commands(owner_id)
            if command_type == "START_NEGOTIATION":
                result = service.start_negotiation(target_inquiry_id, payload, idempotency_key)
            elif command_type == "COMPLETE":
                result = service.complete_inquiry(target_inquiry_id, payload, idempotency_key)
            elif command_type == "CANCEL":
                result = service.cancel_inquiry(target_inquiry_id, payload, idempotency_key)
            else:
                result = service.confirm_inquiry(target_inquiry_id, payload, idempotency_key)
        except OwnerCommandError as error:
            if command_type == "START_NEGOTIATION":
                return map_inquiry_negotiation_error(error)
            return map_inquiry_confirmation_error(error)
        headers = {**_NO_STORE, **({"Idempotent-Replay": "true"} if result.replay else {})}
        return JSONResponse({"data": result.response()}, headers=headers)

    @app.post("/api/v1/admin/inquiries/{inquiry_id}/cash-notes", tags=["admin"])
    async def record_admin_cash_note(inquiry_id: str, request: Request) -> JSONResponse:
        runtime = owner_command_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("UNSUPPORTED_MEDIA_TYPE", 415)
        try:
            payload = await _json_object(request)
        except InquiryCommandError:
            return _error("VALIDATION_ERROR", 422)
        required = {"expected_version", "kind", "amount_minor", "occurred_at", "note"}
        allowed = required | {"expected_visit_versions"}
        if set(payload) - allowed or not required.issubset(payload):
            return _error("EXPECTED_VERSION_REQUIRED" if "expected_version" not in payload else "VALIDATION_ERROR", 422)
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return _error("VALIDATION_ERROR", 422)
        try:
            target_inquiry_id = UUID(inquiry_id)
        except ValueError:
            return _error("NOT_FOUND", 404)
        command_runtime, owner_id, _session = runtime
        try:
            result = command_runtime.owner_commands(owner_id).record_cash_note(
                target_inquiry_id, payload, idempotency_key
            )
        except OwnerCommandError as error:
            return map_cash_note_error(error)
        headers = {**_NO_STORE, **({"Idempotent-Replay": "true"} if result.replay else {})}
        return JSONResponse({"data": result.response()}, headers=headers)

    @app.post("/api/v1/admin/inquiries/{inquiry_id}/cash-notes/{note_id}/corrections", tags=["admin"])
    async def correct_admin_cash_note(inquiry_id: str, note_id: str, request: Request) -> JSONResponse:
        runtime = owner_command_runtime(request)
        if isinstance(runtime, JSONResponse):
            return runtime
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("UNSUPPORTED_MEDIA_TYPE", 415)
        try:
            payload = await _json_object(request)
        except InquiryCommandError:
            return _error("VALIDATION_ERROR", 422)
        required = {"expected_version", "reason", "replacement"}
        allowed = required | {"expected_visit_versions"}
        if set(payload) - allowed or not required.issubset(payload):
            return _error("EXPECTED_VERSION_REQUIRED" if "expected_version" not in payload else "VALIDATION_ERROR", 422)
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return _error("VALIDATION_ERROR", 422)
        try:
            target_inquiry_id = UUID(inquiry_id)
            target_note_id = UUID(note_id)
        except ValueError:
            return _error("NOT_FOUND", 404)
        command_runtime, owner_id, _session = runtime
        try:
            result = command_runtime.owner_commands(owner_id).correct_cash_note(
                target_inquiry_id, target_note_id, payload, idempotency_key
            )
        except OwnerCommandError as error:
            return map_cash_note_correction_error(error)
        headers = {**_NO_STORE, **({"Idempotent-Replay": "true"} if result.replay else {})}
        return JSONResponse({"data": result.response()}, headers=headers)

    @app.post("/api/v1/admin/session", tags=["admin"])
    async def admin_login(request: Request) -> JSONResponse:
        runtime = admin_runtime()
        if isinstance(runtime, JSONResponse):
            return runtime
        if not trusted_origin(request, runtime):
            return _error("FORBIDDEN", 403)
        if request.headers.get("X-Requested-With") != "crm":
            return _error("FORBIDDEN", 403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("UNSUPPORTED_MEDIA_TYPE", 415)
        try:
            runtime.login_attempt_limiter.check("admin-session-login", request)
        except LoginRateLimited:
            return _error("RATE_LIMITED", 429)
        try:
            payload = await _json_object(request)
        except InquiryCommandError:
            return _error("INVALID_REQUEST", 400)
        if set(payload) != {"login", "password"} or not isinstance(payload["login"], str) or not isinstance(payload["password"], str):
            return _error("INVALID_REQUEST", 400)
        result = runtime.login(payload["login"], payload["password"])
        if result is None:
            return private_unauthorized()
        token, owner_id, csrf_token, expires_at = result
        response = JSONResponse({"data": {"owner_id": owner_id, "csrf_token": csrf_token, "expires_at": expires_at.isoformat()}}, headers=_NO_STORE)
        response.set_cookie(COOKIE_NAME, token, max_age=max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds())), secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/api/v1/admin/session", tags=["admin"])
    def read_admin_session(request: Request) -> JSONResponse:
        runtime = admin_runtime()
        if isinstance(runtime, JSONResponse):
            return runtime
        token = request.cookies.get(COOKIE_NAME)
        session = runtime.active_session(token) if token else None
        if session is None:
            return private_unauthorized()
        return JSONResponse({"data": {"owner_id": str(session["owner_id"]), "csrf_token": session["csrf_secret"].decode("ascii"), "expires_at": session["expires_at"].isoformat()}}, headers=_NO_STORE)

    @app.delete("/api/v1/admin/session", tags=["admin"])
    def sign_out_admin_session(request: Request) -> JSONResponse:
        runtime = admin_runtime()
        if isinstance(runtime, JSONResponse):
            return runtime
        if not trusted_origin(request, runtime):
            return _error("FORBIDDEN", 403)
        token = request.cookies.get(COOKIE_NAME)
        session = runtime.active_session(token) if token else None
        if session is None:
            return private_unauthorized()
        csrf_token = request.headers.get("X-CSRF-Token")
        try:
            csrf_valid = hmac.compare_digest(session["csrf_secret"], csrf_token.encode("ascii"))
        except (AttributeError, UnicodeEncodeError):
            csrf_valid = False
        if not csrf_valid:
            return _error("CSRF_FAILED", 403)
        if not runtime.revoke(token, csrf_token):
            return private_unauthorized()
        response = JSONResponse({"data": {"signed_out": True}}, headers=_NO_STORE)
        response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="lax", path="/")
        return response

    return app


app = create_app()
