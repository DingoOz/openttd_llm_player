#!/usr/bin/env bash
# One-shot, no-sudo setup: pinned OpenTTD + OpenGFX into third_party/, Python venv in .venv/.
# Re-running is safe; downloads are skipped when the checksum already matches.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TP="$ROOT/third_party"
DL="$TP/downloads"

# Pinned versions. Map generation is only reproducible for a fixed OpenTTD version,
# so bump these deliberately and re-baseline your results when you do.
OTTD_VERSION="15.3"
OTTD_FILE="openttd-${OTTD_VERSION}-linux-generic-amd64.tar.xz"
OTTD_SHA256="f49eb25d61b00f8f4d332fee02b530ad75552d1efb8f2bb01e7ca5e6540fe059"
GFX_VERSION="8.0"
GFX_FILE="opengfx-${GFX_VERSION}-all.zip"
GFX_SHA256="43a0c1dabf39cb865394f3a6cc36d4da5c10ecfaaf55652043104806810903be"

fetch() { # url file sha256
    local url="$1" file="$2" sha="$3"
    if [[ -f "$file" ]] && echo "$sha  $file" | sha256sum -c --status -; then
        echo "  cached   $(basename "$file")"
        return
    fi
    echo "  download $(basename "$file")"
    curl -fsSL --retry 3 -o "$file.part" "$url"
    echo "$sha  $file.part" | sha256sum -c --status - || { echo "checksum mismatch for $url" >&2; exit 1; }
    mv "$file.part" "$file"
}

echo "[1/3] OpenTTD ${OTTD_VERSION} + OpenGFX ${GFX_VERSION}"
mkdir -p "$DL"
fetch "https://cdn.openttd.org/openttd-releases/${OTTD_VERSION}/${OTTD_FILE}" "$DL/$OTTD_FILE" "$OTTD_SHA256"
fetch "https://cdn.openttd.org/opengfx-releases/${GFX_VERSION}/${GFX_FILE}" "$DL/$GFX_FILE" "$GFX_SHA256"

OTTD_DIR="$TP/openttd-${OTTD_VERSION}"
if [[ ! -x "$OTTD_DIR/openttd" ]]; then
    mkdir -p "$OTTD_DIR"
    tar -xf "$DL/$OTTD_FILE" -C "$OTTD_DIR" --strip-components=1
fi
if ! ls "$OTTD_DIR/baseset" | grep -qi '^opengfx'; then
    unzip -q -o "$DL/$GFX_FILE" -d "$OTTD_DIR/baseset/"
fi
ln -sfn "openttd-${OTTD_VERSION}" "$TP/openttd"

missing="$(ldd "$OTTD_DIR/openttd" | grep 'not found' || true)"
if [[ -n "$missing" ]]; then
    echo "OpenTTD is missing shared libraries:" >&2
    echo "$missing" >&2
    exit 1
fi
echo "  binary   $OTTD_DIR/openttd"

echo "[2/3] Python environment"
if ! command -v uv >/dev/null; then
    echo "uv not found; install it from https://docs.astral.sh/uv/ or create .venv yourself and 'pip install -e .'" >&2
    exit 1
fi
cd "$ROOT"
[[ -d .venv ]] || uv venv --quiet .venv
uv pip install --quiet --python .venv/bin/python -e .
echo "  venv     $ROOT/.venv"

echo "[3/3] Sanity check"
.venv/bin/python -m ottd_bench doctor
