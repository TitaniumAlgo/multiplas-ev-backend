"""
Histórico do modo "Série B sem odds" - só acompanha ACERTO/ERRO das
combinações geradas pelo modelo de probabilidade, sem valor em R$
(porque essa fonte não tem odds reais pra calcular lucro de verdade).
"""

import hashlib
import json
import sqlite3
from datetime import datetime

DB_PATH = "historico_prob.db"


def _conectar():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def iniciar_banco():
    conn = _conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historico_prob (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_jogo TEXT NOT NULL,
            chave TEXT NOT NULL UNIQUE,
            tipo_mercado TEXT NOT NULL DEFAULT 'todos',
            prob_final REAL NOT NULL,
            selecoes_json TEXT NOT NULL,
            resultado TEXT NOT NULL DEFAULT 'pendente',
            criado_em TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _chave(data_jogo, combinacao, tipo_mercado):
    partes = sorted(f"{s['jogo']}|{s['mercado']}" for s in combinacao["selecoes"])
    bruto = data_jogo + "::" + tipo_mercado + "::" + "::".join(partes)
    return hashlib.sha256(bruto.encode()).hexdigest()[:16]


def salvar_combinacoes(data_jogo, combinacoes, tipo_mercado="todos"):
    conn = _conectar()
    for c in combinacoes:
        chave = _chave(data_jogo, c, tipo_mercado)
        try:
            conn.execute(
                "INSERT INTO historico_prob (data_jogo, chave, tipo_mercado, prob_final, selecoes_json, resultado, criado_em) "
                "VALUES (?, ?, ?, ?, ?, 'pendente', ?)",
                (data_jogo, chave, tipo_mercado, c["prob_final"], json.dumps(c["selecoes"]), datetime.utcnow().isoformat()),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()


def listar_por_data(data_jogo, tipo_mercado="todos"):
    conn = _conectar()
    linhas = conn.execute(
        "SELECT * FROM historico_prob WHERE data_jogo = ? AND tipo_mercado = ? ORDER BY prob_final DESC",
        (data_jogo, tipo_mercado),
    ).fetchall()
    conn.close()
    return [_linha_para_dict(r) for r in linhas]


def listar_historico(semana=None):
    conn = _conectar()
    if semana:
        linhas = conn.execute(
            "SELECT * FROM historico_prob WHERE strftime('%Y-W%W', data_jogo) = ? ORDER BY data_jogo DESC", (semana,)
        ).fetchall()
    else:
        linhas = conn.execute("SELECT * FROM historico_prob ORDER BY data_jogo DESC").fetchall()
    conn.close()
    return [_linha_para_dict(r) for r in linhas]


def _linha_para_dict(r):
    return {
        "id": r["id"],
        "data_jogo": r["data_jogo"],
        "tipo_mercado": r["tipo_mercado"],
        "prob_final": r["prob_final"],
        "selecoes": json.loads(r["selecoes_json"]),
        "resultado": r["resultado"],
    }


def marcar_resultado(item_id, resultado):
    if resultado not in ("ganhou", "perdeu", "pendente"):
        raise ValueError("resultado inválido")
    conn = _conectar()
    conn.execute("UPDATE historico_prob SET resultado = ? WHERE id = ?", (resultado, item_id))
    conn.commit()
    conn.close()


def resumo_semanal():
    conn = _conectar()
    linhas = conn.execute("""
        SELECT strftime('%Y-W%W', data_jogo) AS semana, resultado, COUNT(*) AS total
        FROM historico_prob GROUP BY semana, resultado
    """).fetchall()
    conn.close()

    resumo = {}
    for r in linhas:
        semana = r["semana"]
        resumo.setdefault(semana, {"ganhou": 0, "perdeu": 0, "pendente": 0})
        resumo[semana][r["resultado"]] = r["total"]

    resultado = []
    for semana, contagem in resumo.items():
        decididas = contagem["ganhou"] + contagem["perdeu"]
        taxa = round(contagem["ganhou"] / decididas * 100, 1) if decididas else None
        resultado.append({"semana": semana, **contagem, "taxa_acerto": taxa})

    return sorted(resultado, key=lambda x: x["semana"], reverse=True)
