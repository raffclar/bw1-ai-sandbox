#!/bin/sh
# Discharge Nurse: pristine verification, run by hospital.py in a throwaway bw1-worker container with --network none.
#
# Mounts: /work (rw) holds tree/ (the base tree, prepared on the host from git objects) and head/ (the head tree,
# with tools/ and configure.py overlaid from the base: the case can't change how it is verified); the shared
# toolchain is mounted read-only at /work/tree/build/{tools,compilers}, the game data read-only at /opt/orig.
# Env: WORKER_SETUP (the worker setup from sandbox.droplet.toml, a JSON argv list), VERSION (e.g. BW1W120).
#
# Steps: setup + `ninja baseline` at the base; swap in the head tree, delete our compiled objects (ninja's cl rule has
# no header dependencies), setup again, `ninja all_source`, then decomp-regress.py --refresh --fail-on-regression.
# Writes /work/verify.json; exit 0 only when every step passed.
set -u
cd /work/tree || exit 2
result() { printf '{"baseline": %s, "head_build": %s, "regress": %s}\n' "$1" "$2" "$3" >/work/verify.json; }
setup() {
	python3 -c 'import json, os, subprocess, sys; sys.exit(subprocess.run(json.loads(os.environ["WORKER_SETUP"])).returncode)'
}
commit() {
	git add -A >/dev/null 2>&1
	git -c user.name=discharge -c user.email=discharge@localhost commit -q --no-verify --allow-empty -m "$1"
}

echo "== base: setup"
git init -q . && commit base || { result 1 1 1; exit 1; }
if ! setup; then echo "setup failed at the base"; result 1 1 1; exit 1; fi
echo "== base: ninja baseline"
if ! ninja baseline; then echo "ninja baseline failed at the base"; result 1 1 1; exit 1; fi

echo "== head: swap trees"
find . -mindepth 1 -maxdepth 1 ! -name .git ! -name build ! -name orig -exec rm -rf {} +
cp -a /work/head/. . || { result 0 1 1; exit 1; }
find build -path '*/src/*' -name '*.o' -delete
commit head
echo "== head: setup"
if ! setup; then echo "setup failed at the head"; result 0 1 1; exit 1; fi
echo "== head: ninja all_source"
if ! ninja all_source; then echo "ninja all_source failed at the head"; result 0 1 1; exit 1; fi
echo "== head: decomp-regress.py --refresh --fail-on-regression"
python3 tools/decomp-regress.py --refresh --fail-on-regression
status=$?
result 0 0 "$status"
exit "$status"
