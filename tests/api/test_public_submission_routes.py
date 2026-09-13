from fastapi.testclient import TestClient
import pytest

from sivka_burka_api.main import create_app
from sivka_burka_api.public_submission import PublicSubmissionRuntime, SubmissionRateLimitError


def test_public_submission_is_closed_without_injected_runtime(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    client = TestClient(create_app())

    assert client.post("/api/v1/public/submission-tokens", json={}).json() == {
        "error": {"code": "PUBLIC_SUBMISSION_UNAVAILABLE"}
    }
    assert client.post("/api/v1/public/inquiries", json={}).status_code == 503


def test_rate_limit_adapter_rejection_is_a_private_stable_error(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")

    class RejectingLimiter:
        def check(self, operation, request):
            raise SubmissionRateLimitError()

    class Runtime:
        rate_limiter = RejectingLimiter()

    client = TestClient(create_app(public_submission_runtime=Runtime()))
    response = client.post("/api/v1/public/submission-tokens", json={})

    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"code": "RATE_LIMITED"}}


def test_runtime_rejects_missing_hmac_or_rate_limit_dependency() -> None:
    with pytest.raises(ValueError, match="hmac_secret"):
        PublicSubmissionRuntime(object(), b"", object())
    with pytest.raises(ValueError, match="rate_limiter"):
        PublicSubmissionRuntime(object(), b"synthetic", None)
