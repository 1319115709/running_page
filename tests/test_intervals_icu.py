import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from generator import Generator
from generator.db import Activity, init_db
from intervals_icu_import import (
    activity_values,
    consolidate_duplicates,
    import_summaries,
    reconcile,
)
from intervals_icu_sync import hydrate_routes


def summary(activity_id="i42", **changes):
    raw = {
        "id": activity_id,
        "type": "WeightTraining",
        "start_date_local": "2026-09-24T16:00:00",
        "distance": None,
        "moving_time": 0,
        "elapsed_time": 3600,
        "name": "Strength",
    }
    return raw | changes


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.db"
        self.session = init_db(self.path)

    def tearDown(self):
        self.session.close()
        self.session.bind.dispose()
        self.temp.cleanup()

    def test_zero_distance_is_exported_with_duration_and_repeated_sync_is_safe(self):
        mapping, created = import_summaries(self.session, [summary()])
        self.session.commit()
        self.assertEqual(created, 1)
        self.assertEqual(import_summaries(self.session, [summary()]), (mapping, 0))
        self.assertEqual(self.session.query(Activity).count(), 1)
        generator = Generator(self.path)
        self.assertEqual(generator.load(), [])
        exported = generator.load(include_zero=True)
        self.assertEqual(exported[0]["distance"], 0)
        self.assertEqual(exported[0]["moving_time"], "1:00:00")
        report = reconcile([summary()], exported, mapping, "2026-05-25", "now")
        self.assertEqual(report["matched_count"], 1)
        generator.session.close()
        generator.session.bind.dispose()

    def test_existing_strava_strength_record_is_reused(self):
        self.session.add(
            Activity(
                run_id=123,
                type="WeightTraining",
                start_date_local="2026-09-24 16:00:00",
                distance=0,
                moving_time=timedelta(seconds=3599),
            )
        )
        self.session.commit()
        mapping, created = import_summaries(self.session, [summary()])
        self.assertEqual(created, 0)
        self.assertEqual(mapping["intervals_icu:i42"], 123)

    def test_fit_walking_type_and_route_are_preserved(self):
        self.session.add(
            Activity(
                run_id=789,
                type="walking",
                start_date_local="2026-09-24 16:00:00",
                distance=1000,
                summary_polyline="route",
                elevation_gain=12,
            )
        )
        self.session.commit()
        mapping, created = import_summaries(
            self.session, [summary(type="Walk", distance=1005)]
        )
        self.assertEqual((mapping["intervals_icu:i42"], created), (789, 0))
        activity = self.session.get(Activity, 789)
        self.assertEqual(activity.type, "Walk")
        self.assertEqual(activity.summary_polyline, "route")
        self.assertEqual(activity.elevation_gain, 12)

    def test_short_and_nearby_activities_are_not_discarded_or_merged(self):
        first = summary(type="VirtualRun", distance=50)
        second = summary("i43", type="Run", start_date_local="2026-09-24T16:00:20")
        mapping, created = import_summaries(self.session, [first, second])
        self.assertEqual(created, 2)
        self.assertEqual(len(set(mapping.values())), 2)
        self.assertEqual(self.session.get(Activity, -42).distance, 50)

    def test_source_id_survives_metadata_edits(self):
        mapping, _ = import_summaries(self.session, [summary()])
        updated = summary(start_date_local="2026-09-25T16:00:00", name="Edited")
        self.assertEqual(import_summaries(self.session, [updated]), (mapping, 0))
        self.assertEqual(self.session.get(Activity, -42).name, "Edited")

    def test_export_mismatch_and_duplicate_upstream_ids_fail(self):
        mapping, _ = import_summaries(self.session, [summary()])
        with self.assertRaises(RuntimeError):
            reconcile([summary()], [], mapping, "2026-05-25", "now")
        with self.assertRaises(ValueError):
            import_summaries(self.session, [summary(), summary()])

    def test_local_and_explicit_utc_times_and_invalid_metrics(self):
        values = activity_values(summary())
        self.assertEqual(values["start_date"], "2026-09-24 08:00:00")
        self.assertEqual(values["start_date_local"], "2026-09-24 16:00:00")
        values = activity_values(summary(start_date="2026-09-24T07:00:00Z"))
        self.assertEqual(values["start_date"], "2026-09-24 07:00:00")
        for metric in [float("nan"), -10, float("inf")]:
            with self.assertRaises(ValueError):
                activity_values(summary(distance=metric))

    def test_schema_migration_persists_new_columns(self):
        legacy = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE activities (run_id INTEGER PRIMARY KEY)")
        session = init_db(legacy)
        session.close()
        session.bind.dispose()
        with sqlite3.connect(legacy) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(activities)")}
        self.assertTrue({"source", "source_id", "elevation_gain"} <= columns)

    def test_file_arriving_later_attaches_to_same_summary(self):
        raw = summary(type="Walk", distance=1000)
        mapping, _ = import_summaries(self.session, [raw])
        track = SimpleNamespace(
            start_time=True,
            length=1000,
            polyline_str="route",
            subtype="generic",
            average_heartrate=120,
            elevation_gain=10,
            start_latlng=None,
        )
        with patch("intervals_icu_sync.load_fit_file", return_value=track):
            hydrate_routes(self.session, [(raw, "fit")], mapping)
            hydrate_routes(self.session, [(raw, "fit")], mapping)
        self.assertEqual(self.session.query(Activity).count(), 1)
        self.assertEqual(self.session.get(Activity, -42).summary_polyline, "route")
        self.assertEqual(self.session.get(Activity, -42).average_heartrate, 120)

    def test_unparseable_distance_file_blocks_publication(self):
        raw = summary(type="Run", distance=1000)
        mapping, _ = import_summaries(self.session, [raw])
        with (
            patch(
                "intervals_icu_sync.load_fit_file",
                return_value=SimpleNamespace(start_time=None, length=0),
            ),
            self.assertRaises(ValueError),
        ):
            hydrate_routes(self.session, [(raw, "fit")], mapping)

    def test_indoor_export_does_not_fabricate_gps_routes(self):
        raw = summary(type="VirtualRun", distance=5000)
        import_summaries(self.session, [raw])
        self.session.commit()
        generator = Generator(self.path)
        with patch.object(generator, "_fix_indoor_locations") as virtual_routes:
            rows = generator.load(include_zero=True, virtual_indoor_routes=False)
            virtual_routes.assert_not_called()
        self.assertFalse(rows[0]["summary_polyline"])
        self.assertEqual(rows[0]["distance"], 5000)
        generator.session.close()
        generator.session.bind.dispose()

    def test_duplicate_sources_are_grouped_without_deleting_original_records(self):
        first = summary(type="Run", distance=5000)
        second = summary("i43", type="VirtualRun", distance=5000, average_heartrate=150)
        nearby = summary(
            "i44", type="Run", distance=5000, start_date_local="2026-09-24T16:00:20"
        )
        source = [first, second, nearby]
        mapping, _ = import_summaries(self.session, source)
        sizes = {"intervals_icu:i42": 600, "intervals_icu:i43": 30000}
        self.assertEqual(consolidate_duplicates(self.session, mapping, sizes), 1)
        self.session.commit()
        self.assertEqual(self.session.query(Activity).count(), 3)
        self.assertEqual(self.session.get(Activity, -42).duplicate_of, -43)
        generator = Generator(self.path)
        rows = generator.load(include_zero=True, virtual_indoor_routes=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["distance"] for row in rows), 10000)
        report = reconcile(source, rows, mapping, "2026-05-25", "now")
        self.assertEqual(report["matched_count"], 3)
        self.assertEqual(report["deduplicated_count"], 1)
        mapping, created = import_summaries(self.session, source)
        self.assertEqual(created, 0)
        self.assertEqual(consolidate_duplicates(self.session, mapping, sizes), 1)
        generator.session.close()
        generator.session.bind.dispose()


if __name__ == "__main__":
    unittest.main()
