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


def _extrair_medias_da_entrada(entrada):
    """De uma entrada da tabela (standings), pega jogos/gols pró/gols contra.
    Busca de forma tolerante (ignora maiúscula/minúscula e aceita tanto
    'name' quanto 'type'), porque a ESPN não é consistente na grafia
    desses campos entre ligas."""
    def _valor_por_apelidos(stats_lista, apelidos):
        for s in stats_lista:
            candidatos = [str(s.get("name") or "").lower(), str(s.get("type") or "").lower()]
            if any(a in candidatos for a in apelidos):
                valor = s.get("value")
                if valor is not None:
                    return float(valor)
        return None

    stats_lista = entrada.get("stats", [])
    jogos = _valor_por_apelidos(stats_lista, ["gamesplayed"])
    gols_pro = _valor_por_apelidos(stats_lista, ["pointsfor"])
    gols_contra = _valor_por_apelidos(stats_lista, ["pointsagainst"])

    # só usa os números se TODOS vieram e jogos é um valor plausível (>0);
    # senão cai no chute conservador, em vez de gerar média absurda
    if jogos and jogos > 0 and gols_pro is not None and gols_contra is not None:
        return {
            "jogos": jogos,
            "gols_marcados_media": gols_pro / jogos,
            "gols_sofridos_media": gols_contra / jogos,
        }
    return {"jogos": 0, "gols_marcados_media": 1.3, "gols_sofridos_media": 1.3}


def buscar_medias_gols(liga_codigo, debug_info=None):
    """Tabela de classificação -> média de gols marcados/sofridos por time."""
    resp = requests.get(f"{ESPN_STANDINGS_BASE}/{liga_codigo}/standings", timeout=20)
    resp.raise_for_status()
    body = resp.json()

    medias = {}
    com_dados_reais = 0
    no_fallback = 0
    grupos = body.get("children") or [body]
    for grupo in grupos:
        standings = grupo.get("standings", {})
        for entrada in standings.get("entries", []):
            nome_time = entrada.get("team", {}).get("displayName")
            if not nome_time:
                continue
            m = _extrair_medias_da_entrada(entrada)
            medias[_normalizar(nome_time)] = m
            if m["jogos"] > 0:
                com_dados_reais += 1
            else:
                no_fallback += 1

    if debug_info is not None:
        debug_info.setdefault("times_com_dados_reais_por_liga", {})[liga_codigo] = com_dados_reais
        debug_info.setdefault("times_no_fallback_por_liga", {})[liga_codigo] = no_fallback

    return medias


def _stats_do_time(nome_time, medias):
    chave = _normalizar(nome_time)
    if chave in medias:
        return medias[chave]
    for k, v in medias.items():
        if _mesmo_time(k, chave):
            return v
    return {"gols_marcados_media": 1.3, "gols_sofridos_media": 1.3, "jogos": 0}


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


def _media_escanteios_cartoes_time(liga_codigo, time_id, ultimos_n=4):
    """Busca os últimos jogos do time (via schedule) e calcula médias de
    escanteios/cartões. Cacheado por liga+time pra não repetir chamadas."""
    chave_cache = (liga_codigo, time_id)
    if chave_cache in _CACHE_STATS_EXTRAS:
        return _CACHE_STATS_EXTRAS[chave_cache]

    padrao = {"escanteios_media": 9.5, "cartoes_media": 3.5}  # chute conservador se não achar nada
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

    escanteios_lista, cartoes_lista = [], []
    for ev in finalizados:
        extras = _extrair_escanteios_cartoes(ev)
        dados_time = extras.get(str(time_id)) or extras.get(time_id)
        if dados_time:
            escanteios_lista.append(dados_time["escanteios"])
            cartoes_lista.append(dados_time["cartoes"])

    resultado = {
        "escanteios_media": (sum(escanteios_lista) / len(escanteios_lista)) if escanteios_lista else padrao["escanteios_media"],
        "cartoes_media": (sum(cartoes_lista) / len(cartoes_lista)) if cartoes_lista else padrao["cartoes_media"],
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
            medias_gols = buscar_medias_gols(liga_codigo, debug_info=debug_info)
            jogos = buscar_jogos_do_dia(liga_codigo, data_str)
        except Exception as exc:
            if debug_info is not None:
                debug_info.setdefault("erros_por_liga", {})[liga_nome] = str(exc)
            continue

        for jogo in jogos:
            stats_casa = _stats_do_time(jogo["nome_casa"], medias_gols)
            stats_fora = _stats_do_time(jogo["nome_fora"], medias_gols)
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

            if incluir_escanteios_cartoes:
                try:
                    extra_casa = _media_escanteios_cartoes_time(liga_codigo, jogo["id_casa"])
                    extra_fora = _media_escanteios_cartoes_time(liga_codigo, jogo["id_fora"])

                    media_escanteios = extra_casa["escanteios_media"] + extra_fora["escanteios_media"]
                    media_cartoes = extra_casa["cartoes_media"] + extra_fora["cartoes_media"]

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
