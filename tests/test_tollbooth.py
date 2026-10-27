"""
Tests for nano_empire_tollbooth
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from nano_empire_tollbooth import (
    SettlementStatus,
    TollRecord,
    Tollbooth,
    TollboothConfig,
    TollboothMode,
    create_tollbooth,
    get_tollbooth,
    get_usage,
    monetize,
    reset_tollbooth,
    reset_usage,
)


@pytest.fixture()
def tmp_ledger(tmp_path: Path) -> Path:
    return tmp_path / "toll_ledger.jsonl"


@pytest.fixture()
def booth(tmp_ledger: Path) -> Tollbooth:
    config = TollboothConfig(
        toll_per_message_usd=0.001,
        ledger_path=tmp_ledger,
        paper_mode=True,
    )
    return Tollbooth(config=config)


# ── Configuration ─────────────────────────────────────────────────────────

class TestTollboothConfig:
    def test_default_config(self):
        config = TollboothConfig()
        assert config.toll_per_message_usd == 0.001
        assert config.paper_mode is True
        assert config.mode == TollboothMode.PAPER

    def test_env_paper_mode_override(self, tmp_path: Path):
        os.environ["TOLLBOOTH_PAPER_MODE"] = "false"
        try:
            config = TollboothConfig(ledger_path=tmp_path / "ledger.jsonl")
            assert config.paper_mode is False
            assert config.mode == TollboothMode.LIVE
        finally:
            del os.environ["TOLLBOOTH_PAPER_MODE"]

    def test_custom_toll_rate(self):
        config = TollboothConfig(toll_per_message_usd=0.005)
        assert config.toll_per_message_usd == 0.005


# ── TollRecord ────────────────────────────────────────────────────────────

class TestTollRecord:
    def test_creation(self):
        record = TollRecord(
            task_id="t1",
            source_agent="a1",
            target_agent="a2",
            amount_usd=0.001,
            amount_tokens=0.0,
        )
        assert record.task_id == "t1"
        assert record.status == SettlementStatus.PENDING
        assert record.settlement_hash != ""

    def test_hash_length(self):
        record = TollRecord(
            task_id="t1", source_agent="a1", target_agent="a2",
            amount_usd=0.001, amount_tokens=0.0, nonce="abc",
        )
        assert len(record.settlement_hash) == 16

    def test_to_dict_roundtrip(self):
        record = TollRecord(
            task_id="t1",
            source_agent="a1",
            target_agent="a2",
            amount_usd=0.001,
            amount_tokens=0.0,
            status=SettlementStatus.RELEASED,
        )
        d = record.to_dict()
        assert d["status"] == "released"
        restored = TollRecord.from_dict(d)
        assert restored.status == SettlementStatus.RELEASED
        assert restored.task_id == "t1"


# ── Tollbooth Charge (Paper Mode) ─────────────────────────────────────────

class TestChargePaperMode:
    @pytest.mark.asyncio
    async def test_charge_creates_released_record(self, booth: Tollbooth):
        record = await booth.charge("t1", "agent-a", "agent-b")
        assert record.status == SettlementStatus.RELEASED
        assert record.amount_usd == 0.001
        assert record.payer_wallet == "paper_mode"

    @pytest.mark.asyncio
    async def test_charge_updates_stats(self, booth: Tollbooth):
        await booth.charge("t1", "agent-a", "agent-b")
        stats = booth.get_stats()
        assert stats["messages_charged"] == 1
        assert stats["total_usd"] == 0.001
        assert stats["paper_mode"] is True

    @pytest.mark.asyncio
    async def test_charge_writes_ledger(self, booth: Tollbooth, tmp_ledger: Path):
        await booth.charge("t1", "agent-a", "agent-b")
        assert tmp_ledger.exists()
        lines = tmp_ledger.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["task_id"] == "t1"
        assert entry["status"] == "released"

    @pytest.mark.asyncio
    async def test_multiple_charges_append(self, booth: Tollbooth, tmp_ledger: Path):
        await booth.charge("t1", "a", "b")
        await booth.charge("t2", "a", "b")
        lines = tmp_ledger.read_text().strip().splitlines()
        assert len(lines) == 2


# ── Daily Limits ──────────────────────────────────────────────────────────

class TestDailyLimits:
    @pytest.mark.asyncio
    async def test_within_daily_limit(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            max_daily_toll_per_agent=0.005,
            ledger_path=tmp_ledger,
            paper_mode=True,
        )
        booth = Tollbooth(config=config)
        for i in range(5):
            record = await booth.charge(f"t{i}", "agent-a", "agent-b")
            assert record.status != SettlementStatus.FAILED
        # 6th should fail (5 * 0.001 = 0.005, next pushes over)
        record = await booth.charge("t6", "agent-a", "agent-b")
        assert record.status == SettlementStatus.FAILED


# ── Escrow Lifecycle ──────────────────────────────────────────────────────

class TestEscrowLifecycle:
    @pytest.mark.asyncio
    async def test_live_mode_locks_escrow(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            ledger_path=tmp_ledger,
            paper_mode=False,
            require_payment_before_route=False,
        )
        booth = Tollbooth(config=config)
        record = await booth.charge("t1", "a", "b")
        assert record.status == SettlementStatus.ESCROW_LOCKED
        assert record.settled_at is None

    @pytest.mark.asyncio
    async def test_release_changes_status(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            ledger_path=tmp_ledger,
            paper_mode=False,
            require_payment_before_route=False,
        )
        booth = Tollbooth(config=config)
        await booth.charge("t1", "a", "b")
        record = await booth.release("t1")
        assert record is not None
        assert record.status == SettlementStatus.RELEASED
        assert record.settled_at is not None

    @pytest.mark.asyncio
    async def test_refund_changes_status(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            ledger_path=tmp_ledger,
            paper_mode=False,
            require_payment_before_route=False,
        )
        booth = Tollbooth(config=config)
        await booth.charge("t1", "a", "b")
        record = await booth.refund("t1")
        assert record is not None
        assert record.status == SettlementStatus.REFUNDED

    @pytest.mark.asyncio
    async def test_double_release_returns_existing(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            ledger_path=tmp_ledger,
            paper_mode=False,
            require_payment_before_route=False,
        )
        booth = Tollbooth(config=config)
        await booth.charge("t1", "a", "b")
        r1 = await booth.release("t1")
        r2 = await booth.release("t1")
        assert r1 is r2  # same object, already released

    @pytest.mark.asyncio
    async def test_refund_without_escrow_returns_none(self, booth: Tollbooth):
        result = await booth.refund("nonexistent")
        assert result is None


# ── Singleton API ─────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_tollbooth_returns_singleton(self):
        reset_tollbooth()
        b1 = get_tollbooth()
        b2 = get_tollbooth()
        assert b1 is b2

    def test_reset_creates_new_instance(self):
        b1 = get_tollbooth()
        reset_tollbooth()
        b2 = get_tollbooth()
        assert b1 is not b2

    def test_create_always_new(self):
        b1 = create_tollbooth()
        b2 = create_tollbooth()
        assert b1 is not b2


# ── x402 Integration ──────────────────────────────────────────────────────

class TestX402Integration:
    @pytest.mark.asyncio
    async def test_no_verifier_passes_through(self, booth: Tollbooth):
        # No x402 verifier set, should not block
        record = await booth.charge("t1", "a", "b", payer_wallet="w", tx_signature="sig")
        assert record.status == SettlementStatus.RELEASED  # paper mode

    @pytest.mark.asyncio
    async def test_verifier_rejects_payment(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            ledger_path=tmp_ledger,
            paper_mode=False,
            require_payment_before_route=True,
        )
        booth = Tollbooth(config=config)

        async def reject(_w, _s, _a):
            return False

        booth.set_x402_verifier(reject)
        record = await booth.charge("t1", "a", "b", payer_wallet="w", tx_signature="sig")
        assert record.status == SettlementStatus.FAILED

    @pytest.mark.asyncio
    async def test_verifier_accepts_payment(self, tmp_ledger: Path):
        config = TollboothConfig(
            toll_per_message_usd=0.001,
            ledger_path=tmp_ledger,
            paper_mode=False,
            require_payment_before_route=True,
        )
        booth = Tollbooth(config=config)

        async def accept(_w, _s, _a):
            return True

        booth.set_x402_verifier(accept)
        record = await booth.charge("t1", "a", "b", payer_wallet="w", tx_signature="sig")
        assert record.status == SettlementStatus.ESCROW_LOCKED


# ── Stats & Observability ─────────────────────────────────────────────────

class TestStats:
    @pytest.mark.asyncio
    async def test_stats_accumulate(self, booth: Tollbooth):
        await booth.charge("t1", "a", "b")
        await booth.charge("t2", "a", "b")
        stats = booth.get_stats()
        assert stats["messages_charged"] == 2
        assert stats["total_usd"] == 0.002

    @pytest.mark.asyncio
    async def test_reset_daily(self, booth: Tollbooth):
        await booth.charge("t1", "agent-a", "agent-b")
        booth.reset_daily()
        stats = booth.get_stats()
        assert stats["messages_charged"] == 1  # main stats not reset


# ── Record Retrieval ──────────────────────────────────────────────────────

class TestRecordRetrieval:
    @pytest.mark.asyncio
    async def test_get_existing_record(self, booth: Tollbooth):
        await booth.charge("t1", "a", "b")
        record = booth.get_record("t1")
        assert record is not None
        assert record.task_id == "t1"

    @pytest.mark.asyncio
    async def test_get_missing_record(self, booth: Tollbooth):
        record = booth.get_record("nonexistent")
        assert record is None


# ── @monetize Decorator ──────────────────────────────────────────────────

class TestMonetizeDecorator:
    def setup_method(self):
        reset_tollbooth()
        reset_usage()

    def test_sync_function_returns_result(self):
        @monetize(price_usd=0.01)
        def add(a: int, b: int) -> int:
            return a + b

        result = add(2, 3)
        assert result == 5

    def test_sync_function_tracks_usage(self):
        @monetize(price_usd=0.01)
        def double(x: int) -> int:
            return x * 2

        double(5)
        double(10)
        usage = get_usage(double)
        key = list(usage.keys())[0]
        assert usage[key] == 2

    @pytest.mark.asyncio
    async def test_async_function_returns_result(self):
        @monetize(price_usd=0.05)
        async def async_add(a: int, b: int) -> int:
            return a + b

        result = await async_add(3, 4)
        assert result == 7

    @pytest.mark.asyncio
    async def test_async_function_tracks_usage(self):
        @monetize(price_usd=0.05)
        async def async_mul(a: int, b: int) -> int:
            return a * b

        await async_mul(3, 4)
        await async_mul(5, 6)
        await async_mul(7, 8)
        usage = get_usage(async_mul)
        key = list(usage.keys())[0]
        assert usage[key] == 3

    def test_preserves_function_name(self):
        @monetize(price_usd=0.01)
        def my_func():
            pass

        assert my_func.__name__ == "my_func"

    def test_usage_counter_resets(self):
        @monetize(price_usd=0.01)
        def inc(x: int) -> int:
            return x + 1

        inc(1)
        reset_usage()
        usage = get_usage(inc)
        key = list(usage.keys())[0]
        assert usage[key] == 0

    def test_get_all_usage(self):
        @monetize(price_usd=0.01)
        def fn_a():
            return "a"

        @monetize(price_usd=0.02)
        def fn_b():
            return "b"

        fn_a()
        fn_b()
        fn_b()
        all_usage = get_usage()
        assert len(all_usage) >= 2
