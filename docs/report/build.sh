#!/usr/bin/env bash
# Build a graded PDF deliverable from its markdown source.
#
#   build.sh                                   # the §6 report -> docs/report/report.pdf
#   build.sh out.pdf                           # the §6 report -> out.pdf
#   build.sh out.pdf ../baseline-justification.md   # the §2 one-pager -> out.pdf
#
# Both §2 and §6 are PDF deliverables in the spec, so both build here rather than one being
# a rendered artifact and the other a markdown file a grader has to imagine rendered. CI runs
# this on every push touching either source (.github/workflows/report.yml) and uploads the
# results - same reasoning as the router image: the graded artifact rebuilds from the repo,
# not from one laptop's toolchain.
#
# Locally you need pandoc + a LaTeX engine:
#   macOS:  brew install pandoc && brew install --cask basictex
#   Debian: apt-get install pandoc texlive-xetex texlive-latex-recommended texlive-fonts-recommended
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/report.pdf}"
src="${2:-$here/report.md}"
case "$src" in /*) ;; *) src="$here/$src" ;; esac
[ -f "$src" ] || { echo "no such source: $src" >&2; exit 1; }

command -v pandoc >/dev/null || {
  echo "pandoc not found - see the header of this script, or let CI build it" >&2
  exit 1
}

# --resource-path lets the figure links stay repo-relative (../figures/...) regardless of cwd.
opts=(--resource-path="$here:$here/..:$here/../.." --pdf-engine=xelatex)
if [ "$(basename "$src")" = "report.md" ]; then
  # The report carries its own geometry, fontsize and glyph mappings in YAML front matter.
  #
  # No table of contents. The whole PDF is budgeted at 12 pages including the appendix, and a
  # contents page costs one of them to index a document short enough to page through. The
  # section headings do the navigating.
  :
else
  # A one-pager gets no table of contents, and its page geometry has to come from here
  # because the source is plain markdown a grader also reads on GitHub - front matter would
  # render as a stray table there.
  #
  # Tighter than the report on purpose. At the report's 11pt/2.5cm this document rendered at
  # 2 pages against a 1-page cap, and the answer is not to delete evidence the spec asks for
  # by name (main features, default eviction policy). 10pt is the spec's own floor for figure
  # and body text, and 2cm margins are ordinary for a single-page document.
  opts+=(-V geometry:margin=2cm -V fontsize=10pt)
fi

pandoc "$src" "${opts[@]}" -o "$out"

echo "wrote $out"
pages=$(command -v pdfinfo >/dev/null && pdfinfo "$out" | awk '/^Pages/{print $2}' || echo "?")
echo "pages: $pages"
