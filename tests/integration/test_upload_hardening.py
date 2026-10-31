"""Upload reader hardening (#8 follow-ups from the #28 review)."""

from __future__ import annotations

import pytest

from tests.integration.test_upload_auth import (
    FIELDS,
    _authorized,
    _bars_part,
    _csv_path,
    _field,
    _parts,
    drive,
    run,
    uploads_dir,  # noqa: F401 - fixture
)


@pytest.mark.parametrize("field", ["name", "source", "license"])
def test_nul_in_a_text_field_is_a_422_not_a_500(client, uploads_dir, field):  # noqa: F811
    """PostgreSQL cannot store NUL in text; the insert used to fail with a 500."""
    h, ws = _authorized(client)
    fields = {**FIELDS, field: "Broker\x00export"}
    total, chunks = _parts(*(_field(k, v) for k, v in fields.items()), _bars_part())
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 422, res.body
    (detail,) = res.json()["error"]["details"]
    assert detail["loc"] == ["body", field]
    assert uploads_dir.new() == set()


def test_an_echoed_part_name_is_truncated(client, uploads_dir):  # noqa: F811
    h, ws = _authorized(client)
    long_name = "x" * 3000
    total, chunks = _parts(_field(long_name, "v"), _bars_part())
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 422
    loc = res.json()["error"]["details"][0]["loc"]
    assert loc[0] == "body" and len(loc[1]) <= 65 and loc[1].startswith("xxx")
    assert len(res.body) < 1000
