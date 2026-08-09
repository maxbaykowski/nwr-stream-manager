#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import struct
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "src" / "nwr-stream-manager" / "assets"
EVENT_CODES_URL = "https://www.weather.gov/nwr/eventcodes"
COUNTIES_URL = "https://www.weather.gov/gis/Counties"
EAS_NWR_URL = "https://www.weather.gov/gis/EasNWR"
MARINE_ZONES_URL = "https://www.weather.gov/gis/MarineZones"
EVENTS_OUTPUT = ASSETS_DIR / "events.json"
LOCATIONS_OUTPUT = ASSETS_DIR / "locations.json"


class SameDataError(RuntimeError):
    pass


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


class TableParser(HTMLParser):
    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.in_table = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and dict(attrs).get("id") == self.table_id:
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.current_row = []
        elif self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.in_table:
            self.in_table = False
        elif self.in_table and tag in {"td", "th"} and self.in_cell:
            assert self.current_row is not None
            text = " ".join("".join(self.current_cell).split())
            self.current_row.append(html.unescape(text))
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)


@dataclass(frozen=True)
class DbfField:
    name: str
    type: str
    length: int
    decimals: int


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def parse_event_codes(html_text: str, *, minimum_count: int = 40) -> dict[str, dict[str, str]]:
    parser = TableParser("eas_codes")
    parser.feed(html_text)
    if not parser.rows:
        raise SameDataError("could not find NWS event-code table with id 'eas_codes'")
    events: dict[str, dict[str, str]] = {}
    for row in parser.rows:
        if len(row) < 3:
            continue
        name, raw_code, status = row[:3]
        code_match = re.search(r"\b[A-Z]{3}\b", raw_code)
        if not code_match:
            continue
        code = code_match.group(0)
        if status.strip().lower() != "operational":
            continue
        if code in events:
            raise SameDataError(f"duplicate event code {code}")
        events[code] = {"code": code, "name": name.strip()}
    if len(events) < minimum_count:
        raise SameDataError(f"parsed only {len(events)} event codes; source format likely changed")
    validate_events(events)
    return dict(sorted(events.items()))


def parse_links(page_url: str, html_text: str) -> list[str]:
    parser = LinkParser()
    parser.feed(html_text)
    return [urljoin(page_url, link) for link in parser.links]


def latest_link(links: list[str], pattern: str) -> str:
    matches = []
    regex = re.compile(pattern, re.IGNORECASE)
    for link in links:
        match = regex.search(link)
        if match:
            matches.append((date_key(match.group(1)), link))
    if not matches:
        raise SameDataError(f"could not find link matching {pattern}")
    return max(matches, key=lambda item: item[0])[1]


def date_key(token: str) -> tuple[int, int, int]:
    months = {
        "ja": 1,
        "fe": 2,
        "mr": 3,
        "ap": 4,
        "my": 5,
        "jn": 6,
        "jl": 7,
        "au": 8,
        "se": 9,
        "oc": 10,
        "no": 11,
        "de": 12,
    }
    match = re.fullmatch(r"(\d{2})([a-z]{2})(\d{2})", token.lower())
    if not match:
        raise SameDataError(f"unsupported NWS date token {token!r}")
    day = int(match.group(1))
    month = months.get(match.group(2))
    if month is None:
        raise SameDataError(f"unsupported NWS month token {match.group(2)!r}")
    year = 2000 + int(match.group(3))
    return (year, month, day)


def dbf_rows_from_zip(zip_bytes: bytes) -> list[dict[str, str]]:
    with tempfile.TemporaryFile() as fp:
        fp.write(zip_bytes)
        fp.seek(0)
        with zipfile.ZipFile(fp) as zip_file:
            dbf_names = [name for name in zip_file.namelist() if name.lower().endswith(".dbf")]
            if len(dbf_names) != 1:
                raise SameDataError(f"expected exactly one DBF in shapefile zip, found {dbf_names}")
            return parse_dbf(zip_file.read(dbf_names[0]))


def parse_dbf(data: bytes) -> list[dict[str, str]]:
    if len(data) < 33:
        raise SameDataError("DBF data is too short")
    record_count = struct.unpack("<I", data[4:8])[0]
    header_length = struct.unpack("<H", data[8:10])[0]
    record_length = struct.unpack("<H", data[10:12])[0]
    fields: list[DbfField] = []
    offset = 32
    while offset < len(data) and data[offset] != 0x0D:
        descriptor = data[offset : offset + 32]
        if len(descriptor) != 32:
            raise SameDataError("truncated DBF field descriptor")
        name = descriptor[:11].split(b"\0", 1)[0].decode("ascii", errors="replace")
        fields.append(DbfField(name=name, type=chr(descriptor[11]), length=descriptor[16], decimals=descriptor[17]))
        offset += 32
    rows = []
    for index in range(record_count):
        start = header_length + index * record_length
        record = data[start : start + record_length]
        if len(record) != record_length:
            raise SameDataError("truncated DBF record")
        if record[:1] == b"*":
            continue
        column_offset = 1
        row: dict[str, str] = {}
        for field in fields:
            raw = record[column_offset : column_offset + field.length]
            row[field.name] = raw.decode("latin1", errors="replace").strip()
            column_offset += field.length
        rows.append(row)
    return rows


def build_county_locations(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        require_fields(row, ["STATE", "COUNTYNAME", "FIPS"])
        fips = row["FIPS"].strip()
        if not re.fullmatch(r"\d{5}", fips):
            raise SameDataError(f"invalid county FIPS {fips!r}")
        grouped.setdefault("0" + fips, []).append(row)
    locations: dict[str, dict[str, Any]] = {}
    for same, group in grouped.items():
        states = {row["STATE"].strip() for row in group}
        fips_values = {row["FIPS"].strip() for row in group}
        if len(states) != 1 or len(fips_values) != 1:
            raise SameDataError(f"conflicting county rows for SAME {same}: {group!r}")
        aliases = sorted({row["COUNTYNAME"].strip() for row in group if row["COUNTYNAME"].strip()})
        if not aliases:
            raise SameDataError(f"missing county name for SAME {same}")
        entry: dict[str, Any] = {
            "same": same,
            "fips": next(iter(fips_values)),
            "state": next(iter(states)),
            "name": aliases[0] if len(aliases) == 1 else " / ".join(aliases),
            "location_type": "county",
        }
        if len(aliases) > 1:
            entry["aliases"] = aliases
        locations[same] = entry
    return locations


def parse_pipe_rows(text: str, expected_fields: int, label: str) -> list[list[str]]:
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < expected_fields:
            raise SameDataError(f"{label} line {line_number} has {len(fields)} fields, expected {expected_fields}: {line!r}")
        rows.append(fields)
    if not rows:
        raise SameDataError(f"{label} source did not contain any rows")
    return rows


def build_marine_locations(
    marnwr_text: str,
    marst_text: str,
    marine_zone_rows: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    areas = {row[0]: {"area_code": row[1], "area_name": row[3]} for row in parse_pipe_rows(marst_text, 4, "marst")}
    zone_names = {}
    for row in marine_zone_rows:
        zone_id = str(row.get("ID") or row.get("id") or "").strip().upper()
        name = str(row.get("NAME") or row.get("Name") or row.get("name") or "").strip()
        if zone_id and name:
            zone_names[zone_id] = name
    locations: dict[str, dict[str, Any]] = {}
    for area_alpha, ssnum, zone_name, *_rest in parse_pipe_rows(marnwr_text, 3, "marnwr"):
        if not re.fullmatch(r"\d{5}", ssnum):
            raise SameDataError(f"invalid marine SSNUM {ssnum!r}")
        same = "0" + ssnum
        if same in locations:
            raise SameDataError(f"duplicate marine SAME code {same}")
        area_alpha = area_alpha.upper()
        zone_id = f"{area_alpha}Z{ssnum[-3:]}"
        official_zone_name = zone_names.get(zone_id)
        entry: dict[str, Any] = {
            "same": same,
            "name": official_zone_name or zone_name,
            "location_type": "marine",
            "marine_area": area_alpha,
        }
        if zone_id in zone_names:
            entry["ugc"] = zone_id
        if area_alpha in areas:
            entry["marine_area_name"] = areas[area_alpha]["area_name"]
        locations[same] = entry
    return locations


def merge_locations(*sources: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        for same, entry in source.items():
            if same in merged:
                raise SameDataError(f"duplicate geographic SAME code {same}: {merged[same]!r} vs {entry!r}")
            merged[same] = entry
    validate_locations(merged)
    return dict(sorted(merged.items()))


def require_fields(row: dict[str, str], fields: list[str]) -> None:
    missing = [field for field in fields if not row.get(field)]
    if missing:
        raise SameDataError(f"missing required field(s) {missing} in row {row!r}")


def validate_events(events: dict[str, dict[str, str]]) -> None:
    for code, entry in events.items():
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise SameDataError(f"invalid event code key {code!r}")
        if entry.get("code") != code or not entry.get("name"):
            raise SameDataError(f"invalid event entry for {code}: {entry!r}")


def validate_locations(locations: dict[str, dict[str, Any]]) -> None:
    for same, entry in locations.items():
        if not re.fullmatch(r"\d{6}", same):
            raise SameDataError(f"invalid SAME code key {same!r}")
        if entry.get("same") != same or not entry.get("name") or not entry.get("location_type"):
            raise SameDataError(f"invalid location entry for {same}: {entry!r}")
        if entry["location_type"] == "county":
            fips = entry.get("fips")
            if not isinstance(fips, str) or not re.fullmatch(r"\d{5}", fips):
                raise SameDataError(f"invalid FIPS for {same}: {entry!r}")
        if entry["location_type"] == "marine":
            ugc = entry.get("ugc")
            if ugc is not None and not re.fullmatch(r"[A-Z]{2}Z\d{3}", str(ugc)):
                raise SameDataError(f"invalid UGC zone for {same}: {entry!r}")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fp:
        fp.write(data)
        temp_path = Path(fp.name)
    try:
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def generate() -> tuple[dict[str, Any], dict[str, Any]]:
    events = parse_event_codes(fetch_text(EVENT_CODES_URL))
    county_links = parse_links(COUNTIES_URL, fetch_text(COUNTIES_URL))
    eas_links = parse_links(EAS_NWR_URL, fetch_text(EAS_NWR_URL))
    marine_links = parse_links(MARINE_ZONES_URL, fetch_text(MARINE_ZONES_URL))

    county_zip = fetch_bytes(latest_link(county_links, r"/c_(\d{2}[a-z]{2}\d{2})\.zip$"))
    marnwr_text = fetch_text(latest_link(eas_links, r"/marnwr(\d{2}[a-z]{2}\d{2})\.txt$"))
    marst_text = fetch_text(latest_link(eas_links, r"/marst(\d{2}[a-z]{2}\d{2})\.txt$"))
    marine_zone_rows: list[dict[str, str]] = []
    for pattern in (r"/mz(\d{2}[a-z]{2}\d{2})\.zip$", r"/oz(\d{2}[a-z]{2}\d{2})\.zip$", r"/hz(\d{2}[a-z]{2}\d{2})\.zip$"):
        marine_zone_rows.extend(dbf_rows_from_zip(fetch_bytes(latest_link(marine_links, pattern))))

    counties = build_county_locations(dbf_rows_from_zip(county_zip))
    marine = build_marine_locations(marnwr_text, marst_text, marine_zone_rows)
    return events, merge_locations(counties, marine)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update static NWR SAME event and location lookup data.")
    parser.add_argument("--assets-dir", type=Path, default=ASSETS_DIR)
    args = parser.parse_args()
    events, locations = generate()
    atomic_write_json(args.assets_dir / "events.json", events)
    atomic_write_json(args.assets_dir / "locations.json", locations)
    print(f"wrote {len(events)} events and {len(locations)} locations to {args.assets_dir}")


if __name__ == "__main__":
    main()
