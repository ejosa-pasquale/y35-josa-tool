"""
app.auth — sistema di accesso a due livelli per Y35/JoSa.

LIVELLO 1 — EMAIL (chat + strumenti):
  L'utente inserisce la propria email. Il sistema genera un token valido 3 mesi
  e invia una notifica a info@evfieldservice.it con i dati del lead.
  La notifica viene inviata solo alla prima registrazione (email non vista prima).

LIVELLO 2 — PASSWORD (wizard manuale):
  Password condivisa scelta da Pasquale, configurata come variabile d'ambiente.
  Token valido 30 giorni.

Variabili d'ambiente richieste:
  ACCESS_PASSWORD      — password per il wizard manuale
  ACCESS_TOKEN_SECRET  — chiave HMAC per firmare i token
  RESEND_API_KEY       — API key Resend per notifiche email
"""

import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException

TOKEN_VALIDITA_EMAIL_SECONDI   = 60 * 60 * 24 * 90   # 3 mesi
TOKEN_VALIDITA_PWD_SECONDI     = 60 * 60 * 24 * 30   # 30 giorni

EMAIL_NOTIFICA = "info@evfieldservice.it"

# Email già registrate in memoria (reset al riavvio del server — sufficiente per leadgen)
_email_registrate: set = set()


def _secret() -> str:
    return os.environ.get("ACCESS_TOKEN_SECRET", "dev-secret-non-sicuro")


def _password_attesa() -> str:
    return os.environ.get("ACCESS_PASSWORD", "")


def gate_attivo() -> bool:
    return bool(_password_attesa())


# ── TOKEN ──────────────────────────────────────────────────────────────────

def genera_token(tipo: str = "pwd") -> str:
    """
    tipo="pwd"   → token 30 giorni (wizard manuale)
    tipo="email" → token 3 mesi (chat + strumenti)
    """
    validita = TOKEN_VALIDITA_EMAIL_SECONDI if tipo == "email" else TOKEN_VALIDITA_PWD_SECONDI
    scadenza = int(time.time()) + validita
    payload = f"{scadenza}.{tipo}"
    firma = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{firma}"


def _token_valido(token: str) -> bool:
    if not gate_attivo():
        return True
    if not token or token.count(".") < 2:
        return False
    try:
        parts = token.split(".", 2)
        scadenza_str, tipo, firma = parts[0], parts[1], parts[2]
        scadenza = int(scadenza_str)
    except (ValueError, IndexError):
        return False
    if time.time() > scadenza:
        return False
    payload = f"{scadenza_str}.{tipo}"
    firma_attesa = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma, firma_attesa)


def _token_tipo(token: str) -> str:
    """Restituisce 'email', 'pwd' o '' se token non valido."""
    try:
        return token.split(".", 2)[1]
    except (IndexError, AttributeError):
        return ""


# ── VERIFICA PASSWORD ───────────────────────────────────────────────────────

def verifica_password(password: str) -> bool:
    atteso = _password_attesa()
    if not atteso:
        return True
    return hmac.compare_digest(password or "", atteso)


# ── REGISTRAZIONE EMAIL ─────────────────────────────────────────────────────

async def registra_email(email: str, fonte: str = "chat") -> dict:
    """
    Registra una email e invia notifica a info@evfieldservice.it.
    Ritorna il token email valido 3 mesi.
    Invia notifica solo se email non già registrata.
    """
    import re
    email = email.strip().lower()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        raise HTTPException(status_code=422, detail="Email non valida.")

    prima_volta = email not in _email_registrate
    _email_registrate.add(email)

    if prima_volta:
        await _invia_notifica(email, fonte)

    token = genera_token("email")
    return {
        "token": token,
        "email": email,
        "prima_volta": prima_volta,
        "scadenza_giorni": 90,
    }


async def _invia_notifica(email: str, fonte: str):
    """Invia notifica a info@evfieldservice.it via Resend."""
    import os, httpx
    from datetime import datetime

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print(f"[AUTH] Nuovo lead: {email} (fonte: {fonte}) — RESEND_API_KEY non configurata")
        return

    ora = datetime.now().strftime("%d/%m/%Y %H:%M")
    body = {
        "from": "Y35 Tool <noreply@y35.eu>",
        "to": [EMAIL_NOTIFICA],
        "subject": f"🆕 Nuovo lead — {email}",
        "html": f"""
        <div style="font-family:sans-serif;max-width:500px;margin:0 auto;">
          <h2 style="color:#14DDB8;">Nuovo accesso al tool Y35/JoSa</h2>
          <table style="width:100%;border-collapse:collapse;">
            <tr><td style="padding:8px;color:#666;">Email</td><td style="padding:8px;font-weight:600;">{email}</td></tr>
            <tr><td style="padding:8px;color:#666;">Fonte</td><td style="padding:8px;">{fonte}</td></tr>
            <tr><td style="padding:8px;color:#666;">Data/ora</td><td style="padding:8px;">{ora}</td></tr>
            <tr><td style="padding:8px;color:#666;">Token valido</td><td style="padding:8px;">90 giorni</td></tr>
          </table>
          <p style="color:#888;font-size:12px;margin-top:20px;">
            Questo lead ha usato la modalità: <b>{fonte}</b>.<br>
            Contattalo entro 24-48 ore per massimizzare la conversione.
          </p>
        </div>
        """
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body
            )
        if r.status_code not in (200, 201):
            print(f"[AUTH] Resend error {r.status_code}: {r.text[:200]}")
        else:
            print(f"[AUTH] Notifica inviata per {email}")
    except Exception as e:
        print(f"[AUTH] Errore invio notifica: {e}")


# ── DEPENDENCY FASTAPI ──────────────────────────────────────────────────────

def richiede_accesso_valido(x_access_token: str = Header(default="")):
    """Accetta token sia email che password."""
    if not _token_valido(x_access_token):
        raise HTTPException(
            status_code=401,
            detail="Accesso non valido o scaduto. Inserisci la tua email per continuare."
        )


def richiede_password(x_access_token: str = Header(default="")):
    """Richiede specificamente token da password (wizard manuale)."""
    if not _token_valido(x_access_token):
        raise HTTPException(status_code=401, detail="Password non valida.")
    if gate_attivo() and _token_tipo(x_access_token) not in ("pwd", ""):
        raise HTTPException(
            status_code=403,
            detail="Questa funzione richiede la password. Inseriscila per continuare."
        )
