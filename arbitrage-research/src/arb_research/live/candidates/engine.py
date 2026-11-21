"""Prospective T074/T071 comparators with isolated, durable virtual accounts.

Historical fee coefficient is deliberately frozen at .07 for comparability.
This is a research execution model, never an exchange order or fill promise.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
import hashlib
import gzip
from arb_research.platform_io import exclusive_lock
import json
import math
import os
from pathlib import Path
import threading
from zoneinfo import ZoneInfo
from uuid import uuid4

from ..log_storage import partition_lock

SECOND = 1_000_000
D = Decimal
FEE = D("0.07")
LEGACY_RULES = (
    ("S1_net", "翻转·保守", "S1", "10", True),
    ("S1_gross", "翻转·常规", "S1", "10", False),
    ("D1", "D1 距离＋时间", "D1", "14", False),
    ("S3", "S3 距离＋趋势", "S3", "14", False),
)
LEGACY_MODEL = {"version": "candidate-paper-v1", "rules": LEGACY_RULES,
         "decisionIntervalMs": 1000, "orderDelayMs": 300, "maxSchedulerLatenessMs": 1000,
         "minRemaining": 30, "maxRemaining": 270, "askMin": ".30", "askMax": ".50",
         "maxAgeMs": 2000, "closedBarLagMs": 1000, "settlementHoldSeconds": 60,
         "feeRate": str(FEE), "quantityStep": ".01", "fullBudgetTolerance": ".01",
         "fixedBankroll": "100", "martinBankroll": "2047", "martinLevels": 11,
         "distanceScoreMin": 1, "supertrendFactor": 3,
         "reentry": "first_attempt_per_account_market_side"}
RULES = LEGACY_RULES + (
    ("A5", "A5 ATR距离≥1.0", "ATR", "14", False),
    ("A6", "A6 ATR距离≥1.5", "ATR", "14", False),
)
# Additive registry extension only: the four existing rules and account IDs
# retain their exact execution model. Never accept an arbitrary old hash.
LEGACY_FINGERPRINT = hashlib.sha256(json.dumps(LEGACY_MODEL, sort_keys=True).encode()).hexdigest()
MODEL = {**LEGACY_MODEL, "rules": RULES, "atrDistanceThresholds": {"A5": 1.0, "A6": 1.5}}
FINGERPRINT = hashlib.sha256(json.dumps(MODEL, sort_keys=True).encode()).hexdigest()


def price_cap(net: bool) -> Decimal:
    if not net:
        return D(".50")
    low, high = D(0), D(".50")
    for _ in range(60):
        mid = (low + high) / 2
        if mid + FEE * mid * (1 - mid) <= D(".50"):
            low = mid
        else:
            high = mid
    return low


def buy_budget(levels, budget: Decimal, cap: Decimal):
    remaining, quantity, principal, fee = budget, D(0), D(0), D(0)
    worst = D(0)
    for raw_price, raw_size in sorted(levels, key=lambda r: D(str(r[0]))):
        p, size = D(str(raw_price)), D(str(raw_size))
        if not (0 < p <= cap and size > 0):
            continue
        unit_fee = FEE * p * (1 - p)
        take = (min(size, remaining / (p + unit_fee)) / D(".01")).to_integral_value(rounding=ROUND_FLOOR) * D(".01")
        if take <= 0:
            continue
        quantity += take
        principal += take * p
        fee += take * unit_fee
        remaining -= take * (p + unit_fee)
        worst = p
        if remaining <= D(".01"):
            break
    if remaining > D(".01") or quantity <= 0:
        return None
    return {key: str(value) for key, value in {
        "quantity": quantity, "principal": principal, "fee": fee, "cost": principal + fee,
        "vwap": principal / quantity, "effectivePrice": (principal + fee) / quantity,
        "worstPrice": worst}.items()}


def fresh(value: dict | None, now: int):
    return bool(value and all(0 <= now - int(value.get(key, 0)) <= 2 * SECOND
                              for key in ("sourceUs", "receivedUs")))


def week_key(at: int):
    date = datetime.fromtimestamp(at / SECOND, ZoneInfo("Asia/Shanghai")).date()
    return (date - timedelta(days=date.weekday())).isoformat()


class CandidateEngine:
    def __init__(self, directory: Path, started_us: int):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        try:
            self._writer_lock = exclusive_lock(directory / "writer.lock")
        except OSError:
            raise RuntimeError("已有进程使用候选账本，禁止重复启动同一数据目录") from None
        self.journal = directory / "accounts.ndjson"
        self.accounts = {}
        self.recent = []
        self.reasons = {}
        self.lock = threading.RLock()
        self._ready = False
        self._last_tick_us = started_us
        self.last_grid = None
        self.coverage = {"ticks": 0, "eligibleSeconds": 0, "freshBothBookSeconds": 0,
                         "freshSpotSeconds": 0, "indicatorReadySeconds": 0,
                         "anchorReadySeconds": 0, "missedDecisionSeconds": 0}
        self.first_us = started_us
        self.last_input = {}
        self.last_market = None
        self.last_health = None
        self.last_observation = 0
        self.failure = None
        self._observation_handle = None
        self._observation_lock = None
        self._observation_partition = None
        for rule, name, family, period, net in RULES:
            for sizing in ("fixed", "martin11"):
                key = f"{MODEL['version']}:{rule}:{sizing}"
                capital = "100" if sizing == "fixed" else "2047"
                self.accounts[key] = {
                    "id": key, "rule": rule, "name": name, "family": family, "period": period,
                    "netCap": net, "sizing": sizing, "initialCapital": capital, "cash": capital,
                    "pnl": "0", "spent": "0", "fees": "0", "peakPnl": "0", "maxDrawdown": "0",
                    "wins": 0, "losses": 0, "lossStreak": 0, "maxLossStreak": 0,
                    "step": 0, "capLossCycles": 0, "negativeWinCycles": 0, "cyclePnl": "0",
                    "availableUs": 0, "position": None, "pending": None, "attempted": [],
                    "fills": 0, "noFills": 0, "weekly": {}, "byOutcome": {
                        side: {"wins": 0, "losses": 0, "pnl": "0"} for side in ("UP", "DOWN")},
                    "maxBudget": "0", "rejections": {}}
        restored = set()
        try:
            if self.journal.exists():
                with self.journal.open() as source:
                    for line in source:
                        # A truncated/corrupt journal fails closed; never silently reset money.
                        row = json.loads(line)
                        if row["fingerprint"] not in (FINGERPRINT, LEGACY_FINGERPRINT):
                            raise ValueError("候选账本版本不匹配；请使用新版本目录，不能混入旧统计")
                        self.first_us = min(self.first_us, row["atUs"])
                        if "account" in row:
                            a = row["account"]
                            if a["id"] not in self.accounts or (
                                row["fingerprint"] == LEGACY_FINGERPRINT
                                and a["rule"] not in {r[0] for r in LEGACY_RULES}
                            ):
                                raise ValueError("候选账户不属于已知冻结规则")
                            self.accounts[a["id"]] = a
                            restored.add(a["id"])
                        if "coverage" in row:
                            self.coverage = row["coverage"]
                        if row["type"] != "OBSERVATION":
                            self.recent.append({k: v for k, v in row.items() if k != "account"})
                            self.recent = self.recent[-80:]
            for a in self.accounts.values():
                if a["id"] not in restored or "startedUs" not in a:
                    a["startedUs"] = self.first_us if a["rule"] in {r[0] for r in LEGACY_RULES} else started_us
                    self._append("ACCOUNT_STARTED", started_us, a, rule=a["rule"])
        except Exception:
            self.close()
            raise
        self.public_state = self.state()
        self._ready = True

    def _append(self, kind: str, now: int, account=None, **details):
        row = {"type": kind, "atUs": now, "fingerprint": FINGERPRINT, **details}
        if kind == "OBSERVATION":
            partition = datetime.fromtimestamp(now / SECOND, ZoneInfo("UTC")).strftime("%Y-%m-%d/%H")
            if partition != self._observation_partition:
                self._close_observations()
                # Never append a new gzip member behind a stream that an
                # earlier process may have left unfinished during a crash.
                path = self.directory / "observations" / f"{partition}.part-{uuid4().hex}.ndjson.gz"
                path.parent.mkdir(parents=True, exist_ok=True)
                self._observation_lock = partition_lock(path)
                try:
                    self._observation_handle = gzip.open(path, "xt", encoding="utf-8", compresslevel=3)
                except BaseException:
                    self._observation_lock.close()
                    self._observation_lock = None
                    raise
                self._observation_partition = partition
            self._observation_handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._observation_handle.flush()
            return
        if account is not None:
            row["accountId"] = account["id"]
            row["account"] = account
        # Called by the sole worker, off the market-data event loop.
        with self.journal.open("a", encoding="utf-8") as target:
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            target.flush()
            if account is not None:
                os.fsync(target.fileno())
        if account is not None:
            self.accounts[account["id"]] = account
        if kind != "OBSERVATION":
            self.recent.append({k: v for k, v in row.items() if k != "account"})
            self.recent = self.recent[-80:]

    def _close_observations(self):
        try:
            if self._observation_handle is not None:
                self._observation_handle.close()
        finally:
            self._observation_handle = None
            self._observation_partition = None
            if self._observation_lock is not None:
                self._observation_lock.close()
                self._observation_lock = None

    def close(self):
        with self.lock:
            try:
                if self._ready and not self._writer_lock.closed:
                    self._append("COVERAGE", self._last_tick_us, coverage=dict(self.coverage))
            finally:
                try:
                    self._close_observations()
                finally:
                    if not self._writer_lock.closed:
                        self._writer_lock.close()

    def open_markets(self):
        with self.lock:
            return [deepcopy(p["market"]) for a in self.accounts.values()
                    for p in (a["position"], a["pending"]) if p]

    def _signal(self, a, market, feed, now):
        remaining = (market["endUs"] - now) / SECOND
        if not 30 <= remaining <= 270:
            return None, "等待剩余270–30秒", {}
        row = feed.get("indicators", {}).get(a["period"])
        if not row or not (int(row["availableUs"]) <= now < int(row["barEndUs"]) + 60 * SECOND):
            return None, "等待新鲜已收盘1分钟指标（至少延后1秒）", {}
        features = {"indicator": row}
        if a["family"] == "S1":
            side = row.get("flip")
            return side, "等待ATR10×3真实翻转" if side is None else "翻转有效", features
        spot = feed.get("spot")
        if not fresh(spot, now):
            return None, "币安Spot缺失或超过2秒", features
        anchor = feed.get("opens", {}).get(str(market["startUs"]))
        if not anchor or anchor["receivedUs"] > now:
            return None, "缺少本场精确Binance开盘价", features
        atr = row.get("atr")
        if not atr or atr <= 0:
            return None, "ATR14预热中", features
        if a["family"] == "ATR":
            if spot["price"] <= 0 or anchor["price"] <= 0:
                return None, "Spot或本场开盘价无效", features
            # Exact T068 dynamic_signals formula, without D1's time scaling.
            signed_score = math.log(spot["price"] / anchor["price"]) / (atr / spot["price"])
            threshold = MODEL["atrDistanceThresholds"][a["rule"]]
            features.update(spot=spot, anchor=anchor, atrDistanceScore=signed_score,
                            atrDistanceThreshold=threshold)
            side = "UP" if signed_score >= threshold else "DOWN" if signed_score <= -threshold else None
            return side, "ATR距离条件满足" if side else f"ATR距离绝对值不足{threshold:.1f}", features
        difference = spot["price"] - anchor["price"]
        side = "UP" if difference > 0 else "DOWN" if difference < 0 else None
        score = abs(difference) / (atr * math.sqrt(remaining / 60))
        features.update(spot=spot, anchor=anchor, distanceScore=score)
        if side is None or score < 1:
            return None, "距离时间分数不足1", features
        if a["family"] == "S3" and row.get("direction") != side:
            return None, "Supertrend方向未确认或不一致", features
        return side, "方向条件满足", features

    def _no_fill(self, a, now, reason):
        p = a["pending"]
        a["pending"] = None
        a["noFills"] += 1
        self._append("NO_FILL", now, a, marketId=p["market"]["marketId"], side=p["side"], reason=reason)
        self.reasons[a["id"]] = reason

    def _execute(self, a, market, books, now):
        p = a["pending"]
        if now < p["dueUs"]:
            return
        if now - p["dueUs"] > SECOND:
            return self._no_fill(a, now, "执行调度超时，禁止用后来有利盘口补单")
        if not market or market["marketId"] != p["market"]["marketId"] or now >= p["market"]["endUs"]:
            return self._no_fill(a, now, "市场已切换或结束")
        book = books.get(p["side"])
        if not fresh(book, now):
            return self._no_fill(a, now, "执行时盘口缺失、来源时间缺失或陈旧")
        fill = buy_budget(book["asks"], D(p["budget"]), D(p["cap"]))
        if not fill:
            return self._no_fill(a, now, "执行时限价内不足完整预算")
        if a["netCap"] and D(fill["effectivePrice"]) > D(".50"):
            return self._no_fill(a, now, "执行时含费单位成本超过0.50")
        a["pending"] = None
        a["position"] = {**p, **fill, "executionUs": now, "executionBook": book,
                         "actualDelayMs": (now - p["decisionUs"]) / 1000}
        a["cash"] = str(D(a["cash"]) - D(fill["cost"]))
        a["spent"] = str(D(a["spent"]) + D(fill["cost"]))
        a["fees"] = str(D(a["fees"]) + D(fill["fee"]))
        a["fills"] += 1
        a["maxBudget"] = str(max(D(a["maxBudget"]), D(p["budget"])))
        week = week_key(now)
        a["weekly"][week] = a["weekly"].get(week, 0) + 1
        self._append("FILLED", now, a, marketId=market["marketId"], side=p["side"],
                     budget=p["budget"], **fill, actualDelayMs=a["position"]["actualDelayMs"])
        self.reasons[a["id"]] = "已模拟成交，等待官方结算"

    def settle(self, market_id, winner, now):
        with self.lock:
            for original in list(self.accounts.values()):
                p = original["position"]
                if not p or p["market"]["marketId"] != market_id:
                    continue
                a = deepcopy(original)
                won = p["side"] == winner
                payout = D(p["quantity"]) if won else D(0)
                pnl = payout - D(p["cost"])
                a["cash"] = str(D(a["cash"]) + payout)
                a["pnl"] = str(D(a["pnl"]) + pnl)
                a["peakPnl"] = str(max(D(a["peakPnl"]), D(a["pnl"])))
                a["maxDrawdown"] = str(max(D(a["maxDrawdown"]), D(a["peakPnl"]) - D(a["pnl"])))
                a["wins" if won else "losses"] += 1
                a["lossStreak"] = 0 if won else a["lossStreak"] + 1
                a["maxLossStreak"] = max(a["maxLossStreak"], a["lossStreak"])
                a["cyclePnl"] = str(D(a["cyclePnl"]) + pnl)
                if won:
                    a["negativeWinCycles"] += int(D(a["cyclePnl"]) < 0)
                cap_loss = not won and a["sizing"] == "martin11" and a["step"] == 10
                a["capLossCycles"] += int(cap_loss)
                a["step"] = 0 if won or cap_loss or a["sizing"] == "fixed" else a["step"] + 1
                if a["step"] == 0:
                    a["cyclePnl"] = "0"
                a["availableUs"] = max(now, p["market"]["endUs"] + 60 * SECOND)
                side_stats = a["byOutcome"][p["side"]]
                side_stats["wins" if won else "losses"] += 1
                side_stats["pnl"] = str(D(side_stats["pnl"]) + pnl)
                a["position"] = None
                self._append("SETTLED", now, a, marketId=market_id, side=p["side"],
                             winner=winner, won=won, pnl=str(pnl), executionUs=p["executionUs"])

    def tick(self, market, books, feed, settlements, now):
        with self.lock:
            if self.failure:
                return
            try:
                self._tick(market, books, feed, settlements, now)
            except Exception as exc:
                self.failure = f"候选账户暂停：{type(exc).__name__}: {exc}"
                raise
            finally:
                # Publish a replaced snapshot; the market loop never waits for
                # the account lock or a disk fsync to render dashboard state.
                self.public_state = self.state()

    def _tick(self, market, books, feed, settlements, now):
        self._last_tick_us = now
        self.last_input, self.last_market = feed, market
        for market_id, label in settlements.items():
            self.settle(market_id, label["winner"], label["receivedUs"])
        for original in list(self.accounts.values()):
            if original["pending"]:
                self._execute(deepcopy(original), market, books, now)
        grid = now // SECOND
        if grid == self.last_grid:
            return
        if self.last_grid is not None:
            self.coverage["missedDecisionSeconds"] += max(0, grid - self.last_grid - 1)
        self.last_grid = grid
        self.coverage["ticks"] += 1
        eligible = bool(market and 30 <= (market["endUs"] - now) / SECOND <= 270)
        self.coverage["eligibleSeconds"] += int(eligible)
        self.coverage["freshBothBookSeconds"] += int(eligible and all(fresh(books.get(s), now) for s in ("UP", "DOWN")))
        self.coverage["freshSpotSeconds"] += int(eligible and fresh(feed.get("spot"), now))
        self.coverage["anchorReadySeconds"] += int(eligible and str(market["startUs"]) in feed.get("opens", {}))
        self.coverage["indicatorReadySeconds"] += int(eligible and all(
            int(r["availableUs"]) <= now < int(r["barEndUs"]) + 60 * SECOND
            for r in feed.get("indicators", {}).values()) and len(feed.get("indicators", {})) == 2)
        health = (feed.get("status"), feed.get("error"))
        if health != self.last_health:
            self._append("FEED_STATUS", now, status=health[0], error=health[1])
            self.last_health = health
        # One shared compact observation per second for all virtual accounts.
        if eligible:
            self._append("OBSERVATION", now, market=market, books=books,
                         spot=feed.get("spot"), indicators=feed.get("indicators"),
                         anchor=feed.get("opens", {}).get(str(market["startUs"])), coverage=dict(self.coverage))
        if now - self.last_observation >= 60 * SECOND:
            self._append("COVERAGE", now, coverage=dict(self.coverage))
            self.last_observation = now
        for original in list(self.accounts.values()):
            a = deepcopy(original)
            reason = None
            if a["position"]:
                reason = "已有持仓，等待官方结算"
            elif a["pending"]:
                reason = "模拟订单等待执行"
            elif now < a["availableUs"]:
                reason = "等待上一笔市场结束至少60秒"
            elif not market:
                reason = "等待Polymarket市场"
            if reason:
                self.reasons[a["id"]] = reason
                continue
            side, reason, features = self._signal(a, market, feed, now)
            if side:
                flag = f"{market['marketId']}|{side}"
                book = books.get(side)
                budget = D(2) ** a["step"] if a["sizing"] == "martin11" else D(1)
                cap = price_cap(a["netCap"])
                if flag in a["attempted"]:
                    reason = "本场该方向已尝试，不补单"
                elif D(a["cash"]) < budget:
                    reason = "虚拟余额不足下一笔预算"
                elif not fresh(book, now):
                    reason = "决策时盘口来源时间缺失或超过2秒"
                elif not book["asks"] or not D(".30") <= D(str(book["asks"][0][0])) <= D(".50"):
                    reason = "决策卖一不在0.30–0.50"
                elif not buy_budget(book["asks"], budget, cap):
                    reason = "决策时限价内不足完整预算"
                else:
                    a["attempted"].append(flag)
                    a["pending"] = {"market": market, "side": side, "budget": str(budget),
                                    "cap": str(cap), "decisionUs": now, "dueUs": now + 300_000,
                                    "round": a["step"] + 1, "features": features, "decisionBook": book}
                    self._append("ATTEMPT", now, a, marketId=market["marketId"], side=side,
                                 budget=str(budget), round=a["step"] + 1)
                    reason = "条件满足，已提交模拟订单"
            self.reasons[a["id"]] = reason

    def state(self):
        with self.lock:
            accounts = []
            for original in self.accounts.values():
                a = deepcopy(original)
                for key in ("attempted",):
                    a.pop(key)
                n = a["wins"] + a["losses"]
                a["winRate"] = a["wins"] / n if n else None
                a["roi"] = float(D(a["pnl"]) / D(a["spent"])) if D(a["spent"]) else None
                a["nextBudget"] = str(D(2) ** a["step"] if a["sizing"] == "martin11" else D(1))
                a["reason"] = self.reasons.get(a["id"], "等待行情预热")
                accounts.append(a)
            return {"enabled": True, "model": MODEL, "fingerprint": FINGERPRINT,
                    "firstRecordedUs": self.first_us, "failure": self.failure,
                    "feed": {k: v for k, v in self.last_input.items() if k != "opens"},
                    "coverage": dict(self.coverage), "accounts": accounts,
                    "recentEvents": deepcopy(self.recent[-40:])}
