"""
Integração com as APIs reais.

- API-Football  -> estatísticas dos times (médias de gols marcados/sofridos)
- The Odds API  -> odds comparadas entre casas de apostas

O fluxo casa os jogos das duas fontes pelo nome dos times (normalizado) e
monta a mesma estrutura de "jogo" que o multiplas_ev.py já sabe processar.

IMPORTANTE - CHAVES DE API:
As chaves NUNCA ficam escritas neste arquivo. Configure como variável
de ambiente antes de rodar (ou como "Environment Variable" no Render):

    export API_FOOTBALL_KEY="sua_chave_aqui"
    export ODDS_API_KEY="sua_chave_aqui"
    python3 buscar_jogos_reais.py
"""

import os
import re
import unicodedata
from datetime import date

import requests

from multiplas_ev import gerar_selecoes, montar_multiplas

# ---------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

if not API_FOOTBALL_KEY or not ODDS_API_KEY:
    raise RuntimeError(
        "Configure as variáveis de ambiente API_FOOTBALL_KEY e ODDS_API_KEY "
        "antes de rodar (ou como Environment Variables no Render)."
    )

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Ligas que a API-Football usa (id interno). Ex: 71 = Brasileirão Série A.
LIGA_ID = 71
TEMPORADA = 2026

# Esporte/liga equivalente na The Odds API
ODDS_API_SPORT_KEY = "soccer_brazil_campeonato"


# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------

def normalizar_nome(nome):
    """Remove acentos, deixa minúsculo e tira sufixos comuns pra comparar nomes de times."""
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = nome.lower().strip()
    nome = re.sub(r"\b(fc|ec|sc|ac|esporte clube|futebol clube)\b", "", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    return nome


# ---------------------------------------------------------------------
# API-Football: jogos do dia + estatísticas dos times
# ---------------------------------------------------------------------

def buscar_jogos_do_dia_api_football(data_str=None):
    """Retorna a lista de partidas do dia para a liga/temporada configuradas."""
    if data_str is None:
        data_str = date.today().isoformat()

    resp = requests.get(
        f"{API_FOOTBALL_BASE}/fixtures",
        headers={"x-apisports-key": API_FOOTBALL_KEY},
        params={"date": data_str, "league": LIGA_ID, "season": TEMPORADA},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("response", [])


def buscar_estatisticas_time(team_id):
    """Retorna médias de gols marcados/sofridos do time na temporada."""
    resp = requests.get(
        f"{API_FOOTBALL_BASE}/teams/statistics",
        headers={"x-apisports-key": API_FOOTBALL_KEY},
        params={"team": team_id, "league": LIGA_ID, "season": TEMPORADA},
        timeout=15,
    )
    resp.raise_for_status()
    stats = resp.json().get("response", {})

    try:
        jogos = stats["fixtures"]["played"]["total"] or 1
        gols_marcados = stats["goals"]["for"]["total"]["total"] / jogos
        gols_sofridos = stats["goals"]["against"]["total"]["total"] / jogos
    except (KeyError, TypeError, ZeroDivisionError):
        # Fallback conservador se a temporada ainda não tiver dados suficientes
        gols_marcados, gols_sofridos = 1.3, 1.3

    return gols_marcados, gols_sofridos


# ---------------------------------------------------------------------
# The Odds API: odds comparadas entre casas
# ---------------------------------------------------------------------

def buscar_odds_the_odds_api():
    """Retorna odds (melhor preço entre casas) para os jogos disponíveis."""
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/{ODDS_API_SPORT_KEY}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def melhor_odd_por_mercado(jogo_odds):
    """
    Dado um jogo retornado pela The Odds API, varre todas as casas e
    fica com a MELHOR odd (maior) disponível para cada mercado.
    """
    melhores = {}
    for casa in jogo_odds.get("bookmakers", []):
        for mercado in casa.get("markets", []):
            if mercado["key"] == "h2h":
                nomes = ["casa", "empate", "fora"]
                for outcome, nome_mercado in zip(mercado["outcomes"], nomes):
                    odd = outcome["price"]
                    if nome_mercado not in melhores or odd > melhores[nome_mercado]:
                        melhores[nome_mercado] = odd
            elif mercado["key"] == "totals":
                for outcome in mercado["outcomes"]:
                    if outcome.get("point") == 2.5:
                        chave = "over_2.5" if outcome["name"].lower() == "over" else "under_2.5"
                        odd = outcome["price"]
                        if chave not in melhores or odd > melhores[chave]:
                            melhores[chave] = odd
    return melhores


# ---------------------------------------------------------------------
# Junta as duas fontes num único pacote de "jogos" pro multiplas_ev.py
# ---------------------------------------------------------------------

def montar_jogos_reais(data_str=None):
    fixtures = buscar_jogos_do_dia_api_football(data_str)
    odds_lista = buscar_odds_the_odds_api()

    # index das odds por par de nomes normalizados
    odds_por_confronto = {}
    for jogo_odds in odds_lista:
        chave = (normalizar_nome(jogo_odds["home_team"]), normalizar_nome(jogo_odds["away_team"]))
        odds_por_confronto[chave] = melhor_odd_por_mercado(jogo_odds)

    jogos = []
    for fixture in fixtures:
        time_casa_info = fixture["teams"]["home"]
        time_fora_info = fixture["teams"]["away"]

        chave = (normalizar_nome(time_casa_info["name"]), normalizar_nome(time_fora_info["name"]))
        odds = odds_por_confronto.get(chave)
        if not odds:
            continue  # sem odds pra esse jogo nas duas fontes, pula

        gm_casa, gs_casa = buscar_estatisticas_time(time_casa_info["id"])
        gm_fora, gs_fora = buscar_estatisticas_time(time_fora_info["id"])

        jogos.append({
            "time_casa": {
                "nome": time_casa_info["name"],
                "gols_marcados_media": gm_casa,
                "gols_sofridos_media": gs_casa,
            },
            "time_fora": {
                "nome": time_fora_info["name"],
                "gols_marcados_media": gm_fora,
                "gols_sofridos_media": gs_fora,
            },
            "odds": odds,
        })

    return jogos


# ---------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------

if __name__ == "__main__":
    print("Buscando jogos e odds reais...")
    jogos = montar_jogos_reais()
    print(f"{len(jogos)} jogo(s) casado(s) entre as duas APIs (com odds disponíveis).\n")

    if not jogos:
        print("Nenhum jogo encontrado pra hoje com odds nas duas fontes. "
              "Isso é normal fora de dias de rodada, ou se a liga/temporada "
              "configurada (LIGA_ID / TEMPORADA / ODDS_API_SPORT_KEY) não "
              "bater com o campeonato que está rolando agora.")
    else:
        selecoes = gerar_selecoes(jogos, ev_minimo=0.05)
        print("=" * 70)
        print("SELEÇÕES COM EV POSITIVO (>= 5%)")
        print("=" * 70)
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
