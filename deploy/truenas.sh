#!/bin/bash
# Runs ON TrueNAS (copied over and started by scripts\deploy-truenas.cmd).
#   bash truenas.sh setup   create datasets, clone the repo, install the app
#   bash truenas.sh update  pull the latest code and rebuild/restart the app
#   bash truenas.sh https   install the HTTPS proxy for access from the internet
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
import json, os, sys
root, mode, pw_file = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(f"{root}/src/repo/deploy/truenas-compose.yaml").read().replace("/mnt/testpool/printpantry", root)
# The password lives only on the NAS (src/.pp_password), never in the repo.
password = open(pw_file).read().strip()
text = text.replace('PP_PASSWORD: ""', "PP_PASSWORD: " + json.dumps(password))
# The P1S's address, serial and LAN access code live only on the NAS too
# (src/.pp_p1s: HOST=..., SERIAL=..., ACCESS_CODE=... one per line).
p1s = {}
if os.path.exists(f"{root}/src/.pp_p1s"):
    for line in open(f"{root}/src/.pp_p1s"):
        k, _, v = line.strip().partition("=")
        if k and v:
            p1s[k.strip().upper()] = v.strip()
for key in ("HOST", "SERIAL", "ACCESS_CODE"):
    text = text.replace(f'PP_P1S_{key}: ""', f"PP_P1S_{key}: " + json.dumps(p1s.get(key, "")))
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

# Every deploy rebuilds the two images; the old ones lose their tag and would
# pile up (about 1 GB per slicer build). Remember the image ids in use before a
# rebuild, and after it delete the ones no longer tagged. Only images this
# script recorded are ever deleted, never other apps' images.
IMG_LEDGER="$ROOT/src/.pp_old_images"

remember_images() {
  midclt call app.image.query | python3 -c '
import json, sys
for i in json.load(sys.stdin):
    if set(i.get("repo_tags") or []) & {"printpantry:local", "printpantry-slicer:local"}:
        print(i["id"])' >> "$IMG_LEDGER" 2>/dev/null || true
}

prune_images() {
  [ -s "$IMG_LEDGER" ] || return 0
  midclt call app.image.query > "$IMG_LEDGER.query" 2>/dev/null || return 0
  python3 - "$IMG_LEDGER" <<'PY'
import json, subprocess, sys
ledger = sys.argv[1]
images = {i["id"]: i for i in json.load(open(ledger + ".query"))}
keep, freed, removed = [], 0, 0
for img in dict.fromkeys(l.strip() for l in open(ledger) if l.strip()):
    info = images.get(img)
    if info is None:
        continue  # already gone
    if info.get("repo_tags"):
        keep.append(img)  # still the current build
        continue
    if subprocess.run(["midclt", "call", "app.image.delete", img], capture_output=True).returncode == 0:
        removed += 1
        freed += info.get("size", 0)
    else:
        keep.append(img)  # in use or busy; try next deploy
open(ledger, "w").write("".join(k + "\n" for k in keep))
if removed:
    print(f"Removed {removed} old Print Pantry image(s) (up to {freed / 1e9:.1f} GB, less what they shared).")
PY
  rm -f "$IMG_LEDGER.query"
}

# -- HTTPS front door for the internet (app "printpantry-https") --------------
HTTPS_APP=printpantry-https
CERT_DIR="${PP_CERT_DIR:-/mnt/testpool/sparkfall-patches-config}"

https_json() {
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__CERT_DIR__|$CERT_DIR|g" -e "s|__UID__|$ME_UID|g" \
    "$ROOT/src/repo/deploy/https/compose.yaml" | python3 -c '
import json, sys
text = sys.stdin.read()
if sys.argv[1] == "create":
    print(json.dumps({"app_name": "printpantry-https", "custom_app": True, "custom_compose_config_string": text}))
else:
    print(json.dumps({"custom_compose_config_string": text}))' "$1"
}

ensure_https() {
  # Installs or refreshes the front door on port 8484 (http on the LAN, https from outside).
  if [ ! -d "$ROOT/https" ]; then
    # The pool root belongs to root, so create the folder through the middleware.
    quiet midclt call filesystem.mkdir "{\"path\":\"$ROOT/https\"}"
    quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/https\",\"uid\":$ME_UID,\"gid\":$ME_UID,\"mode\":\"755\"}"
  fi
  install -m 644 "$ROOT/src/repo/deploy/https/nginx.conf" "$ROOT/https/nginx.conf"
  if midclt call app.query "[[\"name\",\"=\",\"$HTTPS_APP\"]]" | grep -q '"name"'; then
    quiet midclt call -j app.update "$HTTPS_APP" "$(https_json update)"
    quiet midclt call -j app.redeploy "$HTTPS_APP"
  else
    quiet midclt call -j app.create "$(https_json create)"
  fi
  echo "Front door: http://$(hostname -I | awk '{print $1}'):8484 and https://animalfarm.duckdns.org:8484/"
}

wait_healthy() {
  for _ in $(seq 1 60); do
    if python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8483/healthz',timeout=3)" 2>/dev/null; then
      echo "Print Pantry is up: http://$(hostname -I | awk '{print $1}'):8484"
      return 0
    fi
    sleep 3
  done
  echo "App did not answer on port 8483. Check Apps > printpantry > Logs in the TrueNAS UI."
  return 1
}

# The app (uid/gid 568, apps) moves and renames files in the library, and
# library sync writes as this user. Keep everything owned by this user, group
# apps, group-writable, so both can. Files synced in from Windows otherwise
# land with this user's own group and the app can't move them.
fix_library_perms() {
  quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/library\",\"uid\":$ME_UID,\"gid\":568,\"mode\":\"775\",\"options\":{\"recursive\":true}}" \
    || echo "Could not fix library permissions; moving models in the app may fail." >&2
}

case "${1:-update}" in
  setup)
    ensure_datasets
    quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/src\",\"uid\":$ME_UID,\"gid\":$ME_UID,\"mode\":\"755\"}"
    fix_library_perms
    quiet midclt call -j filesystem.setperm "{\"path\":\"$ROOT/data\",\"uid\":568,\"gid\":568,\"mode\":\"750\"}"
    checkout
    if midclt call app.query "[[\"name\",\"=\",\"$APP\"]]" | grep -q '"name"'; then
      echo "App already installed; updating instead."
      remember_images
      quiet midclt call -j app.update "$APP" "$(compose_json update)"
    else
      echo "Installing app (first build takes a minute)..."
      quiet midclt call -j app.create "$(compose_json create)"
    fi
    wait_healthy
    ensure_https
    prune_images
    ;;
  update)
    ensure_datasets
    fix_library_perms
    checkout
    remember_images
    echo "Rebuilding and restarting (the first build with the slicer downloads OrcaSlicer and takes a few minutes)..."
    quiet midclt call -j app.update "$APP" "$(compose_json update)"
    quiet midclt call -j app.redeploy "$APP"
    wait_healthy
    ensure_https
    prune_images
    ;;
  https)
    # Install/refresh the HTTPS proxy for access from the internet.
    checkout
    ensure_https
    ;;
  password)
    # show the password, or set a new one:  truenas.sh password NEW-PASSWORD
    if [ -n "${2:-}" ]; then
      (umask 077; printf '%s\n' "$2" > "$PW_FILE")
      echo "Password changed; restarting the app..."
      remember_images
      quiet midclt call -j app.update "$APP" "$(compose_json update)"
      wait_healthy
      prune_images
    else
      ensure_password
    fi
    echo "Login password: $(cat "$PW_FILE")"
    ;;
  prune)
    # Delete old Print Pantry images now (also runs after every deploy).
    prune_images
    ;;
  *)
    echo "usage: truenas.sh setup|update|https|password [new]|prune"; exit 2 ;;
esac
