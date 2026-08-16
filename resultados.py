"""
Confere resultados reais dos jogos (via The Odds API) e atualiza
automaticamente o status das múltiplas salvas no histórico:
ganhou (todas as pernas bateram), perdeu (pelo menos uma não bateu),
ou continua pendente (algum jogo ainda não terminou).
"""

import re

import requests

import historico
from buscar_jogos_reais import ODDS_API_BASE, ODDS_API_KEY, ODDS_API_SPORT_KEYS, mesmo_time


def buscar_resultados_reais(dias_atras=3):
    """Busca jogos já concluídos nas últimas N dias, nas ligas configuradas."""
    partidas = []
    for sport_key in ODDS_API_SPORT_KEYS:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/scores",
            params={"apiKey": ODDS_API_KEY, "daysFrom": dias_atras},
            timeout=15,
        )
        if resp.status_code == 200:
            partidas.extend(resp.json())
    return partidas


def _placar_do_time(jogo, nome_time):
    if not jogo.get("scores"):
        return None
    for entrada in jogo["scores"]:
        if mesmo_time(entrada["name"], nome_time):
            try:
                return int(entrada["score"])
            except (TypeError, ValueError):
                return None
    return None


def _avaliar_selecao(selecao, jogo_real):
    """Retorna True (bateu), False (não bateu) ou None (não deu pra avaliar)."""
    nome_casa, nome_fora = jogo_real["home_team"], jogo_real["away_team"]
    gols_casa = _placar_do_time(jogo_real, nome_casa)
    gols_fora = _placar_do_time(jogo_real, nome_fora)
    if gols_casa is None or gols_fora is None:
        return None

    mercado = selecao["mercado"]

    if mercado == "empate":
        return gols_casa == gols_fora

    if mercado.endswith(" vencedor"):
        nome_time = mercado[: -len(" vencedor")]
        if mesmo_time(nome_time, nome_casa):
            return gols_casa > gols_fora
        if mesmo_time(nome_time, nome_fora):
            return gols_fora > gols_casa
        return None

    m = re.match(r"mais de ([\d,]+) gols", mercado)
    if m:
        linha = float(m.group(1).replace(",", "."))
        return (gols_casa + gols_fora) > linha

    m = re.match(r"menos de ([\d,]+) gols", mercado)
    if m:
        linha = float(m.group(1).replace(",", "."))
        return (gols_casa + gols_fora) < linha

    return None


def _encontrar_jogo_real(nome_jogo, jogos_reais):
    """nome_jogo vem como 'Time Casa x Time Fora'."""
    if " x " not in nome_jogo:
        return None
    nome_casa, nome_fora = nome_jogo.split(" x ", 1)
    for jogo in jogos_reais:
        if not jogo.get("completed"):
            continue
        if mesmo_time(nome_casa, jogo["home_team"]) and mesmo_time(nome_fora, jogo["away_team"]):
            return jogo
    return None


def atualizar_pendentes():
    """Confere todas as múltiplas pendentes contra os resultados reais e
    atualiza o status no histórico. Retorna quantas foram atualizadas."""
    pendentes = [item for item in historico.listar_historico() if item["resultado"] == "pendente"]
    if not pendentes:
        return 0

    jogos_reais = buscar_resultados_reais()
    atualizadas = 0

    for item in pendentes:
        resultados_das_pernas = []
        for selecao in item["selecoes"]:
            jogo_real = _encontrar_jogo_real(selecao["jogo"], jogos_reais)
            if jogo_real is None:
                resultados_das_pernas.append(None)  # jogo ainda não terminou / não encontrado
                continue
            resultados_das_pernas.append(_avaliar_selecao(selecao, jogo_real))

        if any(r is False for r in resultados_das_pernas):
            historico.marcar_resultado(item["id"], "perdeu")
            atualizadas += 1
        elif all(r is True for r in resultados_das_pernas):
            historico.marcar_resultado(item["id"], "ganhou")
            atualizadas += 1
        # se tiver None no meio (sem False), continua pendente - algum jogo não terminou ainda

    return atualizadas
