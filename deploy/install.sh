#!/usr/bin/env bash
set -euo pipefail
DEST=/opt/rf-monitor
U=${SUDO_USER:-orangepi}
apt-get update && apt-get install -y python3-venv rsync
mkdir -p "$DEST" && rsync -a --exclude venv --exclude data --exclude __pycache__ ./ "$DEST"/
python3 -m venv "$DEST/venv" && "$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"
chown -R "$U":"$U" "$DEST" && usermod -aG dialout "$U"
timedatectl set-ntp true || true
install -m 644 "$DEST/deploy/rf-monitor.service" /etc/systemd/system/rf-monitor.service
sed -i "s/^User=.*/User=$U/" /etc/systemd/system/rf-monitor.service
systemctl daemon-reload && systemctl enable --now rf-monitor
echo "Готово: http://$(hostname -I | awk '{print $1}'):8000"
