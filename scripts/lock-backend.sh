#!/usr/bin/env bash
set -euo pipefail

# Must match the runtime base image in docker/Dockerfile.
PYTHON_IMAGE="python:3.12-slim"
CONTAINER="${CONTAINER:-docker}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"

TARGET="${REPO_ROOT}/backend/requirements.txt"
TMP_FILE="$(mktemp "${REPO_ROOT}/backend/.requirements.txt.XXXXXX")"
trap 'rm -f "${TMP_FILE}"' EXIT

if "${CONTAINER}" run --rm \
    -e HOME=/tmp \
    -v "${REPO_ROOT}/backend:/src:ro,z" \
    "${PYTHON_IMAGE}" \
    sh -c '
        set -eu
        # Copying /src includes any existing requirements.txt so pip-compile keeps current pins unless dependencies changed.
        cp -a /src /tmp/backend
        cd /tmp/backend
        pip install --quiet --no-warn-script-location pip-tools==7.6.2 >&2
        python -m piptools compile --quiet --generate-hashes --strip-extras --allow-unsafe --output-file requirements.txt pyproject.toml
        cat requirements.txt
    ' > "${TMP_FILE}" && [ -s "${TMP_FILE}" ]; then
    chmod 0644 "${TMP_FILE}"
    mv "${TMP_FILE}" "${TARGET}"
    echo "Wrote backend/requirements.txt"
else
    echo "Failed to generate backend/requirements.txt" >&2
    rm -f "${TMP_FILE}"
    exit 1
fi
