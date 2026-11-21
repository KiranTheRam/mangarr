#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${CONTAINER:-docker}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"

# Read the backend base from the Dockerfile so digest updates also apply here.
PYTHON_IMAGE="$(awk '$1 == "FROM" && $2 ~ /^python:/ { print $2; exit }' "${REPO_ROOT}/docker/Dockerfile")"
if [[ -z "${PYTHON_IMAGE}" ]]; then
    echo "Could not find the Python base image in docker/Dockerfile" >&2
    exit 1
fi

TARGET="${REPO_ROOT}/backend/requirements.txt"
WORK_DIR="$(mktemp -d "${REPO_ROOT}/backend/.lock-backend.XXXXXX")"
trap 'rm -r -- "${WORK_DIR}"' EXIT
INPUT_DIR="${WORK_DIR}/input"
TMP_FILE="${WORK_DIR}/requirements.txt"
mkdir "${INPUT_DIR}"
cp "${REPO_ROOT}/backend/pyproject.toml" "${REPO_ROOT}/backend/VERSION" "${INPUT_DIR}/"
if [[ -f "${TARGET}" ]]; then
    cp "${TARGET}" "${INPUT_DIR}/"
fi

if "${CONTAINER}" run --rm \
    -e HOME=/tmp \
    -v "${INPUT_DIR}:/src:ro,z" \
    "${PYTHON_IMAGE}" \
    sh -c '
        set -eu
        # Reuse the existing lock to keep pins that still satisfy the inputs.
        cp -R /src /tmp/backend
        cd /tmp/backend
        pip install --quiet --no-warn-script-location pip-tools==7.6.2 >&2
        python -m piptools compile --quiet --generate-hashes --strip-extras --allow-unsafe --all-build-deps --output-file requirements.txt pyproject.toml
        cat requirements.txt
    ' > "${TMP_FILE}" && [ -s "${TMP_FILE}" ]; then
    chmod 0644 "${TMP_FILE}"
    mv "${TMP_FILE}" "${TARGET}"
    echo "Wrote backend/requirements.txt"
else
    echo "Failed to generate backend/requirements.txt" >&2
    exit 1
fi
