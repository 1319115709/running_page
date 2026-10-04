"""Import summaries independently of GPS/FIT availability and reconcile by source ID."""

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from config import BASE_TIMEZONE
from generator.db import Activity

RUNNING_TYPES = {"Run", "VirtualRun", "TrailRun"}
FIT_TYPES = {"walking": "Walk", "cycling": "Ride", "swimming": "Swim", "hiking": "Hike"}


def number(value, default=0):
    if value is None:
        return default
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid activity metric")
    return result


def activity_values(raw):
    source_id = f"intervals_icu:{raw['id']}"
    local = datetime.fromisoformat(raw["start_date_local"])
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(BASE_TIMEZONE))
    utc = (
        datetime.fromisoformat(raw["start_date"])
        if raw.get("start_date")
        else local.astimezone(UTC)
    )
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=UTC)
    sport = raw.get("type") or "Workout"
    distance = number(raw.get("distance"))
    duration = number(raw.get("moving_time") or raw.get("elapsed_time"))
    return {
        "source": "intervals_icu",
        "source_id": source_id,
        "name": raw.get("name") or sport,
        "type": "Run" if sport in RUNNING_TYPES else sport,
        "start_date": utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "start_date_local": local.strftime("%Y-%m-%d %H:%M:%S"),
        "distance": distance,
        "moving_time": timedelta(seconds=duration),
        "elapsed_time": timedelta(seconds=number(raw.get("elapsed_time"), duration)),
        "average_heartrate": number(raw.get("average_heartrate"), None),
        "average_speed": number(
            raw.get("average_speed"), distance / duration if duration else 0
        ),
        "elevation_gain": number(raw.get("total_elevation_gain"), None),
    }


def import_summaries(session, activities):
    """Reuse an exact historic timestamp/type/distance match; never merge nearby starts."""
    mapping = {}
    created = 0
    for raw in activities:
        values = activity_values(raw)
        source_id = values["source_id"]
        if source_id in mapping:
            raise ValueError(f"Duplicate upstream ID: {source_id}")
        activity = session.query(Activity).filter_by(source_id=source_id).one_or_none()
        if activity is None:
            candidates = (
                session.query(Activity)
                .filter_by(start_date_local=values["start_date_local"])
                .all()
            )
            matches = [
                item
                for item in candidates
                if not item.source_id
                and FIT_TYPES.get(item.type, item.type) == values["type"]
                and abs((item.distance or 0) - values["distance"])
                <= max(20, values["distance"] * 0.01)
            ]
            if len(matches) > 1:
                raise ValueError(f"Ambiguous historic match: {source_id}")
            if matches:
                activity = matches[0]
            else:
                # Negative provider IDs cannot collide with positive Strava/FIT IDs.
                activity = Activity(
                    run_id=-int(str(raw["id"]).lstrip("i")),
                    summary_polyline="",
                    location_country="",
                    subtype="indoor" if raw.get("type") == "VirtualRun" else "",
                )
                session.add(activity)
                created += 1
        for key, value in values.items():
            # A missing optional summary field must not erase a value read from FIT.
            if value is not None:
                setattr(activity, key, value)
        mapping[source_id] = activity.run_id
        session.flush()
    return mapping, created


def consolidate_duplicates(session, mapping, file_sizes):
    """Hide exact positive-distance duplicates without deleting any source rows."""
    groups = defaultdict(list)
    for activity in session.query(Activity).filter_by(source="intervals_icu"):
        if not activity.distance or activity.distance <= 0:
            continue
        fingerprint = (
            activity.start_date_local,
            activity.start_date,
            activity.type,
            activity.distance,
        )
        groups[fingerprint].append(activity)
    for group in groups.values():
        canonical = max(
            group,
            key=lambda a: (
                file_sizes.get(a.source_id, 0),
                a.average_heartrate is not None,
                a.run_id > 0,
                -abs(a.run_id),
            ),
        )
        for activity in group:
            activity.duplicate_of = None if activity is canonical else canonical.run_id
            if activity.source_id in mapping:
                mapping[activity.source_id] = canonical.run_id
    session.flush()
    return len(mapping) - len(set(mapping.values()))


def reconcile(activities, exported, mapping, start_date, checked_at):
    rows = {row["run_id"]: row for row in exported}
    missing = [
        source_id
        for source_id, run_id in mapping.items()
        if run_id not in rows
        or source_id
        not in rows[run_id].get("source_ids", [rows[run_id].get("source_id")])
    ]
    if missing or len(mapping) != len(activities) or len(rows) != len(exported):
        raise RuntimeError("Activity export does not match upstream source IDs")
    return {
        "provider": "intervals_icu",
        "checked_at": checked_at,
        "start_date": start_date,
        "source_count": len(activities),
        "matched_count": len(mapping),
        "missing_count": len(missing),
        "by_type": dict(
            sorted(Counter(a.get("type") or "Workout" for a in activities).items())
        ),
        "exported_count": len(exported),
        "deduplicated_count": len(mapping) - len(set(mapping.values())),
        "latest_activity": max(
            (a["start_date_local"] for a in activities), default=None
        ),
    }
