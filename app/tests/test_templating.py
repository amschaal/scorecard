"""The `ts` filter shows stored-UTC timestamps in the cluster's zone. Needs no database."""

from datetime import datetime, timezone

from hpcusage.templating import templates

UTC_TS = datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)  # 00:00 PDT


def render(src: str, **ctx) -> str:
    return templates.env.from_string(src).render(**ctx)


def test_ts_uses_page_zone():
    assert render("{{ v|ts }}", v=UTC_TS, tz="America/Los_Angeles") == "2026-09-17 00:00"


def test_ts_row_zone_overrides_page_zone():
    assert render("{{ v|ts('America/New_York') }}", v=UTC_TS, tz="America/Los_Angeles") == "2026-09-17 03:00"


def test_ts_parses_iso_strings_from_json_rows():
    assert render("{{ v|ts }}", v=UTC_TS.isoformat(), tz="America/Los_Angeles") == "2026-09-17 00:00"


def test_ts_without_zone_or_value():
    assert render("{{ v|ts }}", v=UTC_TS) == "2026-09-17 07:00"
    assert render("{{ v|ts }}", v=None, tz="America/Los_Angeles") == "–"
    assert render("{{ v|ts }}", v=UTC_TS, tz="Not/AZone") == "2026-09-17 07:00"
