# ND266: what the local-model profile trusts. Sourced by verify.sh (and so by pull.sh and serve.sh).
# No model weights live in this repository; only names and digests.
#
# Model:    Qwen3 (Apache-2.0), listed with the Ollama "tools" capability.
# Licence:  the manifest must reference this licence layer (the Apache License 2.0 text),
#           https://registry.ollama.ai/v2/library/qwen3/blobs/sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12
# Pins:     the REGISTRY manifest digest of each model (the sha256 of the manifest the registry serves:
#           curl -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
#                https://registry.ollama.ai/v2/library/qwen3/manifests/<tag> | sha256sum).
#           Ollama 0.40.13 stores that manifest byte for byte as blobs/sha256-<digest> and serves a name
#           through the symlink manifests-v2/ollama.com/library/qwen3/<tag> -> that blob. verify.sh checks
#           that symlink, the manifest's sha256 and the sha256 of every blob the manifest lists.
licence="sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12"

# pinned_models: every model name this profile may serve.
pinned_models() { echo "qwen3:8b qwen3:4b"; }

# pin_of <model>: print the pinned registry manifest sha256, or fail for a model that is not pinned.
pin_of() {
  case "$1" in
    qwen3:8b) echo 500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41 ;; # about 5.2 GB
    qwen3:4b) echo 359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7 ;; # about 2.5 GB, low RAM
    *) return 1 ;;
  esac
}
