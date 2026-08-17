"""
Integração 100% baseada na The Odds API (Série A + Série B do Brasileirão).

Sem fonte externa de estatísticas: a "probabilidade real" de cada seleção
é estimada pelo CONSENSO DO MERCADO — a média das probabilidades implícitas
(sem a margem) de várias casas de apostas para o mesmo jogo. Depois comparamos
essa probabilidade de consenso com a MELHOR odd individual disponível.

Se uma casa paga bem acima do que o mercado como um todo indica, isso é
valor (EV positivo). É a mesma lógica usada por apostadores profissionais
("devig" / linha de consenso).

CHAVE DE API: configure como variável de ambiente antes de rodar
(ou como Environment Variable no Render):

    export ODDS_API_KEY="sua_chave_aqui"
"""

import os
import re
from datetime import date

import requests

from multiplas_ev import calcular_ev, montar_multiplas

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
if not ODDS_API_KEY:
    raise RuntimeError("Configure a variável de ambiente ODDS_API_KEY antes de rodar.")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_SPORT_KEYS = ["soccer_brazil_campeonato", "soccer_brazil_serie_b"]


def buscar_odds_the_odds_api(debug_info=None):
    todos_jogos = []
    contagem_por_liga = {}
    for sport_key in ODDS_API_SPORT_KEYS:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "eu,uk,us", "markets": "h2h,totals", "oddsFormat": "decimal"},
            timeout=15,
        )
        if resp.status_code == 200:
            jogos = resp.json()
            todos_jogos.extend(jogos)
            contagem_por_liga[sport_key] = len(jogos)
        else:
            contagem_por_liga[sport_key] = f"erro {resp.status_code}"

    if debug_info is not None:
        debug_info["jogos_por_liga"] = contagem_por_liga
        debug_info["total_jogos_odds_api"] = len(todos_jogos)

    return todos_jogos


def _devig_h2h(outcomes):
    """Remove a margem (overround) de um mercado de 2 ou 3 resultados."""
    implicitas = {o["name"]: 1 / o["price"] for o in outcomes}
    soma = sum(implicitas.values())
    return {nome: p / soma for nome, p in implicitas.items()}


def probabilidade_consenso_e_melhor_odd(jogo_odds):
    """
    Para um jogo, percorre todas as casas de apostas e calcula:
    - probabilidade de consenso (média das probabilidades "devigadas" de cada casa)
    - melhor odd individual disponível para cada resultado
    Retorna um dicionário por mercado: {"Santos vencedor": {"prob_real": x, "odd": y}, ...}

    Cobre: vencedor (casa/fora/empate) e todas as linhas de over/under que
    as casas oferecerem (1.5, 2.5, 3.5, etc - não só uma fixa).
    """
    probs_por_mercado = {}  # nome do mercado -> lista de probabilidades devigadas (uma por casa)
    melhor_odd_por_mercado = {}

    nome_casa, nome_fora = jogo_odds["home_team"], jogo_odds["away_team"]

    def registrar(nome_mercado, prob, odd):
        probs_por_mercado.setdefault(nome_mercado, []).append(prob)
        if nome_mercado not in melhor_odd_por_mercado or odd > melhor_odd_por_mercado[nome_mercado]:
            melhor_odd_por_mercado[nome_mercado] = odd

    for casa in jogo_odds.get("bookmakers", []):
        for mercado in casa.get("markets", []):
            if mercado["key"] == "h2h":
                devigadas = _devig_h2h(mercado["outcomes"])
                for outcome in mercado["outcomes"]:
                    if outcome["name"] == nome_casa:
                        nome_mercado = f"{nome_casa} vencedor"
                    elif outcome["name"] == nome_fora:
                        nome_mercado = f"{nome_fora} vencedor"
                    else:
                        nome_mercado = "empate"
                    registrar(nome_mercado, devigadas[outcome["name"]], outcome["price"])

            elif mercado["key"] == "totals":
                # agrupa os outcomes por linha (1.5, 2.5, 3.5...) - uma casa pode
                # oferecer mais de uma linha ao mesmo tempo
                por_linha = {}
                for outcome in mercado["outcomes"]:
                    ponto = outcome.get("point")
                    if ponto is None:
                        continue
                    por_linha.setdefault(ponto, []).append(outcome)

                for ponto, outcomes_da_linha in por_linha.items():
                    if len(outcomes_da_linha) != 2:
                        continue
                    devigadas = _devig_h2h(outcomes_da_linha)
                    linha_fmt = str(ponto).replace(".", ",")
                    for outcome in outcomes_da_linha:
                        nome_mercado = (
                            f"mais de {linha_fmt} gols" if outcome["name"].lower() == "over"
                            else f"menos de {linha_fmt} gols"
                        )
                        registrar(nome_mercado, devigadas[outcome["name"]], outcome["price"])

    resultado = {}
    for mercado, probs in probs_por_mercado.items():
        if len(probs) < 4:
            continue  # precisa de pelo menos 4 casas pra formar um consenso confiável
        prob_media = sum(probs) / len(probs)
        resultado[mercado] = {"prob_real": prob_media, "odd": melhor_odd_por_mercado[mercado], "n_casas": len(probs)}

    return resultado


def _remover_selecoes_conflitantes(selecoes):
    """
    Evita mostrar resultados que se excluem mutuamente do mesmo jogo como se
    fossem seleções independentes pra combinar (ex: 'Corinthians vencedor',
    'empate' e 'Cruzeiro vencedor' não podem acontecer juntos - só um é
    possível). Também evita 'mais de X gols' e 'menos de X gols' da mesma
    linha aparecerem juntos. Mantém sempre a de maior EV do grupo.
    """
    grupos = {}
    for s in selecoes:
        mercado = s["mercado"]
        if mercado.endswith(" vencedor") or mercado == "empate":
            chave = (s["jogo"], "vencedor_do_jogo")
        else:
            m = re.match(r"(?:mais|menos) de ([\d,]+) gols", mercado)
            chave = (s["jogo"], f"linha_{m.group(1)}") if m else (s["jogo"], mercado)

        if chave not in grupos or s["ev"] > grupos[chave]["ev"]:
            grupos[chave] = s

    return sorted(grupos.values(), key=lambda s: s["ev"], reverse=True)


def gerar_selecoes_consenso(jogos_odds, data_str=None, ev_minimo=0.03, prob_minima=0.5):
    selecoes = []
    for jogo_odds in jogos_odds:
        if data_str and not str(jogo_odds.get("commence_time", "")).startswith(data_str):
            continue

        mercados = probabilidade_consenso_e_melhor_odd(jogo_odds)
        nome_jogo = f"{jogo_odds['home_team']} x {jogo_odds['away_team']}"

        for mercado, dados in mercados.items():
            ev = calcular_ev(dados["prob_real"], dados["odd"])
            if ev >= ev_minimo and dados["prob_real"] >= prob_minima:
                selecoes.append({
                    "jogo": nome_jogo,
                    "mercado": mercado,
                    "odd": dados["odd"],
                    "prob_real": round(dados["prob_real"], 4),
                    "ev": round(ev, 4),
                    "n_casas": dados["n_casas"],
                })

    selecoes = _remover_selecoes_conflitantes(selecoes)
    return sorted(selecoes, key=lambda s: s["prob_real"], reverse=True)


def montar_jogos_reais(data_str=None, debug_info=None, ev_minimo=0.03, prob_minima=0.5):
    """Mantém o nome usado pelo server.py; retorna direto as seleções + total de jogos."""
    if data_str is None:
        data_str = date.today().isoformat()

    jogos_odds = buscar_odds_the_odds_api(debug_info=debug_info)
    selecoes = gerar_selecoes_consenso(jogos_odds, data_str=data_str, ev_minimo=ev_minimo, prob_minima=prob_minima)

    if debug_info is not None:
        debug_info["jogos_na_data_pedida"] = len(
            [j for j in jogos_odds if str(j.get("commence_time", "")).startswith(data_str)]
        )

    return selecoes


if __name__ == "__main__":
    debug_info = {}
    selecoes = montar_jogos_reais(debug_info=debug_info)
    print("DEBUG:", debug_info)
    print(f"\n{len(selecoes)} seleção(ões) com EV positivo.\n")
    for s in selecoes:
        print(f"{s['jogo']:<30} | {s['mercado']:<10} | odd {s['odd']:.2f} | prob {s['prob_real']*100:.1f}% | EV {s['ev']*100:+.1f}%")

    multiplas = montar_multiplas(selecoes, min_selecoes=2, max_selecoes=4, odd_final_max=15.0)
    print(f"\n{len(multiplas)} múltipla(s) possível(is)")
    for m in multiplas[:5]:
        print(f"Odd final: {m['odd_final']:.2f} | EV: {m['ev_final']*100:+.1f}%")
