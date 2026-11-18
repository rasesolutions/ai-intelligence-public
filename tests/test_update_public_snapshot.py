"""Regression checks for snapshot safety and replacement."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("snapshot", Path(__file__).parents[1] / "tools/update_public_snapshot.py")
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


def example():
    primary = {"url": "https://publisher.example/story", "title": "Publisher report", "kind": "primary"}
    entry = {
        "id": "urn:rase:ai:record:A", "url": snapshot.ORIGIN + "/ai/research/profile-a",
        "title": "Checked title", "summary": "Verified summary.", "content_text": "Verified details.",
        "date_published": "2026-11-17T13:01:02Z", "date_modified": "2026-11-18T01:02:03Z",
        "external_url": primary["url"], "tags": ["Research"],
        "_rase": {"event_type": "announcement", "original_event_date":
            {"value": "2026-10-02", "precision": "day", "certainty": "verified"},
            "website_publication_scope": "profile", "website_update_scope": "reviewed_event_publication",
            "summary_scope": "event", "sources": [primary]}}
    independent = copy.deepcopy(entry)
    independent["id"] = "urn:rase:ai:record:B"
    independent["url"] = snapshot.ORIGIN + "/ai/research/profile-b"
    independent.pop("external_url")
    independent["_rase"]["sources"][0]["kind"] = "independent"
    independent["_rase"]["original_event_date"] = {"value": "2026-10", "precision": "month", "certainty": "verified"}
    return {"version": "https://jsonfeed.org/version/1.1", "title": "Rase public feed",
            "home_page_url": snapshot.ORIGIN + "/ai/latest", "feed_url": snapshot.SOURCE,
            "language": "en", "description": "Checked descriptions.",
            "authors": [{"name": "Rase AI Intelligence", "url": snapshot.ORIGIN + "/ai"}],
            "items": [entry, independent]}


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.output = self.folder / "output"
        self.input = self.folder / "source.json"

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, value):
        self.input.write_text(json.dumps(value), encoding="utf-8")

    def update(self, value):
        self.save(value)
        return snapshot.update(self.output, self.input)

    def state(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.output.iterdir()}

    def test_fifty_profiles_and_exact_projection(self):
        value = example()
        items = []
        for index in range(50):
            entry = copy.deepcopy(value["items"][index % 2])
            entry["id"] = f"urn:rase:ai:record:R-{index}"
            entry["url"] = snapshot.ORIGIN + f"/ai/research/profile-{index}"
            items.append(entry)
        value["items"] = items
        self.assertEqual(self.update(value), "updated")
        current = json.loads((self.output / "latest.json").read_text())
        self.assertEqual(current, value)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["item_count"], 50)
        self.assertEqual(len(manifest["content_sha256"]), 64)
        self.assertNotIn("dropped", manifest)

    def test_original_sample_when_available(self):
        sample = Path(__file__).parents[1] / "data/latest.json"
        if not sample.exists():
            self.skipTest("No captured data/latest.json in this checkout.")
        value = json.loads(sample.read_text(encoding="utf-8"))
        self.assertEqual(snapshot.validate(value), value)

    def test_unchanged_does_not_touch_files(self):
        value = example()
        self.update(value)
        before = self.state()
        self.input.write_text(json.dumps(value, indent=4), encoding="utf-8")
        self.assertEqual(snapshot.update(self.output, self.input), "unchanged")
        self.assertEqual(self.state(), before)

    def test_withdrawal_then_empty_replaces(self):
        value = example()
        self.update(value)
        value["items"].pop(0)
        self.update(value)
        current = json.loads((self.output / "latest.json").read_text())
        self.assertEqual(len(current["items"]), 1)
        self.assertNotIn("urn:rase:ai:record:A", (self.output / "latest.json").read_text())
        value["items"].clear()
        self.update(value)
        self.assertEqual(json.loads((self.output / "latest.json").read_text())["items"], [])
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["item_count"], 0)
        self.assertEqual(manifest["date_bounds"], {"earliest": None, "latest": None})

    def test_dates_are_exact_and_capture_is_separate(self):
        value = example()
        self.update(value)
        current = json.loads((self.output / "latest.json").read_text())
        for original, stored in zip(value["items"], current["items"]):
            self.assertEqual(original["date_published"], stored["date_published"])
            self.assertEqual(original["date_modified"], stored["date_modified"])
            self.assertEqual(original["_rase"]["original_event_date"], stored["_rase"]["original_event_date"])
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertNotEqual(manifest["captured_at_utc"], value["items"][0]["date_published"])
        self.assertEqual(manifest["date_bounds"]["latest"]["precision"], "month")

    def test_profile_summary_and_encoded_identifier(self):
        value = example()
        value["items"][0]["_rase"]["summary_scope"] = "profile"
        value["items"][0]["id"] = "urn:rase:ai:record:A%3AB"
        stored = snapshot.validate(value)
        self.assertEqual(stored, value)
        value["items"][0]["id"] = "urn:rase:ai:record:A%2FB"
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(value)

    def test_noncanonical_bytes_are_repaired(self):
        self.update(example())
        manifest = json.loads((self.output / "manifest.json").read_text())
        data = json.loads((self.output / "latest.json").read_text())
        (self.output / "latest.json").write_text(json.dumps(data, indent=4), encoding="utf-8")
        self.assertEqual(snapshot.update(self.output, self.input), "updated")
        actual = (self.output / "latest.json").read_bytes()
        repaired = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(repaired["content_sha256"], snapshot.hashlib.sha256(actual).hexdigest())
        self.assertEqual(repaired["captured_at_utc"], manifest["captured_at_utc"])

    def test_damaged_manifest_is_repaired(self):
        self.update(example())
        correct = json.loads((self.output / "manifest.json").read_text())
        corruptions = {"source_url": "wrong", "item_count": -1,
                       "date_bounds": None, "captured_at_utc": "invalid"}
        for field, value in corruptions.items():
            broken = copy.deepcopy(correct)
            broken[field] = value
            (self.output / "manifest.json").write_text(json.dumps(broken), encoding="utf-8")
            self.assertEqual(snapshot.update(self.output, self.input), "updated")
            fixed = json.loads((self.output / "manifest.json").read_text())
            self.assertNotEqual(fixed[field], value)

    def test_new_keys_rejected_at_each_level(self):
        value = example()
        targets = [value, value["authors"][0], value["items"][0], value["items"][0]["_rase"],
                   value["items"][0]["_rase"]["original_event_date"], value["items"][0]["_rase"]["sources"][0]]
        for target in targets:
            target["raw"] = "SECRET"
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)
            del target["raw"]

    def test_url_injections_and_source_kind_rejected(self):
        for bad in ("https://evil.example/ai/research/a", "https://rasesolutions.com/ai/admin/private",
                    "https://rasesolutions.com/ai/research/a?secret=x", "javascript:alert(1)",
                    "https://user:password@rasesolutions.com/ai/research/a"):
            value = example()
            value["items"][0]["url"] = bad
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)
        for bad in ("javascript:alert(1)", "https://user:password@publisher.example/a",
                    "https://@publisher.example/a", "https://127.0.0.1/a",
                    "https://rasesolutions.com/ai/admin/private", "https://publisher.example/a?token=secret"):
            value = example()
            value["items"][0]["_rase"]["sources"][0]["url"] = bad
            value["items"][0]["external_url"] = bad
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)
        value = example()
        value["items"][0]["external_url"] = "https://another.example/report"
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(value)

    def test_primary_link_requires_primary_kind(self):
        value = example()
        value["items"][0]["_rase"]["sources"][0]["kind"] = "independent"
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(value)

    def test_date_and_private_type_rejected(self):
        for key, bad in (("date_published", "2026-02-31T01:01:01Z"), ("date_modified", "2026-01-01T01:01:01Z")):
            value = example()
            value["items"][0][key] = bad
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)
        value = example()
        value["items"][0]["_rase"]["original_event_date"]["certainty"] = "planned"
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(value)

    def test_invalid_and_network_failure_preserve(self):
        self.update(example())
        before = self.state()
        for content in (b"{", b"x" * (snapshot.MAX_BYTES + 1), b'{"version":1,"version":2}'):
            self.input.write_bytes(content)
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.update(self.output, self.input)
            self.assertEqual(self.state(), before)
        with mock.patch.object(snapshot, "fetch", side_effect=OSError("SECRET")):
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.update(self.output)
        self.assertEqual(self.state(), before)

    def test_write_failure_rolls_back_pair(self):
        self.update(example())
        before = {name: data[0] for name, data in self.state().items()}
        real_replace = snapshot.os.replace
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Simulated")
            return real_replace(source, target)
        value = example()
        value["items"].pop()
        self.save(value)
        with mock.patch.object(snapshot.os, "replace", side_effect=fail_second):
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.update(self.output, self.input)
        self.assertEqual({name: data[0] for name, data in self.state().items()}, before)

    def test_check_does_not_create_directory(self):
        self.save(example())
        self.assertEqual(snapshot.update(self.output, self.input, True), "valid")
        self.assertFalse(self.output.exists())

    def test_network_source_is_fixed_and_redirect_rejected(self):
        opener = mock.Mock()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 200
        response.geturl.return_value = snapshot.SOURCE
        response.read.return_value = b"{}"
        opener.open.return_value = response
        with mock.patch.object(snapshot.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(snapshot.fetch(), b"{}")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, snapshot.SOURCE)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertIsNone(snapshot.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_cli_sanitizes_error(self):
        with mock.patch.object(snapshot.sys, "argv", ["tool"]), mock.patch.object(snapshot, "fetch", side_effect=OSError("SECRET")):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(snapshot.main(), 1)
        self.assertNotIn("SECRET", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
