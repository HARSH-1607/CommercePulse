from datetime import date
from fastapi.testclient import TestClient
from app.main import create_app


def client_for(tmp_path, demo=False):
    return TestClient(create_app(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", demo=demo))


def product(sku="TEST-1", stock=20):
    return {"sku": sku, "name": "Test Product", "category": "Testing", "unit_cost_cents": 500,
            "initial_stock": stock, "reorder_point": 5, "lead_time_days": 7}


def csv_row(order_id, sku="TEST-1", qty=2, price="10.50"):
    return f"{order_id},{date.today().isoformat()},{sku},{qty},{price},Web"


def csv_file(*rows):
    return "order_id,ordered_on,sku,quantity,unit_price,channel\n" + "\n".join(rows) + "\n"


def test_demo_has_reports_forecasts_and_alerts(tmp_path):
    with client_for(tmp_path, demo=True) as client:
        assert client.get("/api/config").json()["demo"] is True
        products = client.get("/api/products").json()
        assert len(products) == 6
        report = client.get("/api/reports/overview?days=30").json()
        assert report["orders"] >= 90 and report["revenue_cents"] > 0
        assert len(report["daily"]) == 30 and report["top_products"][0]["rank"] == 1
        forecast = client.get("/api/forecast").json()
        assert len(forecast) == 6 and all(len(row["daily_forecast"]) == 7 for row in forecast)
        alerts = client.get("/api/alerts").json()
        assert alerts["inventory"] and any(row["kind"] == "spike" for row in alerts["sales"])


def test_public_demo_blocks_writes_but_keeps_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMERCEPULSE_PUBLIC_DEMO", "1")
    with client_for(tmp_path, demo=True) as client:
        assert client.get("/api/config").json()["read_only"] is True
        before = client.get("/api/products").json()
        assert client.post("/api/products", json=product()).status_code == 403
        assert client.post("/api/orders/import", json={"csv": csv_file(csv_row("NEW-1"))}).status_code == 403
        assert client.get("/api/products").json() == before
        assert client.get("/api/reports/overview").status_code == 200


def test_import_is_idempotent_and_atomic(tmp_path):
    with client_for(tmp_path) as client:
        created = client.post("/api/products", json=product()).json()
        data = csv_file(csv_row("A-1"), csv_row("A-2", qty=3))
        first = client.post("/api/orders/import", json={"csv": data})
        assert first.status_code == 200 and first.json()["orders_added"] == 2
        assert client.get("/api/products").json()[0]["stock_on_hand"] == 15
        second = client.post("/api/orders/import", json={"csv": data})
        assert second.json()["duplicates_ignored"] == 2
        assert client.get("/api/products").json()[0]["stock_on_hand"] == 15
        mixed = csv_file(csv_row("A-3", qty=1), csv_row("A-4", qty=99))
        failed = client.post("/api/orders/import", json={"csv": mixed})
        assert failed.status_code == 422
        assert client.get("/api/products").json()[0]["stock_on_hand"] == 15
        assert len(client.get("/api/orders").json()) == 2
        assert client.post("/api/orders/import", json={"csv": csv_file(csv_row("A-1", qty=7))}).status_code == 409
        report = client.get("/api/reports/overview?days=7").json()
        assert report["orders"] == 2 and report["units"] == 5 and report["revenue_cents"] == 5250
        assert report["gross_margin_cents"] == 2750
        assert created["sku"] == "TEST-1"


def test_stock_adjustments_have_ledger(tmp_path):
    with client_for(tmp_path) as client:
        item = client.post("/api/products", json=product(stock=10)).json()
        adjusted = client.post(f"/api/products/{item['id']}/adjust", json={"delta": -3, "reason": "damage", "note": "Broken box"})
        assert adjusted.status_code == 200 and adjusted.json()["stock_on_hand"] == 7
        ledger = client.get(f"/api/products/{item['id']}/movements").json()
        assert [row["delta"] for row in ledger] == [-3, 10]
        assert client.post(f"/api/products/{item['id']}/adjust", json={"delta": -8, "reason": "correction"}).status_code == 422
        assert client.post("/api/products", json=product()).status_code == 409


def test_invalid_csv_does_not_mutate_data(tmp_path):
    with client_for(tmp_path) as client:
        client.post("/api/products", json=product())
        bad = csv_file(csv_row("A-1"), csv_row("A-1", qty=3))
        assert client.post("/api/orders/import", json={"csv": bad}).status_code == 422
        assert client.post("/api/orders/import", json={"csv": csv_file(csv_row("B-1", sku="UNKNOWN"))}).status_code == 422
        assert client.get("/api/products").json()[0]["stock_on_hand"] == 20
        assert client.get("/api/orders").json() == []
