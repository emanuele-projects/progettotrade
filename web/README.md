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

## Cosa mostra (e cosa no)

✅ Soldi (capitale iniziale, valore ora, P&L realizzato vs sulla carta) ·
esposizione e leva media · bilancia long/short · tabella posizioni con ROE ·
track record 7 giorni (win-rate, profit factor) · curva P&L realizzato.

➖ Il **ragionamento testuale di Claude** (market view, motivazioni per crypto,
costi delle chiamate) sta nel journal SQLite sulla VM, non su Binance: quello
resta sulla dashboard Oracle completa (tunnel SSH). Questa vetrina Vercel copre
tutta la parte finanziaria live.
