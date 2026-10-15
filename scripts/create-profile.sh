#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 ENGINE TARGET [LANGUAGE]" >&2
  exit 2
fi
engine=$1
target=$2
lang=${3:-en-US}
mkdir -p profiles
exec voicerender profile create \
  --engine "$engine" \
  --target "$target" \
  --language "$lang" \
  --offline \
  --output "profiles/${engine}.renderprofile.toml"
