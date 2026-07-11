from datetime import UTC, datetime


def test_startup_and_health(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["trading_mode"] == "paper"
    assert response.json()["live_trading_enabled"] is False


def test_protected_write_requires_api_key(client) -> None:  # type: ignore[no-untyped-def]
    response = client.post("/api/v1/risk/emergency-stop")
    assert response.status_code == 401


def test_emergency_stop_blocks_proposal(client, auth_headers) -> None:  # type: ignore[no-untyped-def]
    assert client.post("/api/v1/risk/emergency-stop", headers=auth_headers).status_code == 200
    payload = {
        "idempotency_key": "api-order-123",
        "symbol": "GP",
        "side": "buy",
        "quantity": 10,
        "limit_price": "100",
        "current_price": "100",
        "data_timestamp": datetime.now(UTC).isoformat(),
        "average_daily_volume": 100000,
    }
    response = client.post("/api/v1/orders/proposals", json=payload, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "risk_rejected"


def test_mock_backtest_endpoint(client) -> None:  # type: ignore[no-untyped-def]
    response = client.post("/api/v1/backtests", json={"symbol": "GP", "strategy": "buy_hold"})
    assert response.status_code == 200
    assert response.json()["assumptions"]["next_bar_execution"] is True


def test_order_market_facts_are_rederived_server_side(client, auth_headers) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "idempotency_key": "api-server-validation-1",
        "symbol": "ACI",
        "side": "buy",
        "quantity": 10,
        "limit_price": "1",
        "current_price": "1",
        "data_timestamp": datetime.now(UTC).isoformat(),
        "data_quality_status": "valid",
    }
    response = client.post("/api/v1/orders/proposals", json=payload, headers=auth_headers)
    assert response.status_code == 200
    snapshot = response.json()["risk_decision"]["input_snapshot"]
    assert snapshot["current_price"] != "1"
    assert "PRICE_DEVIATION" in response.json()["risk_decision"]["reason_codes"]
