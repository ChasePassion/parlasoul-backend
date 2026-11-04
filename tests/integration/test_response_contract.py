def test_success_response_envelope(client) -> None:
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ok"
    assert body["message"] == "ok"
    assert body["status"] == 200
    assert body["data"] == {"status": "ok"}


def test_api_error_response_envelope(client) -> None:
    resp = client.get("/boom")

    assert resp.status_code == 409
    body = resp.json()
    assert body == {
        "code": "conflict_demo",
        "message": "resource conflict: demo conflict",
        "status": 409,
    }


def test_validation_error_response_envelope(client) -> None:
    resp = client.post("/validate", json={})

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_failed"
    assert body["status"] == 422
    assert "validation failed" in body["message"]

