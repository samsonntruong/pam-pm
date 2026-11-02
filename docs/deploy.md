# Deploy Pam on a server

Pam is one process with no inbound ports of its own in normal use (Slack
Socket Mode plus Azure DevOps polling), so it runs on any always-on host.
The web console is the only listener, and it must stay up: the scheduler
and the Slack connection stop when the process stops.

Two options are covered here: a small VPS with Docker plus Caddy for
HTTPS, and one managed container host. A hosted multi-tenant version of
Pam is roadmap and is not part of this build.

## Console auth (read first)

- Localhost mode is unchanged: `pam start` prints a one time setup token
  and the wizard URL. No password is needed on 127.0.0.1.
- Network mode needs a console password. Set it one of two ways:
  1. Run `pam start` on the machine, open `/login`, and choose a password.
  2. Set `PAM_CONSOLE_PASSWORD` (at least 8 characters) before first run.
     Pam hashes it with PBKDF2 and stores it in the data directory as
     `console_auth.json`, then ignores the variable on later runs.
- The password is never stored in plain text. Logins set a session cookie
  (`pam_session`, HttpOnly, 7 day expiry). Wrong passwords are rate
  limited to 5 attempts per minute per client.
- `PAM_ALLOWED_HOST` pins the public hostname (for example
  `pam.example.com`). Requests for other host names get a 400 error, except
  `/` and `/health` which stay open for container health probes.
- Pam refuses to bind to a non-loopback address unless a password is
  stored, and prints a plain-English error telling you how to set one.
- This includes any 0.0.0.0 bind including Docker.
- To change the password, log in and re-submit a new one to
  `/api/auth/setup`, or delete `console_auth.json` from the data directory
  and set a fresh password on next start.

## Option A: small VPS with Docker plus Caddy (HTTPS)

Run these on any small VPS (1 vCPU, 1 GB RAM is enough) with a DNS A record
pointing your hostname at the server. Replace `pam.example.com` with your
own hostname everywhere.

1. Install Docker and Caddy on the server:

```sh
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin caddy
sudo systemctl enable --now docker caddy
```

2. Copy the project to the server (repo checkout or file copy), then move
into it:

```sh
cd /opt/pam-pm
```

3. Create the secrets file on the host (never commit it):

```sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` and fill in real values plus the two hosting lines:

```sh
PAM_CONSOLE_PASSWORD=choose-a-long-first-run-password
PAM_ALLOWED_HOST=pam.example.com
```

4. Start Pam with the shipped compose file. It keeps a persistent named
volume for `/data` (SQLite database and data directory) and restarts the
container unless stopped:

```sh
docker compose up -d --build
docker compose logs -f pam
```

The compose file binds the app port to loopback only
(`127.0.0.1:8765:8765`), so the console is not reachable directly from the
network. Caddy below is the only public entry point.

5. Point Caddy at Pam with automatic HTTPS. Write `/etc/caddy/Caddyfile`:

```caddy
pam.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Then reload:

```sh
sudo systemctl reload caddy
```

6. Open `https://pam.example.com/login` and log in with the first-run
password. Work through `/setup` until every Test button is green.

7. Keep the process always on. There is no scale-to-zero on a VPS: the
compose restart policy (`unless-stopped`) plus the Caddy service keep Pam
and HTTPS up across reboots. Check after a reboot with:

```sh
docker compose ps
curl -fsS http://127.0.0.1:8765/health
```

Firewall note: only ports 80 and 443 need to be open to the world. Port
8765 stays on loopback.

Updates:

```sh
cd /opt/pam-pm
git pull
docker compose up -d --build
```

## Option B: managed container host (Fly.io)

Status: untested. This guide was written from the provider docs and was not
run against a paid account. Expect small command differences.

The same rules apply as on the VPS: one always-on container, a persistent
volume mounted at `/data` for the SQLite database and data directory,
scale-to-zero turned off, and secrets in the host secret store.

1. Install the provider CLI and log in, then create the app and volume:

```sh
fly launch --no-deploy
fly volumes create pam_data --size 1 --region fra
```

2. Turn scale-to-zero off so the scheduler and Slack connection stay up.
In `fly.toml`:

```toml
[http_service]
  internal_port = 8765
  force_https = true
  auto_stop_machines = false
  min_machines_running = 1

[[vm]]
  memory = "512mb"
  cpu_kind = "shared"
  cpus = 1

[mounts]
  source = "pam_data"
  destination = "/data"
```

3. Store secrets (never commit them) and deploy:

```sh
fly secrets set META_API_KEY=... META_BASE_URL=... META_MODEL=... \
  SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... \
  ADO_ORG_URL=... ADO_PROJECT=... ADO_PAT=... \
  PAM_CONSOLE_PASSWORD=choose-a-long-first-run-password \
  PAM_ALLOWED_HOST=your-app.fly.dev
fly deploy
```

4. Open `https://your-app.fly.dev/login` and log in. Check health with
`fly status` and the `/health` endpoint.

Backups on the managed host: use the provider volume snapshots where
available, and also copy the data directory off the host on a schedule
(see Backups below). Verify a restore at least once.

## Backups

Everything Pam stores lives in the data directory (`/data` in containers,
`PAM_HOME` elsewhere): `state.db`, `board_<project>.db` files, the provider
key, `console_auth.json`, and wizard progress. Back up the whole directory.

Nightly file backup on the VPS (as root, adjust the target path):

```sh
cat > /usr/local/sbin/pam-backup.sh <<'EOF'
#!/bin/sh
set -eu
SRC=/var/lib/docker/volumes/pam-pm_pam-data/_data
DST=/var/backups/pam
DATE=$(date +%F)
mkdir -p "$DST"
tar -czf "$DST/pam-data-$DATE.tgz" -C "$SRC" .
find "$DST" -name 'pam-data-*.tgz' -mtime +14 -delete
EOF
chmod +x /usr/local/sbin/pam-backup.sh
echo '15 3 * * * root /usr/local/sbin/pam-backup.sh' > /etc/cron.d/pam-backup
```

Hot SQLite copy any time (safe while Pam runs):

```sh
docker compose exec pam sqlite3 /data/state.db ".backup '/data/state-backup.db'"
```

Restore: stop the container, unpack the backup over the volume data, start
again:

```sh
docker compose stop pam
tar -xzf /var/backups/pam/pam-data-YYYY-MM-DD.tgz \
  -C /var/lib/docker/volumes/pam-pm_pam-data/_data
docker compose up -d
```

Keep one backup copy off the server (scp, rsync, or object storage). Test a
restore on a scratch machine before you need it.

## Secrets

- Secrets live in environment variables (the `.env` file on the host, mode
  600) or as files in the data directory on the host. Nothing secret goes
  in git, in logs, or in screenshots.
- The full variable list with empty values is in `.env.example`. Hosting
  adds `PAM_CONSOLE_PASSWORD` (first run only, then hashed in the data
  directory) and `PAM_ALLOWED_HOST` (public hostname).
- If a token leaks, rotate it at the provider first (Meta, Slack, Azure
  DevOps), then update `.env` or the host secret store and restart.
