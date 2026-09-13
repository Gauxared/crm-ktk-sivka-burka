from sivka_burka_api.inquiries import _canonical_payload, _payload_digest


def test_payload_canonicalization_ignores_transport_token_and_unknown_acquisition():
    base = {
        "submission_token": "token-a",
        "catalog_version": 1,
        "service_option_id": "ride-60",
        "requester": {"name": " Client ", "contact": {"kind": "phone", "value": "+70000000000"}},
        "participants_count": 2,
        "requested_time": {"date": "2026-10-01", "time_text": " after 14:00 "},
        "acquisition": {"utm_source": " form ", "ignored": "discard"},
    }
    changed_token = {**base, "submission_token": "token-b"}
    assert _canonical_payload(base)["submission_token"] == "token-a"
    assert _canonical_payload(base)["acquisition"] == {"utm_source": "form"}
    assert _payload_digest(b"secret", _canonical_payload(base)) == _payload_digest(b"secret", _canonical_payload(changed_token))
