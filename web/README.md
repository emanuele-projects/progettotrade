# Dashboard su Vercel (vetrina live)

Streamlit non gira su Vercel (è un server sempre acceso; Vercel è serverless).
Questa cartella è una versione **serverless** della dashboard: una pagina statica
(`index.html`) + una funzione (`api/state.py`) che, a ogni caricamento, interroga
Binance Futures Testnet e restituisce soldi, posizioni, P&L e track record in
diretta. Il bot vero continua a girare 24/7 sulla VM Oracle: Vercel fa solo da
vetrina di sola lettura.

## Deploy (una volta)

1. **vercel.com** → *Add New… → Project* → importa il repo GitHub
   `emanuele-projects/progettotrade`.
2. Nella schermata di configurazione, imposta **Root Directory = `web`**
   (fondamentale: così Vercel guarda solo questa cartella e non il bot in radice).
3. **Environment Variables** — aggiungi queste (i valori li copi dal tuo file
   `.env`, **non** vanno nel repo):

   | Nome | Valore |
   |---|---|
   | `BINANCE_API_KEY` | la tua chiave testnet |
   | `BINANCE_API_SECRET` | il tuo secret testnet |
   | `DASHBOARD_PASSWORD` | la password della dashboard |
   | `INITIAL_CAPITAL` | `3912.89` (opzionale — il capitale di partenza) |

4. **Deploy**. Vercel ti dà un URL tipo `https://progettotrade.vercel.app`.
5. Aprilo, inserisci la password → vedi il conto live da qualsiasi dispositivo.

## Sicurezza

- Le chiavi sono **testnet** (soldi finti): anche nel caso peggiore controllano
  solo un conto di prova. Vivono come Environment Variables su Vercel, mai nel repo.
- I dati sono serviti **solo** dietro la password (`?pw=…` verificata lato server;
  401 senza). Vercel è sempre HTTPS.
- La funzione è **sola lettura**: interroga il conto, non piazza né chiude ordini.

## Cosa mostra

✅ Insight ordinati (chi paga, chi costa, concentrazione, costi, striscia) ·
soldi (P&L realizzato vs sulla carta, funding, commissioni) · guadagno per
crypto con statistiche trade (ordinabile/filtrabile) · grafico temporale
multi-cripto (P&L cumulato per moneta) · alpha vs BTC + drawdown · posizioni
(ordinabile) · ultimi movimenti · track record 7 giorni · curva P&L.

🧠 **La testa di Claude** (market view, ragionamenti per crypto, lezioni della
memoria, equity con marker delle decisioni) arriva dal journal della VM tramite
uno snapshot JSON. Setup una tantum:

1. Vercel dashboard → **Storage → Create → Blob** (piano hobby ok).
2. Copia il token `BLOB_READ_WRITE_TOKEN` nel **`.env` della VM Oracle**
   e riavvia il servizio (`sudo systemctl restart tradingbot`).
3. Entro ~1 minuto il bot carica lo snapshot e logga la riga
   `snapshot uploaded — set SNAPSHOT_URL on Vercel to: https://…` nel bot.log.
   Copia quell'URL nelle env del progetto Vercel come `SNAPSHOT_URL` e redeploy.

Senza questi passi la dashboard funziona lo stesso: mostra tutto tranne la
sezione dei ragionamenti (al suo posto c'è un promemoria con le istruzioni).
Lo snapshot si aggiorna ogni 5 minuti; l'URL del blob è oscuro ma tecnicamente
pubblico — contiene solo commenti di mercato del bot testnet, nessuna chiave.
