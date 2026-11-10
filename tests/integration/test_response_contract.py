def test_api_error_response_envelope(client) -> None:
    resp = client.get("/boom")

    assert resp.status_code == 409
    body = resp.json()
    assert body == {
        "code": "conflict_demo",
        "message": "resource conflict: demo conflict",
        "status": 409,
    }


