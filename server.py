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

from multiplas_ev import gerar_selecoes, montar_multiplas
from buscar_jogos_reais import montar_jogos_reais

app = Flask(__name__)
CORS(app)  # permite o app web (rodando no celular) chamar este servidor


@app.route("/api/multiplas")
def api_multiplas():
    """
    Parâmetros opcionais na URL:
      ?data=2026-08-16   (padrão: hoje)
      ?ev_minimo=0.05    (padrão: 5%)
      ?odd_max=15        (teto da odd final da múltipla)
      ?min_selecoes=2
      ?max_selecoes=4
    """
    data_str = request.args.get("data", date.today().isoformat())
    ev_minimo = float(request.args.get("ev_minimo", 0.05))
    odd_max = float(request.args.get("odd_max", 15.0))
    min_sel = int(request.args.get("min_selecoes", 2))
    max_sel = int(request.args.get("max_selecoes", 4))

    try:
        jogos = montar_jogos_reais(data_str)
    except Exception as exc:  # erro de rede/API -> devolve erro legível pro app
        return jsonify({"erro": str(exc)}), 502

    selecoes = gerar_selecoes(jogos, ev_minimo=ev_minimo)
    multiplas = montar_multiplas(
        selecoes, min_selecoes=min_sel, max_selecoes=max_sel, odd_final_max=odd_max
    )

    # formata a resposta em JSON simples pro app consumir
    return jsonify({
        "data": data_str,
        "total_jogos_analisados": len(jogos),
        "selecoes": selecoes,
        "multiplas": [
            {
                "odd_final": m["odd_final"],
                "ev_final": m["ev_final"],
                "selecoes": [
                    {"jogo": s["jogo"], "mercado": s["mercado"], "odd": s["odd"],
                     "prob_real": s["prob_real"], "ev": s["ev"]}
                    for s in m["selecoes"]
                ],
            }
            for m in multiplas
        ],
    })


@app.route("/api/datas-disponiveis")
def datas_disponiveis():
    """Devolve hoje e os próximos 6 dias, pra popular o seletor de data no app."""
    hoje = date.today()
    dias = [(hoje + timedelta(days=i)).isoformat() for i in range(7)]
    return jsonify({"datas": dias})


@app.route("/")
def health():
    return jsonify({"status": "ok", "servico": "multiplas-ev-backend"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
