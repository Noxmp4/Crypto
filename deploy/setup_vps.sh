#!/bin/bash
# Préparation d'un VPS Ubuntu 24.04 neuf pour daybot. À lancer en root, une fois :
#   curl -fsSLO https://raw.githubusercontent.com/Noxmp4/crypto/main/deploy/setup_vps.sh
#   less setup_vps.sh        # relis-le avant de l'exécuter
#   bash setup_vps.sh
# Relançable sans risque. Il ne démarre PAS le bot : c'est toi qui le fais à la fin.
set -euo pipefail

REPO="${DAYBOT_REPO:-https://github.com/Noxmp4/crypto.git}"
APP_USER="daybot"
APP_DIR="/opt/daybot"

[ "$(id -u)" -eq 0 ] || { echo "À lancer en root (sudo bash setup_vps.sh)." >&2; exit 1; }
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || echo "⚠ Testé pour Ubuntu 24.04 (détecté : ${PRETTY_NAME:-inconnu})."

echo "→ Mises à jour système et paquets de base"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git ufw unattended-upgrades fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades
timedatectl set-ntp true || true

echo "→ Docker (dépôt officiel)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "→ Utilisateur $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
fi
usermod -aG docker "$APP_USER"
if [ -s /root/.ssh/authorized_keys ] && [ ! -s "/home/$APP_USER/.ssh/authorized_keys" ]; then
  install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
  install -m 600 -o "$APP_USER" -g "$APP_USER" /root/.ssh/authorized_keys "/home/$APP_USER/.ssh/authorized_keys"
fi

echo "→ SSH par clé uniquement"
if [ -s "/home/$APP_USER/.ssh/authorized_keys" ]; then
  cat > /etc/ssh/sshd_config.d/10-daybot.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
  systemctl reload ssh || systemctl reload sshd || true
else
  echo "  ⚠ Aucune clé SSH trouvée : connexion par mot de passe laissée active."
  echo "    Ajoute ta clé (ssh-copy-id) puis relance ce script."
fi

echo "→ Pare-feu : SSH seulement en entrée (le site passe par le tunnel Cloudflare)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "→ Code dans $APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p data secrets
[ -f .env ] || cp .env.example .env
[ -f secrets/cdp_api_key.json ] || echo '{}' > secrets/cdp_api_key.json
[ -f secrets/anthropic_api_key ] || : > secrets/anthropic_api_key
# Les conteneurs tournent avec l'UID 10001 : il doit pouvoir écrire data/ et lire secrets/.
chown -R 10001:10001 data secrets
chmod 700 secrets
chmod 400 secrets/*
chmod 600 .env
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chown -R 10001:10001 data secrets

cat <<MSG

✅ VPS prêt. Étapes suivantes (détaillées dans $APP_DIR/DEPLOY.md) :
  1. Cloudflare : créer le tunnel et l'application Access, puis renseigner
       $APP_DIR/.env          (TUNNEL_TOKEN)
       $APP_DIR/config.toml   ([web] cf_team_domain et cf_aud)
  2. Clé Anthropic (facultatif) :  $APP_DIR/secrets/anthropic_api_key
  3. Démarrer :  cd $APP_DIR && sudo -u $APP_USER docker compose up -d --build
MSG
