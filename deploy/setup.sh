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

echo "==> System packages (best effort; falls back to PyPI if mirrors unreachable)..."
sudo apt-get update -y || echo "!! apt update failed (Ubuntu mirror unreachable) -- continuing"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip \
  || echo "!! apt install failed -- will bootstrap pip from PyPI instead"

echo "==> Creating virtualenv..."
cd "$APP_DIR"
rm -rf .venv
if python3 -m venv .venv 2>/dev/null && ./.venv/bin/python -m pip --version >/dev/null 2>&1; then
  echo "   venv + pip ready via stdlib"
else
  # Oracle Cloud ARM images often can't reach ports.ubuntu.com, so ensurepip
  # is missing. Create the venv without pip and bootstrap pip from PyPI (443).
  echo "   ensurepip unavailable -- venv --without-pip + get-pip.py from PyPI"
  rm -rf .venv
  python3 -m venv .venv --without-pip
  curl -sSf -m 30 https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  ./.venv/bin/python /tmp/get-pip.py
fi

echo "==> Installing requirements..."
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
