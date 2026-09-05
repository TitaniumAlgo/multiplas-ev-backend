"""
Modo de probabilidade completo usando a ESPN (endpoint não-oficial, sem
chave) - cobre Brasileirão Série A e B, com 4 tipos de mercado:
vencedor/empate, gols (mais/menos de 2,5), escanteios e cartões.

Sem odds reais dessa fonte (só estatística pública), então calculamos a
probabilidade via Poisson a partir da média histórica de cada time -
igual ao modo Série B, mas agora cobrindo mais mercados e as duas séries.
"""

import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

from multiplas_ev import expected_goals, calcular_probabilidades

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports/soccer"
LIGAS = {
    "bra.1": "Brasileirão Série A",
    "bra.2": "Brasileirão Série B",
    "bra.copa_do_brazil": "Copa do Brasil",
}

_CACHE_STATS_EXTRAS = {}  # (liga, team_id) -> stats do time (limitado em tamanho, ver _guardar_no_cache)
_CACHE_TAMANHO_MAXIMO = 60  # evita crescer pra sempre e vazar memória ao longo do tempo


def _guardar_no_cache(chave, valor):
    if len(_CACHE_STATS_EXTRAS) >= _CACHE_TAMANHO_MAXIMO:
        # remove uma entrada qualquer (a mais antiga por ordem de inserção)
        mais_antiga = next(iter(_CACHE_STATS_EXTRAS))
        del _CACHE_STATS_EXTRAS[mais_antiga]
    _CACHE_STATS_EXTRAS[chave] = valor


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


def _jogo_ainda_nao_comecou(evento):
    """Só interessa pra gerar seleção um jogo que ainda vai acontecer -
    um jogo já finalizado ou em andamento não é mais uma aposta válida."""
    comp = (evento.get("competitions") or [{}])[0]
    status = comp.get("status") or evento.get("status") or {}
    tipo = status.get("type", {})
    estado = str(tipo.get("state", "")).lower()
    if estado:
        return estado == "pre"
    # sem campo 'state' reconhecível - usa o mesmo critério de "finalizado"
    # que já temos, e assume que não-finalizado = ainda não começou
    return not _jogo_finalizado(evento)


def buscar_jogos_do_dia(liga_codigo, data_str):
    params = {"dates": data_str.replace("-", "")} if data_str else {}
    resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/scoreboard", params=params, timeout=20)
    resp.raise_for_status()
    eventos = resp.json().get("events", [])

    jogos = []
    for ev in eventos:
        if not _jogo_ainda_nao_comecou(ev):
            continue
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
    """De um evento (scoreboard/summary), extrai escanteios, cartões e
    faltas cometidas de cada time. Se a estatística não estiver disponível
    nesse evento (comum no endpoint de 'schedule', mais leve que o de
    resultado completo), devolve None em vez de 0 - um 0 falso puxaria a
    média pra baixo e faria qualquer linha parecer 'quase certa' por engano."""
    comp = (fixture_evento.get("competitions") or [{}])[0]
    resultado = {}
    for competidor in comp.get("competitors", []):
        time_id = competidor.get("team", {}).get("id")
        stats_lista = competidor.get("statistics", [])
        stats = {s.get("name"): s.get("displayValue") for s in stats_lista}

        def _pega(nome_stat):
            if nome_stat in stats:
                try:
                    return float(stats[nome_stat])
                except (TypeError, ValueError):
                    return None
            return None  # estatística não veio nesse evento - não é zero real

        resultado[time_id] = {
            "escanteios": _pega("wonCorners"),
            "faltas": _pega("foulsCommitted"),
            "chutes_gol": _pega("shotsOnTarget"),
        }

    tem_detalhes = "details" in comp
    cartoes_por_time = {}
    for detalhe in comp.get("details", []):
        tipo_texto = (detalhe.get("type", {}) or {}).get("text", "")
        if "card" not in tipo_texto.lower():
            continue
        time_id = (detalhe.get("team") or {}).get("id")
        if time_id:
            cartoes_por_time[time_id] = cartoes_por_time.get(time_id, 0) + 1

    for time_id in resultado:
        resultado[time_id]["cartoes"] = cartoes_por_time.get(time_id, 0) if tem_detalhes else None

    return resultado


def _extrair_gols_1o_tempo_bruto(competidor):
    """Pega o valor bruto do placar do intervalo (1º período), se disponível."""
    linescores = competidor.get("linescores") or []
    if not linescores:
        return None
    return linescores[0]


def _extrair_valor_numerico(campo):
    """O placar/valor pode vir como número direto, string, ou objeto tipo
    {'value': 2, 'displayValue': '2'} - tenta reconhecer qualquer um."""
    if campo is None:
        return None
    if isinstance(campo, dict):
        campo = campo.get("value", campo.get("displayValue"))
    try:
        return float(campo)
    except (TypeError, ValueError):
        return None


def _jogo_finalizado(evento_ou_comp):
    """Confere se o jogo terminou, tentando os formatos conhecidos de
    status que a ESPN usa (pode variar entre endpoints)."""
    comp = (evento_ou_comp.get("competitions") or [{}])[0] if "competitions" in evento_ou_comp else evento_ou_comp
    status = comp.get("status") or evento_ou_comp.get("status") or {}
    tipo = status.get("type", {})
    if tipo.get("completed") is True:
        return True
    if str(tipo.get("state", "")).lower() == "post":
        return True
    if str(tipo.get("name", "")).upper() in ("STATUS_FULL_TIME", "STATUS_FINAL"):
        return True
    return False


def _stats_recentes_time(liga_codigo, time_id, ultimos_n=10):
    """Busca os últimos N jogos do time e calcula, tudo numa única
    chamada: média de gols (jogo inteiro e 1º tempo), escanteios,
    cartões, faltas cometidas e chutes a gol. Usa forma recente em vez
    da temporada inteira - reflete melhor o momento atual do time.
    Cacheado por liga+time."""
    chave_cache = (liga_codigo, time_id)
    if chave_cache in _CACHE_STATS_EXTRAS:
        return _CACHE_STATS_EXTRAS[chave_cache]

    padrao = {
        "gols_marcados_media": 1.3, "gols_sofridos_media": 1.3,
        "gols_1t_media": 0.55,
        "escanteios_media": 5.0, "cartoes_media": 1.8,
        "faltas_media": 11.0, "chutes_gol_media": 4.5,
        "jogos_analisados": 0,
    }
    try:
        resp = requests.get(f"{ESPN_BASE}/{liga_codigo}/teams/{time_id}/schedule", timeout=20)
        resp.raise_for_status()
        corpo = resp.json()
        eventos = corpo.get("events", [])
        finalizados = [e for e in eventos if _jogo_finalizado(e)][-ultimos_n:]
        # descarta o resto da resposta bruta (jogos futuros, dados extras)
        # assim que já temos só os finalizados que interessam - o resto
        # não precisa continuar ocupando memória
        del corpo, eventos, resp
    except Exception:
        _guardar_no_cache(chave_cache, padrao)
        return padrao

    gols_marcados, gols_sofridos, gols_1t = [], [], []
    escanteios_lista, cartoes_lista, faltas_lista, chutes_gol_lista = [], [], [], []

    for ev in finalizados:
        comp = (ev.get("competitions") or [{}])[0]
        competidores = comp.get("competitors", [])
        proprio = next((c for c in competidores if str(c.get("team", {}).get("id")) == str(time_id)), None)
        adversario = next((c for c in competidores if str(c.get("team", {}).get("id")) != str(time_id)), None)
        if not proprio or not adversario:
            continue

        gp = _extrair_valor_numerico(proprio.get("score"))
        ga = _extrair_valor_numerico(adversario.get("score"))
        if gp is None or ga is None:
            continue
        gols_marcados.append(gp)
        gols_sofridos.append(ga)

        gols_1t_proprio = _extrair_valor_numerico(_extrair_gols_1o_tempo_bruto(proprio))
        if gols_1t_proprio is not None:
            gols_1t.append(gols_1t_proprio)

        extras = _extrair_escanteios_cartoes(ev)
        dados_time = extras.get(str(time_id)) or extras.get(time_id)
        if dados_time:
            if dados_time["escanteios"] is not None:
                escanteios_lista.append(dados_time["escanteios"])
            if dados_time["cartoes"] is not None:
                cartoes_lista.append(dados_time["cartoes"])
            if dados_time.get("faltas") is not None:
                faltas_lista.append(dados_time["faltas"])
            if dados_time.get("chutes_gol") is not None:
                chutes_gol_lista.append(dados_time["chutes_gol"])

    def _media(lista, padrao_valor):
        return (sum(lista) / len(lista)) if lista else padrao_valor

    resultado = {
        "gols_marcados_media": _media(gols_marcados, padrao["gols_marcados_media"]),
        "gols_sofridos_media": _media(gols_sofridos, padrao["gols_sofridos_media"]),
        "gols_1t_media": _media(gols_1t, padrao["gols_1t_media"]),
        "escanteios_media": _media(escanteios_lista, padrao["escanteios_media"]),
        "cartoes_media": _media(cartoes_lista, padrao["cartoes_media"]),
        "faltas_media": _media(faltas_lista, padrao["faltas_media"]),
        "chutes_gol_media": _media(chutes_gol_lista, padrao["chutes_gol_media"]),
        "jogos_analisados": len(gols_marcados),
    }
    _guardar_no_cache(chave_cache, resultado)
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


def gerar_selecoes(data_str=None, prob_minima=0.5, incluir_escanteios_cartoes=True, incluir_jogadores=False, debug_info=None):
    if data_str is None:
        data_str = date.today().isoformat()

    # 1) busca os jogos do dia em todas as ligas primeiro
    jogos_por_liga = {}
    for liga_codigo, liga_nome in LIGAS.items():
        try:
            jogos_por_liga[liga_codigo] = buscar_jogos_do_dia(liga_codigo, data_str)
        except Exception as exc:
            if debug_info is not None:
                debug_info.setdefault("erros_por_liga", {})[liga_nome] = str(exc)

    # 2) busca a estatística de TODOS os times envolvidos em paralelo,
    # em vez de um de cada vez - com muitas ligas, isso evita demorar
    # minutos numa única busca
    pares_time_liga = set()
    for liga_codigo, jogos in jogos_por_liga.items():
        for jogo in jogos:
            pares_time_liga.add((liga_codigo, jogo["id_casa"]))
            pares_time_liga.add((liga_codigo, jogo["id_fora"]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futuros = {
            executor.submit(_stats_recentes_time, liga_codigo, time_id): (liga_codigo, time_id)
            for liga_codigo, time_id in pares_time_liga
        }
        for futuro in as_completed(futuros):
            futuro.result()  # só espera terminar - já fica salvo no cache interno

    # 3) agora monta as seleções usando o cache já preenchido (rápido, sem rede)
    selecoes = []
    for liga_codigo, liga_nome in LIGAS.items():
        jogos = jogos_por_liga.get(liga_codigo, [])
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
                    media_faltas = stats_casa["faltas_media"] + stats_fora["faltas_media"]
                    media_chutes_gol = stats_casa["chutes_gol_media"] + stats_fora["chutes_gol_media"]

                    probs_escanteios = _poisson_over_under(media_escanteios, [9.5])
                    probs_cartoes = _poisson_over_under(media_cartoes, [3.5])
                    probs_faltas = _poisson_over_under(media_faltas, [21.5])
                    probs_chutes_gol = _poisson_over_under(media_chutes_gol, [8.5])

                    grupos_mercado = [
                        (probs_escanteios, "escanteios"),
                        (probs_cartoes, "cartões"),
                        (probs_faltas, "faltas"),
                        (probs_chutes_gol, "chutes a gol"),
                    ]
                    for probs_grupo, sufixo in grupos_mercado:
                        for nome_mercado, prob in probs_grupo.items():
                            prob = _limitar_prob(prob)
                            rotulo = f"{nome_mercado} {sufixo}"
                            if prob >= prob_minima:
                                selecoes.append({"jogo": nome_jogo, "mercado": rotulo, "prob_real": round(prob, 4), "liga": liga_nome})
                except Exception as exc:
                    if debug_info is not None:
                        debug_info.setdefault("erros_escanteios_cartoes", []).append(f"{nome_jogo}: {exc}")

            if incluir_jogadores:
                try:
                    import espn_jogadores
                    selecoes_jogadores = espn_jogadores.gerar_selecoes_jogadores(
                        liga_codigo, liga_nome, nome_jogo, jogo["id_casa"], jogo["id_fora"], prob_minima=prob_minima
                    )
                    selecoes.extend(selecoes_jogadores)
                except Exception as exc:
                    if debug_info is not None:
                        debug_info.setdefault("erros_jogadores", []).append(f"{nome_jogo}: {exc}")

    return sorted(selecoes, key=lambda s: s["prob_real"], reverse=True)


def montar_multipla_grande(selecoes, max_pernas=10):
    """Monta UMA múltipla "grande" (não é uma combinação por força bruta,
    que explodiria com muitas seleções - isso é O(n log n), seguro).

    Pega a melhor seleção de CADA jogo (sem repetir jogo), ordena da maior
    pra menor probabilidade, e monta progressivamente: com 2 pernas
    (as duas melhores), com 3 (as três melhores), ... até min(10, jogos
    disponíveis). Assim você vê como a probabilidade e a odd mudam
    conforme aumenta o tamanho, e escolhe o tamanho que preferir."""
    melhor_por_jogo = {}
    for s in selecoes:
        atual = melhor_por_jogo.get(s["jogo"])
        if atual is None or s["prob_real"] > atual["prob_real"]:
            melhor_por_jogo[s["jogo"]] = s

    pool_ordenado = sorted(melhor_por_jogo.values(), key=lambda s: s["prob_real"], reverse=True)

    progressao = []
    for tamanho in range(2, min(max_pernas, len(pool_ordenado)) + 1):
        pernas = pool_ordenado[:tamanho]
        prob_final = 1.0
        for s in pernas:
            prob_final *= s["prob_real"]
        progressao.append({
            "tamanho": tamanho,
            "selecoes": pernas,
            "prob_final": round(prob_final, 4),
            "odd_estimada": round(1 / max(prob_final, 0.001), 2),
        })

    return progressao


def _categoria_mercado(mercado):
    """Classifica o mercado em categoria, pra medir variedade dentro da múltipla."""
    if mercado.endswith(" vencedor") or mercado == "empate":
        return "vencedor"
    if "1º tempo" in mercado:
        return "gols_1t"
    if "escanteios" in mercado:
        return "escanteios"
    if "cartões" in mercado:
        return "cartoes"
    if "faltas" in mercado:
        return "faltas"
    if "chutes a gol" in mercado:
        return "chutes"
    if "gols" in mercado:
        return "gols"
    return "outro"


def montar_combinacoes_por_faixa(selecoes, faixas=(0.5, 0.6, 0.7, 0.8, 0.9), min_selecoes=2, max_selecoes=3):
    """Agrupa combinações de pelo menos 2 seleções por faixa de probabilidade.

    Gera combinações usando TODAS as seleções qualificadas (sem descartar
    mercados alternativos do mesmo jogo de antemão), e depois, pra cada
    CONJUNTO de jogos envolvido, mantém só a MELHOR combinação - priorizando
    (1) bater o piso de odd, (2) ter mais VARIEDADE de tipo de mercado
    (vencedor/gols/1º tempo/escanteios/cartões/faltas/chutes - não deixa
    gols sempre "roubar a vaga" de mercados novos), e só por último a
    maior probabilidade entre empates de variedade.

    Cada combinação (já filtrada/única por conjunto de jogos) aparece em
    UMA ÚNICA faixa - a que corresponde à sua perna mais fraca."""
    from itertools import combinations

    ODD_MINIMA_ACEITAVEL = 1.3  # abaixo disso, o retorno não compensa o risco

    elegveis = [s for s in selecoes if s["prob_real"] >= faixas[0]]

    combinacoes_geradas = []
    for n in range(min_selecoes, min(max_selecoes, len(elegveis)) + 1):
        for combo in combinations(elegveis, n):
            jogos = [s["jogo"] for s in combo]
            if len(set(jogos)) != n:
                continue  # não repete o mesmo jogo na mesma múltipla
            prob_final = 1.0
            perna_mais_fraca = 1.0
            for s in combo:
                prob_final *= s["prob_real"]
                perna_mais_fraca = min(perna_mais_fraca, s["prob_real"])
            categorias = {_categoria_mercado(s["mercado"]) for s in combo}
            categorias_estatisticas = {"escanteios", "cartoes", "faltas", "chutes"}
            combinacoes_geradas.append({
                "selecoes": list(combo),
                "prob_final": round(prob_final, 4),
                "perna_mais_fraca": perna_mais_fraca,
                "odd_estimada": round(1 / max(prob_final, 0.03), 2),
                "conjunto_jogos": frozenset(jogos),
                "n_categorias": len(categorias),
                "n_estatisticas": len(categorias & categorias_estatisticas),
            })

    # pra cada conjunto de jogos, mantém só a melhor combinação: prioriza
    # (1) bater o piso de odd, (2) ter mais variedade de tipo de mercado,
    # (3) maior probabilidade como desempate final
    melhor_por_conjunto = {}
    for c in combinacoes_geradas:
        chave = c["conjunto_jogos"]
        atual = melhor_por_conjunto.get(chave)
        if atual is None:
            melhor_por_conjunto[chave] = c
            continue
        c_compensa = c["odd_estimada"] >= ODD_MINIMA_ACEITAVEL
        atual_compensa = atual["odd_estimada"] >= ODD_MINIMA_ACEITAVEL
        if c_compensa != atual_compensa:
            if c_compensa:
                melhor_por_conjunto[chave] = c
            continue
        if c["n_categorias"] != atual["n_categorias"]:
            if c["n_categorias"] > atual["n_categorias"]:
                melhor_por_conjunto[chave] = c
            continue
        if c["prob_final"] > atual["prob_final"]:
            melhor_por_conjunto[chave] = c

    # ADICIONALMENTE: pra cada conjunto de jogos, também guarda a melhor
    # opção que usa mais mercados "estatísticos" (escanteios/cartões/
    # faltas/chutes) - assim, quando só existe 1 conjunto de jogos
    # possível (poucos jogos no dia), você ainda vê essa opção junto com
    # a mais segura, em vez dela nunca aparecer
    melhor_estatisticas_por_conjunto = {}
    for c in combinacoes_geradas:
        if c["odd_estimada"] < ODD_MINIMA_ACEITAVEL or c["n_estatisticas"] == 0:
            continue
        chave = c["conjunto_jogos"]
        atual = melhor_estatisticas_por_conjunto.get(chave)
        if atual is None or c["n_estatisticas"] > atual["n_estatisticas"] or (
            c["n_estatisticas"] == atual["n_estatisticas"] and c["prob_final"] > atual["prob_final"]
        ):
            melhor_estatisticas_por_conjunto[chave] = c

    combinacoes_finais = list(melhor_por_conjunto.values())
    for chave, c_estat in melhor_estatisticas_por_conjunto.items():
        c_principal = melhor_por_conjunto.get(chave)
        # só adiciona como opção extra se for realmente diferente da principal
        if c_principal is None or c_estat["selecoes"] != c_principal["selecoes"]:
            combinacoes_finais.append(c_estat)

    combinacoes_finais = [c for c in combinacoes_finais if c["odd_estimada"] >= ODD_MINIMA_ACEITAVEL]

    # remove duplicatas "econômicas": combos com a mesma odd final são a
    # mesma oportunidade na prática (ex: trocar só QUAL jogo fornece o
    # escanteio de "menos de 9,5", que dá ~97% em quase todo jogo, gera
    # dezenas de combos tecnicamente diferentes mas sem variedade real)
    vistos_por_odd = {}
    for c in sorted(combinacoes_finais, key=lambda x: (x["n_categorias"], x["prob_final"]), reverse=True):
        chave_odd = round(c["odd_estimada"], 2)
        if chave_odd not in vistos_por_odd:
            vistos_por_odd[chave_odd] = c
    combinacoes_finais = list(vistos_por_odd.values())

    def _faixa_da_combinacao(perna_mais_fraca):
        faixa_certa = faixas[0]
        for f in faixas:
            if perna_mais_fraca >= f:
                faixa_certa = f
        return faixa_certa

    resultado = {f"{int(f*100)}%": [] for f in faixas}
    for combo in combinacoes_finais:
        faixa = _faixa_da_combinacao(combo["perna_mais_fraca"])
        combo_limpo = {k: v for k, v in combo.items() if k not in ("conjunto_jogos", "n_categorias", "n_estatisticas")}
        combo_limpo["_faixa"] = f"{int(faixa*100)}%"
        resultado.setdefault(combo_limpo["_faixa"], [])
        resultado[combo_limpo["_faixa"]].append(combo_limpo)

    # exclusividade GLOBAL: uma seleção (jogo + mercado específico) que já
    # apareceu numa múltipla mostrada não pode aparecer em NENHUMA outra,
    # nem na mesma faixa nem em faixa diferente - cada combinação candidata
    # de todas as faixas entra numa fila única, ordenada por variedade de
    # mercado e depois probabilidade, e só é aceita se NENHUMA das pernas
    # dela já foi usada por uma múltipla aceita antes
    todas_candidatas = []
    for combos_da_faixa in resultado.values():
        todas_candidatas.extend(combos_da_faixa)
    todas_candidatas.sort(
        key=lambda c: (len({_categoria_mercado(s["mercado"]) for s in c["selecoes"]}), c["prob_final"]),
        reverse=True,
    )

    pernas_usadas = set()  # (jogo, mercado) já usados em alguma múltipla aceita
    aceitas_por_faixa = {f"{int(f*100)}%": [] for f in faixas}
    LIMITE_POR_FAIXA = 8
    for combo in todas_candidatas:
        pernas_do_combo = {(s["jogo"], s["mercado"]) for s in combo["selecoes"]}
        if pernas_do_combo & pernas_usadas:
            continue  # alguma perna daqui já está em outra múltipla aceita
        faixa = combo["_faixa"]
        if len(aceitas_por_faixa[faixa]) >= LIMITE_POR_FAIXA:
            continue
        combo_final = {k: v for k, v in combo.items() if k != "_faixa"}
        aceitas_por_faixa[faixa].append(combo_final)
        pernas_usadas |= pernas_do_combo

    return aceitas_por_faixa
