#!/usr/bin/env bash
# Simple installer / updater for the Episodes bot.
#   First time:  git clone <your-repo-url> episodes-bot && cd episodes-bot && bash setup.sh
#   Update:      cd episodes-bot && bash setup.sh

main() {
  set -e
  cd "$(dirname "$0")"
  SERVICE=episodes-bot

  if [ -d .git ]; then
    echo "==> Getting latest code"
    git pull || true
  fi

  echo "==> Installing requirements"
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip
  [ -d venv ] || python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt

  if [ ! -f .env ]; then
    read -p "Bot token (from @BotFather): " TOKEN
    read -p "Your Telegram user ID (admin): " ADMIN
    printf "BOT_TOKEN=%s\nADMIN_IDS=%s\n" "$TOKEN" "$ADMIN" > .env
    chmod 600 .env
  fi

  echo "==> Setting up the service"
  sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=Episodes Telegram Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=$PWD
EnvironmentFile=$PWD/.env
ExecStart=$PWD/venv/bin/python $PWD/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable $SERVICE
  sudo systemctl restart $SERVICE
  sleep 2
  sudo systemctl --no-pager status $SERVICE | head -n 8
  echo
  echo "Done. Bot is running. Logs: sudo journalctl -u $SERVICE -f"
}

main "$@"
exit
