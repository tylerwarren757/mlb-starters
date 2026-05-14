#!/usr/bin/env python3
"""Fetch MLB probable starters two days out with ESPN ownership, flags, and recent starts.

Usage:
  python starters.py          # print to terminal
  python starters.py --json   # write docs/data.json
"""

import json
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

TARGET_DATE = date.today() + timedelta(days=2)
MLB_BASE = "https://statsapi.mlb.com/api/v1"
ESPN_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/2026/players"
AVAILABLE_THRESHOLD = 65.0
RECENT_STARTS = 5
JSON_OUT = Path(__file__).parent / "docs" / "data.json"

COUNTRY_FLAGS = {
    "USA": "🇺🇸",
    "Dominican Republic": "🇩🇴",
    "Venezuela": "🇻🇪",
    "Cuba": "🇨🇺",
    "Panama": "🇵🇦",
    "Mexico": "🇲🇽",
    "Japan": "🇯🇵",
    "South Korea": "🇰🇷",
    "Korea, South": "🇰🇷",
    "Puerto Rico": "🇵🇷",
    "Canada": "🇨🇦",
    "Colombia": "🇨🇴",
    "Nicaragua": "🇳🇮",
    "Netherlands": "🇳🇱",
    "Curacao": "🇨🇼",
    "Australia": "🇦🇺",
    "Brazil": "🇧🇷",
    "Taiwan": "🇹🇼",
    "Germany": "🇩🇪",
    "Honduras": "🇭🇳",
    "Aruba": "🇦🇼",
    "Italy": "🇮🇹",
    "Costa Rica": "🇨🇷",
    "Jamaica": "🇯🇲",
    "Bahamas": "🇧🇸",
    "United Kingdom": "🇬🇧",
    "Spain": "🇪🇸",
    "France": "🇫🇷",
}


def fetch_mlb(path):
    url = f"{MLB_BASE}{path}"
    with urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def normalize(name):
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()


def get_probable_starters(game_date):
    data = fetch_mlb(f"/schedule?sportId=1&date={game_date}&hydrate=probablePitcher")
    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            away = game["teams"]["away"]
            home = game["teams"]["home"]
            def extract(side):
                p = side.get("probablePitcher")
                if not p:
                    return None
                return {"id": p["id"], "name": p["fullName"]}
            games.append({
                "away_team": away["team"]["name"],
                "home_team": home["team"]["name"],
                "away_pitcher": extract(away),
                "home_pitcher": extract(home),
                "game_time_utc": game.get("gameDate", ""),
            })
    return games


def fetch_player_details(player_id, season):
    """Return (birth_country, recent_starts_list) for a player."""
    try:
        person = fetch_mlb(f"/people/{player_id}")
        p = person.get("people", [{}])[0]
        country = p.get("birthCountry", "")
    except Exception:
        country = ""

    recent = []
    try:
        logs = fetch_mlb(
            f"/people/{player_id}/stats"
            f"?stats=gameLog&group=pitching&season={season}&gameType=R"
        )
        splits = []
        for stat_block in logs.get("stats", []):
            splits = stat_block.get("splits", [])
        starts = [s for s in splits if s.get("stat", {}).get("gamesStarted", 0) > 0]
        for s in starts[-RECENT_STARTS:]:
            stat = s.get("stat", {})
            opp_name = s.get("opponent", {}).get("name", "")
            opp = opp_name.split()[-1] if opp_name else "?"
            is_home = s.get("isHome", False)
            matchup = f"vs {opp}" if is_home else f"@ {opp}"
            recent.append({
                "date": s.get("date", ""),
                "matchup": matchup,
                "ip": stat.get("inningsPitched", ""),
                "h": stat.get("hits", ""),
                "er": stat.get("earnedRuns", ""),
                "bb": stat.get("baseOnBalls", ""),
                "k": stat.get("strikeOuts", ""),
                "result": "W" if stat.get("wins") else "L" if stat.get("losses") else "ND",
            })
    except Exception:
        pass

    return country, recent


def get_espn_ownership():
    if not HAS_REQUESTS:
        print("Warning: 'requests' not installed — skipping ESPN lookup.", file=sys.stderr)
        return {}
    headers = {
        "X-Fantasy-Filter": json.dumps({
            "limit": 1000,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }),
        "User-Agent": "Mozilla/5.0",
    }
    params = {"scoringPeriodId": TARGET_DATE.timetuple().tm_yday, "view": "kona_player_info"}
    try:
        resp = _requests.get(ESPN_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return {
            normalize(p["fullName"]): p["ownership"]["percentOwned"]
            for p in resp.json()
            if isinstance(p, dict) and "ownership" in p
        }
    except Exception as e:
        print(f"Warning: ESPN lookup failed — {e}", file=sys.stderr)
        return {}


def enrich_pitchers(games, ownership):
    """Parallel-fetch player details for all known pitchers."""
    season = TARGET_DATE.year
    pitchers = {}
    for g in games:
        for side in ("away_pitcher", "home_pitcher"):
            p = g[side]
            if p and p["id"] not in pitchers:
                pitchers[p["id"]] = p["name"]

    details = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_player_details, pid, season): pid for pid in pitchers}
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                country, recent = fut.result()
            except Exception:
                country, recent = "", []
            details[pid] = {"birth_country": country, "flag": COUNTRY_FLAGS.get(country, ""), "recent_starts": recent}

    return details


def build_data(games, ownership, details):
    enriched_games = []
    available = []

    for g in games:
        row = {
            "away_team": g["away_team"],
            "home_team": g["home_team"],
            "game_time_utc": g["game_time_utc"],
        }
        for side in ("away", "home"):
            p = g[f"{side}_pitcher"]
            if p:
                pct = ownership.get(normalize(p["name"]))
                d = details.get(p["id"], {})
                pitcher_obj = {
                    "id": p["id"],
                    "name": p["name"],
                    "flag": d.get("flag", ""),
                    "birth_country": d.get("birth_country", ""),
                    "ownership": round(pct, 1) if pct is not None else None,
                    "recent_starts": d.get("recent_starts", []),
                }
                row[f"{side}_pitcher"] = pitcher_obj
                if pct is not None and pct < AVAILABLE_THRESHOLD:
                    available.append({
                        "name": p["name"],
                        "flag": d.get("flag", ""),
                        "pct_owned": round(pct, 1),
                        "matchup": f"{g['away_team']} @ {g['home_team']}",
                    })
            else:
                row[f"{side}_pitcher"] = None
        enriched_games.append(row)

    available.sort(key=lambda x: x["pct_owned"])

    return {
        "date": TARGET_DATE.isoformat(),
        "date_label": TARGET_DATE.strftime("%A, %B %d, %Y"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "availability_threshold": AVAILABLE_THRESHOLD,
        "games": enriched_games,
        "available": available,
    }


def print_terminal(data):
    print(f"MLB Probable Starters — {data['date_label']}")
    print(f"Ownership threshold: <{data['availability_threshold']:.0f}% = likely available\n")

    for g in data["games"]:
        time_str = g["game_time_utc"][11:16] + " UTC" if g["game_time_utc"] else ""
        ap = g["away_pitcher"]
        hp = g["home_pitcher"]
        away_label = f"{ap['flag']} {ap['name']}" if ap else "TBD"
        home_label = f"{hp['flag']} {hp['name']}" if hp else "TBD"
        print(f"  {g['away_team']:30s} @ {g['home_team']}")
        print(f"  {away_label:32s}   {home_label}")
        if time_str:
            print(f"  {time_str}")
        print()

    if data["available"]:
        print("─" * 60)
        print(f"LIKELY AVAILABLE (owned <{data['availability_threshold']:.0f}% on ESPN)\n")
        for p in data["available"]:
            label = f"{p['flag']} {p['name']}" if p["flag"] else p["name"]
            print(f"  {label:30s}  {p['pct_owned']:5.1f}%  |  {p['matchup']}")


def main():
    json_mode = "--json" in sys.argv

    try:
        games = get_probable_starters(TARGET_DATE.isoformat())
    except URLError as e:
        print(f"Error fetching MLB data: {e}", file=sys.stderr)
        sys.exit(1)

    if not games:
        print("No games scheduled.")
        return

    print("Fetching ownership + player details...", file=sys.stderr)
    ownership = get_espn_ownership()
    details = enrich_pitchers(games, ownership)
    data = build_data(games, ownership, details)

    if json_mode:
        JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
        JSON_OUT.write_text(json.dumps(data, indent=2))
        print(f"Wrote {JSON_OUT}")
    else:
        print_terminal(data)


if __name__ == "__main__":
    main()
