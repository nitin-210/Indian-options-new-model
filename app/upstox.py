"""Upstox as the market-data source (an alternative to Angel One).

Uses Upstox's read-only **Analytics Token** (Developer Apps -> Analytics tab, valid for a year,
no daily login). Put it in config/secrets.toml:

    [upstox]
    access_token = "..."

How it fits in: the rest of the app speaks Angel One's "language" (Angel-style instrument rows,
numeric tokens, WebSocket exchange types 1-4, tick dictionaries). This module translates Upstox's
instrument files, REST replies and protobuf feed into exactly that, so the engine, strategy, risk
checks, journal and dashboard are unchanged.

    (ws_type, token)          Upstox instrument_key
    (1, "99926000")       <-> NSE_INDEX|Nifty 50       (indices keep Angel's tokens)
    (2, "<exchange token>")<-> NSE_FO|<exchange token> (NFO options/futures)
    (1, "<exchange token>")<-> NSE_EQ|<ISIN>           (cash stocks)

There is deliberately no order placement code here or anywhere else in the app (the Analytics
Token cannot place orders anyway).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote as urlquote

from .angel import AngelError, _RateLimiter
from .config import DATA_DIR, SECRETS_PATH, register_secret
from .instruments import INDEX_META, VIX_META
from .timeutil import IST, now_ist

log = logging.getLogger(__name__)

API = "https://api.upstox.com"
QUOTES = "/v2/market-quote/quotes"
FEED_AUTH = "/v3/feed/market-data-feed/authorize"
MASTER_URLS = ("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
               "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz")

# Angel's synthetic index tokens -> Upstox index keys
INDEX_KEYS = {
    (INDEX_META["NIFTY"]["ws_type"], INDEX_META["NIFTY"]["token"]): "NSE_INDEX|Nifty 50",
    (INDEX_META["SENSEX"]["ws_type"], INDEX_META["SENSEX"]["token"]): "BSE_INDEX|SENSEX",
    (INDEX_META["BANKNIFTY"]["ws_type"], INDEX_META["BANKNIFTY"]["token"]): "NSE_INDEX|Nifty Bank",
    (VIX_META["ws_type"], VIX_META["token"]): "NSE_INDEX|India VIX",
}
EXCH_WS = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}
SEG_WS = {"NSE_EQ": 1, "NSE_INDEX": 1, "NSE_FO": 2, "BSE_EQ": 3, "BSE_INDEX": 3, "BSE_FO": 4}
INTERVALS = {"ONE_MINUTE": ("minutes", 1), "THREE_MINUTE": ("minutes", 3), "FIVE_MINUTE": ("minutes", 5),
             "TEN_MINUTE": ("minutes", 10), "FIFTEEN_MINUTE": ("minutes", 15), "THIRTY_MINUTE": ("minutes", 30),
             "ONE_HOUR": ("hours", 1), "ONE_DAY": ("days", 1)}
PLACEHOLDERS = {"", "PASTE_HERE", "PASTE_YOUR_ANALYTICS_TOKEN_HERE"}


class UpstoxError(AngelError):
    """Subclass of AngelError so every existing `except AngelError` handles Upstox failures too."""


# ------------------------------------------------------------------ secrets

@dataclass
class UpstoxSecrets:
    _token: str

    def reveal(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return "UpstoxSecrets(***)"

    def problems(self) -> list[str]:
        t = self._token
        if not t:
            return ["[upstox] access_token is empty"]
        if len(t) < 40 or " " in t:
            return ["[upstox] access_token does not look like an Upstox token (a long string with no spaces)"]
        return []


def load_upstox_secrets(path: Path | None = None) -> UpstoxSecrets | None:
    """The [upstox] access_token from config/secrets.toml, or None if there is none."""
    import tomllib

    path = path or SECRETS_PATH
    if not path.exists():
        return None
    with path.open("rb") as f:
        raw = tomllib.load(f)
    sec = raw.get("upstox")
    if not isinstance(sec, dict):
        return None
    tok = str(sec.get("access_token", "") or sec.get("analytics_token", "") or "").strip()
    if tok in PLACEHOLDERS:
        return None
    register_secret(tok)
    return UpstoxSecrets(tok)


# ---------------------------------------------------------- instrument file

def _expiry(ms) -> date | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, IST).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def convert_instruments(rows: list[dict], index_names=tuple(INDEX_META)) -> tuple[list[dict], dict]:
    """Upstox instrument rows -> (Angel-style rows, {(ws_type, token): instrument_key}).

    The Angel-style rows feed the app's existing parse_master / parse_fno_stocks unchanged.
    Upstox gives strikes in rupees (Angel: paise) and tick sizes in paise (same as Angel).
    """
    out: list[dict] = []
    keys: dict[tuple[int, str], str] = dict(INDEX_KEYS)
    for r in rows:
        seg = r.get("segment", "")
        it = r.get("instrument_type", "")
        tok = str(r.get("exchange_token") or "")
        ukey = r.get("instrument_key", "")
        if not tok or not ukey or seg not in SEG_WS:
            continue
        lot = r.get("lot_size") or 1
        tick = r.get("tick_size") or 5
        if seg == "NSE_EQ" and it == "EQ":
            sym = r.get("trading_symbol", "")
            out.append({"exch_seg": "NSE", "instrumenttype": "", "symbol": f"{sym}-EQ", "name": sym,
                        "token": tok, "lotsize": str(lot), "tick_size": str(tick)})
        elif seg in ("NSE_FO", "BSE_FO") and it in ("CE", "PE", "FUT"):
            usym = r.get("underlying_symbol") or r.get("asset_symbol") or r.get("name") or ""
            utype = r.get("underlying_type") or r.get("asset_type") or ""
            exp = _expiry(r.get("expiry"))
            if exp is None:
                continue
            strike = float(r.get("strike_price") or 0)
            exch = "NFO" if seg == "NSE_FO" else "BFO"
            if utype == "INDEX" and it != "FUT":
                if usym not in index_names:
                    continue
                itype = "OPTIDX"
            elif utype == "EQUITY" and seg == "NSE_FO":
                itype = "FUTSTK" if it == "FUT" else "OPTSTK"
            else:
                continue
            if it == "FUT":
                sym = f"{usym}{exp:%d%b%y}FUT".upper()
            else:
                sym = f"{usym}{exp:%d%b%y}{strike:g}{it}".upper()
            out.append({"exch_seg": exch, "instrumenttype": itype, "symbol": sym, "name": usym, "token": tok,
                        "expiry": exp.strftime("%d%b%Y").upper(), "strike": f"{strike * 100:.6f}",
                        "lotsize": str(lot), "tick_size": str(tick)})
        else:
            continue
        keys[(SEG_WS[seg], tok)] = ukey
    return out, keys


# ------------------------------------------------------------------- client

@dataclass
class Session:
    jwt: str
    created: float
    feed_token: str = ""

    def __repr__(self) -> str:
        return "Session(***)"


class UpstoxClient:
    """Same methods the app calls on AngelClient: login, candles, candles_paged, quote, oi_data, close."""

    broker = "Upstox"

    def __init__(self, secrets: UpstoxSecrets, timeout: float = 20.0):
        import httpx

        self.secrets = secrets
        self.session: Session | None = None
        self.hist_session = None
        self.keys: dict[tuple[int, str], str] = dict(INDEX_KEYS)
        self.back: dict[str, tuple[int, str]] = {v: k for k, v in self.keys.items()}
        self._http = httpx.AsyncClient(base_url=API, timeout=timeout, verify=True)
        self._rl = _RateLimiter(4.0)  # Upstox allows far more; stay polite

    async def close(self):
        await self._http.aclose()

    def _headers(self) -> dict:
        return {"Accept": "application/json", "Authorization": f"Bearer {self.secrets.reveal()}"}

    async def _get(self, path: str, params: dict | None = None, allow_400: bool = False) -> dict:
        import httpx

        for attempt in range(4):
            await self._rl.wait()
            try:
                r = await self._http.get(path, params=params, headers=self._headers())
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise UpstoxError(f"Network error talking to Upstox ({type(e).__name__})")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise UpstoxError("Upstox refused the token (HTTP 401). Generate a new Analytics Token on "
                                  "Upstox -> Developer Apps -> Analytics and paste it into config/secrets.toml.",
                                  "AUTH")
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            data = _json(r)
            if r.status_code >= 400 or data.get("status") == "error":
                msg = _err(data) or f"HTTP {r.status_code}"
                if allow_400 and r.status_code == 400:
                    log.debug("Upstox %s: %s", path, msg)
                    return {"status": "success", "data": {}}
                raise UpstoxError(f"Upstox: {msg}")
            return data
        raise UpstoxError(f"Upstox kept returning errors for {path}")

    async def login(self) -> Session:
        """No login with an Analytics Token: just check it works with one quote."""
        await self._get(QUOTES, {"instrument_key": INDEX_KEYS[(1, INDEX_META["NIFTY"]["token"])]})
        self.session = Session(self.secrets.reveal(), time.time())
        return self.session

    def key_for(self, exchange: str, token: str) -> str:
        wt = EXCH_WS.get(exchange)
        k = self.keys.get((wt, str(token)))
        if not k:
            raise UpstoxError(f"Unknown instrument {exchange}:{token} (instrument list not loaded yet?)")
        return k

    # ---------------------------------------------------------- instruments

    async def download_master(self, cache_dir: Path, today_iso: str) -> list[dict]:
        """Upstox's NSE + BSE instrument files, converted to Angel-style rows (cached for the day).
        Also fills self.keys so tokens can be turned back into Upstox instrument keys."""
        import httpx

        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"upstox-master-{today_iso}.json"
        if path.exists() and path.stat().st_size > 100_000:
            saved = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw: list[dict] = []
            async with httpx.AsyncClient(timeout=180.0, verify=True, follow_redirects=True) as c:
                for url in MASTER_URLS:
                    r = await c.get(url)
                    r.raise_for_status()
                    raw.extend(json.loads(gzip.decompress(r.content)))
            rows, keys = convert_instruments(raw)
            del raw
            saved = {"rows": rows, "keys": [[wt, tok, k] for (wt, tok), k in keys.items()]}
            path.write_text(json.dumps(saved), encoding="utf-8")
            for old in cache_dir.glob("upstox-master-*.json"):
                if old != path:
                    old.unlink(missing_ok=True)
        self.keys = {(int(wt), str(tok)): k for wt, tok, k in saved["keys"]}
        self.keys.update(INDEX_KEYS)
        self.back = {v: k for k, v in self.keys.items()}
        return saved["rows"]

    # ---------------------------------------------------------- market data

    async def _candles_raw(self, exchange: str, token: str, interval: str,
                           start: datetime, end: datetime) -> list[list]:
        """Rows [ts, o, h, l, c, v, oi] oldest first, inside [start, end]."""
        unit, n = INTERVALS.get(interval, ("minutes", 1))
        key = urlquote(self.key_for(exchange, token), safe="")
        start, end = start.astimezone(IST), end.astimezone(IST)
        today = now_ist().date()
        span = 28 if unit == "minutes" and n <= 15 else (85 if unit in ("minutes", "hours") else 3600)
        rows: dict[str, list] = {}
        d0 = start.date()
        last_hist = min(end.date(), today - timedelta(days=1))
        while d0 <= last_hist:
            d1 = min(d0 + timedelta(days=span - 1), last_hist)
            data = await self._get(f"/v3/historical-candle/{key}/{unit}/{n}/{d1.isoformat()}/{d0.isoformat()}",
                                   allow_400=True)
            for c in (data.get("data") or {}).get("candles") or []:
                rows[c[0]] = c
            d0 = d1 + timedelta(days=1)
        if end.date() >= today and unit in ("minutes", "hours", "days") and start.date() <= today:
            data = await self._get(f"/v3/historical-candle/intraday/{key}/{unit}/{n}", allow_400=True)
            for c in (data.get("data") or {}).get("candles") or []:
                rows[c[0]] = c
        out = []
        for c in rows.values():
            ts = datetime.fromisoformat(c[0]).astimezone(IST)
            if unit == "days":
                if not (start.date() <= ts.date() <= end.date()):
                    continue
            elif not (start <= ts <= end):
                continue
            out.append([ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                        float(c[5] or 0) if len(c) > 5 else 0.0, float(c[6] or 0) if len(c) > 6 else 0.0])
        out.sort(key=lambda r: r[0])
        return out

    async def candles(self, exchange: str, token: str, interval: str,
                      start: datetime, end: datetime) -> list[tuple]:
        """[(datetime IST, o, h, l, c, v)] like AngelClient.candles."""
        return [tuple(r[:6]) for r in await self._candles_raw(exchange, token, interval, start, end)]

    async def candles_paged(self, exchange: str, token: str, interval: str, days_back: int,
                            chunk_days: int, end: datetime, progress=None, pace: float = 0.3) -> tuple[list, str]:
        """Walk backwards in chunks; same contract as AngelClient.candles_paged.
        Upstox has 1- to 15-minute candles from January 2022, so older chunks come back empty."""
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
                except UpstoxError as e:
                    if e.code == "AUTH" or attempt == 2:
                        status = f"stopped: {e}"
                        break
                    await asyncio.sleep(5.0 * (attempt + 1))
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
        rows = await self._candles_raw(exchange, token, interval, start, end)
        return [{"time": r[0].isoformat(), "oi": int(r[6])} for r in rows]

    async def quote(self, mode: str, exchange_tokens: dict[str, list[str]]) -> list[dict]:
        """Like AngelClient.quote: [{symbolToken, tradingSymbol, ltp, close (= previous close)}]."""
        back: dict[str, tuple[str, str]] = {}
        for exch, toks in exchange_tokens.items():
            for tok in toks:
                try:
                    back[self.key_for(exch, tok)] = (str(tok), exch)
                except UpstoxError:
                    continue
        if not back:
            return []
        out = []
        ks = list(back)
        for i in range(0, len(ks), 400):
            data = await self._get(QUOTES, {"instrument_key": ",".join(ks[i:i + 400])})
            for name, q in (data.get("data") or {}).items():
                ukey = q.get("instrument_token") or name.replace(":", "|", 1)
                tok = back.get(ukey)
                if tok is None:
                    continue
                ltp = float(q.get("last_price") or 0)
                prev = ltp - float(q.get("net_change") or 0)
                out.append({"symbolToken": tok[0], "exchange": tok[1], "tradingSymbol": q.get("symbol") or name,
                            "ltp": ltp, "close": prev, "ohlc": q.get("ohlc") or {}})
        return out

    async def option_greeks(self, name: str, expiry_code: str) -> list[dict]:
        return []  # the app works out IV and Greeks itself

    async def feed_url(self) -> str:
        data = await self._get(FEED_AUTH)
        d = data.get("data") or {}
        url = d.get("authorized_redirect_uri") or d.get("authorizedRedirectUri")
        if not url:
            raise UpstoxError("Upstox did not return a market-feed address")
        return url


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        snippet = " ".join((r.text or "").split())[:120]
        return {"status": "error", "errors": [{"message": f"Unexpected reply (HTTP {r.status_code})"
                                                          + (f": {snippet}" if snippet else "")}]}


def _err(data: dict) -> str:
    errs = data.get("errors") or []
    if errs and isinstance(errs, list):
        e = errs[0]
        return f"{e.get('message') or e.get('errorCode')}" + (f" ({e.get('errorCode')})" if e.get("errorCode") else "")
    return str(data.get("message") or "")


# --------------------------------------------------------------------- feed

def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def ticks_from_feed(msg: dict, back: dict[str, tuple[int, str]]) -> list[dict]:
    """A decoded Upstox FeedResponse (MessageToDict form) -> Angel-style tick dicts.

    `close` is the previous day's close (Upstox `cp`), as in Angel's feed; `ts` is epoch ms.
    """
    out = []
    now_ms = int(_f(msg.get("currentTs"), 0))
    for ukey, f in (msg.get("feeds") or {}).items():
        wt_tok = back.get(ukey)
        if wt_tok is None:
            continue
        full = f.get("fullFeed") or {}
        m = full.get("marketFF") or full.get("indexFF") or {}
        flg = f.get("firstLevelWithGreeks") or {}
        ltpc = m.get("ltpc") or f.get("ltpc") or flg.get("ltpc") or {}
        if not ltpc and not m:
            continue
        t = {"mode": 3 if full.get("marketFF") else (2 if m else 1), "ws_type": wt_tok[0], "token": wt_tok[1],
             "seq": 0, "ts": int(_f(ltpc.get("ltt"), now_ms) or now_ms), "ltp": _f(ltpc.get("ltp"))}
        if _f(ltpc.get("cp")) > 0:
            t["close"] = _f(ltpc.get("cp"))
        for o in (m.get("marketOHLC") or {}).get("ohlc") or []:
            if o.get("interval") == "1d":
                t["open"], t["high"], t["low"] = _f(o.get("open")), _f(o.get("high")), _f(o.get("low"))
                break
        src = m or flg
        if "vtt" in src:
            t["vol"] = _f(src.get("vtt"))
        if "atp" in src:
            t["atp"] = _f(src.get("atp"))
        if "oi" in src:
            t["oi"] = int(_f(src.get("oi")))
            t["oi_chg_pct"] = None
        levels = (m.get("marketLevel") or {}).get("bidAskQuote") or ([flg["firstDepth"]] if flg.get("firstDepth") else [])
        if levels:
            bids = [(_f(q.get("bidP")), int(_f(q.get("bidQ"))), 0) for q in levels if _f(q.get("bidP")) > 0]
            asks = [(_f(q.get("askP")), int(_f(q.get("askQ"))), 0) for q in levels if _f(q.get("askP")) > 0]
            t["bids"] = sorted(bids, key=lambda x: -x[0])[:5]
            t["asks"] = sorted(asks, key=lambda x: x[0])[:5]
        out.append(t)
    return out


def _load_proto():
    try:
        from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as pb  # ships with upstox-python-sdk
        from google.protobuf.json_format import MessageToDict
    except ImportError as e:  # pragma: no cover - depends on the install
        raise UpstoxError("The Upstox feed decoder is missing. Run:  .venv/bin/python -m pip install -r "
                          f"requirements.txt   ({e})")
    return pb, MessageToDict


class UpstoxFeed:
    """Same interface as AngelFeed: want(), run(), stop(), connected, last_error, reconnects."""

    def __init__(self, client: UpstoxClient, on_tick: Callable[[dict, float], None]):
        self.client = client
        self._on_tick = on_tick
        self.subs: dict[tuple[int, str], int] = {}
        self._ws = None
        self.connected = False
        self.last_message = 0.0
        self.last_error = ""
        self.reconnects = 0
        self._stop = False

    def _key(self, k: tuple[int, str]) -> str | None:
        return self.client.keys.get((int(k[0]), str(k[1])))

    def want(self, tokens: dict[int, set[str]], mode: int = 3) -> None:
        new = {(wt, tok): mode for wt, toks in tokens.items() for tok in toks}
        added = [k for k in new if k not in self.subs]
        removed = [k for k in self.subs if k not in new]
        self.subs = new
        if self._ws is not None and self.connected:
            loop = asyncio.get_running_loop()
            if removed:
                loop.create_task(self._send("unsub", removed))
            if added:
                loop.create_task(self._send("sub", added))

    async def _send(self, method: str, keys) -> None:
        ukeys = [u for u in (self._key(k) for k in keys) if u]
        for i in range(0, len(ukeys), 400):
            data = {"instrumentKeys": ukeys[i:i + 400]}
            if method == "sub":
                data["mode"] = "full"
            msg = {"guid": uuid.uuid4().hex[:20], "method": method, "data": data}
            try:
                await self._ws.send(json.dumps(msg).encode("utf-8"))  # Upstox wants a binary frame
            except Exception as e:  # pragma: no cover - network
                self.last_error = f"subscribe failed: {type(e).__name__}"

    async def run(self):
        import ssl
        import websockets

        pb, to_dict = _load_proto()
        backoff = 1.0
        ctx = ssl.create_default_context()
        while not self._stop:
            try:
                url = await self.client.feed_url()
                async with websockets.connect(url, ssl=ctx, max_size=2**23, open_timeout=15,
                                              ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    log.info("Upstox market feed connected")
                    if self.subs:
                        await self._send("sub", list(self.subs))
                    async for raw in ws:
                        now = time.time()
                        self.last_message = now
                        if not isinstance(raw, (bytes, bytearray)):
                            continue
                        resp = pb.FeedResponse()
                        try:
                            resp.ParseFromString(bytes(raw))
                        except Exception:
                            continue
                        d = to_dict(resp)
                        if not d.get("feeds"):
                            continue
                        for t in ticks_from_feed(d, self.client.back):
                            self._on_tick(t, now)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:160]
                log.warning("Upstox market feed dropped: %s", self.last_error)
                if isinstance(e, UpstoxError) and e.code == "AUTH":
                    backoff = 60.0
            self.connected = False
            self._ws = None
            if self._stop:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def stop(self):
        self._stop = True


# ------------------------------------------------------------- live source

def make_live_source(engine, settings, secrets: UpstoxSecrets):
    from .sources import LiveSource

    class UpstoxLiveSource(LiveSource):
        broker = "Upstox"

        def _make_client(self, secrets):
            return UpstoxClient(secrets)

        def status(self) -> dict:
            d = super().status()
            d["label"] = "Live: Upstox"
            return d

        async def _load_instruments(self) -> None:
            from .instruments import parse_master
            from .movers import parse_fno_stocks

            today = now_ist().date()
            self.engine.status_note = "Downloading Upstox's instrument files…"
            rows = await self.client.download_master(DATA_DIR / "cache", today.isoformat())
            self.contracts = parse_master(rows)
            if not self.contracts:
                raise UpstoxError("The Upstox instrument files had no NIFTY or SENSEX options")
            self.engine.set_contracts(self.contracts)
            self.stocks, stock_opts = parse_fno_stocks(rows, today)
            self.engine.set_stocks(self.stocks, stock_opts, "")
            self.engine.status_note = ""

        def _make_feed(self):
            return UpstoxFeed(self.client, self._on_tick)

    return UpstoxLiveSource(engine, settings, secrets)


# ------------------------------------------------------------- check-login

async def check_login_upstox(secrets: UpstoxSecrets) -> int:
    from .instruments import OptionUniverse, parse_master

    def ok(msg):
        print(f"  PASS  {msg}")

    def bad(msg):
        print(f"  FAIL  {msg}")

    print("\nChecking your Upstox setup (nothing secret is printed)…\n")
    probs = secrets.problems()
    if probs:
        for p in probs:
            bad(f"secrets.toml: {p}")
        return 1
    client = UpstoxClient(secrets)
    try:
        await client.login()
        ok("Upstox accepted the Analytics Token")
        rows = await client.download_master(DATA_DIR / "cache", now_ist().date().isoformat())
        contracts = parse_master(rows)
        for k in INDEX_META:
            u = OptionUniverse(k, contracts)
            exp, flags = u.choose_expiry(now_ist().date())
            if exp:
                ok(f"{k}: next expiry {exp:%a %d %b %Y}, lot size {u.contracts[0].lot}, "
                   f"strike step {u.step(exp):g}" + (f"  ({flags[0]})" if flags else ""))
            else:
                bad(f"{k}: no option contracts found in the instrument files")
        req: dict[str, list[str]] = {"NSE": [VIX_META["token"]]}
        for m in INDEX_META.values():
            req.setdefault(m["exchange"], []).append(m["token"])
        q = await client.quote("FULL", req)
        names = {m["token"]: m["label"] for m in INDEX_META.values()}
        names[VIX_META["token"]] = "India VIX"
        for r in q:
            ok(f"Quote {names.get(r['symbolToken'], r['tradingSymbol'])}: {r['ltp']} (previous close {r['close']:.2f})")
        if not q:
            bad("Quote request returned nothing")
        end = now_ist()
        c = await client.candles("NSE", INDEX_META["NIFTY"]["token"], "FIVE_MINUTE", end - timedelta(days=7), end)
        (ok if c else bad)(f"Historical candles: {len(c)} NIFTY 5-minute bars for the last week")
        try:
            _load_proto()
            ok("Feed decoder installed")
        except UpstoxError as e:
            bad(str(e))
            return 1
        ticks = {"n": 0}
        feed = UpstoxFeed(client, lambda t, rx: ticks.__setitem__("n", ticks["n"] + 1))
        want: dict[int, set[str]] = {VIX_META["ws_type"]: {VIX_META["token"]}}
        for m in INDEX_META.values():
            want.setdefault(m["ws_type"], set()).add(m["token"])
        feed.want(want)
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(12)
        feed.stop()
        task.cancel()
        if feed.connected or ticks["n"]:
            ok(f"Live feed connected; {ticks['n']} ticks in 12 s" +
               ("" if ticks["n"] else " (none is normal when the market is closed)"))
        else:
            bad(f"Live feed did not connect: {feed.last_error or 'no reply'}")
        print("\nAll done. Start the app with:  bash start-demo.command   (or .venv/bin/python -m app)\n")
        return 0
    except AngelError as e:
        bad(str(e))
        return 1
    finally:
        await client.close()
