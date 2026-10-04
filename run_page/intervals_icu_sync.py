import argparse
import gzip
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import eviltransform
import gpxpy
import requests
from config import BASE_TIMEZONE, FOLDER_DICT, JSON_FILE, SQL_FILE
from generator import Generator
from generator.db import Activity, g
from gpxtrackposter.track_loader import load_fit_file, load_gpx_file, load_tcx_file
from intervals_icu_import import consolidate_duplicates, import_summaries, reconcile
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

BASE_URL = "https://intervals.icu/api/v1"
RUNNING_TYPES = ["Run", "VirtualRun", "TrailRun"]
TCX_NAMESPACE = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"


class IntervalsICU:
    def __init__(self, athlete_id, api_key):
        self.athlete_id = athlete_id
        self.api_key = api_key
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth("API_KEY", api_key)
        self.session.headers["Accept"] = "application/json"
        self.session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET"],
                )
            ),
        )

    def get_activities(self, oldest, newest):
        url = (
            f"{BASE_URL}/athlete/{self.athlete_id}/activities"
            f"?oldest={oldest}&newest={newest}"
        )
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise TypeError("Expected an activity list from Intervals.icu")
        return data

    def download_activity_file(self, activity_id, file_type, output_folder):
        url = f"{BASE_URL}/activity/{activity_id}/file"
        numeric_id = str(activity_id).lstrip("i")
        output_path = os.path.join(output_folder, f"{numeric_id}.{file_type}")

        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        content = response.content
        # Decompress only if gzip-compressed (magic bytes: 1f 8b)
        if content[:2] == bytes([0x1F, 0x8B]):
            content = gzip.decompress(content)
        if not content:
            raise ValueError(f"Empty activity file for {activity_id}")
        if file_type == "fit" and content[8:12] != b".FIT":
            raise ValueError(f"Invalid FIT file for {activity_id}")

        with open(output_path, "wb") as f:
            f.write(content)

        return output_path


def get_downloaded_ids(folder):
    return [i.split(".")[0] for i in os.listdir(folder) if not i.startswith(".")]


def hydrate_routes(session, candidates, mapping):
    """Attach file data to provider-mapped rows, including files arriving after summaries."""
    loaders = {"fit": load_fit_file, "gpx": load_gpx_file, "tcx": load_tcx_file}
    for raw, file_type in candidates:
        if not raw.get("distance"):
            continue
        path = os.path.join(
            FOLDER_DICT[file_type], f"{str(raw['id']).lstrip('i')}.{file_type}"
        )
        track = loaders[file_type](path)
        if not track.start_time or not track.length:
            raise ValueError(f"Could not parse distance activity {raw['id']}")
        activity = session.get(Activity, mapping[f"intervals_icu:{raw['id']}"])
        activity.summary_polyline = track.polyline_str or ""
        if track.subtype:
            activity.subtype = track.subtype
        if activity.average_heartrate is None and track.average_heartrate is not None:
            activity.average_heartrate = track.average_heartrate
        if activity.elevation_gain is None and track.elevation_gain is not None:
            activity.elevation_gain = track.elevation_gain
        if not activity.location_country and track.start_latlng:
            try:
                activity.location_country = str(
                    g.reverse(
                        f"{track.start_latlng.lat}, {track.start_latlng.lon}",
                        language="zh-CN",
                        timeout=15,
                    )
                )
            except Exception:  # noqa: BLE001
                print(f"Location unavailable for {raw['id']}; route is preserved")


def correct_gpx_gcj02(file_path):
    """Convert GCJ-02 coordinates to WGS-84 in a GPX file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)

        count = 0
        for track in gpx.tracks:
            for segment in track.segments:
                for point in segment.points:
                    wgs_lat, wgs_lng = eviltransform.gcj2wgs_exact(
                        point.latitude, point.longitude
                    )
                    point.latitude = wgs_lat
                    point.longitude = wgs_lng
                    count += 1

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(gpx.to_xml())

        print(f"  GCJ-02 → WGS-84: corrected {count} points in {file_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  Warning: Failed to correct GCJ-02 coordinates in {file_path}: {e}")


def correct_tcx_gcj02(file_path):
    """Convert GCJ-02 coordinates to WGS-84 in a TCX file."""
    try:
        ET.register_namespace("", TCX_NAMESPACE)

        tree = ET.parse(file_path)
        root = tree.getroot()
        ns = TCX_NAMESPACE

        count = 0
        for position in root.iter(f"{{{ns}}}Position"):
            lat_el = position.find(f"{{{ns}}}LatitudeDegrees")
            lng_el = position.find(f"{{{ns}}}LongitudeDegrees")
            if lat_el is None or lng_el is None:
                continue
            lat = float(lat_el.text)
            lng = float(lng_el.text)
            wgs_lat, wgs_lng = eviltransform.gcj2wgs_exact(lat, lng)
            lat_el.text = str(wgs_lat)
            lng_el.text = str(wgs_lng)
            count += 1

        tree.write(file_path, xml_declaration=True, encoding="UTF-8")
        print(f"  GCJ-02 → WGS-84: corrected {count} positions in {file_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  Warning: Failed to correct GCJ-02 coordinates in {file_path}: {e}")


def correct_fit_gcj02(file_path):
    """Convert GCJ-02 coordinates to WGS-84 in a FIT file."""
    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.profile.messages.record_message import RecordMessage

        fit = FitFile.from_file(file_path)
        count = 0
        for record in fit.records:
            msg = record.message
            if isinstance(msg, RecordMessage) and (
                msg.position_lat is not None and msg.position_long is not None
            ):
                wgs_lat, wgs_lng = eviltransform.gcj2wgs_exact(
                    msg.position_lat, msg.position_long
                )
                msg.position_lat = wgs_lat
                msg.position_long = wgs_lng
                count += 1
        fit.crc = None  # Reset CRC so it recalculates on write
        fit.to_file(file_path)
        print(f"  GCJ-02 → WGS-84: corrected {count} records in {file_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  Warning: Failed to correct GCJ-02 coordinates in {file_path}: {e}")


def correct_file_gcj02(file_path, file_type):
    """Apply GCJ-02 to WGS-84 coordinate correction based on file type."""
    if file_type == "gpx":
        correct_gpx_gcj02(file_path)
    elif file_type == "tcx":
        correct_tcx_gcj02(file_path)
    elif file_type == "fit":
        correct_fit_gcj02(file_path)


def run():
    parser = argparse.ArgumentParser(description="Sync activities from Intervals.icu")
    parser.add_argument("athlete_id", nargs="?", help="Intervals.icu athlete ID")
    parser.add_argument("api_key", nargs="?", help="Intervals.icu API key")
    parser.add_argument(
        "--start-date",
        default="2015-01-01",
        help="Oldest date to sync (YYYY-MM-DD, default: 2015-01-01)",
    )
    parser.add_argument(
        "--all",
        dest="sync_all",
        action="store_true",
        help="Sync all activity types, not just running",
    )
    parser.add_argument(
        "--gcj02",
        action="store_true",
        help="Convert GCJ-02 coordinates to WGS-84 in downloaded files",
    )
    options = parser.parse_args()

    athlete_id = options.athlete_id or os.environ.get("INTERVALS_ICU_ATHLETE_ID")
    api_key = options.api_key or os.environ.get("INTERVALS_ICU_API_KEY")
    if not athlete_id or not api_key:
        parser.error("athlete ID and API key are required")

    today = datetime.now(ZoneInfo(BASE_TIMEZONE)).strftime("%Y-%m-%d")
    client = IntervalsICU(athlete_id, api_key)
    activities = client.get_activities(oldest=options.start_date, newest=today)
    report_path = os.path.join(os.path.dirname(JSON_FILE), "sync_status.json")
    if not activities and os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as f:
            previous = json.load(f)
        if previous.get("start_date") == options.start_date and previous.get(
            "source_count"
        ):
            raise RuntimeError("Upstream returned no activities after a populated sync")
    fetched_count = len(activities)

    if not options.sync_all:
        activities = [
            activity for activity in activities if activity.get("type") in RUNNING_TYPES
        ]

    in_scope_count = len(activities)
    # Only activities with a declared file type and supported folder mapping
    candidates = []
    skipped_no_file = 0
    skipped_unsupported_file = 0
    for activity in activities:
        file_type = activity.get("file_type")
        if not file_type:
            skipped_no_file += 1
            continue
        file_type = str(file_type).lower()
        if file_type not in FOLDER_DICT:
            skipped_unsupported_file += 1
            continue
        candidates.append((activity, file_type))

    downloaded_count = 0
    failed_downloads = []
    total = len(candidates)
    print(
        f"Intervals.icu: fetched {fetched_count}, in scope {in_scope_count}, "
        f"supported files {total}, no file {skipped_no_file}, "
        f"unsupported file {skipped_unsupported_file}"
    )

    for n, (activity, file_type) in enumerate(candidates, start=1):
        output_folder = FOLDER_DICT[file_type]
        numeric_id = str(activity["id"]).lstrip("i")
        output_path = os.path.join(output_folder, f"{numeric_id}.{file_type}")

        if os.path.exists(output_path):
            continue

        activity_id = activity["id"]
        print(f"Downloading activity {activity_id} ({n}/{total})...")

        try:
            saved_path = client.download_activity_file(
                activity_id, file_type, output_folder
            )
            downloaded_count += 1
            if options.gcj02:
                correct_file_gcj02(saved_path, file_type)
        except Exception as e:  # noqa: BLE001
            print(f"Error: Failed to download activity {activity_id}: {e}")
            failed_downloads.append(activity_id)

        time.sleep(1)

    if failed_downloads:
        raise RuntimeError(f"Failed to download {len(failed_downloads)} activities")

    generator = Generator(SQL_FILE)
    mapping, created = import_summaries(generator.session, activities)
    hydrate_routes(generator.session, candidates, mapping)
    file_sizes = {
        f"intervals_icu:{raw['id']}": os.path.getsize(
            os.path.join(
                FOLDER_DICT[file_type], f"{str(raw['id']).lstrip('i')}.{file_type}"
            )
        )
        for raw, file_type in candidates
    }
    consolidate_duplicates(generator.session, mapping, file_sizes)
    generator.session.commit()
    exported = generator.load(include_zero=True, virtual_indoor_routes=False)
    report = reconcile(
        activities,
        exported,
        mapping,
        options.start_date,
        datetime.now(UTC).isoformat(),
    )
    report["files_downloaded"] = downloaded_count
    report["files_supported"] = total
    report["files_unavailable"] = skipped_no_file + skipped_unsupported_file
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(exported, f, ensure_ascii=False)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    summary = (
        f"## Intervals.icu reconciliation\n"
        f"- Period: {options.start_date} to {today}\n"
        f"- Source activities: {in_scope_count}; matched: {len(mapping)}; missing: 0\n"
        f"- New summaries: {created}; exported history: {len(exported)}\n"
        f"- Exact duplicate sources grouped: {report['deduplicated_count']}\n"
        f"- Activity types: {json.dumps(report['by_type'])}\n"
        f"- Supported files: {total}; downloaded: {downloaded_count}; "
        f"unavailable: {report['files_unavailable']}\n"
    )
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)
    print(
        f"Done. Downloaded {downloaded_count} new activities. "
        f"Exported {len(exported)} activities; "
        f"{len(mapping)}/{in_scope_count} source activities reconciled."
    )


if __name__ == "__main__":
    run()
