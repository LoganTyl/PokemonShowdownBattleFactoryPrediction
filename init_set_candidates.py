import re
from typing import Optional, Tuple, List

import pyodbc

from app_config import CONN_STR

# Example raw line often appears like:
# |raw|<div class="broadcast-blue"><b>Battle Factory Tier:</b> Uber</div>
TIER_RE = re.compile(r"Battle Factory Tier:\s*([^<]+)", re.IGNORECASE)

def battle_has_setcandidates(cur, battle_id: str) -> bool:
    cur.execute("""
        SELECT TOP 1 1
        FROM dbo.SetCandidates SC
        JOIN dbo.BattlePokemon BP ON BP.BattlePokemonId = SC.BattlePokemonId
        WHERE BP.BattleId = ?;
    """, battle_id)
    return cur.fetchone() is not None

def extract_bf_tier_name(cur, battle_id: str) -> Optional[str]:
    # 1) Try BattleLogLines first (your stored lines contain "raw|<div ... Battle Factory Tier: Uber</b></div>")
    cur.execute("""
        SELECT LineText
        FROM dbo.BattleLogLines
        WHERE BattleId = ?
          AND LineText LIKE '%Battle Factory Tier:%'
        ORDER BY LineNum;
    """, battle_id)

    for (line_text,) in cur.fetchall():
        m = TIER_RE.search(line_text or "")
        if m:
            return m.group(1).strip()

    # 2) Fallback to RawLog
    cur.execute("SELECT RawLog FROM dbo.Battles WHERE BattleId = ?;", battle_id)
    row = cur.fetchone()
    raw_log = row[0] if row else None
    if raw_log:
        m = TIER_RE.search(raw_log)
        if m:
            return m.group(1).strip()

    return None

def resolve_tier_id(cur, tier_name: str) -> Optional[int]:
    # Adjust TierName column if yours differs
    cur.execute("""
        SELECT TierId
        FROM dbo.BF_Tier
        WHERE TierName = ?;
    """, tier_name)
    row = cur.fetchone()
    return int(row[0]) if row else None

def init_candidates_for_battle(cur, battle_id: str, mode: str = "skip") -> int:
    """
    mode:
      - skip: do nothing if battle already has candidates
      - replace: delete existing and rebuild
    Returns inserted row count (best effort).
    """
    if mode == "skip" and battle_has_setcandidates(cur, battle_id):
        return 0

    if mode == "replace":
        cur.execute("""
            DELETE SC
            FROM dbo.SetCandidates SC
            JOIN dbo.BattlePokemon BP ON BP.BattlePokemonId = SC.BattlePokemonId
            WHERE BP.BattleId = ?;
        """, battle_id)

    tier_name = extract_bf_tier_name(cur, battle_id)
    if not tier_name:
        raise RuntimeError(...)
    
    tier_id = resolve_tier_id(cur, tier_name)
    if tier_id is None:
        raise RuntimeError(f"Tier '{tier_name}' not found in BF_Tier")

    # -------------------------
    # IMPORTANT: Adjust the JOIN here to match your BF schema.
    # -------------------------
    #
    # Goal: Insert (BattlePokemonId, BF_SetId) for all sets matching (tier_id, species).
    #
    # Common patterns:
    #   BF_TierPokemon(TierPokemonId, TierId, PokemonName)
    #   BF_Set(SetId, TierPokemonId, ...)
    #
    # If your column names differ, adjust below.
    #
    cur.execute("""
        INSERT INTO dbo.SetCandidates (BattlePokemonId, BF_SetId)
        SELECT BP.BattlePokemonId, S.SetId
        FROM dbo.BattlePokemon BP
        JOIN dbo.BF_Set S
        ON S.Species =
            CASE
            WHEN BP.Species IN ('Zacian-Crowned', 'Zamazenta-Crowned')
                THEN BP.BattleName
            ELSE BP.Species
            END
        JOIN dbo.BF_TierPokemon TP
        ON TP.TierPokemonId = S.TierPokemonId
        AND TP.TierId = ?
        WHERE BP.BattleId = ?
        AND NOT EXISTS (
                SELECT 1
                FROM dbo.SetCandidates SC
                WHERE SC.BattlePokemonId = BP.BattlePokemonId
                AND SC.BF_SetId = S.SetId
        );
    """, tier_id, battle_id)
    
    cur.execute("""
        UPDATE dbo.Battles SET TierId = ? WHERE BattleId = ?;
    """, tier_id, battle_id)
    
    cur.execute("EXEC dbo.ApplySetCandidateEliminations @BattleId = ?;", battle_id)
    
    # SQL Server doesn't always give accurate rowcount with some settings,
    # but cursor.rowcount is usually good here.
    return cur.rowcount if cur.rowcount != -1 else 0

def get_recent_battles(cur, limit: int = 200) -> List[str]:
    cur.execute(f"SELECT TOP ({int(limit)}) BattleId FROM dbo.Battles ORDER BY CreatedAt DESC;")
    return [r[0] for r in cur.fetchall()]

def main(mode: str = "skip", limit: int = 200) -> None:
    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    try:
        cur = cn.cursor()
        battle_ids = get_recent_battles(cur, limit=limit)

        for i, battle_id in enumerate(battle_ids, start=1):
            # Only BF gen9 battles (defensive)
            if not battle_id.lower().startswith("gen9battlefactory-"):
                cn.rollback()
                continue

            try:
                n = init_candidates_for_battle(cur, battle_id, mode=mode)
                cn.commit()
                if n == 0 and mode == "skip":
                    print(f"[{i}/{len(battle_ids)}] Skipped {battle_id} (already initialized)")
                else:
                    print(f"[{i}/{len(battle_ids)}] Initialized {battle_id}: inserted {n} candidates")
            except Exception as e:
                cn.rollback()
                print(f"[{i}/{len(battle_ids)}] FAILED {battle_id}: {e}")
    finally:
        cn.close()

if __name__ == "__main__":
    import sys
    mode = "skip"
    lim = 200
    if len(sys.argv) >= 2:
        mode = sys.argv[1].strip().lower()
    if len(sys.argv) >= 3:
        lim = int(sys.argv[2])
    if mode not in ("skip", "replace"):
        print("Mode must be 'skip' or 'replace'")
        raise SystemExit(2)
    main(mode=mode, limit=lim)
