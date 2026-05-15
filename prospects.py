#!/usr/bin/env python3
"""Fetch prospect pipeline: IL call-up candidates and top AAA performers.

Usage:
  python prospects.py          # print to terminal
  python prospects.py --json   # write docs/prospects.json
"""

import json
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

SEASON = date.today().year
MLB_BASE = "https://statsapi.mlb.com/api/v1"
JSON_OUT = Path(__file__).parent / "docs" / "prospects.json"

TOP_PROSPECT_OPS = 0.900   # AAA OPS threshold to flag as elite hitter
TOP_PROSPECT_ERA = 3.00    # AAA ERA threshold to flag as elite pitcher
TOP_PROSPECT_AGE = 24      # Max age to flag as "likely top prospect"
MIN_PA = 50                # Min plate appearances for hitter rankings
MIN_IP = 20                # Min innings pitched for pitcher rankings

COUNTRY_FLAGS = {
    "USA": "🇺🇸", "Dominican Republic": "🇩🇴", "Venezuela": "🇻🇪",
    "Cuba": "🇨🇺", "Panama": "🇵🇦", "Mexico": "🇲🇽", "Japan": "🇯🇵",
    "South Korea": "🇰🇷", "Korea, South": "🇰🇷", "Puerto Rico": "🇵🇷",
    "Canada": "🇨🇦", "Colombia": "🇨🇴", "Nicaragua": "🇳🇮",
    "Netherlands": "🇳🇱", "Curacao": "🇨🇼", "Australia": "🇦🇺",
    "Brazil": "🇧🇷", "Taiwan": "🇹🇼", "Germany": "🇩🇪",
    "Honduras": "🇭🇳", "Aruba": "🇦🇼", "Italy": "🇮🇹",
    "Dominican Rep.": "🇩🇴",
}

IL_STATUSES = {"Injured 10-Day", "Injured 15-Day", "Injured 60-Day", "Injured 7-Day"}


def fetch_mlb(path):
    url = f"{MLB_BASE}{path}"
    with urlopen(url, timeout=12) as resp:
        return json.loads(resp.read())


def get_all_teams():
    d = fetch_mlb(f"/teams?sportId=1&season={SEASON}")
    return [(t["id"], t["name"], t.get("abbreviation", "")) for t in d["teams"]]


def get_team_40man(team_id):
    d = fetch_mlb(f"/teams/{team_id}/roster?rosterType=40Man&season={SEASON}&hydrate=person")
    il, minors = [], []
    for p in d.get("roster", []):
        status = p["status"]["description"]
        person = p["person"]
        entry = {
            "id": person["id"],
            "name": person["fullName"],
            "position": p["position"]["abbreviation"],
            "position_type": p["position"]["type"],
            "status": status,
            "birth_country": person.get("birthCountry", ""),
            "age": person.get("currentAge"),
            "debut_date": person.get("mlbDebutDate"),
        }
        if status in IL_STATUSES:
            il.append(entry)
        elif status == "Reassigned to Minors":
            minors.append(entry)
    return il, minors


def get_aaa_stats(player_id, position_type):
    """Return hitting or pitching season stats at AAA level."""
    group = "pitching" if position_type == "Pitcher" else "hitting"
    try:
        d = fetch_mlb(
            f"/people/{player_id}/stats"
            f"?stats=season&group={group}&season={SEASON}&gameType=R&sportId=11"
        )
        for block in d.get("stats", []):
            splits = block.get("splits", [])
            if splits:
                return group, splits[0]["stat"]
    except Exception:
        pass
    return group, None


def get_top_aaa_performers():
    """Fetch top AAA hitters and pitchers by aggregate season stats."""
    hitters, pitchers = [], []
    try:
        d = fetch_mlb(
            f"/stats?stats=season&group=hitting&gameType=R&sportId=11"
            f"&season={SEASON}&limit=100&sortStat=onBasePlusSlugging&order=desc"
        )
        for s in d.get("stats", [{}])[0].get("splits", []):
            stat = s["stat"]
            if stat.get("plateAppearances", 0) < MIN_PA:
                continue
            p = s["player"]
            team = s.get("team", {})
            hitters.append({
                "id": p["id"],
                "name": p["fullName"],
                "team": team.get("name", ""),
                "ops": stat.get("ops", ".000"),
                "avg": stat.get("avg", ".000"),
                "hr": stat.get("homeRuns", 0),
                "sb": stat.get("stolenBases", 0),
                "pa": stat.get("plateAppearances", 0),
                "age": stat.get("age"),
            })
    except Exception as e:
        print(f"Warning: AAA hitter stats failed — {e}", file=sys.stderr)

    try:
        d = fetch_mlb(
            f"/stats?stats=season&group=pitching&gameType=R&sportId=11"
            f"&season={SEASON}&limit=100&sortStat=strikeoutsPer9Inn&order=desc"
        )
        for s in d.get("stats", [{}])[0].get("splits", []):
            stat = s["stat"]
            ip_str = stat.get("inningsPitched", "0.0")
            try:
                ip = float(ip_str)
            except ValueError:
                ip = 0
            if ip < MIN_IP:
                continue
            if stat.get("gamesStarted", 0) == 0:
                continue
            p = s["player"]
            team = s.get("team", {})
            pitchers.append({
                "id": p["id"],
                "name": p["fullName"],
                "team": team.get("name", ""),
                "era": stat.get("era", "-.--"),
                "ip": ip_str,
                "k9": stat.get("strikeoutsPer9Inn", "0.00"),
                "whip": stat.get("whip", "-.--"),
                "gs": stat.get("gamesStarted", 0),
                "age": stat.get("age"),
            })
    except Exception as e:
        print(f"Warning: AAA pitcher stats failed — {e}", file=sys.stderr)

    return hitters[:20], pitchers[:15]


def likely_top_prospect(player, stat, group):
    """Heuristic flag: young + no MLB debut + strong performance."""
    if player.get("debut_date"):
        return False  # already debuted, not a prospect
    age = player.get("age") or 99
    if age > TOP_PROSPECT_AGE:
        return False
    if stat is None:
        return age <= 22  # young with no stats = still might be top prospect
    if group == "hitting":
        try:
            ops = float(stat.get("ops", "0"))
        except ValueError:
            ops = 0
        return ops >= TOP_PROSPECT_OPS or age <= 21
    else:
        try:
            era = float(stat.get("era", "99"))
        except ValueError:
            era = 99
        return era <= TOP_PROSPECT_ERA or age <= 21


def build_data():
    print("Fetching all 30 teams...", file=sys.stderr)
    teams = get_all_teams()

    print("Fetching 40-man rosters...", file=sys.stderr)
    team_data = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(get_team_40man, tid): (tid, name, abbr) for tid, name, abbr in teams}
        for fut in as_completed(futures):
            tid, name, abbr = futures[fut]
            try:
                il, minors = fut.result()
                team_data[tid] = {"name": name, "abbr": abbr, "il": il, "minors": minors}
            except Exception as e:
                print(f"Warning: {name} roster failed — {e}", file=sys.stderr)

    # Fetch AAA stats for all "Reassigned to Minors" players
    all_candidates = []
    for tid, td in team_data.items():
        for p in td["minors"]:
            all_candidates.append((tid, p))

    print(f"Fetching AAA stats for {len(all_candidates)} call-up candidates...", file=sys.stderr)
    candidate_stats = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(get_aaa_stats, p["id"], p["position_type"]): p["id"]
                   for _, p in all_candidates}
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                group, stat = fut.result()
                candidate_stats[pid] = (group, stat)
            except Exception:
                candidate_stats[pid] = ("hitting", None)

    # Build IL opportunity cards
    il_opportunities = []
    for tid, td in sorted(team_data.items(), key=lambda x: x[1]["name"]):
        if not td["il"] or not td["minors"]:
            continue
        il_players = td["il"]
        candidates = []
        for p in td["minors"]:
            group, stat = candidate_stats.get(p["id"], ("hitting", None))
            flag = COUNTRY_FLAGS.get(p["birth_country"], "")
            is_prospect = likely_top_prospect(p, stat, group)
            cand = {
                "id": p["id"],
                "name": p["name"],
                "flag": flag,
                "position": p["position"],
                "position_type": p["position_type"],
                "age": p["age"],
                "debut_date": p["debut_date"],
                "likely_top_prospect": is_prospect,
                "stat_group": group,
                "stats": stat,
            }
            candidates.append(cand)
        if candidates:
            il_opportunities.append({
                "team": td["name"],
                "abbr": td["abbr"],
                "il_players": il_players,
                "candidates": candidates,
            })

    # Top AAA performers (league-wide)
    print("Fetching top AAA performers...", file=sys.stderr)
    top_hitters, top_pitchers = get_top_aaa_performers()

    # Enrich top performers with debut status (parallel)
    all_top_ids = {p["id"] for p in top_hitters + top_pitchers}
    debut_map = {}

    def get_debut(pid):
        try:
            d = fetch_mlb(f"/people/{pid}")
            person = d.get("people", [{}])[0]
            return pid, person.get("mlbDebutDate"), person.get("birthCountry", ""), person.get("currentAge")
        except Exception:
            return pid, None, "", None

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(get_debut, pid): pid for pid in all_top_ids}
        for fut in as_completed(futures):
            pid, debut, country, age = fut.result()
            debut_map[pid] = {"debut": debut, "country": country, "age": age}

    def enrich_top(player, stat_key, threshold_key):
        info = debut_map.get(player["id"], {})
        player["flag"] = COUNTRY_FLAGS.get(info.get("country", ""), "")
        player["debut_date"] = info.get("debut")
        player["likely_top_prospect"] = (
            not info.get("debut")
            and (info.get("age") or 99) <= TOP_PROSPECT_AGE
        )
        return player

    top_hitters = [enrich_top(p, "ops", TOP_PROSPECT_OPS) for p in top_hitters]
    top_pitchers = [enrich_top(p, "era", TOP_PROSPECT_ERA) for p in top_pitchers]

    return {
        "date": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "season": SEASON,
        "il_opportunities": il_opportunities,
        "top_aaa_hitters": top_hitters,
        "top_aaa_pitchers": top_pitchers,
        "notes": {
            "top_prospect_flag": f"Flagged if age ≤ {TOP_PROSPECT_AGE} and no MLB debut",
            "il_candidates": "Players on team's 40-man roster reassigned to minors",
            "rankings_source": "No free prospect rankings API available; using age + performance heuristics",
        },
    }


def print_terminal(data):
    print(f"Prospect Pipeline — {data['date']}\n")
    print("── IL CALL-UP OPPORTUNITIES ──────────────────────")
    for opp in data["il_opportunities"][:10]:
        il_names = ", ".join(f"{p['name']} ({p['position']})" for p in opp["il_players"])
        print(f"\n{opp['team']} | IL: {il_names}")
        for c in opp["candidates"]:
            prospect_tag = " ★" if c["likely_top_prospect"] else ""
            stat = c["stats"] or {}
            if c["stat_group"] == "hitting":
                stat_str = f"OPS {stat.get('ops','?')} HR {stat.get('homeRuns','?')}"
            else:
                stat_str = f"ERA {stat.get('era','?')} {stat.get('inningsPitched','?')}IP"
            debut = "prospect" if not c["debut_date"] else "vet"
            print(f"  {c['flag']} {c['name']:26s} {c['position']:3s} {stat_str}  {debut}{prospect_tag}")

    print("\n── TOP AAA HITTERS ───────────────────────────────")
    for p in data["top_aaa_hitters"][:10]:
        tag = " ★" if p["likely_top_prospect"] else ""
        print(f"  {p['flag']} {p['name']:26s} {p['team']:25s} OPS {p['ops']} HR {p['hr']}{tag}")

    print("\n── TOP AAA PITCHERS (starters) ───────────────────")
    for p in data["top_aaa_pitchers"][:10]:
        tag = " ★" if p["likely_top_prospect"] else ""
        print(f"  {p['flag']} {p['name']:26s} {p['team']:25s} ERA {p['era']} {p['ip']}IP{tag}")


def main():
    json_mode = "--json" in sys.argv
    try:
        data = build_data()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
        JSON_OUT.write_text(json.dumps(data, indent=2))
        print(f"Wrote {JSON_OUT}")
    else:
        print_terminal(data)


if __name__ == "__main__":
    main()
