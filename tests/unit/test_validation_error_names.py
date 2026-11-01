"""A validation error names instruments by their ticker as of the experiment start (#237,
#243 review): the ticker valid then, the first one of an instrument listed later, and the
last one of an instrument delisted before. Ids the history does not know are kept."""

from __future__ import annotations

import pandas as pd

from quantlab.data.identity import normalize_history
from quantlab_api.tasks import name_instruments

HISTORY = normalize_history(
    pd.DataFrame(
        {
            "instrument_id": ["QLA", "QLA", "QLB", "QLB", "QLC", "QLD"],
            "symbol": ["AAA", "AAB", "BBB", "BBC", "CCC", "QLA"],
            "valid_from": [
                "2018-01-02",
                "2021-01-04",
                "2020-06-01",
                "2021-06-01",
                "2015-01-02",
                "2018-01-02",
            ],
            "valid_to": ["2021-01-04", None, "2021-06-01", None, "2019-01-02", None],
        }
    )
)
START = pd.Timestamp("2020-01-02")


def test_the_ticker_valid_at_the_start_not_a_later_rename():
    assert name_instruments("no data for symbols: ['QLA']", HISTORY, START) == (
        "no data for symbols: ['AAA']"
    )


def test_an_instrument_listed_after_the_start_gets_its_first_ticker_not_its_last():
    assert name_instruments("no data for symbols: ['QLB']", HISTORY, START) == (
        "no data for symbols: ['BBB']"
    )


def test_an_instrument_delisted_before_the_start_gets_its_last_ticker():
    assert name_instruments("missing ['QLC']", HISTORY, START) == "missing ['CCC']"


def test_unknown_ids_and_partial_matches_are_kept():
    message = "no data for symbols: ['QLGONE', 'QLAX', 'QLA.B']"
    assert name_instruments(message, HISTORY, START) == message


def test_one_pass_a_ticker_that_equals_another_id_is_not_renamed_again():
    # QLD trades as "QLA", which is also QLA's id: QLD becomes "QLA" and stays so.
    assert name_instruments("['QLD', 'QLA']", HISTORY, START) == "['QLA', 'AAA']"
