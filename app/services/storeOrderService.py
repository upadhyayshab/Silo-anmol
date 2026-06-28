"""Native-Medusa → internal-CRM integration (parallel to the LeadSquared path).

Pure parsers map Medusa's admin order/customer objects to a flat dict the
orchestrators consume. Field paths target Medusa v2 — confirm against a real
webhook sample (money units especially: v2 sends decimal amounts, not cents).
"""
from decimal import Decimal
from typing import Optional


def _name(first: Optional[str], last: Optional[str], fallback: Optional[str]) -> str:
    full = f"{first or ''} {last or ''}".strip()
    return full or (fallback or "Unknown Customer")


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(default)
    except Exception:
        return Decimal(default)


def parse_medusa_order(payload: dict) -> dict:
    addr = payload.get("shipping_address") or payload.get("billing_address") or {}
    items = []
    for raw in payload.get("items") or []:
        variant = raw.get("variant") or {}
        sku = variant.get("sku") or raw.get("variant_sku") or raw.get("sku")
        if not sku:
            continue
        items.append({
            "sku": sku,
            "quantity": int(raw.get("quantity", 1) or 1),
            "unit_price": _dec(raw.get("unit_price")),
            "title": raw.get("title"),
        })
    return {
        "medusa_id": payload.get("id"),
        "customer_name": _name(addr.get("first_name"), addr.get("last_name"),
                               payload.get("email")),
        "mobile": addr.get("phone") or (payload.get("customer") or {}).get("phone"),
        "phone": addr.get("phone"),
        "email": payload.get("email"),
        "address_line": addr.get("address_1"),
        "address_line_2": addr.get("address_2"),
        "city": addr.get("city"),
        "district": addr.get("city"),
        "state": addr.get("province"),
        "pincode": addr.get("postal_code"),
        "items": items,
        "total": _dec(payload.get("total")),
        "payment_status": payload.get("payment_status"),
    }


def parse_medusa_customer(payload: dict) -> dict:
    addr = (payload.get("addresses") or [{}])
    addr = addr[0] if addr else {}
    return {
        "customer_name": _name(payload.get("first_name"), payload.get("last_name"),
                               payload.get("email")),
        "mobile": payload.get("phone") or addr.get("phone"),
        "phone": payload.get("phone"),
        "email": payload.get("email"),
        "address_line": addr.get("address_1"),
        "address_line_2": addr.get("address_2"),
        "city": addr.get("city"),
        "district": addr.get("city"),
        "state": addr.get("province"),
        "pincode": addr.get("postal_code"),
    }


# ---------------------------------------------------------------------------
# Orchestrators (Task 2 additions below)
# ---------------------------------------------------------------------------

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _lead_payload(parsed: dict):
    """Build a LeadCreateRequest from a parsed Medusa order/customer dict."""
    from models import LeadCreateRequest
    from utils.crm_constants import LeadSource

    name = (parsed.get("customer_name") or "Unknown Customer").split(" ", 1)
    return LeadCreateRequest(
        first_name=name[0] or "Unknown",
        last_name=name[1] if len(name) > 1 else None,
        mobile=parsed["mobile"],
        phone=parsed.get("phone"),
        email=parsed.get("email"),
        address_line=parsed.get("address_line"),
        address_line_2=parsed.get("address_line_2"),
        city=parsed.get("city"),
        district=parsed.get("district"),
        state=parsed.get("state"),
        pincode=parsed.get("pincode"),
        source=LeadSource.WEB_VISIT_LOGIN,
    )


async def process_store_lead(engine, payload: dict) -> None:
    """Medusa customer.* → upsert a bare internal lead (no order)."""
    import services.leadService as leadService
    parsed = parse_medusa_customer(payload)
    if not parsed.get("mobile"):
        logger.warning("[store] lead webhook missing phone; skipping")
        return
    lead, created = await leadService.create_lead(
        engine, _lead_payload(parsed), by_user_id="system", source_label="Medusa",
    )
    logger.info("[store] lead %s (%s) from Medusa customer",
                lead.uid, "created" if created else "merged")


async def process_store_order(engine, payload: dict) -> None:
    """Medusa order.placed → upsert lead + create the linked order + timeline."""
    import services.leadService as leadService
    from managers import (
        CustomerOrderManager, CustomerOrderSchema,
        OrderItemSchema, OrderItemManager,
        ProductManager, UserManager,
    )
    from utils.constants import UserRole, OrderStatus, CollectionType, PaymentMethod
    from utils.outlet_assignment import auto_assign_outlet

    order_mgr = CustomerOrderManager(engine)
    item_mgr = OrderItemManager(engine)
    product_mgr = ProductManager(engine)
    user_mgr = UserManager(engine)

    parsed = parse_medusa_order(payload)
    medusa_id = parsed.get("medusa_id")
    if not medusa_id or not parsed.get("mobile"):
        logger.warning("[store] order webhook missing id/phone; skipping")
        return

    # Idempotency: Medusa order id == ERP order uid (same trick as the LSQ path).
    if await order_mgr.fetch(str(medusa_id)):
        logger.info("[store] order %s already exists; skipping", medusa_id)
        return

    # Resolve products by SKU; skip unknown/inactive.
    gross = Decimal("0.00")
    resolved = []
    for it in parsed["items"]:
        product = await product_mgr.fetch_one(filters={"sku": it["sku"]})
        if not product or not product.is_active:
            continue
        line = it["unit_price"] * Decimal(it["quantity"])
        gross += line
        resolved.append((product, it, line))
    if not resolved:
        logger.warning("[store] order %s has no valid products; skipping", medusa_id)
        return

    # Upsert the lead (dedup handles get-or-create), then attribute the order.
    lead, _ = await leadService.create_lead(
        engine, _lead_payload(parsed), by_user_id="system", source_label="Medusa",
    )
    admin = await user_mgr.fetch_one(filters={"role": UserRole.ADMIN, "is_active": True})

    order = await order_mgr.create(CustomerOrderSchema(
        uid=str(medusa_id),
        lead_id=lead.uid,
        order_number=f"ORD-STORE-{str(medusa_id)[-12:]}",
        customer_name=parsed["customer_name"],
        customer_phone=parsed["mobile"],
        address_line=parsed.get("address_line"),
        district=parsed.get("district") or "Hassan",
        state=parsed.get("state") or "karnataka",
        pincode=parsed.get("pincode"),
        telecaller_id=admin.uid if admin else None,
        order_status=OrderStatus.PENDING,
        collection_type=CollectionType.DOORSTEP,
        payment_method=PaymentMethod.ONLINE,
        order_date=datetime.utcnow(),
        gross_amount=gross,
        total_amount=parsed["total"] or gross,
        expected_delivery_date=datetime.utcnow() + timedelta(days=3),
        priority_level=10,
    ))

    for product, it, line in resolved:
        await item_mgr.create(OrderItemSchema(
            order_id=order.uid, product_id=product.uid, quantity=it["quantity"],
            unit_price=it["unit_price"], total_price=line, subtotal=line,
        ))

    assigned = await auto_assign_outlet(
        engine, order.uid, parsed.get("district"), parsed.get("pincode"),
        parsed.get("state"), None,
    )
    if assigned:
        await order_mgr.update(order.uid, {"assigned_outlet_id": assigned.uid})

    # Timeline: increments counts, auto-advances FTU/RTU, logs the `order` activity.
    await leadService.handle_post_order(engine, lead.uid, order.total_amount)
    logger.info("[store] order %s created on lead %s", medusa_id, lead.uid)
