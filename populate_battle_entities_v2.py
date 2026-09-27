import json
import re
from typing import Dict, List, Optional, Tuple

import pyodbc

from app_config import CONN_STR

# ---------
# Helpers
# ---------

def battle_has_entities(cur, battle_id: str) -> bool:
    cur.execute("SELECT TOP 1 1 FROM dbo.BattlePokemon WHERE BattleId = ?;", battle_id)
    return cur.fetchone() is not None

def get_battles(cur, limit: Optional[int] = None) -> List[str]:
    if limit is None:
        cur.execute("SELECT BattleId FROM dbo.Battles ORDER BY CreatedAt DESC;")
    else:
        cur.execute(f"SELECT TOP ({int(limit)}) BattleId FROM dbo.Battles ORDER BY CreatedAt DESC;")
    return [r[0] for r in cur.fetchall()]

def load_battle_header(cur, battle_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    cur.execute("""
        SELECT Format, Player1Name, Player2Name
        FROM dbo.Battles
        WHERE BattleId = ?;
    """, battle_id)
    row = cur.fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]

def upsert_battle_sides(cur, battle_id: str, p1_name: Optional[str], p2_name: Optional[str]) -> None:
    for side, name in (("p1", p1_name), ("p2", p2_name)):
        cur.execute("""
            IF EXISTS (SELECT 1 FROM dbo.BattleSides WHERE BattleId = ? AND Side = ?)
                UPDATE dbo.BattleSides
                SET PlayerName = COALESCE(?, PlayerName)
                WHERE BattleId = ? AND Side = ?;
            ELSE
                INSERT INTO dbo.BattleSides (BattleId, Side, PlayerName)
                VALUES (?, ?, ?);
        """, battle_id, side, name, battle_id, side, battle_id, side, name)

def load_battle_events(cur, battle_id: str) -> List[Dict]:
    """
    Loads all events including EventJson so we can parse names from parts[].
    """
    cur.execute("""
        SELECT
            BattleId, LineNum, TurnNumber,
            EventType, SubType,
            SourceSide, SourceIdent,
            TargetSide, TargetIdent,
            MoveName, ItemName, AbilityName, SpeciesName,
            EventJson
        FROM dbo.BattleEvents
        WHERE BattleId = ?
        ORDER BY LineNum;
    """, battle_id)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        out.append({cols[i]: row[i] for i in range(len(cols))})
    return out

def side_from_ident(ident: Optional[str]) -> Optional[str]:
    if not ident:
        return None
    ident = ident.strip().lower()
    if len(ident) >= 2 and ident[0] == "p" and ident[1] in ("1", "2"):
        return ident[:2]
    return None

def parse_event_parts(ev: Dict) -> List[str]:
    """
    ev['EventJson'] is a JSON string like {"raw":"|move|...","parts":[...]}
    """
    try:
        payload = json.loads(ev.get("EventJson") or "{}")
        parts = payload.get("parts") or []
        return parts if isinstance(parts, list) else []
    except Exception:
        return []

def parse_line_parts(line_text: str) -> List[str]:
    """
    BattleLogLines.LineText may look like:
      "|-terastallize|p1a: Ogerpon|Water"
    or sometimes without the leading pipe:
      "-terastallize|p1a: Ogerpon|Water"
    Returns tokens without empty segments.
    """
    if not line_text:
        return []
    s = line_text.strip()
    if s.startswith("|"):
        s = s[1:]
    parts = [p for p in s.split("|") if p != ""]
    return parts

def name_from_part(part: str) -> Optional[str]:
    """
    "p1a: Zacian" -> "Zacian"
    """
    if not part or not isinstance(part, str):
        return None
    if ": " in part:
        return part.split(": ", 1)[1].strip()
    return None

def ident_from_part(part: str) -> Optional[str]:
    """
    "p1a: Zacian" -> "p1a"
    """
    if not part or not isinstance(part, str):
        return None
    if ": " in part:
        return part.split(": ", 1)[0].strip()
    return None

def ensure_battlepokemon_has_battlename_column(cur) -> None:
    """
    Hard-fail early if the user hasn't added BattleName.
    """
    cur.execute("""
        SELECT 1
        FROM sys.columns
        WHERE object_id = OBJECT_ID('dbo.BattlePokemon')
          AND name = 'BattleName';
    """)
    if cur.fetchone() is None:
        raise RuntimeError(
            "dbo.BattlePokemon is missing column BattleName. Run:\n"
            "ALTER TABLE dbo.BattlePokemon ADD BattleName NVARCHAR(100) NULL;"
        )

# -------------------------
# BattlePokemon construction
# -------------------------
# Since poke| lines are missing in your DB (Step 2 empty), we keep the fallback approach:
#   - Insert species list per side from first-seen switch SpeciesName
#
# This is OK as long as we do NOT use species to link events.
# Event linking will be done by BattleName, which we derive from EventJson parts.

def extract_roster_from_switch_species(events: List[Dict]) -> Dict[str, List[str]]:
    roster: Dict[str, List[str]] = {"p1": [], "p2": []}
    for ev in events:
        if ev.get("EventType") != "switch":
            continue
        side = (ev.get("SourceSide") or side_from_ident(ev.get("SourceIdent")) or "").lower()
        species = ev.get("SpeciesName")
        if side in roster and species and species not in roster[side]:
            roster[side].append(species)
    roster["p1"] = roster["p1"][:6]
    roster["p2"] = roster["p2"][:6]
    return roster

def insert_battle_pokemon(cur, battle_id: str, roster: Dict[str, List[str]]) -> None:
    rows = []
    for side in ("p1", "p2"):
        for i, species in enumerate(roster.get(side, []), start=1):
            rows.append((battle_id, side, i, species))
    if not rows:
        return
    cur.fast_executemany = True
    cur.executemany("""
        INSERT INTO dbo.BattlePokemon (BattleId, Side, Slot, Species)
        VALUES (?, ?, ?, ?);
    """, rows)

# -------------------------
# Assign BattleName mapping
# -------------------------

def load_battlepokemon_slots(cur, battle_id: str) -> Dict[str, List[Tuple[int, int, Optional[str], str]]]:
    """
    Returns per-side list: (BattlePokemonId, Slot, BattleName, Species) sorted by Slot.
    """
    cur.execute("""
        SELECT BattlePokemonId, Side, Slot, BattleName, Species
        FROM dbo.BattlePokemon
        WHERE BattleId = ?
        ORDER BY Side, Slot;
    """, battle_id)
    out = {"p1": [], "p2": []}
    for bp_id, side, slot, bname, species in cur.fetchall():
        side = (side or "").lower()
        if side in out:
            out[side].append((int(bp_id), int(slot), bname, species))
    return out

def assign_battlenames(cur, battle_id: str, events: List[Dict]) -> None:
    """
    Assign BattleName to BattlePokemon rows.
    - For switch events: match to the correct species row (preferred).
    - Fallback: first empty slot order (only if species can't be determined).
    - For move events: keep the old fallback behavior to fill any remaining names.
    """
    slots = load_battlepokemon_slots(cur, battle_id)

    # Build existing name->id map and also species->id map per side
    name_to_id: Dict[Tuple[str, str], int] = {}
    species_to_ids: Dict[str, Dict[str, List[int]]] = {"p1": {}, "p2": {}}
    # Keep slot order list for "first empty" fallback
    for side in ("p1", "p2"):
        for bp_id, slot, bname, species in slots[side]:
            if bname:
                name_to_id[(side, bname)] = bp_id
            if species:
                species_to_ids[side].setdefault(species, []).append(bp_id)

    def assign_name_to_next_empty(side: str, battle_name: str) -> Optional[int]:
        if (side, battle_name) in name_to_id:
            return None
        for i, (bp_id, slot, bname, species) in enumerate(slots[side]):
            if not bname:
                slots[side][i] = (bp_id, slot, battle_name, species)
                name_to_id[(side, battle_name)] = bp_id
                return bp_id
        return None

    def parse_species_from_switch_parts(parts: List[str]) -> Optional[str]:
        """
        Expected switch parts often look like:
            ["", "p2a: Giratina", "Giratina-Origin, L80, M", "100/100"]
        We want the species display name (before the first comma) from parts[2].
        """
        if len(parts) < 3:
            return None
        s = (parts[2] or "").strip()
        if not s:
            return None
        if "," in s:
            s = s.split(",", 1)[0].strip()
        return s or None

    def find_bp_id_for_species(side: str, species: str) -> Optional[int]:
        """
        Match BattlePokemon row by Species (DisplayName) on the given side.
        First try exact match; then try prefix match for formes.
        Prefer an unassigned BattleName row if possible.
        """
        # Exact match in cached slots
        exact_candidates = []
        prefix_candidates = []

        for bp_id, slot, bname, sp in slots[side]:
            if not sp:
                continue
            if sp == species:
                exact_candidates.append((bp_id, bname))
            elif sp.startswith(species) or species.startswith(sp):
                # Handles cases like "Giratina" vs "Giratina-Origin" if needed
                prefix_candidates.append((bp_id, bname))

        # Prefer exact, and within that prefer rows without BattleName set yet
        for cand in exact_candidates:
            if not cand[1]:
                return cand[0]
        if exact_candidates:
            return exact_candidates[0][0]

        for cand in prefix_candidates:
            if not cand[1]:
                return cand[0]
        if prefix_candidates:
            return prefix_candidates[0][0]

        return None

    updates: List[Tuple[str, int]] = []

    # Pass 1: switch events (most reliable)
    for ev in events:
        if ev.get("EventType") != "switch":
            continue
        parts = parse_event_parts(ev)
        if len(parts) < 2:
            continue

        source_part = parts[1]  # "p2a: Giratina"
        ident = ident_from_part(source_part)
        bname = name_from_part(source_part)
        side = side_from_ident(ident) or (ev.get("SourceSide") or "").lower()
        if side not in ("p1", "p2") or not bname:
            continue

        # If already mapped, skip
        if (side, bname) in name_to_id:
            continue

        switched_species = parse_species_from_switch_parts(parts)
        assigned_id: Optional[int] = None

        if switched_species:
            assigned_id = find_bp_id_for_species(side, switched_species)

        if assigned_id is None:
            # Fallback: old behavior
            assigned_id = assign_name_to_next_empty(side, bname)
        else:
            # Update cache so later matches see it
            name_to_id[(side, bname)] = assigned_id
            # also update slots cache
            for i, (bp_id, slot, old_bname, sp) in enumerate(slots[side]):
                if bp_id == assigned_id and not old_bname:
                    slots[side][i] = (bp_id, slot, bname, sp)
                    break

        if assigned_id:
            updates.append((bname, assigned_id))

    # Pass 2: move events (fills any remaining names)
    for ev in events:
        if ev.get("EventType") != "move":
            continue
        parts = parse_event_parts(ev)
        if len(parts) < 2:
            continue
        source_part = parts[1]
        ident = ident_from_part(source_part)
        bname = name_from_part(source_part)
        side = side_from_ident(ident) or (ev.get("SourceSide") or "").lower()
        if side not in ("p1", "p2") or not bname:
            continue
        assigned_id = assign_name_to_next_empty(side, bname)
        if assigned_id:
            updates.append((bname, assigned_id))

    if updates:
        cur.fast_executemany = True
        cur.executemany("""
            UPDATE dbo.BattlePokemon
            SET BattleName = ?
            WHERE BattlePokemonId = ?;
        """, updates)

# -------------------------
# Link BattleEvents to BattlePokemonIds
# -------------------------

def build_name_lookup(cur, battle_id: str) -> Dict[Tuple[str, str], int]:
    """
    (side, BattleName) -> BattlePokemonId
    """
    cur.execute("""
        SELECT BattlePokemonId, Side, BattleName
        FROM dbo.BattlePokemon
        WHERE BattleId = ?
          AND BattleName IS NOT NULL;
    """, battle_id)
    mp: Dict[Tuple[str, str], int] = {}
    for bp_id, side, bname in cur.fetchall():
        side = (side or "").lower()
        if side in ("p1", "p2") and bname:
            mp[(side, bname)] = int(bp_id)
    return mp

def backfill_event_pokemon_ids_by_name(cur, battle_id: str, events: List[Dict]) -> int:
    """
    Updates BattleEvents.SourceBattlePokemonId/TargetBattlePokemonId using name mapping.

    For move: source from parts[1], target from parts[3] if present
    For item/ability/hp/faint: prefer target from parts[1] if it looks like "pXa: NAME"
      (many -ability/-item lines use the affected mon as the first param after the tag)
    """
    name_lookup = build_name_lookup(cur, battle_id)
    updates: List[Tuple[Optional[int], Optional[int], str, int]] = []

    for ev in events:
        line_num = int(ev["LineNum"])
        et = ev.get("EventType")

        parts = parse_event_parts(ev)

        src_id: Optional[int] = None
        tgt_id: Optional[int] = None

        if et == "move":
            # parts: ["move", "p1a: Zacian", "Behemoth Blade", "p2a: Kyogre"]
            if len(parts) >= 2:
                sp = parts[1]
                ident = ident_from_part(sp)
                name = name_from_part(sp)
                side = side_from_ident(ident) or (ev.get("SourceSide") or "").lower()
                if side in ("p1", "p2") and name:
                    src_id = name_lookup.get((side, name))

            if len(parts) >= 4:
                tp = parts[3]
                ident = ident_from_part(tp)
                name = name_from_part(tp)
                side = side_from_ident(ident) or (ev.get("TargetSide") or "").lower()
                if side in ("p1", "p2") and name:
                    tgt_id = name_lookup.get((side, name))

        elif et == "switch":
            # parts: ["switch", "p1a: Zacian", "Zacian", "100/100"]
            if len(parts) >= 2:
                sp = parts[1]
                ident = ident_from_part(sp)
                name = name_from_part(sp)
                side = side_from_ident(ident) or (ev.get("SourceSide") or "").lower()
                if side in ("p1", "p2") and name:
                    src_id = name_lookup.get((side, name))
                    tgt_id = src_id

        else:
            # Generic handling: try to set target from parts[1] if it looks like "pXa: NAME"
            # Examples: "-ability", "-item", "damage/hp_change" style events depending on your parser
            if len(parts) >= 2:
                ap = parts[1]
                ident = ident_from_part(ap)
                name = name_from_part(ap)
                side = side_from_ident(ident) or (ev.get("TargetSide") or ev.get("SourceSide") or "").lower()
                if side in ("p1", "p2") and name:
                    tgt_id = name_lookup.get((side, name))

            # Source sometimes still helpful if available
            if len(parts) >= 2 and et in ("ability_activate", "item_activate", "activate"):
                ap = parts[1]
                ident = ident_from_part(ap)
                name = name_from_part(ap)
                side = side_from_ident(ident) or (ev.get("SourceSide") or "").lower()
                if side in ("p1", "p2") and name:
                    src_id = name_lookup.get((side, name))

        if src_id is not None or tgt_id is not None:
            updates.append((src_id, tgt_id, battle_id, line_num))

    if not updates:
        return 0

    cur.fast_executemany = True
    cur.executemany("""
        UPDATE dbo.BattleEvents
        SET SourceBattlePokemonId = COALESCE(?, SourceBattlePokemonId),
            TargetBattlePokemonId = COALESCE(?, TargetBattlePokemonId)
        WHERE BattleId = ? AND LineNum = ?;
    """, updates)

    return len(updates)

def load_tera_reveals_from_loglines(cur, battle_id: str) -> List[Tuple[int, int, int, str]]:
    """
    Returns list of tuples:
      (BattlePokemonId, TurnNumber, LineNum, TeraType)
    from BattleLogLines where LineType = '-terastallize'.
    """
    name_lookup = build_name_lookup(cur, battle_id)  # (side, BattleName) -> BattlePokemonId

    cur.execute("""
        SELECT LineNum, TurnNumber, LineText
        FROM dbo.BattleLogLines
        WHERE BattleId = ?
          AND LineType = '-terastallize'
        ORDER BY LineNum;
    """, battle_id)

    out: List[Tuple[int, int, int, str]] = []
    for line_num, turn_num, line_text in cur.fetchall():
        line_num = int(line_num)
        turn_num = int(turn_num) if turn_num is not None else 0

        parts = parse_line_parts(str(line_text or ""))
        # Expected parts:
        #   ["-terastallize", "p1a: Ogerpon", "Water"]
        # Sometimes extra fields can appear; we only need the first 3.
        if len(parts) < 3:
            continue

        tag = parts[0]
        if tag != "-terastallize":
            continue

        who = parts[1]          # "p1a: Ogerpon"
        tera_type = parts[2]    # "Water"

        ident = ident_from_part(who)
        bname = name_from_part(who)
        side = side_from_ident(ident)

        if side not in ("p1", "p2") or not bname or not tera_type:
            continue

        bp_id = name_lookup.get((side, bname))
        if not bp_id:
            continue

        out.append((int(bp_id), turn_num, line_num, str(tera_type)))

    return out

# -------------------------
# Populate reveals from linked events
# -------------------------

def populate_reveals_from_events(cur, battle_id: str, mode: str = "skip") -> int:
    if mode == "replace":
        cur.execute("""
            DELETE R
            FROM dbo.BattlePokemonReveals R
            JOIN dbo.BattlePokemon P ON P.BattlePokemonId = R.BattlePokemonId
            WHERE P.BattleId = ?;
        """, battle_id)

    cur.execute("""
        SELECT LineNum, TurnNumber, EventType,
        SourceBattlePokemonId, TargetBattlePokemonId,
        MoveName, ItemName, AbilityName,
        SubType
        FROM dbo.BattleEvents
        WHERE BattleId = ?
        ORDER BY LineNum;
    """, battle_id)

    inserts = []
    for line_num, turn_num, et, src_bp_id, tgt_bp_id, move, item, ability, subtype in cur.fetchall():
        line_num = int(line_num)
        turn_num = int(turn_num) if turn_num is not None else 0

        if et == "move" and src_bp_id and move:
            inserts.append((int(src_bp_id), turn_num, line_num, "move", str(move)))

        # Item reveal events typically apply to the target mon
        if et in ("item_reveal", "item_activate") and tgt_bp_id and item:
            inserts.append((int(tgt_bp_id), turn_num, line_num, "item", str(item)))

        if et in ("item_consume", "item_remove") and tgt_bp_id:
            inserts.append((int(tgt_bp_id), turn_num, line_num, "item", "NONE"))

        if et in ("ability_reveal", "ability_activate") and tgt_bp_id and ability:
            inserts.append((int(tgt_bp_id), turn_num, line_num, "ability", str(ability)))
            
        # Tera reveal: stored as EventType='terastallize', SubType='<TeraType>', applies to target mon
        if et == "terastallize" and tgt_bp_id and subtype:
            inserts.append((int(tgt_bp_id), turn_num, line_num, "tera", str(subtype)))


    # --- Tera reveals from BattleLogLines (-terastallize) ---
    try:
        tera_rows = load_tera_reveals_from_loglines(cur, battle_id)
        for bp_id, turn_num, line_num, tera_type in tera_rows:
            inserts.append((bp_id, turn_num, line_num, "tera", tera_type))
    except Exception as e:
        # If BattleLogLines schema differs, you can see it here without killing the whole battle.
        print(f"[WARN] Could not load tera reveals for {battle_id}: {e}")


    if not inserts:
        return 0

    seen = set()
    deduped = []
    for row in inserts:
        key = (row[0], row[3], row[4])  # (BattlePokemonId, RevealType, RevealValue)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    cur.fast_executemany = True
    cur.executemany("""
    INSERT INTO dbo.BattlePokemonReveals (BattlePokemonId, TurnNumber, LineNum, RevealType, RevealValue, Source)
    SELECT ?, ?, ?, ?, ?, 'log'
    WHERE NOT EXISTS (
        SELECT 1
        FROM dbo.BattlePokemonReveals
        WHERE BattlePokemonId = ?
          AND RevealType = ?
          AND RevealValue = ?
    );
""", [(bp, tn, ln, rt, rv, bp, rt, rv) for (bp, tn, ln, rt, rv) in deduped])

    return len(deduped)

# -------------------------
# Orchestration per battle
# -------------------------

def populate_entities_for_battle(cur, battle_id: str, mode: str = "skip") -> bool:
    ensure_battlepokemon_has_battlename_column(cur)

    # Only BF gen9
    battle_id_norm = battle_id.removeprefix("battle-")
    if not battle_id_norm.lower().startswith("gen9battlefactory-"):
        return False

    fmt, p1_name, p2_name = load_battle_header(cur, battle_id_norm)
    upsert_battle_sides(cur, battle_id_norm, p1_name, p2_name)

    events = load_battle_events(cur, battle_id)

    if mode == "replace":
        # Clear battle-scoped derived data
        cur.execute("""
            DELETE R
            FROM dbo.BattlePokemonReveals R
            JOIN dbo.BattlePokemon P ON P.BattlePokemonId = R.BattlePokemonId
            WHERE P.BattleId = ?;
        """, battle_id)

        cur.execute("""
            UPDATE dbo.BattleEvents
            SET SourceBattlePokemonId = NULL,
                TargetBattlePokemonId = NULL
            WHERE BattleId = ?;
        """, battle_id)

        cur.execute("DELETE FROM dbo.BattlePokemon WHERE BattleId = ?;", battle_id)
        cur.execute("DELETE FROM dbo.BattleSides  WHERE BattleId = ?;", battle_id)
        # Reinsert sides after delete
        upsert_battle_sides(cur, battle_id, p1_name, p2_name)

    # Only roster creation should be skipped when entities already exist
    if not battle_has_entities(cur, battle_id):
        roster = extract_roster_from_switch_species(events)
        insert_battle_pokemon(cur, battle_id, roster)

    # Assign BattleName values
    assign_battlenames(cur, battle_id, events)

    # Link events to pokemon ids by name
    backfill_event_pokemon_ids_by_name(cur, battle_id, events)

    # Populate reveals
    populate_reveals_from_events(cur, battle_id, mode=("replace" if mode == "replace" else "skip"))

    return True

def main(mode: str = "skip", limit: Optional[int] = None) -> None:
    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    try:
        cur = cn.cursor()
        battle_ids = get_battles(cur, limit=limit)

        for i, bid in enumerate(battle_ids, start=1):
            try:
                did = populate_entities_for_battle(cur, bid, mode=mode)
                cn.commit()
                if did:
                    print(f"[{i}/{len(battle_ids)}] Populated entities for {bid}")
                else:
                    print(f"[{i}/{len(battle_ids)}] No-op for {bid} (skipped or not BF Gen9)")
            except Exception as e:
                cn.rollback()
                print(f"[{i}/{len(battle_ids)}] FAILED {bid}: {e}")
    finally:
        cn.close()

if __name__ == "__main__":
    import sys
    mode = "skip"
    lim = None

    if len(sys.argv) >= 2:
        mode = sys.argv[1].strip().lower()
    if len(sys.argv) >= 3:
        lim = int(sys.argv[2])

    if mode not in ("skip", "replace"):
        print("Mode must be 'skip' or 'replace'")
        raise SystemExit(2)

    main(mode=mode, limit=lim)
