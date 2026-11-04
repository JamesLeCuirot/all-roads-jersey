#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Download the Jersey OpenStreetMap extract and cut it into the files build_graph.py reads.
# Needs curl and osmium-tool. Run from the repository root: sh build/fetch_data.sh
set -eu
cd "$(dirname "$0")/../data/raw"

# Delete jersey-latest.osm.pbf first to fetch a newer extract (OpenStreetMap France updates it daily).
[ -f jersey-latest.osm.pbf ] || curl -L -o jersey-latest.osm.pbf https://download.openstreetmap.fr/extracts/europe/jersey-latest.osm.pbf

# Every road a car can use, from A roads down to green lanes. Service roads, tracks and paths are left out.
osmium tags-filter -O jersey-latest.osm.pbf \
  w/highway=trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,unclassified,residential,living_street \
  -o roads.pbf
osmium cat -O roads.pbf -f opl,add_metadata=false -o roads.opl

# The twelve parishes (admin_level 8). Their land polygons also draw the island.
osmium tags-filter -O jersey-latest.osm.pbf r/admin_level=8 -o parishes.pbf
osmium export -O parishes.pbf -f geojson --geometry-types=polygon -o parishes.geojson

# Buildings, to spread each parish's census population.
osmium tags-filter -O jersey-latest.osm.pbf wr/building -o buildings.pbf
osmium export -O buildings.pbf -f geojsonseq --geometry-types=polygon -o buildings.geojsonseq

# Places and landmarks for search and labels.
osmium tags-filter -O jersey-latest.osm.pbf \
  nwr/place nwr/natural=beach nwr/historic=castle,fort nwr/aeroway=aerodrome \
  nwr/amenity=hospital,school,ferry_terminal nwr/man_made=lighthouse nwr/tourism=attraction,museum,zoo \
  -o pois.pbf
osmium export -O pois.pbf -f geojsonseq -o pois.geojsonseq

rm -f roads.pbf parishes.pbf buildings.pbf pois.pbf
echo "data/raw is ready: run python3 build/build_graph.py"
