#!/bin/sh
# Discharge Nurse: pristine verification, run by hospital.py in a throwaway bw1-worker container with --network none
# and capped CPUs and memory (DISCHARGE_CPUS / DISCHARGE_MEMORY on the host).
#
# Mounts: /work (rw) holds tree/ (the base tree, prepared on the host from git objects), head/ (the head tree) and,
# when the case's start must be measured, start/ (the start tree); head/ and start/ have tools/ and configure.py
# overlaid from the base, so the case can't change how it is verified (a start that predates the base, in a merge
# case, keeps its own, since the base's would list sources it lacks). The shared toolchain is mounted read-only at
# /work/tree/build/{tools,compilers}, the game data read-only at /work/tree/orig
# (a real mount, not a symlink, so the project's orig/<VERSION>/../../build paths stay in the tree).
# Env: WORKER_SETUP (the worker setup from sandbox.droplet.toml, a JSON argv list), VERSION (e.g. BW1W120).
#
# Steps: setup + `ninja baseline` at the base (-> /work/base-report.json); if /work/start exists, the start tree:
# objects deleted (ninja's cl rule has no header dependencies), setup, `ninja all_source`, decomp-regress.py --refresh
# (-> /work/start-report.json); then the head the same way, with decomp-regress.py --refresh --fail-on-regression
# (-> /work/head-report.json). Writes /work/verify.json; exit 0 only when the base and head steps passed (a start
# that fails to build is recorded as "start": 1 and leaves no start report; the host then can't measure progress).
set -u
cd /work/tree || exit 2
START=2  # 2 = not measured in this run
result() {
	printf '{"baseline": %s, "start": %s, "head_build": %s, "regress": %s}\n' "$1" "$START" "$2" "$3" >/work/verify.json
}
setup() {
	python3 -c 'import json, os, subprocess, sys; sys.exit(subprocess.run(json.loads(os.environ["WORKER_SETUP"])).returncode)'
}
commit() {
	git add -A >/dev/null 2>&1
	git -c user.name=discharge -c user.email=discharge@localhost commit -q --no-verify --allow-empty -m "$1"
}
swap_to() {
	find . -mindepth 1 -maxdepth 1 ! -name .git ! -name build ! -name orig -exec rm -rf {} +
	cp -a "$1/." . || return 1
	find build -path '*/src/*' -name '*.o' -delete
	commit "$2"
}
REPORT="build/$VERSION/report.json"

echo "== base: setup"
git init -q . && commit base || { result 1 1 1; exit 1; }
if ! setup; then echo "setup failed at the base"; result 1 1 1; exit 1; fi
echo "== base: ninja baseline"
if ! ninja baseline; then echo "ninja baseline failed at the base"; result 1 1 1; exit 1; fi
cp "build/$VERSION/baseline.json" /work/base-report.json

if [ -d /work/start ]; then
	echo "== start: swap trees"
	START=1
	if swap_to /work/start start && echo "== start: setup" && setup && echo "== start: ninja all_source" &&
		ninja all_source && echo "== start: decomp-regress.py --refresh" &&
		python3 tools/decomp-regress.py --refresh >/dev/null; then
		cp "$REPORT" /work/start-report.json && START=0
	else
		echo "the start tree did not build; its progress can't be measured"
	fi
fi

echo "== head: swap trees"
swap_to /work/head head || { result 0 1 1; exit 1; }
echo "== head: setup"
if ! setup; then echo "setup failed at the head"; result 0 1 1; exit 1; fi
echo "== head: ninja all_source"
if ! ninja all_source; then echo "ninja all_source failed at the head"; result 0 1 1; exit 1; fi
echo "== head: decomp-regress.py --refresh --fail-on-regression"
python3 tools/decomp-regress.py --refresh --fail-on-regression
status=$?
cp "$REPORT" /work/head-report.json
result 0 0 "$status"
exit "$status"
