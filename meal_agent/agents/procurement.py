"""Approval-gated procurement using a mock-only retailer and SQLite audit trail."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from meal_agent.adapters.retail.mock import MockRetailer
from meal_agent.config import settings
from meal_agent.storage.domain_store import DomainStore
from meal_agent.storage.workflow_store import WorkflowStore


class ProcurementAgent:
    """Compute missing ingredients, build mock carts, and validate one-time approvals."""

    def __init__(
        self,
        domain_store: DomainStore,
        workflow_store: WorkflowStore,
        retailer: MockRetailer | None = None,
        mock_checkout_enabled: bool | None = None,
    ) -> None:
        self.domain_store = domain_store
        self.workflow_store = workflow_store
        self.retailer = retailer or MockRetailer()
        self.mock_checkout_enabled = (
            settings.procurement_enabled
            if mock_checkout_enabled is None
            else mock_checkout_enabled
        )

    async def compute_gap(self, user_id: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
        state = plan.get("state", plan)
        verification = state.get("verification") or {}
        portions = verification.get("ingredients") or (state.get("candidate") or {}).get("ingredients", [])
        inventory = await asyncio.to_thread(self.domain_store.list_inventory, user_id)
        gaps: list[dict[str, Any]] = []
        for portion in portions:
            name = str(portion.get("name", ""))
            required = float(portion.get("grams", portion.get("min_g", 0)))
            match = next(
                (item for item in inventory if str(item["name"]).casefold() in name.casefold()
                 or name.casefold() in str(item["name"]).casefold()),
                None,
            )
            available = float(match.get("available_grams", 0)) if match else 0.0
            missing = max(0.0, required - available)
            if missing > 0:
                gaps.append({"name": name, "required_g": required, "available_g": available, "missing_g": missing})
        if not gaps:
            verification = state.get("verification") or {}
            for nutrient, amount in (verification.get("missing_capacity") or {}).items():
                if amount > 0:
                    gaps.append({"name": f"high-protein food ({nutrient})", "missing_capacity": amount})
        return gaps

    async def product_search(self, query: str) -> list[dict[str, Any]]:
        return await self.retailer.search_products(query)

    @staticmethod
    def _search_queries(gap: dict[str, Any]) -> list[str]:
        if gap.get("missing_capacity"):
            nutrient = str(gap.get("name", "")).casefold()
            if "protein" in nutrient:
                return ["cottage cheese", "protein powder", "greek yogurt"]
            return ["greek yogurt", "chicken", "tofu"]
        return [str(gap.get("name", ""))]

    @staticmethod
    def score_product(product: dict[str, Any], query: str) -> float:
        """Deterministic score prioritizing ingredient match, low price, and pack size."""
        terms = set(query.casefold().split())
        product_terms = set(str(product.get("name", "")).casefold().split())
        overlap = len(terms & product_terms) / max(len(terms), 1)
        price = float(product.get("price", float("inf")))
        pack_size = float(product.get("pack_size_g", 1))
        waste_penalty = max(0.0, pack_size - 300.0) / 10000.0
        return overlap * 100.0 - price - waste_penalty

    async def price_compare(self, query: str) -> list[dict[str, Any]]:
        products = await self.product_search(query)
        return sorted(products, key=lambda product: self.score_product(product, query), reverse=True)

    async def cart_build(self, user_id: str, plan_id: str, missing: list[dict[str, Any]]) -> dict[str, Any]:
        selected_products: list[dict[str, Any]] = []
        trace_steps: list[dict[str, Any]] = [
            {"tool": "compute_gap", "inputs": {"plan_id": plan_id}, "output": {"missing": missing}}
        ]
        for gap in missing:
            matches: list[dict[str, Any]] = []
            for query in self._search_queries(gap):
                products = await self.product_search(query)
                scored = [
                    {**product, "score": self.score_product(product, query)}
                    for product in products
                ]
                scored.sort(key=lambda product: float(product["score"]), reverse=True)
                trace_steps.append({
                    "tool": "product_search+price_compare+score_product",
                    "inputs": {"query": query},
                    "output": {"products": scored},
                })
                matches.extend(scored)
            if matches:
                query = self._search_queries(gap)[0]
                matches.sort(key=lambda product: self.score_product(product, query), reverse=True)
                selected_products.append(matches[0])
        product_ids = [str(product["id"]) for product in selected_products]
        cart_data = await self.retailer.build_cart(product_ids)
        trace_steps.append({
            "tool": "cart_build",
            "inputs": {"product_ids": product_ids},
            "output": cart_data,
        })
        missing_key = ",".join(sorted(f"{item['name']}:{item.get('missing_g', item.get('missing_capacity', 0))}" for item in missing))
        idempotency_key = str(uuid5(NAMESPACE_URL, f"meal-cart:{user_id}:{plan_id}:{missing_key}"))
        cart_id = await asyncio.to_thread(
            self.domain_store.create_cart,
            user_id,
            plan_id,
            {**cart_data, "missing": missing},
            float(cart_data.get("total", 0)),
            idempotency_key,
        )
        await asyncio.to_thread(
            self.domain_store.audit_procurement,
            user_id,
            cart_id,
            "cart_built",
            {"plan_id": plan_id, "missing": missing, "cart": cart_data, "idempotency_key": idempotency_key},
        )
        return {"cart_id": cart_id, "idempotency_key": idempotency_key, **cart_data, "missing": missing, "trace_steps": trace_steps}

    async def request_approval(self, user_id: str, cart_id: str) -> dict[str, Any]:
        cart = await asyncio.to_thread(self.domain_store.get_cart, user_id, cart_id)
        if cart is None:
            raise LookupError("Cart not found for this user.")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(minutes=settings.approval_ttl_minutes)
        expires_at = expires.strftime("%Y-%m-%d %H:%M:%S")
        await asyncio.to_thread(self.domain_store.save_approval, user_id, cart_id, token_hash, expires_at)
        await asyncio.to_thread(
            self.domain_store.audit_procurement,
            user_id,
            cart_id,
            "approval_requested",
            {"expires_at": expires_at, "total": cart["total"], "mock": True},
        )
        return {
            "approval_token": token,
            "expires_at": expires_at,
            "cart": cart,
            "trace_steps": [{
                "tool": "request_approval",
                "inputs": {"cart_id": cart_id, "expires_at": expires_at},
                "output": {"status": "approval_required", "expires_at": expires_at},
            }],
        }

    async def cart_update(
        self, user_id: str, cart_id: str, product_ids: list[str]
    ) -> dict[str, Any]:
        cart = await self.retailer.build_cart(product_ids)
        updated = await asyncio.to_thread(
            self.domain_store.update_cart,
            user_id,
            cart_id,
            cart,
            float(cart.get("total", 0)),
        )
        if not updated:
            return {"status": "not_found", "spoken_summary": "That pending cart was not found or is no longer editable."}
        await asyncio.to_thread(
            self.domain_store.audit_procurement,
            user_id,
            cart_id,
            "cart_updated",
            {"product_ids": product_ids, "cart": cart},
        )
        return {"status": "ok", "cart_id": cart_id, **cart, "spoken_summary": "The mock cart was updated. Review it before approval."}

    async def order_status(self, user_id: str, cart_id: str) -> dict[str, Any]:
        order = await asyncio.to_thread(self.domain_store.get_order_status, user_id, cart_id)
        if order is None:
            return {"status": "not_found", "spoken_summary": "I could not find that cart for your account."}
        return {"status": "ok", "order": order, "mock": True, "spoken_summary": "The mock cart status is ready."}

    async def checkout(
        self,
        user_id: str,
        cart_id: str,
        approval_token: str | None,
        *,
        explicit_confirmation: bool = False,
    ) -> dict[str, Any]:
        cart = await asyncio.to_thread(self.domain_store.get_cart, user_id, cart_id)
        if cart is None:
            return {"status": "not_found", "spoken_summary": "I could not find that cart for your account."}
        total = float(cart["total"])
        if total > settings.max_order_budget:
            return {"status": "budget_exceeded", "spoken_summary": "The cart exceeds the per-order budget."}
        spent = await asyncio.to_thread(self.domain_store.daily_order_total, user_id)
        if spent + total > settings.max_daily_order_budget:
            return {"status": "budget_exceeded", "spoken_summary": "The order would exceed your daily shopping budget."}
        idem = str(cart["idempotency_key"])
        existing = await asyncio.to_thread(self.domain_store.get_order_by_idempotency, user_id, idem)
        if existing:
            return {"status": "already_ordered", "order": existing, "mock": True, "spoken_summary": "This cart was already processed; no duplicate order was created."}
        if explicit_confirmation:
            consumed = await asyncio.to_thread(
                self.domain_store.consume_latest_approval, user_id, cart_id
            )
        elif approval_token:
            token_hash = hashlib.sha256(approval_token.encode()).hexdigest()
            consumed = await asyncio.to_thread(
                self.domain_store.consume_approval, user_id, cart_id, token_hash
            )
        else:
            consumed = False
        if not consumed:
            await asyncio.to_thread(
                self.domain_store.audit_procurement,
                user_id,
                cart_id,
                "checkout_rejected",
                {"reason": "invalid_expired_or_used_approval", "idempotency_key": idem},
            )
            return {"status": "approval_invalid", "spoken_summary": "That approval is missing, expired, already used, or does not belong to this cart."}
        if not self.mock_checkout_enabled:
            await asyncio.to_thread(
                self.domain_store.audit_procurement,
                user_id,
                cart_id,
                "mock_checkout_disabled",
                {"idempotency_key": idem, "total": total},
            )
            return {"status": "checkout_disabled", "spoken_summary": "The approval was valid, but checkout is disabled by configuration. No order was placed."}
        # Deliberately internal simulation: there is no real retailer adapter.
        order = await asyncio.to_thread(
            self.domain_store.finish_mock_cart,
            user_id,
            cart_id,
            idem,
            {"status": "mock_ordered", "cart_id": cart_id, "total": total, "currency": "USD", "mock": True},
        )
        delivered: list[dict[str, Any]] = []
        cart_data = order.get("cart", {})
        raw_items = cart_data.get("products") or cart_data.get("items") or []
        for item in raw_items:
            name = str(item.get("ingredient") or item.get("name") or "").strip()
            grams = float(item.get("pack_size_g") or item.get("grams") or item.get("grams_estimate") or 0)
            if not grams and item.get("quantity"):
                try:
                    q = float(item["quantity"])
                    unit = str(item.get("unit", "")).lower()
                    if unit in {"kg", "kgs", "kilogram", "kilograms"}:
                        grams = q * 1000.0
                    elif unit in {"g", "gm", "gms", "gram", "grams"}:
                        grams = q
                    elif unit in {"lb", "lbs", "pound", "pounds"}:
                        grams = q * 453.59
                    else:
                        grams = q
                except (ValueError, TypeError):
                    grams = 200.0
            if name and grams > 0:
                delivered_item = await asyncio.to_thread(
                    self.domain_store.upsert_inventory,
                    user_id,
                    {
                        "name": name,
                        "quantity": grams,
                        "unit": "g",
                        "grams_estimate": grams,
                        "confidence": 1.0,
                        "source": "mock_order_delivery",
                        "metadata": {"mock_order_idempotency_key": idem},
                    },
                )
                delivered.append({"name": name, "grams": grams, "item_id": delivered_item["item_id"]})
        order["delivered_inventory"] = delivered
        await asyncio.to_thread(
            self.domain_store.audit_procurement,
            user_id,
            cart_id,
            "mock_delivery_recorded",
            {"items": delivered, "idempotency_key": idem},
        )
        await asyncio.to_thread(
            self.domain_store.audit_procurement,
            user_id,
            cart_id,
            "mock_order_completed",
            order,
        )
        return {
            "status": "mock_ordered",
            "order": order,
            "mock": True,
            "spoken_summary": f"Your grocery order has been approved and delivered! Added {len(delivered)} item(s) directly to your kitchen inventory.",
        }

    async def decline_cart(self, user_id: str, cart_id: str) -> dict[str, Any]:
        await asyncio.to_thread(self.domain_store.cancel_cart, user_id, cart_id)
        await asyncio.to_thread(
            self.domain_store.audit_procurement,
            user_id,
            cart_id,
            "approval_declined",
            {"status": "declined_by_user"},
        )
        return {
            "status": "approval_declined",
            "cart_id": cart_id,
            "spoken_summary": "The grocery cart has been refused and cancelled. No order was placed.",
        }

    async def shop_for_meal(
        self,
        user_id: str,
        plan_id: str,
        confirm: bool = False,
        approval_token: str | None = None,
        cart_id: str | None = None,
        plan_state: dict[str, Any] | None = None,
        explicit_confirmation: bool = False,
        decline: bool = False,
        mock_checkout_enabled: bool | None = None,
    ) -> dict[str, Any]:
        if mock_checkout_enabled is not None:
            self.mock_checkout_enabled = mock_checkout_enabled
        if decline:
            if not cart_id:
                pending_cart = await asyncio.to_thread(self.domain_store.get_latest_pending_cart, user_id, plan_id)
                if pending_cart:
                    cart_id = pending_cart["cart_id"]
            return await self.decline_cart(user_id, cart_id or "")
        if confirm:
            if not cart_id:
                pending_cart = await asyncio.to_thread(self.domain_store.get_latest_pending_cart, user_id, plan_id)
                if pending_cart:
                    cart_id = pending_cart["cart_id"]
            if not cart_id or (not approval_token and not explicit_confirmation):
                return {"status": "approval_required", "spoken_summary": "An explicit approval and cart ID are required; no purchase was attempted."}
            result = await self.checkout(
                user_id, cart_id, approval_token,
                explicit_confirmation=explicit_confirmation,
            )
            result["trace_steps"] = [{
                "tool": "checkout",
                "inputs": {"cart_id": cart_id, "approval_token": approval_token},
                "output": {"status": result.get("status"), "order_id": result.get("order", {}).get("cart_id")},
            }]
            return result
        plan = plan_state or await asyncio.to_thread(self.workflow_store.get_plan, user_id, plan_id)
        if plan is None:
            return {"status": "not_found", "plan_id": plan_id, "spoken_summary": "I could not find a saved meal plan for your account."}
        gaps = await self.compute_gap(user_id, plan)
        if not gaps:
            return {"status": "no_purchase_needed", "plan_id": plan_id, "spoken_summary": "I found no missing ingredients to add to a cart."}
        cart = await self.cart_build(user_id, plan_id, gaps)
        if not cart.get("products"):
            return {"status": "no_products_found", "plan_id": plan_id, "missing": gaps, "spoken_summary": "I could not find mock catalog products for the missing ingredients."}
        approval = await self.request_approval(user_id, cart["cart_id"])
        return {
            "status": "approval_required",
            "plan_id": plan_id,
            "cart_id": cart["cart_id"],
            "products": cart["products"],
            "total": cart["total"],
            "currency": cart.get("currency", "USD"),
            "missing": gaps,
            "approval_token": approval["approval_token"],
            "approval_expires_at": approval["expires_at"],
            "mock": True,
            "spoken_summary": "Review this mock cart and explicitly confirm with its approval token. It will not place a real order.",
            "trace_steps": cart.get("trace_steps", []) + approval.get("trace_steps", []),
        }


_DEFAULT_AGENT: ProcurementAgent | None = None


def get_procurement_agent(mock_checkout_enabled: bool | None = None) -> ProcurementAgent:
    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is None or mock_checkout_enabled is not None:
        domain = DomainStore(settings.workflow_database_path)
        workflow = WorkflowStore(settings.workflow_database_path)
        enabled = True if mock_checkout_enabled is None else mock_checkout_enabled
        _DEFAULT_AGENT = ProcurementAgent(domain, workflow, mock_checkout_enabled=enabled)
    return _DEFAULT_AGENT


async def shop_for_meal(
    plan_id: str,
    confirm: bool = False,
    *,
    user_id: str | None = None,
    approval_token: str | None = None,
    cart_id: str | None = None,
    plan_state: dict[str, Any] | None = None,
    explicit_confirmation: bool = False,
    decline: bool = False,
    mock_checkout_enabled: bool | None = None,
) -> dict[str, Any]:
    return await get_procurement_agent(mock_checkout_enabled=mock_checkout_enabled).shop_for_meal(
        user_id or settings.user_id,
        plan_id,
        confirm,
        approval_token,
        cart_id,
        plan_state,
        explicit_confirmation,
        decline=decline,
    )
