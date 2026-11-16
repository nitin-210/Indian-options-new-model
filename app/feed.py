"""SmartAPI WebSocket V2 market feed: binary parsing, heartbeat, reconnect.

Packet layout (little-endian) follows Angel's WebSocket 2.0 spec and the
official SDK's parser. Prices arrive in paise and are divided by 100 here.
TLS certificate verification stays ON (the official SDK turns it off).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl
import struct
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
MODE_LTP, MODE_QUOTE, MODE_SNAP = 1, 2, 3


def _q(b: bytes, start: int) -> int:
    return struct.unpack_from("<q", b, start)[0]


def _d(b: bytes, start: int) -> float:
    return struct.unpack_from("<d", b, start)[0]


def _plausible_pct(b: bytes, start: int) -> float | None:
    """The OI-change-% field: read as a double; fall back to int64; reject junk."""
    for v in (_d(b, start), float(_q(b, start))):
        if math.isfinite(v) and abs(v) < 100000:
            return v
    return None


def parse_packet(b: bytes) -> dict | None:
    """Parse one binary tick. Returns None for anything too short to be a tick."""
    if len(b) < 51:
        return None
    mode = b[0]
    token = b[2:27].split(b"\x00", 1)[0].decode("ascii", "ignore")
    t = {
        "mode": mode,
        "ws_type": b[1],
        "token": token,
        "seq": _q(b, 27),
        "ts": _q(b, 35),  # exchange timestamp, epoch milliseconds
        "ltp": _q(b, 43) / 100.0,
    }
    if mode in (MODE_QUOTE, MODE_SNAP) and len(b) >= 123:
        t.update({
            "ltq": _q(b, 51),
            "atp": _q(b, 59) / 100.0,
            "vol": _q(b, 67),
            "tbq": _d(b, 75),
            "tsq": _d(b, 83),
            "open": _q(b, 91) / 100.0,
            "high": _q(b, 99) / 100.0,
            "low": _q(b, 107) / 100.0,
            "close": _q(b, 115) / 100.0,
        })
    if mode == MODE_SNAP and len(b) >= 379:
        t["oi"] = _q(b, 131)
        t["oi_chg_pct"] = _plausible_pct(b, 139)
        bids, asks = [], []
        for i in range(10):
            off = 147 + i * 20
            flag = struct.unpack_from("<H", b, off)[0]
            qty = _q(b, off + 2)
            price = _q(b, off + 10) / 100.0
            orders = struct.unpack_from("<H", b, off + 18)[0]
            (bids if flag == 1 else asks).append((price, qty, orders))
        bids = [x for x in bids if x[0] > 0]
        asks = [x for x in asks if x[0] > 0]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        if bids and asks and bids[0][0] > asks[0][0]:  # defensive: flags reversed
            bids, asks = sorted(asks, key=lambda x: -x[0]), sorted(bids, key=lambda x: x[0])
        t["bids"], t["asks"] = bids[:5], asks[:5]
        t["upper"] = _q(b, 347) / 100.0
        t["lower"] = _q(b, 355) / 100.0
    return t


def build_packet(mode: int, ws_type: int, token: str, ts_ms: int, ltp: float, **kw) -> bytes:
    """Inverse of parse_packet (used by tests and the replay recorder self-check)."""
    size = {1: 51, 2: 123, 3: 379}[mode]
    b = bytearray(size)
    b[0], b[1] = mode, ws_type
    tok = token.encode("ascii")[:25]
    b[2:2 + len(tok)] = tok
    struct.pack_into("<q", b, 27, kw.get("seq", 1))
    struct.pack_into("<q", b, 35, ts_ms)
    struct.pack_into("<q", b, 43, round(ltp * 100))
    if mode >= 2:
        struct.pack_into("<q", b, 51, kw.get("ltq", 0))
        struct.pack_into("<q", b, 59, round(kw.get("atp", ltp) * 100))
        struct.pack_into("<q", b, 67, kw.get("vol", 0))
        struct.pack_into("<d", b, 75, kw.get("tbq", 0.0))
        struct.pack_into("<d", b, 83, kw.get("tsq", 0.0))
        for off, key in ((91, "open"), (99, "high"), (107, "low"), (115, "close")):
            struct.pack_into("<q", b, off, round(kw.get(key, ltp) * 100))
    if mode == 3:
        struct.pack_into("<q", b, 123, ts_ms // 1000)
        struct.pack_into("<q", b, 131, kw.get("oi", 0))
        struct.pack_into("<d", b, 139, kw.get("oi_chg_pct", 0.0))
        levels = [(1, p, q) for p, q in kw.get("bids", [])] + [(0, p, q) for p, q in kw.get("asks", [])]
        for i, (flag, p, q) in enumerate(levels[:10]):
            off = 147 + i * 20
            struct.pack_into("<H", b, off, flag)
            struct.pack_into("<q", b, off + 2, q)
            struct.pack_into("<q", b, off + 10, round(p * 100))
            struct.pack_into("<H", b, off + 18, 1)
    return bytes(b)


class AngelFeed:
    """Keeps one WebSocket open, resubscribes after reconnects, reports health."""

    def __init__(self, get_session: Callable[[bool], Awaitable], api_key: str, client_code: str,
                 on_tick: Callable[[dict, float], None]):
        self._get_session = get_session  # async (force_relogin: bool) -> Session
        self._api_key = api_key
        self._client_code = client_code
        self._on_tick = on_tick
        self.subs: dict[tuple[int, str], int] = {}  # (ws_type, token) -> mode
        self._ws = None
        self.connected = False
        self.last_message = 0.0
        self.last_error = ""
        self.reconnects = 0
        self._stop = False
        self._bearer = True

    def want(self, tokens: dict[int, set[str]], mode: int = MODE_SNAP) -> None:
        """Declare the full set of tokens we want; the diff is (un)subscribed."""
        new = {(wt, tok): mode for wt, toks in tokens.items() for tok in toks}
        added = {k: m for k, m in new.items() if k not in self.subs}
        removed = [k for k in self.subs if k not in new]
        self.subs = new
        if self._ws is not None and self.connected:
            loop = asyncio.get_running_loop()
            if removed:
                loop.create_task(self._send_sub(removed, action=0))
            if added:
                loop.create_task(self._send_sub(list(added), action=1))

    async def _send_sub(self, keys, action: int):
        by_mode: dict[int, dict[int, list[str]]] = {}
        for k in keys:
            mode = self.subs.get(k, MODE_SNAP)
            by_mode.setdefault(mode, {}).setdefault(k[0], []).append(k[1])
        for mode, groups in by_mode.items():
            msg = {"correlationID": "iopts", "action": action,
                   "params": {"mode": mode,
                              "tokenList": [{"exchangeType": wt, "tokens": toks} for wt, toks in groups.items()]}}
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:  # pragma: no cover - network
                self.last_error = f"subscribe failed: {type(e).__name__}"

    async def run(self):
        import websockets  # imported here so tests do not need it

        backoff = 1.0
        force_relogin = False
        ctx = ssl.create_default_context()
        while not self._stop:
            try:
                sess = await self._get_session(force_relogin)
                force_relogin = False
                auth = f"Bearer {sess.jwt}" if self._bearer else sess.jwt
                headers = {"Authorization": auth, "x-api-key": self._api_key,
                           "x-client-code": self._client_code, "x-feed-token": sess.feed_token}
                # Angel's keepalive is the text "ping" below. The library's own ping frames are switched off: at the
                # open their replies queue behind the flood of price packets and trip a false "ping timeout".
                async with websockets.connect(WS_URL, additional_headers=headers, ssl=ctx,
                                              ping_interval=None, max_size=2**22,
                                              open_timeout=15) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    log.info("Market feed connected")
                    if self.subs:
                        await self._send_sub(list(self.subs), action=1)
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for msg in ws:
                            now = time.time()
                            self.last_message = now
                            if isinstance(msg, (bytes, bytearray)):
                                t = parse_packet(bytes(msg))
                                if t:
                                    self._on_tick(t, now)
                            elif msg != "pong":
                                log.info("Feed message: %s", str(msg)[:200])
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:
                text = f"{type(e).__name__}: {e}"
                self.last_error = text[:160]
                if "401" in text or "403" in text:
                    # Try the other Authorization style once, then a fresh login.
                    self._bearer = not self._bearer
                    force_relogin = not self._bearer
                log.warning("Market feed dropped: %s", self.last_error)
            self.connected = False
            self._ws = None
            if self._stop:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(10)
            try:
                await ws.send("ping")
            except Exception:
                return
            # nothing at all (not even "pong") for 25 s: the connection is dead, so reconnect
            if self.last_message and time.time() - self.last_message > 25:
                log.warning("Market feed silent for 25 s; reconnecting")
                try:
                    await ws.close()
                except Exception:
                    pass
                return

    def stop(self):
        self._stop = True
