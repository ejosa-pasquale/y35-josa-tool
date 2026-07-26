# Guida al deploy — Y35 / JoSa Sizing Tool (versione semplice: UN solo servizio)

Backend e frontend ora sono uniti: il server Python serve anche la pagina.
Un solo servizio da mettere online, un solo indirizzo, niente da copiare a
mano tra due posti diversi.

---

## 1. Metti il codice su GitHub

Serve un repository perché Render si collega a git (non c'è un modo per
evitarlo su questa piattaforma — se proprio vuoi evitare GitHub del tutto,
vedi l'alternativa PythonAnywhere in fondo).

1. Vai su **github.com** → crea un account se non l'hai già
2. Crea un nuovo repository (es. "y35-josa-tool")
3. Carica dentro tutto il contenuto della cartella `02_api_backend/`
   (quella dello zip che ti ho consegnato — include già `frontend/index.html`)

## 2. Render.com (gratis)

1. **render.com** → crea account gratuito → **New +** → **Web Service**
2. Collega il repository GitHub appena creato
3. Compila così:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
4. Sezione **Environment** → aggiungi due variabili:
   - `ACCESS_PASSWORD` → la password che darai ai lead (es. `y35demo2026`)
   - `ACCESS_TOKEN_SECRET` → stringa casuale, generala così sul tuo computer:
     ```
     python3 -c "import secrets; print(secrets.token_hex(32))"
     ```
     e incolla il risultato
5. **Create Web Service** → aspetta qualche minuto

Alla fine avrai un unico indirizzo, tipo `https://y35-josa-tool.onrender.com`
— aprilo nel browser: deve comparire **direttamente lo strumento**, con la
schermata "Accesso riservato" già attiva. Non c'è nient'altro da configurare:
niente Netlify, niente indirizzo da incollare a mano, niente DNS per adesso.

## 3. Sottodominio (opzionale, quando vuoi tool.y35.eu)

Su Render: **Settings** → **Custom Domains** → aggiungi `tool.y35.eu` → Render
ti mostra un valore CNAME. Vai dove gestisci il DNS di y35.eu (Aruba) e
aggiungi un record CNAME: nome `tool`, valore quello dato da Render.

Nel frattempo l'indirizzo `.onrender.com` funziona già perfettamente per
provarlo o mandarlo a un primo lead.

---

## Alternativa ancora più semplice, se vuoi evitare GitHub del tutto

**PythonAnywhere** (pythonanywhere.com) ha un piano gratuito e permette di
caricare i file direttamente dal browser, senza passare da git — più lento da
configurare la prima volta (il pannello è meno immediato di Render), ma zero
bisogno di un account GitHub. Se preferisci questa strada dimmelo e ti scrivo
la guida dedicata — i passaggi sono diversi da quelli sopra.

---

## Cosa manca ancora (fatti da te)

- **Restringere il CORS**: in `app/main.py`, riga `allow_origins=["*"]` — una
  volta che hai l'indirizzo definitivo, vale la pena restringerla (non
  indispensabile col gate già attivo, ma buona pratica).
- **Cambiare la password quando vuoi**: Render → Environment → modifica
  `ACCESS_PASSWORD` → salva, si riavvia da solo.
