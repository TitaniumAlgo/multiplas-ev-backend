"""
Histórico de múltiplas (acertos/erros).

Guarda em SQLite, no mesmo servidor. AVISO: no plano gratuito do Render,
o disco é apagado quando o serviço reinicia (o que acontece de vez em
quando sozinho, ou a cada novo deploy) — então esse histórico pode
resetar eventualmente. Se isso incomodar no futuro, dá pra migrar pra um
banco externo (Supabase, por exemplo) sem mudar o resto do app.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, date

DB_PATH = "historico.db"


def _conectar():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def iniciar_banco():
    conn = _conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_jogo TEXT NOT NULL,
            chave TEXT NOT NULL UNIQUE,
            odd_final REAL NOT NULL,
            ev_final REAL NOT NULL,
            selecoes_json TEXT NOT NULL,
            resultado TEXT NOT NULL DEFAULT 'pendente',
            criado_em TEXT NOT NULL
        )
    """)
    try:
        conn.execute("ALTER TABLE historico ADD COLUMN tipo_mercado TEXT NOT NULL DEFAULT 'todos'")
    except sqlite3.OperationalError:
        pass  # coluna já existe (banco criado numa versão anterior)
    conn.commit()
    conn.close()


def _chave_da_multipla(data_jogo, multipla, tipo_mercado):
    """Gera uma chave única pra não duplicar a mesma múltipla se o app
    buscar de novo o mesmo dia (ex: usuário toca 'Buscar' várias vezes)."""
    partes = sorted(f"{s['jogo']}|{s['mercado']}" for s in multipla["selecoes"])
    bruto = data_jogo + "::" + tipo_mercado + "::" + "::".join(partes)
    return hashlib.sha256(bruto.encode()).hexdigest()[:16]


def salvar_multiplas(data_jogo, multiplas, tipo_mercado="todos"):
    """Salva cada múltipla gerada como 'pendente', ignorando duplicatas."""
    conn = _conectar()
    for m in multiplas:
        chave = _chave_da_multipla(data_jogo, m, tipo_mercado)
        try:
            conn.execute(
                "INSERT INTO historico (data_jogo, chave, odd_final, ev_final, selecoes_json, resultado, criado_em, tipo_mercado) "
                "VALUES (?, ?, ?, ?, ?, 'pendente', ?, ?)",
                (data_jogo, chave, m["odd_final"], m["ev_final"], json.dumps(m["selecoes"]), datetime.utcnow().isoformat(), tipo_mercado),
            )
        except sqlite3.IntegrityError:
            pass  # já salva antes, ignora
    conn.commit()
    conn.close()


VALOR_APOSTA_SIMULADA = 10.0


def _lucro_do_item(odd_final, resultado):
    if resultado == "ganhou":
        return round(VALOR_APOSTA_SIMULADA * (odd_final - 1), 2)
    if resultado == "perdeu":
        return -VALOR_APOSTA_SIMULADA
    return None  # pendente - ainda não conta


def listar_por_data(data_jogo, tipo_mercado="todos"):
    conn = _conectar()
    linhas = conn.execute(
        "SELECT * FROM historico WHERE data_jogo = ? AND tipo_mercado = ? ORDER BY ev_final DESC",
        (data_jogo, tipo_mercado),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "data_jogo": r["data_jogo"],
            "odd_final": r["odd_final"],
            "ev_final": r["ev_final"],
            "selecoes": json.loads(r["selecoes_json"]),
            "resultado": r["resultado"],
            "lucro": _lucro_do_item(r["odd_final"], r["resultado"]),
            "tipo_mercado": r["tipo_mercado"],
        }
        for r in linhas
    ]


def listar_historico(semana=None):
    conn = _conectar()
    if semana:
        linhas = conn.execute(
            "SELECT * FROM historico WHERE strftime('%Y-W%W', data_jogo) = ? ORDER BY data_jogo DESC", (semana,)
        ).fetchall()
    else:
        linhas = conn.execute("SELECT * FROM historico ORDER BY data_jogo DESC").fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "data_jogo": r["data_jogo"],
            "odd_final": r["odd_final"],
            "ev_final": r["ev_final"],
            "selecoes": json.loads(r["selecoes_json"]),
            "resultado": r["resultado"],
            "lucro": _lucro_do_item(r["odd_final"], r["resultado"]),
            "tipo_mercado": r["tipo_mercado"],
        }
        for r in linhas
    ]


def marcar_resultado(item_id, resultado):
    if resultado not in ("ganhou", "perdeu", "pendente"):
        raise ValueError("resultado inválido")
    conn = _conectar()
    conn.execute("UPDATE historico SET resultado = ? WHERE id = ?", (resultado, item_id))
    conn.commit()
    conn.close()


def resumo_semanal():
    """Agrupa por semana ISO (ano-Wsemana), calcula taxa de acerto e o
    lucro/perda simulado (considerando R$10,00 por múltipla)."""
    conn = _conectar()
    linhas = conn.execute("""
        SELECT strftime('%Y-W%W', data_jogo) AS semana, resultado, odd_final
        FROM historico
    """).fetchall()
    conn.close()

    resumo = {}
    for r in linhas:
        semana = r["semana"]
        resumo.setdefault(semana, {"ganhou": 0, "perdeu": 0, "pendente": 0, "lucro": 0.0})
        resumo[semana][r["resultado"]] += 1
        lucro = _lucro_do_item(r["odd_final"], r["resultado"])
        if lucro is not None:
            resumo[semana]["lucro"] += lucro

    resultado = []
    for semana, contagem in resumo.items():
        decididas = contagem["ganhou"] + contagem["perdeu"]
        taxa = round(contagem["ganhou"] / decididas * 100, 1) if decididas else None
        resultado.append({
            "semana": semana,
            "ganhou": contagem["ganhou"],
            "perdeu": contagem["perdeu"],
            "pendente": contagem["pendente"],
            "taxa_acerto": taxa,
            "lucro": round(contagem["lucro"], 2),
            "investido": round(decididas * VALOR_APOSTA_SIMULADA, 2),
        })

    return sorted(resultado, key=lambda x: x["semana"], reverse=True)
