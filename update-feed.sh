#!/usr/bin/env sh

# SPDX-FileCopyrightText: 2024 Jonah Brüchert <jbb@kaidan.im>
#
# SPDX-License-Identifier: AGPL-3.0-only

set -e

for station in "Podgorica"; do
    echo "# Fetching data for $station"

    # Import data from the DB API into the SQLite database
    python3 fetch-hafas.py "$station"
done

# Create a GTFS feed from the SQLite database
mkdir -p out; cd out
cat ../dump.sql | sqlite3 ../gtfs.sqlite
zip gtfs.zip *.txt

# Minify the resulting feed
gtfsclean --minimize-services --minimize-stoptimes --remove-red-routes --remove-red-services --remove-red-trips --red-trips-fuzzy --non-overlapping-services --explicit-calendar --minimize-ids-char --keep-station-ids --delete-orphans gtfs.zip --output ../me_zpcg.gtfs.zip
