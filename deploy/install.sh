#!/usr/bin/env bash
# Claugine portal installer / updater for Ubuntu 26.04.
# Safe to re-run: existing secrets, database and uploaded files are kept.
#
#   sudo bash deploy/install.sh --site-url https://my.provider.example [--letsencrypt-email you@example.com]
#
# 2026-10-30: the portal VMs get the portal from GitHub only. This script is run by claugine-portal-update
# (deploy/server/bootstrap.sh node | update) from an export of one commit (with its COMMIT file); see
# deploy/server/README.md. Run by hand only for development.
#
# --letsencrypt-email requests a Let's Encrypt certificate (HTTP-01 check:
# public port 80 must reach this VM's HTTP port) and accepts the Let's Encrypt
# subscriber agreement on behalf of that email. Without it, an existing
# Let's Encrypt certificate is used if present, otherwise a self-signed one.
#
# The portal on two sites (accounts/ha.py): give each node its place once, e.g. on dc1
#
#   --site dc1 --server-id 1 --addr 10.81.101.25 --peer-site dc2 --peer-addr 10.82.101.25 \
#   --lb-addrs 10.81.101.24,10.82.101.24 --mail-name service2-dc1.provider.example [--role active|standby]
#   [--status-ports 55587]
#
# --status-ports: the port(s) of the node's role endpoint for HAProxy (GET /active /standby /maintenance /new on --addr),
# a comma list, 80 by default. 2026-10-30: HAProxy runs on the app nodes themselves (deploy/install-lb.sh there), so
# the role endpoint leaves port 80 (e.g. 55587); during a move both (80,55587) while the LB VMs still check 80.
#
# They go into /etc/claugine-portal/node.env (kept on later runs). A second node needs the first node's
# portal.env (the same keys) before its install, then: claugine-manage-root ha_agent replicate <first node>.
# --no-migrate: the database comes from the other node. A standby never runs migrations itself.
# --no-agent-restart: used by the HA agent when it installs a new version.
#
set -euo pipefail

SITE_URL=""
LE_EMAIL=""
HTTP_PORT=""    # the portal's Apache ports: --http-port/--https-port, else node.env, else what this VM runs, else 8080/8443
HTTPS_PORT=""
PREFIX=/opt/claugine-portal
CONF_DIR=/etc/claugine-portal
DATA_DIR=/var/lib/claugine-portal
DB_NAME=claugine_portal
DB_USER=portal
NO_MIGRATE=0
NO_AGENT_RESTART=0
declare -A NODE=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-url) SITE_URL="$2"; shift 2 ;;
    --no-migrate) NO_MIGRATE=1; shift ;;
    --no-agent-restart) NO_AGENT_RESTART=1; shift ;;
    --role) NODE[ROLE]="$2"; shift 2 ;;
    --site) NODE[SITE]="$2"; shift 2 ;;
    --server-id) NODE[SERVER_ID]="$2"; shift 2 ;;
    --addr) NODE[ADDR]="$2"; shift 2 ;;
    --peer-site) NODE[PEER_SITE]="$2"; shift 2 ;;
    --peer-addr) NODE[PEER_ADDR]="$2"; shift 2 ;;
    --lb-addrs) NODE[LB_ADDRS]="$2"; shift 2 ;;
    --mail-name) NODE[MAIL_NAME]="$2"; shift 2 ;;
    --status-ports) NODE[STATUS_PORTS]="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --https-port) HTTPS_PORT="$2"; shift 2 ;;
    --letsencrypt-email) LE_EMAIL="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -n "$SITE_URL" ]] || { echo "--site-url is required, e.g. https://cp.example.com" >&2; exit 2; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
HOST="$(echo "$SITE_URL" | sed -E 's#^https?://##; s#[:/].*$##')"
log() { echo -e "\033[1;32m==>\033[0m $*"; }

log "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q apache2 libapache2-mod-wsgi-py3 mysql-server python3-venv python3-dev \
  build-essential pkg-config default-libmysqlclient-dev gettext openssl rsync openssh-client sshpass 7zip >/dev/null

log "Mail relay (postfix + OpenDKIM, loopback only; relay, account and DKIM key come from Settings → Portal appliance)"
if ! dpkg -s postfix >/dev/null 2>&1; then  # a fresh VM; an existing relay is left as it is
  MAILNAME="$HOST"; [[ "$HOST" =~ ^[0-9.]+$ ]] && MAILNAME="$(hostname -f)"
  echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections
  echo "postfix postfix/mailname string $MAILNAME" | debconf-set-selections
  apt-get install -y -q postfix opendkim opendkim-tools >/dev/null
  postconf -e "inet_interfaces=loopback-only" "myhostname=$MAILNAME" "mydomain=$MAILNAME" "myorigin=$MAILNAME" \
    "mydestination=$MAILNAME, localhost" "smtpd_relay_restrictions=permit_mynetworks, reject" \
    "smtp_tls_CAfile=/etc/ssl/certs/ca-certificates.crt"
  systemctl enable postfix opendkim >/dev/null 2>&1 || true
  systemctl restart postfix || true
fi

log "Application files -> $PREFIX/app"
install -d -m 755 "$PREFIX" "$PREFIX/app" "$PREFIX/static"
rsync -a --delete --exclude '.git' --exclude 'var/' --exclude 'jobs/' --exclude '/COMMIT' --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC/" "$PREFIX/app/" 2>/dev/null || { rm -rf "$PREFIX/app"; cp -a "$SRC" "$PREFIX/app"; rm -rf "$PREFIX/app/var" "$PREFIX/app/jobs" "$PREFIX/app/COMMIT"; }
if [[ -s "$SRC/COMMIT" ]]; then  # the GitHub commit this installation is (claugine-portal-update)
  install -m 644 "$SRC/COMMIT" "$PREFIX/COMMIT"
else
  echo "not from GitHub: $SRC ($(date -u +%Y-%m-%dT%H:%MZ))" > "$PREFIX/COMMIT"; chmod 644 "$PREFIX/COMMIT"
fi
chown -R root:root "$PREFIX/app"
chmod -R u=rwX,go=rX "$PREFIX/app"

log "Python environment"
[[ -x "$PREFIX/venv/bin/python" ]] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q -r "$PREFIX/app/requirements.txt"

log "Data directories"
install -d -m 750 -o www-data -g www-data "$DATA_DIR" "$DATA_DIR/media" "$DATA_DIR/mail"

log "Configuration $CONF_DIR/portal.env"
install -d -m 750 -o root -g www-data "$CONF_DIR"
ENV="$CONF_DIR/portal.env"
touch "$ENV"; chown root:www-data "$ENV"; chmod 640 "$ENV"
setv() {  # setv KEY VALUE [only-if-missing]
  if grep -q "^$1=" "$ENV"; then
    [[ "${3:-}" == "keep" ]] && return 0
    sed -i "s#^$1=.*#$1=$2#" "$ENV"
  else
    echo "$1=$2" >> "$ENV"
  fi
}
getv() { grep "^$1=" "$ENV" | head -1 | cut -d= -f2-; }
setv SECRET_KEY "$(openssl rand -base64 48 | tr -d '\n/+=')" keep
setv DB_PASSWORD "$(openssl rand -base64 24 | tr -d '\n/+=')" keep
setv DB_NAME "$DB_NAME"
setv DB_USER "$DB_USER"
setv DB_HOST localhost
setv SITE_URL "$SITE_URL"
setv ALLOWED_HOSTS "$HOST,localhost,127.0.0.1"
setv STATIC_ROOT "$PREFIX/static"
setv MEDIA_ROOT "$DATA_DIR/media"
setv EMAIL_FILE_PATH "$DATA_DIR/mail"
setv DEFAULT_FROM_EMAIL "portal@$HOST" keep
setv DEBUG 0 keep
setv FIELD_ENCRYPTION_KEY "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')" keep
setv ONE_SUNSTONE_URL "" keep
setv HA_TOKEN "$(openssl rand -base64 36 | tr -d '\n/+=')" keep
setv HA_REPL_PASSWORD "$(openssl rand -base64 24 | tr -d '\n/+=')" keep
if [[ -z "$(getv HA_AGENT_PORT)" ]]; then  # the HA agent's port (both nodes the same): the one it runs on, else 8090
  pid="$(systemctl show -p MainPID --value claugine-ha 2>/dev/null || echo 0)"
  running=""
  if [[ "${pid:-0}" -gt 0 ]]; then
    running="$(ss -Hltnp 2>/dev/null | grep "pid=$pid," | awk '{print $4}' | sed 's/.*://' | head -1 || true)"
  fi
  setv HA_AGENT_PORT "${running:-8090}"
fi
HA_PORT="$(getv HA_AGENT_PORT)"

log "This node: $CONF_DIR/node.env"
NODE_ENV="$CONF_DIR/node.env"
touch "$NODE_ENV"; chown root:root "$NODE_ENV"; chmod 644 "$NODE_ENV"
for k in "${!NODE[@]}"; do
  if grep -q "^$k=" "$NODE_ENV"; then sed -i "s#^$k=.*#$k=${NODE[$k]}#" "$NODE_ENV"; else echo "$k=${NODE[$k]}" >> "$NODE_ENV"; fi
done
nodev() { grep "^$1=" "$NODE_ENV" | head -1 | cut -d= -f2- || true; }
setnode() { if grep -q "^$1=" "$NODE_ENV"; then sed -i "s#^$1=.*#$1=$2#" "$NODE_ENV"; else echo "$1=$2" >> "$NODE_ENV"; fi; }
# The portal's own ports are this installation's: kept in node.env (2026-10-31). A VM that runs the portal already
# keeps the ports it runs on (read from the Apache configuration this script wrote before).
PORTS_CONF=/etc/apache2/ports.conf
if [[ -z "$HTTP_PORT" ]]; then HTTP_PORT="$(nodev HTTP_PORT)"; fi
if [[ -z "$HTTP_PORT" ]] && grep -q "claugine-portal install.sh" "$PORTS_CONF" 2>/dev/null; then
  HTTP_PORT="$(awk '/^Listen [0-9]+$/ {print $2; exit}' "$PORTS_CONF")"
fi
if [[ -z "$HTTPS_PORT" ]]; then HTTPS_PORT="$(nodev HTTPS_PORT)"; fi
if [[ -z "$HTTPS_PORT" ]] && grep -q "claugine-portal install.sh" "$PORTS_CONF" 2>/dev/null; then
  HTTPS_PORT="$(awk '/<IfModule ssl_module>/ {s=1} s && /^[[:space:]]+Listen [0-9]+$/ {print $2; exit}' "$PORTS_CONF")"
fi
HTTP_PORT="${HTTP_PORT:-8080}"; HTTPS_PORT="${HTTPS_PORT:-8443}"
setnode HTTP_PORT "$HTTP_PORT"; setnode HTTPS_PORT "$HTTPS_PORT"
ROLE="$(nodev ROLE)"; ROLE="${ROLE:-active}"
ADDR="$(nodev ADDR)"; PEER_ADDR="$(nodev PEER_ADDR)"; SERVER_ID="$(nodev SERVER_ID)"; LB_ADDRS="$(nodev LB_ADDRS)"
STATUS_PORTS="$(nodev STATUS_PORTS)"; STATUS_PORTS="${STATUS_PORTS:-80}"
if [[ "$ROLE" == "standby" || "$ROLE" == "maintenance" ]]; then NO_MIGRATE=1; fi  # the schema comes by replication
if [[ -n "$ADDR" ]]; then setv ALLOWED_HOSTS "$HOST,localhost,127.0.0.1,$ADDR"; fi
echo "  role $ROLE${ADDR:+, $ADDR}${PEER_ADDR:+, other node $PEER_ADDR}"

log "MySQL database"
DBPW="$(getv DB_PASSWORD)"
systemctl enable --now mysql >/dev/null
if [[ -n "$SERVER_ID" && -n "$ADDR" ]]; then  # two sites: GTID replication between the app nodes
  HA_CNF=/etc/mysql/mysql.conf.d/zz-claugine-ha.cnf
  NEW_CNF="# Managed by claugine-portal install.sh: the portal on two sites (GTID replication between the app nodes)
[mysqld]
server_id = $SERVER_ID
gtid_mode = ON
enforce_gtid_consistency = ON
bind_address = 127.0.0.1,$ADDR
binlog_expire_logs_seconds = 604800
relay_log_recovery = ON"
  if [[ "$(cat "$HA_CNF" 2>/dev/null || true)" != "$NEW_CNF" ]]; then
    printf '%s\n' "$NEW_CNF" > "$HA_CNF"; chmod 644 "$HA_CNF"
    echo "  MySQL replication settings changed: restarting MySQL"
    systemctl restart mysql
  fi
fi
RO="$(mysql --protocol=socket -uroot -N -e 'SELECT @@super_read_only' 2>/dev/null || echo 0)"
if [[ "$RO" == "1" ]]; then mysql --protocol=socket -uroot -e "SET GLOBAL super_read_only=OFF"; fi
# Users belong to this node: kept out of the binary log (never replicated). On a standby the database comes by replication.
mysql --protocol=socket -uroot <<SQL
SET sql_log_bin=0;
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DBPW';
ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$DBPW';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
if [[ -n "$PEER_ADDR" ]]; then
  REPLPW="$(getv HA_REPL_PASSWORD)"
  mysql --protocol=socket -uroot <<SQL
SET sql_log_bin=0;
CREATE USER IF NOT EXISTS 'repl'@'$PEER_ADDR' IDENTIFIED BY '$REPLPW' REQUIRE SSL;
ALTER USER 'repl'@'$PEER_ADDR' IDENTIFIED BY '$REPLPW' REQUIRE SSL;
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'$PEER_ADDR';
FLUSH PRIVILEGES;
SQL
fi
if [[ "$RO" == "1" ]]; then mysql --protocol=socket -uroot -e "SET GLOBAL super_read_only=ON"; fi

log "Management wrapper /usr/local/bin/claugine-manage"
cat > /usr/local/bin/claugine-manage <<EOF
#!/bin/sh
cd $PREFIX/app && exec sudo -u www-data $PREFIX/venv/bin/python manage.py "\$@"
EOF
chmod 755 /usr/local/bin/claugine-manage
cat > /usr/local/bin/claugine-manage-root <<EOF
#!/bin/sh
# As root: the HA agent (ha_agent status | op <name> | replicate <addr> | role <role>)
cd $PREFIX/app && exec $PREFIX/venv/bin/python manage.py "\$@"
EOF
chmod 700 /usr/local/bin/claugine-manage-root

log "Migrations and static files"
if [[ "$NO_MIGRATE" == "0" ]]; then
  claugine-manage migrate --noinput
  claugine-manage load_inventory --once
else
  echo "  migrations skipped: the database comes from the other node"
fi
(cd "$PREFIX/app" && find . -type f \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.po' -o -name '*.sh' -o -name '*.txt' \) \
  -not -path './var/*' -not -path './static/*' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12) > "$PREFIX/release"
echo "  release $(cat "$PREFIX/release"), commit $(cut -c1-12 "$PREFIX/COMMIT")"
"$PREFIX/venv/bin/python" "$PREFIX/app/manage.py" collectstatic --noinput -v0
chmod -R u=rwX,go=rX "$PREFIX/static"
if [[ -d "$PREFIX/app/locale" ]]; then
  (cd "$PREFIX/app" && "$PREFIX/venv/bin/python" manage.py compilemessages -v0) || true
  chmod -R u=rwX,go=rX "$PREFIX/app/locale"
fi

log "TLS certificate (self-signed, SAN $HOST)"
TLS="$CONF_DIR/tls"
install -d -m 750 -o root -g root "$TLS"
if [[ ! -s "$TLS/portal.crt" ]] || ! openssl x509 -in "$TLS/portal.crt" -noout -text | grep -q "$HOST"; then
  if [[ "$HOST" =~ ^[0-9.]+$ ]]; then SAN="IP:$HOST"; else SAN="DNS:$HOST"; fi
  openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=$HOST/O=Claugine" \
    -addext "subjectAltName=$SAN" -keyout "$TLS/portal.key" -out "$TLS/portal.crt" 2>/dev/null
  chmod 600 "$TLS/portal.key"
fi

log "Apache on ports $HTTP_PORT (redirect) and $HTTPS_PORT (TLS)"
a2enmod -q ssl headers rewrite wsgi >/dev/null
STATUS_LISTEN=""
if [[ -n "$ADDR" ]]; then for p in ${STATUS_PORTS//,/ }; do STATUS_LISTEN="${STATUS_LISTEN}Listen $ADDR:$p
"; done; fi
cat > /etc/apache2/ports.conf <<EOF
# Managed by claugine-portal install.sh
Listen $HTTP_PORT
$STATUS_LISTEN
<IfModule ssl_module>
    Listen $HTTPS_PORT
</IfModule>
EOF
ACME_ROOT=/var/www/acme
install -d -m 755 "$ACME_ROOT/.well-known/acme-challenge"
LE_LIVE=/etc/letsencrypt/live/$HOST
write_site() {
  if [[ -s "$LE_LIVE/fullchain.pem" ]]; then
    CERT_FILE="$LE_LIVE/fullchain.pem"; KEY_FILE="$LE_LIVE/privkey.pem"
    HSTS='Header always set Strict-Transport-Security "max-age=31536000"'
  else
    CERT_FILE="$TLS/portal.crt"; KEY_FILE="$TLS/portal.key"; HSTS="# HSTS off: self-signed certificate"
  fi
REMOTEIP="# no HAProxy in front"
if [[ -n "$LB_ADDRS" ]]; then  # the users' addresses in the logs, as HAProxy passes them
  a2enmod -q remoteip >/dev/null
  REMOTEIP="RemoteIPHeader X-Forwarded-For
    RemoteIPInternalProxy ${LB_ADDRS//,/ }"
fi
STATUS_VHOST=""
if [[ -n "$ADDR" ]]; then  # the node's role for HAProxy (GET /active /standby /maintenance /new), nothing else
  for p in ${STATUS_PORTS//,/ }; do
  STATUS_VHOST="$STATUS_VHOST
<VirtualHost $ADDR:$p>
    ServerName $ADDR
    WSGIProcessGroup claugine
    WSGIApplicationGroup %{GLOBAL}
    WSGIScriptAlias / $PREFIX/app/portal/wsgi.py
    <LocationMatch \"^/(?!(active|standby|maintenance|new)\$)\">
        Require all denied
    </LocationMatch>
    <Directory $PREFIX/app/portal>
        <Files wsgi.py>
            Require all granted
        </Files>
    </Directory>
    CustomLog /dev/null combined
</VirtualHost>"
  done
fi
cat > /etc/apache2/sites-available/claugine-portal.conf <<EOF
# Managed by claugine-portal install.sh
WSGIDaemonProcess claugine user=www-data group=www-data processes=2 threads=8 \\
    python-home=$PREFIX/venv python-path=$PREFIX/app lang=C.UTF-8 locale=C.UTF-8
# the version under test at /test-new-version/ (accounts/testmount.py): the same code, a process of its own
WSGIDaemonProcess claugine-test user=www-data group=www-data processes=1 threads=8 \\
    python-home=$PREFIX/venv python-path=$PREFIX/app lang=C.UTF-8 locale=C.UTF-8

$STATUS_VHOST

<VirtualHost *:$HTTP_PORT>
    ServerName $HOST
    Alias /.well-known/acme-challenge/ $ACME_ROOT/.well-known/acme-challenge/
    <Directory $ACME_ROOT>
        Require all granted
    </Directory>
    RewriteEngine On
    RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/
    RewriteRule ^ https://$HOST:$HTTPS_PORT%{REQUEST_URI} [R=301,L]
</VirtualHost>

<VirtualHost *:$HTTPS_PORT>
    ServerName $HOST
    SSLEngine on
    SSLCertificateFile $CERT_FILE
    SSLCertificateKeyFile $KEY_FILE
    SSLProtocol -all +TLSv1.2 +TLSv1.3

    $HSTS
    Header always set Referrer-Policy "same-origin"
    LimitRequestBody 10485760
    $REMOTEIP

    Alias /test-new-version/static/ $PREFIX/static/
    Alias /static/ $PREFIX/static/
    <Directory $PREFIX/static>
        Require all granted
        Options -Indexes
    </Directory>

    WSGIScriptAlias /test-new-version $PREFIX/app/portal/wsgi_test.py process-group=claugine-test application-group=%{GLOBAL}
    WSGIProcessGroup claugine
    WSGIApplicationGroup %{GLOBAL}
    WSGIScriptAlias / $PREFIX/app/portal/wsgi.py
    WSGIPassAuthorization On
    <Directory $PREFIX/app/portal>
        <Files ~ "^wsgi(_test)?\.py$">
            Require all granted
        </Files>
    </Directory>

    ErrorLog \${APACHE_LOG_DIR}/claugine-error.log
    CustomLog \${APACHE_LOG_DIR}/claugine-access.log combined
</VirtualHost>
EOF
}
write_site
a2dissite -q 000-default default-ssl >/dev/null 2>&1 || true
a2ensite -q claugine-portal >/dev/null
apache2ctl configtest
systemctl enable apache2 >/dev/null
systemctl restart apache2

if [[ -n "$LE_EMAIL" && ! -s "$LE_LIVE/fullchain.pem" ]]; then
  log "Let's Encrypt certificate for $HOST"
  apt-get install -y -q certbot >/dev/null
  certbot certonly --webroot -w "$ACME_ROOT" -d "$HOST" --email "$LE_EMAIL" --agree-tos -n \
    --deploy-hook "systemctl reload apache2"
  write_site
  apache2ctl configtest && systemctl reload apache2
fi

log "Done: $SITE_URL"
log "Collector timer (OpenNebula, read-only; the period is in Settings: tenants refresh, minutes; checked every 5 minutes)"
cat > /etc/systemd/system/claugine-collect.service <<UNIT
[Unit]
Description=Claugine portal: collect tenant groups from OpenNebula
[Service]
Type=oneshot
User=www-data
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python manage.py collect_platforms --if-due
UNIT
cat > /etc/systemd/system/claugine-collect.timer <<UNIT
[Unit]
Description=Claugine portal: collect tenant groups when due (Settings), checked every 5 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT
log "Health timer (platform endpoints and network function images, period set in Settings)"
cat > /etc/systemd/system/claugine-health.service <<UNIT
[Unit]
Description=Claugine portal: check the health of the platforms that are due
[Service]
Type=oneshot
User=www-data
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python manage.py check_platforms
UNIT
cat > /etc/systemd/system/claugine-health.timer <<UNIT
[Unit]
Description=Claugine portal: platform health, checked every 5 minutes for platforms that are due
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT
# the old system's daily refresh is gone (2026-10-28: the portal is the only system managing the platforms)
if [[ -e /etc/systemd/system/claugine-oldsync.timer || -e /etc/systemd/system/claugine-oldsync.service ]]; then
  log "Old system refresh timer: removed (the old system is not read any more)"
  systemctl disable --now claugine-oldsync.timer >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/claugine-oldsync.timer /etc/systemd/system/claugine-oldsync.service
fi
if grep -q "^SOURCE_" "$CONF_DIR/portal.env" 2>/dev/null; then  # the old system's connection settings: not used any more
  log "portal.env: the old system's settings removed (a copy in /var/backups/claugine-portal)"
  install -d -m 700 /var/backups/claugine-portal
  install -m 600 "$CONF_DIR/portal.env" "/var/backups/claugine-portal/portal.env.before-no-old-system-$(date -u +%Y%m%d-%H%M)"
  sed -i '/^SOURCE_/d; /^# Old system/d' "$CONF_DIR/portal.env"
fi
log "History cleanup timer (finished tasks and old log entries, nightly at 03:30 Asia/Almaty)"
cat > /etc/systemd/system/claugine-cleanup.service <<UNIT
[Unit]
Description=Claugine portal: remove finished tasks and log entries older than the history settings
[Service]
Type=oneshot
User=www-data
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python manage.py cleanup_history
UNIT
cat > /etc/systemd/system/claugine-cleanup.timer <<UNIT
[Unit]
Description=Claugine portal: history cleanup nightly
[Timer]
OnCalendar=*-*-* 03:30:00 Asia/Almaty
Persistent=true
[Install]
WantedBy=timers.target
UNIT
log "Appliance timer (mail relay in line with Settings, backups when due; as root, every 5 minutes)"
cat > /etc/systemd/system/claugine-appliance.service <<UNIT
[Unit]
Description=Claugine portal: apply the appliance settings and make the backups that are due
[Service]
Type=oneshot
User=root
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python manage.py appliance
UNIT
cat > /etc/systemd/system/claugine-appliance.timer <<UNIT
[Unit]
Description=Claugine portal: appliance settings and backups, every 5 minutes
[Timer]
OnBootSec=4min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now claugine-appliance.timer >/dev/null
systemctl enable --now claugine-cleanup.timer >/dev/null
systemctl enable --now claugine-collect.timer >/dev/null; systemctl restart claugine-collect.timer
systemctl enable --now claugine-health.timer >/dev/null

if [[ -n "$ADDR" ]] && command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  log "Firewall: HAProxy and the other node"
  for lb in ${LB_ADDRS//,/ }; do
    for port in ${STATUS_PORTS//,/ } "$HTTP_PORT" "$HTTPS_PORT"; do ufw allow proto tcp from "$lb" to any port "$port" comment "claugine HAProxy" >/dev/null; done
  done
  if [[ -n "$PEER_ADDR" ]]; then
    for port in 3306 "$HA_PORT" "$HTTPS_PORT"; do ufw allow proto tcp from "$PEER_ADDR" to any port "$port" comment "claugine other node" >/dev/null; done
  fi
fi

if [[ -n "$ADDR" && -n "$(getv HA_TOKEN)" ]]; then
  log "HA agent claugine-ha ($ADDR:$HA_PORT, as root): roles, replication and switching between the nodes"
  cat > /etc/systemd/system/claugine-ha.service <<UNIT
[Unit]
Description=Claugine portal: the HA agent of this node
After=network-online.target mysql.service
Wants=network-online.target
[Service]
User=root
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python manage.py ha_agent serve
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable claugine-ha >/dev/null
  if [[ "$NO_AGENT_RESTART" == "0" ]]; then systemctl restart claugine-ha; fi
fi

if [[ "$ROLE" == "active" ]] && ! claugine-manage shell -c "from accounts.models import User; import sys; sys.exit(0 if User.objects.filter(is_super_admin=True).exists() else 1)" 2>/dev/null; then
  echo
  echo "No super admin yet. Open this one-time link (valid 24 h) to create it:"
  claugine-manage bootstrap_superadmin
fi
