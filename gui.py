"""
gui.py
ORB Option Seller Strategy - GUI
Balfund Trading Private Limited

CustomTkinter-based GUI with white/blue theme.
Runs strategy in background thread, captures stdout to log panel,
displays live instrument status and P&L.
"""

from __future__ import annotations

import io
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import customtkinter as ctk

from strategy import StrategyConfig, InstrumentConfig, ORBOptionSellerStrategy


# ============================================================================
# STDOUT REDIRECTOR
# ============================================================================
class QueueWriter(io.TextIOBase):
    def __init__(self, q: queue.Queue) -> None:
        self._q = q

    def write(self, s: str) -> int:
        if s and s != "\n":
            self._q.put(s)
        return len(s)

    def flush(self) -> None:
        pass


# ============================================================================
# STRATEGY RUNNER THREAD
# ============================================================================
class StrategyRunner(threading.Thread):
    def __init__(self, cfg: StrategyConfig, log_queue: queue.Queue,
                 status_callback, pnl_callback) -> None:
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log_queue = log_queue
        self.status_callback = status_callback
        self.pnl_callback = pnl_callback
        self.engine: Optional[ORBOptionSellerStrategy] = None
        self._status = "STOPPED"

    def run(self) -> None:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        writer = QueueWriter(self.log_queue)
        sys.stdout = writer
        sys.stderr = writer

        try:
            self._status = "RUNNING"
            self.log_queue.put("[STRATEGY] Starting...")
            self.engine = ORBOptionSellerStrategy(self.cfg)
            if self.engine.initialize():
                self.engine.loop()
            else:
                self.log_queue.put("[ERROR] Initialization failed")
        except Exception as e:
            self.log_queue.put(f"[ERROR] Strategy crashed: {e}")
            import traceback
            self.log_queue.put(traceback.format_exc())
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self._status = "STOPPED"
            self.log_queue.put("[STRATEGY] Stopped.")

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()
        self.log_queue.put("[GUI] Stop requested. Finishing current cycle...")


# ============================================================================
# MAIN GUI — White + Blue Theme
# ============================================================================
class StrategyGUI:
    # ── Colour palette: White/Light background with Blue accents ──────────
    CLR_BG       = "#f7f8fc"      # Light grey-white background
    CLR_PANEL    = "#ffffff"      # White panels
    CLR_CARD     = "#f0f4ff"      # Very light blue cards
    CLR_HEADER   = "#1a56db"      # Deep blue header
    CLR_ACCENT   = "#2563eb"      # Blue accent
    CLR_ACCENT_L = "#3b82f6"      # Lighter blue
    CLR_GREEN    = "#16a34a"      # Green for profit
    CLR_RED      = "#dc2626"      # Red for loss
    CLR_TEXT     = "#1e293b"      # Dark slate text
    CLR_MUTED    = "#64748b"      # Muted grey text
    CLR_BORDER   = "#e2e8f0"      # Light border
    CLR_INPUT_BG = "#f1f5f9"      # Input background
    CLR_LOG_BG   = "#fafbfe"      # Log panel background

    SETTINGS_FILE = os.path.join(
        os.path.dirname(os.path.abspath(sys.argv[0])), "orb_settings.json"
    )
    CREDS_FILE = os.path.join(
        os.path.dirname(os.path.abspath(sys.argv[0])), "fyers_credentials.json"
    )

    @staticmethod
    def _load_credentials() -> dict:
        """Load saved credentials from JSON file, return defaults if not found."""
        creds_path = StrategyGUI.CREDS_FILE
        if os.path.exists(creds_path):
            try:
                with open(creds_path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"app_id": "", "secret_key": "", "fy_id": "", "totp_key": "", "pin": ""}

    @staticmethod
    def _save_credentials(app_id: str, secret_key: str, fy_id: str, totp_key: str, pin: str) -> None:
        """Save credentials to JSON file."""
        with open(StrategyGUI.CREDS_FILE, "w") as f:
            json.dump({
                "app_id": app_id, "secret_key": secret_key,
                "fy_id": fy_id, "totp_key": totp_key, "pin": pin
            }, f)

    def __init__(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("ORB Option Seller  |  Balfund Trading Pvt. Ltd.")
        self.root.geometry("1200x780")
        self.root.minsize(1000, 650)
        self.root.configure(fg_color=self.CLR_BG)

        self.log_queue: queue.Queue = queue.Queue()
        self.runner: Optional[StrategyRunner] = None
        self._status = "STOPPED"
        self._day_pnl = 0.0

        self._build_ui()
        self._load_settings()
        self._poll_log()
        self._poll_pnl()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Construction ───────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        header = ctk.CTkFrame(self.root, fg_color=self.CLR_HEADER, height=56, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header, text="  ORB Option Seller Strategy",
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            text_color="#ffffff",
        ).pack(side="left", padx=20, pady=14)

        ctk.CTkLabel(
            header, text="Balfund Trading Pvt. Ltd.  ",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#93c5fd",
        ).pack(side="right", padx=16, pady=14)

        # Body
        body = ctk.CTkFrame(self.root, fg_color=self.CLR_BG, corner_radius=0)
        body.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        # Left panel (config)
        left = ctk.CTkFrame(body, fg_color=self.CLR_PANEL, width=340,
                            corner_radius=10, border_width=1, border_color=self.CLR_BORDER)
        left.pack(fill="y", side="left", padx=(0, 6))
        left.pack_propagate(False)

        left_scroll = ctk.CTkScrollableFrame(
            left, fg_color=self.CLR_PANEL, corner_radius=0,
            scrollbar_button_color=self.CLR_BORDER,
            scrollbar_button_hover_color=self.CLR_MUTED,
        )
        left_scroll.pack(fill="both", expand=True)

        # Right panel (log + status)
        right = ctk.CTkFrame(body, fg_color=self.CLR_PANEL,
                             corner_radius=10, border_width=1, border_color=self.CLR_BORDER)
        right.pack(fill="both", expand=True, side="left", padx=(6, 0))

        self._build_left_panel(left_scroll)
        self._build_right_panel(right)

    def _section(self, parent, title: str) -> ctk.CTkFrame:
        ctk.CTkLabel(
            parent, text=title,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=self.CLR_ACCENT,
        ).pack(anchor="w", padx=16, pady=(14, 2))
        frame = ctk.CTkFrame(parent, fg_color=self.CLR_CARD, corner_radius=8,
                             border_width=1, border_color=self.CLR_BORDER)
        frame.pack(fill="x", padx=10, pady=(0, 4))
        return frame

    def _row(self, parent, label: str, widget_fn):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=12),
                     text_color=self.CLR_TEXT, width=135, anchor="w").pack(side="left")
        w = widget_fn(row)
        w.pack(side="right")
        return w

    def _make_entry(self, parent, var, width=90):
        return ctk.CTkEntry(parent, textvariable=var, width=width,
                            font=ctk.CTkFont(size=12),
                            fg_color=self.CLR_INPUT_BG,
                            border_color=self.CLR_BORDER,
                            text_color=self.CLR_TEXT)

    def _build_left_panel(self, parent: ctk.CTkFrame) -> None:
        ctk.CTkLabel(
            parent, text="Configuration",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=self.CLR_TEXT,
        ).pack(anchor="w", padx=16, pady=(12, 0))

        # Fyers Credentials — App ID, Secret Key, FY ID, TOTP Key, PIN
        fc = self._section(parent, "FYERS CREDENTIALS")
        saved = self._load_credentials()
        self.app_id_var = ctk.StringVar(value=saved.get("app_id", ""))
        self.secret_key_var = ctk.StringVar(value=saved.get("secret_key", ""))
        self.fy_id_var = ctk.StringVar(value=saved.get("fy_id", ""))
        self.totp_key_var = ctk.StringVar(value=saved.get("totp_key", ""))
        self.pin_var = ctk.StringVar(value=saved.get("pin", ""))
        self._row(fc, "App ID", lambda p: ctk.CTkEntry(
            p, textvariable=self.app_id_var, width=150,
            font=ctk.CTkFont(size=12), fg_color=self.CLR_INPUT_BG,
            border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            placeholder_text="e.g. XXXXXX-200"
        ))
        self._row(fc, "Secret Key", lambda p: ctk.CTkEntry(
            p, textvariable=self.secret_key_var, width=150,
            font=ctk.CTkFont(size=12), fg_color=self.CLR_INPUT_BG,
            border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            show="*", placeholder_text="your secret key"
        ))
        self._row(fc, "Fyers ID", lambda p: ctk.CTkEntry(
            p, textvariable=self.fy_id_var, width=150,
            font=ctk.CTkFont(size=12), fg_color=self.CLR_INPUT_BG,
            border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            placeholder_text="e.g. DP02418"
        ))
        self._row(fc, "TOTP Secret", lambda p: ctk.CTkEntry(
            p, textvariable=self.totp_key_var, width=150,
            font=ctk.CTkFont(size=12), fg_color=self.CLR_INPUT_BG,
            border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            show="*", placeholder_text="TOTP secret key"
        ))
        self._row(fc, "PIN", lambda p: ctk.CTkEntry(
            p, textvariable=self.pin_var, width=150,
            font=ctk.CTkFont(size=12), fg_color=self.CLR_INPUT_BG,
            border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            show="*", placeholder_text="4-digit PIN"
        ))
        save_row = ctk.CTkFrame(fc, fg_color="transparent")
        save_row.pack(fill="x", padx=10, pady=(2, 6))
        self.creds_status = ctk.CTkLabel(
            save_row, text="", font=ctk.CTkFont(size=10),
            text_color=self.CLR_GREEN
        )
        self.creds_status.pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            save_row, text="Save", width=60, height=26,
            font=ctk.CTkFont(size=11), corner_radius=6,
            fg_color=self.CLR_ACCENT, hover_color=self.CLR_ACCENT_L,
            text_color="#ffffff",
            command=self._save_creds_clicked,
        ).pack(side="right")

        # Trading Mode — Paper only (Live disabled for client)
        f = self._section(parent, "TRADING MODE")
        mode_row = ctk.CTkFrame(f, fg_color="transparent")
        mode_row.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(mode_row, text="Mode", font=ctk.CTkFont(size=12),
                     text_color=self.CLR_TEXT, width=135, anchor="w").pack(side="left")
        ctk.CTkLabel(mode_row, text="Paper Trading",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=self.CLR_ACCENT).pack(side="right")

        # Instrument configs — one section per instrument
        self.inst_vars: Dict[str, Dict[str, ctk.StringVar]] = {}

        instruments = [
            ("NIFTY", "50", "200", "1", True),
            ("BANKNIFTY", "100", "300", "1", True),
            ("SENSEX", "100", "500", "1", False),
            ("BANKEX", "100", "300", "1", False),
            ("FINNIFTY", "50", "200", "1", False),
        ]

        inst_section = self._section(parent, "INSTRUMENTS")

        for inst_name, step, offset, lots, enabled in instruments:
            inst_frame = ctk.CTkFrame(inst_section, fg_color="transparent")
            inst_frame.pack(fill="x", padx=8, pady=3)

            vars_dict = {}

            # Enable checkbox + name
            vars_dict["enabled"] = ctk.StringVar(value="1" if enabled else "0")
            cb = ctk.CTkCheckBox(
                inst_frame, text=inst_name,
                variable=vars_dict["enabled"],
                onvalue="1", offvalue="0",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=self.CLR_TEXT,
                fg_color=self.CLR_ACCENT,
                hover_color=self.CLR_ACCENT_L,
                width=110,
            )
            cb.pack(side="left")

            # OTM Offset
            vars_dict["offset"] = ctk.StringVar(value=offset)
            ctk.CTkLabel(inst_frame, text="OTM:", font=ctk.CTkFont(size=10),
                         text_color=self.CLR_MUTED).pack(side="left", padx=(4, 0))
            ctk.CTkEntry(
                inst_frame, textvariable=vars_dict["offset"], width=50,
                font=ctk.CTkFont(size=11), fg_color=self.CLR_INPUT_BG,
                border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            ).pack(side="left", padx=2)

            # Lots
            vars_dict["lots"] = ctk.StringVar(value=lots)
            ctk.CTkLabel(inst_frame, text="Lots:", font=ctk.CTkFont(size=10),
                         text_color=self.CLR_MUTED).pack(side="left", padx=(4, 0))
            ctk.CTkEntry(
                inst_frame, textvariable=vars_dict["lots"], width=40,
                font=ctk.CTkFont(size=11), fg_color=self.CLR_INPUT_BG,
                border_color=self.CLR_BORDER, text_color=self.CLR_TEXT,
            ).pack(side="left", padx=2)

            vars_dict["step"] = step
            self.inst_vars[inst_name] = vars_dict

        # EMA Period
        ema_section = self._section(parent, "INDICATOR")
        self.ema_var = ctk.StringVar(value="200")
        self._row(ema_section, "EMA Period", lambda p: self._make_entry(p, self.ema_var, 70))

        # P&L Display
        pnl_section = self._section(parent, "TODAY'S P&L")
        pnl_card = ctk.CTkFrame(pnl_section, fg_color="transparent")
        pnl_card.pack(fill="x", padx=10, pady=8)

        self.pnl_label = ctk.CTkLabel(
            pnl_card, text="₹ 0.00",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
            text_color=self.CLR_TEXT,
        )
        self.pnl_label.pack()

        self.pnl_mode_label = ctk.CTkLabel(
            pnl_card, text="",
            font=ctk.CTkFont(size=10), text_color=self.CLR_MUTED,
        )
        self.pnl_mode_label.pack()

        # Status
        self.status_label = ctk.CTkLabel(
            parent, text="  STOPPED",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=self.CLR_RED,
        )
        self.status_label.pack(pady=(10, 4))

        # Buttons
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=4)

        self.start_btn = ctk.CTkButton(
            btn_frame, text="▶  START STRATEGY",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=self.CLR_ACCENT, hover_color=self.CLR_ACCENT_L,
            text_color="#ffffff", height=44, corner_radius=8,
            command=self._start,
        )
        self.start_btn.pack(fill="x", pady=(0, 6))

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="■  STOP",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=self.CLR_RED, hover_color="#b91c1c",
            text_color="#ffffff", height=44, corner_radius=8,
            state="disabled", command=self._stop,
        )
        self.stop_btn.pack(fill="x", pady=(0, 6))

    def _build_right_panel(self, parent: ctk.CTkFrame) -> None:
        # Tab view
        self.tabs = ctk.CTkTabview(
            parent, fg_color=self.CLR_PANEL,
            segmented_button_fg_color=self.CLR_CARD,
            segmented_button_selected_color=self.CLR_ACCENT,
            segmented_button_selected_hover_color=self.CLR_ACCENT_L,
            segmented_button_unselected_color=self.CLR_CARD,
            segmented_button_unselected_hover_color=self.CLR_BORDER,
            text_color=self.CLR_TEXT,
            corner_radius=8,
        )
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)

        # Instrument Status Tab
        status_tab = self.tabs.add("Instrument Status")
        self._build_status_tab(status_tab)

        # Log Tab
        log_tab = self.tabs.add("Live Log")
        self._build_log_tab(log_tab)

    def _build_status_tab(self, parent: ctk.CTkFrame) -> None:
        """Build instrument status cards"""
        self.status_cards: Dict[str, Dict[str, ctk.CTkLabel]] = {}

        scroll = ctk.CTkScrollableFrame(parent, fg_color=self.CLR_PANEL)
        scroll.pack(fill="both", expand=True)
        self.status_scroll = scroll

        # Headers
        hdr = ctk.CTkFrame(scroll, fg_color=self.CLR_ACCENT, corner_radius=6, height=36)
        hdr.pack(fill="x", padx=4, pady=(4, 2))
        hdr.pack_propagate(False)

        headers = ["Instrument", "OR High", "OR Low", "Direction", "Option",
                    "Entry", "EMA 200", "Status", "P&L"]
        widths = [80, 65, 65, 65, 110, 60, 65, 80, 70]

        for text, w in zip(headers, widths):
            ctk.CTkLabel(
                hdr, text=text, width=w,
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color="#ffffff", anchor="center",
            ).pack(side="left", padx=1)

        # Rows for each instrument
        inst_names = ["NIFTY", "BANKNIFTY", "SENSEX", "BANKEX", "FINNIFTY"]
        for i, name in enumerate(inst_names):
            bg = self.CLR_CARD if i % 2 == 0 else self.CLR_PANEL
            row_frame = ctk.CTkFrame(scroll, fg_color=bg, corner_radius=4, height=32)
            row_frame.pack(fill="x", padx=4, pady=1)
            row_frame.pack_propagate(False)

            labels = {}
            fields = ["name", "or_high", "or_low", "direction", "option",
                       "entry", "ema", "status", "pnl"]

            for field, w in zip(fields, widths):
                val = name if field == "name" else "—"
                color = self.CLR_TEXT if field == "name" else self.CLR_MUTED
                lbl = ctk.CTkLabel(
                    row_frame, text=val, width=w,
                    font=ctk.CTkFont(size=11, weight="bold" if field == "name" else "normal"),
                    text_color=color, anchor="center",
                )
                lbl.pack(side="left", padx=1)
                labels[field] = lbl

            self.status_cards[name] = labels

    def _build_log_tab(self, parent: ctk.CTkFrame) -> None:
        log_frame = ctk.CTkFrame(parent, fg_color=self.CLR_LOG_BG, corner_radius=8)
        log_frame.pack(fill="both", expand=True, padx=4, pady=4)

        self.log_text = ctk.CTkTextbox(
            log_frame, wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=self.CLR_LOG_BG,
            text_color=self.CLR_TEXT,
            border_width=0,
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        # Clear button
        ctk.CTkButton(
            log_frame, text="Clear Log", width=80, height=28,
            font=ctk.CTkFont(size=11),
            fg_color=self.CLR_BORDER, hover_color=self.CLR_MUTED,
            text_color=self.CLR_TEXT,
            corner_radius=6, command=self._clear_log,
        ).pack(side="right", padx=8, pady=4)

    def _save_creds_clicked(self) -> None:
        app_id = self.app_id_var.get().strip()
        secret = self.secret_key_var.get().strip()
        fy_id = self.fy_id_var.get().strip()
        totp_key = self.totp_key_var.get().strip()
        pin = self.pin_var.get().strip()
        if not all([app_id, secret, fy_id, totp_key, pin]):
            self.creds_status.configure(text="All fields required", text_color=self.CLR_RED)
            return
        self._save_credentials(app_id, secret, fy_id, totp_key, pin)
        self.creds_status.configure(text="Saved ✓", text_color=self.CLR_GREEN)

    def _apply_credentials(self) -> None:
        """Override fyers_connect and fyers_token credentials at runtime from saved file."""
        saved = self._load_credentials()
        fy_id = saved.get("fy_id", "").strip()
        pin = saved.get("pin", "").strip()
        totp_key = saved.get("totp_key", "").strip()
        app_id_full = saved.get("app_id", "").strip()
        secret = saved.get("secret_key", "").strip()
        if not app_id_full or not secret:
            self.log_queue.put("[CREDS] App ID or Secret Key empty — skipping patch")
            return
        # Parse APP_ID and APP_TYPE from "XXXX-200" format
        if "-" in app_id_full:
            app_id, app_type = app_id_full.rsplit("-", 1)
        else:
            app_id, app_type = app_id_full, "200"
        client_id = f"{app_id}-{app_type}"
        self.log_queue.put(f"[CREDS] Patching: client_id={client_id}, fy_id={fy_id}, pin={'set' if pin else 'EMPTY'}")

        # Build the patch dict
        patch = {
            "APP_ID": app_id, "APP_TYPE": app_type, "SECRET_KEY": secret,
            "CLIENT_ID": client_id,
        }
        if fy_id:
            patch["FY_ID"] = fy_id
        if pin:
            patch["PIN"] = pin
        if totp_key:
            patch["TOTP_KEY"] = totp_key

        # Patch module objects (works when running as separate files)
        for mod_name in ("fyers_connect", "fyers_token", "strategy"):
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                for k, v in patch.items():
                    if hasattr(mod, k):
                        setattr(mod, k, v)
                self.log_queue.put(f"[CREDS] {mod_name} patched OK")
            except ImportError:
                self.log_queue.put(f"[CREDS] {mod_name} not a separate module (bundled mode)")

        # Also patch globals directly (works in bundled single-file mode)
        import sys
        main_mod = sys.modules.get("__main__")
        if main_mod:
            for k, v in patch.items():
                if hasattr(main_mod, k):
                    setattr(main_mod, k, v)
            self.log_queue.put(f"[CREDS] __main__ globals patched: CLIENT_ID={getattr(main_mod, 'CLIENT_ID', '?')}")

    # ── Actions ────────────────────────────────────────────────────────────
    def _start(self) -> None:
        try:
            if self.runner and self.runner.is_alive():
                self.log_queue.put("[GUI] Strategy already running.")
                return

            self.log_queue.put("[GUI] Start button clicked.")
            # Switch to log tab first so user sees all messages
            self.tabs.set("Live Log")

            # Validate credentials exist
            saved = self._load_credentials()
            self.log_queue.put(f"[GUI] Credentials loaded: app_id={'set' if saved.get('app_id') else 'EMPTY'}, "
                              f"secret={'set' if saved.get('secret_key') else 'EMPTY'}, "
                              f"fy_id={'set' if saved.get('fy_id') else 'EMPTY'}, "
                              f"totp={'set' if saved.get('totp_key') else 'EMPTY'}, "
                              f"pin={'set' if saved.get('pin') else 'EMPTY'}")

            if not saved.get("app_id") or not saved.get("secret_key"):
                self.log_queue.put("[ERROR] Please enter and SAVE your Fyers credentials first.")
                return
            if not saved.get("fy_id") or not saved.get("totp_key") or not saved.get("pin"):
                self.log_queue.put("[ERROR] All credential fields (App ID, Secret Key, Fyers ID, TOTP Secret, PIN) must be saved.")
                return

            self.log_queue.put("[GUI] Applying credentials...")
            self._apply_credentials()
            self._save_settings()

            self.log_queue.put("[GUI] Building config...")
            cfg = self._build_config()

            enabled = [k for k, v in cfg.instruments.items() if v.enabled]
            self.log_queue.put(f"[GUI] Enabled instruments: {enabled}")
            self.log_queue.put(f"[GUI] Paper trading: {cfg.paper_trading}")

            self.log_queue.put("[GUI] Launching strategy thread...")
            self.runner = StrategyRunner(
                cfg=cfg,
                log_queue=self.log_queue,
                status_callback=self._update_status,
                pnl_callback=self._update_pnl,
            )
            self.runner.start()
            self._update_status("RUNNING")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.log_queue.put("[GUI] Strategy thread started successfully.")
        except Exception as e:
            import traceback
            self.log_queue.put(f"[GUI ERROR] _start failed: {e}")
            self.log_queue.put(traceback.format_exc())

    def _stop(self) -> None:
        if self.runner:
            self.runner.stop()
        self.stop_btn.configure(state="disabled")

    def _build_config(self) -> StrategyConfig:
        instruments = {}
        step_map = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "BANKEX": 100, "FINNIFTY": 50}
        futures_map = {
            "NIFTY": "NSE:NIFTY50-INDEX",
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "SENSEX": "BSE:SENSEX-INDEX",
            "BANKEX": "BSE:BANKEX-INDEX",
            "FINNIFTY": "NSE:FINNIFTY-INDEX",
        }

        for name, vars_dict in self.inst_vars.items():
            enabled = vars_dict["enabled"].get() == "1"
            try:
                offset = int(vars_dict["offset"].get())
            except ValueError:
                offset = 200
            try:
                lots = int(vars_dict["lots"].get())
            except ValueError:
                lots = 1

            instruments[name] = InstrumentConfig(
                name=name,
                futures_symbol=futures_map.get(name, ""),
                strike_step=step_map.get(name, 50),
                otm_offset=offset,
                lots=lots,
                enabled=enabled,
            )

        try:
            ema_period = int(self.ema_var.get())
        except ValueError:
            ema_period = 200

        return StrategyConfig(
            paper_trading=True,  # Paper mode only — Live disabled
            instruments=instruments,
            ema_period=ema_period,
        )

    # ── Polling ────────────────────────────────────────────────────────────
    def _poll_log(self) -> None:
        max_lines = 50
        lines_this_tick = 0
        while not self.log_queue.empty() and lines_this_tick < max_lines:
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_text.insert("end", f"[{ts}] {msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                lines_this_tick += 1
            except Exception:
                break

        # Check if runner thread has died — reset buttons
        if self.runner and not self.runner.is_alive() and self._status == "RUNNING":
            self._update_status("STOPPED")

        self.root.after(200, self._poll_log)

    def _poll_pnl(self) -> None:
        """Update instrument status cards from engine state"""
        if self.runner and self.runner.engine:
            try:
                pnl_data = self.runner.engine.get_live_pnl()
                self._day_pnl = pnl_data.get("day_pnl", 0.0)

                # Update P&L label
                color = self.CLR_GREEN if self._day_pnl >= 0 else self.CLR_RED
                self.pnl_label.configure(
                    text=f"₹ {self._day_pnl:,.0f}",
                    text_color=color,
                )
                mode = "Paper"
                self.pnl_mode_label.configure(text=f"{mode} Trading")

                # Update instrument cards
                for name, info in pnl_data.get("instruments", {}).items():
                    if name not in self.status_cards:
                        continue
                    cards = self.status_cards[name]

                    # OR High/Low
                    if info["or_high"] is not None:
                        cards["or_high"].configure(
                            text=f"{info['or_high']:.0f}",
                            text_color=self.CLR_TEXT,
                        )
                        cards["or_low"].configure(
                            text=f"{info['or_low']:.0f}",
                            text_color=self.CLR_TEXT,
                        )

                    # Direction
                    if info["direction"]:
                        dir_color = self.CLR_GREEN if info["direction"] == "BULLISH" else self.CLR_RED
                        cards["direction"].configure(
                            text=info["direction"][:4],
                            text_color=dir_color,
                        )

                    # Option symbol
                    if info["option_symbol"]:
                        sym_short = info["option_symbol"].split(":")[-1][:15]
                        cards["option"].configure(text=sym_short, text_color=self.CLR_TEXT)

                    # Entry
                    if info["entry_price"]:
                        cards["entry"].configure(
                            text=f"{info['entry_price']:.1f}",
                            text_color=self.CLR_TEXT,
                        )

                    # EMA
                    if info["ema_200"]:
                        cards["ema"].configure(
                            text=f"{info['ema_200']:.1f}",
                            text_color=self.CLR_ACCENT,
                        )

                    # Status
                    if info["in_trade"]:
                        cards["status"].configure(text="IN TRADE", text_color=self.CLR_GREEN)
                    elif info["awaiting_reentry"]:
                        cards["status"].configure(text="WAIT RE", text_color="#d97706")
                    elif info["breakout"]:
                        cards["status"].configure(text="EXITED", text_color=self.CLR_MUTED)
                    else:
                        cards["status"].configure(text="Watching", text_color=self.CLR_MUTED)

                    # P&L
                    pnl = info.get("live_pnl", 0)
                    pnl_color = self.CLR_GREEN if pnl >= 0 else self.CLR_RED
                    cards["pnl"].configure(
                        text=f"₹{pnl:,.0f}" if pnl != 0 else "—",
                        text_color=pnl_color if pnl != 0 else self.CLR_MUTED,
                    )

            except Exception:
                pass

        self.root.after(1000, self._poll_pnl)

    def _update_status(self, status: str) -> None:
        self._status = status
        if status == "RUNNING":
            self.status_label.configure(text="  ● RUNNING", text_color=self.CLR_GREEN)
        else:
            self.status_label.configure(text="  ■ STOPPED", text_color=self.CLR_RED)
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

    def _update_pnl(self, pnl: float) -> None:
        self._day_pnl = pnl

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ── Settings Persistence ───────────────────────────────────────────────
    def _save_settings(self) -> None:
        data = {
            "ema_period": self.ema_var.get(),
            "instruments": {},
        }
        for name, vars_dict in self.inst_vars.items():
            data["instruments"][name] = {
                "enabled": vars_dict["enabled"].get(),
                "offset": vars_dict["offset"].get(),
                "lots": vars_dict["lots"].get(),
            }
        try:
            with open(self.SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_settings(self) -> None:
        if not os.path.exists(self.SETTINGS_FILE):
            return
        try:
            with open(self.SETTINGS_FILE, "r") as f:
                data = json.load(f)
            self.ema_var.set(data.get("ema_period", "200"))
            for name, vals in data.get("instruments", {}).items():
                if name in self.inst_vars:
                    self.inst_vars[name]["enabled"].set(vals.get("enabled", "0"))
                    self.inst_vars[name]["offset"].set(vals.get("offset", "200"))
                    self.inst_vars[name]["lots"].set(vals.get("lots", "1"))
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_settings()
        if self.runner and self.runner.is_alive():
            self.runner.stop()
            time.sleep(0.5)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ============================================================================
# ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    app = StrategyGUI()
    app.run()
