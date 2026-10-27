"""
x402 Payment Protector
======================

Universal HTTP 402 Payment Required guard that wraps any FastAPI route.
This is the enforcement layer that turns documented fee schedules into
real payment challenges.

How it works:
  1. Agent sends request to protected endpoint without X-402-Receipt
  2. Middleware returns HTTP 402 + JSON challenge (price, wallet, nonce, expiry)
  3. Agent pays on-chain (Solana USDC/SOL), gets tx signature
  4. Agent replays with X-402-Receipt header
  5. Middleware verifies, logs revenue, passes request through

Zero changes to module code. The pricing catalog is the single source
of truth. Add a new module? Add its routes to the catalog.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from nano_empire_tollbooth.gate import (
    create_challenge,
    verify_receipt,
    PaymentReceipt,
    revenue_today,
    revenue_lifetime,
    revenue_by_endpoint,
)


# ── A2A Pricing Catalog ─────────────────────────────────────────────────
# Maps API path prefixes to pricing tiers.
# Agents pay per-call. Prices are in USD.
# Add new modules here — everything else is automatic.

A2A_PRICING_CATALOG: dict[str, dict[str, Any]] = {
    # Memory Vault — persistent agent memory
    "/api/vault/store": {"tier": "standard", "price_usd": 0.002, "module": "memory_vault"},
    "/api/vault/recall": {"tier": "standard", "price_usd": 0.002, "module": "memory_vault"},
    "/api/vault/grant": {"tier": "standard", "price_usd": 0.01, "module": "memory_vault"},
    "/api/vault/purchase": {"tier": "standard", "price_usd": 0.05, "module": "memory_vault"},
    # Marketplace listing is free (commission taken on sale)

    # Workflow Composer — intent-to-DAG compiler
    "/api/composer/compile": {"tier": "standard", "price_usd": 0.01, "module": "workflow_composer"},
    "/api/composer/execute": {"tier": "premium", "price_usd": 0.05, "module": "workflow_composer"},
    "/api/composer/run/": {"tier": "micro", "price_usd": 0.002, "module": "workflow_composer"},

    # Reputation Oracle — stake-weighted attestations
    "/api/oracle/attest": {"tier": "standard", "price_usd": 0.01, "module": "reputation_oracle"},
    "/api/oracle/certify": {"tier": "premium", "price_usd": 0.50, "module": "reputation_oracle"},

    # Equilibrium Engine — market coordination
    "/api/equilibrium/auction/create": {"tier": "standard", "price_usd": 0.01, "module": "equilibrium"},
    "/api/equilibrium/auction/bid": {"tier": "standard", "price_usd": 0.005, "module": "equilibrium"},
    "/api/equilibrium/guild/create": {"tier": "standard", "price_usd": 0.50, "module": "equilibrium"},
    "/api/equilibrium/insurance/create": {"tier": "standard", "price_usd": 0.02, "module": "equilibrium"},
    "/api/equilibrium/insurance/claim": {"tier": "standard", "price_usd": 0.05, "module": "equilibrium"},

    # Agent Gateway — universal A2A access
    "/a2a/checkout": {"tier": "standard", "price_usd": 0.005, "module": "agent_gateway"},
    "/a2a/register": {"tier": "free", "price_usd": 0.00, "module": "agent_gateway"},

    # Collective Intelligence — swarm problem-solving
    "/api/swarm/problem": {"tier": "standard", "price_usd": 0.05, "module": "collective_intelligence"},
    "/api/swarm/solution": {"tier": "standard", "price_usd": 0.01, "module": "collective_intelligence"},
    "/api/swarm/vote": {"tier": "micro", "price_usd": 0.001, "module": "collective_intelligence"},
    "/api/swarm/synthesize": {"tier": "premium", "price_usd": 0.10, "module": "collective_intelligence"},

    # Temporal Contracts — recurring agent obligations
    "/api/contracts/create": {"tier": "standard", "price_usd": 0.02, "module": "temporal_contracts"},
    "/api/contracts/activate": {"tier": "standard", "price_usd": 0.01, "module": "temporal_contracts"},
    "/api/contracts/deliver": {"tier": "standard", "price_usd": 0.005, "module": "temporal_contracts"},
    "/api/contracts/confirm": {"tier": "standard", "price_usd": 0.005, "module": "temporal_contracts"},
    "/api/contracts/terminate": {"tier": "standard", "price_usd": 0.01, "module": "temporal_contracts"},
    "/api/contracts/check-overdue": {"tier": "standard", "price_usd": 0.01, "module": "temporal_contracts"},

    # Knowledge Graph — collective knowledge
    "/api/graph/triple": {"tier": "micro", "price_usd": 0.001, "module": "knowledge_graph"},
    "/api/graph/path": {"tier": "standard", "price_usd": 0.02, "module": "knowledge_graph"},
    "/api/graph/subgraph": {"tier": "standard", "price_usd": 0.01, "module": "knowledge_graph"},
    "/api/graph/resolve": {"tier": "micro", "price_usd": 0.001, "module": "knowledge_graph"},

    # Evolution Engine — strategy optimization
    "/api/evolution/strategy": {"tier": "free", "price_usd": 0.00, "module": "evolution_engine"},
    "/api/evolution/record": {"tier": "micro", "price_usd": 0.002, "module": "evolution_engine"},
    "/api/evolution/fitness/": {"tier": "micro", "price_usd": 0.005, "module": "evolution_engine"},
    "/api/evolution/compare/": {"tier": "standard", "price_usd": 0.01, "module": "evolution_engine"},
    "/api/evolution/mutations/": {"tier": "standard", "price_usd": 0.02, "module": "evolution_engine"},
    "/api/evolution/mutate": {"tier": "standard", "price_usd": 0.02, "module": "evolution_engine"},
    "/api/evolution/leaderboard": {"tier": "standard", "price_usd": 0.01, "module": "evolution_engine"},
}

# Paths that never require payment
FREE_PATHS: set[str] = {
    "/health",
    "/products",
    "/public/",
    "/api/vault/stats",
    "/api/vault/marketplace",
    "/api/composer/estimate/",
    "/api/composer/workflows",
    "/api/composer/templates",
    "/api/composer/stats",
    "/api/oracle/reputation/",
    "/api/oracle/leaderboard",
    "/api/oracle/category/",
    "/api/oracle/stats",
    "/api/equilibrium/auction/",
    "/api/equilibrium/guild/",
    "/api/equilibrium/market/",
    "/api/equilibrium/stats",
    "/a2a/capabilities",
    "/a2a/register",
    "/a2a/health",
    "/a2a/stats",
    "/api/swarm/problem/",
    "/api/swarm/problems",
    "/api/swarm/expertise/",
    "/api/swarm/stats",
    "/api/contracts/contract/",
    "/api/contracts/contracts",
    "/api/contracts/stats",
    "/api/graph/entity/",
    "/api/graph/conflicts",
    "/api/graph/contributors",
    "/api/graph/stats",
    "/api/graph/search",
    "/api/evolution/strategy",
    "/api/evolution/lineage/",
    "/api/evolution/agent/",
    "/api/evolution/stats",
    "/api/evolution/leaderboard",
    "/api/vault/agent/",
    "/.well-known/",
    "/webhooks/",
    "/api/a2a/revenue",
    "/api/a2a/pricing",
    "/api/a2a/health",
}

# Override: some paths within free modules should still cost
PAID_OVERRIDES: set[str] = {
    "/api/oracle/certify",
    "/api/evolution/mutate",
    "/api/evolution/record",
    "/api/evolution/fitness/",
    "/api/evolution/compare/",
    "/api/evolution/mutations/",
    "/api/evolution/leaderboard",
}


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware that enforces x402 payment for protected endpoints.

    Apply to the FastAPI app:
        app.add_middleware(X402PaymentMiddleware)

    Or to individual routers:
        router.add_middleware(X402PaymentMiddleware)
    """

    def __init__(
        self,
        app: ASGIApp,
        pricing_catalog: dict[str, dict[str, Any]] | None = None,
        free_paths: set[str] | None = None,
    ):
        super().__init__(app)
        self.pricing_catalog = pricing_catalog or A2A_PRICING_CATALOG
        self.free_paths_override = free_paths

    def _get_free_paths(self) -> set[str]:
        if self.free_paths_override is not None:
            return self.free_paths_override
        return FREE_PATHS

    def _is_free_path(self, path: str) -> bool:
        free = self._get_free_paths()
        for fp in free:
            if path.startswith(fp) or path == fp:
                return True
        return False

    def _is_paid_override(self, path: str) -> bool:
        for po in PAID_OVERRIDES:
            if path.startswith(po) or path == po:
                return True
        return False

    def _get_pricing(self, path: str) -> dict[str, Any] | None:
        # Exact match first
        if path in self.pricing_catalog:
            return self.pricing_catalog[path]
        # Prefix match (e.g., /api/vault/store matches /api/vault/)
        longest_prefix = ""
        for route in self.pricing_catalog:
            if path.startswith(route):
                if len(route) > len(longest_prefix):
                    longest_prefix = route
        if longest_prefix:
            return self.pricing_catalog[longest_prefix]
        return None

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1. Skip webhooks, health, and explicitly free paths
        if self._is_free_path(path) and not self._is_paid_override(path):
            return await call_next(request)

        # 2. Find pricing for this path
        pricing = self._get_pricing(path)
        if pricing is None:
            # Path not in catalog — let it through (free)
            return await call_next(request)

        # 3. Free tier — let through
        if pricing.get("price_usd", 0) == 0:
            return await call_next(request)

        # 4. Check payment — use catalog price (not SKILL_PRICING tier default)
        receipt_header = request.headers.get("X-402-Receipt") or request.headers.get("x-402-receipt")
        catalog_price = pricing["price_usd"]

        if receipt_header is None:
            # No receipt — issue 402 challenge with correct catalog price
            challenge = create_challenge(path, custom_price_usd=catalog_price)
            challenge_dict = challenge.to_dict()
            return Response(
                content=json.dumps({
                    "status": "payment_required",
                    "protocol": "x402",
                    "message": f"This endpoint requires payment. Send X-402-Receipt header with on-chain payment receipt.",
                    "challenge": {
                        "nonce": challenge_dict["nonce"],
                        "price_usd": catalog_price,
                        "accepted_tokens": ["USDC", "SOL"],
                        "expires_at": challenge_dict["expires_at"],
                        "endpoint": path,
                        "tier": pricing["tier"],
                        "chain": "solana",
                    },
                }),
                status_code=402,
                media_type="application/json",
                headers={
                    "X-402-Nonce": challenge_dict["nonce"],
                    "X-402-Price-USD": str(catalog_price),
                    "X-402-Accepted-Tokens": "USDC,SOL",
                    "X-402-Expires": str(challenge_dict["expires_at"]),
                },
            )

        # Receipt present — verify
        try:
            import base64
            import json as j
            raw = base64.b64decode(receipt_header)
            receipt_data = j.loads(raw)
            receipt = PaymentReceipt(
                nonce=receipt_data["nonce"],
                tx_signature=receipt_data["tx_signature"],
                payer_wallet=receipt_data["payer_wallet"],
                amount_usd=float(receipt_data["amount_usd"]),
                token=receipt_data.get("token", "USDC"),
                chain=receipt_data.get("chain", "solana"),
                timestamp=float(receipt_data.get("timestamp", 0)),
            )
        except Exception:
            return Response(
                content=json.dumps({"status": "invalid_receipt", "error": "Cannot decode X-402-Receipt", "protocol": "x402"}),
                status_code=402,
                media_type="application/json",
            )

        ok, reason = verify_receipt(receipt)
        if ok:
            request.state.x402_paid = True
            request.state.x402_amount = catalog_price
            request.state.x402_module = pricing.get("module", "unknown")
            request.state.x402_nonce = receipt.nonce
            return await call_next(request)

        return Response(
            content=json.dumps({"status": "payment_failed", "error": reason, "protocol": "x402"}),
            status_code=402,
            media_type="application/json",
        )


def add_payment_protection(app, pricing_catalog: dict[str, dict[str, Any]] | None = None):
    """
    Convenience function: add x402 payment protection to a FastAPI app.
    Call once in server.py after app creation.
    """
    app.add_middleware(X402PaymentMiddleware, pricing_catalog=pricing_catalog)
    return app


def get_module_revenue(module_name: str) -> float:
    """Get lifetime revenue for a specific module."""
    by_endpoint = revenue_by_endpoint()
    total = 0.0
    for endpoint, amount in by_endpoint.items():
        if endpoint.startswith(f"/api/{module_name}") or module_name in endpoint:
            total += amount
    return total


def get_a2a_revenue_summary() -> dict[str, Any]:
    """Complete revenue breakdown across all A2A modules."""
    by_endpoint = revenue_by_endpoint()
    modules: dict[str, float] = {}
    for endpoint, amount in by_endpoint.items():
        # Determine module from endpoint path
        parts = endpoint.strip("/").split("/")
        module_key = parts[1] if len(parts) > 1 else "unknown"
        if module_key == "api" and len(parts) > 2:
            module_key = parts[2]
        modules[module_key] = modules.get(module_key, 0) + amount

    return {
        "today_usd": revenue_today(),
        "lifetime_usd": revenue_lifetime(),
        "by_module": {
            "memory_vault": get_module_revenue("vault"),
            "workflow_composer": get_module_revenue("composer"),
            "reputation_oracle": get_module_revenue("oracle"),
            "equilibrium_engine": get_module_revenue("equilibrium"),
            "agent_gateway": get_module_revenue("a2a"),
            "collective_intelligence": get_module_revenue("swarm"),
            "temporal_contracts": get_module_revenue("contracts"),
            "knowledge_graph": get_module_revenue("graph"),
            "evolution_engine": get_module_revenue("evolution"),
        },
        "by_endpoint": by_endpoint,
    }
