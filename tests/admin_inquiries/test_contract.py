"""Route and cursor contracts that do not require a configured database."""

from fastapi.testclient import TestClient
import pytest

from sivka_burka_api.admin_inquiries import InvalidInquiryCursor, _cursor_decode, _cursor_encode, _json
from sivka_burka_api.main import create_app


class MissingSession:
    def active_session(self, token):
        return None


def test_owner_read_routes_hide_data_without_a_valid_session():
    client = TestClient(create_app(admin_session_runtime=MissingSession()))
    for path in ("/api/v1/admin/inquiries", "/api/v1/admin/inquiries/not-a-uuid"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "AUTH_REQUIRED"}}
        assert response.headers["cache-control"] == "no-store"


def test_cursor_is_opaque_and_bound_to_every_active_filter():
    filters = {"status": "NEW", "has_visit": False, "channel": "PHONE"}
    cursor = _cursor_encode({"filters": filters, "received_at": "2026-01-02T03:04:05+00:00", "id": "12345678-1234-5678-1234-567812345678"})
    received_at, inquiry_id = _cursor_decode(cursor, filters)
    assert received_at.isoformat() == "2026-01-02T03:04:05+00:00"
    assert str(inquiry_id) == "12345678-1234-5678-1234-567812345678"
    with pytest.raises(InvalidInquiryCursor):
        _cursor_decode(cursor, {**filters, "status": "CONFIRMED"})


@pytest.mark.parametrize(("value", "expected"), [(b'{"source":"synthetic"}', {"source": "synthetic"}), ('{"source":"synthetic"}', {"source": "synthetic"}), ("not-json", {})])
def test_jsonb_read_values_are_safe_when_a_driver_returns_text(value, expected):
    assert _json(value) == expected
