"""
Modo de probabilidade (Poisson) usando a SportMonks - cobre as ligas que
o plano da conta liberar (hoje: Superliga Dinamarquesa e Premiership
Escocesa, por exemplo). Sem odds reais dessa fonte, então só probabilidade,
igual ao modo Série B da API Futebol.
"""

import os
import unicodedata
from datetime import date

import requests

from multiplas_ev import expected_goals, calcular_probabilidades

SPORTMONKS_TOKEN = os.environ.get("SPORTMONKS_API_TOKEN")
SPORTMONKS_BASE = "https://api.sportmonks.com/v3/football"


def disponivel():
    return bool(SPORTMONKS_TOKEN)


def _normalizar(nome):
    nome = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return nome.lower().strip()


def _mesmo_time(a, b):
    a, b = _normalizar(a), _normalizar(b)
    return a == b or (a and a in b) or (b and b in a)


def listar_ligas():
    resp = requests.get(f"{SPORTMONKS_BASE}/leagues", params={"api_token": SPORTMONKS_TOKEN}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _season_id_da_liga(liga):
    """Tenta todas as variações conhecidas do campo de temporada atual."""
    for chave in ("currentSeason", "currentseason", "current_season"):
        temporada = liga.get(chave)
        if temporada and temporada.get("id"):
            return temporada["id"]
    return None


def buscar_liga_com_temporada(liga_id):
    resp = requests.get(
        f"{SPORTMONKS_BASE}/leagues/{liga_id}",
        params={"api_token": SPORTMONKS_TOKEN, "include": "currentSeason"},
        timeout=20,
    )
    resp.raise_for_status()
    liga = resp.json().get("data", {})
    return liga, _season_id_da_liga(liga)


def _extrair_placar(fixture):
    """Tenta extrair (gols_casa, gols_fora) de um fixture com include=scores.
    A SportMonks tem mais de um formato conhecido pro campo scores - tenta
    os mais comuns e desiste (retorna None) se não reconhecer nenhum."""
    scores = fixture.get("scores") or []
    gols = {}
    for s in scores:
        descricao = (s.get("description") or "").upper()
        if descricao and descricao not in ("CURRENT", "2ND_HALF", "FULLTIME", "FT"):
            continue
        participant_id = s.get("participant_id")
        valor = s.get("score", {})
        gol = valor.get("goals") if isinstance(valor, dict) else None
        local = valor.get("participant") if isinstance(valor, dict) else s.get("location")
        if participant_id is not None and gol is not None and local in ("home", "away"):
            gols[local] = gol

    if "home" in gols and "away" in gols:
        return gols["home"], gols["away"]
    return None


def _times_do_fixture(fixture):
    participantes = fixture.get("participants") or []
    casa = next((p for p in participantes if p.get("meta", {}).get("location") == "home"), None)
    fora = next((p for p in participantes if p.get("meta", {}).get("location") == "away"), None)
    if casa is None or fora is None:
        return None, None
    return casa.get("name"), fora.get("name")


def calcular_stats_temporada(liga_id, season_id, debug_info=None):
    """Calcula média de gols marcados/sofridos de cada time na temporada,
    a partir dos jogos já disputados."""
    resp = requests.get(
        f"{SPORTMONKS_BASE}/fixtures",
        params={
            "api_token": SPORTMONKS_TOKEN,
            "filters": f"fixtureSeasons:{season_id}",
            "include": "participants;scores",
            "per_page": 50,
        },
        timeout=20,
    )
    resp.raise_for_status()
    fixtures = resp.json().get("data", [])

    stats = {}  # nome_time -> {"marcados": [...], "sofridos": [...]}
    jogos_com_placar = 0
    for fx in fixtures:
        placar = _extrair_placar(fx)
        nome_casa, nome_fora = _times_do_fixture(fx)
        if placar is None or not nome_casa or not nome_fora:
            continue
        gols_casa, gols_fora = placar
        jogos_com_placar += 1
        stats.setdefault(nome_casa, {"marcados": [], "sofridos": []})
        stats.setdefault(nome_fora, {"marcados": [], "sofridos": []})
        stats[nome_casa]["marcados"].append(gols_casa)
        stats[nome_casa]["sofridos"].append(gols_fora)
        stats[nome_fora]["marcados"].append(gols_fora)
        stats[nome_fora]["sofridos"].append(gols_casa)

    if debug_info is not None:
        debug_info.setdefault("sportmonks_jogos_com_placar_por_liga", {})[liga_id] = jogos_com_placar

    medias = {}
    for time, valores in stats.items():
        n = len(valores["marcados"]) or 1
        medias[_normalizar(time)] = {
            "nome_original": time,
            "gols_marcados_media": sum(valores["marcados"]) / n,
            "gols_sofridos_media": sum(valores["sofridos"]) / n,
        }
    return medias


def _stats_do_time(nome_time, medias):
    chave = _normalizar(nome_time)
    if chave in medias:
        return medias[chave]
    for k, v in medias.items():
        if _mesmo_time(k, chave):
            return v
    return {"gols_marcados_media": 1.3, "gols_sofridos_media": 1.3}


def buscar_jogos_do_dia(liga_id, data_str):
    resp = requests.get(
        f"{SPORTMONKS_BASE}/fixtures/date/{data_str}",
        params={"api_token": SPORTMONKS_TOKEN, "filters": f"fixtureLeagues:{liga_id}", "include": "participants"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def gerar_selecoes(data_str=None, prob_minima=0.5, debug_info=None):
    if data_str is None:
        data_str = date.today().isoformat()

    ligas = listar_ligas()
    selecoes = []

    for liga in ligas:
        if not isinstance(liga, dict):
            continue
        liga_id = liga.get("id")
        liga_nome = liga.get("name", str(liga_id))
        try:
            liga_completa, season_id = buscar_liga_com_temporada(liga_id)
            if season_id is None:
                continue
            medias = calcular_stats_temporada(liga_id, season_id, debug_info=debug_info)
            jogos_hoje = buscar_jogos_do_dia(liga_id, data_str)
        except Exception as exc:
            if debug_info is not None:
                debug_info.setdefault("erros_por_liga", {})[liga_nome] = str(exc)
            continue

        for jogo in jogos_hoje:
            nome_casa, nome_fora = _times_do_fixture(jogo)
            if not nome_casa or not nome_fora:
                continue
            stats_casa = _stats_do_time(nome_casa, medias)
            stats_fora = _stats_do_time(nome_fora, medias)
            gc, gf = expected_goals(stats_casa, stats_fora)
            probs = calcular_probabilidades(gc, gf)
            nome_jogo = f"{nome_casa} x {nome_fora}"

            mapa = {
                "casa": f"{nome_casa} vencedor",
                "fora": f"{nome_fora} vencedor",
                "empate": "empate",
                "over_2.5": "mais de 2,5 gols",
                "under_2.5": "menos de 2,5 gols",
            }
            for chave_mercado, nome_mercado in mapa.items():
                prob = probs.get(chave_mercado)
                if prob is not None and prob >= prob_minima:
                    selecoes.append({
                        "jogo": nome_jogo,
                        "mercado": nome_mercado,
                        "prob_real": round(prob, 4),
                        "liga": liga_nome,
                    })

    return sorted(selecoes, key=lambda s: s["prob_real"], reverse=True)
