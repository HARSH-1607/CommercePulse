"""Parameterized SQL reports and evaluated, local demand forecasts."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

import numpy as np
from sklearn.linear_model import Ridge
from sqlalchemy import text
from sqlalchemy.orm import Session


def _date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def overview(db: Session, days: int = 30) -> dict:
    start = date.today() - timedelta(days=days - 1)
    previous_start = start - timedelta(days=days)
    params = {"start": start, "previous_start": previous_start}
    totals = db.execute(text("""
        SELECT COUNT(DISTINCT o.id) AS orders,
               COALESCE(SUM(oi.quantity), 0) AS units,
               COALESCE(SUM(oi.quantity * oi.unit_price_cents), 0) AS revenue_cents,
               COALESCE(SUM(oi.quantity * (oi.unit_price_cents - p.unit_cost_cents)), 0) AS gross_margin_cents
        FROM orders o JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.ordered_on >= :start
    """), params).mappings().one()
    previous_revenue = db.execute(text("""
        SELECT COALESCE(SUM(oi.quantity * oi.unit_price_cents), 0)
        FROM orders o JOIN order_items oi ON oi.order_id = o.id
        WHERE o.ordered_on >= :previous_start AND o.ordered_on < :start
    """), params).scalar_one()
    daily_rows = db.execute(text("""
        SELECT o.ordered_on AS day, COUNT(DISTINCT o.id) AS orders,
               SUM(oi.quantity) AS units,
               SUM(oi.quantity * oi.unit_price_cents) AS revenue_cents
        FROM orders o JOIN order_items oi ON oi.order_id = o.id
        WHERE o.ordered_on >= :start
        GROUP BY o.ordered_on ORDER BY o.ordered_on
    """), {"start": start}).mappings().all()
    daily_map = {_date(row["day"]).isoformat(): dict(row) for row in daily_rows}
    daily = []
    for index in range(days):
        day = (start + timedelta(days=index)).isoformat()
        row = daily_map.get(day, {})
        daily.append({"day": day, "orders": row.get("orders", 0), "units": row.get("units", 0),
                      "revenue_cents": row.get("revenue_cents", 0)})
    for index, row in enumerate(daily):
        row["rolling_7d_cents"] = round(sum(x["revenue_cents"] for x in daily[max(0, index-6):index+1]) / min(index+1, 7))
    categories = [dict(row) for row in db.execute(text("""
        SELECT p.category, SUM(oi.quantity) AS units,
               SUM(oi.quantity * oi.unit_price_cents) AS revenue_cents
        FROM orders o JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.ordered_on >= :start
        GROUP BY p.category ORDER BY revenue_cents DESC
    """), {"start": start}).mappings()]
    top_products = [dict(row) for row in db.execute(text("""
        WITH product_sales AS (
          SELECT p.sku, p.name, SUM(oi.quantity) AS units,
                 SUM(oi.quantity * oi.unit_price_cents) AS revenue_cents
          FROM orders o JOIN order_items oi ON oi.order_id = o.id
          JOIN products p ON p.id = oi.product_id
          WHERE o.ordered_on >= :start
          GROUP BY p.id, p.sku, p.name
        )
        SELECT sku, name, units, revenue_cents,
               ROW_NUMBER() OVER (ORDER BY revenue_cents DESC) AS rank
        FROM product_sales ORDER BY rank LIMIT 5
    """), {"start": start}).mappings()]
    inventory = db.execute(text("""
        SELECT COALESCE(SUM(stock_on_hand * unit_cost_cents), 0) AS value_cents,
               COALESCE(SUM(CASE WHEN stock_on_hand <= reorder_point THEN 1 ELSE 0 END), 0) AS below_reorder
        FROM products
    """)).mappings().one()
    revenue = int(totals["revenue_cents"])
    return {
        "days": days, "orders": int(totals["orders"]), "units": int(totals["units"]),
        "revenue_cents": revenue, "gross_margin_cents": int(totals["gross_margin_cents"]),
        "average_order_cents": round(revenue / totals["orders"]) if totals["orders"] else 0,
        "revenue_change_pct": round((revenue - previous_revenue) / previous_revenue * 100, 1) if previous_revenue else None,
        "inventory_value_cents": int(inventory["value_cents"]), "below_reorder": int(inventory["below_reorder"]),
        "daily": daily, "categories": categories, "top_products": top_products,
    }


def _features(indices: np.ndarray, start: date) -> np.ndarray:
    weekdays = np.array([(start + timedelta(days=int(i))).weekday() for i in indices])
    one_hot = np.eye(7)[weekdays]
    return np.column_stack([indices / 30.0, one_hot])


def _forecast_series(values: list[int], start: date) -> tuple[list[int], str, float | None]:
    count = len(values)
    if count < 21:
        average = round(sum(values[-7:]) / max(1, min(7, count)))
        return [average] * 7, "seven_day_average", None
    train, actual = np.asarray(values[:-14], dtype=float), np.asarray(values[-14:], dtype=float)
    model = Ridge(alpha=4.0)
    model.fit(_features(np.arange(len(train)), start), train)
    model_pred = np.maximum(0, model.predict(_features(np.arange(len(train), count), start)))
    seasonal_pred = np.asarray([train[-7 + (i % 7)] for i in range(14)])
    model_mae = float(np.mean(np.abs(actual - model_pred)))
    seasonal_mae = float(np.mean(np.abs(actual - seasonal_pred)))
    if model_mae < seasonal_mae * 0.95:
        model.fit(_features(np.arange(count), start), np.asarray(values, dtype=float))
        future = model.predict(_features(np.arange(count, count + 7), start))
        return [max(0, round(float(v))) for v in future], "ridge_trend_weekday", round(model_mae, 2)
    return [int(v) for v in values[-7:]], "seasonal_naive", round(seasonal_mae, 2)


def forecasts(db: Session) -> list[dict]:
    products = db.execute(text("""
        SELECT id, sku, name, category, stock_on_hand, reorder_point, lead_time_days
        FROM products ORDER BY name
    """)).mappings().all()
    if not products:
        return []
    start = date.today() - timedelta(days=89)
    rows = db.execute(text("""
        SELECT oi.product_id, o.ordered_on AS day, SUM(oi.quantity) AS units
        FROM orders o JOIN order_items oi ON oi.order_id = o.id
        WHERE o.ordered_on >= :start
        GROUP BY oi.product_id, o.ordered_on
    """), {"start": start}).mappings().all()
    by_product = {(row["product_id"], _date(row["day"])): int(row["units"]) for row in rows}
    results = []
    for product in products:
        values = [by_product.get((product["id"], start + timedelta(days=i)), 0) for i in range(90)]
        predicted, method, validation_mae = _forecast_series(values, start)
        weekly = sum(predicted)
        daily_rate = weekly / 7
        stock = int(product["stock_on_hand"])
        threshold = round(daily_rate * product["lead_time_days"]) + product["reorder_point"]
        results.append({
            "sku": product["sku"], "name": product["name"], "category": product["category"],
            "stock_on_hand": stock, "reorder_point": product["reorder_point"],
            "lead_time_days": product["lead_time_days"], "predicted_units_7d": weekly,
            "daily_forecast": predicted, "method": method, "validation_mae_units": validation_mae,
            "days_of_cover": round(stock / daily_rate, 1) if daily_rate > 0 else None,
            "reorder_suggested": stock <= threshold,
            "suggested_restock_units": max(0, threshold + weekly - stock),
        })
    return sorted(results, key=lambda row: (not row["reorder_suggested"], row["days_of_cover"] if row["days_of_cover"] is not None else 9999))


def alerts(db: Session) -> dict:
    forecast_rows = forecasts(db)
    inventory = [{"sku": row["sku"], "name": row["name"], "stock_on_hand": row["stock_on_hand"],
                  "days_of_cover": row["days_of_cover"], "suggested_restock_units": row["suggested_restock_units"]}
                 for row in forecast_rows if row["reorder_suggested"]]
    history = overview(db, 90)["daily"]
    sales = []
    for index in range(14, len(history)):
        baseline = [row["revenue_cents"] for row in history[index-14:index]]
        value = history[index]["revenue_cents"]
        mid = median(baseline)
        mad = median([abs(x - mid) for x in baseline])
        scale = max(mad, mid * 0.1, 100)
        score = 0.6745 * (value - mid) / scale
        if abs(score) >= 3.5 and abs(value - mid) >= max(mid * 0.4, 5000):
            sales.append({"day": history[index]["day"], "kind": "spike" if score > 0 else "drop",
                          "revenue_cents": value, "baseline_cents": round(mid), "score": round(score, 1)})
    return {"inventory": inventory, "sales": sorted(sales, key=lambda row: row["day"], reverse=True)[:8]}
