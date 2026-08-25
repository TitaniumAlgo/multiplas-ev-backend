"""
Confere resultados reais (gols, vencedor, escanteios, cartões) dos jogos
gerados no modo ESPN, e atualiza ganhou/perdeu automaticamente.
"""

import re

import requests

import historico_prob
from espn_probabilidade import LIGAS, _mesmo_time, _extrair_escanteios_cartoes, ESPN_BASE


def _buscar_eventos_finalizados(dias_atras_datas):
    """Busca eventos de várias datas passadas, nas duas ligas."""
    eventos = []
    for liga_codigo in LIGAS:
        for data_str in dias_atras_datas:
            try:
                resp = requests.get(
                    f"{ESPN_BASE}/{liga_codigo}/scoreboard",
                    params={"dates": data_str.replace("-", "")},
                    timeout=20,
                )
                if resp.status_code == 200:
                    eventos.extend(resp.json().get("events", []))
            except Exception:
                continue
    return eventos


def _placar_e_extras(evento):
    comp = (evento.get("competitions") or [{}])[0]
    status = comp.get("status", {}).get("type", {})
    if not status.get("completed"):
        return None

    competidores = comp.get("competitors", [])
    casa = next((c for c in competidores if c.get("homeAway") == "home"), None)
    fora = next((c for c in competidores if c.get("homeAway") == "away"), None)
    if not casa or not fora:
        return None

    try:
        gols_casa, gols_fora = int(casa.get("score", 0)), int(fora.get("score", 0))
    except (TypeError, ValueError):
        return None

    extras = _extrair_escanteios_cartoes(evento)
    id_casa, id_fora = casa["team"]["id"], fora["team"]["id"]
    escanteios_total = (extras.get(id_casa, {}).get("escanteios", 0) or 0) + (extras.get(id_fora, {}).get("escanteios", 0) or 0)
    cartoes_total = (extras.get(id_casa, {}).get("cartoes", 0) or 0) + (extras.get(id_fora, {}).get("cartoes", 0) or 0)

    return {
        "nome_casa": casa["team"]["displayName"],
        "nome_fora": fora["team"]["displayName"],
        "gols_casa": gols_casa,
        "gols_fora": gols_fora,
        "escanteios_total": escanteios_total,
        "cartoes_total": cartoes_total,
    }


def _encontrar_resultado(nome_jogo, resultados):
    if " x " not in nome_jogo:
        return None
    nome_casa, nome_fora = nome_jogo.split(" x ", 1)
    for r in resultados:
        if _mesmo_time(nome_casa, r["nome_casa"]) and _mesmo_time(nome_fora, r["nome_fora"]):
            return r
    return None


def _avaliar_selecao(selecao, resultado):
    mercado = selecao["mercado"]
    gc, gf = resultado["gols_casa"], resultado["gols_fora"]

    if mercado == "empate":
        return gc == gf
    if mercado.endswith(" vencedor"):
        nome_time = mercado[: -len(" vencedor")]
        if _mesmo_time(nome_time, resultado["nome_casa"]):
            return gc > gf
        if _mesmo_time(nome_time, resultado["nome_fora"]):
            return gf > gc
        return None

    m = re.match(r"mais de ([\d,]+) gols", mercado)
    if m:
        return (gc + gf) > float(m.group(1).replace(",", "."))
    m = re.match(r"menos de ([\d,]+) gols", mercado)
    if m:
        return (gc + gf) < float(m.group(1).replace(",", "."))

    m = re.match(r"mais de ([\d,]+) escanteios", mercado)
    if m:
        return resultado["escanteios_total"] > float(m.group(1).replace(",", "."))
    m = re.match(r"menos de ([\d,]+) escanteios", mercado)
    if m:
        return resultado["escanteios_total"] < float(m.group(1).replace(",", "."))

    m = re.match(r"mais de ([\d,]+) cartões", mercado)
    if m:
        return resultado["cartoes_total"] > float(m.group(1).replace(",", "."))
    m = re.match(r"menos de ([\d,]+) cartões", mercado)
    if m:
        return resultado["cartoes_total"] < float(m.group(1).replace(",", "."))

    return None


def atualizar_pendentes():
    from datetime import date, timedelta

    pendentes = [item for item in historico_prob.listar_historico() if item["resultado"] == "pendente"]
    if not pendentes:
        return 0

    # busca só as datas que realmente têm pendente, evitando chamadas à toa
    datas_necessarias = sorted({item["data_jogo"] for item in pendentes})
    eventos = _buscar_eventos_finalizados(datas_necessarias)

    resultados = []
    for ev in eventos:
        r = _placar_e_extras(ev)
        if r:
            resultados.append(r)

    atualizadas = 0
    for item in pendentes:
        avaliacoes = []
        for selecao in item["selecoes"]:
            resultado = _encontrar_resultado(selecao["jogo"], resultados)
            if resultado is None:
                avaliacoes.append(None)
                continue
            avaliacoes.append(_avaliar_selecao(selecao, resultado))

        if any(a is False for a in avaliacoes):
            historico_prob.marcar_resultado(item["id"], "perdeu", selecoes_resultado=avaliacoes)
            atualizadas += 1
        elif avaliacoes and all(a is True for a in avaliacoes):
            historico_prob.marcar_resultado(item["id"], "ganhou", selecoes_resultado=avaliacoes)
            atualizadas += 1

    return atualizadas
