"""
Lógica principal do programa de múltiplas por Valor Esperado (EV).

Etapas:
1. Estimar a probabilidade "real" de cada mercado (1X2, over/under, BTTS)
   usando um modelo de Poisson simples baseado em médias de gols.
2. Converter a odd da casa em probabilidade implícita e remover a margem
   (overround) da casa de apostas.
3. Calcular o EV de cada seleção: EV = (prob_real * odd) - 1
4. Filtrar só seleções com EV acima de um limite mínimo.
5. Montar múltiplas combinando seleções de jogos diferentes, respeitando
   um teto de odd final e um número máximo de seleções.

Este script roda com DADOS DE TESTE (sem API ainda) para você validar a
lógica antes de conectarmos a API-Football e o envio pro WhatsApp.
"""

import math
from itertools import combinations


# ---------------------------------------------------------------------
# 1. Modelo de probabilidade (Poisson) a partir de médias de gols
# ---------------------------------------------------------------------

def poisson_prob(k, lam):
    """Probabilidade de marcar exatamente k gols, dado lambda (média esperada)."""
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def expected_goals(time_casa, time_fora, media_gols_liga=2.7):
    """
    Calcula o número esperado de gols de cada time no confronto,
    a partir da força de ataque/defesa de cada um.

    time_casa / time_fora: dict com:
      - gols_marcados_media: média de gols marcados por jogo
      - gols_sofridos_media: média de gols sofridos por jogo
    """
    media_marcados_liga = media_gols_liga / 2
    media_sofridos_liga = media_gols_liga / 2

    forca_ataque_casa = time_casa["gols_marcados_media"] / media_marcados_liga
    forca_defesa_fora = time_fora["gols_sofridos_media"] / media_sofridos_liga
    gols_esperados_casa = forca_ataque_casa * forca_defesa_fora * media_marcados_liga

    forca_ataque_fora = time_fora["gols_marcados_media"] / media_marcados_liga
    forca_defesa_casa = time_casa["gols_sofridos_media"] / media_sofridos_liga
    gols_esperados_fora = forca_ataque_fora * forca_defesa_casa * media_marcados_liga

    return gols_esperados_casa, gols_esperados_fora


def calcular_probabilidades(gols_casa, gols_fora, max_gols=6):
    """
    A partir dos gols esperados de cada time, monta a matriz de placares
    possíveis (Poisson) e calcula probabilidades de mercados comuns:
    vitória casa / empate / vitória fora, over/under 2.5, BTTS (ambas marcam).
    """
    matriz = [[poisson_prob(i, gols_casa) * poisson_prob(j, gols_fora)
               for j in range(max_gols + 1)] for i in range(max_gols + 1)]

    p_casa = sum(matriz[i][j] for i in range(max_gols + 1)
                 for j in range(max_gols + 1) if i > j)
    p_empate = sum(matriz[i][j] for i in range(max_gols + 1)
                   for j in range(max_gols + 1) if i == j)
    p_fora = sum(matriz[i][j] for i in range(max_gols + 1)
                 for j in range(max_gols + 1) if i < j)

    p_over25 = sum(matriz[i][j] for i in range(max_gols + 1)
                   for j in range(max_gols + 1) if i + j > 2)
    p_under25 = 1 - p_over25

    p_btts_sim = sum(matriz[i][j] for i in range(1, max_gols + 1)
                      for j in range(1, max_gols + 1))
    p_btts_nao = 1 - p_btts_sim

    return {
        "casa": p_casa,
        "empate": p_empate,
        "fora": p_fora,
        "over_2.5": p_over25,
        "under_2.5": p_under25,
        "btts_sim": p_btts_sim,
        "btts_nao": p_btts_nao,
    }


# ---------------------------------------------------------------------
# 2. Odds da casa -> probabilidade implícita sem a margem
# ---------------------------------------------------------------------

def remover_margem(odds_dict):
    """
    odds_dict: {"casa": 2.10, "empate": 3.40, "fora": 3.20} (por exemplo)
    Remove o overround (margem da casa) normalizando as probabilidades
    implícitas para somarem 100%.
    """
    implicitas = {k: 1 / v for k, v in odds_dict.items()}
    soma = sum(implicitas.values())
    return {k: v / soma for k, v in implicitas.items()}


# ---------------------------------------------------------------------
# 3. Cálculo de EV
# ---------------------------------------------------------------------

def calcular_ev(prob_real, odd):
    """EV = (probabilidade real * odd) - 1. Ex: 0.10 = 10% de valor esperado."""
    return (prob_real * odd) - 1


def gerar_selecoes(jogos, ev_minimo=0.05):
    """
    Para cada jogo, calcula probabilidade real de cada mercado, compara
    com a odd oferecida e retorna só as seleções com EV >= ev_minimo.
    """
    selecoes = []
    for jogo in jogos:
        gols_casa, gols_fora = expected_goals(jogo["time_casa"], jogo["time_fora"])
        probs = calcular_probabilidades(gols_casa, gols_fora)

        for mercado, odd in jogo["odds"].items():
            prob_real = probs.get(mercado)
            if prob_real is None:
                continue
            ev = calcular_ev(prob_real, odd)
            if ev >= ev_minimo:
                selecoes.append({
                    "jogo": f"{jogo['time_casa']['nome']} x {jogo['time_fora']['nome']}",
                    "mercado": mercado,
                    "odd": odd,
                    "prob_real": round(prob_real, 4),
                    "ev": round(ev, 4),
                })
    return sorted(selecoes, key=lambda s: s["ev"], reverse=True)


# ---------------------------------------------------------------------
# 4. Montagem de múltiplas
# ---------------------------------------------------------------------

def montar_multiplas(selecoes, min_selecoes=2, max_selecoes=4,
                      odd_final_max=15.0, jogos_distintos=True):
    """
    Combina seleções (de jogos diferentes) em múltiplas, respeitando um
    teto de odd final. Retorna as combinações ordenadas pelo EV combinado
    (produto das probabilidades reais * odd final - 1).
    """
    multiplas = []
    for n in range(min_selecoes, max_selecoes + 1):
        for combo in combinations(selecoes, n):
            jogos_da_combo = [s["jogo"] for s in combo]
            if jogos_distintos and len(set(jogos_da_combo)) != n:
                continue  # não repete jogo na mesma múltipla

            odd_final = 1.0
            prob_final = 1.0
            for s in combo:
                odd_final *= s["odd"]
                prob_final *= s["prob_real"]

            if odd_final > odd_final_max:
                continue

            ev_final = calcular_ev(prob_final, odd_final)
            multiplas.append({
                "selecoes": combo,
                "odd_final": round(odd_final, 2),
                "prob_final": round(prob_final, 4),
                "ev_final": round(ev_final, 4),
            })

    return sorted(multiplas, key=lambda m: m["ev_final"], reverse=True)


# ---------------------------------------------------------------------
# 5. Dados de teste (substituídos pela API-Football depois)
# ---------------------------------------------------------------------

JOGOS_TESTE = [
    {
        "time_casa": {"nome": "Flamengo", "gols_marcados_media": 1.9, "gols_sofridos_media": 0.9},
        "time_fora": {"nome": "Bahia", "gols_marcados_media": 1.1, "gols_sofridos_media": 1.3},
        "odds": {"casa": 1.55, "empate": 4.20, "fora": 5.50, "over_2.5": 1.90, "btts_sim": 2.05},
    },
    {
        "time_casa": {"nome": "Palmeiras", "gols_marcados_media": 1.7, "gols_sofridos_media": 0.8},
        "time_fora": {"nome": "Fortaleza", "gols_marcados_media": 1.0, "gols_sofridos_media": 1.4},
        "odds": {"casa": 1.60, "empate": 3.90, "fora": 5.80, "over_2.5": 2.00, "btts_sim": 2.15},
    },
    {
        "time_casa": {"nome": "Corinthians", "gols_marcados_media": 1.2, "gols_sofridos_media": 1.2},
        "time_fora": {"nome": "Cruzeiro", "gols_marcados_media": 1.3, "gols_sofridos_media": 1.1},
        "odds": {"casa": 2.60, "empate": 3.10, "fora": 2.90, "over_2.5": 1.85, "btts_sim": 1.75},
    },
    {
        "time_casa": {"nome": "Grêmio", "gols_marcados_media": 1.4, "gols_sofridos_media": 1.0},
        "time_fora": {"nome": "Vitória", "gols_marcados_media": 0.9, "gols_sofridos_media": 1.6},
        "odds": {"casa": 1.75, "empate": 3.60, "fora": 4.80, "over_2.5": 2.10, "btts_sim": 2.25},
    },
]


if __name__ == "__main__":
    print("=" * 70)
    print("SELEÇÕES COM EV POSITIVO (>= 5%)")
    print("=" * 70)
    selecoes = gerar_selecoes(JOGOS_TESTE, ev_minimo=0.05)
    for s in selecoes:
        print(f"{s['jogo']:<28} | {s['mercado']:<10} | odd {s['odd']:.2f} | "
              f"prob {s['prob_real']*100:.1f}% | EV {s['ev']*100:+.1f}%")

    print()
    print("=" * 70)
    print("TOP 5 MÚLTIPLAS SUGERIDAS")
    print("=" * 70)
    multiplas = montar_multiplas(selecoes, min_selecoes=2, max_selecoes=4, odd_final_max=15.0)
    for m in multiplas[:5]:
        descricao = " + ".join(f"{s['jogo']} ({s['mercado']} @{s['odd']:.2f})" for s in m["selecoes"])
        print(f"Odd final: {m['odd_final']:.2f} | EV: {m['ev_final']*100:+.1f}%")
        print(f"  {descricao}")
        print()
