#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copy the built page and its data into the lecuirot.je site repository, which serves it at /roads/.
# Usage, from the repository root: sh build/publish.sh ../lecuirot.je
set -eu
site="${1:?usage: sh build/publish.sh path/to/lecuirot.je}"
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$site/vercel.json" ] || { echo "$site does not look like the lecuirot.je repository" >&2; exit 1; }
mkdir -p "$site/roads"
cp "$here/index.html" "$here/graph.json" "$here/central.json" "$here/og.png" "$site/roads/"
echo "copied to $site/roads/: index.html graph.json central.json og.png"
