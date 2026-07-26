"""
app.auth — gate di accesso semplice per lo strumento.

Non e' un sistema di autenticazione enterprise (niente utenti, ruoli, refresh
token) — e' un filtro leggero per lead-gen: una password condivisa, distribuita
manualmente a chi richiede una consulenza, verificata lato server (non scritta
in chiaro nel codice del frontend) con un token temporaneo firmato.

Configurazione tramite variabili d'ambiente (mai nel codice):
- ACCESS_PASSWORD: la password condivisa da distribuire ai lead
- ACCESS_TOKEN_SECRET: chiave segreta per firmare i token (genera una stringa
  casuale lunga, es. `python3 -c "import secrets; print(secrets.token_hex(32))"`)

Se ACCESS_PASSWORD non e' impostata, il gate e' DISATTIVATO (tutte le richieste
passano) — utile in sviluppo locale, ma verificare che sia impostata prima di
andare online.
"""

import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException

TOKEN_VALIDITA_SECONDI = 60 * 60 * 12  # 12 ore


def _secret() -> str:
    return os.environ.get("ACCESS_TOKEN_SECRET", "")


def _password_attesa() -> str:
    return os.environ.get("ACCESS_PASSWORD", "")


def gate_attivo() -> bool:
    return bool(_password_attesa())


def verifica_password(password: str) -> bool:
    """Confronto a tempo costante, per non rivelare via timing quanto della
    password inserita e' corretta."""
    atteso = _password_attesa()
    if not atteso:
        return True  # gate disattivato: nessuna password configurata
    return hmac.compare_digest(password or "", atteso)


def genera_token() -> str:
    scadenza = int(time.time()) + TOKEN_VALIDITA_SECONDI
    firma = hmac.new(_secret().encode(), str(scadenza).encode(), hashlib.sha256).hexdigest()
    return f"{scadenza}.{firma}"


def _token_valido(token: str) -> bool:
    if not gate_attivo():
        return True
    if not token or "." not in token:
        return False
    try:
        scadenza_str, firma = token.split(".", 1)
        scadenza = int(scadenza_str)
    except ValueError:
        return False
    if time.time() > scadenza:
        return False
    firma_attesa = hmac.new(_secret().encode(), scadenza_str.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma, firma_attesa)


def richiede_accesso_valido(x_access_token: str = Header(default="")):
    """Dependency FastAPI: da usare su ogni endpoint che deve stare dietro al gate."""
    if not _token_valido(x_access_token):
        raise HTTPException(status_code=401, detail="Accesso non valido o scaduto. Richiedi una nuova password.")
