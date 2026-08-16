"""
Integração com as APIs reais.

- API Futebol (api-futebol.com.br) -> jogos do dia e estatísticas dos times
- The Odds API                     -> odds comparadas entre casas de apostas

O fluxo casa os jogos das duas fontes pelo nome dos times (normalizado) e
monta a mesma estrutura de "jogo" que o multiplas_ev.py já sabe processar.

CHAVES DE API: configure como variável de ambiente antes de rodar
(ou como Environment Variable no Render):

    export API_FUTEBOL_KEY="sua_chave_aqui"
    export ODDS_API_KEY="sua_chave_aqui"
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

API_FUTEBOL_KEY = os.environ.get("API_FUTEBOL_KEY")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

if not API_FUTEBOL_KEY or not ODDS_API_KEY:
    raise RuntimeError(
        "Configure as variáveis de ambiente API_FUTEBOL_KEY e ODDS_API_KEY "
        "antes de rodar (ou como Environment Variables no Render)."
    )

API_FUTEBOL_BASE = "https://api.api-futebol.com.br/v1"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

CAMPEONATO_ID = None  # descoberto automaticamente (plano grátis inclui Série B)
ODDS_API_SPORT_KEYS = ["soccer_brazil_campeonato", "soccer_brazil_serie_b"]


def obter_campeonato_id(debug_info=None):
    """Descobre o campeonato_id certo consultando /v1/campeonatos (evita
    depender de um número fixo, já que o plano contratado define qual
    campeonato está liberado)."""
    global CAMPEONATO_ID
    if CAMPEONATO_ID is not None:
        return CAMPEONATO_ID

    resp = requests.get(
        f"{API_FUTEBOL_BASE}/campeonatos",
        headers={"Authorization": f"Bearer {API_FUTEBOL_KEY}"},
        timeout=20,
    )
    resp.raise_for_status()
    campeonatos = resp.json()
    if debug_info is not None:
        debug_info["campeonatos_disponiveis"] = [
            c.get("nome") for c in campeonatos if isinstance(c, dict)
        ]

    escolhido = None
    for c in campeonatos:
        nome = (c.get("nome") or "").lower()
        if "série b" in nome or "serie b" in nome:
            escolhido = c.get("campeonato_id")
            break
    if escolhido is None and campeonatos:
        escolhido = campeonatos[0].get("campeonato_id")

    CAMPEONATO_ID = escolhido
    return CAMPEONATO_ID

ALIASES = {
    "atletico mg": "atletico mineiro",
    "atletico-mg": "atletico mineiro",
    "atl mineiro": "atletico mineiro",
    "vasco": "vasco da gama",
    "inter": "internacional",
    "bragantino": "red bull bragantino",
    "rb bragantino": "red bull bragantino",
}


def normalizar_nome(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = nome.lower().strip()
    nome = nome.replace("-", " ")
    nome = re.sub(r"\b(fc|ec|sc|ac|esporte clube|futebol clube|clube de regatas)\b", "", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    nome = ALIASES.get(nome, nome)
    return nome


def mesmo_time(nome_a, nome_b):
    a, b = normalizar_nome(nome_a), normalizar_nome(nome_b)
    if a == b:
        return True
    if a in b or b in a:
        return True
    tokens_a = {t for t in a.split() if len(t) > 2}
    tokens_b = {t for t in b.split() if len(t) > 2}
    if not tokens_a or not tokens_b:
        return False
    return tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a)


def buscar_jogos_do_dia_api_futebol(data_str=None, debug_info=None):
    if data_str is None:
        data_str = date.today().isoformat()

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

    todas_partidas = _achatar_partidas(body)
    partidas_do_dia = [
        p for p in todas_partidas
        if str(p.get("data_realizacao", "")).startswith(data_str)
    ]

    if debug_info is not None:
        debug_info["total_partidas_campeonato"] = len(todas_partidas)
        debug_info["partidas_do_dia_api_futebol"] = len(partidas_do_dia)

    return partidas_do_dia


def _achatar_partidas(node, acumulado=None):
    if acumulado is None:
        acumulado = []
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and "partida_id" in item:
                acumulado.append(item)
            else:
                _achatar_partidas(item, acumulado)
    elif isinstance(node, dict):
        if "partida_id" in node:
            acumulado.append(node)
        else:
            for value in node.values():
                _achatar_partidas(value, acumulado)
    return acumulado


def buscar_tabela_api_futebol(debug_info=None):
    campeonato_id = obter_campeonato_id(debug_info=debug_info)
    if campeonato_id is None:
        return {}

    resp = requests.get(
        f"{API_FUTEBOL_BASE}/campeonatos/{campeonato_id}/tabela",
        headers={"Authorization": f"Bearer {API_FUTEBOL_KEY}"},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()

    entradas = _achatar_tabela(body)
    tabela = {}
    for entrada in entradas:
        time_info = entrada.get("time", {})
        nome = time_info.get("nome_popular")
        if not nome:
            continue
        tabela[normalizar_nome(nome)] = {
            "nome_original": nome,
            "jogos": entrada.get("jogos", 0),
            "gols_pro": entrada.get("gols_pro", 0),
            "gols_contra": entrada.get("gols_contra", 0),
        }

    if debug_info is not None:
        debug_info["times_na_tabela"] = len(tabela)

    return tabela


def _achatar_tabela(node, acumulado=None):
    if acumulado is None:
        acumulado = []
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and "time" in item and "gols_pro" in item:
                acumulado.append(item)
            else:
                _achatar_tabela(item, acumulado)
    elif isinstance(node, dict):
        if "time" in node and "gols_pro" in node:
            acumulado.append(node)
        else:
            for value in node.values():
                _achatar_tabela(value, acumulado)
    return acumulado


def buscar_estatisticas_time(nome_time, tabela):
    chave = normalizar_nome(nome_time)
    entrada = tabela.get(chave)
    if entrada is None:
        entrada = next((v for k, v in tabela.items() if mesmo_time(k, chave)), None)

    if not entrada or not entrada.get("jogos"):
        return 1.3, 1.3

    jogos = entrada["jogos"]
    return entrada["gols_pro"] / jogos, entrada["gols_contra"] / jogos


def buscar_odds_the_odds_api():
    todos_jogos = []
    for sport_key in ODDS_API_SPORT_KEYS:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"},
            timeout=15,
        )
        if resp.status_code == 200:
            todos_jogos.extend(resp.json())
    return todos_jogos


def melhor_odd_por_mercado(jogo_odds):
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


def montar_jogos_reais(data_str=None, debug_info=None):
    if data_str is None:
        data_str = date.today().isoformat()

    partidas = buscar_jogos_do_dia_api_futebol(data_str, debug_info=debug_info)
    tabela = buscar_tabela_api_futebol(debug_info=debug_info)
    odds_lista = buscar_odds_the_odds_api()

    if debug_info is not None:
        debug_info["jogos_odds_api"] = len(odds_lista)
        debug_info["nomes_times_odds_api"] = [f"{j['home_team']} x {j['away_team']}" for j in odds_lista[:15]]
        debug_info["nomes_times_api_futebol"] = [
            f"{p['time_mandante']['nome_popular']} x {p['time_visitante']['nome_popular']}" for p in partidas[:15]
        ]

    jogos = []
    for partida in partidas:
        nome_casa = partida["time_mandante"]["nome_popular"]
        nome_fora = partida["time_visitante"]["nome_popular"]

        odds_encontradas = None
        for jogo_odds in odds_lista:
            if mesmo_time(nome_casa, jogo_odds["home_team"]) and mesmo_time(nome_fora, jogo_odds["away_team"]):
                odds_encontradas = melhor_odd_por_mercado(jogo_odds)
                break

        if not odds_encontradas:
            continue

        gm_casa, gs_casa = buscar_estatisticas_time(nome_casa, tabela)
        gm_fora, gs_fora = buscar_estatisticas_time(nome_fora, tabela)

        jogos.append({
            "time_casa": {"nome": nome_casa, "gols_marcados_media": gm_casa, "gols_sofridos_media": gs_casa},
            "time_fora": {"nome": nome_fora, "gols_marcados_media": gm_fora, "gols_sofridos_media": gs_fora},
            "odds": odds_encontradas,
        })

    return jogos


if __name__ == "__main__":
    debug_info = {}
    jogos = montar_jogos_reais(debug_info=debug_info)
    print("DEBUG:", debug_info)
    print(f"\n{len(jogos)} jogo(s) casado(s).\n")
