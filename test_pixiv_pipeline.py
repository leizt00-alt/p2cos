"""Offline regression tests: no live credentials, requests, or production state."""

from contextlib import redirect_stdout
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pixiv_pipeline as pipeline
from p2cos_state import State, inspect_image
from PIL import Image


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 8, 16, 5, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class OfflineClient:
    def __init__(self, pages=2):
        self.auth_calls = 0
        self.downloads = []
        self.item = {"id": 456, "title": "Offline", "user": {"name": "Artist"},
                     "page_count": pages, "type": "illust", "x_restrict": 0,
                     "meta_pages": [{"image_urls": {"original": f"https://i.pximg.net/456_p{i}.png"}}
                                    for i in range(pages)]}
        self.api = SimpleNamespace(illust_detail=lambda identifier: {"illust": self.item})

    def authenticate(self):
        self.auth_calls += 1

    def call(self, stage, method, *args, **kwargs):
        return method(*args, **kwargs)

    def download(self, url, target, stage):
        if target.exists():
            raise AssertionError("test client refuses file replacement")
        self.downloads.append(str(target))
        Image.new("RGB", (13, 17), "blue").save(target)
        return inspect_image(target)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="p2cos_offline_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.config = {"storage": {"state_db": str(self.root / "state.sqlite3"),
                                    "local_root": str(self.root / "artworks")},
                       "selection": {"visual_shortlist_limit": 10,
                                     "ranking_schedule": {"timezone": "Asia/Shanghai", "anchor_date": "2026-10-09", "cycle": ["month", "day"]}},
                       "publishing": {"platforms": ["xiaohongshu"]}}
        self.state = State(self.config["storage"]["state_db"], platforms=["xiaohongshu"])
        self.addCleanup(patch.stopall)
        patch.object(pipeline, "CANDIDATE_ROOT", self.cache).start()
        patch.object(pipeline, "datetime", FrozenDateTime).start()

    def shortlist(self, fetched_at):
        run = self.cache / "run_test"
        run.mkdir()
        manifest = run / "candidates.json"
        manifest.write_text(json.dumps({"fetched_at": fetched_at, "ranking_mode": "month",
            **pipeline.ranking_context(self.config, date(2026, 10, 9)), "candidates": [
            {"id": "456", "page_count": 2, "rank": 1, "preview_status": "decoded"}]}))
        shortlist = run / "approved_shortlist.json"
        shortlist.write_text(json.dumps({"candidate_manifest": str(manifest), "approved": [
            {"id": "456", "visual_reviewed": True, "platform_compatible": True, "reason": "Offline review"}]}))
        return shortlist

    def reserve(self):
        return self.state.reserve_daily([{"id": "456", "page_count": 2, "rank": 1,
                                          "reason": "Persisted original approval",
                                          "selection_decision": {"visual_reviewed": True,
                                                                 "source": "offline_fixture"}}],
                                        day="2026-10-07")

    def existing_page(self, job, page=0):
        folder = Path(self.config["storage"]["local_root"]) / "456"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"456_p{page}_raw.png"
        Image.new("RGB", (13, 17), "red").save(target)
        self.state.record_raw(job["job_id"], page, target)
        return target

    def test_bootstrap_has_no_installation_specific_archive_defaults(self):
        state = Mock()
        pipeline.bootstrap(state, self.config)
        self.assertEqual(state.bootstrap_history.call_args.kwargs["known_archived"], [])

    def test_configured_history_import_preserves_permanent_global_deduplication(self):
        selection = self.root / "selection.json"
        selection.write_text(json.dumps({"selected": [{"id": "123", "files": ["old.png"]}]}))
        storage = self.root / "storage.json"
        storage.write_text(json.dumps({"google_drive": {"artworks": {"234": {}}}}))
        raws = self.root / "old_raws"
        raws.mkdir()
        Image.new("RGB", (13, 17)).save(raws / "345_p0_raw.png")
        self.config["history_import"] = {"selection_path": str(selection), "storage_path": str(storage),
                                        "raw_roots": [str(raws)], "known_archived": ["567"]}
        job = self.reserve()
        with self.state._transaction() as db:
            db.execute("UPDATE jobs SET status='complete' WHERE job_id=?", (job["job_id"],))
            db.execute("UPDATE publications SET status='published',receipt_id='synthetic-receipt' WHERE job_id=?", (job["job_id"],))
        published = self.state.get_job(job["job_id"])
        result = pipeline.bootstrap(self.state, self.config)
        self.assertEqual(result["added"], 4)
        self.assertEqual(self.state.excluded_ids(), {"123", "234", "345", "456", "567"})
        self.config["history_import"]["known_archived"] = []
        self.assertEqual(pipeline.bootstrap(self.state, self.config)["added"], 0)
        self.assertIn("567", self.state.excluded_ids())
        self.assertEqual(self.state.get_job(job["job_id"]), published)
        self.assertIsNone(self.state.reserve_daily(
            [{"id": value, "page_count": 1} for value in self.state.excluded_ids()], day="2026-10-20"))

    def test_current_shanghai_day_accepts_and_copies_decision(self):
        candidate = pipeline.approved_candidate(self.shortlist("2026-10-08T16:00:00+00:00"), "456", self.config)
        self.assertEqual(candidate["selection_decision"]["selected_day"], "2026-10-09")
        self.assertEqual(candidate["selection_decision"]["approved_artwork_ids"], ["456"])
        self.assertTrue(candidate["selection_decision"]["visual_reviewed"])
        self.assertEqual(candidate["selection_decision"]["source_mode"], "month")
        self.assertEqual(candidate["selection_decision"]["provenance"]["api_mode"], "month")

    def test_calendar_alternates_by_day_even_after_gaps(self):
        for value, expected in (("2026-10-09", "month"), ("2026-10-10", "day"),
                                ("2026-10-11", "month"), ("2026-10-14", "day"),
                                ("2026-10-08", "day")):
            with self.subTest(day=value):
                self.assertEqual(pipeline.resolve_ranking_mode(self.config, value), expected)
        # UTC 16:00 is the Shanghai calendar rollover; wall clock server UTC
        # and job success/failure do not define the ranking day.
        self.assertEqual(pipeline.resolve_ranking_mode(self.config, datetime(2026, 10, 8, 15, 59, tzinfo=timezone.utc)), "day")
        self.assertEqual(pipeline.resolve_ranking_mode(self.config, datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)), "month")

    def test_manual_override_does_not_change_calendar(self):
        before = json.dumps(self.config, sort_keys=True)
        self.assertEqual(pipeline.resolve_ranking_mode(self.config, "2026-10-10", "month"), "month")
        self.assertEqual(pipeline.resolve_ranking_mode(self.config, "2026-10-10"), "day")
        self.assertEqual(json.dumps(self.config, sort_keys=True), before)

    def test_opposite_ranking_cache_requires_explicit_override(self):
        shortlist = self.shortlist("2026-10-08T16:00:00+00:00")
        manifest_path = Path(json.loads(shortlist.read_text())["candidate_manifest"])
        manifest = json.loads(manifest_path.read_text())
        manifest.update(pipeline.ranking_context(self.config, date(2026, 10, 9), "day"))
        manifest["ranking_mode"] = "day"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(pipeline.PipelineError) as error:
            pipeline.approved_candidate(shortlist, "456", self.config)
        self.assertEqual(error.exception.code, "candidate_ranking_mode_mismatch")
        candidate = pipeline.approved_candidate(shortlist, "456", self.config, "day")
        self.assertEqual(candidate["selection_decision"]["source_mode"], "day")
        self.assertEqual(candidate["selection_decision"]["reservation_manual_override"], "day")

    def test_provenance_cannot_mislabel_api_mode(self):
        shortlist = self.shortlist("2026-10-08T16:00:00+00:00")
        manifest_path = Path(json.loads(shortlist.read_text())["candidate_manifest"])
        manifest = json.loads(manifest_path.read_text())
        manifest["provenance"]["api_mode"] = "day"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(pipeline.PipelineError) as error:
            pipeline.approved_candidate(shortlist, "456", self.config)
        self.assertEqual(error.exception.code, "invalid_ranking_provenance")

    def test_month_api_parameter_and_manifest_provenance(self):
        self.config["selection"]["candidate_limit"] = 100
        self.config["publishing"]["xiaohongshu"] = {"observed_limits": {"max_images_per_post": 18}}
        client = OfflineClient()
        ranking_calls = []
        def ranking(mode, offset):
            ranking_calls.append((mode, offset))
            start = offset or 0
            return {"illusts": [{"id": 1000 + i, "title": "Offline monthly work", "user": {},
                                 "page_count": 19 if i == 2 else 1, "type": "illust", "x_restrict": 0,
                                 "image_urls": {"medium": f"https://i.pximg.net/{1000 + i}.png"}}
                                for i in range(start, start + 50)],
                    "next_url": "https://app-api.pixiv.net/v1/illust/ranking?mode=month&offset=50" if start == 0 else None}
        client.api.illust_ranking = ranking
        self.state.bootstrap_history(selection_path=self.root / "no-selection.json",
                                     storage_path=self.root / "no-storage.json",
                                     raw_roots=[], known_archived=["1000"])
        with patch.object(pipeline, "bootstrap"):
            result = pipeline.candidates(self.state, self.config, client, "month")
        manifest = json.loads(Path(result["candidate_manifest"]).read_text())
        self.assertEqual(ranking_calls, [("month", None), ("month", 50)])
        self.assertEqual(manifest["ranking_items_seen"], 100)
        self.assertEqual(manifest["source_mode"], "month")
        self.assertEqual(manifest["selection_date"], "2026-10-09")
        self.assertTrue(manifest["provenance"]["manual_override"])
        self.assertEqual(manifest["history_excluded"], 1)
        self.assertEqual(manifest["page_limit_excluded"], 1)
        self.assertNotIn("1000", [item["id"] for item in manifest["candidates"]])
        self.assertEqual(len(client.downloads), 98)
        self.assertIsNone(self.state.latest_job())

    def test_resume_preserves_original_ranking_source(self):
        original_decision = {"source_mode": "day", "selection_date": "2026-10-08",
                             "provenance": {"api_mode": "day"}, "visual_reviewed": True}
        job = self.state.reserve_daily([{"id": "456", "page_count": 2, "rank": 1,
                                          "selection_decision": original_decision}], day="2026-10-08")
        client = OfflineClient()
        with patch.object(pipeline, "resolve_ranking_mode", side_effect=AssertionError("resume must not switch source")):
            result = pipeline.resume_download(self.state, self.config, client, job["run_id"])
        self.assertEqual(result["job"]["source_mode"], "day")
        saved = json.loads(Path(result["metadata_path"]).read_text())
        self.assertEqual(saved["selection_decision"], original_decision)
        self.assertEqual(saved["source_mode"], "day")

    def test_previous_shanghai_day_rejected_without_reservation(self):
        args = SimpleNamespace(id="456", shortlist=self.shortlist("2026-10-08T15:59:59+00:00"))
        with patch.object(pipeline, "bootstrap"), self.assertRaises(pipeline.PipelineError) as error:
            pipeline.download_selected(self.state, self.config, OfflineClient(), args)
        self.assertEqual(error.exception.code, "stale_candidate_manifest")
        self.assertIsNone(self.state.latest_job())

    def test_ambiguous_timestamp_rejected(self):
        with self.assertRaises(pipeline.PipelineError) as error:
            pipeline.approved_candidate(self.shortlist("2026-10-09T00:00:00"), "456", self.config)
        self.assertEqual(error.exception.code, "invalid_manifest_timestamp")

    def test_resume_download_keeps_original_and_downloads_only_missing_page(self):
        job = self.reserve()
        old = self.existing_page(job)
        before = old.read_bytes()
        client = OfflineClient()
        result = pipeline.resume_download(self.state, self.config, client, job["run_id"])
        self.assertEqual(result["originals_downloaded"], 1)
        self.assertEqual(result["existing_originals_revalidated"], 1)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(len(client.downloads), 1)
        self.assertEqual(result["job"]["artwork_id"], "456")
        self.assertEqual(self.state.latest_job()["metadata"]["reason"], "Persisted original approval")
        self.assertEqual(self.state.latest_job()["required_platforms"], ["xiaohongshu"])
        saved_decision = self.state.latest_job()["metadata"]["selection_decision"]
        self.assertEqual(saved_decision, {"visual_reviewed": True, "source": "offline_fixture"})
        folder = Path(self.config["storage"]["local_root"]) / "456"
        self.assertEqual(json.loads((folder / "selection_decision.json").read_text()), saved_decision)
        self.assertEqual(json.loads((folder / "pixiv_metadata.json").read_text())["selection_decision"], saved_decision)

    def test_selected_resume_needs_no_old_cache_or_network_when_raw_complete(self):
        job = self.reserve()
        self.existing_page(job, 0)
        self.existing_page(job, 1)
        client = OfflineClient()
        args = SimpleNamespace(id="456", shortlist=self.root / "deleted_shortlist.json", run_id=job["run_id"])
        with patch.object(pipeline, "bootstrap"), patch.object(pipeline, "approved_candidate") as approval:
            result = pipeline.download_selected(self.state, self.config, client, args)
        approval.assert_not_called()
        self.assertEqual(result["existing_originals_revalidated"], 2)
        self.assertEqual(client.auth_calls, 0)
        self.assertEqual(client.downloads, [])

    def test_resume_empty_state_does_not_authenticate_or_reserve(self):
        client = OfflineClient()
        self.assertEqual(pipeline.resume_download(self.state, self.config, client)["status"], "no_existing_job")
        self.assertEqual(client.auth_calls, 0)
        self.assertIsNone(self.state.latest_job())

    def test_explicit_run_does_not_resume_unrelated_daily_job(self):
        old_job = self.reserve()
        shortlist = self.shortlist("2026-10-08T16:00:00+00:00")
        args = SimpleNamespace(id="456", shortlist=shortlist,
                               ranking_mode="month", run_id="manual-new-run")
        with patch.object(pipeline, "bootstrap"), \
             patch.object(self.state, "reserve_run", create=True, side_effect=[None, old_job]) as reserve, \
             patch.object(self.state, "reserve_daily", side_effect=AssertionError("wrong daily job")), \
             patch.object(pipeline, "download_job", return_value={"status": "test_download"}):
            result = pipeline.download_selected(self.state, self.config, OfflineClient(), args)
        self.assertEqual(result["status"], "test_download")
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(reserve.call_args_list[0].kwargs, {"run_id": "manual-new-run"})
        self.assertEqual(reserve.call_args_list[1].kwargs, {"run_id": "manual-new-run"})
        self.assertEqual(reserve.call_args_list[1].args[0][0]["selection_decision"]["source_mode"], "month")

    def test_explicit_run_resume_uses_its_own_slot(self):
        with patch.object(self.state, "reserve_run", create=True, return_value=None) as reserve, \
             patch.object(self.state, "reserve_daily", side_effect=AssertionError("wrong daily job")):
            result = pipeline.resume_download(self.state, self.config, OfflineClient(), "manual-new-run")
        self.assertEqual(result["status"], "no_existing_job")
        reserve.assert_called_once_with([], run_id="manual-new-run")

    def test_cli_passes_exact_configured_platforms_to_state(self):
        config_path = self.root / "workflow.json"
        config_path.write_text(json.dumps(self.config))
        with patch.object(pipeline, "State", wraps=State) as constructor, redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(["--config", str(config_path), "status"]), 0)
        constructor.assert_called_once_with(self.config["storage"]["state_db"], platforms=["xiaohongshu"])


if __name__ == "__main__":
    unittest.main()
