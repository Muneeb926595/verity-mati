#!/usr/bin/env bash
# Installs every toolchain Verity needs INTO ./tools (no system-wide installs). Run once while online.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; T="$ROOT/tools"; mkdir -p "$T"; cd "$T"     # tools/ does not exist on a fresh copy
echo "[tools] llama.cpp"
if [ ! -x "$T/llama.cpp/llama-server" ]; then
  M="$(uname -m)"; [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] && M=arm64    # Apple silicon, even under Rosetta
  case "$(uname -s)-$M" in                       # build b11293; macOS arm64 is the tested one, the others are CPU builds
    Darwin-arm64) LB=macos-arm64 ;; Darwin-x86_64) LB=macos-x64 ;;
    Linux-x86_64) LB=ubuntu-x64 ;; Linux-aarch64|Linux-arm64) LB=ubuntu-arm64 ;;
    *) LB=""; echo "[tools] no llama.cpp build for $(uname -s) $(uname -m): Verity will answer in Direct mode (facts only)" ;;
  esac
  if [ -n "$LB" ]; then
    curl -L --fail --retry 10 -o llama.tgz "https://github.com/ggml-org/llama.cpp/releases/download/b11293/llama-b11293-bin-$LB.tar.gz" && mkdir -p llama.cpp && tar -xzf llama.tgz -C llama.cpp --strip-components=1 || tar -xzf llama.tgz -C llama.cpp
    rm -f llama.tgz
  fi
fi
echo "[tools] dotnet"
if [ ! -x "$T/dotnet/dotnet" ]; then
  curl -sSL --retry 10 -o dotnet-install.sh https://dot.net/v1/dotnet-install.sh && bash dotnet-install.sh --channel 10.0 --install-dir "$T/dotnet" --no-path
fi
echo "[tools] rust"
if [ ! -x "$T/cargo/bin/cargo" ]; then
  curl -sSf --retry 10 https://sh.rustup.rs -o rustup-init.sh && RUSTUP_HOME="$T/rustup" CARGO_HOME="$T/cargo" sh rustup-init.sh -y --no-modify-path --profile minimal --default-toolchain stable
fi
echo "[tools] cppcheck"
if [ ! -x "$T/cppcheck/bin/cppcheck" ]; then
  V="${CPPCHECK_VERSION:-2.22.0}"                # same pin as scripts/windows/catalog.json
  if [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
    echo "[tools] cppcheck skipped: building it needs the Xcode Command Line Tools (xcode-select --install). It is an optional cross-check."
  elif ! command -v cmake >/dev/null || ! { command -v c++ >/dev/null || command -v g++ >/dev/null || command -v clang++ >/dev/null; }; then
    echo "[tools] cppcheck skipped: building it needs cmake and a C++ compiler. It is an optional cross-check."
  else
  curl -L --fail --retry 10 -o cppcheck.tgz "https://github.com/danmar/cppcheck/archive/refs/tags/$V.tar.gz" && mkdir -p cppcheck-src && tar -xzf cppcheck.tgz -C cppcheck-src --strip-components=1 \
   && cmake -S cppcheck-src -B cppcheck-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$T/cppcheck" -DUSE_MATCHCOMPILER=Off -DBUILD_GUI=Off >/dev/null \
   && cmake --build cppcheck-build -j 6 >/dev/null && cmake --install cppcheck-build >/dev/null
  rm -f cppcheck.tgz
  fi
fi
# cppcheck has its install folder compiled in, so moving this folder breaks it ("Failed to load std.cfg"). It also
# looks for cfg/ next to its binary: a relative link there keeps it working wherever the folder goes.
if [ -d "$T/cppcheck/share/Cppcheck/cfg" ] && [ ! -e "$T/cppcheck/bin/cfg" ]; then ln -s ../share/Cppcheck/cfg "$T/cppcheck/bin/cfg"; fi
echo "[tools] done"
