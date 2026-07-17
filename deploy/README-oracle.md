# Deploy 24/7 su Oracle Cloud (Always Free)

Obiettivo: far girare il bot su una VM Linux sempre accesa, così **non dipende
più dal PC acceso + loggato**. Su Linux la persistenza la gestisce **systemd**
(riavvio ai crash + all'accensione), che sostituisce la cartella Startup di
Windows. Il codice non cambia: `runner.py` è già cross-platform.

> ⚠️ **Regola d'oro — una sola istanza.** Le chiavi testnet sono le stesse, quindi
> PC e VM userebbero lo **stesso account Binance**. Due bot sullo stesso account
> = ordini doppi (è il bug che aveva bruciato equity). Il passaggio è a staffetta:
> **prima si spegne il bot sul PC, poi si accende sulla VM.** Mai i due insieme.

---

## 1. Crea la VM (console Oracle — lo fai tu)

1. Menu ☰ → **Compute → Instances → Create instance**.
2. **Name**: `tradingbot`.
3. **Image**: *Canonical Ubuntu 22.04*.
4. **Shape**: *Change shape* → **Ampere (ARM)** → `VM.Standard.A1.Flex`,
   **1 OCPU / 6 GB** (rientra nell'Always Free; puoi arrivare a 4 OCPU / 24 GB).
   - Se dice *"Out of capacity"*: cambia Availability Domain (AD-1/2/3) e riprova,
     oppure riprova più tardi (capita spesso sull'ARM free). Ultima spiaggia:
     `VM.Standard.E2.1.Micro` (AMD, 1 GB — funziona ma è tirato: vedi nota RAM in fondo).
5. **Networking**: lascia i default (crea VCN + subnet pubblica). Di default è
   aperta **solo la 22 (SSH)** — la dashboard la raggiungeremo via tunnel SSH,
   quindi **non aprire altre porte**.
6. **SSH keys**: *Generate a key pair for me* → **Download private key** e salvala,
   es. `C:\Users\emanu\.ssh\oracle_tradingbot.key`. (Oppure incolla la tua chiave
   pubblica già esistente.)
7. **Create**. Quando è *Running*, copia il **Public IP address** dalla pagina
   dei dettagli.

Poi mandami **IP pubblico** + **percorso del file .key**: da lì guido io il resto
dal tuo PC. (Oppure esegui tu i comandi qui sotto — sono tutti pronti.)

---

## 2. Primo accesso SSH (dal PC)

Windows è schizzinoso sui permessi della chiave: la prima volta esegui
```powershell
icacls "C:\Users\emanu\.ssh\oracle_tradingbot.key" /inheritance:r /grant:r "$env:USERNAME:R"
```
Poi entra (accetta il fingerprint la prima volta):
```powershell
ssh -i "C:\Users\emanu\.ssh\oracle_tradingbot.key" ubuntu@<IP_PUBBLICO>
```

---

## 3. Trasferisci il codice (dal PC → VM)

Il repo è privato, quindi niente `git clone` sulla VM: impacchettiamo l'albero
locale (senza venv/git/dati/segreti) e lo copiamo cifrato via `scp`.

Da PowerShell, nella cartella del progetto (`D:\Claude\trading-bot`):
```powershell
# bundle del solo codice (esclude roba pesante/rigenerabile e i segreti)
tar --exclude=.venv --exclude=.git --exclude=data --exclude=__pycache__ `
    --exclude=*.pyc --exclude=.env -czf "$env:TEMP\bot.tar.gz" .

# copia sulla VM
scp -i "C:\...\oracle_tradingbot.key" "$env:TEMP\bot.tar.gz" ubuntu@<IP>:~/bot.tar.gz
```
Sulla VM:
```bash
mkdir -p ~/progettotrade && tar -xzf ~/bot.tar.gz -C ~/progettotrade && rm ~/bot.tar.gz
```

---

## 4. Installa (sulla VM)

```bash
cd ~/progettotrade
bash deploy/setup.sh
```
Installa python+venv, le dipendenze, e registra il servizio systemd
`tradingbot` (abilitato all'avvio). **Non** avvia ancora il bot: prima i segreti.

---

## 5. Segreti + continuità (la staffetta)

Questa è la parte delicata. Ordine esatto:

**5a. Spegni il bot sul PC** (rilascia l'account testnet e chiude pulito il journal):
```powershell
# ferma il supervisor locale (rilascia il lock 47821)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'runner\.py|main\.py|streamlit' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**5b. Copia `.env` + lo storico `journal.db`** sulla VM (dopo lo stop il DB è
consistente → la VM riparte con lo stesso storico P&L e gli stessi target SL/TP
delle posizioni aperte):
```powershell
scp -i "C:\...\key" .env            ubuntu@<IP>:~/progettotrade/.env
scp -i "C:\...\key" data\journal.db ubuntu@<IP>:~/progettotrade/data/journal.db
```
> Il `.env` contiene le chiavi vere: viaggia solo dentro il canale SSH cifrato,
> non finisce mai su GitHub (è gitignored) e non va stampato.

**5c. Avvia il servizio sulla VM:**
```bash
sudo systemctl start tradingbot
journalctl -u tradingbot -f          # segui i log; Ctrl-C per staccarti
```
Cerca nel log: universo dei mover, `cache_write`/`cache_read`, `cycle end` senza
errori, e il seed delle posizioni esistenti dall'account testnet.

**5d. Disattiva l'autostart sul PC** (così non si riaccende al prossimo login e
non ricrea il doppione):
```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\TradingBot.lnk" -ErrorAction SilentlyContinue
```

Da qui in poi il bot vive **solo sulla VM**, 24/7, PC spento o acceso.

---

## 6. Dashboard (via tunnel SSH — nessuna porta pubblica)

Non esponiamo la 8501 su internet. Apri un tunnel dal PC:
```powershell
ssh -i "C:\...\key" -L 8501:localhost:8501 ubuntu@<IP>
```
Lascia la finestra aperta e vai su **http://localhost:8501**
(la password è il valore di `DASHBOARD_PASSWORD` nel `.env` del server — mai committarla).

---

## 7. Comandi utili (sulla VM)

```bash
systemctl status tradingbot         # stato
journalctl -u tradingbot -f         # log in tempo reale
journalctl -u tradingbot -n 200     # ultime 200 righe
sudo systemctl restart tradingbot   # riavvio
sudo systemctl stop tradingbot      # stop (per manutenzione)
```

**Aggiornare il codice** (dal PC): rifai il bundle dello step 3, `scp`, poi sulla VM:
```bash
tar -xzf ~/bot.tar.gz -C ~/progettotrade && rm ~/bot.tar.gz
sudo systemctl restart tradingbot
```
(`.env` e `data/` non sono nel bundle, quindi restano intatti.)

**Kill switch d'emergenza** (dalla VM): crea il file che il bot controlla —
`hard` = spegni tutto, altrimenti = solo blocco nuove aperture (le posizioni
restano protette dal RiskEngine).
```bash
echo hard > ~/progettotrade/KILL_SWITCH        # o: echo soft > ...
```

---

## Note

- **Regione / Binance**: scegli una regione UE (Milano/Francoforte/Amsterdam).
  I dati di mercato vengono dagli endpoint pubblici mainnet; dall'UE sono
  raggiungibili. Se un giorno rispondessero `HTTP 451`, si cambia approccio —
  ma sul testnet non è mai stato un problema.
- **RAM sul micro AMD (1 GB)**: bot + Streamlit + pandas possono essere al limite.
  Se usi quella shape e va in OOM, si può far girare il solo bot senza dashboard
  (la dashboard è opzionale, legge solo il journal). Con l'ARM A1 (6 GB) non è un problema.
- **Costo**: la shape ARM A1 e la micro AMD sono *Always Free* — 0 €. L'unico
  costo che resta è l'API Anthropic del bot (~0,4–0,9 $/giorno con lo scanner).
