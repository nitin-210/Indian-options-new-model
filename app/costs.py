"""Charges on a round trip (buy, then sell) of index options.

Defaults (editable in Settings; check them against one of your contract notes):
- Brokerage: Rs 20 per executed order (resident Angel One F&O rate; NRI may differ)
- STT: 0.15% of the sell-side premium (from 1 April 2026)
- Exchange transaction charges: NSE 0.03503%, BSE SENSEX 0.0325% of premium, both sides
  (BSE uses turnover slabs on the nearest SENSEX expiry, so treat that one as approximate)
- SEBI fee: Rs 10 per crore of turnover, both sides
- Stamp duty: 0.003% of the buy-side premium
- GST: 18% of (brokerage + exchange charges + SEBI fee)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .config import Settings


@dataclass
class Costs:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def to_json(self) -> dict:
        d = {k: round(v, 2) for k, v in asdict(self).items()}
        d["total"] = round(self.total, 2)
        return d


def round_trip(buy_premium: float, sell_premium: float, qty: int, exch: str, s: Settings,
               orders: int = 2) -> Costs:
    buy_turn = max(buy_premium, 0.0) * qty
    sell_turn = max(sell_premium, 0.0) * qty
    turnover = buy_turn + sell_turn
    brokerage = s.brokerage_per_order * orders if qty > 0 else 0.0
    stt = s.stt_sell_pct / 100.0 * sell_turn
    exchange = s.txn_pct(exch) / 100.0 * turnover
    sebi = s.sebi_per_crore / 1e7 * turnover
    stamp = s.stamp_buy_pct / 100.0 * buy_turn
    gst = s.gst_pct / 100.0 * (brokerage + exchange + sebi)
    return Costs(brokerage, stt, exchange, sebi, stamp, gst)


def slip(tick: float, s: Settings) -> float:
    return max(0, s.slippage_ticks) * tick
