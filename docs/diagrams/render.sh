#!/usr/bin/env sh
# Render every docs/diagrams/*.mmd to a PNG next to it (needs Node; downloads mermaid-cli on first run).
# The images are committed so the README looks the same everywhere, rather than depending on GitHub's Mermaid.
# Pass a Puppeteer config with -p to use an installed Chromium, e.g.: docs/diagrams/render.sh -p puppeteer.json
set -eu
cd "$(dirname "$0")"
for src in *.mmd; do
  npx -y @mermaid-js/mermaid-cli@12.0.0 "$@" -i "$src" -o "${src%.mmd}.png" -s 2 -b white
done
