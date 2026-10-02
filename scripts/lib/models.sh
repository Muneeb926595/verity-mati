# Verity's model list and "which models fit this machine", shared by scripts/download_models.sh and
# scripts/check_prereqs.sh. bash 3.2 compatible. Same pins as scripts/windows/catalog.json; the fit rule is the one
# verity/probe.py uses (choose_rungs): model GB + 1 GB for the embedding model must fit the memory budget.

# key|file|bytes|sha256|resident GB|label|url
VR_MODELS='embed|Qwen3-Embedding-0.6B-Q8_0.gguf|639150592|06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439|1|Qwen3-Embedding-0.6B (meaning search)|https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf
C1|Qwen3-4B-Instruct-2507-Q4_K_M.gguf|2497281120|3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597|4|Qwen3-4B (C1, small and fast)|https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
C2|gpt-oss-20b-MXFP4.gguf|12109566624|27cd6c432c7672cb812a92f611cf3ba7bbc35928262bb1e1253ff4ee6ae35901|14|gpt-oss-20b (C2, the default)|https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf
C3|gpt-oss-120b-MXFP4.gguf|63387346208|582bd40f6886200101f4c4ed9f25f3fe80cc14c86e9e2b37746cd8904a0c622d|66|gpt-oss-120b (C3, not yet benchmarked)|https://huggingface.co/ggml-org/gpt-oss-120b-GGUF/resolve/main/gpt-oss-120b-MXFP4.gguf'

vr_model() { # key field(file|bytes|sha|gb|label|url)
  local n
  case "$2" in file) n=2 ;; bytes) n=3 ;; sha) n=4 ;; gb) n=5 ;; label) n=6 ;; url) n=7 ;; *) return 1 ;; esac
  printf '%s\n' "$VR_MODELS" | awk -F'|' -v k="$1" -v n="$n" '$1 == k { print $n }'
}

vr_ram_gb() { # total memory in GB (one decimal), 0 when unknown
  local b=0
  case "$(uname -s)" in
    Darwin) b=$(sysctl -n hw.memsize 2>/dev/null || echo 0) ;;
    Linux) b=$(awk '/^MemTotal:/ { print $2 * 1024 }' /proc/meminfo 2>/dev/null || echo 0) ;;
  esac
  awk -v b="${b:-0}" 'BEGIN { printf "%.1f", b / 1073741824 }'
}

vr_apple_silicon() { [ "$(uname -s)" = Darwin ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; }

vr_nvidia_vram_gb() { # Linux with an NVIDIA driver: VRAM of the first GPU in GB, else nothing
  command -v nvidia-smi >/dev/null 2>&1 || return 0
  nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | awk '{ printf "%.1f", $1 / 1024 }'
}

vr_budget_gb() { # memory a model may use: 70% of unified memory on Apple silicon, else RAM (or VRAM + 30% of RAM)
  local ram vram
  ram=$(vr_ram_gb)
  if vr_apple_silicon; then awk -v r="$ram" 'BEGIN { printf "%.1f", r * 0.70 }'; return; fi
  vram=$(vr_nvidia_vram_gb)
  awk -v r="$ram" -v v="${vram:-0}" 'BEGIN { b = r; if (v > 0 && v + r * 0.3 > b) b = v + r * 0.3; printf "%.1f", b }'
}

vr_models_for() { # auto|direct|C1|C2|C3 -> the model keys to download
  case "$1" in
    direct) echo "embed" ;;
    C1) echo "embed C1" ;;
    C2) echo "embed C1 C2" ;;
    C3) echo "embed C1 C3" ;;
    auto|"")
      local b; b=$(vr_budget_gb)
      if awk -v b="$b" 'BEGIN { exit !(b >= 15) }'; then echo "embed C1 C2"
      elif awk -v b="$b" 'BEGIN { exit !(b >= 5) }'; then echo "embed C1"
      else echo "embed"; fi ;;
    *) return 1 ;;
  esac
}

vr_model_present() { # key -> 0 when the file is there, complete (size at least 95% of the pinned size) and not partial
  local f; f="$(vr_model "$1" file)"
  local d="${VR_MODELS_DIR:-models}"
  [ -f "$d/$f" ] && [ ! -f "$d/$f.part" ] || return 1
  local have; have=$(wc -c < "$d/$f" | tr -d ' ')
  awk -v h="$have" -v w="$(vr_model "$1" bytes)" 'BEGIN { exit !(h >= w * 0.95) }'
}

vr_gb() { awk -v b="$1" 'BEGIN { printf "%.1f", b / 1e9 }'; }
