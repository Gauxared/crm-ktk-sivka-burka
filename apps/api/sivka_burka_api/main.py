"""FastAPI application entry point."""

import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
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
from .settings import load_settings


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


def create_app(*, public_submission_runtime: PublicSubmissionRuntime | None = None, public_catalog_runtime: PublicCatalogRuntime | None = None, admin_session_runtime: AdminSessionRuntime | None = None) -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Sivka-Burka API", version="0.1.0")

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        """Report process liveness only; this endpoint intentionally does not touch PostgreSQL."""
        return {"status": "ok", "service": "sivka-burka-api", "environment": settings.environment}

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
        return _error("ADMIN_SESSION_UNAUTHORIZED", 401)

    @app.post("/api/v1/admin/session", tags=["admin"])
    async def admin_login(request: Request) -> JSONResponse:
        runtime = admin_runtime()
        if isinstance(runtime, JSONResponse):
            return runtime
        if not trusted_origin(request, runtime) or request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json" or request.headers.get("X-Requested-With") != "crm":
            return _error("INVALID_REQUEST", 400)
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
        token, csrf_token = request.cookies.get(COOKIE_NAME), request.headers.get("X-CSRF-Token")
        if not trusted_origin(request, runtime) or not token or not csrf_token or not runtime.revoke(token, csrf_token):
            return private_unauthorized()
        response = JSONResponse({"data": {"signed_out": True}}, headers=_NO_STORE)
        response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="lax", path="/")
        return response

    return app


app = create_app()
