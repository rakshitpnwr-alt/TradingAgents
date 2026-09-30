"""Regressions for defects found auditing this fork's own additions.

Each test here corresponds to a bug that shipped. They are cheap, and they
are the difference between "fixed" and "fixed until someone edits it".
"""
import json
import pathlib
import tempfile

import pytest

from tradingagents.dataflows import archive
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.vendors import reddit
from tradingagents.dataflows.vendors.yahoo.market import _missing_value_label


@pytest.mark.unit
def test_archive_names_a_record_after_the_tool_not_the_ticker(monkeypatch):
    """The decorator took args[0] as the call name. That is the method for
    route_to_vendor(method, ...) but the ticker for fetch_reddit_posts(ticker,
    ...), so social records were filed under a ticker and a reader could not
    tell which source had failed.

    Asserted against what record() is handed, since the archive itself
    (correctly) declines to write anything while the suite is running.
    """
    seen = []
    monkeypatch.setattr(archive, "record",
                        lambda source, call, *a, **k: seen.append((source, call)))

    @archive.archived("reddit")
    def fetch_reddit_posts(ticker, **kw):
        return "posts"

    @archive.archived("vendor", method_arg=True)
    def route_to_vendor(method, *a, **k):
        return "routed"

    fetch_reddit_posts("BTC-USD")
    route_to_vendor("get_news", "BTC-USD")

    assert seen == [("reddit", "fetch_reddit_posts"), ("vendor", "get_news")], seen


@pytest.mark.unit
def test_archive_records_nothing_while_pytest_is_running(tmp_path):
    """Every vendor is mocked under test, so archiving during a run filled the
    archive with fixtures indistinguishable from real fetches afterwards."""
    set_config({"archive_enabled": True, "archive_dir": str(tmp_path)})
    archive.record("vendor", "get_news", ("BTC-USD",), {}, "payload")
    assert not list(pathlib.Path(tmp_path).rglob("*.jsonl"))


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["not-a-date", "", None, "2026-02-30", "2026"])
def test_missing_value_label_survives_an_unparseable_date(bad):
    """It split on '-' and int()ed the parts, raising from inside a helper
    whose only job is to describe absent data calmly."""
    assert isinstance(_missing_value_label("AAPL", bad), str)


@pytest.mark.unit
def test_missing_value_label_still_names_a_real_weekend():
    """The repair must not flatten the distinction it was written to draw."""
    assert "weekend" in _missing_value_label("AAPL", "2026-09-26")          # Saturday
    assert "weekend" not in _missing_value_label("AAPL", "2026-09-29")      # Tuesday
    assert "weekend" not in _missing_value_label("BTC-USD", "2026-09-26")   # 24/7
