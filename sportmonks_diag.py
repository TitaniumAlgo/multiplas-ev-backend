"""
Integração com a SportMonks - fase de diagnóstico.

Antes de montar toda a lógica de probabilidade em cima dela, esse módulo
só confirma o que o plano da conta realmente libera: quais ligas, e quais
jogos aparecem pra uma data. Evita repetir o problema que tivemos com as
outras duas APIs (só descobrir a restrição depois de tudo pronto).
"""

import os
import requests

SPORTMONKS_TOKEN = os.environ.get("SPORTMONKS_API_TOKEN")
SPORTMONKS_BASE = "https://api.sportmonks.com/v3/football"


def disponivel():
    return bool(SPORTMONKS_TOKEN)


def buscar_ligas():
    """Lista as ligas que o plano da conta libera."""
    resp = requests.get(
        f"{SPORTMONKS_BASE}/leagues",
        params={"api_token": SPORTMONKS_TOKEN},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", []), body.get("meta", {})


def encontrar_ligas_brasil():
    """Procura por ligas do Brasil (Brasileirão Série A/B) na lista liberada."""
    ligas, meta = buscar_ligas()
    encontradas = [
        {"id": l.get("id"), "name": l.get("name"), "country_id": l.get("country_id")}
        for l in ligas
        if isinstance(l, dict) and any(
            termo in (l.get("name") or "").lower()
            for termo in ["brasil", "brazil", "série", "serie"]
        )
    ]
    return encontradas, len(ligas), meta


def buscar_jogos_data(liga_id, data_str):
    """Jogos de uma liga específica numa data (YYYY-MM-DD)."""
    resp = requests.get(
        f"{SPORTMONKS_BASE}/fixtures/date/{data_str}",
        params={"api_token": SPORTMONKS_TOKEN, "filters": f"fixtureLeagues:{liga_id}", "include": "participants"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])
