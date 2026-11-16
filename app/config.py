"""Settings and secrets.

Secrets (API key, client code, PIN, TOTP secret) live in config/secrets.toml,
which is git-ignored. They are wrapped in `Secret`, whose str() and repr()
never show the value, and every log line passes through `RedactingFilter`,
which masks any registered secret and anything that looks like a JWT.
Nothing in this module (or anywhere in the app) sends a secret to the browser.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import tomllib
from dataclasses import dataclass, fields, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
SECRETS_PATH = CONFIG_DIR / "secrets.toml"
SETTINGS_PATH = CONFIG_DIR / "settings.toml"
HOLIDAYS_PATH = CONFIG_DIR / "holidays.toml"

# ----------------------------------------------------------------- secrets


class Secret:
    """A string that refuses to print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = (value or "").strip()

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"


_SECRET_VALUES: set[str] = set()
_SECRET_LOCK = threading.Lock()
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def register_secret(value: str | None) -> None:
    """Remember a value so the log filter masks it wherever it appears."""
    if value and len(value) >= 4:
        with _SECRET_LOCK:
            _SECRET_VALUES.add(value)


def redact(text: str) -> str:
    with _SECRET_LOCK:
        values = sorted(_SECRET_VALUES, key=len, reverse=True)
    for v in values:
        if v in text:
            text = text.replace(v, "***")
    return _JWT_RE.sub("***jwt***", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info:
            # Render the traceback now, redacted, and drop the raw exception.
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        return True


class NoiseFilter(logging.Filter):
    """Hide harmless Windows/browser noise that looks alarming in the console.

    - "Invalid HTTP request received": a browser trying a secure (https) connection to the app.
    - ConnectionResetError 10054 in _call_connection_lost: Windows reporting that a browser tab
      closed its connection (a known, harmless asyncio quirk on Windows).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        if "Invalid HTTP request received" in msg:
            return False
        if record.name == "asyncio" and ("_call_connection_lost" in msg or (
                record.exc_info and isinstance(record.exc_info[1], (ConnectionResetError, ConnectionAbortedError)))):
            return False
        return True


@dataclass
class Secrets:
    api_key: Secret
    client_code: Secret
    pin: Secret
    totp_secret: Secret
    historical_api_key: Secret
    public_ip: str = ""

    def problems(self) -> list[str]:
        out = []
        if not self.api_key:
            out.append("api_key is empty")
        if not self.client_code:
            out.append("client_code is empty")
        if not self.pin:
            out.append("pin is empty")
        elif not self.pin.reveal().isdigit():
            out.append("pin should be digits only (your Angel One MPIN)")
        if not self.totp_secret:
            out.append("totp_secret is empty")
        elif not re.fullmatch(r"[A-Z2-7=\s]+", self.totp_secret.reveal().upper()):
            out.append("totp_secret does not look like a TOTP key (letters A-Z and digits 2-7)")
        return out


PLACEHOLDERS = {"", "PASTE_HERE", "your_api_key", "YOUR_API_KEY"}


def load_secrets(path: Path | None = None) -> Secrets | None:
    """Return Secrets, or None when the file is missing or still has placeholders."""
    path = path or SECRETS_PATH
    if not path.exists():
        return None
    with path.open("rb") as f:
        raw = tomllib.load(f)
    a = raw.get("angel", raw)

    def get(key: str) -> str:
        v = str(a.get(key, "") or "").strip()
        return "" if v in PLACEHOLDERS else v

    s = Secrets(
        api_key=Secret(get("api_key")),
        client_code=Secret(get("client_code").upper()),
        pin=Secret(get("pin")),
        totp_secret=Secret(get("totp_secret").replace(" ", "").upper()),
        historical_api_key=Secret(get("historical_api_key")),
        public_ip=get("public_ip"),
    )
    for v in (s.api_key, s.client_code, s.pin, s.totp_secret, s.historical_api_key):
        register_secret(v.reveal())
    if not (s.api_key or s.client_code or s.pin or s.totp_secret):
        return None
    return s


# ---------------------------------------------------------------- settings


@dataclass
class Settings:
    # Risk limits (rupees)
    max_loss_per_trade: float = 2000.0
    max_loss_confirmed: bool = False  # becomes true once you save it in Settings
    daily_loss_limit: float = 6000.0  # 3x max loss by default
    max_trades_per_day: int = 3
    max_lots: int = 10
    # Strategy
    or_start: str = "09:15"
    or_end: str = "09:30"
    last_signal_time: str = "14:00"
    time_exit: str = "15:00"
    stop_rule: str = "mid"  # "mid" = range midpoint, "far" = far side of the range (what the tested model uses)
    target_r: float = 2.0  # target in R (1, 1.5 or 2); with exit_mode "hold" it is a reference level only
    breakout_buffer: float = 0.2  # the 5-min close must clear the range by this x the range width
    exit_mode: str = "trail"  # "trail" = stop or 15:00, and after +1R the stop follows 1R behind the best price
    #                           (tested: same profit as "hold", more wins, smaller bad months); "hold" (A) = stop or
    #                           time exit; "half" (B) = sell half at half_at_r, stop the rest
    #                          at entry; "target" = take all profit at target_r
    half_at_r: float = 1.0   # version B: where half is sold, in R
    trend_filter: str = "yesterday"  # "yesterday" = only trade in the direction of yesterday's move; "none"
    skip_after_wide_day: bool = True  # tested model: no signal the day after a wide-range day
    wide_day_atr: float = 1.2         # wide = yesterday's range > this x its 14-day average range
    strike_mode: str = "atm"  # "atm", "itm1" or "best" (best fit from the strike comparison)
    max_signal_age_min: int = 10
    min_reward_risk: float = 1.0  # don't chase: skip once the rest of the move pays less than this
    # Option sanity checks
    nifty_min_premium: float = 20.0
    nifty_max_premium: float = 400.0
    sensex_min_premium: float = 50.0
    sensex_max_premium: float = 1500.0
    banknifty_min_premium: float = 40.0
    banknifty_max_premium: float = 1500.0
    max_spread_pct: float = 3.0
    # Costs (edit to match your contract note)
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.15
    nse_txn_pct: float = 0.03503
    bse_txn_pct: float = 0.0325
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0
    slippage_ticks: int = 1
    slippage_pct_no_quote: float = 0.5
    # Model
    risk_free_rate_pct: float = 6.0
    default_iv_pct: float = 14.0
    # Odds
    mc_replays: int = 10000
    mc_seed: int = 19
    history_max_days: int = 1500
    # Feed
    stale_after_s: float = 5.0
    disconnected_after_s: float = 30.0
    chain_strikes_each_side: int = 10
    port: int = 8765

    def validate(self) -> list[str]:
        errs = []
        if not (self.max_loss_per_trade > 0):
            errs.append("max_loss_per_trade must be above 0")
        if not (self.daily_loss_limit >= self.max_loss_per_trade):
            errs.append("daily_loss_limit must be at least max_loss_per_trade")
        if not (1 <= self.max_trades_per_day <= 20):
            errs.append("max_trades_per_day must be 1-20")
        if not (0.5 <= float(self.wide_day_atr) <= 5):
            errs.append("wide_day_atr must be 0.5-5")
        if self.stop_rule not in ("mid", "far"):
            errs.append("stop_rule must be 'mid' or 'far'")
        if self.target_r not in (1.0, 1.5, 2.0):
            errs.append("target_r must be 1, 1.5 or 2")
        if not (0.0 <= self.breakout_buffer <= 1.0):
            errs.append("breakout_buffer must be 0-1 (a fraction of the opening range width)")
        if self.exit_mode not in ("trail", "hold", "half", "target"):
            errs.append("exit_mode must be 'trail', 'hold', 'half' or 'target'")
        if not (0.25 <= self.half_at_r <= 3.0):
            errs.append("half_at_r must be 0.25-3")
        if self.trend_filter not in ("yesterday", "none"):
            errs.append("trend_filter must be 'yesterday' or 'none'")
        if self.strike_mode not in ("atm", "itm1", "best"):
            errs.append("strike_mode must be 'atm', 'itm1' or 'best'")
        if not (0.2 <= self.min_reward_risk <= 3):
            errs.append("min_reward_risk must be 0.2-3")
        times_ok = True
        for name in ("or_start", "or_end", "last_signal_time", "time_exit"):
            if not re.fullmatch(r"\d{2}:\d{2}", getattr(self, name)):
                errs.append(f"{name} must look like 09:30")
                times_ok = False
        if times_ok:
            if self.time_exit > "15:10":
                errs.append("time_exit must be 15:10 or earlier (NIFTY and SENSEX stop moving at 15:15 "
                            "for the closing auction)")
            if not (self.or_end < self.last_signal_time < self.time_exit):
                errs.append("times must run: opening range end < last signal time < time exit")
        if not (1 <= self.max_lots <= 100):
            errs.append("max_lots must be 1-100")
        if not (1024 <= int(self.port) <= 65535):
            errs.append("port must be 1024-65535")
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float) and not math.isfinite(v):
                errs.append(f"{f.name} must be a number")
        return errs

    def min_premium(self, index: str) -> float:
        return float(getattr(self, f"{index.lower()}_min_premium", self.sensex_min_premium))

    def max_premium(self, index: str) -> float:
        return float(getattr(self, f"{index.lower()}_max_premium", self.sensex_max_premium))

    def txn_pct(self, exch: str) -> float:
        return self.nse_txn_pct if exch == "nse" else self.bse_txn_pct


def _coerce(f, value):
    t = f.type if isinstance(f.type, str) else f.type.__name__
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t == "int":
        return int(float(value))
    if t == "float":
        return float(value)
    return str(value)


def settings_from_dict(d: dict, base: Settings | None = None) -> Settings:
    s = Settings(**asdict(base)) if base else Settings()
    by_name = {f.name: f for f in fields(Settings)}
    flat = {}
    for k, v in d.items():
        if isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    for k, v in flat.items():
        if k in by_name:
            setattr(s, k, _coerce(by_name[k], v))
    return s


def load_settings(path: Path | None = None) -> Settings:
    path = path or SETTINGS_PATH
    if not path.exists():
        return Settings()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    s = settings_from_dict(raw)
    errs = s.validate()
    if errs:
        raise ValueError("config/settings.toml: " + "; ".join(errs))
    return s


SECTIONS = {
    "risk": ["max_loss_per_trade", "max_loss_confirmed", "daily_loss_limit", "max_trades_per_day", "max_lots"],
    "strategy": ["or_start", "or_end", "last_signal_time", "time_exit", "stop_rule", "target_r",
                 "breakout_buffer", "trend_filter", "skip_after_wide_day", "wide_day_atr", "exit_mode", "half_at_r", "strike_mode", "max_signal_age_min", "min_reward_risk"],
    "option_checks": ["nifty_min_premium", "nifty_max_premium", "sensex_min_premium", "sensex_max_premium",
                      "banknifty_min_premium", "banknifty_max_premium", "max_spread_pct"],
    "costs": ["brokerage_per_order", "stt_sell_pct", "nse_txn_pct", "bse_txn_pct", "sebi_per_crore",
              "stamp_buy_pct", "gst_pct", "slippage_ticks", "slippage_pct_no_quote"],
    "model": ["risk_free_rate_pct", "default_iv_pct"],
    "odds": ["mc_replays", "mc_seed", "history_max_days"],
    "feed": ["stale_after_s", "disconnected_after_s", "chain_strikes_each_side", "port"],
}


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_settings(s: Settings, path: Path | None = None) -> None:
    path = path or SETTINGS_PATH
    errs = s.validate()
    if errs:
        raise ValueError("; ".join(errs))
    lines = ["# India options signals - settings. Edit here or in the app's Settings tab.", ""]
    for section, names in SECTIONS.items():
        lines.append(f"[{section}]")
        for n in names:
            lines.append(f"{n} = {_toml_value(getattr(s, n))}")
        lines.append("")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def setup_logging(level: int = logging.INFO) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    file_h = logging.FileHandler(DATA_DIR / "logs" / "app.log", encoding="utf-8")
    for h in (console, file_h):
        h.setFormatter(fmt)
        h.addFilter(NoiseFilter())
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    # Third-party loggers can print request URLs/headers at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
