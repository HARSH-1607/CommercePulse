"""Deterministic fictional retail data for the local demo only."""
from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Order, OrderItem, Product, StockMovement

CATALOG = [
    ("BREW-MINI", "Brew Mini", "Coffee makers", 3900, 8900, 24, 16, 8),
    ("BREW-PRO", "Brew Pro", "Coffee makers", 7900, 16900, 15, 64, 12),
    ("BEANS-HOUSE", "House Blend Beans", "Coffee", 1100, 2400, 45, 210, 5),
    ("BEANS-DARK", "Dark Roast Beans", "Coffee", 1200, 2600, 40, 155, 5),
    ("GRINDER-X", "Precision Grinder", "Accessories", 3200, 7400, 20, 46, 10),
    ("FILTER-100", "Paper Filters 100pk", "Accessories", 300, 900, 35, 120, 4),
]


def seed_demo(db: Session) -> None:
    if db.scalar(select(func.count(Product.id))) != 0:
        return
    rng = random.Random(17)
    rows: list[tuple[str, date, str, list[tuple[str, int, int]]]] = []
    units = Counter()
    first_day = date.today() - timedelta(days=89)
    for day_index in range(90):
        day = first_day + timedelta(days=day_index)
        for order_index in range(3 + day_index // 45):
            chosen = rng.sample(CATALOG, k=rng.randint(1, 3))
            items = []
            for sku, _, _, _, price, _, _, _ in chosen:
                quantity = rng.randint(1, 3)
                if day_index == 78:
                    quantity *= 5  # Deliberate demand spike for alert demonstration.
                items.append((sku, quantity, price))
                units[sku] += quantity
            rows.append((f"DEMO-{day_index:03d}-{order_index:02d}", day, rng.choice(["Web", "Marketplace", "Retail"]), items))
    products = {}
    for sku, name, category, cost, _, reorder, ending_stock, lead in CATALOG:
        opening_stock = units[sku] + ending_stock
        product = Product(sku=sku, name=name, category=category, unit_cost_cents=cost,
                          stock_on_hand=opening_stock, reorder_point=reorder, lead_time_days=lead)
        db.add(product)
        db.flush()
        db.add(StockMovement(product_id=product.id, delta=opening_stock, reason="opening", reference="DEMO-SEED"))
        products[sku] = product
    for external_id, day, channel, items in rows:
        order = Order(external_id=external_id, ordered_on=day, channel=channel)
        db.add(order)
        db.flush()
        for sku, quantity, price in items:
            product = products[sku]
            db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price_cents=price))
            product.stock_on_hand -= quantity
            db.add(StockMovement(product_id=product.id, delta=-quantity, reason="sale", reference=external_id))
    db.commit()
