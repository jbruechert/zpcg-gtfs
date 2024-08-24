# SPDX-FileCopyrightText: 2024 Jonah Brüchert <jbb@kaidan.im>
#
# SPDX-License-Identifier: AGPL-3.0-only

import datetime
from typing import List, Tuple
import json
import sqlite3
import re

from pyhafas import HafasClient
from pyhafas.profile import DBProfile
from pyhafas.types.fptf import Leg, Mode


def prepare_database():
    db = sqlite3.connect("gtfs.sqlite")
    cur = db.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS `agencies` (`agency_id` text PRIMARY KEY NOT NULL, `agency_name` text NOT NULL, `agency_url` text NOT NULL, `agency_timezone` text NOT NULL, `agency_phone` text DEFAULT NULL, `agency_fare_url` text DEFAULT NULL,  `agency_email` text DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `calendar_dates` (`service_id` text NOT NULL, `date` integer NOT NULL,`exception_type` integer NOT NULL, PRIMARY KEY(`service_id`, `date`));
    CREATE TABLE IF NOT EXISTS `routes` (`route_id` text PRIMARY KEY NOT NULL, `agency_id` text NOT NULL, `route_short_name` text DEFAULT NULL, `route_long_name` text DEFAULT NULL, `route_desc` text DEFAULT NULL, `route_type` smallint NOT NULL, `route_url` text DEFAULT NULL, `route_color` text DEFAULT NULL, `route_text_color` text DEFAULT NULL, `route_sort_order` smallint DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `stops` (`stop_id` text PRIMARY KEY NOT NULL, `stop_code` text DEFAULT NULL, `stop_name` text NOT NULL, `tts_stop_name` text DEFAULT NULL, `stop_desc` text DEFAULT NULL, `stop_lat` real NOT NULL, `stop_lon` real NOT NULL, `zone_id` text DEFAULT NULL, `stop_url` text DEFAULT NULL, `location_type` integer DEFAULT NULL, `parent_station` text DEFAULT NULL, `stop_timezone`, `wheelchair_boarding` integer DEFAULT NULL, `level_id` text DEFAULT NULL, `platform_code` text DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS `stop_times` (`trip_id` text NOT NULL, `arrival_time` text DEFAULT NULL, `departure_time` text DEFAULT NULL, `stop_id` text NOT NULL, `location_group_id` text, `location_id` text, `stop_sequence` smallint NOT NULL, `stop_headsign` text DEFAULT NULL, `pickup_type` integer DEFAULT NULL, `drop_off_type` integer DEFAULT NULL, `timepoint` integer DEFAULT NULL, PRIMARY KEY(`trip_id`, `stop_sequence`));
    CREATE TABLE IF NOT EXISTS `trips` (`route_id` text NOT NULL, `service_id` text NOT NULL, `trip_id` text PRIMARY KEY NOT NULL, `trip_headsign` text DEFAULT NULL, `trip_short_name` text DEFAULT NULL, `direction_id` integer DEFAULT NULL, `block_id` text DEFAULT NULL, `shape_id` text DEFAULT NULL, `wheelchair_accessible` integer DEFAULT NULL, `bikes_allowed` integer DEFAULT NULL);
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


def search_station(stations, lat: float, lon: float):
    for station in stations:
        if (
            distance(
                (
                    station["geometry"]["coordinates"][1],
                    station["geometry"]["coordinates"][0],
                ),
                (lat, lon),
            )
            < 0.000032
        ) and not "abandoned:railway" in station["properties"]:
            return station

    raise Exception("Station not found")

def mode_to_route_type(mode):
    match mode:
        case Mode.BUS:
            return 3
        case Mode.TRAIN:
            return 2

    raise Exception("Unknown mode")


def service_id(trip_id):
    return hash("service" + trip.id)


def clean_name(db_name):
    return re.sub(r"\([A-Z]*\)", "", stopover.stop.name)


def time_to_gtfs(start_date, time):
    rel_time = time - datetime.datetime.combine(
        start_date, datetime.datetime.min.time(), tzinfo=time.tzinfo
    )
    seconds = int(rel_time.total_seconds())
    result = (
        f"{seconds // (60 * 60):02}:{seconds % (60 * 60) // 60:02}:{seconds % 60:02}"
    )
    return result


def station_name_fallback(station):
    priority = ["name:sr-Latn", "name:en", "name"]

    for prop in priority:
        if prop in station["properties"]:
            return station["properties"][prop]

    raise Exception(f"No matching property found in {station["properties"]}")


db, cur = prepare_database()

stations = get_stations()

client = HafasClient(DBProfile())

locations = client.locations("Podgorica")
best_found_location = locations[0]

try:
    with open("latest_timestamp.txt", "r") as tf:
        timestamp = int(tf.read())
        latest_time = datetime.datetime.fromtimestamp(timestamp)
except FileNotFoundError:
    latest_time = datetime.datetime.now()

print(f"Starting at {latest_time}")

departures: List[Leg] = client.departures(
    station=best_found_location.id,
    date=latest_time,
    max_trips=600,
    products={
        "long_distance_express": True,
        "regional_express": True,
        "regional": True,
        "suburban": True,
        "bus": False,
        "ferry": False,
        "subway": False,
        "tram": False,
        "taxi": False,
    },
)

cur.execute(
    """insert or replace into agencies values ("zpcg", "Željeznički prevoz Crne Gore", "https://zpgc.me", "Europe/Berlin", "+382 20 441 197", NULL, "info@zpcg.me")"""
)

for departure in departures:
    trip = client.trip(departure.id)
    cur.execute(
        """insert or replace into routes values (?, "zpcg", ?, NULL, NULL, ?, NULL, NULL, NULL, NULL)""",
        (trip.name, trip.name, mode_to_route_type(trip.mode)),
    )
    cur.execute(
        """insert or replace into trips values (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)""",
        (trip.name, service_id(trip.id), hash(trip.id)),
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
        station_metadata = search_station(
            stations, stopover.stop.latitude, stopover.stop.longitude
        )
        name = (station_name_fallback(station_metadata))
        if name.startswith("["):
            print(name)
        cur.execute(
            """insert or replace into stops values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stopover.stop.id,
                None,
                station_name_fallback(station_metadata),
                None,
                None,
                station_metadata["geometry"]["coordinates"][1],
                station_metadata["geometry"]["coordinates"][0],
                None,
                None,
                0,
                None,
                "Europe/Podgorica",
                None,
                None,
                None,
            ),
        )
        cur.execute(
            """insert or replace into stop_times values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hash(trip.id),
                time_to_gtfs(
                    trip.departure.date(),
                    stopover.arrival if stopover.arrival else stopover.departure,
                ),
                time_to_gtfs(
                    trip.departure.date(),
                    stopover.departure if stopover.departure else stopover.arrival,
                ),
                stopover.stop.id,
                None,
                None,
                sequence,
                None,
                None,
                None,
                None,
            ),
        )
        sequence += 1

db.commit()

with open("latest_timestamp.txt", "w") as tf:
    tf.write(f"{int(departures[-1].dateTime.timestamp())}")