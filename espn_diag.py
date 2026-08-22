"""
Diagnóstico do endpoint não-oficial da ESPN pra jogos do Brasileirão.
Sem chave, sem cadastro - mas por ser não-oficial, pode mudar/quebrar
sem aviso, então valida a estrutura antes de confiar nela.
"""

import requests

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports/soccer"

LIGAS_BRASIL = {"serie_a": "bra.1", "serie_b": "bra.2"}


def buscar_scoreboard(liga_codigo, data_str=None):
    """data_str no formato YYYYMMDD (ou None pra hoje)."""
    params = {}
    if data_str:
        params["dates"] = data_str.replace("-", "")
    resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/scoreboard", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def buscar_standings(liga_codigo):
    resp = requests.get(f"{ESPN_STANDINGS_BASE}/{liga_codigo}/standings", timeout=20)
    resp.raise_for_status()
    return resp.json()
