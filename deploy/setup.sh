#!/usr/bin/env bash
# Bootstrap the trading bot on a fresh Ubuntu box (Oracle Cloud ARM or x86).
#
# Idempotent: safe to re-run after you scp updated code. It does NOT touch
# .env or data/ -- secrets and the journal are transferred separately
# (see README-oracle.md). Run it from anywhere:  bash deploy/setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="tradingbot"
RUN_USER="$(id -un)"

echo "==> App dir : $APP_DIR"
echo "==> Service : $SERVICE_NAME  (user: $RUN_USER)"

echo "==> Installing system packages (python3, venv, pip)..."
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip

echo "==> Creating virtualenv + installing requirements..."
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip wheel
./.venv/bin/pip install -r requirements.txt

echo "==> Installing systemd unit..."
TMP_UNIT="$(mktemp)"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/deploy/tradingbot.service" > "$TMP_UNIT"
sudo cp "$TMP_UNIT" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$TMP_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo
if [ ! -f "$APP_DIR/.env" ]; then
  echo "!!  .env is MISSING -- copy it over before starting (README-oracle.md, step 5)."
else
  echo "==> .env found."
fi
echo
echo "Done. Start it with:"
echo "    sudo systemctl start $SERVICE_NAME && journalctl -u $SERVICE_NAME -f"
