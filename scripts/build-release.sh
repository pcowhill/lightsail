#!/usr/bin/env bash
# Build the lightsail-demo release artifact for one exact commit.
#
#   scripts/build-release.sh [--sha SHA] [--out DIR]
#
# Produces DIR/lightsail-demo-<sha>.tar.gz and DIR/lightsail-demo-<sha>.tar.gz.sha256
# (default DIR: dist/). The archive contains only the runtime allowlist:
#
#   main.py  requirements.txt  REVISION  lightsail_demo/**/*.py  public/**
#
# Nothing else: no tests, .git, .github, scripts, virtual environments, caches
# or local files. Members are regular files (0644) and directories (0755)
# owned by 0:0, sorted, with the commit's timestamp, so the same commit gives
# the same bytes. scripts/inspect_release.py then verifies every member.
#
# The checkout must be clean and at SHA (default: HEAD): the artifact is the
# exact verified commit, never a working tree with edits.
set -euo pipefail

usage() { echo "usage: $0 [--sha SHA] [--out DIR] [--allow-dirty]" >&2; exit 64; }

sha=""
out="dist"
allow_dirty=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) sha="${2:-}"; shift 2 ;;
    --out) out="${2:-}"; shift 2 ;;
    --allow-dirty) allow_dirty=1; shift ;;   # local experiments only; CI never passes this
    *) usage ;;
  esac
done

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "${repo}"

head="$(git rev-parse --verify HEAD)"
sha="${sha:-${head}}"
sha="$(printf '%s' "${sha}" | tr '[:upper:]' '[:lower:]')"
[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || { echo "error: --sha must be a full commit SHA" >&2; exit 1; }
[[ "${sha}" == "${head}" ]] || { echo "error: checkout is at ${head}, not ${sha}" >&2; exit 1; }
if [[ "${allow_dirty}" -ne 1 && -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "error: working tree has uncommitted changes; the artifact must be built from the exact commit" >&2
  exit 1
fi

commit_time="$(git show -s --format=%ct "${sha}")"
stage="$(mktemp -d)"
trap 'rm -rf "${stage}"' EXIT

# Copy the allowlisted, *tracked* files only (git ls-files), never the working
# tree wholesale, then the generated REVISION.
git ls-files -z -- main.py requirements.txt 'lightsail_demo/**/*.py' 'lightsail_demo/*.py' 'public/**' \
  | while IFS= read -r -d '' file; do
      case "${file}" in
        */__pycache__/*|*.pyc|.*|*/.*) echo "error: refusing hidden or cached file ${file}" >&2; exit 1 ;;
      esac
      if [[ -L "${file}" ]]; then echo "error: refusing symbolic link ${file}" >&2; exit 1; fi
      mkdir -p "${stage}/$(dirname "${file}")"
      cp --no-dereference --preserve=timestamps "${file}" "${stage}/${file}"
    done
printf '%s\n' "${sha}" > "${stage}/REVISION"

for required in main.py requirements.txt REVISION lightsail_demo/__init__.py public/index.html \
                public/chat/index.html public/draw/index.html public/game/index.html; do
  [[ -f "${stage}/${required}" ]] || { echo "error: ${required} missing from the release" >&2; exit 1; }
done
grep -q -- '--hash=sha256:' "${stage}/requirements.txt" || { echo "error: requirements.txt is not hash-pinned" >&2; exit 1; }

# Normalize modes: every file 0644, every directory 0755.
find "${stage}" -type f -exec chmod 0644 {} +
find "${stage}" -type d -exec chmod 0755 {} +

mkdir -p "${out}"
name="lightsail-demo-${sha}.tar.gz"
archive="${out}/${name}"
rm -f "${archive}" "${archive}.sha256"

# ustar, numeric 0:0 ownership, fixed mtime and sorted names for reproducibility.
tar --format=ustar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@${commit_time}" --mode='u=rwX,go=rX' \
    -C "${stage}" -cf - main.py requirements.txt REVISION lightsail_demo public \
  | gzip -n -9 > "${archive}"

( cd "${out}" && sha256sum "${name}" > "${name}.sha256" )

echo "built ${archive} ($(stat -c %s "${archive}") bytes)"
cat "${archive}.sha256"
