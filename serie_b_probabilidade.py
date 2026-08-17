"""
Modo alternativo: usa só a API Futebol (Série B, plano gratuito) pra
calcular a probabilidade real de cada resultado via modelo de Poisson
(baseado em gols marcados/sofridos de cada time na tabela).

IMPORTANTE: este modo NÃO usa odds de casas de apostas (não temos essa
fonte disponível pra Série B no plano gratuito). Por isso, aqui não dá
pra calcular "valor" (EV) nem comparar com o que a casa paga - só mostra
a probabilidade real de cada resultado, do mais provável pro menos
provável. Não entra no histórico de lucro/prejuízo (que depende de odd).
"""

import os
import re
import unicodedata
from datetime import date
from itertools import combinations

import requests

from multiplas_ev import expected_goals, calcular_probabilidades

API_FUTEBOL_KEY = os.environ.get("API_FUTEBOL_KEY")
API_FUTEBOL_BASE = "https://api.api-futebol.com.br/v1"

_CAMPEONATO_ID_CACHE = None


def disponivel():
    """Retorna True se a chave da API Futebol estiver configurada."""
    return bool(API_FUTEBOL_KEY)


def _normalizar_nome(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return nome.lower().strip()


def _mesmo_time(nome_a, nome_b):
    a, b = _normalizar_nome(nome_a), _normalizar_nome(nome_b)
    return a == b or a in b or b in a


def obter_campeonato_id(debug_info=None):
    """Descobre o campeonato_id da Série B (ou o único disponível no plano)."""
    global _CAMPEONATO_ID_CACHE
    if _CAMPEONATO_ID_CACHE is not None:
        return _CAMPEONATO_ID_CACHE

    resp = requests.get(
        f"{API_FUTEBOL_BASE}/campeonatos",
        headers={"Authorization": f"Bearer {API_FUTEBOL_KEY}"},
        timeout=20,
    )
    resp.raise_for_status()
    campeonatos = resp.json()
    if debug_info is not None:
        debug_info["campeonatos_disponiveis"] = [c.get("nome") for c in campeonatos if isinstance(c, dict)]

    escolhido = None
    for c in campeonatos:
        nome = (c.get("nome") or "").lower()
        if "série b" in nome or "serie b" in nome:
            escolhido = c.get("campeonato_id")
            break
    if escolhido is None and campeonatos:
        escolhido = campeonatos[0].get("campeonato_id")

    _CAMPEONATO_ID_CACHE = escolhido
    return escolhido


def _achatar(node, checar_chave, acumulado=None):
    if acumulado is None:
        acumulado = []
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and checar_chave(item):
                acumulado.append(item)
            else:
                _achatar(item, checar_chave, acumulado)
    elif isinstance(node, dict):
        if checar_chave(node):
            acumulado.append(node)
        else:
            for value in node.values():
                _achatar(value, checar_chave, acumulado)
    return acumulado


def buscar_jogos_do_dia(data_str, debug_info=None):
    campeonato_id = obter_campeonato_id(debug_info=debug_info)
    if campeonato_id is None:
        return []

    resp = requests.get(
        f"{API_FUTEBOL_BASE}/campeonatos/{campeonato_id}/partidas",
        headers={"Authorization": f"Bearer {API_FUTEBOL_KEY}"},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and "erro" in body:
        if debug_info is not None:
            debug_info["api_futebol_erro"] = body.get("erro")
        return []

    todas = _achatar(body, lambda item: "partida_id" in item)
    if debug_info is not None:
        debug_info["campeonato_id_escolhido"] = campeonato_id
        debug_info["exemplo_datas"] = sorted({str(p.get("data_realizacao", ""))[:10] for p in todas})[-8:]

    do_dia = [p for p in todas if str(p.get("data_realizacao", "")).startswith(data_str)]
    if debug_info is not None:
        debug_info["total_partidas_campeonato"] = len(todas)
        debug_info["partidas_do_dia"] = len(do_dia)
    return do_dia


def buscar_tabela(debug_info=None):
    campeonato_id = obter_campeonato_id(debug_info=debug_info)
    if campeonato_id is None:
        return {}

    resp = requests.get(
        f"{API_FUTEBOL_BASE}/campeonatos/{campeonato_id}/tabela",
        headers={"Authorization": f"Bearer {API_FUTEBOL_KEY}"},
        timeout=20,
    )
    resp.raise_for_status()
    entradas = _achatar(resp.json(), lambda item: "time" in item and "gols_pro" in item)

    tabela = {}
    for entrada in entradas:
        nome = entrada.get("time", {}).get("nome_popular")
        if not nome:
            continue
        tabela[_normalizar_nome(nome)] = {
            "nome_original": nome,
            "jogos": entrada.get("jogos", 0),
            "gols_pro": entrada.get("gols_pro", 0),
            "gols_contra": entrada.get("gols_contra", 0),
        }
    return tabela


def _stats_do_time(nome_time, tabela):
    chave = _normalizar_nome(nome_time)
    entrada = tabela.get(chave) or next((v for k, v in tabela.items() if _mesmo_time(k, chave)), None)
    if not entrada or not entrada.get("jogos"):
        return {"gols_marcados_media": 1.3, "gols_sofridos_media": 1.3}
    jogos = entrada["jogos"]
    return {
        "gols_marcados_media": entrada["gols_pro"] / jogos,
        "gols_sofridos_media": entrada["gols_contra"] / jogos,
    }


def _remover_conflitantes(selecoes):
    grupos = {}
    for s in selecoes:
        mercado = s["mercado"]
        if mercado in ("casa", "empate", "fora"):
            chave = (s["jogo"], "vencedor_do_jogo")
        elif mercado in ("over_2.5", "under_2.5"):
            chave = (s["jogo"], "linha_2.5")
        else:
            chave = (s["jogo"], mercado)
        if chave not in grupos or s["prob_real"] > grupos[chave]["prob_real"]:
            grupos[chave] = s
    return list(grupos.values())


NOMES_MERCADO = {
    "casa": None,  # preenchido dinamicamente com o nome do time da casa
    "fora": None,
    "empate": "empate",
    "over_2.5": "mais de 2,5 gols",
    "under_2.5": "menos de 2,5 gols",
}


def gerar_selecoes_probabilidade(data_str=None, prob_minima=0.5, debug_info=None):
    if data_str is None:
        data_str = date.today().isoformat()

    jogos = buscar_jogos_do_dia(data_str, debug_info=debug_info)
    tabela = buscar_tabela(debug_info=debug_info)

    selecoes = []
    for jogo in jogos:
        nome_casa = jogo["time_mandante"]["nome_popular"]
        nome_fora = jogo["time_visitante"]["nome_popular"]
        stats_casa = _stats_do_time(nome_casa, tabela)
        stats_fora = _stats_do_time(nome_fora, tabela)

        gols_casa, gols_fora = expected_goals(stats_casa, stats_fora)
        probs = calcular_probabilidades(gols_casa, gols_fora)
        nome_jogo = f"{nome_casa} x {nome_fora}"

        mapa_nomes = {
            "casa": f"{nome_casa} vencedor",
            "fora": f"{nome_fora} vencedor",
            "empate": "empate",
            "over_2.5": "mais de 2,5 gols",
            "under_2.5": "menos de 2,5 gols",
        }

        for chave_mercado, nome_mercado in mapa_nomes.items():
            prob = probs.get(chave_mercado)
            if prob is not None and prob >= prob_minima:
                selecoes.append({
                    "jogo": nome_jogo,
                    "mercado": nome_mercado,
                    "prob_real": round(prob, 4),
                })

    selecoes = _remover_conflitantes(selecoes)
    return sorted(selecoes, key=lambda s: s["prob_real"], reverse=True)


def montar_combinacoes(selecoes, min_selecoes=2, max_selecoes=3):
    """Combina seleções de jogos diferentes, mostrando só a probabilidade
    combinada (sem odd/EV, já que não temos odds reais nessa fonte)."""
    combinacoes = []
    for n in range(min_selecoes, max_selecoes + 1):
        for combo in combinations(selecoes, n):
            jogos = [s["jogo"] for s in combo]
            if len(set(jogos)) != n:
                continue
            prob_final = 1.0
            for s in combo:
                prob_final *= s["prob_real"]
            combinacoes.append({"selecoes": list(combo), "prob_final": round(prob_final, 4)})

    return sorted(combinacoes, key=lambda c: c["prob_final"], reverse=True)
