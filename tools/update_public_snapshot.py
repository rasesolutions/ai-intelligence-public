#!/usr/bin/env python3
"""Validate the fixed public feed and replace the current local snapshot."""
import argparse
import calendar
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request

SOURCE = "https://rasesolutions.com/ai/feed.json"
ORIGIN = "https://rasesolutions.com"
MAX_BYTES = 1_000_000
TYPES = frozenset(("announcement first_demo first_access public_release commercial_release "
    "research_breakthrough significant_update discontinuation regulatory_clearance legal_milestone "
    "in_force law_passed legislative market_launch milestone policy_adopted policy_launch "
    "policy_milestone preprint_submission preview_opened research_disclosure research_preprint "
    "research_publication").split())


class SnapshotError(Exception):
    """A safe message with no input content."""


def require(condition):
    if not condition:
        raise SnapshotError("Public feed contract validation failed.")


def fields(value, required, optional=()):
    require(type(value) is dict)
    require(set(required) <= set(value) <= set(required) | set(optional))


def text(value, limit=16_000):
    require(type(value) is str and 0 < len(value) <= limit and value.strip())
    require(not any(ord(char) < 32 and char not in "\n\r\t" for char in value))
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise SnapshotError("Invalid text encoding.")
    return value


def url(value, profile=False):
    text(value, 4096)
    require(not any(char.isspace() for char in value))
    require(not any(char in value for char in '<>"\\'))
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise SnapshotError("Invalid URL.")
    require(parts.scheme in ("http", "https") and parts.hostname and parts.username is None)
    require(parts.password is None and port in (None, 80, 443))
    require(parts.hostname not in ("localhost", "localhost.localdomain"))
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    require(address is None or address.is_global)
    if parts.hostname == "rasesolutions.com":
        private = ("/includes", "/forms", "/data", "/database", "/tools", "/admin", "/tests",
                   "/ai/admin", "/ai/newsletter", "/ai/media")
        decoded = urllib.parse.unquote(parts.path)
        require(not any(decoded == path or decoded.startswith(path + "/") for path in private))
    if profile:
        require(parts.scheme == "https" and parts.netloc == "rasesolutions.com")
        require(not parts.query and not parts.fragment and port is None)
        require(re.fullmatch(r"/ai/[a-z0-9-]+/[a-z0-9-]+", parts.path))
        require(parts.path.split("/")[2] not in ("admin", "newsletter", "media", "data", "tools"))
    else:
        query = urllib.parse.parse_qsl(parts.query)
        require(not any(re.search(r"token|secret|password|credential|api.?key", key, re.I) for key, _ in query))
    return value


def timestamp(value):
    text(value, 20)
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value))
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SnapshotError("Invalid publication date.")
    return value


def event_date(value):
    fields(value, ("value", "precision", "certainty"))
    require(value["precision"] in ("day", "month") and value["certainty"] == "verified")
    require(type(value["value"]) is str)
    precision = value["precision"]
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}" + ("-[0-9]{2}" if precision == "day" else ""), value["value"]))
    try:
        dt.date.fromisoformat(value["value"] + ("-01" if precision == "month" else ""))
    except ValueError:
        raise SnapshotError("Invalid development date.")
    return {key: value[key] for key in ("value", "precision", "certainty")}


def item(value):
    fields(value, ("id", "url", "title", "summary", "content_text", "date_published", "tags", "_rase"),
           ("external_url", "date_modified"))
    require(type(value["id"]) is str)
    prefix = "urn:rase:ai:record:"
    identifier = value["id"][len(prefix):]
    require(value["id"].startswith(prefix))
    require(re.fullmatch(r"(?:[A-Za-z0-9_.-]|%3A)+", identifier))
    require(len(urllib.parse.unquote(identifier)) <= 160)
    tags = value["tags"]
    require(type(tags) is list and 0 < len(tags) <= 8)
    tags = [text(tag, 256) for tag in tags]
    rase = value["_rase"]
    fields(rase, ("event_type", "original_event_date", "website_publication_scope",
                  "website_update_scope", "summary_scope", "sources"))
    require(type(rase["event_type"]) is str and rase["event_type"] in TYPES)
    require(rase["website_publication_scope"] == "profile")
    require(rase["summary_scope"] in ("event", "profile"))
    require(rase["website_update_scope"] in ("profile_first_publication",
            "reviewed_profile_publication", "reviewed_event_publication"))
    sources = rase["sources"]
    require(type(sources) is list and 0 < len(sources) <= 100)
    projected = []
    for source in sources:
        fields(source, ("url", "title", "kind"))
        require(source["kind"] in ("primary", "independent"))
        projected.append({"url": url(source["url"]), "title": text(source["title"]), "kind": source["kind"]})
    require(len({source["url"] for source in projected}) == len(projected))
    result = {"id": value["id"], "url": url(value["url"], profile=True),
              "title": text(value["title"]), "summary": text(value["summary"]),
              "content_text": text(value["content_text"]), "date_published": timestamp(value["date_published"]),
              "tags": tags, "_rase": {"event_type": rase["event_type"],
              "original_event_date": event_date(rase["original_event_date"]),
              "website_publication_scope": "profile", "website_update_scope": rase["website_update_scope"],
              "summary_scope": rase["summary_scope"], "sources": projected}}
    primary = [source["url"] for source in projected if source["kind"] == "primary"]
    require(("external_url" in value) == bool(primary))
    if primary:
        result["external_url"] = url(value["external_url"])
        require(result["external_url"] in primary)
    if "date_modified" in value:
        result["date_modified"] = timestamp(value["date_modified"])
        require(result["date_modified"] > result["date_published"])
    return result


def validate(value):
    fields(value, ("version", "title", "home_page_url", "feed_url", "language",
                   "description", "authors", "items"))
    require(value["version"] == "https://jsonfeed.org/version/1.1")
    require(value["feed_url"] == SOURCE and value["home_page_url"] == ORIGIN + "/ai/latest")
    require(value["language"] == "en" and type(value["authors"]) is list and len(value["authors"]) == 1)
    author = value["authors"][0]
    fields(author, ("name", "url"))
    require(author == {"name": "Rase AI Intelligence", "url": ORIGIN + "/ai"})
    require(type(value["items"]) is list and len(value["items"]) <= 100)
    items = [item(entry) for entry in value["items"]]
    for key in ("id", "url"):
        require(len({entry[key] for entry in items}) == len(items))
    return {"version": value["version"], "title": text(value["title"]), "home_page_url": value["home_page_url"],
            "feed_url": SOURCE, "language": "en", "description": text(value["description"]),
            "authors": [{"name": "Rase AI Intelligence", "url": ORIGIN + "/ai"}], "items": items}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, target):
        return None


def fetch():
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(SOURCE, headers={"User-Agent": "RasePublicSnapshot/1.0"})
    try:
        with opener.open(request, timeout=15) as response:
            if response.getcode() != 200 or response.geturl() != SOURCE:
                raise SnapshotError("Feed request failed.")
            return response.read(MAX_BYTES + 1)
    except Exception:
        raise SnapshotError("Feed request failed.")


def pairs(values):
    result = {}
    for key, value in values:
        require(key not in result)
        result[key] = value
    return result


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def replace(output, latest, manifest):
    output.mkdir(parents=True, exist_ok=True)
    paths = (output / "latest.json", output / "manifest.json")
    require(not any(path.is_symlink() for path in paths))
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    temporary = []
    try:
        for path, content in zip(paths, (latest, manifest)):
            descriptor, name = tempfile.mkstemp(dir=output, prefix=".snapshot-")
            temporary.append(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for path, name in zip(paths, temporary):
            os.replace(name, path)
    except OSError:
        for path, content in before.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise SnapshotError("Snapshot write failed.")
    finally:
        for name in temporary:
            Path(name).unlink(missing_ok=True)


def update(output, input_file=None, check=False):
    try:
        if input_file is None:
            raw = fetch()
        else:
            with input_file.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        current = validate(value)
    except SnapshotError:
        raise
    except Exception:
        raise SnapshotError("Feed could not be validated.")
    if check:
        return "valid"
    latest = encoded(current)
    old = output / "latest.json"
    manifest_path = output / "manifest.json"
    previous = None
    same_content = False
    same_bytes = False
    try:
        previous_bytes = old.read_bytes()
        same_bytes = previous_bytes == latest
        same_content = encoded(validate(json.loads(previous_bytes, object_pairs_hook=pairs))) == latest
        previous = json.loads(manifest_path.read_bytes(), object_pairs_hook=pairs)
    except (OSError, ValueError, KeyError, SnapshotError):
        pass
    dates = [entry["_rase"]["original_event_date"] for entry in current["items"]]
    def edge(date, last):
        value = date["value"]
        if date["precision"] == "month":
            year, month = map(int, value.split("-"))
            value += "-%02d" % (calendar.monthrange(year, month)[1] if last else 1)
        return value
    bounds = {"earliest": min(dates, key=lambda date: edge(date, False)) if dates else None,
              "latest": max(dates, key=lambda date: edge(date, True)) if dates else None}
    captured = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if same_content and type(previous) is dict:
        try:
            candidate = timestamp(previous.get("captured_at_utc"))
            require(candidate <= captured)
            captured = candidate
        except SnapshotError:
            pass
    manifest = {"source_url": SOURCE, "captured_at_utc": captured,
                "content_sha256": hashlib.sha256(latest).hexdigest(), "item_count": len(current["items"]),
                "date_bounds": bounds, "publication_scope": "Public feed profiles"}
    if same_bytes and type(previous) is dict:
        if previous == manifest and type(previous.get("item_count")) is int:
            return "unchanged"
    replace(output, latest, encoded(manifest))
    return "updated"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        print(update(arguments.output_dir, arguments.input, arguments.check))
        return 0
    except Exception:
        print("Snapshot update failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
