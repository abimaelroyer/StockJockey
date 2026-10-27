#!/usr/bin/env bash
# Serve the trader panel from the droplet. Safe to re-run.
#
#   ssh root@<ip> 'bash -s' < deploy/setup_droplet.sh <ip>
#
# What it sets up:
#   * nginx serves the built panel from /var/www/stockjockey and proxies
#     /api/* to the Flask API, adding the X-API-Key header itself so the key
#     never ships to the browser.
#   * Everything is behind a username/password (HTTP basic auth) except the
#     TradingView webhook, which checks its own secret.
#   * HTTPS from Let's Encrypt for <ip-with-dashes>.sslip.io, a free hostname
#     that resolves to the droplet's IP. Certificates renew automatically.
#   * The API listens on 127.0.0.1 only, so nginx is the only way in.
#
# Add or change the panel login afterwards with:
#   htpasswd /etc/nginx/stockjockey.htpasswd <username>
set -euo pipefail

PUBLIC_IP="${1:?usage: setup_droplet.sh <public-ip>}"
DOMAIN="${PUBLIC_IP//./-}.sslip.io"
APP_DIR=/opt/stockjockey
WEB_ROOT=/var/www/stockjockey
HTPASSWD=/etc/nginx/stockjockey.htpasswd
KEY_CONF=/etc/nginx/stockjockey-apikey.conf
SITE=/etc/nginx/sites-available/stockjockey

echo "==> Installing nginx and certbot"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx certbot python3-certbot-nginx apache2-utils

echo "==> Writing API key include (root-only)"
KEY=$(grep -E '^PANEL_API_KEY=' "$APP_DIR/.env" | cut -d= -f2- | tr -d "\"'\r")
if [[ ! "$KEY" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    echo "PANEL_API_KEY in $APP_DIR/.env is missing or contains characters other than letters, digits and ._~-" >&2
    exit 1
fi
(umask 077; printf 'proxy_set_header X-API-Key "%s";\n' "$KEY" > "$KEY_CONF")

install -d -m 755 "$WEB_ROOT"
[ -f "$WEB_ROOT/index.html" ] || echo '<p>Panel not deployed yet: run <code>npm run deploy</code> in trader-panel.</p>' > "$WEB_ROOT/index.html"
touch "$HTPASSWD"
chown root:www-data "$HTPASSWD"
chmod 640 "$HTPASSWD"

echo "==> Writing nginx site for $DOMAIN"
cat > "$SITE" <<'NGINX'
# Requests by bare IP (or any other name) go to the real hostname.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 301 http://__DOMAIN__$request_uri;
}

server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN__;

    root __WEB_ROOT__;
    index index.html;

    auth_basic "StockJockey";
    auth_basic_user_file __HTPASSWD__;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:5000/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        include __KEY_CONF__;
    }

    # TradingView can't log in; the endpoint checks TRADINGVIEW_WEBHOOK_SECRET.
    location = /api/webhook/tradingview {
        auth_basic off;
        proxy_pass http://127.0.0.1:5000/webhook/tradingview;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
NGINX
sed -i -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__WEB_ROOT__|$WEB_ROOT|g" \
       -e "s|__HTPASSWD__|$HTPASSWD|g" -e "s|__KEY_CONF__|$KEY_CONF|g" "$SITE"
ln -sf "$SITE" /etc/nginx/sites-enabled/stockjockey
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo "==> Getting HTTPS certificate"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect --keep-until-expiring

echo "==> Binding the API to 127.0.0.1"
install -d /etc/systemd/system/stockjockey-api.service.d
cat > /etc/systemd/system/stockjockey-api.service.d/local-only.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=/opt/stockjockey/venv/bin/python -u -c "from api import app; app.run(host='127.0.0.1', port=5000, use_reloader=False)"
UNIT
systemctl daemon-reload
systemctl restart stockjockey-api

echo
echo "Panel URL: https://$DOMAIN"
if [ ! -s "$HTPASSWD" ]; then
    echo "No panel login yet. Create one with:  htpasswd $HTPASSWD <username>"
fi
