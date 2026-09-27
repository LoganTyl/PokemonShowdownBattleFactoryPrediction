"""
Live ingest v2: writes live protocol lines to BattleLogLines and rebuilds BattleEvents at turn boundaries,
then runs per-battle pipeline steps (entities -> candidates -> eliminations/suspicious) so your UI can query
mid-match without running manual scripts.

This file supersedes live_ingest.py by adding an optional post-checkpoint hook.

Key behavior:
- Every line: insert into dbo.BattleLogLines immediately
- On |turn|: rebuild dbo.BattleEvents, then run live_turn_pipeline.run_turn_checkpoint(battle_id, mode="skip")
- On |win|/|tie|: same (final)

If you want to disable the heavier post-checkpoint work during a match, pass run_turn_pipeline=False.

How to use:
  from live_ingest_v2 import LiveBattleIngestor
  ing = LiveBattleIngestor(conn_str=..., run_turn_pipeline=True)
  connect_and_join_room(..., on_battle_line=ing.on_line)

Note: This still does NOT rely on |request| lines. Those can be added later to fill LegalActionsJson.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Dict

import pyodbc

import parse_battle_events
from live_turn_pipeline import run_turn_checkpoint, CONN_STR as PIPE_CONN_STR
from predict_and_insert_predictions_v2 import run_predictions_for_battle
import init_set_candidates

TURN_RE = re.compile(r"^\|turn\|(\d+)\s*$", re.IGNORECASE)
PLAYER_RE = re.compile(r"^\|player\|(p1|p2)\|([^|]+)\|")
TIER_RE = re.compile(r"^\|tier\|([^|]+)\|?")
POKE_LINE_RE = re.compile(r"^\|poke\|(p1|p2)\|([^|]+)\|")

def infer_line_type(line_text: str) -> Optional[str]:
    if not line_text or not line_text.startswith("|"):
        return None
    parts = line_text.split("|")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[0]:
        return parts[0][:32]
    return None

def normalize_battle_id(battle_id: str) -> str:
    return battle_id.removeprefix("battle-")

def _to_id(self, s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", s.lower())

def _extract_species(poke_details: str) -> str:
    # "Swampert, M" -> "Swampert"
    # "Swampert, M" could include extra tokens; we only need species before first comma
    s = poke_details.strip()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    return s

@dataclass
class BattleStreamState:
    battle_id: str
    next_line_num: int
    current_turn: Optional[int]
    preview_collecting: bool = False
    preview_roster: Dict[str, list] = None


class LiveBattleIngestor:
    def __init__(
        self,
        conn_str: str = PIPE_CONN_STR,
        run_turn_pipeline_after_checkpoints: bool = True,
        turn_pipeline_mode: str = "skip",
        run_sql_procs: bool = True,
        run_predictions_after_checkpoints: bool = True,
    ):
        self.conn_str = conn_str
        self.run_turn_pipeline_after_checkpoints = run_turn_pipeline_after_checkpoints
        self.turn_pipeline_mode = turn_pipeline_mode
        self.run_sql_procs = run_sql_procs
        self.run_predictions_after_checkpoints = run_predictions_after_checkpoints
        self.predict_for_player_name: Optional[str] = None
        
        self.conn = pyodbc.connect(conn_str, timeout=120)
        self.conn.autocommit = False
        self.cur = self.conn.cursor()

        self._states: Dict[str, BattleStreamState] = {}

    def _ensure_battle_row(self, battle_id: str) -> None:
        self.cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM dbo.Battles WHERE BattleId = ?)
            BEGIN
                INSERT INTO dbo.Battles (BattleId, Format, Source)
                VALUES (?, ?, ?);
            END
            ELSE
            BEGIN
                UPDATE dbo.Battles
                    SET Source = COALESCE(Source, ?),
                    Format = COALESCE(Format, ?)
                WHERE BattleId = ?;
            END
        """, battle_id, battle_id, "[Gen 9] Battle Factory", "live", "live", "[Gen 9] Battle Factory", battle_id)
        self.conn.commit()

    def _load_state(self, battle_id: str) -> BattleStreamState:
        if battle_id in self._states:
            return self._states[battle_id]

        self._ensure_battle_row(battle_id)

        self.cur.execute(
            "SELECT ISNULL(MAX(LineNum), 0) FROM dbo.BattleLogLines WHERE BattleId = ?;",
            battle_id
        )
        max_line = int(self.cur.fetchone()[0] or 0)

        self.cur.execute(
            "SELECT TOP 1 TurnNumber FROM dbo.BattleLogLines WHERE BattleId = ? AND TurnNumber IS NOT NULL ORDER BY LineNum DESC;",
            battle_id
        )
        row = self.cur.fetchone()
        cur_turn = int(row[0]) if row and row[0] is not None else None

        st = BattleStreamState(battle_id=battle_id, next_line_num=max_line + 1, current_turn=cur_turn)
        st.preview_collecting = False
        st.preview_roster = {"p1": [], "p2": []}
        self._states[battle_id] = st
        return st

    def _insert_log_line(self, st: BattleStreamState, line_text: str) -> None:
        m = TURN_RE.match(line_text.strip())
        if m:
            st.current_turn = int(m.group(1))

        line_type = infer_line_type(line_text)
        self.cur.execute(
            """
            INSERT INTO dbo.BattleLogLines (BattleId, LineNum, TurnNumber, LineType, LineText)
            VALUES (?, ?, ?, ?, ?);
            """,
            st.battle_id, st.next_line_num, st.current_turn, line_type, line_text
        )
        st.next_line_num += 1
        
        # Update battle header fields (player names / tier) as soon as we see them
        self._maybe_update_battle_header(st.battle_id, line_text)

    def _checkpoint(self, battle_id: str) -> None:
        # 1) rebuild events from loglines
        n = parse_battle_events.parse_and_insert_for_battle(self.cur, battle_id, mode="replace")
        self.conn.commit()
        print(f"[live_ingest] Rebuilt BattleEvents for {battle_id}: {n} events")

        # 2) run per-battle pipeline (entities/candidates/elims)
        if self.run_turn_pipeline_after_checkpoints:
            res = run_turn_checkpoint(
                battle_id=battle_id,
                mode=self.turn_pipeline_mode,
                conn_str=self.conn_str,
                run_sql=self.run_sql_procs,
            )
            print(f"[live_ingest] Turn pipeline ran for {battle_id}: elim={res.sql_elim_ran}, susp={res.sql_susp_ran}")
            
        # 3) Generate predictions for newest DecisionPoints (selected opponent)
        if self.run_predictions_after_checkpoints:
            if not self.predict_for_player_name:
                print(f"[live_ingest] Predictions skipped for {battle_id}: no predict_for_player_name selected yet")
                return
            try:
                pr = run_predictions_for_battle(
                    conn_str=self.conn_str,
                    battle_id=battle_id,
                    predict_for_player_name=self.predict_for_player_name,
                    mode="replace",
                    latest_only=True,
                    n_per_side=1,
                )
                print(
                    f"[live_ingest] Predictions inserted for {battle_id}: "
                    f"selected={pr.get('selected')} inserted={pr.get('inserted')}"
                )
            except Exception as e:
                print(f"[live_ingest] Prediction step failed for {battle_id}: {e}")

    def on_line(self, battle_id: str, line_text: str) -> None:
        if not battle_id or not line_text:
            return
        
        self._ensure_sql()
        
        battle_id = normalize_battle_id(battle_id)

        st = self._load_state(battle_id)
        
        line = line_text.strip()

        if line == "|clearpoke":
            st.preview_collecting = True
            st.preview_roster = {"p1": [], "p2": []}

        m = POKE_LINE_RE.match(line)
        if m and st.preview_collecting:
            side = m.group(1)        # p1 / p2
            details = m.group(2)     # "Swampert, M"
            species = _extract_species(details)
            if species:
                st.preview_roster[side].append(species)

        if line == "|teampreview" and st.preview_collecting:
            st.preview_collecting = False
            # Persist roster -> BattlePokemon (+ candidates)
            self._persist_teampreview_roster(st.battle_id, st.preview_roster)

        try:
            self._insert_log_line(st, line_text)
            self.conn.commit()
        except Exception:
            try:
                if self.conn is not None:
                    self.conn.rollback()
            except Exception:
                pass

            # Reconnect and retry once
            self._close_sql()
            self._ensure_sql()
            self._insert_log_line(st, line_text)
            self.conn.commit()

        lt = infer_line_type(line_text)
        
        # Trigger checkpoint when a new decision point should exist.
        # - turn: normal move/switch choice point
        # - faint: forced replacement choice point
        # - win/tie: finalize
        if lt in ("turn", "faint"):
            self._checkpoint(battle_id)
        elif lt in ("win", "tie"):
            self._checkpoint(battle_id)
            try:
                self._finalize_rawlog(battle_id)
                self._finalize_inputlog(battle_id)
                self.conn.commit()
            except Exception:
                try:
                    if self.conn is not None:
                        self.conn.rollback()
                except Exception:
                    self._close_sql()
                raise

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.commit()
        except Exception:
            pass
        self._close_sql()
        
    def _maybe_update_battle_header(self, battle_id: str, line_text: str) -> None:
        line = line_text.strip()

        m = PLAYER_RE.match(line)
        if m:
            side = m.group(1)  # p1 or p2
            name = m.group(2).strip()
            if side == "p1":
                self.cur.execute(
                    "UPDATE dbo.Battles SET Player1Name = COALESCE(Player1Name, ?) WHERE BattleId = ?;",
                    name, battle_id
                )
            else:
                self.cur.execute(
                    "UPDATE dbo.Battles SET Player2Name = COALESCE(Player2Name, ?) WHERE BattleId = ?;",
                    name, battle_id
                )
            return

        m = TIER_RE.match(line)
        if m:
            tier = m.group(1).strip()
            try:
                self.cur.execute(
                    "UPDATE dbo.Battles SET Format = COALESCE(Format, ?) WHERE BattleId = ?;",
                    tier, battle_id
                )
            except Exception:
                pass
            
    def _to_id(self, name: str) -> str:
        # Showdown-ish ID normalization (matches PokemonId style)
        return re.sub(r"[^a-z0-9]", "", name.lower())
            
    def _persist_teampreview_roster(self, battle_id: str, roster: dict) -> None:
        """
        Writes BattlePokemon rows keyed by (BattleId, Side, Slot) with Species = DexPokemon.PokemonId.
        """
        # Map display name -> PokemonId (DexPokemon schema you showed)
        def normalize_to_display_name(display: str) -> str:
            # Try exact display name (best)
            self.cur.execute(
                "SELECT TOP 1 DisplayName FROM dbo.DexPokemon WHERE DisplayName = ?;",
                display
            )
            r = self.cur.fetchone()
            if r:
                return r[0]

            # Fallback: try by PokemonId derived from the string
            pid = self._to_id(display)
            self.cur.execute(
                "SELECT TOP 1 DisplayName FROM dbo.DexPokemon WHERE PokemonId = ?;",
                pid
            )
            r = self.cur.fetchone()
            return r[0] if r else display  # last resort: keep original

        for side in ("p1", "p2"):
            species_names = (roster.get(side) or [])[:6]
            for slot, display_name in enumerate(species_names, start=1):
                species_display = normalize_to_display_name(display_name)

                # Upsert on (BattleId, Side, Slot)
                self.cur.execute("""
                    SET NOCOUNT ON;             

                    DECLARE @ExistingId BIGINT;

                    SELECT @ExistingId = BattlePokemonId
                    FROM dbo.BattlePokemon
                    WHERE BattleId = ? AND Side = ? AND Slot = ?;

                    IF @ExistingId IS NULL
                    BEGIN
                        INSERT INTO dbo.BattlePokemon (BattleId, Side, Slot, Species, IsLead)
                        VALUES (?, ?, ?, ?, 0);
                        SET @ExistingId = SCOPE_IDENTITY();
                    END
                    ELSE
                    BEGIN
                        UPDATE dbo.BattlePokemon
                        SET Species = COALESCE(Species, ?)
                        WHERE BattlePokemonId = @ExistingId;
                    END

                    SELECT @ExistingId;
                """,
                battle_id, side, slot,
                battle_id, side, slot, species_display,
                species_display)

                bp_id = int(self.cur.fetchone()[0])

        self.conn.commit()

        init_set_candidates.init_candidates_for_battle(self.cur, battle_id, mode="skip")

        self.conn.commit()
        
    def _finalize_rawlog(self, battle_id: str) -> None:
        # SQL Server 2017+ supports STRING_AGG
        self.cur.execute("""
            UPDATE dbo.Battles
            SET RawLog = (
                SELECT STRING_AGG(LineText, CHAR(10)) WITHIN GROUP (ORDER BY LineNum)
                FROM dbo.BattleLogLines
                WHERE BattleId = ?
            )
            WHERE BattleId = ?;
        """, battle_id, battle_id)
    
    def _finalize_inputlog(self, battle_id: str) -> None:
        self.cur.execute("""
            SELECT Format, Player1Name, Player2Name
            FROM dbo.Battles
            WHERE BattleId = ?;
        """, battle_id)
        row = self.cur.fetchone()
        fmt = row[0] if row else "[Gen 9] Battle Factory"
        p1 = row[1] if row else None
        p2 = row[2] if row else None

        lines = []
        lines.append("version live")
        # Keep it close to replay semantics; formatid is the engine id, not display name
        lines.append('start {"formatid":"gen9battlefactory"}')
        if p1:
            lines.append(f'player p1 {{"name":"{p1}"}}')
        if p2:
            lines.append(f'player p2 {{"name":"{p2}"}}')

        self.cur.execute("""
            SELECT LineNum, TurnNumber, EventType, SourceSide, TargetSide, MoveName, SpeciesName, SubType
            FROM dbo.BattleEvents
            WHERE BattleId = ?
            ORDER BY LineNum;
        """, battle_id)

        last_move_idx = {}  # (side, turn) -> index in lines
        for ln, tn, et, src_side, tgt_side, move, species, subtype in self.cur.fetchall():
            tn = int(tn) if tn is not None else 0

            if et == "switch" and src_side and species:
                lines.append(f"{src_side} switch {self._to_id(species)}")
            elif et == "move" and src_side and move:
                m = self._to_id(move)
                lines.append(f"{src_side} move {m}")
                last_move_idx[(src_side, tn)] = len(lines) - 1
            elif et == "terastallize" and tgt_side:
                # Best-effort: attach tera to the last move by that side this turn, else emit standalone
                idx = last_move_idx.get((tgt_side, tn))
                if idx is not None and "terastallize" not in lines[idx]:
                    lines[idx] = lines[idx] + " terastallize"
                else:
                    ttype = self._to_id(subtype or "")
                    if ttype:
                        lines.append(f"{tgt_side} terastallize {ttype}")

        inputlog = "\n".join(lines)

        self.cur.execute("""
            UPDATE dbo.Battles
            SET InputLog = COALESCE(InputLog, ?)
            WHERE BattleId = ?;
        """, inputlog, battle_id)
        
    def _close_sql(self):
        try:
            if getattr(self, "cur", None) is not None:
                self.cur.close()
        except Exception:
            pass
        try:
            if getattr(self, "conn", None) is not None:
                self.conn.close()
        except Exception:
            pass
        self.cur = None
        self.conn = None


    def _ensure_sql(self):
        """
        Ensure self.conn/self.cur exist and are alive.
        Reopen them if they were closed or invalidated.
        """
        if getattr(self, "conn", None) is not None and getattr(self, "cur", None) is not None:
            try:
                self.cur.execute("SELECT 1;")
                self.cur.fetchone()
                return
            except Exception:
                self._close_sql()

        self.conn = pyodbc.connect(self.conn_str, timeout=120)
        self.conn.autocommit = False
        self.cur = self.conn.cursor()