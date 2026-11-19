"""Regression checks for snapshot safety and replacement."""
import contextlib
import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.parse

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

    def test_sensitive_text_rejected_in_every_editorial_field(self):
        # Deliberately synthetic nonworking data, never copied production data.
        setters = (
            lambda v, s: v.update(title=s),
            lambda v, s: v.update(description=s),
            lambda v, s: v["items"][0].update(title=s),
            lambda v, s: v["items"][0].update(summary=s),
            lambda v, s: v["items"][0].update(content_text=s),
            lambda v, s: v["items"][0].update(tags=[s]),
            lambda v, s: v["items"][0]["_rase"]["sources"][0].update(title=s),
        )
        for index, setter in enumerate(setters):
            value = example()
            setter(value, "internal editorial notes: synthetic review material")
            with self.subTest(field=index), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)

    def test_credential_shapes_and_assignments_are_rejected(self):
        # Construct shapes rather than committing usable or provider-valid keys.
        credentials = (
            "gh" + "p_" + "a" * 36,
            "github" + "_pat_" + "b" * 30,
            "AK" + "IA" + "0" * 16,
            "sk" + "-proj-" + "c" * 28,
            "xox" + "b-" + "1" * 24,
            "eyJ" + "a" * 18 + "." + "b" * 18 + "." + "c" * 18,
            "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" + "-" * 5,
            "-" * 5 + "BEGIN PGP PRIVATE KEY BLOCK" + "-" * 5,
            "Authorization: Bearer synthetic-token-not-usable",
            "Authorization: Basic " + base64.b64encode(b"synthetic:not-a-secret").decode(),
            '"client_secret": "synthetic-not-a-real-credential"',
            "api_key = synthetic-not-a-real-credential",
            "password: synthetic-not-a-real-credential",
            "refresh_token=synthetic-not-a-real-credential",
            "postgresql://synthetic:unusable@db.example/database",
        )
        for index, credential in enumerate(credentials):
            value = example()
            value["items"][0]["summary"] = credential
            with self.subTest(pattern=index), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)

    def test_personal_contact_and_payment_patterns_are_rejected(self):
        examples = (
            "A synthetic person: reader@example.invalid",
            "Call (202) 555-0199 for details.",
            "Contact +44 20 7946 0958.",
            "phone: 2025550199",
            "Social security number: 123456789",
            "Identifier 123-45-6789",
            "16 Synthetic Example Drive, Sampletown",
            "16 Synthetic Example Drive",
            "16 Synthetic Example Dr. Unit 2",
            "16 Synthetic Example Drive Suite 2",
            "16 Synthetic Example Drive 00000",
            "Card: " + "4111" + " 1111" * 3,
        )
        for index, content in enumerate(examples):
            value = example()
            value["items"][0]["content_text"] = content
            with self.subTest(pattern=index), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)

    def test_internal_files_and_material_markers_are_rejected(self):
        examples = (
            "C:/Users/synthetic/private/export.json",
            r"C:\Users\synthetic\private\export.json",
            r"\\internal-host\private-share\export.json",
            "/home/synthetic/export.json",
            "/Users/synthetic/export.json",
            "/var/www/private-site/export.json",
            "/etc/synthetic-config",
            ".deployment-results/private-batch.json",
            "./.env.production",
            "./.codex/private-notes",
            "raw_research: synthetic material",
            "subscriber_email: synthetic information",
            "Confidential: synthetic business information",
            "Internal only: synthetic editorial notes",
            "Do not publish this review material.",
        )
        for index, content in enumerate(examples):
            value = example()
            value["items"][0]["summary"] = content
            with self.subTest(pattern=index), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)

    def test_embedded_private_urls_are_rejected_in_text(self):
        examples = (
            "https://synthetic:unusable@publisher.example/article",
            "https://rasesolutions.com/ai/admin/private",
            "https://rasesolutions.com/AI/%61dmin/private",
            "https://10.0.0.1/private",
            "http://localhost/private",
            "https://editorial.internal/private",
            "https://publisher.example/report?access_token=synthetic-not-usable",
            "https://publisher.example/report?credential=synthetic-not-usable",
            "https://publisher.example/report?api_key",
        )
        for index, content in enumerate(examples):
            value = example()
            value["items"][0]["content_text"] = "Supporting evidence: " + content
            with self.subTest(pattern=index), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)

    def test_common_encoded_sensitive_text_is_rejected(self):
        content = "internal_notes: synthetic private material"
        examples = (
            urllib.parse.quote(urllib.parse.quote(content, safe=""), safe=""),
            "reader&#64;example.invalid",
            "reader&amp;#64;example.invalid",
            "api_\u200bkey=synthetic-not-usable",
            "ｒｅａｄｅｒ＠ｅｘａｍｐｌｅ．ｉｎｖａｌｉｄ",
            base64.b64encode(content.encode()).decode(),
        )
        for index, encoded in enumerate(examples):
            value = example()
            value["items"][0]["summary"] = encoded
            with self.subTest(encoding=index), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)

    def test_normal_ai_reporting_and_public_source_urls_remain_allowed(self):
        value = example()
        content = (
            "OpenAI C++ SDK 2.12 and a 70B model were announced on 2026-11-18. "
            "The private beta supports confidential computing, secret scanning and SSN protection. "
            "Token pricing is available for the GPT-6.1 model and its 128K context window. "
            "Draft legislation concerns AI safety. NVIDIA and Google DeepMind published research. "
            "Sources: https://arxiv.org/abs/2611.12345 and "
            "https://doi.org/10.1038/s41586-026-12345-6 and "
            "https://publisher.example/users/research/home/models."
        )
        value["items"][0]["content_text"] = content
        self.assertEqual(snapshot.validate(value), value)

    def test_street_suffix_words_in_ai_news_do_not_imply_an_address(self):
        for content in (
            "3 billion parameters drive better reasoning.",
            "10 million users drive adoption.",
            "5 companies drive AI adoption.",
            "8 teams build models that drive progress.",
        ):
            value = example()
            value["items"][0]["summary"] = content
            self.assertEqual(snapshot.validate(value), value)

    def test_draft_or_status_metadata_never_expands_public_allowlist(self):
        for field in ("status", "publication_status", "published", "indexable", "research_notes"):
            value = example()
            value["items"][0][field] = "draft"
            with self.subTest(field=field), self.assertRaises(snapshot.SnapshotError):
                snapshot.validate(value)
        value = example()
        value["items"][0]["_rase"]["website_publication_scope"] = "private"
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(value)

    def test_sensitive_research_rejection_preserves_verified_snapshot_and_logs(self):
        self.update(example())
        before = self.state()
        value = example()
        marker = "subscriber_data: synthetic private list"
        value["items"][0]["content_text"] = marker
        self.save(value)
        errors = io.StringIO()
        output = io.StringIO()
        arguments = ["tool", "--input", str(self.input), "--output-dir", str(self.output)]
        with mock.patch.object(snapshot.sys, "argv", arguments):
            with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
                self.assertEqual(snapshot.main(), 1)
        self.assertEqual(self.state(), before)
        self.assertNotIn(marker, errors.getvalue() + output.getvalue())
        self.assertEqual(errors.getvalue().strip(), "Snapshot update failed.")

    def test_private_manifest_additions_and_capture_are_replaced(self):
        self.update(example())
        path = self.output / "manifest.json"
        correct = json.loads(path.read_bytes())
        corruptions = []
        nested = copy.deepcopy(correct)
        nested["date_bounds"]["earliest"]["internal_notes"] = "synthetic private review"
        corruptions.append(nested)
        captured = copy.deepcopy(correct)
        captured["captured_at_utc"] = "reader@example.invalid"
        corruptions.append(captured)
        for index, broken in enumerate(corruptions):
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(field=index):
                self.assertEqual(snapshot.update(self.output, self.input), "updated")
                repaired = json.loads(path.read_bytes())
                self.assertNotIn("internal_notes", repaired["date_bounds"]["earliest"])
                self.assertNotIn("@", repaired["captured_at_utc"])
                self.assertEqual(repaired["content_sha256"], snapshot.hashlib.sha256((self.output / "latest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
