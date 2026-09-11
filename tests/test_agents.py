from datetime import date

import pytest

from finsql.agents import date_context, fiscal_quarter, parse_writer_output


@pytest.mark.parametrize("today, start, expected", [
    (date(2026, 9, 12), 4, (2027, 2)),   # April fiscal year: Sep 2026 is FY2027-Q2
    (date(2026, 4, 1), 4, (2027, 1)),
    (date(2026, 3, 31), 4, (2026, 4)),
    (date(2026, 9, 12), 1, (2026, 3)),   # calendar fiscal year
    (date(2026, 1, 5), 1, (2026, 1)),
])
def test_fiscal_quarter(today, start, expected):
    assert fiscal_quarter(today, start) == expected


def test_last_quarter_wraps_to_previous_fiscal_year():
    ctx = date_context(date(2026, 4, 15), 4)  # FY2027-Q1
    assert "Last quarter: FY2026-Q4 (fiscal_year = 2026, fiscal_quarter = 4)" in ctx


def test_writer_output_parsing():
    assert parse_writer_output("```sql\nSELECT 1;\n```") == ("SELECT 1", None)
    assert parse_writer_output("CLARIFY: Which year?") == (None, "Which year?")
    assert parse_writer_output("SELECT 2") == ("SELECT 2", None)
    sql, clarify = parse_writer_output("I'm not sure what you mean.")
    assert sql is None and clarify
