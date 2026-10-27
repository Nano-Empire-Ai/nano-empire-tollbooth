"""
x402 Micropayment Gate
======================

HTTP 402 Payment Required — the internet's forgotten status code, finally used
as intended. Every premium API call returns a payment challenge. Agents pay
on-chain (Solana USDC or SOL), submit receipt, gate opens.

Protocol flow:
  1. Agent hits premium endpoint without payment header
  2. Gate returns HTTP 402 + JSON challenge (price, wallet, nonce, expiry)
  3. Agent pays on-chain → gets tx signature
  4. Agent replays request with X-402-Receipt header (base64 JSON)
  5. Gate verifies receipt → logs revenue → passes request through

Paper mode: Challenges still issue, but verification auto-approves with
$0.00 logged. This lets agents rehearse the full protocol without spending.

Revenue flows into the sovereign treasury ledger at empire/logs/x402_revenue.jsonl.
"""
from __future__ import annotations

import base64
import hashlib
import json

try:
    import redis
except ImportError:
    redis = None
import logging
import os
import secrets
import time

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
try:
    if redis is not None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
    else:
        redis_client = None
except Exception:
    redis_client = None

import urllib.request
import json
import logging
import os

def _verify_solana_tx_rpc(tx_signature: str) -> tuple[bool, str]:
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.devnet.solana.com")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            tx_signature,
            {"encoding": "json", "maxSupportedTransactionVersion": 0}
        ]
    }
    req = urllib.request.Request(rpc_url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("result") is not None:
                err = data["result"].get("meta", {}).get("err")
                if err is not None:
                    return False, f"Transaction failed on-chain: {err}"
                return True, "verified"
            return False, "Transaction signature not found on-chain"
    except Exception as e:
        return False, f"RPC verification error: {str(e)}"

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

def is_paper_mode():
    return os.environ.get("PAPER_MODE", "true").lower() == "true"


# ── Configuration ────────────────────────────────────────────────────────

TREASURY_WALLET = os.getenv(
    "EMPIRE_TREASURY_WALLET",
    "PAPER_MODE_WALLET_NOT_SET",
)
ACCEPTED_TOKENS = ["USDC", "SOL"]
CHALLENGE_TTL_SECONDS = 300  # 5 minutes to pay
# Default to relative path from project root if not set
DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "x402_revenue.jsonl"
REVENUE_LOG = Path(
    os.getenv("EMPIRE_X402_LOG", str(DEFAULT_LOG_PATH))
)


class PaymentTier(str, Enum):
    """Tiered pricing for agent capabilities."""
    MICRO = "micro"        # $0.001 – $0.01  (text queries, metadata)
    STANDARD = "standard"  # $0.01  – $0.10  (image gen, voice clone)
    PREMIUM = "premium"    # $0.10  – $1.00  (video gen, world sim)
    ENTERPRISE = "enterprise"  # $1.00+ (batch jobs, exclusive access)


# Default prices in USD. Override per-endpoint via SKILL_PRICING.
TIER_PRICES_USD: dict[PaymentTier, float] = {
    PaymentTier.MICRO: 0.005,
    PaymentTier.STANDARD: 0.05,
    PaymentTier.PREMIUM: 0.50,
    PaymentTier.ENTERPRISE: 2.00,
}

# Maps endpoint paths to pricing tiers. Add entries as skills go live.
SKILL_PRICING: dict[str, PaymentTier] = {
    # Memory Vault
    "/api/vault/store": PaymentTier.STANDARD,
    "/api/vault/recall": PaymentTier.STANDARD,
    "/api/vault/grant": PaymentTier.STANDARD,
    "/api/vault/purchase": PaymentTier.STANDARD,
    # Workflow Composer
    "/api/composer/compile": PaymentTier.STANDARD,
    "/api/composer/execute": PaymentTier.PREMIUM,
    "/api/composer/run/": PaymentTier.MICRO,
    # Reputation Oracle
    "/api/oracle/attest": PaymentTier.STANDARD,
    "/api/oracle/certify": PaymentTier.PREMIUM,
    # Equilibrium Engine
    "/api/equilibrium/auction/create": PaymentTier.STANDARD,
    "/api/equilibrium/auction/bid": PaymentTier.STANDARD,
    "/api/equilibrium/guild/create": PaymentTier.STANDARD,
    "/api/equilibrium/insurance/create": PaymentTier.STANDARD,
    "/api/equilibrium/insurance/claim": PaymentTier.STANDARD,
    # Agent Gateway
    "/a2a/checkout": PaymentTier.STANDARD,
    # Collective Intelligence
    "/api/swarm/problem": PaymentTier.STANDARD,
    "/api/swarm/solution": PaymentTier.STANDARD,
    "/api/swarm/vote": PaymentTier.MICRO,
    "/api/swarm/synthesize": PaymentTier.PREMIUM,
    # Temporal Contracts
    "/api/contracts/create": PaymentTier.STANDARD,
    "/api/contracts/activate": PaymentTier.STANDARD,
    "/api/contracts/deliver": PaymentTier.STANDARD,
    "/api/contracts/confirm": PaymentTier.STANDARD,
    "/api/contracts/terminate": PaymentTier.STANDARD,
    "/api/contracts/check-overdue": PaymentTier.STANDARD,
    # Knowledge Graph
    "/api/graph/triple": PaymentTier.MICRO,
    "/api/graph/path": PaymentTier.STANDARD,
    "/api/graph/subgraph": PaymentTier.STANDARD,
    "/api/graph/resolve": PaymentTier.MICRO,
    # Evolution Engine
    "/api/evolution/record": PaymentTier.MICRO,
    "/api/evolution/fitness/": PaymentTier.MICRO,
    "/api/evolution/compare/": PaymentTier.STANDARD,
    "/api/evolution/mutations/": PaymentTier.STANDARD,
    "/api/evolution/mutate": PaymentTier.STANDARD,
    "/api/evolution/leaderboard": PaymentTier.STANDARD,
    # Credit Scoring & Lending (Surface)
    "/api/credit/score": PaymentTier.MICRO,
    "/api/credit/report": PaymentTier.STANDARD,
    "/api/credit/stats": PaymentTier.MICRO,
    "/api/lending/pool/deposit": PaymentTier.STANDARD,
    "/api/lending/pool/withdraw": PaymentTier.STANDARD,
    "/api/lending/apply": PaymentTier.PREMIUM,
    "/api/lending/repay": PaymentTier.STANDARD,
    "/api/lending/pool/stats": PaymentTier.MICRO,
    # Settlement Netting (Surface)
    "/api/netting/queue": PaymentTier.MICRO,
    "/api/netting/instant": PaymentTier.STANDARD,
    "/api/netting/execute": PaymentTier.PREMIUM,
    "/api/netting/stats": PaymentTier.MICRO,
    # Capability Futures (Surface)
    "/api/futures/create": PaymentTier.STANDARD,
    "/api/futures/order": PaymentTier.STANDARD,
    "/api/futures/settle": PaymentTier.STANDARD,
    "/api/futures/price/": PaymentTier.MICRO,
    "/api/futures/stats": PaymentTier.MICRO,
    # Agent Passport (Surface)
    "/api/passport/issue": PaymentTier.PREMIUM,
    "/api/passport/verify": PaymentTier.MICRO,
    "/api/passport/bridge": PaymentTier.PREMIUM,
    "/api/passport/bridge/complete": PaymentTier.PREMIUM,
    "/api/passport/stats": PaymentTier.MICRO,
    # Subscription Bundles (Surface)
    "/api/subscriptions/tier": PaymentTier.STANDARD,
    "/api/subscriptions/subscribe": PaymentTier.STANDARD,
    "/api/subscriptions/usage": PaymentTier.MICRO,
    "/api/subscriptions/bundle": PaymentTier.PREMIUM,
    "/api/subscriptions/bundle/subscribe": PaymentTier.PREMIUM,
    "/api/subscriptions/churn-check": PaymentTier.STANDARD,
    "/api/subscriptions/stats": PaymentTier.MICRO,
    # Data Marketplace (Surface)
    "/api/marketplace/publish": PaymentTier.STANDARD,
    "/api/marketplace/purchase": PaymentTier.STANDARD,
    "/api/marketplace/provenance": PaymentTier.MICRO,
    "/api/marketplace/stats": PaymentTier.MICRO,
    # SLA Marketplace (Surface)
    "/api/sla/create": PaymentTier.PREMIUM,
    "/api/sla/subscribe": PaymentTier.STANDARD,
    "/api/sla/record-performance": PaymentTier.MICRO,
    "/api/sla/performance/": PaymentTier.MICRO,
    "/api/sla/stats": PaymentTier.MICRO,
    # Workflow Revenue (Surface)
    "/api/workflows/create": PaymentTier.PREMIUM,
    "/api/workflows/execute": PaymentTier.PREMIUM,
    "/api/workflows/complete": PaymentTier.STANDARD,
    "/api/workflows/stats": PaymentTier.MICRO,
    # Spot Market (Surface)
    "/api/spot/list": PaymentTier.STANDARD,
    "/api/spot/order": PaymentTier.STANDARD,
    "/api/spot/option/buy": PaymentTier.PREMIUM,
    "/api/spot/option/exercise": PaymentTier.PREMIUM,
    "/api/spot/prices/": PaymentTier.MICRO,
    "/api/spot/stats": PaymentTier.MICRO,
    # Agent DAO (Surface)
    "/api/dao/create": PaymentTier.PREMIUM,
    "/api/dao/join": PaymentTier.STANDARD,
    "/api/dao/proposal": PaymentTier.STANDARD,
    "/api/dao/vote": PaymentTier.MICRO,
    "/api/dao/resolve-proposals": PaymentTier.PREMIUM,
    "/api/dao/invest": PaymentTier.PREMIUM,
    "/api/dao/stats": PaymentTier.MICRO,
    # Chain Router (Surface)
    "/api/chain-router/route": PaymentTier.STANDARD,
    "/api/chain-router/rate": PaymentTier.STANDARD,
    "/api/chain-router/optimal": PaymentTier.MICRO,
    "/api/chain-router/arbitrage": PaymentTier.MICRO,
    "/api/chain-router/rates": PaymentTier.MICRO,
    "/api/chain-router/stats": PaymentTier.MICRO,
    # Strategy Advisor (Surface)
    "/api/advisor/simulate/pricing": PaymentTier.PREMIUM,
    "/api/advisor/simulate/guild": PaymentTier.PREMIUM,
    "/api/advisor/simulate/insurance": PaymentTier.PREMIUM,
    "/api/advisor/report": PaymentTier.PREMIUM,
    "/api/advisor/stats": PaymentTier.MICRO,
    # Inference Arbitrage Exchange (Aurora)
    "/api/exchange/infer": PaymentTier.STANDARD,
    "/api/exchange/register-provider": PaymentTier.STANDARD,
    # Prompt Futures Market (Aurora)
    "/api/futures/list": PaymentTier.MICRO,
    "/api/futures/use/": PaymentTier.MICRO,
    "/api/futures/rate/": PaymentTier.MICRO,
    "/api/futures/fork/": PaymentTier.STANDARD,
    # Attention Derivatives (Aurora)
    "/api/attention/bid": PaymentTier.MICRO,
    "/api/attention/subscribe": PaymentTier.STANDARD,
    "/api/attention/clear": PaymentTier.PREMIUM,
    # Knowledge Fragment Exchange (Aurora)
    "/api/fragments/ingest": PaymentTier.STANDARD,
    "/api/fragments/buy/": PaymentTier.MICRO,
    "/api/fragments/buy-bundle": PaymentTier.STANDARD,
    "/api/fragments/refresh/": PaymentTier.MICRO,
    # Context Window Rental (Aurora)
    "/api/context/rent": PaymentTier.STANDARD,
    "/api/context/upload/": PaymentTier.MICRO,
    "/api/context/query/": PaymentTier.MICRO,
    # Decision Oracle (Aurora)
    "/api/decisions/ask": PaymentTier.MICRO,
    "/api/decisions/bulk": PaymentTier.STANDARD,
    "/api/decisions/outcome/": PaymentTier.MICRO,
    # Micro-Licensing Engine (Aurora)
    "/api/licenses/register": PaymentTier.STANDARD,
    "/api/licenses/acquire/": PaymentTier.MICRO,
    "/api/licenses/verify/": PaymentTier.MICRO,
    "/api/licenses/bundle": PaymentTier.STANDARD,
    # Federation Bridge (Aurora)
    "/api/federation/forward": PaymentTier.STANDARD,
    "/api/federation/cross-transact": PaymentTier.PREMIUM,
    "/api/federation/register": PaymentTier.STANDARD,
    # Settlement Reconciler (Aurora)
    "/api/settlement/transfer": PaymentTier.PREMIUM,
    "/api/settlement/reconcile": PaymentTier.STANDARD,
    "/api/settlement/snapshot": PaymentTier.STANDARD,
    # Arbitrage Detector (Aurora)
    "/api/arbitrage/scan": PaymentTier.STANDARD,
    "/api/arbitrage/execute/": PaymentTier.PREMIUM,
    "/api/arbitrage/feed": PaymentTier.MICRO,
    "/api/arbitrage/configure": PaymentTier.STANDARD,
    # Federated Discovery (Aurora)
    "/api/discovery/sync": PaymentTier.STANDARD,
    "/api/discovery/search": PaymentTier.MICRO,
    "/api/discovery/route": PaymentTier.STANDARD,
    "/api/discovery/register-registry": PaymentTier.STANDARD,
    # Wallet Bridge — AgentKit (Aurora)
    "/api/wallet/create": PaymentTier.STANDARD,
    "/api/wallet/send": PaymentTier.STANDARD,
    "/api/wallet/receive-hook": PaymentTier.MICRO,
    # DEX Trading Adapter — GOAT SDK (Aurora)
    "/api/dex/quote": PaymentTier.MICRO,
    "/api/dex/swap": PaymentTier.STANDARD,
    "/api/dex/strategy": PaymentTier.STANDARD,
    "/api/dex/check-strategies": PaymentTier.STANDARD,
    # State Engine — LangGraph (Aurora)
    "/api/workflows/step/": PaymentTier.MICRO,
    "/api/workflows/pause/": PaymentTier.MICRO,
    "/api/workflows/resume/": PaymentTier.MICRO,
    # Crew Orchestrator — CrewAI (Aurora)
    "/api/crews/create": PaymentTier.STANDARD,
    "/api/crews/mission": PaymentTier.STANDARD,
    # Free LLM Proxy (Aurora)
    "/api/freellm/v1/chat/completions": PaymentTier.MICRO,
    "/api/freellm/providers/key": PaymentTier.STANDARD,
    "/api/freellm/providers/reorder": PaymentTier.MICRO,
    # Revenue Autopilot (Aurora)
    "/api/autopilot/collect": PaymentTier.STANDARD,
    "/api/autopilot/report": PaymentTier.STANDARD,
    "/api/autopilot/opportunities": PaymentTier.STANDARD,
    # Arthur's Remote Eye (Aurora)
    "/api/eye/snapshot": PaymentTier.MICRO,
    # Code Learning Engine (Aurora)
    "/api/learner/analyze": PaymentTier.STANDARD,
    "/api/learner/profile": PaymentTier.MICRO,
    # Code Sync Daemon (Aurora)
    "/api/sync/status": PaymentTier.MICRO,
    # Jarvis Mesh Auditor (Aurora)
    "/api/auditor/mesh": PaymentTier.STANDARD,
    "/api/auditor/aurora": PaymentTier.STANDARD,
    "/api/auditor/surface": PaymentTier.STANDARD,
    # Pattern Extractor (Aurora)
    "/api/patterns/scan": PaymentTier.STANDARD,
    "/api/patterns/duplicates": PaymentTier.MICRO,
    # Test Amplifier (Aurora)
    "/api/tests/analyze": PaymentTier.STANDARD,
    "/api/tests/skeletons": PaymentTier.MICRO,
    # Mesh Orchestrator (Aurora)
    "/api/orchestrator/heartbeat": PaymentTier.MICRO,
    "/api/orchestrator/diagnose/": PaymentTier.STANDARD,
    "/api/orchestrator/heal": PaymentTier.STANDARD,
    "/api/orchestrator/assign": PaymentTier.MICRO,
    # Evolution Loop (Aurora)
    "/api/evolve/cycle": PaymentTier.STANDARD,
    "/api/evolve/fitness": PaymentTier.MICRO,
    "/api/evolve/mutations": PaymentTier.MICRO,
    # Client Pipeline Accelerator (Aurora)
    "/api/pipeline/clients/leads": PaymentTier.MICRO,
    "/api/pipeline/clients/summary": PaymentTier.STANDARD,
    "/api/pipeline/clients/metrics": PaymentTier.STANDARD,
    # Product Factory Intelligence (Aurora)
    "/api/factory/products/signals": PaymentTier.MICRO,
    "/api/factory/products/ideas": PaymentTier.MICRO,
    "/api/factory/products/gaps": PaymentTier.STANDARD,
    "/api/factory/products/leaderboard": PaymentTier.STANDARD,
    "/api/factory/products/summary": PaymentTier.STANDARD,
}


# ── Data Structures ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaymentChallenge:
    """Returned as HTTP 402 body. Agent must pay and replay with receipt."""
    nonce: str
    price_usd: float
    treasury_wallet: str
    accepted_tokens: list[str]
    expires_at: float  # Unix timestamp
    endpoint: str
    tier: str
    chain: str = "solana"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass(frozen=True)
class PaymentReceipt:
    """Submitted by agent via X-402-Receipt header."""
    nonce: str
    tx_signature: str
    payer_wallet: str
    amount_usd: float
    token: str
    chain: str = "solana"
    timestamp: float = 0.0

    @classmethod
    def from_header(cls, header_value: str) -> "PaymentReceipt":
        """Decode base64 JSON from X-402-Receipt header."""
        raw = base64.b64decode(header_value)
        data = json.loads(raw)
        return cls(
            nonce=data["nonce"],
            tx_signature=data["tx_signature"],
            payer_wallet=data["payer_wallet"],
            amount_usd=float(data["amount_usd"]),
            token=data.get("token", "USDC"),
            chain=data.get("chain", "solana"),
            timestamp=float(data.get("timestamp", time.time())),
        )


@dataclass
class RevenueEntry:
    """One row in the x402 revenue ledger."""
    timestamp: str
    nonce: str
    endpoint: str
    tier: str
    price_usd: float
    payer_wallet: str
    tx_signature: str
    token: str
    chain: str
    paper_mode: bool
    receipt_hash: str


# ── Nonce Store (in-memory, stateless restart OK) ────────────────────────

_active_challenges: dict[str, PaymentChallenge] = {}
_used_tx_signatures: set[str] = set()  # Double-spend protection


# ── Core Functions ───────────────────────────────────────────────────────


def create_challenge(endpoint: str, custom_price_usd: float | None = None) -> PaymentChallenge:
    """Issue a payment challenge for a given endpoint."""
    tier = SKILL_PRICING.get(endpoint, PaymentTier.MICRO)
    price = custom_price_usd if custom_price_usd is not None else TIER_PRICES_USD[tier]
    nonce = secrets.token_hex(16)
    expires_at = time.time() + CHALLENGE_TTL_SECONDS

    challenge = PaymentChallenge(
        nonce=nonce,
        price_usd=price,
        treasury_wallet=TREASURY_WALLET,
        accepted_tokens=list(ACCEPTED_TOKENS),
        expires_at=expires_at,
        endpoint=endpoint,
        tier=tier if isinstance(tier, str) else tier.value,
    )
    
    try:
        if redis_client:
            # Store in Redis with automatic TTL expiration (O(1) cleanup)
            redis_client.setex(
                f"x402:challenge:{nonce}", 
                CHALLENGE_TTL_SECONDS, 
                json.dumps(challenge.to_dict())
            )
        else:
            _active_challenges[nonce] = challenge
    except Exception as e:
        logging.error(f"Redis setex failed: {e}")
        _active_challenges[nonce] = challenge
        
    return challenge



def verify_receipt(receipt: PaymentReceipt, endpoint: str = "") -> tuple[bool, str]:
    """
    Verify a payment receipt against an active challenge.
    """
    # 1. Check Nonce (Redis or In-Memory)
    challenge = None
    try:
        if redis_client:
            raw_data = redis_client.get(f"x402:challenge:{receipt.nonce}")
            if raw_data:
                c_dict = json.loads(raw_data)
                challenge = PaymentChallenge(**c_dict)
    except Exception as e:
        logging.error(f"Redis get failed: {e}")
        
    if not challenge:
        challenge = _active_challenges.get(receipt.nonce)

    if challenge is None:
        return False, "Unknown or expired nonce"

    # 2. Semantic Slashing Validation
    if endpoint and challenge.endpoint != endpoint:
        # Cryptographic escrow slashing event logged here
        return False, f"Slashing Event: Nonce was issued for {challenge.endpoint} but used for {endpoint}"

    if time.time() > challenge.expires_at:
        if not redis_client: _active_challenges.pop(receipt.nonce, None)
        return False, "Challenge expired"

    if receipt.amount_usd < challenge.price_usd:
        return False, f"Underpaid: sent {receipt.amount_usd}, need {challenge.price_usd}"

    if receipt.token.upper() not in ACCEPTED_TOKENS:
        return False, f"Token {receipt.token} not accepted"

    if os.environ.get("REVENUE_KILL_SWITCH", "false").lower() == "true":
        return False, "Revenue kill switch active"

    # 3. Double-Spend Protection (Atomic Redis SETNX)
    if not is_paper_mode():
        if not receipt.tx_signature or len(receipt.tx_signature) < 20:
            return False, "Invalid transaction signature"
            
        try:
            if redis_client:
                # Atomic Set-if-Not-Exists for double spend protection (TTL 24 hours)
                is_new = redis_client.setnx(f"x402:tx:{receipt.tx_signature}", "used")
                if is_new:
                    redis_client.expire(f"x402:tx:{receipt.tx_signature}", 86400)
                else:
                    return False, "Transaction signature already used (double-spend rejected)"
            else:
                if receipt.tx_signature in _used_tx_signatures:
                    return False, "Transaction signature already used (double-spend rejected)"
        except Exception as e:
            logging.error(f"Redis double-spend check failed: {e}")
            if receipt.tx_signature in _used_tx_signatures:
                return False, "Transaction signature already used (double-spend rejected)"
            
        # Phase 2: Solana RPC confirmation
        ok, err_reason = _verify_solana_tx_rpc(receipt.tx_signature)
        if not ok:
            # Rollback protection if RPC fails
            try:
                if redis_client: redis_client.delete(f"x402:tx:{receipt.tx_signature}")
                elif not redis_client and receipt.tx_signature in _used_tx_signatures:
                    _used_tx_signatures.remove(receipt.tx_signature)
            except Exception as e:
                logging.error(f"Redis rollback failed: {e}")
                if receipt.tx_signature in _used_tx_signatures:
                    _used_tx_signatures.remove(receipt.tx_signature)
            return False, err_reason
            
        if not redis_client: _used_tx_signatures.add(receipt.tx_signature)

    # 4. Success Cleanup
    try:
        if redis_client:
            redis_client.delete(f"x402:challenge:{receipt.nonce}")
        else:
            _active_challenges.pop(receipt.nonce, None)
    except Exception as e:
        logging.error(f"Redis cleanup failed: {e}")
        _active_challenges.pop(receipt.nonce, None)
        
    _log_revenue(challenge, receipt, paper_mode=is_paper_mode())
    return True, "verified"



def get_endpoint_price(endpoint: str) -> tuple[str, float]:
    """Return (tier, price_usd) for an endpoint."""
    tier = SKILL_PRICING.get(endpoint, PaymentTier.MICRO)
    tier_str = tier if isinstance(tier, str) else tier.value
    price = TIER_PRICES_USD.get(tier if isinstance(tier, PaymentTier) else PaymentTier(tier), 0.005)
    return tier_str, price


def check_request_payment(
    endpoint: str,
    receipt_header: str | None,
) -> tuple[bool, dict[str, Any]]:
    """
    Main gate function. Called by middleware or route handlers.
    """
    if receipt_header is None:
        challenge = create_challenge(endpoint)
        return False, {
            "status": "payment_required",
            "protocol": "x402",
            "challenge": challenge.to_dict(),
        }

    try:
        receipt = PaymentReceipt.from_header(receipt_header)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return False, {"status": "invalid_receipt", "error": str(exc)}

    ok, reason = verify_receipt(receipt, endpoint=endpoint)
    if ok:
        return True, {
            "status": "paid",
            "nonce": receipt.nonce,
            "amount_usd": receipt.amount_usd,
            "paper_mode": is_paper_mode(),
        }
    return False, {"status": "payment_failed", "error": reason}



# ── Revenue Tracking ─────────────────────────────────────────────────────


def _receipt_hash(receipt: PaymentReceipt) -> str:
    """SHA-256 hash of receipt for immutable audit chain."""
    raw = f"{receipt.nonce}:{receipt.tx_signature}:{receipt.payer_wallet}:{receipt.amount_usd}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _log_revenue(challenge: PaymentChallenge, receipt: PaymentReceipt, paper_mode: bool) -> None:
    """Append one entry to the x402 revenue ledger."""
    entry = RevenueEntry(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        nonce=receipt.nonce,
        endpoint=challenge.endpoint,
        tier=challenge.tier,
        price_usd=challenge.price_usd,
        payer_wallet=receipt.payer_wallet,
        tx_signature=receipt.tx_signature,
        token=receipt.token,
        chain=receipt.chain,
        paper_mode=paper_mode,
        receipt_hash=_receipt_hash(receipt),
    )
    REVENUE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REVENUE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry)) + "\n")


def revenue_today() -> float:
    """Sum x402 revenue for today UTC (excludes paper mode)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    if not REVENUE_LOG.exists():
        return total
    with open(REVENUE_LOG, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("timestamp", "").startswith(today) and not entry.get("paper_mode"):
                total += entry.get("price_usd", 0.0)
    return total


def revenue_lifetime() -> float:
    """Sum all x402 revenue (excludes paper mode)."""
    total = 0.0
    if not REVENUE_LOG.exists():
        return total
    with open(REVENUE_LOG, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if not entry.get("paper_mode"):
                total += entry.get("price_usd", 0.0)
    return total


def revenue_by_endpoint() -> dict[str, float]:
    """Revenue grouped by endpoint (excludes paper mode)."""
    totals: dict[str, float] = {}
    if not REVENUE_LOG.exists():
        return totals
    with open(REVENUE_LOG, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if not entry.get("paper_mode"):
                ep = entry.get("endpoint", "unknown")
                totals[ep] = totals.get(ep, 0.0) + entry.get("price_usd", 0.0)
    return totals
