# ORB Option Seller Strategy

**Balfund Trading Private Limited**

Intraday option selling strategy using Opening Range Breakout on index futures chart with 200 EMA stop loss on option premium.

## Strategy Summary

- **Chart**: Index Futures (1-min) for ORB levels and breakout detection
- **Entry**: First 1-min candle defines Opening Range. Breakout above → Sell OTM PE; Breakout below → Sell OTM CE
- **Strike**: ATM ± configurable OTM offset (per instrument)
- **Stop Loss**: 200 EMA on the sold option's 1-min premium chart (green candle closes above EMA = exit)
- **Re-Entry**: Candle closes below 200 EMA → re-enter same direction
- **Expiry**: Monthly options. Before 20th → current month; From 20th → next month
- **Session**: Entry 09:16–14:45 IST. Mandatory exit at 15:29

## Supported Instruments

| Instrument | ATM Step | OTM Offset | Exchange |
|------------|----------|------------|----------|
| NIFTY      | 50       | Configurable | NSE    |
| BANKNIFTY  | 100      | Configurable | NSE    |
| SENSEX     | 100      | Configurable | BSE    |
| BANKEX     | 100      | Configurable | BSE    |
| FINNIFTY   | 50       | Configurable | NSE    |

## Files

| File | Purpose |
|------|---------|
| `fyers_connect.py` | Fyers API V3 automated login (TOTP + PIN) |
| `fyers_token.py` | Compact token generator |
| `strategy.py` | Core strategy engine |
| `gui.py` | CustomTkinter GUI (white + blue theme) |
| `bundler.py` | Merges all files into single `bundled_main.py` for PyInstaller |
| `requirements.txt` | Python dependencies |

## Quick Start

```bash
pip install -r requirements.txt
python gui.py
```

## Building EXE (Windows)

```bash
python bundler.py
pyinstaller --onefile --windowed --name ORBOptionSeller bundled_main.py
```

## Broker: Fyers API V3

- App ID: `LPXLEAXXE1-200`
- Uses automated TOTP login (no manual browser auth needed)
- SSL fix included for PyInstaller bundles
- Symbol master auto-downloaded from `public.fyers.in`
