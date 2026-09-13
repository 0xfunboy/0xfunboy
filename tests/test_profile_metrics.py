"""Regression coverage for GitHub's rolling public calendar at a week boundary."""
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "update-profile-metrics.py"
SPEC = importlib.util.spec_from_file_location("profile_metrics", SOURCE)
metrics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = metrics
SPEC.loader.exec_module(metrics)


def calendar_html(declared_start, displayed_start, end, omitted=None, total=None):
    days = []
    day = displayed_start
    while day <= end:
        if day != omitted:
            days.append(day)
        day += timedelta(days=1)
    parts = [f'<div data-from="{declared_start}" data-to="{end}">',
             f'<h2 id="js-contribution-activity-description">{len(days) if total is None else total} contributions in the last year</h2>']
    for i, day in enumerate(days):
        parts += [f'<td id="day-{i}" data-date="{day}" data-level="1"></td>',
                  f'<tool-tip for="day-{i}">1 contribution on {day}</tool-tip>']
    return "".join(parts) + "</div>"


class CalendarRolloverTests(unittest.TestCase):
    def test_saturday_full_grid(self):
        end = date(2026, 9, 12)
        result = metrics.parse_calendar(calendar_html(date(2025, 9, 7), date(2025, 9, 7), end), end)
        self.assertEqual((len(result.days), result.total), (371, 371))

    def test_sunday_accepts_full_grid_with_prior_week_metadata(self):
        end = date(2026, 9, 13)
        result = metrics.parse_calendar(calendar_html(date(2025, 9, 7), date(2025, 9, 14), end), end)
        self.assertEqual(result.days[0][0], date(2025, 9, 14))
        self.assertEqual((len(result.days), result.total, result.current), (365, 365, 365))

    def test_missing_interior_day_is_still_rejected(self):
        end = date(2026, 9, 13)
        html = calendar_html(date(2025, 9, 7), date(2025, 9, 14), end, omitted=date(2026, 4, 20))
        with self.assertRaisesRegex(ValueError, "missing or unexpected dates"):
            metrics.parse_calendar(html, end)

    def test_partial_leading_week_is_not_treated_as_padding(self):
        end = date(2026, 9, 13)
        with self.assertRaisesRegex(ValueError, "missing leading dates"):
            metrics.parse_calendar(calendar_html(date(2025, 9, 7), date(2025, 9, 8), end), end)

    def test_rollover_does_not_bypass_total_validation(self):
        end = date(2026, 9, 13)
        with self.assertRaisesRegex(ValueError, "do not match GitHub's total"):
            metrics.parse_calendar(calendar_html(date(2025, 9, 7), date(2025, 9, 14), end, total=366), end)


if __name__ == "__main__":
    unittest.main()
