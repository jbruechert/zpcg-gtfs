#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2024 Jonah Brüchert <jbb@kaidan.im>
#
# SPDX-License-Identifier: AGPL-3.0-only

import datetime
from typing import List, Tuple, Optional
import json
import sqlite3
import subprocess
import os
import tomllib
import sys
import unicodedata
import re
from hashlib import sha256
from pathlib import Path
from difflib import SequenceMatcher

from pyhafas import HafasClient
from pyhafas.profile import OEBBProfile
from pyhafas.types.fptf import Leg, Mode
from pyhafas.types.exceptions import GeneralHafasError


def prepare_database(sqlite_filename):
    db = sqlite3.connect(sqlite_filename)
    cur = db.cursor()
    cur.executescript(
        """
    CREATE TABLE IF NOT EXISTS `agencies` (`agency_id` text PRIMARY KEY NOT NULL, `agency_name` text NOT NULL, `agency_url` text NOT NULL, `agency_timezone` text NOT NULL, `agency_phone` text DEFAULT NULL, `agency_fare_url` text DEFAULT NULL,  `agency_email` text DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `calendar_dates` (`service_id` text NOT NULL, `date` integer NOT NULL,`exception_type` integer NOT NULL, PRIMARY KEY(`service_id`, `date`));
    CREATE TABLE IF NOT EXISTS `routes` (`route_id` text PRIMARY KEY NOT NULL, `agency_id` text NOT NULL, `route_short_name` text DEFAULT NULL, `route_long_name` text DEFAULT NULL, `route_desc` text DEFAULT NULL, `route_type` smallint NOT NULL, `route_url` text DEFAULT NULL, `route_color` text DEFAULT NULL, `route_text_color` text DEFAULT NULL, `route_sort_order` smallint DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `stops` (`stop_id` text PRIMARY KEY NOT NULL, `stop_code` text DEFAULT NULL, `stop_name` text NOT NULL, `tts_stop_name` text DEFAULT NULL, `stop_desc` text DEFAULT NULL, `stop_lat` real NOT NULL, `stop_lon` real NOT NULL, `zone_id` text DEFAULT NULL, `stop_url` text DEFAULT NULL, `location_type` integer DEFAULT NULL, `parent_station` text DEFAULT NULL, `stop_timezone`, `wheelchair_boarding` integer DEFAULT NULL, `level_id` text DEFAULT NULL, `platform_code` text DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `stop_times` (`trip_id` text NOT NULL, `arrival_time` text DEFAULT NULL, `departure_time` text DEFAULT NULL, `stop_id` text NOT NULL, `location_group_id` text, `location_id` text, `stop_sequence` smallint NOT NULL, `stop_headsign` text DEFAULT NULL, `pickup_type` integer DEFAULT NULL, `drop_off_type` integer DEFAULT NULL, `timepoint` integer DEFAULT NULL, PRIMARY KEY(`trip_id`, `stop_sequence`));
    CREATE TABLE IF NOT EXISTS `trips` (`route_id` text NOT NULL, `service_id` text NOT NULL, `trip_id` text PRIMARY KEY NOT NULL, `trip_headsign` text DEFAULT NULL, `trip_short_name` text DEFAULT NULL, `direction_id` integer DEFAULT NULL, `block_id` text DEFAULT NULL, `shape_id` text DEFAULT NULL, `wheelchair_accessible` integer DEFAULT NULL, `bikes_allowed` integer DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `feed_info` (`feed_publisher_name` text PRIMARY KEY NOT NULL, `feed_publisher_url` text NOT NULL, `feed_lang` text NOT NULL, `feed_contact_email` text);
    """
    )
    return (db, cur)


def get_stations():
    stops_geojson = json.load(open("stations.geojson", "r"))
    stations = stops_geojson["features"]

    stations.sort(key=lambda s: s["geometry"]["coordinates"][0])
    stations.sort(key=lambda s: s["geometry"]["coordinates"][1])
    return stations


def distance(a: Tuple[float, float], b: Tuple[float, float]):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


class Stop:
    name: str
    lat: float
    lon: float


def choose_best_osm_node(candidates, stop):
    for candidate in candidates:
        # Prefer node with matching ibnr
        if "ref:ibnr" in candidate["properties"] and candidate["properties"]["ref:ibnr"] == stop.id:
            return candidate

    for candidate in candidates:
        # Prefer stations and halts over yards and disused / abandoned nodes
        if (
            (
                "railway" in candidate["properties"]
                and (
                    candidate["properties"]["railway"] == "station"
                    or candidate["properties"]["railway"] == "halt"
                )
                or (
                    "public_transport" in candidate["properties"]
                    and "public_transport" in candidate["properties"] == "station"
                )
            )
            and "abandoned:railway" not in candidate["properties"]
            and "disused:railway" not in candidate["properties"]
        ):
            return candidate
    else:
        # If nothing obvious was found, use the next best thing
        return candidates[0]


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_name(name):
    return (
        re.sub(r"\(.*\)", "", strip_accents(name))
        .replace("Stajaliste ", "")
        .replace(" Stajaliste", "")
        .lower()
    )


def station_name_matches(osm_station, name):
    """
    Returns True if the station name does not contradict the station matching.
    Returns False if the two are unlikely to be the same.
    """
    match_name = normalize_name(name)
    matcher = SequenceMatcher(None, match_name)
    match = False
    for prop, value in osm_station["properties"].items():
        if prop.startswith("name") or prop.startswith("alt_name") or prop.startswith("int_name"):
            osm_name = normalize_name(value)
            matcher.set_seq2(osm_name)
            if matcher.ratio() > 0.75:
                match = True

            if osm_name and match_name and (osm_name in match_name or match_name in osm_name):
                match = True

    return match


def search_station(stations, stop, cache={}):
    osm_stop = Stop()

    if (stop.latitude, stop.longitude) in cache:
        return cache[(stop.latitude, stop.longitude)]

    candidates = []

    for station in stations:
        if (
            distance(
                (
                    station["geometry"]["coordinates"][1],
                    station["geometry"]["coordinates"][0],
                ),
                (stop.latitude, stop.longitude),
            )
            < 0.00002
            and station_name_matches(station, stop.name)
        ) or ("ref:ibnr" in station["properties"] and station["properties"]["ref:ibnr"] == stop.id):
            candidates.append(station)

    if candidates:
        osm_node = choose_best_osm_node(candidates, stop)

        osm_stop.name = station_name_fallback(osm_node)
        osm_stop.lat = osm_node["geometry"]["coordinates"][1]
        osm_stop.lon = osm_node["geometry"]["coordinates"][0]

        cache[(stop.latitude, stop.longitude)] = osm_stop

        return osm_stop
    else:
        print(
            f"Did not find {stop.name} ({stop.id}) in OSM data near {stop.latitude}, {stop.longitude}"
        )
        osm_stop.name = stop.name
        osm_stop.lat = stop.latitude
        osm_stop.lon = stop.longitude

        cache[(stop.latitude, stop.longitude)] = osm_stop

        return osm_stop


def mode_to_route_type(mode, route_type: Optional[str]):
    match mode:
        case Mode.BUS:
            return 3
        case Mode.TRAIN:
            match route_type:
                case "R":
                    return 106
                case "E":
                    return 106
                case "IC":
                    return 102
                case "EC":
                    return 102
                case "D":
                    return 102
                case None:
                    return 2
                case _:
                    print("Unknown train type", route_type)
                    return 2

    raise Exception("Unknown mode")


def service_id(trip_id):
    return sha256(("service" + trip.id).encode()).hexdigest()


def split_trip_name(name: str) -> Tuple[Optional[str], str]:
    parts = name.split(" ")
    if len(parts) >= 2 and parts[0].isalpha():
        return (parts[0], "".join(parts[1:]))
    else:
        return (None, name)


def time_to_gtfs(start_date, time):
    rel_time = time - datetime.datetime.combine(
        start_date,
        datetime.datetime.min.time(),
        tzinfo=time.tzinfo,
    )
    seconds = int(rel_time.total_seconds())
    result = f"{seconds // (60 * 60):02}:{seconds % (60 * 60) // 60:02}:{seconds % 60:02}"
    return result


def station_name_fallback(station):
    priority = ["name:sr-Latn", "name:en", "name"]

    for prop in priority:
        if prop in station["properties"]:
            return station["properties"][prop].strip(
                "[]"
            )  # Some railway stations are abandoned but trains still seem to stop there.
            # Strip away OSMs abandoned markers in the name.

    raise Exception(f"No matching property found in {station["properties"]}")


with open(sys.argv[1], "rb") as cf:
    config = tomllib.load(cf)
    print(config)

database_file = config["operator"]["id"] + ".sqlite"
(db, cur) = prepare_database(database_file)

for search_name in config["data"]["stations"]:
    print(f"# Fetching data for {search_name}")

    stations = get_stations()

    client = HafasClient(OEBBProfile())

    locations = client.locations(search_name)
    best_found_location = locations[0]

    search_name_id = search_name.replace(" ", "_")
    timestamp_file = f"latest_timestamp_{search_name_id}.txt"

    try:
        with open(timestamp_file, "r") as tf:
            timestamp = int(tf.read())
            latest_time = datetime.datetime.fromtimestamp(timestamp)
    except FileNotFoundError:
        latest_time = datetime.datetime.now()

    print(f"Starting at {latest_time}")

    cur.execute(
        """insert or replace into feed_info values ("Jonah Brüchert", "https://jbb.ghsq.de", ?, "jbb@kaidan.im")""",
        (config["operator"]["lang"],),
    )

    # Try to fetch until hafas complains
    departures: List[Leg] = []
    while True:
        try:
            args = dict(
                station=best_found_location.id,
                date=latest_time,
                max_trips=600,
                products={
                    "long_distance_express": True,
                    "regional": True,
                    "suburban": True,
                    "bus": False,
                    "ferry": False,
                    "subway": False,
                    "tram": False,
                    "taxi": False,
                },
            )
            departures = client.departures(**args)
            departures += client.arrivals(**args)
            if not departures:
                print("Stopping, because there are no departures / arrivals from the stop")
                break

            latest_departure = departures[-1].dateTime
            latest_arrival = departures[-1].dateTime

            operator_config = config["operator"]
            cur.execute(
                """insert or replace into agencies values (?, ?, ?, "Europe/Vienna", ?, NULL, ?)""",
                (
                    operator_config["id"],
                    operator_config["name"],
                    operator_config["url"],
                    operator_config["phone"],
                    operator_config["email"],
                ),
            )

            for departure in departures:
                trip = client.trip(departure.id)
                (route_type, trip_name) = split_trip_name(trip.name)

                start = search_station(stations, trip.stopovers[0].stop)
                dest = search_station(stations, trip.stopovers[-1].stop)

                cur.execute(
                    """insert or replace into routes values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trip.name,
                        operator_config["id"],
                        None,
                        f"{start.name} - {dest.name}",
                        None,
                        mode_to_route_type(trip.mode, route_type),
                        None,
                        operator_config["color"],
                        operator_config["text_color"],
                        None,
                    ),
                )
                cur.execute(
                    """insert or replace into trips values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trip.name,
                        service_id(trip.id),
                        sha256(trip.id.encode()).hexdigest(),
                        dest.name,
                        trip_name,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )

                if trip.cancelled:
                    cur.execute(
                        """insert or replace into calendar_dates values (?, ?, ?)""",
                        (service_id(trip.id), trip.departure.date().strftime("%Y%m%d"), 0),
                    )
                else:
                    cur.execute(
                        """insert or replace into calendar_dates values (?, ?, ?)""",
                        (service_id(trip.id), trip.departure.date().strftime("%Y%m%d"), 1),
                    )

                sequence = 1
                for stopover in trip.stopovers:
                    station_metadata = search_station(stations, stopover.stop)

                    cur.execute(
                        """insert or replace into stops values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            stopover.stop.id,
                            None,
                            station_metadata.name,
                            None,
                            None,
                            station_metadata.lat,
                            station_metadata.lon,
                            None,
                            None,
                            0,
                            None,
                            config["data"]["timezone"],
                            None,
                            None,
                            None,
                        ),
                    )
                    if not stopover.departure and not stopover.arrival:
                        print("Skipping", stopover.stop.name, "because it has neither arrival nor departure time")
                        continue

                    cur.execute(
                        """insert or replace into stop_times values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            sha256(trip.id.encode()).hexdigest(),
                            time_to_gtfs(
                                trip.departure.date(),
                                (stopover.arrival if stopover.arrival else stopover.departure),
                            ),
                            time_to_gtfs(
                                trip.departure.date(),
                                (stopover.departure if stopover.departure else stopover.arrival),
                            ),
                            stopover.stop.id,
                            None,
                            None,
                            sequence,
                            None,
                            None,
                            None,
                            1,  # exact times
                        ),
                    )
                    sequence += 1

            latest_time_end = min(
                latest_departure,
                latest_arrival,
            )
            print(f"Fetched until {latest_time_end}")

            print("Writing changes to database…")
            db.commit()

            if departures:
                with open(
                    timestamp_file,
                    "w",
                ) as tf:
                    tf.write(f"{int(latest_time_end.timestamp())}")

            if latest_time.timestamp() == latest_time_end.timestamp():
                print("Stopping because no more data is available")
                break

            latest_time = latest_time_end

        except (GeneralHafasError, KeyboardInterrupt) as e:
            print("Stopping because of", e)
            break


if not os.path.exists("out"):
    os.makedirs("out")

subprocess.run(
    ["sqlite3", "../" + database_file],
    input=""".headers on
.mode csv
.output stops.txt
select * from stops;
.output trips.txt
select * from trips;
.output routes.txt
select * from routes;
.output agency.txt
select * from agencies;
.output stop_times.txt
select * from stop_times;
.output calendar_dates.txt
select * from calendar_dates;
.output feed_info.txt
select * from feed_info;
""",
    text=True,
    check=True,
    cwd="out",
)

output_filename = f"{config["operator"]["id"]}.gtfs.zip"

files = list(map(lambda p: p.name, Path("out").glob("*.txt")))
subprocess.check_call(
    [
        "zip",
        "../" + output_filename,
    ]
    + files,
    cwd="out",
)

subprocess.check_call(
    [
        "gtfsclean",
        "--minimize-services",
        "--minimize-stoptimes",
        "--remove-red-routes",
        "--remove-red-services",
        "--remove-red-trips",
        "--red-trips-fuzzy",
        "--explicit-calendar",
        "--minimize-ids-char",
        "--keep-station-ids",
        "--delete-orphans",
        "--date-start",
        datetime.datetime.today().strftime("%Y%m%d"),
        output_filename,
        "--output",
        output_filename,
    ]
)

subprocess.check_call(["pfaedle", "--inplace", "-x", config["data"]["osm_shapes"], output_filename])
subprocess.check_call(["gtfsclean", "--compress", "--min-shapes", output_filename, "-o", output_filename])
