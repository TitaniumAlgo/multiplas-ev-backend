"""
Diagnóstico dos dados de jogador individual da ESPN (roster, líderes,
estatísticas) - antes de montar a lógica de chutes a gol por jogador.
"""

import requests

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"


def buscar_roster(liga_codigo, time_id):
    resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/teams/{time_id}/roster", timeout=20)
    resp.raise_for_status()
    return resp.json()


def buscar_lideres(liga_codigo, time_id):
    resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/teams/{time_id}", timeout=20)
    resp.raise_for_status()
    return resp.json()
