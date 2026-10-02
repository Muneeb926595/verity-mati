#!/usr/bin/env bash
# Restores (locked) and publishes the Verity Roslyn worker, framework-dependent, into ./bin.
# Needs network only the first time (to fill the NuGet cache); afterwards it builds fully offline.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MATI="$(cd "$HERE/../.." && pwd)"
DOTNET="${DOTNET:-$MATI/tools/dotnet/dotnet}"

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
export DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
export DOTNET_GENERATE_ASPNET_CERTIFICATE=false
export DOTNET_ROOT="${DOTNET_ROOT:-$(dirname "$DOTNET")}"
export NUGET_PACKAGES="${NUGET_PACKAGES:-$MATI/tools/nuget-packages}"

if [ ! -x "$DOTNET" ]; then
  echo "error: dotnet not found at $DOTNET (set DOTNET=/path/to/dotnet)" >&2
  exit 1
fi

cd "$HERE"
LOCK_ARGS=()
if [ -f packages.lock.json ]; then LOCK_ARGS=(--locked-mode); fi

echo "== restore (NUGET_PACKAGES=$NUGET_PACKAGES)"
"$DOTNET" restore VerityRoslyn.csproj "${LOCK_ARGS[@]}"

echo "== publish -> $HERE/bin"
rm -rf "$HERE/bin"
"$DOTNET" publish VerityRoslyn.csproj -c Release --no-restore -o "$HERE/bin" \
  -p:UseAppHost=false -p:DebugType=none -p:GenerateDocumentationFile=false

echo "== smoke test"
"$DOTNET" "$HERE/bin/VerityRoslyn.dll" --version
echo "ok: run with  $DOTNET $HERE/bin/VerityRoslyn.dll <repo_root> [--out facts.jsonl]"
