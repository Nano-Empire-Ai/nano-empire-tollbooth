"""
nano_empire_tollbooth.tollbooth
===============================
Monetize any Python function with one decorator.

    from nano_empire_tollbooth import monetize

    @monetize(price_usd=0.01)
    def summarize(text: str) -> str:
        return my_llm(text)

Free tier: 100 paper-mode calls. Upgrade to Tollbooth Pro ($19/mo) for live payments.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, TypeVar, overload

from pydantic import BaseModel, ConfigDict, Field

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger("nano_empire_tollbooth")


# ── Configuration ────────────────────────────────────────────────────────

class TollboothMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class TollboothConfig(BaseModel):
    """Configuration for the tollbooth."""

    model_config = ConfigDict(extra="allow")

    # Pricing
    toll_per_message_usd: float = Field(default=0.001, ge=0.0)
    toll_per_message_tokens: float = Field(default=0.0, ge=0.0)

    # Settlement
    ledger_path: Optional[Path] = None
    escrow_timeout_s: float = Field(default=300.0, ge=0.0)

    # Mode
    mode: TollboothMode = Field(default=TollboothMode.PAPER)
    paper_mode: bool = Field(default=True)  # override via env

    # x402 integration
    x402_endpoint: Optional[str] = None
    x402_api_key: Optional[str] = None

    # Limits
    max_daily_toll_per_agent: float = Field(default=10.0, ge=0.0)
    require_payment_before_route: bool = Field(default=False)

    # Observability
    metrics_enabled: bool = Field(default=True)

    def model_post_init(self, __context: Any) -> None:
        # Env override for paper mode
        paper_env = os.environ.get("TOLLBOOTH_PAPER_MODE", "").lower()
        if paper_env in ("1", "true", "yes"):
            self.paper_mode = True
            self.mode = TollboothMode.PAPER
        elif paper_env in ("0", "false", "no"):
            self.paper_mode = False
            self.mode = TollboothMode.LIVE

        # Default ledger path
        if self.ledger_path is None:
            self.ledger_path = Path(__file__).resolve().parent.parent / "logs" / "toll_ledger.jsonl"


# ── Data Models ──────────────────────────────────────────────────────────

class SettlementStatus(str, Enum):
    PENDING = "pending"
    ESCROW_LOCKED = "escrow_locked"
    RELEASED = "released"
    REFUNDED = "refunded"
    FAILED = "failed"


@dataclass
class TollRecord:
    """Single toll transaction record."""
    task_id: str
    source_agent: str
    target_agent: str
    amount_usd: float
    amount_tokens: float
    status: SettlementStatus = SettlementStatus.PENDING
    tx_signature: Optional[str] = None
    payer_wallet: Optional[str] = None
    nonce: str = ""
    settled_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "amount_usd": self.amount_usd,
            "amount_tokens": self.amount_tokens,
            "status": self.status.value,
            "tx_signature": self.tx_signature,
            "payer_wallet": self.payer_wallet,
            "nonce": self.nonce,
            "settled_at": self.settled_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TollRecord":
        d = d.copy()
        d["status"] = SettlementStatus(d["status"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def settlement_hash(self) -> str:
        raw = f"{self.task_id}:{self.nonce}:{self.tx_signature}:{self.created_at}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Tollbooth Core ────────────────────────────────────────────────────────

class Tollbooth:
    """
    Protocol-level tollbooth for A2A agent swarms.

    Usage:
        config = TollboothConfig(toll_per_message_usd=0.001)
        booth = Tollbooth(config)
        record = await booth.charge(task)
    """

    def __init__(self, config: Optional[TollboothConfig] = None) -> None:
        self.config = config or TollboothConfig()
        self._records: dict[str, TollRecord] = {}
        self._daily_totals: dict[str, float] = {}
        self._x402_verify: Optional[Callable] = None
        self._stats = {
            "messages_charged": 0,
            "total_usd": 0.0,
            "total_tokens": 0.0,
            "escrow_locked": 0,
            "escrow_released": 0,
            "escrow_refunded": 0,
            "paper_mode": self.config.paper_mode,
        }
        self._ledger_path = self.config.ledger_path
        if self._ledger_path:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────

    async def charge(
        self,
        task_id: str,
        source_agent: str,
        target_agent: str,
        payer_wallet: Optional[str] = None,
        tx_signature: Optional[str] = None,
    ) -> TollRecord:
        """
        Charge the toll for a routed message.

        Returns a TollRecord. If payment is required and not provided,
        returns a PENDING record (caller must retry with payment).
        """
        amount_usd = self.config.toll_per_message_usd
        amount_tokens = self.config.toll_per_message_tokens

        # Check daily limit
        if not self._check_daily_limit(source_agent, amount_usd):
            record = TollRecord(
                task_id=task_id,
                source_agent=source_agent,
                target_agent=target_agent,
                amount_usd=amount_usd,
                amount_tokens=amount_tokens,
                status=SettlementStatus.FAILED,
            )
            self._log_record(record)
            logger.warning("Daily toll limit exceeded for %s (task=%s)", source_agent, task_id)
            return record

        # Paper mode: immediate release
        if self.config.paper_mode:
            record = TollRecord(
                task_id=task_id,
                source_agent=source_agent,
                target_agent=target_agent,
                amount_usd=amount_usd,
                amount_tokens=amount_tokens,
                status=SettlementStatus.RELEASED,
                payer_wallet=payer_wallet or "paper_mode",
                tx_signature=tx_signature or "paper_mode",
                nonce=self._generate_nonce(),
                settled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self._records[task_id] = record
            self._update_stats(record)
            self._log_record(record)
            logger.debug("[PAPER] Charged $%.6f for task=%s", amount_usd, task_id)
            return record

        # Live mode: require x402 receipt
        if self.config.require_payment_before_route:
            if not payer_wallet or not tx_signature:
                record = TollRecord(
                    task_id=task_id,
                    source_agent=source_agent,
                    target_agent=target_agent,
                    amount_usd=amount_usd,
                    amount_tokens=amount_tokens,
                    status=SettlementStatus.PENDING,
                )
                self._records[task_id] = record
                logger.info("Payment required before route for task=%s", task_id)
                return record

        # x402 verification (if configured)
        if self._x402_verify and payer_wallet and tx_signature:
            valid = await self._verify_x402(payer_wallet, tx_signature, amount_usd)
            if not valid:
                record = TollRecord(
                    task_id=task_id,
                    source_agent=source_agent,
                    target_agent=target_agent,
                    amount_usd=amount_usd,
                    amount_tokens=amount_tokens,
                    status=SettlementStatus.FAILED,
                    payer_wallet=payer_wallet,
                    tx_signature=tx_signature,
                )
                self._records[task_id] = record
                logger.warning("x402 verification failed for task=%s", task_id)
                return record

        # Escrow locked
        nonce = self._generate_nonce()
        record = TollRecord(
            task_id=task_id,
            source_agent=source_agent,
            target_agent=target_agent,
            amount_usd=amount_usd,
            amount_tokens=amount_tokens,
            status=SettlementStatus.ESCROW_LOCKED,
            payer_wallet=payer_wallet or "live",
            tx_signature=tx_signature or "live",
            nonce=nonce,
        )
        self._records[task_id] = record
        self._update_stats(record)
        self._log_record(record)
        self._stats["escrow_locked"] += 1
        logger.info("[LIVE] Escrow locked $%.6f for task=%s", amount_usd, task_id)
        return record

    async def release(self, task_id: str) -> Optional[TollRecord]:
        """Release escrowed toll after successful task completion."""
        record = self._records.get(task_id)
        if record is None:
            logger.warning("No toll record found for task=%s", task_id)
            return None

        if record.status != SettlementStatus.ESCROW_LOCKED:
            logger.warning("Cannot release: task=%s status=%s", task_id, record.status.value)
            return record

        record.status = SettlementStatus.RELEASED
        record.settled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._stats["escrow_released"] += 1
        self._log_record(record)
        logger.info("Toll released $%.6f for task=%s", record.amount_usd, task_id)
        return record

    async def refund(self, task_id: str) -> Optional[TollRecord]:
        """Refund escrowed toll on task failure."""
        record = self._records.get(task_id)
        if record is None:
            logger.warning("No toll record found for task=%s", task_id)
            return None

        if record.status != SettlementStatus.ESCROW_LOCKED:
            logger.warning("Cannot refund: task=%s status=%s", task_id, record.status.value)
            return record

        record.status = SettlementStatus.REFUNDED
        record.settled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._stats["escrow_refunded"] += 1
        self._log_record(record)
        logger.info("Toll refunded $%.6f for task=%s", record.amount_usd, task_id)
        return record

    def get_record(self, task_id: str) -> Optional[TollRecord]:
        """Get toll record by task ID."""
        return self._records.get(task_id)

    def get_stats(self) -> dict[str, Any]:
        """Get tollbooth statistics."""
        return {
            **self._stats,
            "active_escrows": sum(1 for r in self._records.values() if r.status == SettlementStatus.ESCROW_LOCKED),
            "total_records": len(self._records),
        }

    def reset_daily(self) -> None:
        """Reset daily agent totals (call at midnight)."""
        self._daily_totals.clear()

    # ── x402 Integration ────────────────────────────────────────────────

    def set_x402_verifier(self, verifier: Callable[[str, str, float], Coroutine[Any, Any, bool]]) -> None:
        """Set async x402 payment verifier: (payer_wallet, tx_signature, amount) -> bool."""
        self._x402_verify = verifier

    async def _verify_x402(self, payer_wallet: str, tx_signature: str, amount: float) -> bool:
        if self._x402_verify is None:
            return True  # no verifier configured, trust
        try:
            return await self._x402_verify(payer_wallet, tx_signature, amount)
        except Exception as exc:
            logger.error("x402 verification error: %s", exc)
            return False

    # ── Internal Helpers ────────────────────────────────────────────────

    def _check_daily_limit(self, agent: str, amount: float) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{agent}:{today}"
        current = self._daily_totals.get(key, 0.0)
        return (current + amount) <= self.config.max_daily_toll_per_agent

    def _update_stats(self, record: TollRecord) -> None:
        self._stats["messages_charged"] += 1
        self._stats["total_usd"] += record.amount_usd
        self._stats["total_tokens"] += record.amount_tokens
        # Daily tracking
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{record.source_agent}:{today}"
        self._daily_totals[key] = self._daily_totals.get(key, 0.0) + record.amount_usd

    def _log_record(self, record: TollRecord) -> None:
        if self._ledger_path is None:
            return
        try:
            entry = record.to_dict()
            entry["_footer"] = "Powered by Nano Empire — pip install nano-empire-tollbooth"
            with open(self._ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.error("Failed to write toll ledger: %s", exc)

    @staticmethod
    def _generate_nonce() -> str:
        return hashlib.sha256(f"{time.time()}{os.urandom(16).hex()}".encode()).hexdigest()[:32]


# ── Singleton + Convenience API ──────────────────────────────────────────

_tollbooth: Optional[Tollbooth] = None


def create_tollbooth(config: Optional[TollboothConfig] = None) -> Tollbooth:
    """Create a new Tollbooth instance."""
    return Tollbooth(config=config)


def get_tollbooth(config: Optional[TollboothConfig] = None) -> Tollbooth:
    """Get or create the global Tollbooth singleton."""
    global _tollbooth
    if _tollbooth is None:
        _tollbooth = Tollbooth(config=config)
    return _tollbooth


def reset_tollbooth() -> None:
    """Reset the global Tollbooth singleton (for testing)."""
    global _tollbooth
    _tollbooth = None


# ── Usage Counter ───────────────────────────────────────────────────────

_UPGRADE_URL = "https://buy.stripe.com/14A9ATaI76K8gjo9JE1Nu0h"
_FREE_LIMIT = 100

_usage_counts: dict[str, int] = {}
_upgrade_warned: set[str] = set()


def _get_fn_key(fn: Callable[..., Any]) -> str:
    module = getattr(fn, "__module__", "__unknown__")
    qualname = getattr(fn, "__qualname__", fn.__name__)
    return f"{module}.{qualname}"


def _check_usage(fn_key: str, price_usd: float) -> bool:
    """
    Track usage. Returns True if the call should proceed.
    After FREE_LIMIT calls, prints upgrade prompt and still allows
    the call (paper mode continues working, just nags).
    """
    count = _usage_counts.get(fn_key, 0) + 1
    _usage_counts[fn_key] = count

    if count == _FREE_LIMIT:
        print(
            f"\n{'=' * 60}\n"
            f"  nano-empire-tollbooth: {_FREE_LIMIT} free calls used\n"
            f"  Upgrade to Tollbooth Pro — $19/mo for live payments\n"
            f"  {_UPGRADE_URL}\n"
            f"{'=' * 60}\n",
            file=sys.stderr,
        )
    elif count > _FREE_LIMIT and fn_key not in _upgrade_warned:
        _upgrade_warned.add(fn_key)
        logger.info(
            "Tollbooth free tier exceeded for %s (%d calls). "
            "Upgrade: %s",
            fn_key, count, _UPGRADE_URL,
        )
    return True


def get_usage(fn: Optional[Callable[..., Any]] = None) -> dict[str, int]:
    """Get usage counts. Pass a function to get its count, or None for all."""
    if fn is not None:
        key = _get_fn_key(fn)
        return {key: _usage_counts.get(key, 0)}
    return dict(_usage_counts)


def reset_usage() -> None:
    """Reset all usage counters (for testing)."""
    _usage_counts.clear()
    _upgrade_warned.clear()


# ── @monetize Decorator ─────────────────────────────────────────────────

def monetize(
    config: Optional[TollboothConfig] = None,
    price_usd: float = 0.001,
    *,
    capability: str = "",
    source_agent: str = "caller",
    target_agent: str = "self",
) -> Callable[[F], F]:
    """
    Wrap any Python function with a tollbooth charge.

    Usage:
        @monetize(price_usd=0.01)
        def summarize(text: str) -> str:
            return my_llm(text)

        @monetize(price_usd=0.05)
        async def translate(text: str, lang: str) -> str:
            return await my_async_llm(text, lang)

    First 100 calls are free (paper mode). After that, prints an upgrade
    prompt to stderr. The function keeps working — we nag, not block.

    Args:
        price_usd: toll per call in USD (default $0.001)
        source_agent: agent identity of the caller
        target_agent: agent identity of the function owner
    """
    def decorator(fn: F) -> F:
        fn_key = _get_fn_key(fn)
        booth = get_tollbooth()

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check_usage(fn_key, price_usd)
                task_id = f"{fn_key}:{_usage_counts.get(fn_key, 0)}:{time.time():.0f}"

                old_toll = booth.config.toll_per_message_usd
                booth.config.toll_per_message_usd = price_usd
                record = await booth.charge(
                    task_id=task_id,
                    source_agent=source_agent,
                    target_agent=target_agent,
                )
                booth.config.toll_per_message_usd = old_toll

                result = await fn(*args, **kwargs)

                if record.status == SettlementStatus.ESCROW_LOCKED:
                    await booth.release(task_id)

                return result

            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check_usage(fn_key, price_usd)
                task_id = f"{fn_key}:{_usage_counts.get(fn_key, 0)}:{time.time():.0f}"

                old_toll = booth.config.toll_per_message_usd
                booth.config.toll_per_message_usd = price_usd

                # Handle both "no loop running" and "loop already running" cases
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None:
                    # Already in an async context — create a task (caller must await)
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        record = pool.submit(
                            asyncio.run,
                            booth.charge(task_id=task_id, source_agent=source_agent, target_agent=target_agent)
                        ).result()
                else:
                    record = asyncio.run(
                        booth.charge(task_id=task_id, source_agent=source_agent, target_agent=target_agent)
                    )
                booth.config.toll_per_message_usd = old_toll

                result = fn(*args, **kwargs)

                if record.status == SettlementStatus.ESCROW_LOCKED:
                    if loop is not None:
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pool.submit(asyncio.run, booth.release(task_id)).result()
                    else:
                        asyncio.run(booth.release(task_id))

                return result

            return sync_wrapper  # type: ignore[return-value]

    return decorator
