"""
Confere os resultados reais dos jogos da Série B (via API Futebol, mesma
fonte usada pra gerar as previsões) e marca ganhou/perdeu automaticamente
no historico_prob.
"""

import re

import historico_prob
from serie_b_probabilidade import (
    API_FUTEBOL_BASE, API_FUTEBOL_KEY, obter_campeonato_id, _mesmo_time, _achatar,
)
import requests


def _buscar_partidas_com_placar():
    campeonato_id = obter_campeonato_id()
    if campeonato_id is None:
        return []
    resp = requests.get(
        f"{API_FUTEBOL_BASE}/campeonatos/{campeonato_id}/partidas",
        headers={"Authorization": f"Bearer {API_FUTEBOL_KEY}"},
        timeout=20,
    )
    if resp.status_code != 200:
        return []
    body = resp.json()
    if isinstance(body, dict) and "erro" in body:
        return []
    return _achatar(body, lambda item: "partida_id" in item)


def _placar_finalizado(partida):
    pm, pv = partida.get("placar_mandante"), partida.get("placar_visitante")
    if pm is None or pv is None:
        return None
    return int(pm), int(pv)


def _encontrar_partida(nome_jogo, partidas):
    if " x " not in nome_jogo:
        return None
    nome_casa, nome_fora = nome_jogo.split(" x ", 1)
    for p in partidas:
        if _mesmo_time(nome_casa, p["time_mandante"]["nome_popular"]) and _mesmo_time(nome_fora, p["time_visitante"]["nome_popular"]):
            return p
    return None


def _avaliar_selecao(selecao, placar):
    gols_casa, gols_fora = placar
    mercado = selecao["mercado"]

    if mercado == "empate":
        return gols_casa == gols_fora
    # "vencedor" é tratado por _avaliar_vencedor_correto (precisa do nome do time)

    m = re.match(r"mais de ([\d,]+) gols", mercado)
    if m:
        linha = float(m.group(1).replace(",", "."))
        return (gols_casa + gols_fora) > linha
    m = re.match(r"menos de ([\d,]+) gols", mercado)
    if m:
        linha = float(m.group(1).replace(",", "."))
        return (gols_casa + gols_fora) < linha
    return None


def _avaliar_vencedor_correto(selecao, partida, placar):
    """Versão mais precisa pro mercado 'vencedor', usando o nome real do time."""
    gols_casa, gols_fora = placar
    nome_time = selecao["mercado"][: -len(" vencedor")]
    nome_casa = partida["time_mandante"]["nome_popular"]
    nome_fora = partida["time_visitante"]["nome_popular"]
    if _mesmo_time(nome_time, nome_casa):
        return gols_casa > gols_fora
    if _mesmo_time(nome_time, nome_fora):
        return gols_fora > gols_casa
    return None


def atualizar_pendentes():
    pendentes = [item for item in historico_prob.listar_historico() if item["resultado"] == "pendente"]
    if not pendentes:
        return 0

    partidas = _buscar_partidas_com_placar()
    atualizadas = 0

    for item in pendentes:
        resultados_pernas = []
        for selecao in item["selecoes"]:
            partida = _encontrar_partida(selecao["jogo"], partidas)
            if partida is None:
                resultados_pernas.append(None)
                continue
            placar = _placar_finalizado(partida)
            if placar is None:
                resultados_pernas.append(None)
                continue
            if selecao["mercado"].endswith(" vencedor"):
                resultados_pernas.append(_avaliar_vencedor_correto(selecao, partida, placar))
            else:
                resultados_pernas.append(_avaliar_selecao(selecao, placar))

        if any(r is False for r in resultados_pernas):
            historico_prob.marcar_resultado(item["id"], "perdeu")
            atualizadas += 1
        elif resultados_pernas and all(r is True for r in resultados_pernas):
            historico_prob.marcar_resultado(item["id"], "ganhou")
            atualizadas += 1

    return atualizadas
