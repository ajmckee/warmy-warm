#!/bin/sh
# Run from any directory. Preserve local onboarding, config and Recorder data.
set -eu
cd "$(dirname "$0")/.."
mkdir -p .dev-ha/custom_components
if [ ! -f .dev-ha/configuration.yaml ]; then
    cp dev/configuration.yaml .dev-ha/configuration.yaml
fi
if [ "${1:-}" = "--prepare-only" ]; then
    exit 0
fi
exec docker compose -f compose.dev.yaml up -d
