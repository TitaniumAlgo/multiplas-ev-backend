"""
Servidor backend do app de múltiplas.

Protege as chaves de API (elas ficam só aqui no servidor, nunca no
celular) e expõe um endpoint HTTP simples que o app Android (web) chama
pra pegar as múltiplas sugeridas do dia.

COMO RODAR LOCALMENTE (teste):
    pip install flask flask-cors requests
    export API_FOOTBALL_KEY="sua_chave"
    export ODDS_API_KEY="sua_chave"
    python3 server.py
    -> sobe em http://localhost:8000

COMO PUBLICAR (pra o celular acessar de fora de casa):
Veja o arquivo DEPLOY.md com o passo a passo no Render (grátis).
"""

import os
from datetime import date, timedelta

from flask import Flask, jsonify, request
from flask_cors import CORS

from multiplas_ev import montar_multiplas
from buscar_jogos_reais import montar_jogos_reais
import historico
import resultados
import serie_b_probabilidade
import historico_prob
import resultados_prob
import sportmonks_diag

app = Flask(__name__)
CORS(app)  # permite o app web (rodando no celular) chamar este servidor
historico.iniciar_banco()
historico_prob.iniciar_banco()


@app.route("/api/multiplas")
def api_multiplas():
    """
    Parâmetros opcionais na URL:
      ?data=2026-08-16   (padrão: hoje)
      ?ev_minimo=0.03    (padrão: 3%)
      ?odd_max=15        (teto da odd final da múltipla)
      ?min_selecoes=2
      ?max_selecoes=4
    """
    data_str = request.args.get("data", date.today().isoformat())
    ev_minimo = float(request.args.get("ev_minimo", 0.04))
    prob_minima = float(request.args.get("prob_minima", 0.5))
    tipo_mercado = request.args.get("tipo_mercado", "todos")
    odd_max = float(request.args.get("odd_max", 15.0))
    min_sel = int(request.args.get("min_selecoes", 2))
    max_sel = int(request.args.get("max_selecoes", 3))
    forcar_nova_busca = request.args.get("forcar") == "1"

    # se já tem múltiplas salvas pra essa data, devolve elas (travadas) em vez
    # de recalcular - assim a seleção não muda depois de gerada, só quando o
    # jogo terminar e você marcar/for marcado o resultado
    if not forcar_nova_busca:
        ja_salvas = historico.listar_por_data(data_str, tipo_mercado=tipo_mercado)
        if ja_salvas:
            def _prob_combinada(m):
                p = 1.0
                for s in m["selecoes"]:
                    p *= s["prob_real"]
                return round(p, 4)

            return jsonify({
                "data": data_str,
                "de_cache": True,
                "total_jogos_analisados": None,
                "debug": {},
                "selecoes": [],
                "multiplas": [
                    {"odd_final": m["odd_final"], "ev_final": m["ev_final"], "prob_final": _prob_combinada(m),
                     "selecoes": m["selecoes"], "id": m["id"], "resultado": m["resultado"]}
                    for m in ja_salvas
                ],
            })

    debug_info = {}
    try:
        selecoes = montar_jogos_reais(data_str, debug_info=debug_info, ev_minimo=ev_minimo, prob_minima=prob_minima, tipo_mercado=tipo_mercado)
    except Exception as exc:  # erro de rede/API -> devolve erro legível pro app
        return jsonify({"erro": str(exc), "debug": debug_info}), 502

    multiplas = montar_multiplas(
        selecoes, min_selecoes=min_sel, max_selecoes=max_sel, odd_final_max=odd_max
    )

    multiplas_formatadas = [
        {
            "odd_final": m["odd_final"],
            "ev_final": m["ev_final"],
            "prob_final": m["prob_final"],
            "selecoes": [
                {"jogo": s["jogo"], "mercado": s["mercado"], "odd": s["odd"],
                 "prob_real": s["prob_real"], "ev": s["ev"]}
                for s in m["selecoes"]
            ],
        }
        for m in multiplas
    ]

    # salva automaticamente todas as múltiplas geradas no histórico (pendente)
    try:
        historico.salvar_multiplas(data_str, multiplas_formatadas, tipo_mercado=tipo_mercado)
    except Exception:
        pass  # nunca deixa um problema no histórico quebrar a resposta principal

    # formata a resposta em JSON simples pro app consumir
    return jsonify({
        "data": data_str,
        "total_jogos_analisados": debug_info.get("jogos_na_data_pedida", 0),
        "debug": debug_info,
        "selecoes": selecoes,
        "multiplas": multiplas_formatadas,
    })


@app.route("/api/serie-b-probabilidade")
def api_serie_b_probabilidade():
    """
    Modo alternativo (sem odds, só probabilidade real via Poisson) pra usar
    quando os créditos da The Odds API acabarem. Cobre só a Série B, que é
    o campeonato liberado no plano gratuito da API Futebol.
    """
    if not serie_b_probabilidade.disponivel():
        return jsonify({"erro": "API_FUTEBOL_KEY não configurada no servidor."}), 502

    data_str = request.args.get("data", date.today().isoformat())
    prob_minima = float(request.args.get("prob_minima", 0.5))
    tipo_mercado = request.args.get("tipo_mercado", "todos")
    min_sel = int(request.args.get("min_selecoes", 2))
    max_sel = int(request.args.get("max_selecoes", 3))
    forcar_nova_busca = request.args.get("forcar") == "1"

    if not forcar_nova_busca:
        ja_salvas = historico_prob.listar_por_data(data_str, tipo_mercado=tipo_mercado)
        if ja_salvas:
            return jsonify({
                "data": data_str,
                "de_cache": True,
                "aviso": "Modo só-probabilidade (Série B, sem odds reais - não representa aposta de valor)",
                "debug": {},
                "selecoes": [],
                "combinacoes": [
                    {"prob_final": m["prob_final"], "selecoes": m["selecoes"], "id": m["id"], "resultado": m["resultado"]}
                    for m in ja_salvas
                ],
            })

    debug_info = {}
    try:
        selecoes = serie_b_probabilidade.gerar_selecoes_probabilidade(
            data_str, prob_minima=prob_minima, debug_info=debug_info
        )
    except Exception as exc:
        return jsonify({"erro": str(exc), "debug": debug_info}), 502

    if tipo_mercado == "gols":
        selecoes = [s for s in selecoes if "gols" in s["mercado"]]
    elif tipo_mercado == "vencedor":
        selecoes = [s for s in selecoes if s["mercado"].endswith(" vencedor") or s["mercado"] == "empate"]

    combinacoes = serie_b_probabilidade.montar_combinacoes(selecoes, min_sel, max_sel)

    try:
        historico_prob.salvar_combinacoes(data_str, combinacoes, tipo_mercado=tipo_mercado)
    except Exception:
        pass

    return jsonify({
        "data": data_str,
        "aviso": "Modo só-probabilidade (Série B, sem odds reais - não representa aposta de valor)",
        "debug": debug_info,
        "selecoes": selecoes,
        "combinacoes": combinacoes,
    })


@app.route("/api/historico-prob")
def api_historico_prob():
    try:
        resultados_prob.atualizar_pendentes()
    except Exception:
        pass
    return jsonify({
        "itens": historico_prob.listar_historico(),
        "semanas": historico_prob.resumo_semanal(),
    })
@app.route("/api/sportmonks-amostra-placar/<int:liga_id>")
def api_sportmonks_amostra_placar(liga_id):
    if not sportmonks_diag.disponivel():
        return jsonify({"erro": "SPORTMONKS_API_TOKEN não configurado."}), 502
    try:
        return jsonify(sportmonks_diag.buscar_amostra_fixture_com_placar(liga_id))
    except Exception as exc:
        return jsonify({"erro": str(exc)}), 502


@app.route("/api/sportmonks-diagnostico")
def api_sportmonks_diagnostico():
    if not sportmonks_diag.disponivel():
        return jsonify({"erro": "SPORTMONKS_API_TOKEN não configurado no servidor."}), 502

    data_str = request.args.get("data", date.today().isoformat())
    try:
        ligas_brasil, total_ligas, meta = sportmonks_diag.encontrar_ligas_brasil()
    except Exception as exc:
        return jsonify({"erro": str(exc)}), 502

    resultado = {
        "total_ligas_no_plano": total_ligas,
        "ligas_brasil_encontradas": ligas_brasil,
        "meta_da_conta": meta,
    }

    todas_ligas = request.args.get("todas") == "1"
    if todas_ligas:
        ligas, _ = sportmonks_diag.buscar_ligas()
        resultado["todas_as_ligas"] = [
            {"id": l.get("id"), "name": l.get("name")} for l in ligas if isinstance(l, dict)
        ]
        jogos_por_liga = {}
        for liga in ligas:
            if not isinstance(liga, dict):
                continue
            try:
                jogos = sportmonks_diag.buscar_jogos_data(liga["id"], data_str)
                jogos_por_liga[liga.get("name", str(liga.get("id")))] = [j.get("name") for j in jogos]
            except Exception as exc:
                jogos_por_liga[liga.get("name", str(liga.get("id")))] = f"erro: {exc}"
        resultado["jogos_na_data_todas_ligas"] = jogos_por_liga
    elif ligas_brasil:
        jogos_por_liga = {}
        for liga in ligas_brasil:
            try:
                jogos = sportmonks_diag.buscar_jogos_data(liga["id"], data_str)
                jogos_por_liga[liga["name"]] = [j.get("name") for j in jogos]
            except Exception as exc:
                jogos_por_liga[liga["name"]] = f"erro: {exc}"
        resultado["jogos_na_data"] = jogos_por_liga

    return jsonify(resultado)


@app.route("/api/historico")
def api_historico():
    """Lista o histórico. Opcional: ?semana=2026-W33 pra filtrar uma semana."""
    try:
        resultados.atualizar_pendentes()
    except Exception:
        pass  # se a checagem de resultados falhar, ainda mostra o histórico salvo
    semana = request.args.get("semana")
    return jsonify({"itens": historico.listar_historico(semana=semana)})


@app.route("/api/historico/<int:item_id>", methods=["PATCH"])
def api_marcar_resultado(item_id):
    """Body JSON: {"resultado": "ganhou" | "perdeu" | "pendente"}"""
    body = request.get_json(force=True, silent=True) or {}
    resultado = body.get("resultado")
    try:
        historico.marcar_resultado(item_id, resultado)
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/historico/resumo-semanal")
def api_resumo_semanal():
    return jsonify({"semanas": historico.resumo_semanal()})


@app.route("/api/datas-disponiveis")
def datas_disponiveis():
    """Devolve hoje e os próximos 6 dias, pra popular o seletor de data no app."""
    hoje = date.today()
    dias = [(hoje + timedelta(days=i)).isoformat() for i in range(7)]
    return jsonify({"datas": dias})


@app.route("/")
def home():
    return app.send_static_file("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "servico": "multiplas-ev-backend"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
