#!/usr/bin/env bash
# Build the §6 report PDF from report.md.
#
# CI builds this on every push that touches docs/report/ or docs/figures/ and uploads the
# PDF as a workflow artifact (.github/workflows/report.yml) - same reasoning as the router
# image: the deliverable is reproducible from the repo, not from one laptop's toolchain.
#
# Locally you need pandoc + a LaTeX engine:
#   macOS:  brew install pandoc && brew install --cask basictex
#   Debian: apt-get install pandoc texlive-xetex texlive-latex-recommended texlive-fonts-recommended
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/report.pdf}"

command -v pandoc >/dev/null || {
  echo "pandoc not found - see the header of this script, or let CI build it" >&2
  exit 1
}

# --resource-path lets the figure links stay repo-relative (../figures/...) regardless of cwd.
pandoc "$here/report.md" \
  --resource-path="$here:$here/.." \
  --pdf-engine=xelatex \
  --toc --toc-depth=2 \
  -o "$out"

echo "wrote $out"
pages=$(command -v pdfinfo >/dev/null && pdfinfo "$out" | awk '/^Pages/{print $2}' || echo "?")
echo "pages: $pages  (the spec asks for 8-12, excluding appendices)"
