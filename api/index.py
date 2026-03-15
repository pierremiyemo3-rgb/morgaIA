#!/usr/bin/env python3
"""
MorgaIA v2 — Backend Flask
API Football + Gemini + SQLite + The Odds API
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import json
import math
import statistics
from database import init_db, save_analysis, get_history, get_analysis_by_id, delete_analysis
import google.generativeai as genai

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

FOOTBALL_KEY = "257ad93c80bd84a753ce6d9730bd01db"
FOOTBALL_URL = "https://v3.football.api-sports.io"
ODDS_KEY     = "6dbbca627d69321af1a1ae7a8435265b"
ODDS_URL     = "https://api.the-odds-api.com/v4"
SEASON       = 2025
PORT         = 8765

LIGUES_SUIVIES = [61, 62, 39, 140, 135, 78, 2, 3, 848, 94, 88, 144, 203, 253, 197, 345, 207, 206, 172]

# Mapping ligue ID -> sport key pour The Odds API
ODDS_SPORT_MAP = {
    39:  "soccer_epl",
    61:  "soccer_france_ligue_one",
    62:  "soccer_france_ligue_two",
    140: "soccer_spain_la_liga",
    135: "soccer_italy_serie_a",
    78:  "soccer_germany_bundesliga",
    2:   "soccer_uefa_champs_league",
    3:   "soccer_uefa_europa_league",
    848: "soccer_uefa_europa_conference_league",
    94:  "soccer_portugal_primeira_liga",
    88:  "soccer_netherlands_eredivisie",
}

app           = Flask(__name__)
CORS(app)
gemini_client = None

# ─────────────────────────────────────────────
# API FOOTBALL — APPELS
# ─────────────────────────────────────────────

def football_get(endpoint, params={}):
    headers = {
        "x-apisports-key": FOOTBALL_KEY,
        "Accept": "application/json"
    }
    r = requests.get(f"{FOOTBALL_URL}/{endpoint}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    # Log quota restant
    remaining = r.headers.get("x-ratelimit-requests-remaining", "?")
    limit = r.headers.get("x-ratelimit-requests-limit", "?")
    print(f"  API [{endpoint}] quota: {remaining}/{limit} restants")
    return data

def find_team(name):
    data = football_get("teams", {"search": name})
    if not data.get("response"):
        raise ValueError(f"Equipe '{name}' introuvable")
    return data["response"][0]["team"]

def fetch_fixtures(team_id):
    data = football_get("fixtures", {"team": team_id, "season": SEASON, "status": "FT"})
    if not data.get("response"):
        data = football_get("fixtures", {"team": team_id, "season": SEASON - 1, "status": "FT"})
    return data.get("response", [])

def fetch_h2h(id1, id2):
    data = football_get("fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": 20, "status": "FT"})
    return data.get("response", [])

def fetch_standings(league_id):
    data = football_get("standings", {"league": league_id, "season": SEASON})
    try:
        result = data["response"][0]["league"]["standings"][0]
        if result:
            return result
    except:
        pass
    # Fallback saison precedente
    try:
        data2 = football_get("standings", {"league": league_id, "season": SEASON - 1})
        return data2["response"][0]["league"]["standings"][0]
    except:
        return []

def fetch_top_scorers(league_id):
    data = football_get("players/topscorers", {"league": league_id, "season": SEASON})
    result = data.get("response", [])
    if not result:
        data2 = football_get("players/topscorers", {"league": league_id, "season": SEASON - 1})
        result = data2.get("response", [])
    return result

def fetch_top_assists(league_id):
    data = football_get("players/topassists", {"league": league_id, "season": SEASON})
    result = data.get("response", [])
    if not result:
        data2 = football_get("players/topassists", {"league": league_id, "season": SEASON - 1})
        result = data2.get("response", [])
    return result

def fetch_team_stats(team_id, league_id):
    def has_data(resp):
        """Vérifie que la réponse contient des vraies données utiles."""
        if not resp:
            return False
        shots = resp.get("shots") or {}
        goals = resp.get("goals") or {}
        fp    = resp.get("fixtures", {}).get("played", {})
        games = fp.get("total") or fp.get("home", 0) or 0
        shots_total = shots.get("total") or 0
        goals_total = goals.get("for", {}).get("total", {}).get("total") or 0
        return games > 0 or shots_total > 0 or goals_total > 0

    # Essayer saison courante
    data = football_get("teams/statistics", {"team": team_id, "league": league_id, "season": SEASON})
    resp = data.get("response", {})
    if has_data(resp):
        return resp

    # Fallback saison precedente
    data2 = football_get("teams/statistics", {"team": team_id, "league": league_id, "season": SEASON - 1})
    resp2 = data2.get("response", {})
    if has_data(resp2):
        print(f"  [fallback] stats saison {SEASON-1} pour team {team_id}")
        return resp2

    return resp  # Retourner quand même même si vide

# ─────────────────────────────────────────────
# THE ODDS API — COTES BOOKMAKERS
# ─────────────────────────────────────────────

def fetch_odds(home_name, away_name, league_id):
    try:
        sport_key = ODDS_SPORT_MAP.get(league_id)
        if not sport_key:
            return None

        r = requests.get(
            f"{ODDS_URL}/sports/{sport_key}/odds",
            params={
                "apiKey":     ODDS_KEY,
                "regions":    "eu",
                "markets":    "h2h",
                "oddsFormat": "decimal"
            },
            timeout=15
        )
        if r.status_code != 200:
            return None

        events  = r.json()
        hl      = home_name.lower()
        al      = away_name.lower()

        for ev in events:
            ht = ev.get("home_team", "").lower()
            at = ev.get("away_team", "").lower()
            if not (any(w in ht for w in hl.split()[:2]) and
                    any(w in at for w in al.split()[:2])):
                continue

            h_odds, d_odds, a_odds = [], [], []
            for bk in ev.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    if mkt["key"] == "h2h":
                        for outcome in mkt["outcomes"]:
                            n = outcome["name"].lower()
                            o = outcome["price"]
                            if any(w in n for w in ht.split()[:1]):
                                h_odds.append(o)
                            elif any(w in n for w in at.split()[:1]):
                                a_odds.append(o)
                            else:
                                d_odds.append(o)

            if not h_odds:
                continue

            avg_h = sum(h_odds) / len(h_odds)
            avg_d = sum(d_odds) / len(d_odds) if d_odds else 3.5
            avg_a = sum(a_odds) / len(a_odds)

            raw_h = 1 / avg_h
            raw_d = 1 / avg_d
            raw_a = 1 / avg_a
            total = raw_h + raw_d + raw_a

            return {
                "home_odds":  round(avg_h, 2),
                "draw_odds":  round(avg_d, 2),
                "away_odds":  round(avg_a, 2),
                "impl_home":  round(raw_h / total * 100, 1),
                "impl_draw":  round(raw_d / total * 100, 1),
                "impl_away":  round(raw_a / total * 100, 1),
                "bookmakers": len(ev.get("bookmakers", [])),
                "margin":     round((total - 1) * 100, 2)
            }
    except Exception as e:
        print(f"Erreur odds: {e}")
    return None

# ─────────────────────────────────────────────
# JOUEURS PAR EQUIPE
# ─────────────────────────────────────────────

def team_scorers(team_id, league_id, n=3):
    players = []
    for entry in fetch_top_scorers(league_id):
        p = entry.get("player", {})
        for s in entry.get("statistics", []):
            if s.get("team", {}).get("id") == team_id:
                shots  = s.get("shots", {})
                goals  = s.get("goals", {}).get("total") or 0
                games  = s.get("games", {}).get("appearences") or 1
                total  = shots.get("total") or 0
                on     = shots.get("on") or 0
                players.append({
                    "name":      p.get("name", "-"),
                    "photo":     p.get("photo", ""),
                    "goals":     goals,
                    "assists":   s.get("goals", {}).get("assists") or 0,
                    "games":     games,
                    "shots":     total,
                    "on_target": on,
                    "accuracy":  round(on / total * 100) if total else 0,
                    "per_game":  round(goals / games, 2)
                })
                break
    return sorted(players, key=lambda x: -x["goals"])[:n]

def team_assisters(team_id, league_id, n=3):
    players = []
    for entry in fetch_top_assists(league_id):
        p = entry.get("player", {})
        for s in entry.get("statistics", []):
            if s.get("team", {}).get("id") == team_id:
                shots   = s.get("shots", {})
                assists = s.get("goals", {}).get("assists") or 0
                total   = shots.get("total") or 0
                on      = shots.get("on") or 0
                players.append({
                    "name":      p.get("name", "-"),
                    "photo":     p.get("photo", ""),
                    "assists":   assists,
                    "goals":     s.get("goals", {}).get("total") or 0,
                    "games":     s.get("games", {}).get("appearences") or 1,
                    "shots":     total,
                    "on_target": on,
                    "accuracy":  round(on / total * 100) if total else 0,
                })
                break
    return sorted(players, key=lambda x: -x["assists"])[:n]

def team_shots(team_id, league_id):
    ts    = fetch_team_stats(team_id, league_id)
    shots = ts.get("shots", {})
    fp    = ts.get("fixtures", {}).get("played", {})
    games = fp.get("total") or (fp.get("home", 0) + fp.get("away", 0)) or 1
    total = shots.get("total") or 0
    on    = shots.get("on") or 0
    return {
        "total":       total,
        "on_target":   on,
        "accuracy":    round(on / total * 100) if total else 0,
        "per_game":    round(total / games, 1),
        "on_per_game": round(on / games, 1),
        "games":       games
    }

# ─────────────────────────────────────────────
# STYLE DE JEU
# ─────────────────────────────────────────────

def analyze_style(team_id, league_id):
    try:
        ts      = fetch_team_stats(team_id, league_id)
        fp      = ts.get("fixtures", {}).get("played", {})
        games   = fp.get("total") or 1
        gf      = float(ts.get("goals", {}).get("for", {}).get("average", {}).get("total") or 0)
        ga      = float(ts.get("goals", {}).get("against", {}).get("average", {}).get("total") or 0)
        cs      = ts.get("clean_sheet", {}).get("total") or 0
        fs      = ts.get("failed_to_score", {}).get("total") or 0
        shots   = ts.get("shots", {})
        sh_tot  = shots.get("total") or 0
        sh_on   = shots.get("on") or 0

        if gf >= 2.0 and ga >= 1.5:
            style = "Offensif ouvert"
        elif gf >= 1.8 and ga <= 1.0:
            style = "Offensif solide"
        elif gf <= 1.2 and ga <= 1.0:
            style = "Defensif"
        elif gf <= 1.2 and ga >= 1.5:
            style = "Defensif fragile"
        else:
            style = "Equilibre"

        return {
            "style":         style,
            "goals_for_avg": round(gf, 2),
            "goals_ag_avg":  round(ga, 2),
            "clean_sheets":  cs,
            "cs_pct":        round(cs / games * 100, 1),
            "failed_score":  fs,
            "shots_pg":      round(sh_tot / games, 1),
            "accuracy":      round(sh_on / sh_tot * 100, 1) if sh_tot else 0
        }
    except:
        return {"style": "Inconnu"}

# ─────────────────────────────────────────────
# CONTEXTE DU MATCH
# ─────────────────────────────────────────────

def match_context(rank_home, rank_away, home_fx, away_fx):
    from datetime import datetime, timedelta
    context = {}

    if rank_home and rank_away:
        rh = rank_home.get("rank", 10)
        ra = rank_away.get("rank", 10)
        if rh <= 4 or ra <= 4:
            context["enjeu"] = "Match au sommet"
        elif rh >= 15 or ra >= 15:
            context["enjeu"] = "Match maintien"
        else:
            context["enjeu"] = "Match milieu de tableau"
        context["rank_gap"] = abs(rh - ra)
        context["pts_home"] = rank_home.get("points", 0)
        context["pts_away"] = rank_away.get("points", 0)
    else:
        context["enjeu"] = "Inconnu"

    now      = datetime.now()
    week_ago = now - timedelta(days=7)

    def recent_games(fx):
        count = 0
        for f in fx:
            try:
                d = datetime.fromisoformat(f["fixture"]["date"][:10])
                if d >= week_ago:
                    count += 1
            except:
                pass
        return count

    context["home_recent_games"] = recent_games(home_fx)
    context["away_recent_games"] = recent_games(away_fx)
    context["home_fatigue"] = "Possible fatigue" if context["home_recent_games"] >= 2 else "Repose"
    context["away_fatigue"] = "Possible fatigue" if context["away_recent_games"] >= 2 else "Repose"

    return context

# ─────────────────────────────────────────────
# MOTEUR STATISTIQUE
# ─────────────────────────────────────────────

def mean(lst):
    return round(statistics.mean(lst), 2) if lst else 0.0

def goals_split(fixtures, team_id, venue="all"):
    scored, conceded = [], []
    for f in fixtures:
        is_home = f["teams"]["home"]["id"] == team_id
        is_away = f["teams"]["away"]["id"] == team_id
        if not is_home and not is_away:
            continue
        if venue == "home" and not is_home:
            continue
        if venue == "away" and not is_away:
            continue
        hg = f["goals"]["home"] or 0
        ag = f["goals"]["away"] or 0
        if is_home:
            scored.append(hg); conceded.append(ag)
        else:
            scored.append(ag); conceded.append(hg)
    return scored, conceded

def poisson_prob(lam, k):
    return (lam ** k * math.exp(-lam)) / math.factorial(k)

def over_pct(lst, threshold):
    if not lst:
        return 50.0
    return round(len([g for g in lst if g > threshold]) / len(lst) * 100, 1)

def bts_pct(fixtures):
    if not fixtures:
        return 50.0
    n = sum(1 for f in fixtures
            if (f["goals"]["home"] or 0) > 0 and (f["goals"]["away"] or 0) > 0)
    return round(n / len(fixtures) * 100, 1)

def calc_probabilities(hfx, afx, h2h, hid, aid):
    hS, hC = goals_split(hfx, hid, "home")
    aS, aC = goals_split(afx, aid, "away")

    def win_rate(s, c):
        return sum(1 for x, y in zip(s, c) if x > y) / len(s) if s else 0.33

    def draw_rate(s, c):
        return sum(1 for x, y in zip(s, c) if x == y) / len(s) if s else 0.25

    n   = len(h2h) or 1
    hhw = sum(1 for f in h2h
              if f["teams"]["home"]["id"] == hid
              and (f["goals"]["home"] or 0) > (f["goals"]["away"] or 0))
    aaw = sum(1 for f in h2h
              if f["teams"]["away"]["id"] == aid
              and (f["goals"]["away"] or 0) > (f["goals"]["home"] or 0))

    rH  = win_rate(hS, hC) * 0.5 + (hhw / n) * 0.3 + 0.08
    rA  = win_rate(aS, aC) * 0.5 + (aaw / n) * 0.3
    rD  = (draw_rate(hS, hC) + draw_rate(aS, aC)) / 2 * 0.5 + 0.15
    tot = rH + rA + rD

    return {
        "home": round(rH / tot * 100, 1),
        "draw": round(rD / tot * 100, 1),
        "away": round(rA / tot * 100, 1)
    }

def find_in_standings(standings, team_id):
    for s in standings:
        if s["team"]["id"] == team_id:
            return s
    return None

# ─────────────────────────────────────────────
# 5 MODELES DE SCORE EXACT
# ─────────────────────────────────────────────

def model_poisson(xH, xA):
    scores = []
    for h in range(8):
        for a in range(8):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH, h) * poisson_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"])

def model_dixon_coles(xH, xA):
    rho = -0.13
    def tau(x, y, lam, mu, r):
        if   x == 0 and y == 0: return 1 - lam * mu * r
        elif x == 0 and y == 1: return 1 + lam * r
        elif x == 1 and y == 0: return 1 + mu * r
        elif x == 1 and y == 1: return 1 - r
        return 1.0
    scores = []
    for h in range(8):
        for a in range(8):
            p = poisson_prob(xH, h) * poisson_prob(xA, a) * tau(h, a, xH, xA, rho)
            scores.append({"h": h, "a": a, "p": round(max(p, 0) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"])

def model_weighted_form(home_fx, away_fx, home_id, away_id):
    def wavg_goals(fixtures, team_id, venue):
        items = []
        fx = sorted(fixtures, key=lambda f: f["fixture"]["date"])
        n  = len(fx) or 1
        for i, f in enumerate(fx):
            is_home = f["teams"]["home"]["id"] == team_id
            is_away = f["teams"]["away"]["id"] == team_id
            if not is_home and not is_away: continue
            if venue == "home" and not is_home: continue
            if venue == "away" and not is_away: continue
            w  = (i + 1) / n
            hg = f["goals"]["home"] or 0
            ag = f["goals"]["away"] or 0
            s  = hg if is_home else ag
            c  = ag if is_home else hg
            items.append((s, c, w))
        if not items: return 1.0, 1.0
        tw = sum(w for _, _, w in items)
        ws = sum(s * w for s, _, w in items) / tw
        wc = sum(c * w for _, c, w in items) / tw
        return ws, wc

    hs, hc = wavg_goals(home_fx, home_id, "home")
    as_, ac = wavg_goals(away_fx, away_id, "away")
    xH = round((hs + ac) / 2, 2)
    xA = round((as_ + hc) / 2, 2)
    scores = []
    for h in range(8):
        for a in range(8):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH, h) * poisson_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"]), xH, xA

def model_negative_binomial(xH, xA):
    def nb_prob(mu, k, r=2.0):
        p = r / (r + mu)
        try:
            coef = math.gamma(r + k) / (math.gamma(r) * math.factorial(k))
            return coef * (p ** r) * ((1 - p) ** k)
        except:
            return poisson_prob(mu, k)
    scores = []
    for h in range(8):
        for a in range(8):
            scores.append({"h": h, "a": a,
                "p": round(nb_prob(xH, h) * nb_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"])

def model_home_away(home_fx, away_fx, home_id, away_id):
    hS_h, hC_h = goals_split(home_fx, home_id, "home")
    aS_a, aC_a = goals_split(away_fx, away_id, "away")
    hS_a, hC_a = goals_split(home_fx, home_id, "away")
    aS_h, aC_h = goals_split(away_fx, away_id, "home")
    xH = round((mean(hS_h)*0.7 + mean(hS_a)*0.3 + mean(aC_a)*0.7 + mean(aC_h)*0.3) / 2, 2)
    xA = round((mean(aS_a)*0.7 + mean(aS_h)*0.3 + mean(hC_h)*0.7 + mean(hC_a)*0.3) / 2, 2)
    scores = []
    for h in range(8):
        for a in range(8):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH, h) * poisson_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"]), xH, xA

def combine_models(models_scores):
    """Combine les 5 modèles et retourne top 9 + top 2 scores."""
    weights  = [0.20, 0.25, 0.25, 0.15, 0.15]
    combined = {}
    for i, scores in enumerate(models_scores):
        w = weights[i]
        for s in scores[:20]:
            key = (s["h"], s["a"])
            combined[key] = combined.get(key, 0) + s["p"] * w

    sorted_s = sorted(combined.items(), key=lambda x: -x[1])
    results  = [{"h": h, "a": a, "p": round(p, 2)} for (h, a), p in sorted_s[:9]]
    total_p  = sum(r["p"] for r in results)
    top2     = []
    for r in results[:2]:
        top2.append({
            "h":          r["h"],
            "a":          r["a"],
            "p":          r["p"],
            "confidence": round(r["p"] / total_p * 100, 1) if total_p else 0
        })
    return results, top2


# ─────────────────────────────────────────────
# NOUVEAUX MODÈLES DE SCORE
# ─────────────────────────────────────────────

def model_high_scoring(home_fx, away_fx, home_id, away_id, xH, xA):
    """Poisson non-homogène boosté pour scores élevés.
    Amplifie le xG selon la tendance offensive des 5 derniers matchs."""
    def last_n_avg(fixtures, team_id, n=5):
        scored = []
        for f in sorted(fixtures, key=lambda f: f["fixture"]["date"], reverse=True)[:n]:
            is_home = f["teams"]["home"]["id"] == team_id
            g = f["goals"]["home"] if is_home else f["goals"]["away"]
            scored.append(g or 0)
        return sum(scored) / len(scored) if scored else 1.0

    h_recent = last_n_avg(home_fx, home_id)
    a_recent = last_n_avg(away_fx, away_id)
    # Booster le xG si forme offensive récente > 1.5 buts/match
    boost_h = max(1.0, h_recent / 1.2)
    boost_a = max(1.0, a_recent / 1.2)
    xH_high = round(xH * boost_h, 2)
    xA_high = round(xA * boost_a, 2)
    scores = []
    for h in range(10):
        for a in range(10):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH_high, h) * poisson_prob(xA_high, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"]), xH_high, xA_high

def model_momentum_xg(home_fx, away_fx, home_id, away_id):
    """xG Style × Momentum exponentiel — donne plus de poids aux 3 derniers matchs."""
    def exp_avg(fixtures, team_id, n=8):
        scored, conceded = [], []
        fx_sorted = sorted(fixtures, key=lambda f: f["fixture"]["date"], reverse=True)[:n]
        for i, f in enumerate(fx_sorted):
            is_home = f["teams"]["home"]["id"] == team_id
            hg = f["goals"]["home"] or 0
            ag = f["goals"]["away"] or 0
            s = hg if is_home else ag
            c = ag if is_home else hg
            w = (0.85 ** i)  # Poids exponentiel décroissant
            scored.append((s, w))
            conceded.append((c, w))
        tw = sum(w for _, w in scored) or 1
        avg_s = sum(s * w for s, w in scored) / tw
        avg_c = sum(c * w for c, w in conceded) / tw
        return avg_s, avg_c

    h_s, h_c = exp_avg(home_fx, home_id)
    a_s, a_c = exp_avg(away_fx, away_id)
    xH = round((h_s + a_c) / 2, 2)
    xA = round((a_s + h_c) / 2, 2)
    scores = []
    for h in range(10):
        for a in range(10):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH, h) * poisson_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"]), xH, xA

def model_strength_index(home_fx, away_fx, home_id, away_id, rank_home, rank_away, standings):
    """Strength Index — pondère selon le classement et la force relative."""
    total = len(standings) if standings else 20
    # Rang → coefficient (1er = 1.0, dernier = 0.4)
    rh = rank_home.get("rank", 10) if rank_home else 10
    ra = rank_away.get("rank", 10) if rank_away else 10
    coef_h = round(1.0 - ((rh - 1) / max(total, 1)) * 0.6, 3)
    coef_a = round(1.0 - ((ra - 1) / max(total, 1)) * 0.6, 3)

    def avg_goals(fixtures, team_id):
        scored = [((f["goals"]["home"] if f["teams"]["home"]["id"] == team_id
                    else f["goals"]["away"]) or 0) for f in fixtures
                  if f["teams"]["home"]["id"] == team_id or f["teams"]["away"]["id"] == team_id]
        return sum(scored) / len(scored) if scored else 1.0

    base_h = avg_goals(home_fx, home_id)
    base_a = avg_goals(away_fx, away_id)
    # Force adversaire inverse
    opp_coef_h = 1.0 - (coef_a - 0.4) * 0.5
    opp_coef_a = 1.0 - (coef_h - 0.4) * 0.5
    xH = round(base_h * coef_h * opp_coef_h, 2)
    xA = round(base_a * coef_a * opp_coef_a, 2)
    scores = []
    for h in range(9):
        for a in range(9):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH, h) * poisson_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"]), xH, xA

def fetch_lineups(fixture_id):
    """Récupère les compositions (probables ou confirmées) pour un match."""
    if not fixture_id:
        return None, None
    try:
        data = football_get("fixtures/lineups", {"fixture": fixture_id})
        lineups = data.get("response", [])
        if not lineups:
            return None, None
        home_lu = next((l for l in lineups if l.get("team", {}).get("id")), None)
        away_lu = next((l for l in lineups[1:] if l.get("team", {}).get("id")), None)
        return home_lu, away_lu
    except:
        return None, None

def model_lineup_based(home_fx, away_fx, home_id, away_id,
                       home_lineup, away_lineup, xH_base, xA_base):
    """Ajuste le xG selon la composition alignée.
    Si des joueurs clés sont absents → réduit le xG offensif."""
    def lineup_strength(lineup, team_id, base_fx, n_key=3):
        if not lineup:
            return 1.0
        starters = [p["player"]["id"] for p in lineup.get("startXI", []) if p.get("player")]
        # Compter les buts de l'équipe dans les derniers matchs par joueur présent
        key_scorers_present = 0
        for f in base_fx[-10:]:
            is_home = f["teams"]["home"]["id"] == team_id
            events  = f.get("events", [])
            for ev in events:
                if ev.get("type") == "Goal" and ev.get("team", {}).get("id") == team_id:
                    pid = ev.get("player", {}).get("id")
                    if pid and pid in starters:
                        key_scorers_present += 1
        # Coefficient : si buteurs clés présents → boost, sinon réduction
        return min(1.3, max(0.6, 0.8 + key_scorers_present * 0.05))

    coef_h = lineup_strength(home_lineup, home_id, home_fx)
    coef_a = lineup_strength(away_lineup, away_id, away_fx)
    xH = round(xH_base * coef_h, 2)
    xA = round(xA_base * coef_a, 2)
    scores = []
    for h in range(9):
        for a in range(9):
            scores.append({"h": h, "a": a,
                "p": round(poisson_prob(xH, h) * poisson_prob(xA, a) * 100, 2)})
    return sorted(scores, key=lambda x: -x["p"]), xH, xA, coef_h, coef_a

def combine_models_extended(models_list):
    """Combine N modèles avec leurs poids et retourne top scores + top 6."""
    models_scores, weights = zip(*models_list)
    combined = {}
    for scores, w in zip(models_scores, weights):
        for s in scores[:20]:
            key = (s["h"], s["a"])
            combined[key] = combined.get(key, 0) + s["p"] * w

    sorted_s = sorted(combined.items(), key=lambda x: -x[1])
    results  = [{"h": h, "a": a, "p": round(p, 2)} for (h, a), p in sorted_s[:9]]
    total_p  = sum(r["p"] for r in results)
    top6 = []
    for r in results[:6]:
        top6.append({
            "h":          r["h"],
            "a":          r["a"],
            "p":          r["p"],
            "confidence": round(r["p"] / total_p * 100, 1) if total_p else 0
        })
    return results, top6

# ─────────────────────────────────────────────
# H2H APPROFONDI
# ─────────────────────────────────────────────

def h2h_deep(h2h, home_id, away_id):
    if not h2h:
        return None

    totals     = [(f["goals"]["home"] or 0) + (f["goals"]["away"] or 0) for f in h2h]
    score_freq = {}
    for f in h2h:
        hg = f["goals"]["home"] or 0
        ag = f["goals"]["away"] or 0
        is_home = f["teams"]["home"]["id"] == home_id
        s = f"{hg}-{ag}" if is_home else f"{ag}-{hg}"
        score_freq[s] = score_freq.get(s, 0) + 1

    top_scores_h2h = sorted(score_freq.items(), key=lambda x: -x[1])[:3]

    hW = sum(1 for f in h2h if
             (f["teams"]["home"]["id"] == home_id and
              (f["goals"]["home"] or 0) > (f["goals"]["away"] or 0)) or
             (f["teams"]["away"]["id"] == home_id and
              (f["goals"]["away"] or 0) > (f["goals"]["home"] or 0)))
    aW = sum(1 for f in h2h if
             (f["teams"]["home"]["id"] == away_id and
              (f["goals"]["home"] or 0) > (f["goals"]["away"] or 0)) or
             (f["teams"]["away"]["id"] == away_id and
              (f["goals"]["away"] or 0) > (f["goals"]["home"] or 0)))
    draws = len(h2h) - hW - aW

    return {
        "total":      len(h2h),
        "home_wins":  hW,
        "away_wins":  aW,
        "draws":      draws,
        "avg_goals":  mean(totals),
        "max_goals":  max(totals),
        "over25":     over_pct(totals, 2.5),
        "bts":        bts_pct(h2h),
        "top_scores": [{"score": s, "count": c} for s, c in top_scores_h2h],
        "recent": [{
            "date":       f["fixture"]["date"][:10],
            "home":       f["teams"]["home"]["name"],
            "away":       f["teams"]["away"]["name"],
            "hg":         f["goals"]["home"] or 0,
            "ag":         f["goals"]["away"] or 0,
            "home_is_ht": f["teams"]["home"]["id"] == home_id
        } for f in h2h[:6]]
    }

# ─────────────────────────────────────────────
# ANALYSE COMPLETE
# ─────────────────────────────────────────────

def full_analysis(home_name, away_name, league_id):
    print(f"\n Analyse : {home_name} vs {away_name} (Ligue {league_id})")

    home_team = find_team(home_name)
    away_team = find_team(away_name)
    print(f"  OK {home_team['name']} vs {away_team['name']}")

    home_fx   = fetch_fixtures(home_team["id"])
    away_fx   = fetch_fixtures(away_team["id"])
    h2h       = fetch_h2h(home_team["id"], away_team["id"])
    standings = fetch_standings(league_id)

    hS, hC = goals_split(home_fx, home_team["id"], "home")
    aS, aC = goals_split(away_fx, away_team["id"], "away")

    xH = round((mean(hS) + mean(aC)) / 2, 2)
    xA = round((mean(aS) + mean(hC)) / 2, 2)
    xT = round(xH + xA, 2)

    probs = calc_probabilities(home_fx, away_fx, h2h, home_team["id"], away_team["id"])
    dc    = {
        "1X": round(probs["home"] + probs["draw"], 1),
        "X2": round(probs["away"] + probs["draw"], 1),
        "12": round(probs["home"] + probs["away"], 1)
    }

    h2h_totals = [(f["goals"]["home"] or 0) + (f["goals"]["away"] or 0) for f in h2h]
    all_totals = [s + c for s, c in zip(hS, hC)] + [s + c for s, c in zip(aS, aC)] + h2h_totals

    markets = {}
    for t in [1.5, 2.5, 3.5, 4.5]:
        markets[f"o{t}"] = over_pct(all_totals, t)
        markets[f"u{t}"] = round(100 - markets[f"o{t}"], 1)

    bts     = bts_pct(home_fx + away_fx + h2h)
    bts_o25 = round(bts * markets["o2.5"] / 100, 1)
    bts_o15 = round(bts * markets["o1.5"] / 100, 1)

    ind_home = {
        "o05": over_pct(hS, 0.5),
        "o15": over_pct(hS, 1.5),
        "o25": over_pct(hS, 2.5)
    }
    ind_away = {
        "o05": over_pct(aS, 0.5),
        "o15": over_pct(aS, 1.5),
        "o25": over_pct(aS, 2.5)
    }

    # 5 modeles de score classiques
    print("  -> Modeles de score...")
    m1 = model_poisson(xH, xA)
    m2 = model_dixon_coles(xH, xA)
    m3, xH_f, xA_f = model_weighted_form(home_fx, away_fx, home_team["id"], away_team["id"])
    m4 = model_negative_binomial(xH, xA)
    m5, xH_ha, xA_ha = model_home_away(home_fx, away_fx, home_team["id"], away_team["id"])

    all_scores, top2_scores = combine_models([m1, m2, m3, m4, m5])
    pred_score = {"h": round(xH), "a": round(xA)}

    # H2H approfondi
    h2h_stats = h2h_deep(h2h, home_team["id"], away_team["id"])

    # Classement et contexte
    rank_home = find_in_standings(standings, home_team["id"])
    rank_away = find_in_standings(standings, away_team["id"])
    context   = match_context(rank_home, rank_away, home_fx, away_fx)

    # 4 nouveaux modeles
    print("  -> Modeles avancés (scores élevés + classement + compositions)...")
    m6, xH_high, xA_high = model_high_scoring(home_fx, away_fx, home_team["id"], away_team["id"], xH, xA)
    m7, xH_mom, xA_mom   = model_momentum_xg(home_fx, away_fx, home_team["id"], away_team["id"])
    m8, xH_str, xA_str   = model_strength_index(home_fx, away_fx, home_team["id"], away_team["id"],
                                                  rank_home, rank_away, standings)

    # Compositions
    fixture_id = data_input.get("fixture_id") if 'data_input' in dir() else None
    home_lu, away_lu = fetch_lineups(fixture_id)
    m9, xH_lu, xA_lu, coef_h_lu, coef_a_lu = model_lineup_based(
        home_fx, away_fx, home_team["id"], away_team["id"],
        home_lu, away_lu, xH, xA
    )

    # Combiner les 9 modèles avec poids
    all_scores_ext, top6_scores = combine_models_extended([
        (m1, 0.12), (m2, 0.15), (m3, 0.13), (m4, 0.10), (m5, 0.10),
        (m6, 0.12), (m7, 0.12), (m8, 0.10), (m9, 0.06)
    ])

    # Étiqueter les top6 par méthode dominante
    method_labels = ["Statistique", "Statistique Alt.", "Scores Élevés", "Momentum", "Classement", "Composition"]
    for i, sc in enumerate(top6_scores):
        sc["method"] = method_labels[i] if i < len(method_labels) else f"Modèle {i+1}"

    # Garder top2 pour compatibilité
    top2_scores = top6_scores[:2]

    # Style de jeu
    print("  -> Style de jeu...")
    h_style = analyze_style(home_team["id"], league_id)
    a_style = analyze_style(away_team["id"], league_id)

    # Joueurs et tirs
    print("  -> Joueurs & tirs...")
    h_scorers = team_scorers(home_team["id"], league_id)
    a_scorers = team_scorers(away_team["id"], league_id)
    h_assists = team_assisters(home_team["id"], league_id)
    a_assists = team_assisters(away_team["id"], league_id)
    h_shots   = team_shots(home_team["id"], league_id)
    a_shots   = team_shots(away_team["id"], league_id)

    # Cotes bookmakers
    print("  -> Cotes bookmakers...")
    odds = fetch_odds(home_name, away_name, league_id)

    print("  OK Analyse terminee !")

    return {
        "home_team":   home_team,
        "away_team":   away_team,
        "probs":       probs,
        "dc":          dc,
        "xG":          {"home": xH, "away": xA, "total": xT},
        "markets":     markets,
        "bts":         bts,
        "bts_o15":     bts_o15,
        "bts_o25":     bts_o25,
        "ind_home":    ind_home,
        "ind_away":    ind_away,
        "top_scores":     all_scores,
        "top2_scores":    top2_scores,
        "top6_scores":    top6_scores,
        "pred_score":     pred_score,
        "lineup_home":    home_lu,
        "lineup_away":    away_lu,
        "lineup_coef_h":  coef_h_lu,
        "lineup_coef_a":  coef_a_lu,
        "score_models": {
            "poisson":        m1[:3],
            "dixon_coles":    m2[:3],
            "weighted_form":  m3[:3],
            "neg_binomial":   m4[:3],
            "home_away":      m5[:3],
            "high_scoring":   m6[:3],
            "momentum":       m7[:3],
            "strength":       m8[:3],
            "lineup":         m9[:3]
        },
        "h2h":         h2h_stats,
        "rank_home":   rank_home,
        "rank_away":   rank_away,
        "h_scorers":   h_scorers,
        "a_scorers":   a_scorers,
        "h_assists":   h_assists,
        "a_assists":   a_assists,
        "h_shots":     h_shots,
        "a_shots":     a_shots,
        "h_style":     h_style,
        "a_style":     a_style,
        "context":     context,
        "odds":        odds,
        "home_games":  len(hS),
        "away_games":  len(aS),
        "season":      SEASON,
        "home_fx_raw": home_fx,
        "away_fx_raw": away_fx
    }

# ─────────────────────────────────────────────
# GEMINI — CONCLUSION
# ─────────────────────────────────────────────

def gpt_conclusion(data):
    if not gemini_client:
        return None

    d   = data
    hn  = d["home_team"]["name"]
    an  = d["away_team"]["name"]
    p   = d["probs"]
    m   = d["markets"]
    rH  = d.get("rank_home")
    rA  = d.get("rank_away")
    h2  = d.get("h2h")
    hs  = d.get("h_shots") or {}
    as_ = d.get("a_shots") or {}
    hsc = d.get("h_scorers") or []
    asc = d.get("a_scorers") or []
    t2  = d.get("top2_scores") or []
    ctx = d.get("context") or {}
    hst = d.get("h_style") or {}
    ast = d.get("a_style") or {}
    odd = d.get("odds")

    def fmt(lst):
        return ", ".join([
            f"{pl['name']} ({pl['goals']} buts {pl.get('assists',0)} passes {pl['shots']} tirs {pl['accuracy']}%)"
            for pl in lst
        ]) or "N/A"

    scores_str = " | ".join([f"{s['h']}-{s['a']} ({s['p']}%)" for s in t2]) if t2 else "N/A"
    odds_str   = (
        f"Dom={odd['home_odds']} Nul={odd['draw_odds']} Ext={odd['away_odds']} "
        f"| Impl: {odd['impl_home']}%/{odd['impl_draw']}%/{odd['impl_away']}%"
        if odd else "N/A"
    )
    h2h_scores = str(h2.get("top_scores", "N/A")) if h2 else "N/A"

    # Derniers matchs formatés pour le prompt
    def fmt_recent_games(fixtures, team_id, venue, n=5):
        games = []
        for f in fixtures:
            is_home = f["teams"]["home"]["id"] == team_id
            if venue == "home" and not is_home: continue
            if venue == "away" and is_home: continue
            hg = f["goals"]["home"] or 0
            ag = f["goals"]["away"] or 0
            opp = f["teams"]["away"]["name"] if is_home else f["teams"]["home"]["name"]
            sc = hg if is_home else ag
            co = ag if is_home else hg
            date = f["fixture"]["date"][:10]
            games.append(f"{date} vs {opp}: {sc}-{co}")
            if len(games) >= n: break
        return " | ".join(games) if games else "N/A"

    home_recent_home = fmt_recent_games(d.get("home_fx_raw", []), d["home_team"]["id"], "home")
    away_recent_away = fmt_recent_games(d.get("away_fx_raw", []), d["away_team"]["id"], "away")

    def fmt_assisters_full(lst):
        return ", ".join([f"{pl['name']} ({pl.get('assists',0)} passes)" for pl in lst]) or "N/A"

    prompt = f"""Tu es un analyste football professionnel. Réponds UNIQUEMENT en JSON valide (sans markdown).

MATCH : {hn} (DOMICILE) vs {an} (EXTÉRIEUR) - Saison {d['season']}

CLASSEMENT :
- {hn} : {f"{rH['rank']}e ({rH['points']}pts forme:{rH.get('form','?')})" if rH else 'N/A'}
- {an} : {f"{rA['rank']}e ({rA['points']}pts forme:{rA.get('form','?')})" if rA else 'N/A'}

CONTEXTE : {ctx.get('enjeu','?')} | Fatigue {hn}: {ctx.get('home_fatigue','?')} | Fatigue {an}: {ctx.get('away_fatigue','?')}

STYLE DE JEU :
- {hn} : {hst.get('style','?')} | {hst.get('goals_for_avg','?')} buts marqués/match | {hst.get('goals_ag_avg','?')} encaissés/match | CS: {hst.get('cs_pct','?')}%
- {an} : {ast.get('style','?')} | {ast.get('goals_for_avg','?')} buts marqués/match | {ast.get('goals_ag_avg','?')} encaissés/match | CS: {ast.get('cs_pct','?')}%

DERNIERS MATCHS À DOMICILE DE {hn} :
{home_recent_home}

DERNIERS MATCHS À L'EXTÉRIEUR DE {an} :
{away_recent_away}

H2H (confrontations directes) : {f"{h2['total']} matchs : {hn} {h2['home_wins']}V/{h2['draws']}N/{h2['away_wins']}D | Scores fréquents: {h2h_scores}" if h2 else 'N/A'}

PROBABILITÉS MODÈLES :
- 1X2 : Dom={p['home']}% Nul={p['draw']}% Ext={p['away']}%
- xG : {hn}={d['xG']['home']} | {an}={d['xG']['away']} | Total={d['xG']['total']}
- Over/Under : O1.5={m['o1.5']}% O2.5={m['o2.5']}% O3.5={m['o3.5']}%
- BTS : {d['bts']}%

COTES BOOKMAKERS : {odds_str}

SCORES PROBABLES (5 modèles statistiques) : {scores_str}

TIRS : {hn} {hs.get('per_game','?')} tirs/match ({hs.get('accuracy','?')}%) | {an} {as_.get('per_game','?')} tirs/match ({as_.get('accuracy','?')}%)

BUTEURS DE LA SAISON :
- {hn} : {fmt(hsc)}
- {an} : {fmt(asc)}

PASSEURS DE LA SAISON :
- {hn} : {fmt_assisters_full(d.get('h_assists', []))}
- {an} : {fmt_assisters_full(d.get('a_assists', []))}

En analysant les derniers matchs joués par chacun (domicile / extérieur), le nombre de buts marqués et encaissés par match, et les confrontations face à face, donne-moi un score exact et les buteurs/passeurs probables.

Réponds UNIQUEMENT en JSON :
{{
  "verdict": "home"|"draw"|"away",
  "verdict_label": "phrase courte ex: Victoire {hn} solide",
  "analyse": "4-5 phrases clés. Mentionne les derniers matchs, le H2H, les scores probables, les cotes.",
  "score1": {{"h": 1, "a": 0, "confidence": 35.2, "reason": "basé sur les derniers matchs, H2H et buts marqués/encaissés", "method": "Gemini"}},
  "score2": {{"h": 1, "a": 1, "confidence": 22.1, "reason": "basé sur les derniers matchs, H2H et buts marqués/encaissés", "method": "Gemini"}},
  "probable_scorers": [
    {{"name": "Nom Joueur", "team": "nom equipe", "probability": 45.0, "goals_season": 12, "reason": "Forme et stats"}},
    {{"name": "Nom Joueur", "team": "nom equipe", "probability": 35.0, "goals_season": 8, "reason": "..."}}
  ],
  "probable_assisters": [
    {{"name": "Nom Joueur", "team": "nom equipe", "probability": 35.0, "assists_season": 8, "reason": "..."}},
    {{"name": "Nom Joueur", "team": "nom equipe", "probability": 25.0, "assists_season": 5, "reason": "..."}}
  ],
  "picks": [
    {{"label":"nom marché","pct":72.5,"confidence":"ÉLEVÉE"|"MOYENNE"|"FAIBLE","is_top":true,"reason":"1-2 phrases","value_bet":true}}
  ]
}}
Règles :
- 4 à 6 buteurs probables (les deux équipes, triés par probabilité décroissante)
- 4 à 6 passeurs probables (les deux équipes, triés par probabilité décroissante)
- 7 à 9 picks, triés du plus probable au moins probable
- is_top:true pour UN seul pick uniquement
- value_bet:true si probabilité modèle > cote implicite bookmaker
- PAS de contradictions (jamais Over 2.5 ET Under 2.5 ensemble)
- Inclus toujours : 1X2, Over/Under, BTS, score exact, et un combiné"""

    # Ajouter les fixtures brutes pour le prompt
    data["home_fx_raw"] = data.get("home_fx_raw", [])
    data["away_fx_raw"] = data.get("away_fx_raw", [])

    full_prompt = "Tu es un analyste football expert. Réponds UNIQUEMENT en JSON valide.\n\n" + prompt
    response = gemini_client.generate_content(full_prompt)
    raw   = response.text.strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ─────────────────────────────────────────────
# ROUTES FLASK
# ─────────────────────────────────────────────

@app.route("/")
def index():
    import os
    folder = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(folder, "index.html")

@app.route("/api/status")
def status():
    return jsonify({
        "ok":      True,
        "season":  SEASON,
        "has_gpt": gemini_client is not None
    })

@app.route("/api/today")
def today_matches():
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    all_matches = []
    try:
        data = football_get("fixtures", {"date": today})
        print(f"DEBUG today={today} total={len(data.get('response', []))}")
        for f in data.get("response", []):
            lid = f["league"]["id"]
            if lid not in LIGUES_SUIVIES:
                continue
            all_matches.append({
                "id":         f["fixture"]["id"],
                "time":       f["fixture"]["date"][11:16],
                "status":     f["fixture"]["status"]["short"],
                "elapsed":    f["fixture"]["status"].get("elapsed"),
                "home":       f["teams"]["home"]["name"],
                "away":       f["teams"]["away"]["name"],
                "home_logo":  f["teams"]["home"]["logo"],
                "away_logo":  f["teams"]["away"]["logo"],
                "home_score": f["goals"]["home"],
                "away_score": f["goals"]["away"],
                "league":     f["league"]["name"],
                "league_id":  lid,
                "league_logo":f["league"]["logo"],
                "venue":      f["fixture"]["venue"]["name"] or ""
            })
    except Exception as e:
        print(f"Erreur today_matches: {e}")

    # Croiser avec les predictions en base
    predictions_map = {}  # fixture_id -> [preds]  OU  "home|away" -> [preds]
    try:
        init_predictions_table()
        conn = get_db()
        preds = [dict(r) for r in conn.execute(
            "SELECT * FROM pending_predictions ORDER BY created_at DESC"
        ).fetchall()]
        conn.close()
        for p in preds:
            # Index par fixture_id
            if p.get("fixture_id"):
                key = str(p["fixture_id"])
                predictions_map.setdefault(key, []).append(p)
            # Index par nom
            name_key = f"{p['home'].lower().strip()}|{p['away'].lower().strip()}"
            predictions_map.setdefault(name_key, []).append(p)
    except:
        pass

    # Attacher les predictions a chaque match
    for m in all_matches:
        preds_for_match = []
        # Par fixture_id
        by_id = predictions_map.get(str(m["id"]), [])
        preds_for_match.extend(by_id)
        # Par nom (si pas deja trouve)
        if not preds_for_match:
            name_key = f"{m['home'].lower().strip()}|{m['away'].lower().strip()}"
            preds_for_match.extend(predictions_map.get(name_key, []))
        # Dedupliquer
        seen = set()
        unique_preds = []
        for p in preds_for_match:
            k = (p["predicted_h"], p["predicted_a"], p["method"])
            if k not in seen:
                seen.add(k)
                unique_preds.append({
                    "predicted_h": p["predicted_h"],
                    "predicted_a": p["predicted_a"],
                    "method":      p.get("method", "Gemini"),
                    "confidence":  p.get("confidence"),
                    "status":      p.get("status", "pending")
                })
        m["predictions"] = unique_preds

        # Si match termine, calculer is_exact pour chaque prediction
        if m["status"] == "FT" and m["home_score"] is not None:
            for p in m["predictions"]:
                p["is_exact"] = (
                    p["predicted_h"] == m["home_score"] and
                    p["predicted_a"] == m["away_score"]
                )

    all_matches.sort(key=lambda x: x["time"])
    return jsonify({"ok": True, "matches": all_matches, "date": today})

@app.route("/api/setkey", methods=["POST"])
def set_key():
    global gemini_client
    key = request.json.get("key", "").strip()
    if not key.startswith("AIza"):
        return jsonify({"ok": False, "error": "Cle invalide - doit commencer par AIza"})
    genai.configure(api_key=key)
    gemini_client = genai.GenerativeModel("gemini-2.0-flash")
    print(f"  Cle Gemini enregistree : {key[:16]}...")
    return jsonify({"ok": True})

@app.route("/api/analyze", methods=["POST"])
def analyze():
    body   = request.json
    home   = body.get("home", "").strip()
    away   = body.get("away", "").strip()
    league = int(body.get("league", 61))

    if not home or not away:
        return jsonify({"ok": False, "error": "Equipes manquantes"}), 400

    try:
        data = full_analysis(home, away, league)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/conclude", methods=["POST"])
def conclude():
    data = request.json
    if not gemini_client:
        return jsonify({"ok": False, "need_key": True})
    try:
        conclusion = gpt_conclusion(data)
        top = conclusion.get("picks", [{}])[0] if conclusion else {}
        save_analysis(
            home     = data["home_team"]["name"],
            away     = data["away_team"]["name"],
            league   = str(data.get("season", SEASON)),
            data     = {"analysis": data, "conclusion": conclusion},
            top_pick = top.get("label"),
            top_pct  = top.get("pct")
        )
        # Auto-save predictions en attente
        try:
            init_predictions_table()
            conn2 = get_db()
            from datetime import date as _date
            s1 = conclusion.get("score1", {})
            s2 = conclusion.get("score2", {})
            match_date = _date.today().strftime("%Y-%m-%d")
            league_name = data.get("league_name") or str(data.get("season", SEASON))
            fid = data.get("fixture_id")
            home_name = data["home_team"]["name"]
            away_name = data["away_team"]["name"]
            for s, m in [(s1, "Gemini"), (s2, "Gemini + Modele")]:
                if s.get("h") is not None and s.get("a") is not None:
                    conn2.execute(
                        "INSERT INTO pending_predictions "
                        "(fixture_id, home, away, league, match_date, predicted_h, predicted_a, method, confidence, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (fid, home_name, away_name, league_name, match_date,
                         int(s.get("h",0)), int(s.get("a",0)), m,
                         s.get("confidence"), s.get("reason",""))
                    )
            conn2.commit()
            conn2.close()
        except Exception as ep:
            print(f"Avertissement save_prediction: {ep}")
        return jsonify({"ok": True, "conclusion": conclusion})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ─────────────────────────────────────────────
# LIVE — MATCHS EN COURS
# ─────────────────────────────────────────────

@app.route("/api/live")
def live_matches():
    """Liste des matchs en cours dans les ligues suivies."""
    try:
        data = football_get("fixtures", {"live": "all"})
        matches = []
        for f in data.get("response", []):
            lid = f["league"]["id"]
            if lid not in LIGUES_SUIVIES:
                continue
            elapsed = f["fixture"]["status"].get("elapsed") or 0
            matches.append({
                "id":          f["fixture"]["id"],
                "status":      f["fixture"]["status"]["short"],
                "elapsed":     elapsed,
                "home":        f["teams"]["home"]["name"],
                "away":        f["teams"]["away"]["name"],
                "home_logo":   f["teams"]["home"]["logo"],
                "away_logo":   f["teams"]["away"]["logo"],
                "home_score":  f["goals"]["home"] or 0,
                "away_score":  f["goals"]["away"] or 0,
                "ht_score":    f.get("score", {}).get("halftime", {}),
                "league":      f["league"]["name"],
                "league_id":   lid,
                "league_logo": f["league"]["logo"],
                "venue":       f["fixture"]["venue"].get("name", ""),
                "home_id":     f["teams"]["home"]["id"],
                "away_id":     f["teams"]["away"]["id"],
            })
        matches.sort(key=lambda x: x["elapsed"], reverse=True)
        return jsonify({"ok": True, "matches": matches, "count": len(matches)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/live/<int:fixture_id>")
def live_detail(fixture_id):
    """Statistiques détaillées d'un match en cours + probabilités recalculées."""
    try:
        # Stats du match en cours
        fix_data = football_get("fixtures", {"id": fixture_id})
        if not fix_data.get("response"):
            return jsonify({"ok": False, "error": "Match introuvable"}), 404

        f = fix_data["response"][0]
        elapsed   = f["fixture"]["status"].get("elapsed") or 0
        status    = f["fixture"]["status"]["short"]
        home_score = f["goals"]["home"] or 0
        away_score = f["goals"]["away"] or 0
        home_id   = f["teams"]["home"]["id"]
        away_id   = f["teams"]["away"]["id"]
        league_id = f["league"]["id"]

        # Stats live du match (tirs, possession, corners, cartons...)
        stats_data = football_get("fixtures/statistics", {"fixture": fixture_id})
        stats = {}
        for team_stat in stats_data.get("response", []):
            tid = team_stat["team"]["id"]
            side = "home" if tid == home_id else "away"
            stats[side] = {}
            for s in team_stat.get("statistics", []):
                key = s["type"].lower().replace(" ", "_")
                stats[side][key] = s["value"]

        # Events (buts, cartons, remplacements)
        events_data = football_get("fixtures/events", {"fixture": fixture_id})
        events = []
        for e in events_data.get("response", []):
            etype = e["type"]
            detail = e.get("detail", "")
            player = e.get("player", {}).get("name", "")
            assist = e.get("assist", {}).get("name", "")
            team_id = e.get("team", {}).get("id")
            side = "home" if team_id == home_id else "away"
            minute = e.get("time", {}).get("elapsed", 0)
            extra  = e.get("time", {}).get("extra")

            if etype == "Goal":
                icon = "⚽" if detail != "Own Goal" else "🔴"
            elif etype == "Card":
                icon = "🟨" if detail == "Yellow Card" else "🟥"
            elif etype == "subst":
                icon = "🔄"
            else:
                icon = "📌"

            events.append({
                "minute": minute,
                "extra":  extra,
                "type":   etype,
                "detail": detail,
                "icon":   icon,
                "player": player,
                "assist": assist,
                "side":   side,
                "team":   e.get("team", {}).get("name", ""),
            })

        # Recalcul des probabilités selon le score live
        # Modèle Poisson simple sur les buts restants attendus
        # Basé sur xG de la saison (on utilise les stats live comme proxy)
        def _v(stats, side, key, default=0):
            try:
                val = stats.get(side, {}).get(key, default)
                if val is None:
                    return default
                return float(str(val).replace("%",""))
            except:
                return default

        poss_h = _v(stats, "home", "ball_possession", 50)
        poss_a = _v(stats, "away", "ball_possession", 50)
        shots_h = _v(stats, "home", "total_shots", 0)
        shots_a = _v(stats, "away", "total_shots", 0)
        on_h    = _v(stats, "home", "shots_on_goal", 0)
        on_a    = _v(stats, "away", "shots_on_goal", 0)

        # xG live estimé depuis tirs cadrés
        xg_h_live = round(on_h * 0.33, 2)
        xg_a_live = round(on_a * 0.33, 2)

        # Minutes restantes
        minutes_left = max(0, 90 - elapsed)
        ratio = minutes_left / 90.0

        # xG restant pondéré par le temps
        xg_h_rem = round(xg_h_live * ratio + 0.001, 3)
        xg_a_rem = round(xg_a_live * ratio + 0.001, 3)

        def poisson_prob(lam, k):
            import math
            return (lam**k * math.exp(-lam)) / math.factorial(k)

        # Probabilités résultat final en tenant compte du score actuel
        p_home_win = 0.0
        p_draw     = 0.0
        p_away_win = 0.0

        for add_h in range(7):
            ph = poisson_prob(xg_h_rem, add_h)
            for add_a in range(7):
                pa = poisson_prob(xg_a_rem, add_a)
                final_h = home_score + add_h
                final_a = away_score + add_a
                p = ph * pa
                if final_h > final_a:
                    p_home_win += p
                elif final_h == final_a:
                    p_draw += p
                else:
                    p_away_win += p

        total = p_home_win + p_draw + p_away_win
        if total > 0:
            p_home_win = round(p_home_win / total * 100, 1)
            p_draw     = round(p_draw     / total * 100, 1)
            p_away_win = round(p_away_win / total * 100, 1)

        # Probabilité Over 2.5 restante
        p_over25 = 0.0
        current_goals = home_score + away_score
        if current_goals >= 3:
            p_over25 = 100.0
        else:
            needed = 3 - current_goals
            for add_h in range(7):
                ph = poisson_prob(xg_h_rem, add_h)
                for add_a in range(7):
                    pa = poisson_prob(xg_a_rem, add_a)
                    if add_h + add_a >= needed:
                        p_over25 += ph * pa
            p_over25 = round(p_over25 * 100, 1)

        # Contexte live pour Gemini
        live_context = {
            "fixture_id":  fixture_id,
            "home":        f["teams"]["home"]["name"],
            "away":        f["teams"]["away"]["name"],
            "home_logo":   f["teams"]["home"]["logo"],
            "away_logo":   f["teams"]["away"]["logo"],
            "home_id":     home_id,
            "away_id":     away_id,
            "league":      f["league"]["name"],
            "league_id":   league_id,
            "league_logo": f["league"]["logo"],
            "status":      status,
            "elapsed":     elapsed,
            "home_score":  home_score,
            "away_score":  away_score,
            "ht_home":     f.get("score", {}).get("halftime", {}).get("home"),
            "ht_away":     f.get("score", {}).get("halftime", {}).get("away"),
            "stats":       stats,
            "events":      events,
            "live_probs":  {
                "home":    p_home_win,
                "draw":    p_draw,
                "away":    p_away_win,
                "over25":  p_over25,
            },
            "xg_live": {
                "home": xg_h_live,
                "away": xg_a_live,
                "home_rem": xg_h_rem,
                "away_rem": xg_a_rem,
            },
            "possession": {
                "home": poss_h,
                "away": poss_a,
            },
        }
        return jsonify({"ok": True, "data": live_context})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/live/<int:fixture_id>/comment", methods=["POST"])
def live_comment(fixture_id):
    """Commentaire Gemini sur le match en cours."""
    if not gemini_client:
        return jsonify({"ok": False, "need_key": True})
    try:
        data = request.json
        home      = data.get("home", "?")
        away      = data.get("away", "?")
        score_h   = data.get("home_score", 0)
        score_a   = data.get("away_score", 0)
        elapsed   = data.get("elapsed", 0)
        probs     = data.get("live_probs", {})
        stats     = data.get("stats", {})
        events    = data.get("events", [])
        xg        = data.get("xg_live", {})
        poss      = data.get("possession", {})

        # Résumé des événements
        goals = [e for e in events if e["type"] == "Goal"]
        cards = [e for e in events if e["type"] == "Card"]
        events_txt = ""
        for g in goals:
            events_txt += f"  • {g['minute']}' - But {g['icon']} {g['player']} ({g['team']})\n"
        for c in cards:
            events_txt += f"  • {c['minute']}' - Carton {c['icon']} {c['player']} ({c['team']})\n"

        prompt = f"""Tu es un commentateur expert de football en direct.

MATCH EN COURS : {home} {score_h}-{score_a} {away}
Minute : {elapsed}'
Possession : {home} {poss.get('home',50)}% — {away} {poss.get('away',50)}%
xG accumulé : {home} {xg.get('home',0)} — {away} {xg.get('away',0)}

Événements :
{events_txt if events_txt else '  Aucun événement notable'}

Probabilités recalculées :
  Victoire {home} : {probs.get('home',0)}%
  Nul : {probs.get('draw',0)}%
  Victoire {away} : {probs.get('away',0)}%
  Over 2.5 : {probs.get('over25',0)}%

Donne un commentaire live expert de 2-3 phrases maximum, puis 2-3 picks encore jouables à ce stade du match avec leur niveau de confiance.

Couvre ces marchés : 1X2, Over/Under, BTS, Double Chance.
Pour chaque pick, indique le marché concerné dans "market".
Réponds UNIQUEMENT en JSON sans backticks :
{{
  "comment": "commentaire expert 2-3 phrases",
  "picks": [
    {{"label": "...", "pct": XX, "confidence": "ÉLEVÉE|MOYENNE|FAIBLE", "reason": "..."}}
  ],
  "momentum": "home|away|balanced"
}}"""

        response = gemini_client.generate_content(prompt)
        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return jsonify({"ok": True, "comment": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



def get_db():
    import sqlite3, os
    db_path = os.path.join(os.path.dirname(__file__), "scoutai.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_predictions_table():
    conn = get_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pending_predictions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "fixture_id INTEGER,"
        "home TEXT NOT NULL,"
        "away TEXT NOT NULL,"
        "league TEXT,"
        "league_id INTEGER,"
        "match_date TEXT,"
        "predicted_h INTEGER NOT NULL,"
        "predicted_a INTEGER NOT NULL,"
        "method TEXT DEFAULT \'Gemini\',"
        "confidence REAL,"
        "reason TEXT,"
        "created_at TEXT DEFAULT (datetime(\'now\')),  "
        "status TEXT DEFAULT \'pending\')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS validated_scores ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "match_date TEXT, home TEXT, away TEXT,"
        "predicted_h INTEGER, predicted_a INTEGER,"
        "real_h INTEGER, real_a INTEGER,"
        "method TEXT,"
        "validated_at TEXT DEFAULT (datetime(\'now\')),  "
        "league TEXT, notes TEXT, is_exact INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# SCORES EXACTS VALIDÉS
# ─────────────────────────────────────────────


@app.route("/api/save-prediction", methods=["POST"])
def save_prediction():
    """Sauvegarde une prediction en attente de validation automatique."""
    try:
        init_predictions_table()
        b = request.json
        conn = get_db()
        conn.execute(
            "INSERT INTO pending_predictions "
            "(fixture_id, home, away, league, league_id, match_date, predicted_h, predicted_a, method, confidence, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b.get("fixture_id"), b.get("home",""), b.get("away",""),
             b.get("league",""), b.get("league_id"), b.get("match_date",""),
             b.get("predicted_h",0), b.get("predicted_a",0),
             b.get("method","Gemini"), b.get("confidence"), b.get("reason",""))
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pending-predictions")
def get_pending():
    """Retourne les predictions en attente."""
    try:
        init_predictions_table()
        conn = get_db()
        rows = conn.execute("SELECT * FROM pending_predictions WHERE status='pending' ORDER BY created_at DESC").fetchall()
        conn.close()
        return jsonify({"ok": True, "predictions": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/check-results", methods=["POST"])
def check_results():
    """Verifie les resultats des predictions en attente et sauvegarde dans validated_scores."""
    try:
        init_predictions_table()
        conn = get_db()
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM pending_predictions WHERE status='pending'"
        ).fetchall()]
        validated = []

        def flexible_match(n1, n2):
            a, b = n1.lower().strip(), n2.lower().strip()
            if a == b: return True
            if len(a) >= 5 and (a in b or b in a): return True
            wa = a.split()[0] if a.split() else a
            wb = b.split()[0] if b.split() else b
            return len(wa) >= 4 and wa == wb

        def find_result(pred):
            # 1. Par fixture_id direct
            if pred.get("fixture_id"):
                try:
                    data = football_get("fixtures", {"id": pred["fixture_id"]})
                    for f in data.get("response", []):
                        if f["fixture"]["status"]["short"] == "FT":
                            return {
                                "real_h":   f["goals"]["home"] or 0,
                                "real_a":   f["goals"]["away"] or 0,
                                "real_date": f["fixture"]["date"][:10]
                            }
                except: pass

            # 2. Derniers matchs FT de l equipe domicile
            try:
                td = football_get("teams", {"search": pred["home"]})
                teams = td.get("response", [])
                if teams:
                    tid = teams[0]["team"]["id"]
                    fd = football_get("fixtures", {"team": tid, "last": 15, "status": "FT"})
                    for f in fd.get("response", []):
                        fh = f["teams"]["home"]["name"]
                        fa = f["teams"]["away"]["name"]
                        if flexible_match(fh, pred["home"]) and flexible_match(fa, pred["away"]):
                            return {
                                "real_h":   f["goals"]["home"] or 0,
                                "real_a":   f["goals"]["away"] or 0,
                                "real_date": f["fixture"]["date"][:10]
                            }
            except: pass

            # 3. Par date (3 derniers jours)
            try:
                from datetime import date, timedelta
                for delta in range(0, 4):
                    d = (date.today() - timedelta(days=delta)).strftime("%Y-%m-%d")
                    data = football_get("fixtures", {"date": d, "status": "FT"})
                    for f in data.get("response", []):
                        fh = f["teams"]["home"]["name"]
                        fa = f["teams"]["away"]["name"]
                        if flexible_match(fh, pred["home"]) and flexible_match(fa, pred["away"]):
                            return {
                                "real_h":   f["goals"]["home"] or 0,
                                "real_a":   f["goals"]["away"] or 0,
                                "real_date": d
                            }
            except: pass
            return None

        for pred in pending:
            result = find_result(pred)
            if result:
                is_exact = 1 if (
                    result["real_h"] == pred["predicted_h"] and
                    result["real_a"] == pred["predicted_a"]
                ) else 0
                conn.execute(
                    "INSERT INTO validated_scores "
                    "(match_date, home, away, predicted_h, predicted_a, "
                    "real_h, real_a, method, league, is_exact) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (result.get("real_date") or pred.get("match_date", ""),
                     pred["home"], pred["away"],
                     pred["predicted_h"], pred["predicted_a"],
                     result["real_h"], result["real_a"],
                     pred.get("method", "Gemini"),
                     pred.get("league", ""), is_exact)
                )
                conn.execute(
                    "UPDATE pending_predictions SET status='done' WHERE id=?",
                    (pred["id"],)
                )
                validated.append({
                    "home": pred["home"], "away": pred["away"],
                    "predicted": f"{pred['predicted_h']}-{pred['predicted_a']}",
                    "real":      f"{result['real_h']}-{result['real_a']}",
                    "method":    pred.get("method", "Gemini"),
                    "is_exact":  bool(is_exact)
                })
            else:
                try:
                    from datetime import datetime, timedelta
                    created = datetime.fromisoformat(
                        pred.get("created_at", "").replace("Z", "")
                    )
                    if (datetime.now() - created).days >= 7:
                        conn.execute(
                            "UPDATE pending_predictions SET status='not_found' WHERE id=?",
                            (pred["id"],)
                        )
                except: pass

        conn.commit()
        conn.close()
        return jsonify({"ok": True, "checked": len(pending), "validated": validated})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/validate-score", methods=["POST"])
def validate_score():
    """Enregistre manuellement un score exact valide."""
    try:
        init_predictions_table()
        body = request.json
        conn = get_db()
        is_exact = 1 if (body.get("predicted_h") == body.get("real_h") and body.get("predicted_a") == body.get("real_a")) else 0
        cur = conn.execute(
            "INSERT INTO validated_scores "
            "(match_date,home,away,predicted_h,predicted_a,real_h,real_a,method,league,notes,is_exact) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (body.get("match_date",""), body.get("home",""), body.get("away",""),
             body.get("predicted_h",0), body.get("predicted_a",0),
             body.get("real_h",0), body.get("real_a",0),
             body.get("method","Gemini"), body.get("league",""),
             body.get("notes",""), is_exact)
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": cur.lastrowid, "is_exact": bool(is_exact)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/validated-scores")
def get_validated_scores():
    """Retourne tous les scores valides."""
    try:
        init_predictions_table()
        conn = get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM validated_scores ORDER BY validated_at DESC").fetchall()]
        conn.close()
        total = len(rows)
        exact = sum(1 for r in rows if r.get("is_exact") == 1)
        rate  = round(exact / total * 100, 1) if total else 0
        return jsonify({"ok": True, "scores": rows, "stats": {"total": total, "exact": exact, "rate": rate}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/validated-scores/<int:sid>", methods=["DELETE"])
def delete_validated_score(sid):
    try:
        init_predictions_table()
        conn = get_db()
        conn.execute("DELETE FROM validated_scores WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# AUTO-PREDICTION MATCHS DU JOUR
# ─────────────────────────────────────────────

@app.route("/api/auto-predict-today", methods=["POST"])
def auto_predict_today():
    """
    Calcule automatiquement les scores predits (modele statistique)
    pour tous les matchs du jour pas encore en base.
    Appele au chargement de la page et toutes les heures.
    """
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")
    results = {"ok": True, "predicted": [], "skipped": [], "errors": []}

    try:
        init_predictions_table()
        # Recuperer les matchs du jour
        data = football_get("fixtures", {"date": today})
        matches = []
        for f in data.get("response", []):
            lid = f["league"]["id"]
            if lid not in LIGUES_SUIVIES:
                continue
            status = f["fixture"]["status"]["short"]
            # Seulement les matchs pas encore commences ou en cours
            if status in ("FT", "AET", "PEN"):
                continue
            matches.append({
                "id":       f["fixture"]["id"],
                "home":     f["teams"]["home"]["name"],
                "away":     f["teams"]["away"]["name"],
                "league":   f["league"]["name"],
                "league_id": lid,
            })

        conn = get_db()
        # Recuperer les predictions deja en base aujourd'hui
        # Supprimer les anciennes prédictions du jour pour recalculer proprement
        conn.execute(
            "DELETE FROM pending_predictions WHERE match_date=? AND status='pending' AND method IN ('Statistique','Statistique Alt.')",
            (today,)
        )
        conn.commit()

        for m in matches:
            try:
                analysis = full_analysis(m["home"], m["away"], m["league_id"])
                # Score 1 : score le plus probable (top2_scores[0])
                top2 = analysis.get("top2_scores") or []
                pred = analysis.get("pred_score") or {}
                # On prend le score le plus vote par les 5 modeles
                top6 = analysis.get("top6_scores") or top2
                scores_to_save = []
                for sc in top6[:6]:
                    scores_to_save.append((sc["h"], sc["a"], sc.get("method", "Statistique")))
                if not scores_to_save:
                    scores_to_save = [(0, 0, "Statistique")]

                # Sauvegarder jusqu'à 6 scores predits
                for (ph, pa, method) in scores_to_save:
                    conn.execute(
                        "INSERT INTO pending_predictions "
                        "(fixture_id, home, away, league, match_date, predicted_h, predicted_a, method, confidence, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (m["id"], m["home"], m["away"], m["league"], today,
                         int(ph), int(pa), method, None,
                         f"Modele statistique automatique — {m['home']} vs {m['away']}")
                    )
                conn.commit()
                results["predicted"].append(f"{m['home']} vs {m['away']} → {s1h}-{s1a}")
                print(f"  AUTO-PREDICT: {m['home']} vs {m['away']} → {s1h}-{s1a}")
            except Exception as e:
                results["errors"].append(f"{m['home']} vs {m['away']}: {str(e)[:60]}")
                print(f"  AUTO-PREDICT ERREUR {m['home']} vs {m['away']}: {e}")

        conn.close()
    except Exception as e:
        import traceback; traceback.print_exc()
        results["ok"] = False
        results["error"] = str(e)

    return jsonify(results)

# ─────────────────────────────────────────────
# HISTORIQUE
# ─────────────────────────────────────────────

@app.route("/api/clear-predictions", methods=["POST"])
def clear_predictions():
    """Vide toutes les predictions en attente du jour pour forcer un recalcul."""""
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")
    try:
        init_predictions_table()
        conn = get_db()
        n = conn.execute(
            "DELETE FROM pending_predictions WHERE match_date=? AND status='pending'",
            (today,)
        ).rowcount
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "deleted": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/retry-predict", methods=["POST"])
def retry_predict():
    """Relance la prediction pour un match sans prediction."""
    from datetime import date as _date
    body = request.json or {}
    home       = body.get("home", "").strip()
    away       = body.get("away", "").strip()
    league_id  = int(body.get("league_id", 0))
    fixture_id = body.get("fixture_id")
    today      = _date.today().strftime("%Y-%m-%d")

    if not home or not away or not league_id:
        return jsonify({"ok": False, "error": "Parametres manquants"})
    try:
        init_predictions_table()
        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM pending_predictions WHERE home=? AND away=? AND match_date=?",
            (home, away, today)
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({"ok": True, "skipped": True})

        analysis = full_analysis(home, away, league_id)
        top2 = analysis.get("top2_scores") or []
        if top2:
            s1h, s1a = top2[0]["h"], top2[0]["a"]
            s2h, s2a = (top2[1]["h"], top2[1]["a"]) if len(top2) > 1 else (top2[0]["h"], top2[0]["a"])
        else:
            s1h = s1a = s2h = s2a = 0

        for ph, pa, method in [(s1h, s1a, "Statistique"), (s2h, s2a, "Statistique Alt.")]:
            conn.execute(
                "INSERT INTO pending_predictions "
                "(fixture_id, home, away, league, match_date, predicted_h, predicted_a, method, confidence, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fixture_id, home, away, "", today, int(ph), int(pa), method, None, "Retry")
            )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "predicted": f"{s1h}-{s1a}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/history")
def history():
    rows = get_history(50)
    return jsonify({"ok": True, "history": rows})

@app.route("/api/history/<int:hid>")
def history_detail(hid):
    row = get_analysis_by_id(hid)
    if not row:
        return jsonify({"ok": False, "error": "Introuvable"}), 404
    return jsonify({"ok": True, **row})

@app.route("/api/history/<int:hid>", methods=["DELETE"])
def history_delete(hid):
    delete_analysis(hid)
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# DEMARRAGE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"""
+==========================================+
|    MorgaIA v2  -  Serveur Flask          |
+==========================================+
  Saison  : {SEASON}
  URL     : http://localhost:{PORT}
  Gemini  : Entrer la cle dans le site
  Odds    : The Odds API activee
  Arret   : Ctrl+C
""")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8765)), debug=False)
