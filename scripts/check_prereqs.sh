#!/usr/bin/env bash
# Lists everything Verity needs on THIS machine (macOS or Linux), without installing or changing anything:
#   1. this machine: platform, memory (and the model it can run), disk, internet
#   2. what you must install yourself before setup (just uv or Python 3.12 on a normal Mac)
#   3. what scripts/setup.sh installs on its own, inside this folder: already there, or still to do
#   4. optional extras and the features they add
#   5. what is NOT needed: Ollama, Docker, a system LLVM, Xcode, git, cloud accounts, admin rights
# Run it first on an empty machine, and again whenever something does not work.
#   bash scripts/check_prereqs.sh [--model auto|direct|C1|C2|C3] [--offline] [--json]
# Exit code 0: nothing blocks setup (or everything is already set up). 1: something to install first. 2: bad usage.
# Windows: scripts\windows\check_prereqs.cmd. On a Mac without the Xcode tools this never runs the /usr/bin stubs (clang,
# git, make, python3, xcrun) that would open the "install developer tools" dialog. Works with macOS's bash 3.2.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OS="$(uname -s)"
JSON=0; OFFLINE=0; MODEL="${VERITY_MODEL:-auto}"

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --offline) OFFLINE=1 ;;
    --model) MODEL="${2:-}"; shift ;;
    --model=*) MODEL="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
case "$MODEL" in auto|direct|C1|C2|C3) ;; *) echo "--model must be auto, direct, C1, C2 or C3 (got '$MODEL')" >&2; exit 2 ;; esac
case "$OS" in
  MINGW*|MSYS*|CYGWIN*) echo "This is Windows: run scripts\\windows\\check_prereqs.cmd instead." >&2; exit 2 ;;
esac
if [ ! -f "$ROOT/scripts/lib/models.sh" ]; then
  echo "Incomplete copy: $ROOT/scripts/lib/models.sh is missing. Copy the whole Verity folder again." >&2; exit 1
fi
. "$ROOT/scripts/lib/models.sh"
export VR_MODELS_DIR="$ROOT/models"

# ------------------------------------------------------------------------------------------------ results
IDS=(); GRPS=(); STATES=(); TEXTS=(); FIXES=()
add() { IDS+=("$1"); GRPS+=("$2"); STATES+=("$3"); TEXTS+=("$4"); FIXES+=("${5:-}"); }   # id group status text [fix]

have() { command -v "$1" >/dev/null 2>&1; }
CLT=1; CLT_PATH=""
if [ "$OS" = Darwin ]; then
  CLT_PATH=$(xcode-select -p 2>/dev/null || true)
  { [ -n "$CLT_PATH" ] && [ -d "$CLT_PATH" ]; } || CLT=0
fi
real() { # name: on PATH and, on a Mac without the Xcode tools, not one of the /usr/bin stubs
  local p; p=$(command -v "$1" 2>/dev/null) || return 1
  if [ "$OS" = Darwin ] && [ "$CLT" = 0 ]; then
    case "$p" in /usr/bin/clang|/usr/bin/clang++|/usr/bin/cc|/usr/bin/c++|/usr/bin/gcc|/usr/bin/g++|/usr/bin/git|\
/usr/bin/make|/usr/bin/python3|/usr/bin/pip3|/usr/bin/xcrun|/usr/bin/swift) return 1 ;; esac
  fi
  return 0
}
ver_ge() { awk -v a="$1" -v b="$2" 'BEGIN { n = split(a, x, "."); m = split(b, y, "."); for (i = 1; i <= 3; i++) {
  if ((x[i] + 0) > (y[i] + 0)) exit 0; if ((x[i] + 0) < (y[i] + 0)) exit 1 } exit 0 }'; }
gb_ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a >= b) }'; }
fsum() { awk 'BEGIN { s = 0; for (i = 1; i < ARGC; i++) s += ARGV[i]; printf "%.1f", s }' "$@"; }

ARCH="$(uname -m)"; ARM_MAC=0
if [ "$OS" = Darwin ] && vr_apple_silicon; then ARM_MAC=1; fi
ROSETTA=0; [ "$OS" = Darwin ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ] && ROSETTA=1
case "$OS-$ARCH" in
  Darwin-*) if [ "$ARM_MAC" = 1 ]; then LLAMA_BUILD=macos-arm64; else LLAMA_BUILD=macos-x64; fi ;;
  Linux-x86_64) LLAMA_BUILD=ubuntu-x64 ;;
  Linux-aarch64|Linux-arm64) LLAMA_BUILD=ubuntu-arm64 ;;
  *) LLAMA_BUILD="" ;;
esac

# what still has to be installed: id, disk GB, download GB, hosts it downloads from
TODO_IDS=""; NEED_DISK=(); NEED_DL=(); HOSTS=""
todo() { TODO_IDS="$TODO_IDS $1"; NEED_DISK+=("$2"); NEED_DL+=("$3"); HOSTS="$HOSTS $4"; }

# ------------------------------------------------------------------------------------------------ 2. required
# (checked before the machine group because the disk and internet checks depend on what is still missing)
missing_tools=""
for t in curl tar awk sed df; do have "$t" || missing_tools="$missing_tools $t"; done
have shasum || have sha256sum || missing_tools="$missing_tools shasum/sha256sum"
if [ -z "$missing_tools" ]; then
  add shell-tools required ok "bash, curl, tar, shasum: present (setup downloads with curl and checks every model's SHA256)"
else
  if [ "$OS" = Linux ]; then fx="Install them with your package manager, for example:
  sudo apt install curl tar coreutils"
  else fx="These come with macOS; reinstalling macOS tools is unusual. Check your PATH."; fi
  add shell-tools required missing "Missing:$missing_tools" "$fx"
fi

VENV_OK=0
if [ -x "$ROOT/.venv/bin/python" ] && "$ROOT/.venv/bin/python" -c \
    'import clang.cindex as c; c.Index.create(); import numpy, mcp, httpx2' >/dev/null 2>&1; then VENV_OK=1; fi
UV=""
for c in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if [ -n "$c" ] && [ -x "$c" ]; then UV="$c"; break; fi
done
P312=""
if real python3.12 && python3.12 -c 'import venv, ssl, sys; assert sys.version_info[:2] == (3, 12)' >/dev/null 2>&1; then
  P312=$(command -v python3.12)
fi
if [ "$VENV_OK" = 1 ]; then
  add python required ok "Not needed any more: .venv already has Python 3.12 and Verity's packages"
elif [ -n "$UV" ]; then
  uvv=$("$UV" --version 2>/dev/null | awk '{ print $2 }')
  case "$UV" in
    "$(command -v uv 2>/dev/null)") add python required ok "uv ${uvv:-?} at $UV: setup uses it to get Python 3.12 (no system Python needed)" ;;
    *) add python required ok "uv ${uvv:-?} at $UV (not on PATH yet; setup finds it there anyway)" ;;
  esac
elif [ -n "$P312" ]; then
  add python required ok "Python 3.12 at $P312 (setup makes its own .venv from it)"
else
  add python required missing "Neither uv nor Python 3.12 found. Setup needs one of them to create its Python environment" \
    "Install uv (no admin; it downloads Python 3.12 by itself), then run this check again:
  curl -LsSf https://astral.sh/uv/install.sh | sh
Or install Python 3.12 from python.org$( [ "$OS" = Darwin ] && echo ' (or: brew install python@3.12)' )"
fi

absent_files=""
for f in verity/__init__.py verity/__main__.py bin/verity requirements.lock scripts/setup.sh scripts/setup_toolchains.sh \
         scripts/download_models.sh workers/roslyn/VerityRoslyn.csproj workers/roslyn/build.sh; do
  [ -e "$ROOT/$f" ] || absent_files="$absent_files $f"
done
if [ -z "$absent_files" ]; then
  add setup-files required ok "Verity's code and setup scripts are complete in $ROOT"
else
  add setup-files required missing "Incomplete copy; missing:$absent_files" "Copy the whole Verity folder again (or extract the zip again)."
fi

# ------------------------------------------------------------------------------------------------ 3. installed by setup
if [ "$VENV_OK" = 1 ]; then
  add python-env auto done "Python environment (.venv) with the pinned packages; libclang (the C parser) loads"
else
  h="pypi.org files.pythonhosted.org"; [ -z "$P312" ] && h="$h github.com"
  todo python-env 0.5 0.2 "$h"
  add python-env auto todo "Setup creates .venv (Python 3.12 + pinned packages, about 0.4 GB). libclang comes as a Python package: no LLVM or Xcode needed"
fi

LS="$ROOT/tools/llama.cpp/llama-server"
if [ -x "$LS" ]; then
  if lv=$("$LS" --version 2>&1); then
    add llama.cpp auto done "llama.cpp $(printf '%s\n' "$lv" | sed -n 's/.*(build \([0-9]*\).*/build \1/p' | head -1) in tools/llama.cpp (runs the local models)"
  else
    add llama.cpp auto warn "tools/llama.cpp/llama-server is there but does not start" "Delete tools/llama.cpp and run setup again."
  fi
elif [ -n "$LLAMA_BUILD" ]; then
  todo llama.cpp 0.05 0.02 "github.com"
  add llama.cpp auto todo "Setup downloads llama.cpp build b11293 ($LLAMA_BUILD, about 15 MB) into tools/llama.cpp. This replaces Ollama"
else
  add llama.cpp auto warn "No llama.cpp build for $OS $ARCH: setup skips it and Verity answers in Direct mode (facts only)"
fi

DN="$ROOT/tools/dotnet/dotnet"
icu_fix=""
if [ "$OS" = Linux ] && have ldconfig && ! ldconfig -p 2>/dev/null | grep -q 'libicuuc'; then
  icu_fix="On Linux .NET also needs ICU (or set DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1):
  sudo apt install libicu-dev"
fi
if [ -x "$DN" ] && "$DN" --list-sdks 2>/dev/null | grep -q '^10\.'; then
  add dotnet auto done ".NET SDK $("$DN" --list-sdks 2>/dev/null | awk '/^10\./ { print $1; exit }') in tools/dotnet (runs the C# analyser; a system-wide .NET is not used)"
else
  todo dotnet 0.9 0.3 "dot.net builds.dotnet.microsoft.com"
  if [ -n "$icu_fix" ]; then add dotnet auto warn "Setup installs .NET SDK 10 (about 0.7 GB) into tools/dotnet, but libicu was not found" "$icu_fix"
  else add dotnet auto todo "Setup installs .NET SDK 10 (about 0.7 GB) into tools/dotnet, with Microsoft's dotnet-install script. No admin"; fi
fi

DLL="$ROOT/workers/roslyn/bin/VerityRoslyn.dll"
if [ -f "$DLL" ]; then
  if [ -x "$DN" ] && wv=$(DOTNET_ROOT="$ROOT/tools/dotnet" DOTNET_CLI_TELEMETRY_OPTOUT=1 "$DN" "$DLL" --version 2>/dev/null); then
    add csharp-worker auto done "C# analyser worker built and running: $wv"
  else
    add csharp-worker auto done "C# analyser worker is prebuilt (workers/roslyn/bin); it runs once .NET is installed"
  fi
else
  todo csharp-worker 0.2 0.1 "api.nuget.org"
  add csharp-worker auto todo "Setup builds the C# analyser worker with .NET (downloads its packages from nuget.org once)"
fi

CXX=0; { real c++ || real clang++ || real g++; } && CXX=1
if [ -x "$ROOT/tools/cppcheck/bin/cppcheck" ] || real cppcheck; then
  cp="$ROOT/tools/cppcheck/bin/cppcheck"; [ -x "$cp" ] || cp=$(command -v cppcheck)
  add cppcheck auto done "$("$cp" --version 2>/dev/null || echo cppcheck) (optional second opinion on C code)"
elif have cmake && [ "$CXX" = 1 ]; then
  todo cppcheck 0.6 0.01 "github.com"
  add cppcheck auto todo "Setup builds cppcheck 2.22.0 from source (about 2 minutes), an optional second opinion on C code"
else
  if [ "$OS" = Darwin ]; then fx="Optional. To get it, install the Xcode Command Line Tools and cmake, then run setup again:
  xcode-select --install
  uv tool install cmake"
  else fx="Optional. To get it, install cmake and a C++ compiler (or your distribution's cppcheck package):
  sudo apt install cmake g++"; fi
  add cppcheck auto warn "Setup will skip cppcheck: building it needs cmake and a C++ compiler. Verity works without it" "$fx"
fi

if [ -x "$ROOT/tools/cargo/bin/cargo" ]; then
  add rust auto done "Rust in tools/cargo (only used to build the desktop app)"
else
  todo rust 0.7 0.2 "sh.rustup.rs static.rust-lang.org"
  add rust auto todo "Setup installs Rust (about 0.6 GB) into tools/; only used to build the desktop app"
fi

NODE_V=""; NODE_OK=0
if have node; then
  NODE_V=$(node --version 2>/dev/null | sed 's/^v//')
  if [ -n "$NODE_V" ]; then
    maj=${NODE_V%%.*}
    if [ "$maj" -eq 20 ] 2>/dev/null; then ver_ge "$NODE_V" 20.19.0 && NODE_OK=1
    elif [ "$maj" -eq 22 ] 2>/dev/null; then ver_ge "$NODE_V" 22.12.0 && NODE_OK=1
    elif [ "$maj" -gt 22 ] 2>/dev/null; then NODE_OK=1; fi
  fi
fi
NODE_FIX="Install Node.js 22 LTS from https://nodejs.org (any 20.19+, 22.12+ or newer works)"
if [ -f "$ROOT/ui/dist/index.html" ]; then
  add ui auto done "User interface is prebuilt (ui/dist): no Node.js needed to use it"
elif [ "$NODE_OK" = 1 ]; then
  todo ui 0.3 0.1 "registry.npmjs.org"
  add ui auto todo "Setup builds the user interface with Node.js $NODE_V"
else
  add ui auto missing "ui/dist is missing and Node.js ${NODE_V:+$NODE_V is too old}${NODE_V:-is not installed}: setup would stop at the UI step" \
    "$NODE_FIX; or copy ui/dist from a machine where it is built (the Windows zip includes it)"
fi

# models: what --model selects, what is already here, what Verity will then use
RAM=$(vr_ram_gb); BUDGET=$(vr_budget_gb)
KEYS=$(vr_models_for "$MODEL")
have_list=""; get_list=""; get_bytes=0
for k in $KEYS; do
  if vr_model_present "$k"; then have_list="$have_list, $(vr_model "$k" label)"
  else get_list="$get_list, $(vr_model "$k" label) $(vr_gb "$(vr_model "$k" bytes)") GB"; get_bytes=$(fsum "$get_bytes" "$(vr_model "$k" bytes)"); fi
done
have_list=${have_list#, }; get_list=${get_list#, }
USE=""      # what Verity will run after setup: the strongest model present that fits (verity/probe.py choose_rungs)
for k in C3 C2 C1; do
  case " $KEYS " in *" $k "*) inset=1 ;; *) inset=0 ;; esac
  if { [ "$inset" = 1 ] || vr_model_present "$k"; } && gb_ge "$BUDGET" "$(fsum "$(vr_model "$k" gb)" 1)"; then
    USE=$(vr_model "$k" label); [ "$inset" = 1 ] || USE="$USE (already in models/)"; break
  fi
done
use_txt="Verity will answer with ${USE:-no chat model (Direct mode: answers built from checked facts)}"
fit_warn=""      # a forced C1/C2/C3 that this machine's memory cannot hold is downloaded but never used
case "$MODEL" in C1|C2|C3)
  need=$(fsum "$(vr_model "$MODEL" gb)" 1)
  gb_ge "$BUDGET" "$need" || fit_warn="$(vr_model "$MODEL" label) needs about $need GB but this machine's budget is $BUDGET GB, so it would not be used" ;;
esac
if [ -z "$get_list" ]; then
  if [ -n "$fit_warn" ]; then add models auto warn "Models present: $have_list. $fit_warn. $use_txt" "Use --model auto (the models this machine can run)"
  else add models auto done "Models ready: $have_list. $use_txt"; fi
else
  dl_gb=$(awk -v b="$get_bytes" 'BEGIN { printf "%.1f", b / 1e9 }')
  todo models "$dl_gb" "$dl_gb" "huggingface.co"
  if [ -n "$fit_warn" ]; then
    add models auto warn "Setup would download $dl_gb GB (--model $MODEL): $get_list. $fit_warn. $use_txt" "Use --model auto (the models this machine can run)"
  else
    add models auto todo "Setup downloads $dl_gb GB (--model $MODEL): $get_list. Resumable and SHA256-checked. $use_txt"
  fi
fi

# ------------------------------------------------------------------------------------------------ 1. this machine
if [ "$OS" = Darwin ]; then
  mv_=$(sw_vers -productVersion 2>/dev/null || echo "?"); mmaj=${mv_%%.*}
  if [ "$ARM_MAC" = 1 ] && [ "$ROSETTA" = 1 ]; then
    add platform machine warn "macOS $mv_ on Apple silicon, but this terminal runs under Rosetta (Intel emulation)" \
      "Use a native terminal (Terminal > Get Info > untick 'Open using Rosetta'), or uv may fetch an Intel Python"
  elif [ "$ARM_MAC" = 1 ] && [ "$mmaj" -lt 14 ] 2>/dev/null; then
    add platform machine warn "macOS $mv_ on Apple silicon. Verity was tested on macOS 26; .NET 10 and this llama.cpp build may need macOS 14 or newer" \
      "Update macOS if setup fails at the .NET or llama.cpp step"
  elif [ "$ARM_MAC" = 1 ]; then
    add platform machine ok "macOS $mv_ on Apple silicon ($(sysctl -n machdep.cpu.brand_string 2>/dev/null)): the platform Verity is tested on"
  else
    add platform machine warn "macOS $mv_ on an Intel Mac: not tested; models run on the CPU only (slow), Direct mode is unaffected"
  fi
elif [ "$OS" = Linux ]; then
  dist=$( (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || echo Linux)
  if [ -n "$LLAMA_BUILD" ]; then
    add platform machine warn "$dist ($ARCH): should work but not tested yet; models use the CPU build of llama.cpp; the desktop app is built for macOS and Windows only"
  else
    add platform machine warn "$dist ($ARCH): not tested and no llama.cpp build for $ARCH; Direct mode only"
  fi
else
  add platform machine missing "$OS $ARCH is not supported" "Use macOS (Apple silicon), Linux, or Windows (scripts\\windows\\check_prereqs.cmd)"
fi

if gb_ge "$BUDGET" 15; then
  rtxt="$RAM GB memory: enough for gpt-oss-20b, Verity's default model"
  gb_ge "$BUDGET" 67 && rtxt="$RAM GB memory: enough even for gpt-oss-120b (setup --model C3; 63 GB download, not yet benchmarked)"
  add ram machine ok "$rtxt"
elif gb_ge "$BUDGET" 5; then
  add ram machine warn "$RAM GB memory: enough for Qwen3-4B (weaker); gpt-oss-20b needs about 15 GB for the model ($( [ "$ARM_MAC" = 1 ] && echo 'a 24 GB Mac' || echo '16 GB or more'))" \
    "Fine to use: setup picks Qwen3-4B on its own. Direct mode (facts only) works on any machine"
else
  add ram machine warn "$RAM GB memory: too little for a local model; Verity will answer in Direct mode (facts only)"
fi

NEED=$(fsum 2 "${NEED_DISK[@]+"${NEED_DISK[@]}"}")       # +2 GB for indexes and logs
FREE=$(df -Pk "$ROOT" 2>/dev/null | awk 'NR == 2 { printf "%.1f", $4 / 1048576 }')
FREE=${FREE:-0}
if [ -z "$TODO_IDS" ]; then
  gb_ge "$FREE" 2 && add disk machine ok "$FREE GB free; nothing left to download (keep a few GB for project indexes)" \
                  || add disk machine warn "$FREE GB free: low; indexing large projects needs a few GB" "Free some disk space"
elif gb_ge "$FREE" "$(fsum "$NEED" 10)"; then
  add disk machine ok "$FREE GB free; setup needs about $NEED GB (including 2 GB for indexes)"
elif gb_ge "$FREE" "$NEED"; then
  add disk machine warn "$FREE GB free; setup needs about $NEED GB: enough, but little to spare" "Free more space, or use --model C1 / --model direct"
else
  add disk machine missing "$FREE GB free; setup needs about $NEED GB" "Free disk space, or choose a smaller model: --model C1 (2.5 GB) or --model direct"
fi

DL=$(fsum "${NEED_DL[@]+"${NEED_DL[@]}"}")
if [ -z "$TODO_IDS" ]; then
  add internet machine ok "Not needed: everything is installed; Verity works offline"
elif [ "$OFFLINE" = 1 ]; then
  add internet machine warn "Not checked (--offline). Setup still has to download about $DL GB from:$(printf '%s\n' $HOSTS | sort -u | tr '\n' ' ' | sed 's/^/ /')"
else
  TMPD=$(mktemp -d 2>/dev/null || echo "/tmp/vr-prereq-$$"); mkdir -p "$TMPD"
  uhosts=$(printf '%s\n' $HOSTS | sort -u)
  for h in $uhosts; do
    ( curl -sS -o /dev/null -I --max-time 10 "https://$h/" >/dev/null 2>&1 && : > "$TMPD/$h" ) &
  done
  wait
  bad=""; for h in $uhosts; do [ -f "$TMPD/$h" ] || bad="$bad $h"; done
  rm -rf "$TMPD"
  if [ -z "$bad" ]; then
    add internet machine ok "Reachable: $(echo $uhosts | sed 's/ /, /g'). Setup downloads about $DL GB; afterwards Verity works offline"
  else
    add internet machine missing "Setup must download about $DL GB, but these sites are unreachable:$bad" \
      "Connect to the internet, or ask IT to allow: $(echo $uhosts | sed 's/ /, /g'). Behind a proxy, set https_proxy first"
  fi
fi

# ------------------------------------------------------------------------------------------------ 4. optional
if [ "$OS" = Darwin ]; then
  if [ "$CLT" = 1 ]; then
    add c-compiler optional ok "Xcode Command Line Tools ($CLT_PATH): Verity can compile and run code for \"what happens when\" questions, measure coverage, build cppcheck"
  else
    add c-compiler optional absent "No Xcode Command Line Tools: questions about runtime behaviour are answered from the code alone. C parsing still works (bundled headers)" \
      "Optional (a free Apple download; adds clang, make and git):
  xcode-select --install"
  fi
else
  if real cc || real gcc || real clang; then add c-compiler optional ok "C compiler $(command -v cc || command -v gcc || command -v clang): enables running code for \"what happens when\" questions"
  else add c-compiler optional absent "No C compiler: questions about runtime behaviour are answered from the code alone" "Optional:
  sudo apt install build-essential"; fi
fi

if [ "$OS" = Darwin ]; then
  add sandbox optional ok "sandbox-exec (built into macOS): code Verity compiles from your project runs isolated, without network"
elif have bwrap; then
  add sandbox optional ok "bubblewrap: code Verity compiles from your project runs isolated"
else
  add sandbox optional absent "No bubblewrap: Verity will not run any project code (it never runs code unsandboxed)" "Optional:
  sudo apt install bubblewrap"
fi

if have cmake; then
  add cmake optional ok "$(cmake --version 2>/dev/null | head -1): Verity reads build settings from CMake projects; setup can build cppcheck"
else
  add cmake optional absent "No cmake: CMake projects are read without their exact build flags (still works, a little less precise); setup skips cppcheck" \
    "Optional (no admin):
  uv tool install cmake"
fi

if [ "$NODE_OK" = 1 ]; then
  add node optional ok "Node.js $NODE_V: can rebuild the UI and build the desktop app"
elif [ -n "$NODE_V" ]; then
  add node optional warn "Node.js $NODE_V is too old to build the UI (needs 20.19+ or 22.12+); not needed to use the prebuilt UI" "$NODE_FIX"
else
  add node optional absent "No Node.js: only needed to rebuild the UI or build the desktop app" "Optional: $NODE_FIX"
fi

if [ "$OS" = Darwin ]; then
  if [ "$CLT" = 1 ] && [ "$NODE_OK" = 1 ]; then
    add desktop-app optional ok "Can build the desktop app: bash scripts/build_desktop.sh (Rust comes from setup)"
  else
    add desktop-app optional absent "The desktop app needs the Xcode Command Line Tools and Node.js to build. The same pages work in the browser: bin/verity ui"
  fi
else
  add desktop-app optional info "The desktop app is macOS only; use the browser: bin/verity ui"
fi

if [ "$ARM_MAC" = 1 ]; then
  add gpu optional ok "Apple GPU via Metal: built in, models run on it with nothing to install"
elif have nvidia-smi && gname=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1) && [ -n "$gname" ]; then
  add gpu optional info "NVIDIA $gname found; setup installs the CPU build of llama.cpp here (a CUDA build from the same release can replace tools/llama.cpp)"
else
  add gpu optional absent "No supported GPU: models run on the CPU (slower answers; Direct mode is unaffected)"
fi

# ------------------------------------------------------------------------------------------------ 5. not needed
if have ollama || [ -d /Applications/Ollama.app ]; then
  add ollama not_needed info "Not needed: Verity runs models with its own llama.cpp and model files. Ollama is installed here; it can stay, but don't run a big model in it while using Verity (memory)"
else
  add ollama not_needed info "Not needed: Verity runs models with its own llama.cpp (tools/) and model files (models/), not Ollama"
fi
add docker not_needed info "Not needed: no containers or virtual machines"
add system-llvm not_needed info "Not needed: no system clang/LLVM; the C parser (libclang 18) comes with the Python packages"
if [ "$OS" = Darwin ]; then
  add xcode not_needed info "Not needed: the full Xcode app. The free Command Line Tools are enough, and only for the optional features above"
fi
add system-python not_needed info "Not needed: your system's python3. Setup makes its own Python 3.12 environment in .venv"
add git not_needed info "Not needed: setup downloads pinned releases and Verity never calls git"
add homebrew not_needed info "Not needed: nothing is installed with Homebrew or another package manager$( [ "$OS" = Linux ] && echo ' (except the optional extras)' )"
add cloud not_needed info "Not needed: accounts, API keys or cloud services. After setup Verity works fully offline and sends nothing anywhere"
add admin not_needed info "Not needed: admin rights or sudo. Everything installs inside $ROOT; deleting the folder removes it all"

# ------------------------------------------------------------------------------------------------ verdict
n_missing=0; n_todo=0; n_warn=0
i=0; while [ $i -lt ${#IDS[@]} ]; do
  case "${STATES[$i]}" in missing) n_missing=$((n_missing + 1)) ;; todo) n_todo=$((n_todo + 1)) ;; warn) n_warn=$((n_warn + 1)) ;; esac
  i=$((i + 1))
done
MFLAG=""; [ "$MODEL" != auto ] && MFLAG=" --model $MODEL"
if [ "$n_missing" -gt 0 ]; then
  STATE=blocked; NEXT="Install what is marked MISSING, then run this check again: bash scripts/check_prereqs.sh"
  VERDICT="NOT READY: $n_missing thing(s) to fix first (marked MISSING)."
elif [ "$n_todo" -gt 0 ]; then
  STATE=ready; NEXT="bash scripts/setup.sh$MFLAG"
  VERDICT="READY FOR SETUP: setup installs the $n_todo item(s) marked TODO by itself (about $DL GB download, $NEED GB disk)."
else
  STATE=installed; NEXT="bin/verity probe   then   bin/verity ask samples/navgps-c \"Explain how GPS data travels through this application.\""
  VERDICT="ALREADY SET UP: everything Verity needs is installed."
fi

# ------------------------------------------------------------------------------------------------ output
if [ "$JSON" = 1 ]; then
  jesc() { local s=$1; s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\t'/ }; s=${s//$'\n'/\\n}; printf '%s' "$s"; }
  printf '{\n  "tool": "verity-check-prereqs",\n  "schema": 1,\n'
  printf '  "platform": "%s",\n  "root": "%s",\n' "$(jesc "$OS-$ARCH")" "$(jesc "$ROOT")"
  printf '  "model_choice": "%s",\n  "ram_gb": %s,\n  "model_budget_gb": %s,\n' "$MODEL" "$RAM" "$BUDGET"
  printf '  "models_selected": "%s",\n  "model_used": "%s",\n  "backend": "%s",\n' "$KEYS" "$(jesc "${USE:-direct}")" "${LLAMA_BUILD:-none}"
  printf '  "disk_free_gb": %s,\n  "disk_needed_gb": %s,\n  "download_gb": %s,\n' "$FREE" "$NEED" "$DL"
  printf '  "state": "%s",\n  "missing": %d,\n  "todo": %d,\n  "warnings": %d,\n' "$STATE" "$n_missing" "$n_todo" "$n_warn"
  printf '  "next": "%s",\n  "items": [\n' "$(jesc "$NEXT")"
  k=0
  for g in machine required auto optional not_needed; do       # same order as the text report
    i=0; while [ $i -lt ${#IDS[@]} ]; do
      if [ "${GRPS[$i]}" = "$g" ]; then
        k=$((k + 1)); sep=","; [ $k -eq ${#IDS[@]} ] && sep=""
        printf '    {"id": "%s", "group": "%s", "status": "%s", "summary": "%s", "fix": "%s"}%s\n' \
          "${IDS[$i]}" "${GRPS[$i]}" "${STATES[$i]}" "$(jesc "${TEXTS[$i]}")" "$(jesc "${FIXES[$i]}")" "$sep"
      fi
      i=$((i + 1))
    done
  done
  printf '  ]\n}\n'
else
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
  else B=""; D=""; R=""; G=""; Y=""; C=""; N=""; fi
  W=$(( $( (tput cols 2>/dev/null || echo 110) | tr -dc 0-9) + 0 )); [ "$W" -gt 120 ] && W=120; [ "$W" -lt 70 ] && W=70
  TW=$((W - 26))
  printf '%sVerity: what this machine needs%s   %s\n' "$B" "$N" "$ROOT"
  printf '%s%s, model choice: %s%s\n' "$D" "$(date '+%Y-%m-%d %H:%M')" "$MODEL" "$N"
  for g in machine required auto optional not_needed; do
    case "$g" in
      machine) title="1. This machine" ;;
      required) title="2. Install yourself before setup" ;;
      auto) title="3. Installed by setup on its own (inside this folder, no admin)" ;;
      optional) title="4. Optional extras" ;;
      not_needed) title="5. NOT needed" ;;
    esac
    printf '\n%s%s%s\n' "$B" "$title" "$N"
    i=0; while [ $i -lt ${#IDS[@]} ]; do
      if [ "${GRPS[$i]}" = "$g" ]; then
        case "${STATES[$i]}" in
          ok) tag="${G}  OK   ${N}" ;; done) tag="${G} DONE  ${N}" ;; todo) tag="${C} TODO  ${N}" ;;
          warn) tag="${Y} WARN  ${N}" ;; missing) tag="${R}MISSING${N}" ;; absent) tag="${D}  --   ${N}" ;; *) tag="${D}  no   ${N}" ;;
        esac
        first=1
        printf '%s\n' "${TEXTS[$i]}" | fold -s -w "$TW" | while IFS= read -r ln; do
          if [ "$first" = 1 ]; then printf '  %s %-14s %s\n' "$tag" "${IDS[$i]}" "$ln"; first=0
          else printf '  %7s %-14s %s\n' "" "" "$ln"; fi
        done
        if [ -n "${FIXES[$i]}" ]; then
          # each line of the fix starts with "->"; a line that wraps continues indented (commands stay copyable)
          printf '%s\n' "${FIXES[$i]}" | while IFS= read -r para; do
            printf '%s\n' "$para" | fold -s -w "$((TW - 3))" | {
              IFS= read -r ln; printf '  %7s %-14s %s-> %s%s\n' "" "" "$C" "$ln" "$N"
              while IFS= read -r ln; do printf '  %7s %-14s %s   %s%s\n' "" "" "$C" "$ln" "$N"; done
            }
          done
        fi
      fi
      i=$((i + 1))
    done
  done
  printf '\n%s%s%s\n' "$B" "$VERDICT" "$N"
  [ "$n_warn" -gt 0 ] && printf '%s%d warning(s): Verity works, with the limits noted.%s\n' "$D" "$n_warn" "$N"
  printf 'Next: %s%s%s\n' "$B" "$NEXT" "$N"
fi
[ "$n_missing" -eq 0 ]
