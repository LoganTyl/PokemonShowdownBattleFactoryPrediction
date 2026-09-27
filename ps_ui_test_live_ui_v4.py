import asyncio
import json
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Dict, List, Optional, Tuple

import pyodbc
import websockets

from live_ingest_v2 import LiveBattleIngestor
from live_turn_pipeline import CONN_STR as DB_CONN_STR
import time

WS_URL = "wss://sim3.psim.us/showdown/websocket"


def extract_battle_id(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    m = re.search(r"(battle-[A-Za-z0-9-]+)", url)
    if not m:
        return None
    battle_id = m.group(1)
    if not re.fullmatch(r"battle-(?:[a-z0-9]+-)*gen9battlefactory-[a-z0-9]+", battle_id, flags=re.IGNORECASE):
        return None
    return battle_id


async def connect_and_join_room(
    battle_id: str,
    on_connected,
    on_disconnected,
    stop_event,
    on_join_failed=None,
    on_battle_over=None,
    on_players=None,
    on_battle_line=None,
    join_timeout_sec: float = 8.0,
) -> None:
    def fail(msg: str) -> None:
        if on_join_failed:
            on_join_failed(msg)

    connected = False
    current_room = None
    battle_over_notified = False
    p1_name = None
    p2_name = None
    players_notified = False
    ever_connected = False

    def maybe_notify_players():
        nonlocal players_notified
        if players_notified:
            return
        if p1_name and p2_name and on_players:
            players_notified = True
            on_players(battle_id, p1_name, p2_name)

    async def handle_battle_over(line: str):
        nonlocal battle_over_notified
        if battle_over_notified:
            return
        if line.startswith("|win|"):
            winner = line[len("|win|"):].strip()
            battle_over_notified = True
            if on_battle_over:
                on_battle_over(battle_id, winner if winner else None)
        elif line.startswith("|tie|"):
            battle_over_notified = True
            if on_battle_over:
                on_battle_over(battle_id, None)

    def try_parse_players(line: str):
        nonlocal p1_name, p2_name
        if not line.startswith("|player|"):
            return
        parts = line.split("|")
        if len(parts) >= 4:
            slot = parts[2].strip().lower()
            name = parts[3].strip()
            if slot == "p1" and name:
                p1_name = name
            elif slot == "p2" and name:
                p2_name = name

    try:
        while not stop_event.is_set():
            current_room = None
            connected = False

            try:
                async with websockets.connect(
                    WS_URL,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(f"|/j {battle_id}")
                    deadline = asyncio.get_event_loop().time() + join_timeout_sec

                    # Join/init phase
                    while not stop_event.is_set() and not connected:
                        if asyncio.get_event_loop().time() > deadline:
                            fail("Room not available (no init received). It may be expired or already ended.")
                            return
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            print("[WS RAW]", str(msg)[:200])
                        except asyncio.TimeoutError:
                            continue

                        for raw_line in str(msg).split("\n"):
                            line = raw_line.strip()
                            if not line:
                                continue
                            if line.startswith(">"):
                                current_room = line[1:].strip()
                                continue

                            in_target = current_room == battle_id
                            if line.startswith("|noinit|"):
                                noinit_room = line[len("|noinit|"):].strip()
                                if noinit_room == battle_id:
                                    fail("Room not available (noinit). It may be expired or already ended.")
                                    return
                            if not in_target:
                                continue

                            if on_battle_line:
                                try:
                                    on_battle_line(battle_id, line)
                                except Exception as e:
                                    import traceback
                                    print("[on_battle_line] callback failed:", e)
                                    traceback.print_exc()

                            try_parse_players(line)
                            maybe_notify_players()

                            if line == "|init|battle":
                                connected = True
                                if not ever_connected:
                                    on_connected()
                                    ever_connected = True
                                continue

                            await handle_battle_over(line)

                            if line.startswith("|expire|") or line.startswith("|deinit|"):
                                fail("Room expired or closed.")
                                return
                            if line.lower().startswith("|popup|") and "expired" in line.lower():
                                fail("Room expired or closed.")
                                return

                    # Live streaming phase
                    while connected and not stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            print("[WS RAW]", str(msg)[:200])
                        except asyncio.TimeoutError:
                            continue

                        for raw_line in str(msg).split("\n"):
                            line = raw_line.strip()
                            if not line:
                                continue
                            if line.startswith(">"):
                                current_room = line[1:].strip()
                                continue
                            if current_room != battle_id:
                                continue

                            if on_battle_line:
                                try:
                                    on_battle_line(battle_id, line)
                                except Exception as e:
                                    import traceback
                                    print("[on_battle_line] callback failed:", e)
                                    traceback.print_exc()

                            try_parse_players(line)
                            maybe_notify_players()
                            await handle_battle_over(line)

                            if line.startswith("|expire|") or line.startswith("|deinit|"):
                                return
                            if line.lower().startswith("|popup|") and "expired" in line.lower():
                                return

                # If the websocket context exits normally, stop trying.
                break

            except (websockets.exceptions.ConnectionClosedError, OSError) as e:
                print(f"[connect_and_join_room] websocket dropped: {e}")
                if stop_event.is_set():
                    break
                # brief pause before reconnecting
                await asyncio.sleep(2.0)
                continue

            except Exception as e:
                import traceback
                print("[connect_and_join_room] failed:", e)
                traceback.print_exc()
                if stop_event.is_set():
                    break
                await asyncio.sleep(2.0)
                continue

    finally:
        on_disconnected()


def add_right_click_menu_for_entry(entry: ttk.Entry) -> None:
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Cut", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Paste", command=lambda: entry.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: entry.select_range(0, "end"))

    def show_menu(event):
        try:
            entry.selection_get()
            has_selection = True
        except tk.TclError:
            has_selection = False
        menu.entryconfig("Cut", state=("normal" if has_selection else "disabled"))
        menu.entryconfig("Copy", state=("normal" if has_selection else "disabled"))
        try:
            entry.clipboard_get()
            has_clipboard = True
        except tk.TclError:
            has_clipboard = False
        menu.entryconfig("Paste", state=("normal" if has_clipboard else "disabled"))
        menu.tk_popup(event.x_root, event.y_root)

    entry.bind("<Button-3>", show_menu)
    entry.bind("<Control-Button-1>", show_menu)


def to_id(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def combine_action_rankings(action_type_pred: Dict[str, Any], next_move_pred: Dict[str, Any], next_switch_pred: Dict[str, Any], top_n: int = 5) -> List[Dict[str, Any]]:
    p_move = 0.0
    p_switch = 0.0
    for item in action_type_pred.get("topK", []):
        lab = str(item.get("label", "")).lower()
        if lab == "move":
            p_move = float(item.get("prob", 0.0))
        elif lab == "switch":
            p_switch = float(item.get("prob", 0.0))

    combined: List[Dict[str, Any]] = []
    for item in next_move_pred.get("topK", []):
        prob = float(item.get("prob", 0.0))
        combined.append({
            "kind": "move",
            "label": str(item.get("label", "")),
            "score": p_move * prob,
            "p_type": p_move,
            "p_choice": prob,
        })
    for item in next_switch_pred.get("topK", []):
        prob = float(item.get("prob", 0.0))
        combined.append({
            "kind": "switch",
            "label": str(item.get("label", "")),
            "score": p_switch * prob,
            "p_type": p_switch,
            "p_choice": prob,
        })
    combined.sort(key=lambda x: x["score"], reverse=True)
    return combined[:top_n]


class BattleConnectUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pokémon Showdown FutureSight")
        self.geometry("1080x860")
        self.resizable(True, True)

        self.current_battle_id: Optional[str] = None
        self.predict_player: Optional[str] = None
        self._predict_dialog_open = False
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Autofollow / selection tracking
        self._user_selected_team_row = False
        self._suppress_team_select_event = False
        self._last_user_select_ts = 0.0
        self._last_user_selected_bp_id: Optional[int] = None
        self._last_programmatic_select_ts = 0.0
        self._last_programmatic_selected_bp_id: Optional[int] = None
        self._last_seen_turn_for_predict_side: Optional[int] = None
        self.ingestor = LiveBattleIngestor(
            conn_str=DB_CONN_STR,
            run_turn_pipeline_after_checkpoints=True,
            run_predictions_after_checkpoints=True,
        )
        self.ingestor.predict_for_player_name = None

        self._db_conn: Optional[pyodbc.Connection] = None
        self._db_cur: Optional[pyodbc.Cursor] = None
        self._ui_log_q: queue.Queue[str] = queue.Queue()
        self._selected_bp_id: Optional[int] = None
        self._refresh_interval_ms = 1000
        self._last_refresh_ts = 0.0

        self._build_ui()
        self._set_status(False)
        self.bind("<Return>", lambda _e: self.on_connect_clicked())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(self._refresh_interval_ms, self._refresh_tick)

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x")

        info = ttk.Frame(root)
        info.pack(fill="x", pady=(8, 0))

        self.connected_battle_var = tk.StringVar(value="")
        self.predicting_var = tk.StringVar(value="")
        self.turn_var = tk.StringVar(value="")

        ttk.Label(info, textvariable=self.connected_battle_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.predicting_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.turn_var).pack(anchor="w")

        ttk.Label(top, text="Battle URL").pack(side="left")
        self.url_var = tk.StringVar(value="https://play.pokemonshowdown.com/battle-gen9battlefactory-...")
        self.url_entry = ttk.Entry(top, textvariable=self.url_var, width=72)
        self.url_entry.pack(side="left", padx=(10, 12), fill="x", expand=True)
        add_right_click_menu_for_entry(self.url_entry)

        status_frame = ttk.Frame(top)
        status_frame.pack(side="right")
        self.dot = tk.Canvas(status_frame, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_oval = self.dot.create_oval(2, 2, 12, 12, outline="", fill="#9aa0a6")
        self.status_label = ttk.Label(status_frame, text="Not connected")
        self.status_label.pack(side="left")

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(12, 0))
        self.connect_btn = ttk.Button(buttons, text="Connect", command=self.on_connect_clicked)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(buttons, text="Disconnect", command=self.on_disconnect_clicked)
        self.disconnect_btn.pack(side="left", padx=(10, 0))
        self.refresh_btn = ttk.Button(buttons, text="Refresh", command=self.refresh_now)
        self.refresh_btn.pack(side="left", padx=(10, 0))
        self.help_btn = ttk.Button(buttons, text="Column Help", command=self.show_column_help)
        self.help_btn.pack(side="left", padx=(10, 0))

        main_pane = ttk.Panedwindow(root, orient="horizontal")
        main_pane.pack(fill="both", expand=True, pady=(12, 0))

        left = ttk.Frame(main_pane)
        right = ttk.Frame(main_pane)
        main_pane.add(left, weight=2)
        main_pane.add(right, weight=3)

        left_pane = ttk.Panedwindow(left, orient="vertical")
        left_pane.pack(fill="both", expand=True)

        teams_frame = ttk.Labelframe(left_pane, text="Teams")
        left_pane.add(teams_frame, weight=1)
        team_cols = [("side", 44, False), ("slot", 44, False), ("species", 140, True), ("bname", 110, True), ("status", 64, False)]
        self.team_tree = ttk.Treeview(
            teams_frame,
            columns=tuple(c for c, _, _ in team_cols),
            show="headings",
            height=8,
        )
        for col, w, stretch in team_cols:
            self.team_tree.heading(col, text=col.upper())
            self.team_tree.column(col, width=w, minwidth=w, anchor="w", stretch=stretch)
        team_y = ttk.Scrollbar(teams_frame, orient="vertical", command=self.team_tree.yview)
        team_x = ttk.Scrollbar(teams_frame, orient="horizontal", command=self.team_tree.xview)
        self.team_tree.configure(yscrollcommand=team_y.set, xscrollcommand=team_x.set)
        self.team_tree.grid(row=0, column=0, sticky="nsew")
        team_y.grid(row=0, column=1, sticky="ns")
        team_x.grid(row=1, column=0, sticky="ew")
        teams_frame.rowconfigure(0, weight=1)
        teams_frame.columnconfigure(0, weight=1)
        self.team_tree.bind("<<TreeviewSelect>>", self._on_team_select)

        cand_frame = ttk.Labelframe(left_pane, text="Set Candidates")
        left_pane.add(cand_frame, weight=3)
        self.cand_tree = ttk.Treeview(
            cand_frame,
            columns=("status", "weight", "species", "item", "ability", "tera", "nature", "evs", "ivs", "move1", "move2", "move3", "move4", "reason"),
            show="headings",
            height=16,
        )
        widths = {
            "status": 58, "weight": 58, "species": 110, "item": 110, "ability": 100,
            "tera": 100, "nature": 86, "evs": 120, "ivs": 120,
            "move1": 110, "move2": 110, "move3": 110, "move4": 110, "reason": 150
        }
        # Use fixed column widths for candidates so horizontal scrollbar
        # behavior remains stable when columns are reordered or resized.
        for col in self.cand_tree["columns"]:
            self.cand_tree.heading(col, text=col.upper())
            self.cand_tree.column(col, width=widths[col], minwidth=56, anchor="w", stretch=False)
        self.cand_tree.tag_configure("eliminated", foreground="#888888")
        cand_y = ttk.Scrollbar(cand_frame, orient="vertical", command=self.cand_tree.yview)
        cand_x = ttk.Scrollbar(cand_frame, orient="horizontal", command=self.cand_tree.xview)
        self.cand_tree.configure(yscrollcommand=cand_y.set, xscrollcommand=cand_x.set)
        self.cand_tree.grid(row=0, column=0, sticky="nsew")
        cand_y.grid(row=0, column=1, sticky="ns")
        cand_x.grid(row=1, column=0, sticky="ew")
        cand_frame.rowconfigure(0, weight=1)
        cand_frame.columnconfigure(0, weight=1)

        right_pane = ttk.Panedwindow(right, orient="vertical")
        right_pane.pack(fill="both", expand=True)

        pred_frame = ttk.Labelframe(right_pane, text="Predictions")
        right_pane.add(pred_frame, weight=2)

        combined_frame = ttk.Labelframe(pred_frame, text="Most likely next actions")
        combined_frame.pack(fill="both", expand=False)
        self.combined_tree = ttk.Treeview(
            combined_frame,
            columns=("rank", "kind", "label", "score", "ptype", "pchoice"),
            show="headings",
            height=6,
        )
        for col, w, stretch in [("rank", 42, False), ("kind", 58, False), ("label", 150, True), ("score", 72, False), ("ptype", 72, False), ("pchoice", 78, False)]:
            self.combined_tree.heading(col, text=col.upper())
            self.combined_tree.column(col, width=w, minwidth=w, anchor="w", stretch=stretch)
        comb_y = ttk.Scrollbar(combined_frame, orient="vertical", command=self.combined_tree.yview)
        comb_x = ttk.Scrollbar(combined_frame, orient="horizontal", command=self.combined_tree.xview)
        self.combined_tree.configure(yscrollcommand=comb_y.set, xscrollcommand=comb_x.set)
        self.combined_tree.grid(row=0, column=0, sticky="nsew")
        comb_y.grid(row=0, column=1, sticky="ns")
        comb_x.grid(row=1, column=0, sticky="ew")
        combined_frame.rowconfigure(0, weight=1)
        combined_frame.columnconfigure(0, weight=1)

        dist_pane = ttk.Panedwindow(pred_frame, orient="horizontal")
        dist_pane.pack(fill="both", expand=True, pady=(10, 0))
        moves_box = ttk.Labelframe(dist_pane, text="Moves distribution")
        switch_box = ttk.Labelframe(dist_pane, text="Switch distribution")
        dist_pane.add(moves_box, weight=3)
        dist_pane.add(switch_box, weight=2)

        self.moves_tree = ttk.Treeview(moves_box, columns=("label", "prob"), show="headings", height=10)
        self.moves_tree.heading("label", text="LABEL")
        self.moves_tree.heading("prob", text="PROB")
        self.moves_tree.column("label", width=150, minwidth=120, anchor="w", stretch=True)
        self.moves_tree.column("prob", width=70, minwidth=60, anchor="w", stretch=False)
        moves_y = ttk.Scrollbar(moves_box, orient="vertical", command=self.moves_tree.yview)
        moves_x = ttk.Scrollbar(moves_box, orient="horizontal", command=self.moves_tree.xview)
        self.moves_tree.configure(yscrollcommand=moves_y.set, xscrollcommand=moves_x.set)
        self.moves_tree.grid(row=0, column=0, sticky="nsew")
        moves_y.grid(row=0, column=1, sticky="ns")
        moves_x.grid(row=1, column=0, sticky="ew")
        moves_box.rowconfigure(0, weight=1)
        moves_box.columnconfigure(0, weight=1)

        self.switch_tree = ttk.Treeview(switch_box, columns=("label", "prob"), show="headings", height=10)
        self.switch_tree.heading("label", text="LABEL")
        self.switch_tree.heading("prob", text="PROB")
        self.switch_tree.column("label", width=140, minwidth=110, anchor="w", stretch=True)
        self.switch_tree.column("prob", width=70, minwidth=60, anchor="w", stretch=False)
        sw_y = ttk.Scrollbar(switch_box, orient="vertical", command=self.switch_tree.yview)
        sw_x = ttk.Scrollbar(switch_box, orient="horizontal", command=self.switch_tree.xview)
        self.switch_tree.configure(yscrollcommand=sw_y.set, xscrollcommand=sw_x.set)
        self.switch_tree.grid(row=0, column=0, sticky="nsew")
        sw_y.grid(row=0, column=1, sticky="ns")
        sw_x.grid(row=1, column=0, sticky="ew")
        switch_box.rowconfigure(0, weight=1)
        switch_box.columnconfigure(0, weight=1)

        log_frame = ttk.Labelframe(right_pane, text="Battle Log")
        right_pane.add(log_frame, weight=1)
        self.log_text = tk.Text(log_frame, height=10, wrap="none")
        log_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_x = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_y.grid(row=0, column=1, sticky="ns")
        log_x.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text.configure(state="disabled")

    def _set_status(self, connected: bool):
        if connected:
            self.dot.itemconfig(self.dot_oval, fill="#16a34a")
            self.status_label.config(text="Connected")
            self.url_entry.state(["disabled"])
        else:
            self._close_db()
            self.dot.itemconfig(self.dot_oval, fill="#9aa0a6")
            self.status_label.config(text="Not connected")
            self.url_entry.state(["!disabled"])
            self.current_battle_id = None
            self.predict_player = None
            self._predict_dialog_open = False
            self.connected_battle_var.set("")
            self.predicting_var.set("")
            self.turn_var.set("")
            self._selected_bp_id = None
            self.ingestor.predict_for_player_name = None
            self._clear_tree(self.team_tree)
            self._clear_tree(self.cand_tree)
            self._clear_tree(self.combined_tree)
            self._clear_tree(self.moves_tree)
            self._clear_tree(self.switch_tree)
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

    def _clear_tree(self, tree: ttk.Treeview):
        for iid in tree.get_children():
            tree.delete(iid)

    def _ui_thread_safe(self, fn):
        self.after(0, fn)

    def _close_db(self):
        try:
            if self._db_cur is not None:
                self._db_cur.close()
        except Exception:
            pass
        try:
            if self._db_conn is not None:
                self._db_conn.close()
        except Exception:
            pass
        self._db_cur = None
        self._db_conn = None


    def _ensure_db(self):
        # If we already have a connection/cursor, make sure they are still usable.
        if self._db_conn is not None and self._db_cur is not None:
            try:
                self._db_cur.execute("SELECT 1;")
                self._db_cur.fetchone()
                return
            except Exception:
                self._close_db()

        # Reopen fresh connection/cursor
        self._db_conn = pyodbc.connect(DB_CONN_STR, autocommit=True)
        self._db_cur = self._db_conn.cursor()


    def _db(self) -> pyodbc.Cursor:
        self._ensure_db()
        assert self._db_cur is not None
        return self._db_cur

    def on_connect_clicked(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return

        battle_id = extract_battle_id(self.url_var.get())
        self.current_battle_id = battle_id
        if not battle_id:
            messagebox.showerror("Invalid URL", "Could not find a battle id in that URL.")
            self._set_status(False)
            return


        self._stop_event.clear()

        def got_players(bid: str, p1: str, p2: str):
            self._ui_thread_safe(lambda: (self.connected_battle_var.set(f"Connected to {bid}"), self.show_predict_player_dialog(bid, p1, p2)))

        def mark_connected():
            def do():
                self._set_status(True)
                if self.current_battle_id:
                    self.connected_battle_var.set(f"Connected to {self.current_battle_id}")
                self.refresh_now()
            self._ui_thread_safe(do)

        def mark_disconnected():
            self._ui_thread_safe(lambda: self._set_status(False))

        def join_failed(msg: str):
            self._ui_thread_safe(lambda: (self._set_status(False), messagebox.showerror("Connect failed", msg)))

        def battle_over(bid: str, winner: str | None):
            def prompt():
                if winner:
                    msg = f"Battle {bid} finished (winner: {winner}).\n\nWould you like to disconnect?"
                else:
                    msg = f"Battle {bid} finished.\n\nWould you like to disconnect?"
                if messagebox.askyesno("Battle finished", msg):
                    self.on_disconnect_clicked()
            self._ui_thread_safe(prompt)

        def _on_battle_line(_bid, line: str):
            try:
                print("[WS LINE]", line[:120])
                self._ui_log_q.put(line)
                bid = _bid.removeprefix("battle-")
                self.ingestor.on_line(bid, line)
            except Exception as e:
                import traceback
                print("[WS LINE] ingestor.on_line crashed:", e)
                traceback.print_exc()

        def worker():
            print("[UI TEST] Starting websocket worker thread...")
            try:
                asyncio.run(connect_and_join_room(
                    battle_id=battle_id,
                    on_connected=mark_connected,
                    on_disconnected=mark_disconnected,
                    on_join_failed=join_failed,
                    on_battle_over=battle_over,
                    on_players=got_players,
                    stop_event=self._stop_event,
                    on_battle_line=_on_battle_line,
                ))
            finally:
                try:
                    self.ingestor.close()
                except Exception:
                    pass

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def on_disconnect_clicked(self):
        self._stop_event.set()
        self._close_db()
        self._set_status(False)

    def on_close(self):
        self._stop_event.set()
        try:
            if self._db_cur is not None:
                self._db_cur.close()
            if self._db_conn is not None:
                self._db_conn.close()
        except Exception:
            pass
        self.destroy()

    def show_predict_player_dialog(self, battle_id: str, p1: str, p2: str):
        if self._predict_dialog_open:
            return
        self._predict_dialog_open = True

        dialog = tk.Toplevel(self)
        dialog.title("Choose player")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 140, self.winfo_rooty() + 120))

        container = ttk.Frame(dialog, padding=14)
        container.pack(fill="both", expand=True)
        msg = f"Connected to battle {battle_id} ({p1} vs {p2}).\nWhich player would you like to predict?"
        ttk.Label(container, text=msg, justify="left", wraplength=380).pack(anchor="w")

        choice_var = tk.StringVar(value=p1)
        radios = ttk.Frame(container)
        radios.pack(fill="x", pady=(10, 0))
        ttk.Radiobutton(radios, text=p1, variable=choice_var, value=p1).pack(anchor="w")
        ttk.Radiobutton(radios, text=p2, variable=choice_var, value=p2).pack(anchor="w")

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(14, 0))

        def confirm():
            self.predict_player = choice_var.get()
            self.predicting_var.set(f"Predicting: {self.predict_player}")
            self.ingestor.predict_for_player_name = self.predict_player
            self._predict_dialog_open = False
            dialog.destroy()
            self.refresh_now()

        def cancel():
            self._predict_dialog_open = False
            dialog.destroy()
            self.on_disconnect_clicked()

        ttk.Button(buttons, text="Confirm", command=confirm).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=(0, 10))

    def show_column_help(self):
        help_text = """Teams
- SIDE: Player side (p1 or p2).
- SLOT: Team-preview slot order.
- SPECIES: Pokémon species for that slot.
- BNAME: In-battle short name once revealed (for example, Giratina for Giratina-Origin).
- STATUS: ACTIVE when that Pokémon is currently on the field.

Set Candidates
- STATUS: OK means still possible, ELIM means ruled out.
- WEIGHT: Relative set weight from the Battle Factory data.
- SPECIES / ITEM / ABILITY / TERA / NATURE: Static set details.
- EVS / IVS: Spread for that exact set.
- MOVE1-MOVE4: Possible move options for each move slot. Multiple moves in one column means that slot can randomize between those options.
- REASON: Why a candidate was eliminated, if applicable.

Predictions
- Most likely next actions combines the action-type model with the move/switch models.
- SCORE = P(action type) × P(specific move or switch | that action type).
- PTYPE = probability of the action type itself (move or switch).
- PCHOICE = conditional probability of that specific move or switch within its model.
- Moves distribution shows all currently allowed move labels.
- Switch distribution shows all currently allowed switch targets.
"""

        win = tk.Toplevel(self)
        win.title("Column Help")
        win.geometry("760x560")
        win.transient(self)
        txt = tk.Text(win, wrap="word")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", help_text)
        txt.configure(state="disabled")

    def _refresh_tick(self):
        try:
            self._refresh_once(force=False)
        finally:
            self.after(self._refresh_interval_ms, self._refresh_tick)

    def refresh_now(self):
        self._refresh_once(force=True)

    def _refresh_once(self, force: bool):
        self._drain_log()
        if not self.current_battle_id:
            return
        if not force and (time.time() - self._last_refresh_ts) < 0.5:
            return
        self._last_refresh_ts = time.time()
        try:
            self._render_roster()
            if self._selected_bp_id is not None:
                self._render_candidates(self._selected_bp_id)
            self._render_predictions()
        except Exception as e:
            print("[UI refresh] failed:", e)
            try:
                self._close_db()
            except Exception:
                pass

    def _drain_log(self):
        lines: List[str] = []
        while True:
            try:
                lines.append(self._ui_log_q.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.configure(state="normal")
        for ln in lines:
            self.log_text.insert("end", ln + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _normalized_battle_id(self) -> Optional[str]:
        if not self.current_battle_id:
            return None
        return self.current_battle_id.removeprefix("battle-")

    def _get_predict_side(self, battle_id_norm: str) -> Optional[str]:
        if not self.predict_player:
            return None
        cur = self._db()
        cur.execute("SELECT Player1Name, Player2Name FROM dbo.Battles WHERE BattleId = ?;", battle_id_norm)
        row = cur.fetchone()
        if not row:
            return None
        p1 = str(row[0]) if row[0] is not None else ""
        p2 = str(row[1]) if row[1] is not None else ""
        if self.predict_player == p1:
            return "p1"
        if self.predict_player == p2:
            return "p2"
        return None

    def _get_latest_active_bpid_for_side(self, battle_id_norm: str, side: str) -> Optional[int]:
        cur = self._db()
        cur.execute("""
            SELECT TOP 1 ActorBattlePokemonId
            FROM dbo.DecisionPoints
            WHERE BattleId = ? AND ActorSide = ?
            ORDER BY LineNum DESC, DecisionPointId DESC;
        """, battle_id_norm, side)
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])

    def _get_active_info(self, battle_id_norm: str) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
        cur = self._db()
        out = {"p1": (None, None), "p2": (None, None)}

        cur.execute("""
            WITH Ranked AS (
                SELECT
                    ActorSide,
                    ActorBattlePokemonId,
                    ActivePokemonSlot,
                    ROW_NUMBER() OVER (
                        PARTITION BY ActorSide
                        ORDER BY LineNum DESC, DecisionPointId DESC
                    ) AS rn
                FROM dbo.DecisionPoints
                WHERE BattleId = ?
                AND ActorSide IN ('p1', 'p2')
            )
            SELECT ActorSide, ActorBattlePokemonId, ActivePokemonSlot
            FROM Ranked
            WHERE rn = 1;
        """, battle_id_norm)

        for r in cur.fetchall():
            side = str(r[0])
            out[side] = (
                int(r[1]) if r[1] is not None else None,
                int(r[2]) if r[2] is not None else None,
            )

        return out

    def _render_roster(self):
        bid = self._normalized_battle_id()
        if not bid:
            return
        # Suppress selection events during render to avoid treating
        # programmatic selection changes as user clicks.
        self._suppress_team_select_event = True
        try:
            cur = self._db()
            active = self._get_active_info(bid)
            predict_side = self._get_predict_side(bid)
            # Check turn number for predict_side so we can resume autofollow at turn start
            turn_num = None
            if predict_side:
                try:
                    cur.execute("""
                        SELECT TOP 1 TurnNumber
                        FROM dbo.DecisionPoints
                        WHERE BattleId = ? AND ActorSide = ?
                        ORDER BY LineNum DESC, DecisionPointId DESC;
                    """, bid, predict_side)
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        turn_num = int(row[0])
                except Exception:
                    turn_num = None
                try:
                    if turn_num is not None and self._last_seen_turn_for_predict_side is not None and turn_num != self._last_seen_turn_for_predict_side:
                        # Turn changed -> resume autofollow
                        self._user_selected_team_row = False
                    if turn_num is not None:
                        self._last_seen_turn_for_predict_side = turn_num
                except Exception:
                    pass
            predicted_active_bpid = self._get_latest_active_bpid_for_side(bid, predict_side) if predict_side else None
            self._clear_tree(self.team_tree)
            cur.execute("""
                SELECT BattlePokemonId, Side, Slot, Species, BattleName
                FROM dbo.BattlePokemon
                WHERE BattleId = ?
                ORDER BY Side, Slot;
            """, bid)
            rows = cur.fetchall()
            first_predict_side_bpid = None
            for bpid, side, slot, species, bname in rows:
                bpid_i = int(bpid)
                side_s = str(side)
                status = ""
                active_bpid, active_slot = active.get(side_s, (None, None))
                if active_bpid is not None and bpid_i == active_bpid:
                    status = "ACTIVE"
                self.team_tree.insert("", "end", iid=str(bpid_i), values=(side_s, int(slot), str(species or ""), str(bname or ""), status))
                if predict_side and side_s == predict_side and first_predict_side_bpid is None:
                    first_predict_side_bpid = bpid_i

            # If the user has manually selected a row, preserve it if still valid.
            if self._user_selected_team_row:
                current_selection = self.team_tree.selection()
                has_valid_selection = False

                if current_selection:
                    try:
                        sel_bpid = int(current_selection[0])
                        if self.team_tree.exists(str(sel_bpid)):
                            has_valid_selection = True
                            self._selected_bp_id = sel_bpid
                    except Exception:
                        has_valid_selection = False

                if not has_valid_selection and self._selected_bp_id is not None:
                    if self.team_tree.exists(str(self._selected_bp_id)):
                        has_valid_selection = True
                        self.team_tree.selection_set(str(self._selected_bp_id))
                        self.team_tree.focus(str(self._selected_bp_id))

                # If the user's old selection disappeared, fall back to auto-follow again
                if not has_valid_selection:
                    self._user_selected_team_row = False

            # Auto-follow opponent active until the user manually clicks something
            if not self._user_selected_team_row:
                target_bpid = predicted_active_bpid or first_predict_side_bpid
                if target_bpid is not None and self.team_tree.exists(str(target_bpid)):
                    # Programmatic selection should not be treated as a user click.
                    self._selected_bp_id = int(target_bpid)
                    # Record programmatic selection so we can ignore selection events
                    # that are triggered as a result of it.
                    try:
                        self._last_programmatic_selected_bp_id = self._selected_bp_id
                        self._last_programmatic_select_ts = time.time()
                    except Exception:
                        pass
                    # Set suppress flag and schedule clearing shortly after to ensure
                    # the selection event handler ignores this programmatic selection.
                    self._suppress_team_select_event = True
                    self.team_tree.selection_set(str(target_bpid))
                    self.team_tree.focus(str(target_bpid))
                    try:
                        self.team_tree.focus_set()
                    except Exception:
                        pass
                    self.team_tree.see(str(target_bpid))
                    try:
                        self.after(100, lambda: setattr(self, '_suppress_team_select_event', False))
                    except Exception:
                        self._suppress_team_select_event = False
        finally:
            # Ensure suppress flag is cleared after render completes.
            try:
                self._suppress_team_select_event = False
            except Exception:
                pass

    def _on_team_select(self, _evt):
        # If this selection event was triggered programmatically for autofollow,
        # ignore it so we don't mark it as a user selection.
        if getattr(self, "_suppress_team_select_event", False):
            return

        sel = self.team_tree.selection()
        if not sel:
            return
        try:
            self._selected_bp_id = int(sel[0])
        except Exception:
            self._selected_bp_id = None
            return
        # If this selection matches the most-recent programmatic selection
        # and it happened recently, ignore it as it's not a real user click.
        try:
            if self._last_programmatic_selected_bp_id == self._selected_bp_id and (time.time() - self._last_programmatic_select_ts) < 0.5:
                return
        except Exception:
            pass
        # Deduplicate rapid identical select events (some selection calls fire twice)
        now = time.time()
        if self._last_user_selected_bp_id == self._selected_bp_id and (now - self._last_user_select_ts) < 0.25:
            return
        self._last_user_selected_bp_id = self._selected_bp_id
        self._last_user_select_ts = now
        # mark as user selection and render candidates
        self._user_selected_team_row = True
        self._render_candidates(self._selected_bp_id)

    def _format_spread(self, row: Dict[str, Any], prefix: str) -> str:
        keys = ["HP", "ATK", "DEF", "SPA", "SPD", "SPE"]
        vals = []
        for k in keys:
            col = f"{prefix}_{k}"
            v = row.get(col)
            if v is None:
                continue
            vals.append(f"{k}:{v}")
        return " / ".join(vals)

    def _render_candidates(self, battle_pokemon_id: int):
        bid = self._normalized_battle_id()
        if not bid:
            return
        cur = self._db()
        self._clear_tree(self.cand_tree)

        cur.execute("""
            SELECT
                SC.BF_SetId,
                SC.IsEliminated,
                SC.EliminatedAtTurn,
                SC.EliminatedLineNum,
                SC.EliminationReason,
                BS.Species,
                BS.SetWeight,
                BS.WantsTera,
                BS.Gender,
                BS.EV_HP, BS.EV_ATK, BS.EV_DEF, BS.EV_SPA, BS.EV_SPD, BS.EV_SPE,
                BS.IV_HP, BS.IV_ATK, BS.IV_DEF, BS.IV_SPA, BS.IV_SPD, BS.IV_SPE
            FROM dbo.SetCandidates SC
            JOIN dbo.BF_Set BS ON BS.SetId = SC.BF_SetId
            WHERE SC.BattlePokemonId = ?
            ORDER BY SC.IsEliminated ASC, BS.SetWeight DESC, SC.BF_SetId ASC;
        """, battle_pokemon_id)
        base_rows = cur.fetchall()

        for r in base_rows:
            set_id = int(r[0])
            is_elim = bool(r[1])
            elim_reason = str(r[4] or "")
            species = str(r[5] or "")
            set_weight = int(r[6]) if r[6] is not None else 0
            row = {
                "EV_HP": r[9], "EV_ATK": r[10], "EV_DEF": r[11], "EV_SPA": r[12], "EV_SPD": r[13], "EV_SPE": r[14],
                "IV_HP": r[15], "IV_ATK": r[16], "IV_DEF": r[17], "IV_SPA": r[18], "IV_SPD": r[19], "IV_SPE": r[20],
            }
            evs = self._format_spread(row, "EV")
            ivs = self._format_spread(row, "IV")

            cur.execute("SELECT ItemName FROM dbo.BF_SetItem WHERE SetId = ? ORDER BY ItemName;", set_id)
            items = [str(x[0]) for x in cur.fetchall()]
            cur.execute("SELECT AbilityName FROM dbo.BF_SetAbility WHERE SetId = ? ORDER BY AbilityName;", set_id)
            abilities = [str(x[0]) for x in cur.fetchall()]
            cur.execute("SELECT NatureName FROM dbo.BF_SetNature WHERE SetId = ? ORDER BY NatureName;", set_id)
            natures = [str(x[0]) for x in cur.fetchall()]
            cur.execute("SELECT TeraTypeName FROM dbo.BF_SetTeraType WHERE SetId = ? ORDER BY TeraTypeName;", set_id)
            tera_types = [str(x[0]) for x in cur.fetchall()]
            cur.execute("SELECT MoveSlot, MoveName FROM dbo.BF_SetMoveOption WHERE SetId = ? ORDER BY MoveSlot, MoveName;", set_id)
            move_rows = cur.fetchall()
            moves_by_slot: Dict[int, List[str]] = {}
            for ms, mn in move_rows:
                moves_by_slot.setdefault(int(ms), []).append(str(mn))
            move_cols: List[str] = []
            for move_slot in range(1, 5):
                opts = sorted(set(moves_by_slot.get(move_slot, [])))
                move_cols.append(" / ".join(opts))

            values = (
                "ELIM" if is_elim else "OK",
                set_weight,
                species,
                " / ".join(items),
                " / ".join(abilities),
                " / ".join(tera_types),
                " / ".join(natures),
                evs,
                ivs,
                move_cols[0],
                move_cols[1],
                move_cols[2],
                move_cols[3],
                elim_reason,
            )
            tags = ("eliminated",) if is_elim else ()
            self.cand_tree.insert("", "end", values=values, tags=tags)

    def _fetch_latest_prediction_payloads(self, battle_id_norm: str) -> Dict[str, Dict[str, Any]]:
        cur = self._db()
        predict_side = self._get_predict_side(battle_id_norm)
        if not predict_side:
            return {}

        # 1) Prefer the latest normal move decision point with predictions
        cur.execute("""
            SELECT TOP 1
                DP.DecisionPointId,
                DP.TurnNumber,
                DP.Phase,
                DP.RequestType
            FROM dbo.DecisionPoints DP
            WHERE DP.BattleId = ?
            AND DP.ActorSide = ?
            AND DP.RequestType = 'move'
            AND EXISTS (
                SELECT 1
                FROM dbo.Predictions P
                WHERE P.DecisionPointId = DP.DecisionPointId
            )
            ORDER BY DP.LineNum DESC, DP.DecisionPointId DESC;
        """, battle_id_norm, predict_side)

        dp = cur.fetchone()

        # 2) If there isn't one yet, fall back to the latest predicted DP of any type
        if not dp:
            cur.execute("""
                SELECT TOP 1
                    DP.DecisionPointId,
                    DP.TurnNumber,
                    DP.Phase,
                    DP.RequestType
                FROM dbo.DecisionPoints DP
                WHERE DP.BattleId = ?
                AND DP.ActorSide = ?
                AND EXISTS (
                    SELECT 1
                    FROM dbo.Predictions P
                    WHERE P.DecisionPointId = DP.DecisionPointId
                )
                ORDER BY DP.LineNum DESC, DP.DecisionPointId DESC;
            """, battle_id_norm, predict_side)
            dp = cur.fetchone()

        if not dp:
            return {}

        dp_id = int(dp[0])
        turn_num = int(dp[1])
        phase = str(dp[2])
        request_type = str(dp[3] or "")
        self.turn_var.set(f"Current decision point: turn {turn_num}, phase {phase}, request {request_type}")

        cur.execute("""
            SELECT PredictionType, PredictionJson, CreatedAt
            FROM dbo.Predictions
            WHERE DecisionPointId = ?
            ORDER BY CreatedAt DESC, PredictionId DESC;
        """, dp_id)

        out: Dict[str, Dict[str, Any]] = {}
        for ptype, pjson, _ in cur.fetchall():
            ptype = str(ptype)
            if ptype in out:
                continue
            try:
                out[ptype] = json.loads(pjson) if isinstance(pjson, str) else pjson
            except Exception:
                continue

        return out

    def _render_predictions(self):
        bid = self._normalized_battle_id()
        if not bid or not self.predict_player:
            return

        payloads = self._fetch_latest_prediction_payloads(bid)

        self._clear_tree(self.combined_tree)
        self._clear_tree(self.moves_tree)
        self._clear_tree(self.switch_tree)

        if not payloads:
            return

        action_type = payloads.get("action_type", {})
        next_move = payloads.get("next_move", {})
        next_switch = payloads.get("next_switch", {})

        # Pull requestType from whichever payload exists
        meta = {}
        for p in (action_type, next_move, next_switch):
            if p and isinstance(p, dict):
                meta = p.get("meta", {}) or {}
                if meta:
                    break

        request_type = str(meta.get("requestType") or "").lower()

        # Normal move-turn decision point
        if request_type == "move":
            combined = combine_action_rankings(action_type, next_move, next_switch, top_n=5)

            for i, row in enumerate(combined, start=1):
                self.combined_tree.insert(
                    "",
                    "end",
                    values=(
                        i,
                        row["kind"],
                        row["label"],
                        f"{row['score']:.4f}",
                        f"{row['p_type']:.4f}",
                        f"{row['p_choice']:.4f}",
                    ),
                )

            for item in next_move.get("topK", []):
                self.moves_tree.insert(
                    "",
                    "end",
                    values=(
                        str(item.get("label", "")),
                        f"{float(item.get('prob', 0.0)):.4f}",
                    ),
                )

            for item in next_switch.get("topK", []):
                self.switch_tree.insert(
                    "",
                    "end",
                    values=(
                        str(item.get("label", "")),
                        f"{float(item.get('prob', 0.0)):.4f}",
                    ),
                )

        # Forced replacement / switch-only state
        elif request_type in ("switch", "forced_switch", "teampreview"):
            self.combined_tree.insert(
                "",
                "end",
                values=(
                    1,
                    "switch",
                    "Opponent choosing replacement",
                    "",
                    "",
                    "",
                ),
            )

            for item in next_switch.get("topK", []):
                self.switch_tree.insert(
                    "",
                    "end",
                    values=(
                        str(item.get("label", "")),
                        f"{float(item.get('prob', 0.0)):.4f}",
                    ),
                )

        # Fallback if requestType is missing or something unexpected
        else:
            combined = combine_action_rankings(action_type, next_move, next_switch, top_n=5)

            for i, row in enumerate(combined, start=1):
                self.combined_tree.insert(
                    "",
                    "end",
                    values=(
                        i,
                        row["kind"],
                        row["label"],
                        f"{row['score']:.4f}",
                        f"{row['p_type']:.4f}",
                        f"{row['p_choice']:.4f}",
                    ),
                )

            for item in next_move.get("topK", []):
                self.moves_tree.insert(
                    "",
                    "end",
                    values=(
                        str(item.get("label", "")),
                        f"{float(item.get('prob', 0.0)):.4f}",
                    ),
                )

            for item in next_switch.get("topK", []):
                self.switch_tree.insert(
                    "",
                    "end",
                    values=(
                        str(item.get("label", "")),
                        f"{float(item.get('prob', 0.0)):.4f}",
                    ),
                )


if __name__ == "__main__":
    app = BattleConnectUI()
    app.mainloop()
