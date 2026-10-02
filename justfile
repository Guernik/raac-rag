mermaid_cli := "@mermaid-js/mermaid-cli@12.0.0"

# The diagram PNGs are committed so the README looks the same everywhere instead of
# depending on GitHub's Mermaid. Needs Node. Extra args go to mermaid-cli, e.g.
# `just diagrams -p puppeteer.json` to point it at an installed Chromium.

# Re-render every docs/diagrams/*.mmd to the PNG next to it
diagrams *args:
    #!/usr/bin/env sh
    set -eu
    for src in docs/diagrams/*.mmd; do
        npx -y {{mermaid_cli}} {{args}} -i "$src" -o "${src%.mmd}.png" -s 2 -b white
    done
