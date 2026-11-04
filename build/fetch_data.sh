#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Download the Jersey OpenStreetMap extract and cut it into the files build_graph.py reads.
# Needs curl and osmium-tool. Run from the repository root: sh build/fetch_data.sh
set -eu
cd "$(dirname "$0")/../data/raw"

# Delete jersey-latest.osm.pbf first to fetch a newer extract (OpenStreetMap France updates it daily).
[ -f jersey-latest.osm.pbf ] || curl -L -o jersey-latest.osm.pbf https://download.openstreetmap.fr/extracts/europe/jersey-latest.osm.pbf

# Every road, lane, service road, track, path, cycleway and set of steps. build_graph.py works out
# who may use each one (car, bike, on foot) and in which direction.
osmium tags-filter -O jersey-latest.osm.pbf \
  w/highway=trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,unclassified,residential,living_street,service,track,footway,path,cycleway,bridleway,steps,pedestrian \
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

# Heights, for walking and cycling up and down hills: Terrarium terrain tiles (Mapzen/Tilezen) from AWS
# Open Data, zoom 13 (about 12 m a pixel), covering the main island.
mkdir -p terrain
python3 - <<'PY' | while read -r z x y; do
import math
z = 13
def tile(lon, lat):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
x0, y0 = tile(-2.27, 49.28)
x1, y1 = tile(-1.99, 49.15)
for x in range(x0, x1 + 1):
    for y in range(y0, y1 + 1):
        print(z, x, y)
PY
  [ -f "terrain/$z-$x-$y.png" ] || curl -fsS -o "terrain/$z-$x-$y.png" "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/$z/$x/$y.png"
done

rm -f roads.pbf parishes.pbf buildings.pbf pois.pbf
echo "data/raw is ready: run python3 build/build_graph.py"
