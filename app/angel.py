"""Small Angel One SmartAPI REST client (market data only).

Why not the official SDK? smartapi-python 1.5.5 writes request headers and
bodies (your API key and PIN) to logs/<date>/app.log whenever a request fails,
and its WebSocket client turns TLS certificate checks off. This client uses
the same endpoints, keeps certificate checks on, and never logs a secret.

There is deliberately no order placement, modification or cancellation code
in this file or anywhere else in the app.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pyotp

from .config import Secrets, register_secret
from .instruments import MASTER_URL
from .timeutil import IST

log = logging.getLogger(__name__)

BASE = "https://apiconnect.angelone.in"
LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
CANDLES = "/rest/secure/angelbroking/historical/v1/getCandleData"
OI_DATA = "/rest/secure/angelbroking/historical/v1/getOIData"
QUOTE = "/rest/secure/angelbroking/market/v1/quote"
GREEKS = "/rest/secure/angelbroking/marketData/v1/optionGreek"


class AngelError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class Session:
    jwt: str
    refresh: str
    feed_token: str
    api_key: str
    created: float

    def __repr__(self) -> str:
        return "Session(***)"


class _RateLimiter:
    def __init__(self, per_second: float):
        self.min_gap = 1.0 / per_second
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_gap:
                await asyncio.sleep(self.min_gap - gap)
            self._last = time.monotonic()


def _mac() -> str:
    n = uuid.getnode()
    return ":".join(f"{(n >> s) & 0xff:02x}" for s in range(40, -1, -8))


class AngelClient:
    def __init__(self, secrets: Secrets, timeout: float = 20.0):
        self.secrets = secrets
        self.session: Session | None = None
        self.hist_session: Session | None = None
        self._http = httpx.AsyncClient(base_url=BASE, timeout=timeout, verify=True)
        self._candle_rl = _RateLimiter(2.5)  # Angel allows 3/s for candles
        self._misc_rl = _RateLimiter(1.0)

    async def close(self):
        await self._http.aclose()

    def _headers(self, api_key: str, jwt: str | None = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": self.secrets.public_ip or "127.0.0.1",
            "X-MACAddress": _mac(),
            "X-PrivateKey": api_key,
        }
        if jwt:
            h["Authorization"] = f"Bearer {jwt}"
        return h

    async def _login_with(self, api_key: str) -> Session:
        totp = pyotp.TOTP(self.secrets.totp_secret.reveal()).now()
        body = {"clientcode": self.secrets.client_code.reveal(),
                "password": self.secrets.pin.reveal(), "totp": totp}
        try:
            r = await self._http.post(LOGIN, headers=self._headers(api_key), content=json.dumps(body))
        except httpx.HTTPError as e:
            raise AngelError(f"Could not reach Angel One ({type(e).__name__}). Check your internet connection.")
        data = _json(r)
        if not data.get("status") or not data.get("data"):
            msg = data.get("message") or f"HTTP {r.status_code}"
            raise AngelError(f"Login refused by Angel One: {msg}", str(data.get("errorcode", "")))
        d = data["data"]
        for k in ("jwtToken", "refreshToken", "feedToken"):
            register_secret(d.get(k))
        return Session(d["jwtToken"], d.get("refreshToken", ""), d.get("feedToken", ""), api_key, time.time())

    async def login(self) -> Session:
        self.session = await self._login_with(self.secrets.api_key.reveal())
        if self.secrets.historical_api_key:
            try:
                await asyncio.sleep(1.1)  # login is limited to 1 request a second
                self.hist_session = await self._login_with(self.secrets.historical_api_key.reveal())
            except AngelError as e:
                log.warning("Historical-data key login failed (%s); using the main key", e)
                self.hist_session = None
        return self.session

    async def _post(self, path: str, body: dict, historical: bool = False) -> dict:
        sess = (self.hist_session if historical and self.hist_session else self.session)
        if not sess:
            raise AngelError("Not logged in")
        for attempt in range(3):
            try:
                r = await self._http.post(path, headers=self._headers(sess.api_key, sess.jwt),
                                          content=json.dumps(body))
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise AngelError(f"Network error talking to Angel One ({type(e).__name__})")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 403 and attempt < 2:
                # Angel's gateway answers HTTP 403 (not JSON) when requests come too fast: wait, then try again
                wait = (2, 6)[attempt]     # Angel also refuses requests at random (well below its limits)
                log.warning("Angel One refused a request (HTTP 403, usually too many requests); retrying in %d s", wait)
                await asyncio.sleep(wait)
                continue
            data = _json(r)
            if data.get("errorCode") in ("AG8001", "AG8002") or data.get("errorcode") in ("AG8001", "AG8002"):
                raise AngelError("Session expired or invalid token", "AUTH")
            return data
        raise AngelError(f"Angel One kept returning errors for {path}")

    # ---------------------------------------------------------- market data

    async def candles(self, exchange: str, token: str, interval: str,
                      start: datetime, end: datetime) -> list[tuple]:
        """[(datetime IST, o, h, l, c, v)] for one request window."""
        await self._candle_rl.wait()
        body = {"exchange": exchange, "symboltoken": token, "interval": interval,
                "fromdate": start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                "todate": end.astimezone(IST).strftime("%Y-%m-%d %H:%M")}
        data = await self._post(CANDLES, body, historical=True)
        if not data.get("status"):
            msg = str(data.get("message", ""))
            if "no data" in msg.lower() or data.get("errorcode") in ("AB1004", "AB9019"):
                return []
            raise AngelError(f"Candle request failed: {msg}", str(data.get("errorcode", "")))
        out = []
        for row in data.get("data") or []:
            ts = datetime.fromisoformat(row[0]).astimezone(IST)
            out.append((ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5] or 0)))
        return out

    async def candles_paged(self, exchange: str, token: str, interval: str, days_back: int,
                            chunk_days: int, end: datetime, progress=None, pace: float = 1.0) -> tuple[list, str]:
        """Walk backwards in chunks. Returns (rows, status).

        status: "done" (covered days_back), "exhausted" (Angel has nothing older), or
        "stopped: <reason>" (a request kept failing; the rows fetched so far are still returned).
        Requests are spaced by `pace` seconds to stay well inside Angel's rate limits.
        """
        rows: dict[datetime, tuple] = {}
        cur_end = end
        empty_runs = 0
        fetched_days = 0
        status = "done"
        while fetched_days < days_back:
            start = cur_end - timedelta(days=chunk_days)
            chunk = None
            for attempt in range(3):
                try:
                    chunk = await self.candles(exchange, token, interval, start, cur_end)
                    break
                except AngelError as e:
                    if attempt == 2:
                        status = f"stopped: {e}"
                    else:
                        await asyncio.sleep(5.0 * (attempt + 1))  # back off, then try again
            if chunk is None:
                break
            if chunk:
                empty_runs = 0
                for r in chunk:
                    rows[r[0]] = r
            else:
                empty_runs += 1
                if empty_runs >= 2:
                    status = "exhausted"
                    break
            fetched_days += chunk_days
            cur_end = start - timedelta(minutes=1)
            if progress:
                progress(len(rows), cur_end)
            await asyncio.sleep(pace)
        return [rows[k] for k in sorted(rows)], status

    async def oi_data(self, exchange: str, token: str, interval: str, start: datetime, end: datetime):
        await self._misc_rl.wait()
        body = {"exchange": exchange, "symboltoken": token, "interval": interval,
                "fromdate": start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                "todate": end.astimezone(IST).strftime("%Y-%m-%d %H:%M")}
        data = await self._post(OI_DATA, body, historical=True)
        return data.get("data") or []

    async def quote(self, mode: str, exchange_tokens: dict[str, list[str]]) -> list[dict]:
        await self._misc_rl.wait()
        data = await self._post(QUOTE, {"mode": mode, "exchangeTokens": exchange_tokens})
        return ((data.get("data") or {}).get("fetched")) or []

    async def option_greeks(self, name: str, expiry_code: str) -> list[dict]:
        await self._misc_rl.wait()
        data = await self._post(GREEKS, {"name": name, "expirydate": expiry_code})
        return data.get("data") or []


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        snippet = " ".join((r.text or "").split())[:120]
        return {"status": False, "message": f"Unexpected reply from Angel One (HTTP {r.status_code})"
                                            + (f": {snippet}" if snippet else "")}


async def download_master(cache_dir: Path, today_iso: str) -> list[dict]:
    """Download Angel's instrument file once a day (it is large, ~40 MB)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"scripmaster-{today_iso}.json"
    if path.exists() and path.stat().st_size > 1_000_000:
        return json.loads(path.read_text(encoding="utf-8"))
    async with httpx.AsyncClient(timeout=120.0, verify=True) as c:
        r = await c.get(MASTER_URL)
        r.raise_for_status()
        path.write_bytes(r.content)
    for old in cache_dir.glob("scripmaster-*.json"):
        if old != path:
            old.unlink(missing_ok=True)
    return json.loads(path.read_text(encoding="utf-8"))
