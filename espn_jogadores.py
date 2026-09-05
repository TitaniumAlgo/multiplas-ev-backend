"""
Mercados de jogador individual (chutes a gol, faltas) via ESPN.

A resposta do elenco (roster) já vem com estatística de TEMPORADA de cada
jogador (aparições, chutes a gol, faltas) - não precisa buscar jogo por
jogo, só dividir pelo número de aparições pra ter a média por partida.

Fica OPCIONAL (desligado por padrão) porque a resposta do roster é bem
grande - ativar sempre arriscaria repetir os travamentos de memória que
já tivemos antes com muitos dados de uma vez.
"""

import requests

from espn_probabilidade import ESPN_BASE, _poisson_over_under, _limitar_prob

_CACHE_ROSTER = {}
_CACHE_ROSTER_TAMANHO_MAXIMO = 30


def _guardar_no_cache_roster(chave, valor):
    if len(_CACHE_ROSTER) >= _CACHE_ROSTER_TAMANHO_MAXIMO:
        mais_antiga = next(iter(_CACHE_ROSTER))
        del _CACHE_ROSTER[mais_antiga]
    _CACHE_ROSTER[chave] = valor


def _extrair_stat_valor(categorias, nome_stat):
    for cat in categorias:
        for stat in cat.get("stats", []):
            if stat.get("name") == nome_stat:
                return stat.get("value")
    return None


def _jogadores_relevantes(liga_codigo, time_id, min_jogos=5):
    """Devolve os jogadores de linha (não-goleiros) com pelo menos
    min_jogos de aparições, e a média deles de chutes a gol e faltas
    por partida. Cacheado por liga+time."""
    chave_cache = (liga_codigo, time_id)
    if chave_cache in _CACHE_ROSTER:
        return _CACHE_ROSTER[chave_cache]

    try:
        resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/teams/{time_id}/roster", timeout=20)
        resp.raise_for_status()
        atletas = resp.json().get("athletes", [])
    except Exception:
        _guardar_no_cache_roster(chave_cache, [])
        return []

    relevantes = []
    for atleta in atletas:
        posicao = atleta.get("position", {}).get("name", "")
        if posicao == "Goalkeeper":
            continue
        categorias = atleta.get("statistics", {}).get("splits", {}).get("categories", [])
        if not categorias:
            continue
        jogos = _extrair_stat_valor(categorias, "appearances") or 0
        if jogos < min_jogos:
            continue
        chutes_gol = _extrair_stat_valor(categorias, "shotsOnTarget") or 0
        faltas = _extrair_stat_valor(categorias, "foulsCommitted") or 0
        relevantes.append({
            "nome": atleta.get("shortName") or atleta.get("displayName") or "Jogador",
            "posicao": posicao,
            "jogos": jogos,
            "chutes_gol_media": chutes_gol / jogos,
            "faltas_media": faltas / jogos,
        })

    relevantes.sort(key=lambda j: j["chutes_gol_media"], reverse=True)
    _guardar_no_cache_roster(chave_cache, relevantes)
    return relevantes


def gerar_selecoes_jogadores(liga_codigo, liga_nome, jogo_nome, time_casa_id, time_fora_id, prob_minima=0.5, top_n=1):
    """Gera seleções de 'mais/menos de 0,5 chutes a gol' e 'mais/menos de
    X,5 faltas' pro(s) jogador(es) de maior média de cada time do jogo."""
    selecoes = []
    for time_id in (time_casa_id, time_fora_id):
        jogadores = _jogadores_relevantes(liga_codigo, time_id)
        for jogador in jogadores[:top_n]:
            probs_chutes = _poisson_over_under(jogador["chutes_gol_media"], [0.5])
            probs_faltas = _poisson_over_under(jogador["faltas_media"], [1.5])
            for nome_mercado, prob in {**probs_chutes, **probs_faltas}.items():
                prob = _limitar_prob(prob)
                if prob < prob_minima:
                    continue
                sufixo = "chutes a gol" if nome_mercado in probs_chutes else "faltas"
                selecoes.append({
                    "jogo": jogo_nome,
                    "mercado": f"{nome_mercado} {sufixo} ({jogador['nome']})",
                    "prob_real": round(prob, 4),
                    "liga": liga_nome,
                })
    return selecoes
