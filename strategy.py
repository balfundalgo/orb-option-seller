from __future__ import annotations

"""
ORB Option Seller Strategy - Fyers API
Balfund Trading Private Limited

Strategy Logic:
- Uses index FUTURES chart (1-min) for Opening Range Breakout detection
- Sells OTM options (configurable offset per instrument)
- 200 EMA on option premium chart for stop loss
- Re-entry on candle close below 200 EMA (same direction)
- Monthly expiry with calendar-based roll (>=20th → next month)
- Supports: NIFTY, BANKNIFTY, SENSEX, BANKEX, FINNIFTY
"""

import csv
import json
import re
import time
import urllib.request
import urllib.error
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws, order_ws

from fyers_connect import auto_login, CLIENT_ID

IST = pytz.timezone("Asia/Kolkata")

# ── Global SSL fix for PyInstaller bundles on Windows ─────────────────────────
import ssl as _ssl
try:
    import certifi as _certifi
    _ssl_ctx = _ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _ssl_ctx = _ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = _ssl.CERT_NONE
_ssl._create_default_https_context = lambda: _ssl_ctx

import websocket as _ws_mod
_orig_run_forever = _ws_mod.WebSocketApp.run_forever
def _patched_run_forever(self, **kwargs):
    if "sslopt" not in kwargs:
        kwargs["sslopt"] = {"cert_reqs": _ssl.CERT_NONE}
    return _orig_run_forever(self, **kwargs)
_ws_mod.WebSocketApp.run_forever = _patched_run_forever


# ============================================================================
# CONFIG
# ============================================================================
@dataclass
class InstrumentConfig:
    """Per-instrument configuration"""
    name: str
    futures_symbol: str         # Fyers futures symbol for ORB chart
    strike_step: int            # ATM rounding step (50 or 100)
    otm_offset: int             # OTM offset in points (configurable)
    lots: int = 1               # Number of lots
    enabled: bool = True


@dataclass
class StrategyConfig:
    paper_trading: bool = True

    # Session timing (IST)
    orb_candle_time: Tuple[int, int] = (9, 15)       # First candle: 09:15
    entry_start_time: Tuple[int, int] = (9, 16)      # Entry window opens
    entry_end_time: Tuple[int, int] = (14, 45)        # No entries/re-entries after
    force_exit_time: Tuple[int, int] = (15, 29)       # Mandatory square-off

    # EMA
    ema_period: int = 200

    # Instrument configs (populated from GUI)
    instruments: Dict[str, InstrumentConfig] = field(default_factory=dict)

    # Symbol master
    symbol_master_csv_path: Optional[str] = None
    symbol_master_refresh_days: int = 1

    # Polling
    poll_interval_seconds: float = 1.0

    # Order socket
    enable_order_socket: bool = True

    def __post_init__(self):
        if not self.instruments:
            self.instruments = {
                "NIFTY": InstrumentConfig(
                    name="NIFTY",
                    futures_symbol="NSE:NIFTY50-INDEX",  # Will be resolved to futures
                    strike_step=50,
                    otm_offset=200,
                    lots=1,
                ),
                "BANKNIFTY": InstrumentConfig(
                    name="BANKNIFTY",
                    futures_symbol="NSE:NIFTYBANK-INDEX",
                    strike_step=100,
                    otm_offset=300,
                    lots=1,
                ),
                "SENSEX": InstrumentConfig(
                    name="SENSEX",
                    futures_symbol="BSE:SENSEX-INDEX",
                    strike_step=100,
                    otm_offset=500,
                    lots=1,
                ),
                "BANKEX": InstrumentConfig(
                    name="BANKEX",
                    futures_symbol="BSE:BANKEX-INDEX",
                    strike_step=100,
                    otm_offset=300,
                    lots=1,
                ),
                "FINNIFTY": InstrumentConfig(
                    name="FINNIFTY",
                    futures_symbol="NSE:FINNIFTY-INDEX",
                    strike_step=50,
                    otm_offset=200,
                    lots=1,
                ),
            }


# ============================================================================
# DATA STRUCTURES
# ============================================================================
@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_green(self) -> bool:
        return self.close > self.open


@dataclass
class OpeningRange:
    high: float
    low: float
    candle: Candle


@dataclass
class TradeState:
    instrument_name: str
    direction: str              # "BULLISH" or "BEARISH"
    option_type: str            # "PE" or "CE"
    option_symbol: str          # Fyers option symbol
    entry_price: float          # Option premium at entry
    entry_time: datetime
    lots: int
    lot_size: int
    is_live: bool = True        # False = exited
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    order_id: Optional[str] = None


@dataclass
class InstrumentRuntime:
    """Runtime state for each instrument"""
    config: InstrumentConfig
    futures_symbol: str         # Resolved futures symbol (e.g., NSE:NIFTY25APRFUT)
    opening_range: Optional[OpeningRange] = None
    direction: Optional[str] = None       # Locked after first breakout
    option_type: Optional[str] = None     # "PE" or "CE"
    current_trade: Optional[TradeState] = None
    trade_history: List[TradeState] = field(default_factory=list)
    option_1m_candles: List[Candle] = field(default_factory=list)
    ema_200: Optional[float] = None
    last_fetched_bucket: Optional[datetime] = None
    awaiting_reentry: bool = False
    breakout_detected: bool = False


# ============================================================================
# HELPERS
# ============================================================================
def now_ist() -> datetime:
    return datetime.now(IST)


def epoch_to_ist(ts: int | float) -> datetime:
    if ts > 1e12:
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST)


def time_past(hh: int, mm: int) -> bool:
    n = now_ist()
    return (n.hour, n.minute) >= (hh, mm)


def time_before(hh: int, mm: int) -> bool:
    n = now_ist()
    return (n.hour, n.minute) < (hh, mm)


def calc_ema(candles: List[Candle], period: int) -> Optional[float]:
    """Calculate EMA on closing prices. Returns latest EMA value."""
    if not candles:
        return None
    closes = [c.close for c in candles]
    if len(closes) < 2:
        return closes[-1]
    k = 2.0 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def get_monthly_expiry_contract_month() -> Tuple[int, int]:
    """
    Returns (year, month) for which monthly expiry to use.
    Before 20th: current month. From 20th onwards: next month.
    """
    today = now_ist().date()
    if today.day >= 20:
        # Next month
        if today.month == 12:
            return (today.year + 1, 1)
        return (today.year, today.month + 1)
    return (today.year, today.month)


def calc_atm_strike(price: float, step: int) -> int:
    """Round price to nearest strike step"""
    return int(round(price / step) * step)


def calc_otm_strike(atm: int, offset: int, direction: str) -> int:
    """
    Calculate OTM strike from ATM.
    Bullish breakout → sell PE → ATM - offset
    Bearish breakout → sell CE → ATM + offset
    """
    if direction == "BULLISH":
        return atm - offset  # OTM PUT
    else:
        return atm + offset  # OTM CALL


# ============================================================================
# FYERS BROKER WRAPPER
# ============================================================================
class FyersBroker:
    def __init__(self, access_token: Optional[str] = None) -> None:
        raw_token = access_token or auto_login()
        if not raw_token:
            raise RuntimeError("Login failed — no access token returned")
        self.access_token = raw_token if ":" in raw_token else f"{CLIENT_ID}:{raw_token}"
        self.token_only = self.access_token.split(":", 1)[1]
        self.fyers = fyersModel.FyersModel(
            token=self.token_only,
            is_async=False,
            client_id=CLIENT_ID,
            log_path="",
        )

    def history(self, symbol: str, resolution: str, range_from: int, range_to: int) -> List[Candle]:
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "0",
            "range_from": str(range_from),
            "range_to": str(range_to),
            "cont_flag": "1",
        }
        resp = self.fyers.history(payload)
        candles: List[Candle] = []
        for row in resp.get("candles", []) or []:
            if len(row) < 6:
                continue
            candles.append(Candle(
                ts=epoch_to_ist(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            ))
        return candles

    def quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        payload = {"symbols": ",".join(symbols)}
        resp = self.fyers.quotes(payload)
        out: Dict[str, Dict[str, Any]] = {}
        for item in resp.get("d", []) or []:
            key = item.get("n") or item.get("symbol")
            v = item.get("v", {}) if isinstance(item.get("v"), dict) else {}
            if key:
                out[key] = {
                    "ltp": float(v.get("lp", v.get("ltp", 0.0)) or 0.0),
                    "bid": float(v.get("bid_price", v.get("bid", 0.0)) or 0.0),
                    "ask": float(v.get("ask_price", v.get("ask", 0.0)) or 0.0),
                    "prev_close": float(v.get("prev_close_price", 0.0) or 0.0),
                    "open": float(v.get("open_price", v.get("open", 0.0)) or 0.0),
                    "high": float(v.get("high_price", v.get("high", 0.0)) or 0.0),
                    "low": float(v.get("low_price", v.get("low", 0.0)) or 0.0),
                }
        return out

    def place_market_order(self, symbol: str, side: int, qty: int,
                           product_type: str = "INTRADAY") -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "qty": qty,
            "type": 2,          # Market order
            "side": side,       # -1 = SELL, 1 = BUY
            "productType": product_type,
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
            "isSliceOrder": False,
        }
        return self.fyers.place_order(payload)

    def place_limit_order(self, symbol: str, side: int, qty: int, price: float,
                          product_type: str = "INTRADAY") -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "qty": qty,
            "type": 1,          # Limit order
            "side": side,
            "productType": product_type,
            "limitPrice": price,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
            "isSliceOrder": False,
        }
        return self.fyers.place_order(payload)


# ============================================================================
# INSTRUMENT MASTER + RESOLVER
# ============================================================================
class InstrumentMaster:
    FYERS_NSE_FO_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
    FYERS_BSE_FO_URL = "https://public.fyers.in/sym_details/BSE_FO.csv"

    FIELDNAMES = [
        "token", "description", "instrument_type_code", "lot_size", "tick_size",
        "isin", "trading_session", "last_update_date", "expiry_epoch", "symbol",
        "exchange", "segment", "scrip_code", "underlying", "underlying_code",
        "strike", "option_type", "underlying_token", "reserved_1", "reserved_2", "ltp",
    ]

    def __init__(self, csv_path: Optional[str] = None, refresh_days: int = 1) -> None:
        self.refresh_days = max(1, int(refresh_days))
        self.nse_rows: List[Dict[str, Any]] = []
        self.bse_rows: List[Dict[str, Any]] = []
        self.all_rows: List[Dict[str, Any]] = []
        self.available = False

        cache_dir = Path.cwd() / "cache" / "fyers"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Download/load NSE FO
        nse_path = cache_dir / "NSE_FO.csv"
        self._ensure_csv(nse_path, self.FYERS_NSE_FO_URL)
        if nse_path.exists():
            self.nse_rows = self._load_csv(nse_path)

        # Download/load BSE FO (for SENSEX, BANKEX)
        bse_path = cache_dir / "BSE_FO.csv"
        self._ensure_csv(bse_path, self.FYERS_BSE_FO_URL)
        if bse_path.exists():
            self.bse_rows = self._load_csv(bse_path)

        self.all_rows = self.nse_rows + self.bse_rows
        self.available = bool(self.all_rows)
        print(f"[MASTER] Total rows: {len(self.all_rows)} (NSE: {len(self.nse_rows)}, BSE: {len(self.bse_rows)})")

        # Sanity
        ce = sum(1 for r in self.all_rows if r["instrument_type"] == "CE")
        pe = sum(1 for r in self.all_rows if r["instrument_type"] == "PE")
        fut = sum(1 for r in self.all_rows if r["instrument_type"] == "FUT")
        print(f"[MASTER] Parsed → CE: {ce}, PE: {pe}, FUT: {fut}")

    def _ensure_csv(self, path: Path, url: str) -> None:
        if path.exists():
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age <= timedelta(days=self.refresh_days):
                return
        try:
            import ssl
            print(f"[MASTER] Downloading {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            ssl_ctx = ssl.create_default_context()
            try:
                import certifi
                ssl_ctx.load_verify_locations(certifi.where())
            except ImportError:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
                data = resp.read()
            if data:
                path.write_bytes(data)
                print(f"[MASTER] Downloaded {len(data)} bytes → {path}")
        except Exception as e:
            print(f"[MASTER WARN] Download failed: {e}")

    def _load_csv(self, path: Path) -> List[Dict[str, Any]]:
        rows = []
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            for raw in reader:
                if not raw or len(raw) < 10:
                    continue
                row = {self.FIELDNAMES[i]: raw[i] if i < len(raw) else ""
                       for i in range(len(self.FIELDNAMES))}
                parsed = self._normalize(row)
                if parsed:
                    rows.append(parsed)
        print(f"[MASTER] Loaded {len(rows)} rows from {path.name}")
        return rows

    def _normalize(self, row: Dict[str, str]) -> Optional[Dict[str, Any]]:
        fyers_symbol = row.get("symbol", "").strip()
        description = row.get("description", "").strip()
        raw_type = row.get("instrument_type_code", "").strip()
        option_type_col = row.get("option_type", "").strip().upper()
        exchange = row.get("exchange", "").strip()

        # Determine instrument type
        if raw_type in {"11", "13"} or "FUT" in fyers_symbol.upper():
            itype = "FUT"
        elif raw_type == "14":
            if option_type_col in {"CE", "PE"}:
                itype = option_type_col
            elif "CE" in fyers_symbol.upper().split("-")[0][-5:]:
                itype = "CE"
            elif "PE" in fyers_symbol.upper().split("-")[0][-5:]:
                itype = "PE"
            else:
                itype = "UNKNOWN"
        else:
            itype = "UNKNOWN"

        if itype == "UNKNOWN":
            return None

        # Base symbol
        base = self._extract_base(fyers_symbol, description)
        if not base:
            return None

        # Expiry
        expiry = None
        expiry_epoch = row.get("expiry_epoch", "").strip()
        if expiry_epoch:
            try:
                expiry = datetime.fromtimestamp(int(float(expiry_epoch)), tz=timezone.utc).date()
            except Exception:
                pass

        # Strike
        strike = None
        strike_raw = row.get("strike", "").strip()
        try:
            strike = float(strike_raw.replace(",", "")) if strike_raw else None
        except Exception:
            pass

        # Lot size
        lot_raw = row.get("lot_size", "").strip()
        try:
            lot_size = max(int(float(lot_raw)), 1) if lot_raw else 1
        except Exception:
            lot_size = 1

        return {
            "base": base,
            "fyers_symbol": fyers_symbol,
            "description": description,
            "instrument_type": itype,
            "expiry": expiry,
            "strike": strike,
            "lot_size": lot_size,
            "exchange": exchange,
        }

    @staticmethod
    def _extract_base(symbol: str, description: str) -> str:
        s = symbol.split(":")[-1].upper() if ":" in symbol else symbol.upper()
        s = re.sub(r"-EQ$", "", s)
        s = re.sub(r"-INDEX$", "", s)
        # Strip FO date suffix
        s = re.sub(r"\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*$", "", s)
        s = re.sub(r"\s+\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*$",
                   "", s.strip(), flags=re.IGNORECASE)
        return s.strip()

    def resolve_futures(self, base: str) -> Optional[str]:
        """Find nearest month futures symbol for a given base"""
        today = now_ist().date()
        fut_rows = [
            r for r in self.all_rows
            if r["base"] == base and r["instrument_type"] == "FUT"
            and r["expiry"] and r["expiry"] >= today
        ]
        if not fut_rows:
            return None
        fut_rows.sort(key=lambda x: x["expiry"])
        return fut_rows[0]["fyers_symbol"]

    def resolve_option(self, base: str, strike: int, opt_type: str,
                       target_year: int, target_month: int) -> Optional[Dict[str, Any]]:
        """
        Find a specific option contract.
        opt_type: "CE" or "PE"
        Returns the row dict or None.
        """
        today = now_ist().date()
        candidates = [
            r for r in self.all_rows
            if r["base"] == base
            and r["instrument_type"] == opt_type
            and r["strike"] is not None
            and abs(r["strike"] - strike) < 0.01
            and r["expiry"] is not None
            and r["expiry"] >= today
            and r["expiry"].year == target_year
            and r["expiry"].month == target_month
        ]
        if not candidates:
            # Fallback: try nearest expiry in that month
            month_candidates = [
                r for r in self.all_rows
                if r["base"] == base
                and r["instrument_type"] == opt_type
                and r["strike"] is not None
                and abs(r["strike"] - strike) < 0.01
                and r["expiry"] is not None
                and r["expiry"] >= today
            ]
            if month_candidates:
                month_candidates.sort(key=lambda x: x["expiry"])
                return month_candidates[0]
            return None

        # Pick the last expiry in the target month (monthly expiry)
        candidates.sort(key=lambda x: x["expiry"], reverse=True)
        return candidates[0]

    def get_lot_size(self, base: str) -> int:
        """Get standard lot size for an instrument"""
        for r in self.all_rows:
            if r["base"] == base and r["instrument_type"] in {"CE", "PE", "FUT"}:
                return r["lot_size"]
        return 1


# ============================================================================
# ORDER SOCKET TRACKER
# ============================================================================
class OrderSocketTracker:
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self.socket = None
        self.order_events: List[Dict[str, Any]] = []

    def on_order(self, msg: Dict[str, Any]) -> None:
        self.order_events.append(msg)
        print(f"[ORDER WS] {json.dumps(msg, default=str)[:600]}")

    def on_connect(self) -> None:
        print("[ORDER WS] Connected")
        self.socket.subscribe(data_type="OnOrders,OnTrades,OnPositions")
        self.socket.keep_running()

    def on_error(self, msg: Dict[str, Any]) -> None:
        print(f"[ORDER WS][ERROR] {msg}")

    def on_close(self, msg: Dict[str, Any]) -> None:
        print(f"[ORDER WS][CLOSED] {msg}")

    def start(self) -> None:
        self.socket = order_ws.FyersOrderSocket(
            access_token=self.access_token,
            write_to_file=False,
            log_path="",
            on_connect=self.on_connect,
            on_close=self.on_close,
            on_error=self.on_error,
            on_orders=self.on_order,
            on_positions=lambda m: print(f"[ORDER WS][POS] {m}"),
            on_trades=lambda m: print(f"[ORDER WS][TRADE] {m}"),
            on_general=lambda m: print(f"[ORDER WS][GEN] {m}"),
        )
        self.socket.connect()


# ============================================================================
# MAIN STRATEGY ENGINE
# ============================================================================
class ORBOptionSellerStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.cfg = config
        self.broker: Optional[FyersBroker] = None
        self.master: Optional[InstrumentMaster] = None
        self.order_tracker: Optional[OrderSocketTracker] = None
        self.runtimes: Dict[str, InstrumentRuntime] = {}
        self.day_pnl: float = 0.0
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _is_stopped(self) -> bool:
        return self._stop_event.is_set()

    # ── Initialization ─────────────────────────────────────────────────────
    def initialize(self) -> bool:
        """Login, load master, resolve futures, start order socket"""
        print("=" * 60)
        print("  ORB OPTION SELLER - Initializing")
        print("  Balfund Trading Pvt. Ltd.")
        print("=" * 60)

        # Login
        print("\n[INIT] Logging in to Fyers...")
        try:
            self.broker = FyersBroker()
        except Exception as e:
            print(f"[INIT ERROR] Login failed: {e}")
            return False
        print("[INIT] ✓ Broker connected")

        # Load instrument master
        print("[INIT] Loading instrument master...")
        try:
            self.master = InstrumentMaster(
                csv_path=self.cfg.symbol_master_csv_path,
                refresh_days=self.cfg.symbol_master_refresh_days,
            )
        except Exception as e:
            print(f"[INIT ERROR] Master load failed: {e}")
            return False

        if not self.master.available:
            print("[INIT ERROR] No symbol master data available")
            return False
        print("[INIT] ✓ Instrument master loaded")

        # Resolve futures symbols and create runtimes
        enabled_instruments = {
            k: v for k, v in self.cfg.instruments.items() if v.enabled
        }
        if not enabled_instruments:
            print("[INIT ERROR] No instruments enabled")
            return False

        for name, inst_cfg in enabled_instruments.items():
            base = name.upper()
            futures_sym = self.master.resolve_futures(base)
            if not futures_sym:
                print(f"[INIT WARN] Could not resolve futures for {base}, skipping")
                continue

            self.runtimes[name] = InstrumentRuntime(
                config=inst_cfg,
                futures_symbol=futures_sym,
            )
            print(f"[INIT] ✓ {name}: futures={futures_sym}, step={inst_cfg.strike_step}, "
                  f"offset={inst_cfg.otm_offset}, lots={inst_cfg.lots}")

        if not self.runtimes:
            print("[INIT ERROR] No instruments successfully resolved")
            return False

        # Order socket (live mode only)
        if not self.cfg.paper_trading and self.cfg.enable_order_socket:
            try:
                self.order_tracker = OrderSocketTracker(self.broker.access_token)
                threading.Thread(target=self.order_tracker.start, daemon=True).start()
                print("[INIT] ✓ Order socket started")
            except Exception as e:
                print(f"[INIT WARN] Order socket failed: {e}")

        mode = "PAPER" if self.cfg.paper_trading else "LIVE"
        print(f"\n[INIT] ✓ Ready. Mode: {mode}. Instruments: {list(self.runtimes.keys())}")
        return True

    # ── Main Loop ──────────────────────────────────────────────────────────
    def loop(self) -> None:
        """Main strategy loop — call after initialize()"""
        print("\n[STRATEGY] Entering main loop...")

        # Wait for ORB candle to close (09:16)
        self._wait_for_time(self.cfg.entry_start_time[0], self.cfg.entry_start_time[1],
                            "Waiting for ORB candle close (09:16)...")

        if self._is_stopped():
            return

        # Fetch ORB candle for each instrument
        self._fetch_opening_ranges()

        # Main polling loop
        while not self._is_stopped():
            n = now_ist()

            # Force exit time
            if (n.hour, n.minute) >= tuple(self.cfg.force_exit_time):
                self._force_exit_all("TIME_EXIT_15:29")
                break

            # Process each instrument
            for name, rt in self.runtimes.items():
                if rt.opening_range is None:
                    continue
                self._process_instrument(name, rt)

            time.sleep(self.cfg.poll_interval_seconds)

        print("\n[STRATEGY] Loop ended.")
        self._print_summary()

    def _wait_for_time(self, hh: int, mm: int, msg: str) -> None:
        """Wait until IST hh:mm, sleeping in 1s intervals"""
        print(f"[WAIT] {msg}")
        while not self._is_stopped():
            n = now_ist()
            if (n.hour, n.minute) >= (hh, mm):
                return
            time.sleep(1)

    # ── ORB Candle ─────────────────────────────────────────────────────────
    def _fetch_opening_ranges(self) -> None:
        """Fetch first 1-min candle from futures chart for each instrument"""
        for name, rt in self.runtimes.items():
            try:
                today = now_ist().replace(hour=9, minute=15, second=0, microsecond=0)
                end = today.replace(minute=16)
                candles = self.broker.history(
                    rt.futures_symbol, "1",
                    int(today.timestamp()),
                    int(end.timestamp())
                )
                if candles:
                    orb = candles[0]
                    rt.opening_range = OpeningRange(
                        high=orb.high,
                        low=orb.low,
                        candle=orb,
                    )
                    print(f"[ORB] {name}: High={orb.high:.2f}, Low={orb.low:.2f} "
                          f"(O={orb.open:.2f} C={orb.close:.2f})")
                else:
                    print(f"[ORB WARN] {name}: No candle data for 09:15")
            except Exception as e:
                print(f"[ORB ERROR] {name}: {e}")

    # ── Per-Instrument Processing ──────────────────────────────────────────
    def _process_instrument(self, name: str, rt: InstrumentRuntime) -> None:
        """Process one instrument per tick"""
        n = now_ist()
        current_bucket = n.replace(second=0, microsecond=0)

        # Skip if we already processed this minute
        if rt.last_fetched_bucket == current_bucket:
            return

        # Fetch latest closed 1-min futures candle
        prev_bucket = current_bucket - timedelta(minutes=1)

        # Phase 1: Breakout detection (if no direction locked yet)
        if not rt.breakout_detected:
            self._check_breakout(name, rt, prev_bucket)
            return

        # Phase 2: Active trade management
        if rt.current_trade and rt.current_trade.is_live:
            self._manage_trade(name, rt, prev_bucket)
        elif rt.awaiting_reentry:
            # Phase 3: Re-entry monitoring
            if (n.hour, n.minute) <= tuple(self.cfg.entry_end_time):
                self._check_reentry(name, rt, prev_bucket)

        rt.last_fetched_bucket = current_bucket

    def _check_breakout(self, name: str, rt: InstrumentRuntime,
                        bucket: datetime) -> None:
        """Check if futures candle closes beyond OR levels"""
        try:
            candle = self._fetch_1m_candle(rt.futures_symbol, bucket)
            if candle is None:
                return

            orb = rt.opening_range
            if candle.close > orb.high:
                rt.direction = "BULLISH"
                rt.option_type = "PE"
                rt.breakout_detected = True
                print(f"[BREAKOUT] {name}: BULLISH — Close {candle.close:.2f} > OR High {orb.high:.2f}")
                self._enter_trade(name, rt)
            elif candle.close < orb.low:
                rt.direction = "BEARISH"
                rt.option_type = "CE"
                rt.breakout_detected = True
                print(f"[BREAKOUT] {name}: BEARISH — Close {candle.close:.2f} < OR Low {orb.low:.2f}")
                self._enter_trade(name, rt)
        except Exception as e:
            print(f"[BREAKOUT ERROR] {name}: {e}")

    def _enter_trade(self, name: str, rt: InstrumentRuntime) -> None:
        """Enter option selling trade"""
        n = now_ist()
        if (n.hour, n.minute) > tuple(self.cfg.entry_end_time):
            print(f"[ENTRY SKIP] {name}: Past entry cutoff {self.cfg.entry_end_time}")
            return

        try:
            # Get current futures price for ATM calculation
            quotes = self.broker.quotes([rt.futures_symbol])
            fut_data = quotes.get(rt.futures_symbol, {})
            fut_ltp = fut_data.get("ltp", 0)
            if fut_ltp <= 0:
                print(f"[ENTRY ERROR] {name}: No futures LTP")
                return

            # Calculate strike
            atm = calc_atm_strike(fut_ltp, rt.config.strike_step)
            otm_strike = calc_otm_strike(atm, rt.config.otm_offset, rt.direction)

            # Resolve option contract
            exp_year, exp_month = get_monthly_expiry_contract_month()
            option_row = self.master.resolve_option(
                base=name,
                strike=otm_strike,
                opt_type=rt.option_type,
                target_year=exp_year,
                target_month=exp_month,
            )

            if not option_row:
                print(f"[ENTRY ERROR] {name}: Could not resolve {rt.option_type} "
                      f"strike={otm_strike} for {exp_year}-{exp_month:02d}")
                return

            option_symbol = option_row["fyers_symbol"]
            lot_size = option_row["lot_size"]
            total_qty = lot_size * rt.config.lots

            print(f"[ENTRY] {name}: Selling {rt.option_type} | Symbol={option_symbol} "
                  f"| Strike={otm_strike} | Lots={rt.config.lots} | Qty={total_qty} "
                  f"| Futures LTP={fut_ltp:.2f} | ATM={atm} | Expiry={option_row['expiry']}")

            # Get option LTP for paper trade
            opt_quotes = self.broker.quotes([option_symbol])
            opt_data = opt_quotes.get(option_symbol, {})
            opt_ltp = opt_data.get("ltp", 0)

            if self.cfg.paper_trading:
                # Paper trade
                entry_price = opt_ltp if opt_ltp > 0 else 100.0
                print(f"[PAPER ENTRY] {name}: SELL {option_symbol} @ {entry_price:.2f}")
                order_id = f"PAPER_{name}_{n.strftime('%H%M%S')}"
            else:
                # Live trade
                resp = self.broker.place_market_order(
                    symbol=option_symbol,
                    side=-1,  # SELL
                    qty=total_qty,
                    product_type="INTRADAY",
                )
                status = resp.get("s", "")
                order_id = resp.get("id") or resp.get("orderId") or ""
                if status != "ok":
                    print(f"[ENTRY ERROR] {name}: Order failed: {resp}")
                    return
                entry_price = opt_ltp if opt_ltp > 0 else 0
                print(f"[LIVE ENTRY] {name}: SELL {option_symbol} order_id={order_id}")

            # Create trade state
            rt.current_trade = TradeState(
                instrument_name=name,
                direction=rt.direction,
                option_type=rt.option_type,
                option_symbol=option_symbol,
                entry_price=entry_price,
                entry_time=n,
                lots=rt.config.lots,
                lot_size=lot_size,
                order_id=order_id,
            )
            rt.awaiting_reentry = False

            # Reset option candles for fresh EMA tracking
            rt.option_1m_candles = []
            rt.ema_200 = None

        except Exception as e:
            print(f"[ENTRY ERROR] {name}: {e}")

    def _manage_trade(self, name: str, rt: InstrumentRuntime,
                      bucket: datetime) -> None:
        """Check 200 EMA stop loss on option premium chart"""
        try:
            # Fetch latest 1m candle of the OPTION
            candle = self._fetch_1m_candle(rt.current_trade.option_symbol, bucket)
            if candle is None:
                return

            rt.option_1m_candles.append(candle)

            # Calculate 200 EMA
            ema = calc_ema(rt.option_1m_candles, self.cfg.ema_period)
            rt.ema_200 = ema

            if ema is None:
                return

            # Exit condition: GREEN candle closes ABOVE 200 EMA
            if candle.is_green and candle.close > ema:
                print(f"[SL HIT] {name}: Green candle Close={candle.close:.2f} > "
                      f"200 EMA={ema:.2f} — Exiting")
                self._exit_trade(name, rt, candle.close, "200_EMA_SL")
                rt.awaiting_reentry = True
            else:
                # Log periodic status
                if len(rt.option_1m_candles) % 15 == 0:
                    pnl = (rt.current_trade.entry_price - candle.close) * \
                          rt.current_trade.lot_size * rt.current_trade.lots
                    print(f"[MONITOR] {name}: Premium={candle.close:.2f} "
                          f"EMA={ema:.2f} Entry={rt.current_trade.entry_price:.2f} "
                          f"P&L=₹{pnl:.0f}")
        except Exception as e:
            print(f"[MANAGE ERROR] {name}: {e}")

    def _check_reentry(self, name: str, rt: InstrumentRuntime,
                       bucket: datetime) -> None:
        """Check if candle closes below 200 EMA for re-entry"""
        if not rt.option_1m_candles:
            return

        try:
            # We need to keep tracking the option premium even after exit
            # Use the last traded option symbol for EMA continuity
            last_symbol = rt.trade_history[-1].option_symbol if rt.trade_history else None
            if not last_symbol:
                return

            candle = self._fetch_1m_candle(last_symbol, bucket)
            if candle is None:
                return

            rt.option_1m_candles.append(candle)
            ema = calc_ema(rt.option_1m_candles, self.cfg.ema_period)
            rt.ema_200 = ema

            if ema is None:
                return

            if candle.close < ema:
                print(f"[REENTRY SIGNAL] {name}: Close={candle.close:.2f} < "
                      f"200 EMA={ema:.2f} — Re-entering same direction")
                self._enter_trade(name, rt)
        except Exception as e:
            print(f"[REENTRY ERROR] {name}: {e}")

    def _exit_trade(self, name: str, rt: InstrumentRuntime,
                    exit_price: float, reason: str) -> None:
        """Exit current trade"""
        trade = rt.current_trade
        if not trade or not trade.is_live:
            return

        n = now_ist()
        trade.exit_price = exit_price
        trade.exit_time = n
        trade.exit_reason = reason
        trade.is_live = False

        # Calculate P&L (sold at entry, buy back at exit)
        pnl = (trade.entry_price - exit_price) * trade.lot_size * trade.lots
        self.day_pnl += pnl

        if not self.cfg.paper_trading:
            # Place buy-back order
            total_qty = trade.lot_size * trade.lots
            try:
                resp = self.broker.place_market_order(
                    symbol=trade.option_symbol,
                    side=1,  # BUY (to close short)
                    qty=total_qty,
                    product_type="INTRADAY",
                )
                oid = resp.get("id") or resp.get("orderId") or ""
                print(f"[LIVE EXIT] {name}: BUY {trade.option_symbol} x{total_qty} "
                      f"order_id={oid} reason={reason}")
            except Exception as e:
                print(f"[EXIT ERROR] {name}: Live exit failed: {e}")
        else:
            print(f"[PAPER EXIT] {name}: {trade.option_symbol} @ {exit_price:.2f} "
                  f"reason={reason} P&L=₹{pnl:.0f}")

        rt.trade_history.append(trade)
        rt.current_trade = None

    def _force_exit_all(self, reason: str) -> None:
        """Force exit all open positions at 15:29"""
        print(f"\n[FORCE EXIT] {reason} — Exiting all positions")
        for name, rt in self.runtimes.items():
            if rt.current_trade and rt.current_trade.is_live:
                # Get current premium
                try:
                    quotes = self.broker.quotes([rt.current_trade.option_symbol])
                    data = quotes.get(rt.current_trade.option_symbol, {})
                    ltp = data.get("ltp", rt.current_trade.entry_price)
                except Exception:
                    ltp = rt.current_trade.entry_price
                self._exit_trade(name, rt, ltp, reason)

    # ── Data Fetching ──────────────────────────────────────────────────────
    def _fetch_1m_candle(self, symbol: str, bucket: datetime) -> Optional[Candle]:
        """Fetch a single 1-minute candle from history"""
        start_ts = int(bucket.timestamp())
        end_ts = start_ts + 60
        candles = self.broker.history(symbol, "1", start_ts, end_ts)
        for c in candles:
            c_bucket = c.ts.replace(second=0, microsecond=0)
            if c_bucket == bucket:
                return Candle(ts=bucket, open=c.open, high=c.high, low=c.low,
                              close=c.close, volume=c.volume)
        return None

    # ── Summary ────────────────────────────────────────────────────────────
    def _print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("  DAY SUMMARY")
        print("=" * 60)
        total_trades = 0
        for name, rt in self.runtimes.items():
            trades = rt.trade_history
            if not trades:
                if rt.opening_range:
                    print(f"  {name}: No breakout detected (OR: {rt.opening_range.high:.2f} / {rt.opening_range.low:.2f})")
                else:
                    print(f"  {name}: No ORB data")
                continue
            for t in trades:
                pnl = (t.entry_price - (t.exit_price or t.entry_price)) * t.lot_size * t.lots
                total_trades += 1
                print(f"  {name}: {t.option_type} {t.option_symbol} "
                      f"Entry={t.entry_price:.2f} Exit={t.exit_price:.2f} "
                      f"P&L=₹{pnl:.0f} ({t.exit_reason})")

        print(f"\n  Total Trades: {total_trades}")
        print(f"  Day P&L: ₹{self.day_pnl:.0f}")
        print("=" * 60)

    def get_live_pnl(self) -> Dict[str, Any]:
        """Get current P&L for GUI updates"""
        result = {"day_pnl": self.day_pnl, "instruments": {}}
        for name, rt in self.runtimes.items():
            info = {
                "or_high": rt.opening_range.high if rt.opening_range else None,
                "or_low": rt.opening_range.low if rt.opening_range else None,
                "direction": rt.direction,
                "breakout": rt.breakout_detected,
                "in_trade": bool(rt.current_trade and rt.current_trade.is_live),
                "awaiting_reentry": rt.awaiting_reentry,
                "option_symbol": rt.current_trade.option_symbol if rt.current_trade else None,
                "entry_price": rt.current_trade.entry_price if rt.current_trade else None,
                "ema_200": rt.ema_200,
                "trade_count": len(rt.trade_history),
                "live_pnl": 0.0,
            }
            if rt.current_trade and rt.current_trade.is_live:
                # Estimate live P&L from last candle
                if rt.option_1m_candles:
                    last_premium = rt.option_1m_candles[-1].close
                    info["live_pnl"] = (rt.current_trade.entry_price - last_premium) * \
                                       rt.current_trade.lot_size * rt.current_trade.lots
                    info["current_premium"] = last_premium
            result["instruments"][name] = info
        return result


if __name__ == "__main__":
    cfg = StrategyConfig(paper_trading=True)
    engine = ORBOptionSellerStrategy(cfg)
    if engine.initialize():
        engine.loop()
