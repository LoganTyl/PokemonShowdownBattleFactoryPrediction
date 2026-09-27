import re
import time
from typing import Dict, List, Optional, Tuple
import requests
import pyodbc

from app_config import CONN_STR

TURN_RE = re.compile(r"^\|turn\|(\d+)\s*$")

def normalize_replay_json_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    # Accept either .../battleid or .../battleid.json
    if not url.endswith(".json"):
        url += ".json"
    return url

def battle_id_from_url(json_url: str) -> str:
    # https://replay.pokemonshowdown.com/gen9battlefactory-2261490510.json -> gen9battlefactory-2261490510
    last = json_url.rstrip("/").split("/")[-1]
    if last.endswith(".json"):
        last = last[:-5]
    return last

def fetch_replay_json(json_url: str, timeout_s: int = 30) -> Dict:
    # Pokemon Showdown sets permissive CORS for these endpoints; plain GET is fine.
    r = requests.get(json_url, timeout=timeout_s)
    r.raise_for_status()
    return r.json()

def parse_log_lines(log_text: str) -> List[str]:
    # Keep empty lines out; PS logs are newline separated
    return [ln for ln in (log_text or "").split("\n") if ln != ""]

def infer_line_type(line_text: str) -> Optional[str]:
    # Protocol lines start with |type|...
    if not line_text.startswith("|"):
        return None
    # Example: |move|p1a: Pikachu|Thunderbolt|p2a: ...
    parts = line_text.split("|")
    # parts[0] is '' because line starts with |
    if len(parts) > 1 and parts[1]:
        return parts[1][:32]
    return None

def assign_turn_numbers(lines: List[str]) -> List[Tuple[int, Optional[int], Optional[str], str]]:
    """
    Returns list of tuples:
      (line_num, turn_number, line_type, line_text)
    TurnNumber is set when we've seen |turn|N. Pre-turn/setup lines are NULL.
    """
    out = []
    cur_turn: Optional[int] = None
    for i, line in enumerate(lines, start=1):
        m = TURN_RE.match(line)
        if m:
            cur_turn = int(m.group(1))
        out.append((i, cur_turn, infer_line_type(line), line))
    return out

def build_turn_boundaries(tagged: List[Tuple[int, Optional[int], Optional[str], str]]) -> List[Tuple[int, int, int]]:
    """
    Returns list of (turn_number, start_line_num, end_line_num)
    Uses the first line where TurnNumber==N as start; end is last line before next turn.
    """
    # Collect min/max line per turn
    bounds: Dict[int, List[int]] = {}
    for line_num, turn_num, _, _ in tagged:
        if turn_num is None:
            continue
        bounds.setdefault(turn_num, []).append(line_num)

    turn_rows = []
    for turn_num in sorted(bounds.keys()):
        start = min(bounds[turn_num])
        end = max(bounds[turn_num])
        turn_rows.append((turn_num, start, end))
    return turn_rows

def upsert_battle(cur, battle_id: str, json_url: str, payload: Dict) -> None:
    # JSON fields: id, format, players, log, inputlog, etc.
    fmt = payload.get("format") or payload.get("formatid") or ""
    players = payload.get("players") or []
    p1 = players[0] if len(players) > 0 else None
    p2 = players[1] if len(players) > 1 else None
    raw_log = payload.get("log")
    input_log = payload.get("inputlog")

    # Upsert pattern: UPDATE if exists else INSERT
    cur.execute("""
        IF EXISTS (SELECT 1 FROM dbo.Battles WHERE BattleId = ?)
        BEGIN
            UPDATE dbo.Battles
            SET Format = COALESCE(NULLIF(?, ''), Format),
                Source = COALESCE(NULLIF(?, ''), Source),
                ReplayUrl = COALESCE(?, ReplayUrl),
                Player1Name = COALESCE(?, Player1Name),
                Player2Name = COALESCE(?, Player2Name),
                RawLog = COALESCE(?, RawLog),
                InputLog = COALESCE(?, InputLog)
            WHERE BattleId = ?;
        END
        ELSE
        BEGIN
            INSERT INTO dbo.Battles
            (BattleId, Format, Source, ReplayUrl, Player1Name, Player2Name, RawLog, InputLog)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        END
    """,
    battle_id,
    fmt, "replay_import", json_url, p1, p2, raw_log, input_log, battle_id,
    battle_id, fmt, "replay_import", json_url, p1, p2, raw_log, input_log
    )

def replace_battle_log_lines(cur, battle_id: str, tagged_lines: List[Tuple[int, Optional[int], Optional[str], str]]) -> None:
    # Simplest approach: delete then insert (safe because BattleId scopes it)
    cur.execute("DELETE FROM dbo.BattleLogLines WHERE BattleId = ?;", battle_id)

    rows = []
    for line_num, turn_num, line_type, line_text in tagged_lines:
        rows.append((battle_id, line_num, turn_num, line_type, line_text))

    cur.fast_executemany = True
    cur.executemany("""
        INSERT INTO dbo.BattleLogLines (BattleId, LineNum, TurnNumber, LineType, LineText)
        VALUES (?, ?, ?, ?, ?);
    """, rows)

def replace_turns(cur, battle_id: str, turn_bounds: List[Tuple[int, int, int]]) -> None:
    cur.execute("DELETE FROM dbo.Turns WHERE BattleId = ?;", battle_id)

    rows = [(battle_id, t, start, end) for (t, start, end) in turn_bounds]
    cur.fast_executemany = True
    cur.executemany("""
        INSERT INTO dbo.Turns (BattleId, TurnNumber, StartLineNum, EndLineNum)
        VALUES (?, ?, ?, ?);
    """, rows)

def import_one_replay(cur, json_url: str) -> str:
    json_url = normalize_replay_json_url(json_url)
    battle_id = battle_id_from_url(json_url)
    
    if battle_exists(cur, battle_id):
        return None

    payload = fetch_replay_json(json_url)
    log_text = payload.get("log") or ""
    lines = parse_log_lines(log_text)
    tagged = assign_turn_numbers(lines)
    bounds = build_turn_boundaries(tagged)

    upsert_battle(cur, battle_id, json_url, payload)
    replace_battle_log_lines(cur, battle_id, tagged)
    replace_turns(cur, battle_id, bounds)

    return battle_id

def read_urls_from_txt(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        urls = []
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            urls.append(raw)
        return urls
    
def battle_exists(cur, battle_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM dbo.Battles WHERE BattleId = ?;",
        battle_id
    )
    return cur.fetchone() is not None


def main(urls_txt_path: str, sleep_s: float = 0.2) -> None:
    urls = read_urls_from_txt(urls_txt_path)
    if not urls:
        print("No URLs found.")
        return

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False

    try:
        cur = cn.cursor()
        for i, url in enumerate(urls, start=1):
            try:
                battle_id = import_one_replay(cur, url)
                if battle_id is None:
                    cn.rollback()
                    print(f"[{i}/{len(urls)}] Skipped {battle_id} (already exists)")
                else:
                    cn.commit()
                    print(f"[{i}/{len(urls)}] Imported {battle_id}")
            except Exception as e:
                cn.rollback()
                print(f"[{i}/{len(urls)}] FAILED {url}: {e}")
            time.sleep(sleep_s)
    finally:
        cn.close()

if __name__ == "__main__":
    # Example usage:
    # python import_replays.py replays.txt
    import sys
    if len(sys.argv) < 2:
        print("Usage: python import_replays.py <replays.txt>")
        raise SystemExit(2)
    main(sys.argv[1])
