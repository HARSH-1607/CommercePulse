from __future__ import annotations

import csv
import io
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.staticfiles import StaticFiles

from .analytics import alerts, forecasts, overview
from .db import Base, Order, OrderItem, Product, StockMovement, make_engine, make_session_factory
from .seed import seed_demo

REQUIRED_COLUMNS = {"order_id", "ordered_on", "sku", "quantity", "unit_price", "channel"}


class ProductIn(BaseModel):
    sku: str = Field(min_length=2, max_length=40, pattern=r"^[A-Z0-9-]+$")
    name: str = Field(min_length=3, max_length=120)
    category: str = Field(min_length=2, max_length=60)
    unit_cost_cents: int = Field(ge=0, le=100_000_000)
    initial_stock: int = Field(ge=0, le=1_000_000)
    reorder_point: int = Field(ge=0, le=100_000)
    lead_time_days: int = Field(ge=1, le=365)

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value: str) -> str:
        return value.upper()


class AdjustmentIn(BaseModel):
    delta: int = Field(ge=-100_000, le=100_000)
    reason: str = Field(pattern=r"^(restock|correction|damage)$")
    note: str = Field(default="", max_length=100)


class ImportIn(BaseModel):
    csv: str


def product_dict(product: Product) -> dict:
    return {"id": product.id, "sku": product.sku, "name": product.name, "category": product.category,
            "unit_cost_cents": product.unit_cost_cents, "stock_on_hand": product.stock_on_hand,
            "reorder_point": product.reorder_point, "lead_time_days": product.lead_time_days}


def _parse_price(raw: str, row_number: int) -> int:
    try:
        amount = Decimal(raw.strip())
        cents = amount * 100
        if not cents.is_finite() or cents != cents.to_integral_value() or not 0 < cents <= 100_000_000:
            raise ValueError()
        return int(cents)
    except (InvalidOperation, ValueError, AttributeError):
        raise HTTPException(status_code=422, detail=f"Row {row_number}: unit_price must be a positive amount with at most two decimals") from None


def parse_csv(content: str) -> dict[str, dict]:
    if len(content.encode("utf-8")) > 2_000_000:
        raise HTTPException(status_code=413, detail="CSV exceeds 2 MB")
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        raise HTTPException(status_code=422, detail="CSV needs columns: order_id, ordered_on, sku, quantity, unit_price, channel")
    orders: dict[str, dict] = {}
    count = 0
    for row_number, row in enumerate(reader, start=2):
        count += 1
        if count > 10_000:
            raise HTTPException(status_code=413, detail="Maximum 10,000 order lines per import")
        try:
            external_id = row["order_id"].strip()
            ordered_on = date.fromisoformat(row["ordered_on"].strip())
            sku = row["sku"].strip().upper()
            quantity = int(row["quantity"])
            channel = row["channel"].strip()
            if not 1 <= len(external_id) <= 80 or not re.fullmatch(r"[A-Z0-9-]{2,40}", sku):
                raise ValueError()
            if not 1 <= quantity <= 10_000 or not 1 <= len(channel) <= 40 or ordered_on > date.today():
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=422, detail=f"Row {row_number}: invalid order ID, date, SKU, quantity, or channel") from None
        cents = _parse_price(row["unit_price"], row_number)
        group = orders.setdefault(external_id, {"ordered_on": ordered_on, "channel": channel, "items": {}})
        if group["ordered_on"] != ordered_on or group["channel"] != channel or sku in group["items"]:
            raise HTTPException(status_code=422, detail=f"Row {row_number}: conflicting order details or duplicate SKU")
        group["items"][sku] = (quantity, cents)
    if not orders:
        raise HTTPException(status_code=422, detail="CSV has no order lines")
    return orders


def import_orders(db: Session, content: str) -> dict:
    parsed = parse_csv(content)
    skus = {sku for order in parsed.values() for sku in order["items"]}
    products = {product.sku: product for product in db.scalars(select(Product).where(Product.sku.in_(skus)).with_for_update()).all()}
    missing = sorted(skus - products.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown SKU(s): {', '.join(missing[:10])}")
    added_orders = added_lines = duplicates = 0
    try:
        for external_id, data in parsed.items():
            existing = db.scalar(select(Order).where(Order.external_id == external_id))
            if existing:
                existing_items = {db.get(Product, item.product_id).sku: (item.quantity, item.unit_price_cents) for item in existing.items}
                if existing.ordered_on != data["ordered_on"] or existing.channel != data["channel"] or existing_items != data["items"]:
                    raise HTTPException(status_code=409, detail=f"Order {external_id} already exists with different data")
                duplicates += 1
                continue
            for sku, (quantity, _) in data["items"].items():
                if products[sku].stock_on_hand < quantity:
                    raise HTTPException(status_code=422, detail=f"Insufficient stock for {sku} in order {external_id}")
            order = Order(external_id=external_id, ordered_on=data["ordered_on"], channel=data["channel"])
            db.add(order)
            db.flush()
            for sku, (quantity, price_cents) in data["items"].items():
                product = products[sku]
                product.stock_on_hand -= quantity
                db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price_cents=price_cents))
                db.add(StockMovement(product_id=product.id, delta=-quantity, reason="sale", reference=external_id))
                added_lines += 1
            added_orders += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"orders_added": added_orders, "lines_added": added_lines, "duplicates_ignored": duplicates,
            "orders_received": len(parsed)}


def create_app(database_url: str | None = None, demo: bool | None = None) -> FastAPI:
    engine = make_engine(database_url)
    SessionLocal = make_session_factory(engine)
    demo_enabled = demo if demo is not None else engine.url.get_backend_name() == "sqlite" and os.environ.get("COMMERCEPULSE_DEMO", "0") == "1"
    public_demo = os.environ.get("COMMERCEPULSE_PUBLIC_DEMO", "0") == "1"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Base.metadata.create_all(engine)
        if demo_enabled:
            with SessionLocal() as db:
                seed_demo(db)
        yield
        engine.dispose()

    app = FastAPI(title="CommercePulse", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def protect_public_demo(request: Request, call_next):
        if public_demo and request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse(status_code=403, content={"detail": "This public demo is read-only"})
        return await call_next(request)

    def get_db():
        with SessionLocal() as db:
            yield db

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {"demo": demo_enabled, "read_only": public_demo, "currency": "USD"}

    @app.get("/api/products")
    def products(db: Session = Depends(get_db)):
        return [product_dict(product) for product in db.scalars(select(Product).order_by(Product.name)).all()]

    @app.post("/api/products", status_code=201)
    def create_product(payload: ProductIn, db: Session = Depends(get_db)):
        sku = payload.sku.upper()
        if db.scalar(select(Product.id).where(Product.sku == sku)):
            raise HTTPException(status_code=409, detail="SKU already exists")
        product = Product(sku=sku, name=payload.name.strip(), category=payload.category.strip(),
                          unit_cost_cents=payload.unit_cost_cents, stock_on_hand=payload.initial_stock,
                          reorder_point=payload.reorder_point, lead_time_days=payload.lead_time_days)
        if len(product.name) < 3 or len(product.category) < 2:
            raise HTTPException(status_code=422, detail="Name or category is too short")
        db.add(product)
        try:
            db.flush()
            if payload.initial_stock:
                db.add(StockMovement(product_id=product.id, delta=payload.initial_stock, reason="opening", reference="PRODUCT-CREATED"))
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="SKU already exists") from None
        return product_dict(product)

    @app.post("/api/products/{product_id}/adjust")
    def adjust_stock(product_id: int, payload: AdjustmentIn, db: Session = Depends(get_db)):
        product = db.scalar(select(Product).where(Product.id == product_id).with_for_update())
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        if payload.delta == 0 or product.stock_on_hand + payload.delta < 0:
            raise HTTPException(status_code=422, detail="Adjustment must be nonzero and cannot make stock negative")
        product.stock_on_hand += payload.delta
        db.add(StockMovement(product_id=product.id, delta=payload.delta, reason=payload.reason,
                             reference=payload.note.strip() or "MANUAL"))
        db.commit()
        return product_dict(product)

    @app.get("/api/products/{product_id}/movements")
    def movements(product_id: int, db: Session = Depends(get_db)):
        if not db.get(Product, product_id):
            raise HTTPException(status_code=404, detail="Product not found")
        rows = db.scalars(select(StockMovement).where(StockMovement.product_id == product_id)
                          .order_by(StockMovement.id.desc()).limit(100)).all()
        return [{"id": row.id, "delta": row.delta, "reason": row.reason, "reference": row.reference,
                 "created_at": row.created_at} for row in rows]

    @app.get("/api/orders")
    def recent_orders(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
        orders = db.scalars(select(Order).order_by(Order.ordered_on.desc(), Order.id.desc()).limit(limit)).all()
        return [{"order_id": order.external_id, "ordered_on": order.ordered_on, "channel": order.channel,
                 "lines": len(order.items), "units": sum(item.quantity for item in order.items),
                 "total_cents": sum(item.quantity * item.unit_price_cents for item in order.items)} for order in orders]

    @app.get("/api/orders/template", response_class=PlainTextResponse)
    def csv_template():
        return f"order_id,ordered_on,sku,quantity,unit_price,channel\nEXAMPLE-001,{date.today().isoformat()},BREW-MINI,1,89.00,Web\n"

    @app.post("/api/orders/import")
    def upload_orders(payload: ImportIn, db: Session = Depends(get_db)):
        return import_orders(db, payload.csv)

    @app.get("/api/reports/overview")
    def report(days: int = Query(default=30, ge=7, le=90), db: Session = Depends(get_db)):
        return overview(db, days)

    @app.get("/api/forecast")
    def demand_forecast(db: Session = Depends(get_db)):
        return forecasts(db)

    @app.get("/api/alerts")
    def unusual_sales(db: Session = Depends(get_db)):
        return alerts(db)

    frontend_dist = Path(os.environ.get("FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
    if frontend_dist.exists():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            candidate = frontend_dist / path
            if path and candidate.is_file() and candidate.resolve().is_relative_to(frontend_dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")
    return app


app = create_app()
