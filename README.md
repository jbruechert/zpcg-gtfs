<!--
SPDX-FileCopyrightText: 2024 Jonah Brüchert <jbb@kaidan.im>

SPDX-License-Identifier: AGPL-3.0-only
-->

# ŽPCG GTFS-Feed Generator

This repository contains scripts to fetch the timetable from the Deutsche Bahn API, augment it with OpenStreetMap data and generate a GTFS feed from it.

The resulting feed can be found at [jbb.ghsq.de/gtfs/me-zpcg.gtfs.zip](https://jbb.ghsq.de/gtfs/me-zpcg.gtfs.zip)

## Dependencies

To run the `update-feed.sh` script, you need

* python3
* pyhafas
* zip
* [gtfsclean](https://github.com/public-transport/gtfsclean/)


## License

`stations.geojson` is exported from OpenStreetMap and licensed under the [ODbL](https://opendatacommons.org/licenses/odbl/)

## Maintainance

You can regenerate the stations.geojson file using the following query on https://overpass-turbo.eu/
```
(
    area["ISO3166-1"="RS"];
    area["ISO3166-1"="ME"];
    area["ISO3166-1"="BA"];
    area["ISO3166-1"="HU"];
);
(
  (
    nwr[~"disused:railway|construction:railway|railway"~"station|halt|yard"](area);
  );
);
out center;
```
