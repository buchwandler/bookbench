#!/usr/bin/env bash
set -euo pipefail

readonly WHISPER_CPP_URL="https://github.com/ggml-org/whisper.cpp.git"
readonly WHISPER_CPP_REV="0c7be696371069e0baa2515ed7aa08d5a5473aa6"
readonly RUNTIME_ROOT="${SPEECHSCOPE_WHISPERCPP_CACHE:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/bookbench-e2e-runtime}"
readonly SOURCE_DIR="$RUNTIME_ROOT/whisper.cpp"
readonly BUILD_DIR="$SOURCE_DIR/build"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required to install the macOS E2E prerequisites." >&2
  exit 1
fi

missing_packages=()
for item in cmake:cmake ffmpeg:ffmpeg espeak-ng:espeak-ng git:git; do
  command_name="${item%%:*}"
  package_name="${item#*:}"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    missing_packages+=("$package_name")
  fi
done
if ((${#missing_packages[@]})); then
  brew install "${missing_packages[@]}"
fi

xcode-select -p >/dev/null
for command_name in git cmake ffmpeg espeak-ng clang++; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required macOS E2E prerequisite is missing: $command_name" >&2
    exit 1
  fi
done

mkdir -p "$RUNTIME_ROOT"
CACHED_REVISION="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
WHISPER_CLI="$BUILD_DIR/bin/whisper-cli"
if [[ ! -x "$WHISPER_CLI" || "$CACHED_REVISION" != "$WHISPER_CPP_REV" ]]; then
  rm -rf "$SOURCE_DIR"
  git init "$SOURCE_DIR"
  git -C "$SOURCE_DIR" remote add origin "$WHISPER_CPP_URL"
  git -C "$SOURCE_DIR" fetch --depth 1 origin "$WHISPER_CPP_REV"
  git -C "$SOURCE_DIR" checkout --detach FETCH_HEAD
  test "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$WHISPER_CPP_REV"
  cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release \
    -DWHISPER_BUILD_TESTS=OFF -DGGML_METAL=OFF
  cmake --build "$BUILD_DIR" --config Release --target whisper-cli --parallel 2
fi

if [[ ! -x "$WHISPER_CLI" ]]; then
  WHISPER_CLI="$(find "$BUILD_DIR" -type f -name whisper-cli -print -quit)"
fi
if [[ -z "$WHISPER_CLI" || ! -x "$WHISPER_CLI" ]]; then
  echo "Could not discover an executable whisper-cli in $BUILD_DIR" >&2
  exit 1
fi
"$WHISPER_CLI" --help >/dev/null

if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf 'SPEECHSCOPE_WHISPERCPP_BIN=%s\n' "$WHISPER_CLI" >> "$GITHUB_ENV"
  printf '%s\n' "$(dirname "$WHISPER_CLI")" >> "$GITHUB_PATH"
fi

printf 'Built whisper.cpp %s at %s\n' "$WHISPER_CPP_REV" "$WHISPER_CLI"
