# The §6 reproducibility deliverable: the router image carrying our changes.
#
# The whole contribution lives in the router pod — both `vllm_router` and the LMCache
# `cache_controller` are installed there as plain Python — so the image is the stock
# pinned router plus a file overlay. No compilation, no CUDA, no model weights.
#
# Build and push happen in CI (.github/workflows/router-image.yml), not on a laptop:
# that way the image is rebuilt from this file on every change and a grader reproduces it
# by pushing a commit rather than trusting an artifact someone built by hand.
#
# Build locally (only if you have a container runtime):
#   docker build -t quay.io/<ns>/lmstack-router-loadaware:$(git rev-parse --short HEAD) .

# Pinned by digest, not just tag: the router and the engines must carry the SAME lmcache
# minor version (both 0.3.9post2 here) or the controller<->worker msgspec ZMQ decoding
# fails *silently*. A floating tag is a silent-failure trap — see deploy/README.md.
FROM docker.io/lmcache/lmstack-router@sha256:23c64ba6f14ac363be30800764f22e3b937fee2acd7280cf61100c527644f3c7

# `patches/` mirrors the layout under site-packages, so the overlay is a straight copy and
# the image gets byte-identical code to what deploy/dev/apply-router-patch.sh mounts.
COPY patches/lmcache /opt/venv/lib/python3.12/site-packages/lmcache
COPY patches/vllm_router /opt/venv/lib/python3.12/site-packages/vllm_router

# OpenShift runs the pod as an arbitrary UID, so the copied files must be world-readable.
USER root
RUN chmod -R a+rX /opt/venv/lib/python3.12/site-packages/lmcache \
                  /opt/venv/lib/python3.12/site-packages/vllm_router
USER 1001

LABEL org.opencontainers.image.title="lmstack-router with loadaware placement" \
      org.opencontainers.image.description="vLLM production-stack router + LMCache controller, \
patched with multi-instance lookup and loadaware KV-cache-aware request placement (BGU final project)" \
      org.opencontainers.image.source="https://github.com/BenEpstein/caching-in-llms"
