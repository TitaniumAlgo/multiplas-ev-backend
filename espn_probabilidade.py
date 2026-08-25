"""
Modo de probabilidade completo usando a ESPN (endpoint não-oficial, sem
chave) - cobre Brasileirão Série A e B, com 4 tipos de mercado:
vencedor/empate, gols (mais/menos de 2,5), escanteios e cartões.

Sem odds reais dessa fonte (só estatística pública), então calculamos a
probabilidade via Poisson a partir da média histórica de cada time -
igual ao modo Série B, mas agora cobrindo mais mercados e as duas séries.
"""

import unicodedata
from datetime import date

import requests

from multiplas_ev import expected_goals, calcular_probabilidades

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports/soccer"
LIGAS = {"bra.1": "Brasileirão Série A", "bra.2": "Brasileirão Série B"}

_CACHE_STATS_EXTRAS = {}  # (liga, team_id) -> {"escanteios": x, "cartoes": y} (por jogo)


def disponivel():
    return True  # sem chave, sempre "disponível" (pode falhar em runtime se a ESPN mudar algo)


def _normalizar(nome):
    nome = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return nome.lower().strip()


def _mesmo_time(a, b):
    a, b = _normalizar(a), _normalizar(b)
    return a == b or (a and a in b) or (b and b in a)


def _limitar_prob(p):
    """Nunca deixa passar como 'certeza absoluta' - sempre existe incerteza real."""
    return max(0.01, min(0.97, p))


def buscar_jogos_do_dia(liga_codigo, data_str):
    params = {"dates": data_str.replace("-", "")} if data_str else {}
    resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/scoreboard", params=params, timeout=20)
    resp.raise_for_status()
    eventos = resp.json().get("events", [])

    jogos = []
    for ev in eventos:
        comp = (ev.get("competitions") or [{}])[0]
        competidores = comp.get("competitors", [])
        casa = next((c for c in competidores if c.get("homeAway") == "home"), None)
        fora = next((c for c in competidores if c.get("homeAway") == "away"), None)
        if not casa or not fora:
            continue
        jogos.append({
            "nome_casa": casa["team"]["displayName"],
            "id_casa": casa["team"]["id"],
            "nome_fora": fora["team"]["displayName"],
            "id_fora": fora["team"]["id"],
        })
    return jogos


def _extrair_escanteios_cartoes(fixture_evento):
    """De um evento (scoreboard/summary), extrai escanteios e cartões de cada time."""
    comp = (fixture_evento.get("competitions") or [{}])[0]
    resultado = {}
    for competidor in comp.get("competitors", []):
        time_id = competidor.get("team", {}).get("id")
        stats = {s.get("name"): s.get("displayValue") for s in competidor.get("statistics", [])}
        try:
            corners = float(stats.get("wonCorners", 0) or 0)
        except (TypeError, ValueError):
            corners = 0
        resultado[time_id] = {"escanteios": corners}

    cartoes_por_time = {}
    for detalhe in comp.get("details", []):
        tipo_texto = (detalhe.get("type", {}) or {}).get("text", "")
        if "card" not in tipo_texto.lower():
            continue
        time_id = (detalhe.get("team") or {}).get("id")
        if time_id:
            cartoes_por_time[time_id] = cartoes_por_time.get(time_id, 0) + 1

    for time_id in resultado:
        resultado[time_id]["cartoes"] = cartoes_por_time.get(time_id, 0)

    return resultado


def _extrair_gols_1o_tempo(competidor):
    """Pega o placar do intervalo (1º período) de um competidor, se disponível."""
    linescores = competidor.get("linescores") or []
    if not linescores:
        return None
    primeiro = linescores[0]
    valor = primeiro.get("value") if isinstance(primeiro, dict) else None
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _stats_recentes_time(liga_codigo, time_id, ultimos_n=10):
    """Busca os últimos N jogos do time e calcula, tudo numa única
    chamada: média de gols (jogo inteiro e 1º tempo), escanteios e
    cartões. Usa forma recente em vez da temporada inteira - reflete
    melhor o momento atual do time. Cacheado por liga+time."""
    chave_cache = (liga_codigo, time_id)
    if chave_cache in _CACHE_STATS_EXTRAS:
        return _CACHE_STATS_EXTRAS[chave_cache]

    padrao = {
        "gols_marcados_media": 1.3, "gols_sofridos_media": 1.3,
        "gols_1t_media": 0.55,
        "escanteios_media": 5.0, "cartoes_media": 1.8,
        "jogos_analisados": 0,
    }
    try:
        resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/teams/{time_id}/schedule", timeout=20)
        resp.raise_for_status()
        eventos = resp.json().get("events", [])
    except Exception:
        _CACHE_STATS_EXTRAS[chave_cache] = padrao
        return padrao

    finalizados = [
        e for e in eventos
        if (e.get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("completed")
    ][-ultimos_n:]

    gols_marcados, gols_sofridos, gols_1t = [], [], []
    escanteios_lista, cartoes_lista = [], []

    for ev in finalizados:
        comp = (ev.get("competitions") or [{}])[0]
        competidores = comp.get("competitors", [])
        proprio = next((c for c in competidores if str(c.get("team", {}).get("id")) == str(time_id)), None)
        adversario = next((c for c in competidores if str(c.get("team", {}).get("id")) != str(time_id)), None)
        if not proprio or not adversario:
            continue

        try:
            gols_marcados.append(float(proprio.get("score", 0) or 0))
            gols_sofridos.append(float(adversario.get("score", 0) or 0))
        except (TypeError, ValueError):
            continue

        gols_1t_proprio = _extrair_gols_1o_tempo(proprio)
        if gols_1t_proprio is not None:
            gols_1t.append(gols_1t_proprio)

        extras = _extrair_escanteios_cartoes(ev)
        dados_time = extras.get(str(time_id)) or extras.get(time_id)
        if dados_time:
            escanteios_lista.append(dados_time["escanteios"])
            cartoes_lista.append(dados_time["cartoes"])

    def _media(lista, padrao_valor):
        return (sum(lista) / len(lista)) if lista else padrao_valor

    resultado = {
        "gols_marcados_media": _media(gols_marcados, padrao["gols_marcados_media"]),
        "gols_sofridos_media": _media(gols_sofridos, padrao["gols_sofridos_media"]),
        "gols_1t_media": _media(gols_1t, padrao["gols_1t_media"]),
        "escanteios_media": _media(escanteios_lista, padrao["escanteios_media"]),
        "cartoes_media": _media(cartoes_lista, padrao["cartoes_media"]),
        "jogos_analisados": len(gols_marcados),
    }
    _CACHE_STATS_EXTRAS[chave_cache] = resultado
    return resultado


def _poisson_over_under(media_total, linhas):
    """Modelo simples pra escanteios/cartões: usa distribuição de Poisson
    em torno da média combinada dos dois times, pra cada linha pedida."""
    import math

    def prob_poisson_le(k, lam):
        return sum((lam ** i) * math.exp(-lam) / math.factorial(i) for i in range(k + 1))

    resultado = {}
    for linha in linhas:
        k = int(linha)  # ex: linha 9.5 -> P(X <= 9)
        p_under = prob_poisson_le(k, media_total)
        resultado[f"mais de {linha}"] = round(1 - p_under, 4)
        resultado[f"menos de {linha}"] = round(p_under, 4)
    return resultado


def gerar_selecoes(data_str=None, prob_minima=0.5, incluir_escanteios_cartoes=True, debug_info=None):
    if data_str is None:
        data_str = date.today().isoformat()

    selecoes = []
    for liga_codigo, liga_nome in LIGAS.items():
        try:
            jogos = buscar_jogos_do_dia(liga_codigo, data_str)
        except Exception as exc:
            if debug_info is not None:
                debug_info.setdefault("erros_por_liga", {})[liga_nome] = str(exc)
            continue

        for jogo in jogos:
            try:
                stats_casa = _stats_recentes_time(liga_codigo, jogo["id_casa"])
                stats_fora = _stats_recentes_time(liga_codigo, jogo["id_fora"])
            except Exception as exc:
                if debug_info is not None:
                    debug_info.setdefault("erros_stats", []).append(f"{jogo['nome_casa']} x {jogo['nome_fora']}: {exc}")
                continue

            if debug_info is not None:
                debug_info.setdefault("jogos_analisados_por_time", {})[jogo["nome_casa"]] = stats_casa["jogos_analisados"]
                debug_info.setdefault("jogos_analisados_por_time", {})[jogo["nome_fora"]] = stats_fora["jogos_analisados"]

            gc, gf = expected_goals(stats_casa, stats_fora)
            probs_gols = calcular_probabilidades(gc, gf)
            nome_jogo = f"{jogo['nome_casa']} x {jogo['nome_fora']}"

            mapa = {
                "casa": f"{jogo['nome_casa']} vencedor",
                "fora": f"{jogo['nome_fora']} vencedor",
                "empate": "empate",
                "over_2.5": "mais de 2,5 gols",
                "under_2.5": "menos de 2,5 gols",
            }
            for chave, nome_mercado in mapa.items():
                prob = probs_gols.get(chave)
                if prob is not None:
                    prob = _limitar_prob(prob)
                if prob is not None and prob >= prob_minima:
                    selecoes.append({"jogo": nome_jogo, "mercado": nome_mercado, "prob_real": round(prob, 4), "liga": liga_nome})

            # gols no 1º tempo (linha única: mais/menos de 0,5)
            try:
                media_1t = stats_casa["gols_1t_media"] + stats_fora["gols_1t_media"]
                probs_1t = _poisson_over_under(media_1t, [0.5])
                for nome_mercado, prob in probs_1t.items():
                    prob = _limitar_prob(prob)
                    rotulo = f"{nome_mercado} gols no 1º tempo"
                    if prob >= prob_minima:
                        selecoes.append({"jogo": nome_jogo, "mercado": rotulo, "prob_real": round(prob, 4), "liga": liga_nome})
            except Exception:
                pass

            if incluir_escanteios_cartoes:
                try:
                    media_escanteios = stats_casa["escanteios_media"] + stats_fora["escanteios_media"]
                    media_cartoes = stats_casa["cartoes_media"] + stats_fora["cartoes_media"]

                    probs_escanteios = _poisson_over_under(media_escanteios, [9.5])
                    probs_cartoes = _poisson_over_under(media_cartoes, [3.5])

                    for nome_mercado, prob in {**probs_escanteios, **probs_cartoes}.items():
                        prob = _limitar_prob(prob)
                        rotulo = f"{nome_mercado} escanteios" if nome_mercado in probs_escanteios else f"{nome_mercado} cartões"
                        if prob >= prob_minima:
                            selecoes.append({"jogo": nome_jogo, "mercado": rotulo, "prob_real": round(prob, 4), "liga": liga_nome})
                except Exception as exc:
                    if debug_info is not None:
                        debug_info.setdefault("erros_escanteios_cartoes", []).append(f"{nome_jogo}: {exc}")

    return sorted(selecoes, key=lambda s: s["prob_real"], reverse=True)


def montar_combinacoes_por_faixa(selecoes, faixas=(0.5, 0.6, 0.7, 0.8, 0.9), min_selecoes=2, max_selecoes=4):
    """Agrupa combinações de pelo menos 2 seleções por faixa de probabilidade.

    Cada combinação aparece em UMA ÚNICA faixa - a que corresponde à sua
    perna mais fraca (a que "segura" a confiança da múltipla inteira).
    Assim, 50%/60%/70%/80%/90% mostram conjuntos DIFERENTES de múltiplas,
    em vez de repetir as mesmas em todas as faixas superiores."""
    from itertools import combinations

    elegveis_brutos = [s for s in selecoes if s["prob_real"] >= faixas[0]]

    # no máximo 1 seleção por jogo (a de maior probabilidade) - evita gerar
    # várias múltiplas quase-idênticas só trocando o mercado de um mesmo jogo
    melhor_por_jogo = {}
    for s in elegveis_brutos:
        atual = melhor_por_jogo.get(s["jogo"])
        if atual is None or s["prob_real"] > atual["prob_real"]:
            melhor_por_jogo[s["jogo"]] = s
    elegveis = list(melhor_por_jogo.values())

    combinacoes_geradas = []
    for n in range(min_selecoes, min(max_selecoes, len(elegveis)) + 1):
        for combo in combinations(elegveis, n):
            jogos = [s["jogo"] for s in combo]
            if len(set(jogos)) != n:
                continue
            prob_final = 1.0
            perna_mais_fraca = 1.0
            for s in combo:
                prob_final *= s["prob_real"]
                perna_mais_fraca = min(perna_mais_fraca, s["prob_real"])
            combinacoes_geradas.append({
                "selecoes": list(combo),
                "prob_final": round(prob_final, 4),
                "perna_mais_fraca": perna_mais_fraca,
                "odd_estimada": round(1 / max(prob_final, 0.03), 2),
            })

    def _faixa_da_combinacao(perna_mais_fraca):
        # acha a maior faixa que a perna mais fraca ainda atinge
        faixa_certa = faixas[0]
        for f in faixas:
            if perna_mais_fraca >= f:
                faixa_certa = f
        return faixa_certa

    ODD_MINIMA_ACEITAVEL = 1.4  # abaixo disso, o retorno não compensa o risco

    resultado = {f"{int(f*100)}%": [] for f in faixas}
    for combo in combinacoes_geradas:
        if combo["odd_estimada"] < ODD_MINIMA_ACEITAVEL:
            continue  # descarta - odd baixa demais, não compensa
        faixa = _faixa_da_combinacao(combo["perna_mais_fraca"])
        resultado[f"{int(faixa*100)}%"].append(combo)

    for chave in resultado:
        resultado[chave].sort(key=lambda c: c["prob_final"], reverse=True)
        resultado[chave] = resultado[chave][:10]  # top 10 por faixa

    return resultado
