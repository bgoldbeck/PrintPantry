#!/bin/bash
# Runs ON TrueNAS (copied over and started by scripts\deploy-truenas.cmd).
#   bash truenas.sh setup   create datasets, clone the repo, install the app
#   bash truenas.sh update  pull the latest code and rebuild/restart the app
# Uses TrueNAS's middleware (midclt), which works for the admin user without sudo.
set -euo pipefail
ROOT="${PP_NAS_ROOT:-/mnt/testpool/printpantry}"
POOL_DS="${ROOT#/mnt/}"
APP=printpantry
REPO_URL=https://github.com/bgoldbeck/PrintPantry.git
ME_UID=$(id -u)

quiet() { "$@" >/dev/null 2>&1; }

checkout() {
  if [ -d "$ROOT/src/repo/.git" ]; then
    git -C "$ROOT/src/repo" pull -q --ff-only
  else
    git clone -q "$REPO_URL" "$ROOT/src/repo"
  fi
  echo "Code: $(git -C "$ROOT/src/repo" log --oneline -1)"
}

PW_FILE="$ROOT/src/.pp_password"

ensure_password() {
  if [ ! -s "$PW_FILE" ]; then
    (umask 077; python3 -c "import secrets,string;a=string.ascii_lowercase+string.digits;print('-'.join(''.join(secrets.choice(a) for _ in range(4)) for _ in range(3)))" > "$PW_FILE")
    echo "Generated a login password (see: deploy-truenas.cmd password)." >&2
  fi
}

compose_json() {
  ensure_password
  python3 - "$ROOT" "$1" "$PW_FILE" <<'PY'
import json, sys
root, mode, pw_file = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(f"{root}/src/repo/deploy/truenas-compose.yaml").read().replace("/mnt/testpool/printpantry", root)
# The password lives only on the NAS (src/.pp_password), never in the repo.
password = open(pw_file).read().strip()
text = text.replace('PP_PASSWORD: ""', "PP_PASSWORD: " + json.dumps(password))
if mode == "create":
    print(json.dumps({"app_name": "printpantry", "custom_app": True, "custom_compose_config_string": text}))
else:
    print(json.dumps({"custom_compose_config_string": text}))
PY
}

ensure_datasets() {
  for ds in "" /library /data /src /config; do
    if ! midclt call pool.dataset.query "[[\"id\",\"=\",\"$POOL_DS$ds\"]]" | grep -q '"id"'; then
      midclt call pool.dataset.create "{\"name\":\"$POOL_DS$ds\",\"share_type\":\"GENERIC\"}" >/dev/null
      echo "Created dataset $POOL_DS$ds"
      # Slicer presets (scripts\sync-config.cmd): yours to write, the apps user reads.
      if [ "$ds" = /config ]; then
        quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/config\",\"uid\":$ME_UID,\"gid\":568,\"mode\":\"775\"}"
      fi
    fi
  done
}

wait_healthy() {
  for _ in $(seq 1 60); do
    if python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8484/healthz',timeout=3)" 2>/dev/null; then
      echo "Print Pantry is up: http://$(hostname -I | awk '{print $1}'):8484"
      return 0
    fi
    sleep 3
  done
  echo "App did not answer on port 8484. Check Apps > printpantry > Logs in the TrueNAS UI."
  return 1
}

case "${1:-update}" in
  setup)
    ensure_datasets
    quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/src\",\"uid\":$ME_UID,\"gid\":$ME_UID,\"mode\":\"755\"}"
    quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/library\",\"uid\":$ME_UID,\"gid\":568,\"mode\":\"775\"}"
    quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/data\",\"uid\":568,\"gid\":568,\"mode\":\"750\"}"
    checkout
    if midclt call app.query "[[\"name\",\"=\",\"$APP\"]]" | grep -q '"name"'; then
      echo "App already installed; updating instead."
      quiet midclt call -j app.update "$APP" "$(compose_json update)"
    else
      echo "Installing app (first build takes a minute)..."
      quiet midclt call -j app.create "$(compose_json create)"
    fi
    wait_healthy
    ;;
  update)
    ensure_datasets
    checkout
    echo "Rebuilding and restarting (the first build with the slicer downloads OrcaSlicer and takes a few minutes)..."
    quiet midclt call -j app.update "$APP" "$(compose_json update)"
    quiet midclt call -j app.redeploy "$APP"
    wait_healthy
    ;;
  password)
    # show the password, or set a new one:  truenas.sh password NEW-PASSWORD
    if [ -n "${2:-}" ]; then
      (umask 077; printf '%s\n' "$2" > "$PW_FILE")
      echo "Password changed; restarting the app..."
      quiet midclt call -j app.update "$APP" "$(compose_json update)"
      wait_healthy
    else
      ensure_password
    fi
    echo "Login password: $(cat "$PW_FILE")"
    ;;
  *)
    echo "usage: truenas.sh setup|update|password [new]"; exit 2 ;;
esac
