#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Tests for the cron scheduler: :class:`CronSchedule`, :class:`ScheduledJob`,
:class:`EncreScheduler`, :class:`ScheduleType`, and :class:`JobState`.

The suite covers cron expression parsing and field matching, next-fire
computation including leap-year boundaries, JSON round-trip serialization of
jobs, durable save/load with corruption recovery, skipping of recurring
occurrences that were missed before a restart, job lifecycle callbacks with
execution history, and the enum shapes used across the codebase.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from encre.scheduler import (
    CronSchedule,
    EncreScheduler,
    JobExecution,
    JobState,
    ScheduledJob,
    ScheduleType,
)


@pytest.fixture(autouse=True)
def isolate_default_scheduler_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep default scheduler instances independent across tests."""
    monkeypatch.setattr("encre.config.get_data_dir", lambda: tmp_path)


# ===========================================================================
# CronSchedule.parse()
# ===========================================================================

class TestCronScheduleParse:
    """Engineered to validate ``CronSchedule.parse`` expression decoding.

    This test class exercises cron-expression parsing across 11 scenarios
    covering valid expressions (wildcards, specific times, ranges, steps,
    named days, comma lists, round-trip serialization) and invalid inputs
    (too few fields, too many fields, empty string, whitespace-only). The
    design ensures the parser correctly maps the five-field cron format to
    the ``CronSchedule`` dataclass fields and rejects malformed input with
    a clear ``ValueError`` so that job scheduling never proceeds with an
    ambiguous or unparseable expression.
    """

    def test_verify_parse_every_minute(self):
        """Validate that ``* * * * *`` maps all fields to wildcard star.

        The test exercises a fully-wildcard expression and asserts every
        ``CronSchedule`` field equals ``"*"`` because an every-minute schedule
        must match every calendar tick in every field position.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs.minute == "*"
        assert cs.hour == "*"
        assert cs.day_of_month == "*"
        assert cs.month == "*"
        assert cs.day_of_week == "*"

    def test_verify_parse_specific_time(self):
        """Validate that explicit numeric fields are parsed correctly.

        The test exercises ``"30 9 15 3 *"`` and asserts each field maps to
        its corresponding position because explicit cron expressions must be
        interpreted literally without field shifting or default substitution.
        """
        cs = CronSchedule.parse("30 9 15 3 *")
        assert cs.minute == "30"
        assert cs.hour == "9"
        assert cs.day_of_month == "15"
        assert cs.month == "3"
        assert cs.day_of_week == "*"

    def test_verify_parse_with_ranges(self):
        """Validate that hyphenated ranges are preserved in their fields.

        The test exercises ``"0 9-17 * * 1-5"`` and asserts ``hour`` is
        ``"9-17"`` and ``day_of_week`` is ``"1-5"`` because range expressions
        must pass through unchanged so the match logic can evaluate them later.
        """
        cs = CronSchedule.parse("0 9-17 * * 1-5")
        assert cs.hour == "9-17"
        assert cs.day_of_week == "1-5"

    def test_verify_parse_with_step(self):
        """Validate that step expressions (``*/N``) are preserved correctly.

        The test exercises ``"*/5 * * * *"`` and asserts ``minute`` is
        ``"*/5"`` because step syntax must survive parsing so the matcher
        can compute divisibility at fire-time evaluation.
        """
        cs = CronSchedule.parse("*/5 * * * *")
        assert cs.minute == "*/5"

    def test_verify_parse_with_named_days(self):
        """Validate that textual day-of-week abbreviations are stored as-is.

        The test exercises ``"0 9 * * mon"`` and asserts ``day_of_week`` is
        ``"mon"`` because named days must be preserved for later normalization
        by ``_normalize_dow`` rather than being converted at parse time.
        """
        cs = CronSchedule.parse("0 9 * * mon")
        assert cs.day_of_week == "mon"

    def test_verify_parse_with_comma_list(self):
        """Validate that comma-separated value lists are preserved per field.

        The test exercises ``"0,30 9,17 * * *"`` and asserts both ``minute``
        and ``hour`` carry their comma-separated strings because multi-value
        fields must survive parsing so the matcher can check set membership.
        """
        cs = CronSchedule.parse("0,30 9,17 * * *")
        assert cs.minute == "0,30"
        assert cs.hour == "9,17"

    def test_verify_parse_too_few_fields_raises_value_error(self):
        """Validate that an expression with fewer than five fields raises ``ValueError``.

        The test exercises ``"0 9 * *"``, a four-field string, and asserts a
        ``ValueError`` containing ``"Expected 5 fields"`` is raised because
        the cron grammar strictly requires exactly five space-separated tokens.
        """
        with pytest.raises(ValueError, match="Expected 5 fields"):
            CronSchedule.parse("0 9 * *")

    def test_verify_parse_too_many_fields_raises_value_error(self):
        """Validate that an expression with more than five fields raises ``ValueError``.

        The test exercises ``"0 9 * * * *"`` (six fields) and asserts the same
        validation error is raised because extra fields indicate a malformed
        expression that should not be silently accepted.
        """
        with pytest.raises(ValueError, match="Expected 5 fields"):
            CronSchedule.parse("0 9 * * * *")

    def test_verify_parse_empty_string_raises_value_error(self):
        """Validate that an empty string raises ``ValueError`` with the field-count message.

        The test exercises ``""`` and asserts a ``ValueError`` containing
        ``"Expected 5 fields"`` is raised because an empty expression is
        indistinguishable from a missing argument and must be rejected.
        """
        with pytest.raises(ValueError, match="Expected 5 fields"):
            CronSchedule.parse("")

    def test_verify_parse_whitespace_only_raises_value_error(self):
        """Validate that a whitespace-only string raises ``ValueError``.

        The test exercises a string of spaces and asserts a ``ValueError`` is
        raised because whitespace provides no parseable tokens and must be
        rejected rather than producing an empty five-field schedule.
        """
        with pytest.raises(ValueError):
            CronSchedule.parse("     ")

    def test_verify_to_expression_roundtrip_preserves_original(self):
        """Validate that ``to_expression()`` reconstructs the original cron string.

        The test exercises a complex expression with steps, ranges, comma lists,
        and named days, parses it, then calls ``to_expression()`` and asserts
        the result matches the input exactly because round-trip fidelity is
        required for storing and reloading schedules from disk.
        """
        expr = "*/10 8-18 1,15 * mon-fri"
        cs = CronSchedule.parse(expr)
        assert cs.to_expression() == expr


# ===========================================================================
# CronSchedule._match_field
# ===========================================================================

class TestMatchField:
    """Engineered to validate ``CronSchedule._match_field`` pattern matching.

    This test class exercises the field-matching logic across 7 scenarios
    covering wildcards, exact matches, step expressions, step-with-base,
    range expressions, comma lists, and invalid integer inputs. The design
    ensures the matcher correctly evaluates each cron field token against
    a concrete integer value so that the scheduler fires only on intended
    calendar ticks without false positives.
    """

    def test_verify_star_matches_all_values(self):
        """Validate that ``*`` matches every integer in the 0-59 range.

        The test exercises a wildcard field against 60 integer values and
        asserts every call returns ``True`` because a star field represents
        unconditional acceptance for that position.
        """
        cs = CronSchedule.parse("* * * * *")
        for v in range(60):
            assert cs._match_field(v, "*") is True

    def test_verify_exact_integer_match(self):
        """Validate that a literal number matches only itself.

        The test exercises ``_match_field(30, "30")`` and ``_match_field(31, "30")``
        and asserts the former returns ``True`` and the latter ``False`` because
        exact-match fields must only fire on the precise integer they specify.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs._match_field(30, "30") is True
        assert cs._match_field(31, "30") is False

    def test_verify_step_expression_matches_correctly(self):
        """Validate that ``*/5`` matches multiples of 5 and rejects others.

        The test exercises values 0, 5, and 7 against ``"*/5"`` and asserts
        0 and 5 return ``True`` while 7 returns ``False`` because step syntax
        means ``start..end step N`` from zero, matching only exact remainders.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs._match_field(0, "*/5") is True
        assert cs._match_field(5, "*/5") is True
        assert cs._match_field(7, "*/5") is False

    def test_verify_step_with_base_matches_correctly(self):
        """Validate that ``10/5`` matches 10, 15, 20 and so on while rejecting 9.

        The test exercises values 10, 15, and 9 against ``"10/5"`` and asserts
        10 and 15 return ``True`` while 9 returns ``False`` because step-with-base
        syntax means ``start..end step N`` starting from the base value.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs._match_field(10, "10/5") is True
        assert cs._match_field(15, "10/5") is True
        assert cs._match_field(9, "10/5") is False

    def test_verify_range_matches_inclusive_bounds(self):
        """Validate that ``9-17`` matches all integers from 9 through 17 inclusive.

        The test exercises boundary values 8, 9, 17, and 18 against ``"9-17"``
        and asserts the in-range values return ``True`` while the out-of-range
        values return ``False`` because range expressions are inclusive on both ends.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs._match_field(10, "9-17") is True
        assert cs._match_field(9, "9-17") is True
        assert cs._match_field(17, "9-17") is True
        assert cs._match_field(8, "9-17") is False
        assert cs._match_field(18, "9-17") is False

    def test_verify_comma_list_matches_any_member(self):
        """Validate that ``0,30`` matches only the listed values.

        The test exercises values 0, 30, and 15 against ``"0,30"`` and asserts
        the listed values return ``True`` while 15 returns ``False`` because
        comma-separated lists represent an explicit membership check.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs._match_field(0, "0,30") is True
        assert cs._match_field(30, "0,30") is True
        assert cs._match_field(15, "0,30") is False

    def test_verify_invalid_non_integer_field_returns_false(self):
        """Validate that a non-numeric pattern returns ``False`` for any integer value.

        The test exercises ``_match_field(5, "mon")`` and asserts ``False`` is
        returned without raising because day-name strings are not valid minute/
        hour values and must silently fail the match rather than crash the loop.
        """
        cs = CronSchedule.parse("* * * * *")
        assert cs._match_field(5, "mon") is False


# ===========================================================================
# CronSchedule._normalize_dow
# ===========================================================================

class TestNormalizeDOW:
    """Engineered to validate ``CronSchedule._normalize_dow`` name-to-number conversion.

    This test class exercises named-day conversion across 10 scenarios covering
    each weekday abbreviation, case insensitivity, mixed ranges like ``mon-fri``,
    and numeric passthrough. The design ensures that human-readable cron expressions
    are normalized to numeric day-of-week values before matching so that the
    integer-based ``_match_field`` logic can evaluate them uniformly.
    """

    def test_verify_sun_maps_to_zero(self):
        """Validate that ``sun`` normalizes to ``"0"``.

        The test exercises ``"sun"`` and asserts the result is ``"0"`` because
        cron convention maps Sunday to 0 for downstream numeric matching.
        """
        assert CronSchedule._normalize_dow("sun") == "0"

    def test_verify_mon_maps_to_one(self):
        """Validate that ``mon`` normalizes to ``"1"``.

        The test exercises ``"mon"`` and asserts the result is ``"1"`` because
        Monday is the second day in the cron week starting from Sunday=0.
        """
        assert CronSchedule._normalize_dow("mon") == "1"

    def test_verify_tue_maps_to_two(self):
        """Validate that ``tue`` normalizes to ``"2"``.

        The test exercises ``"tue"`` and asserts the result is ``"2"`` because
        Tuesday follows Monday in the zero-indexed cron day numbering.
        """
        assert CronSchedule._normalize_dow("tue") == "2"

    def test_verify_wed_maps_to_three(self):
        """Validate that ``wed`` normalizes to ``"3"``.

        The test exercises ``"wed"`` and asserts the result is ``"3"`` because
        Wednesday is the fourth day of the cron week.
        """
        assert CronSchedule._normalize_dow("wed") == "3"

    def test_verify_thu_maps_to_four(self):
        """Validate that ``thu`` normalizes to ``"4"``.

        The test exercises ``"thu"`` and asserts the result is ``"4"`` because
        Thursday is the fifth day of the cron week.
        """
        assert CronSchedule._normalize_dow("thu") == "4"

    def test_verify_fri_maps_to_five(self):
        """Validate that ``fri`` normalizes to ``"5"``.

        The test exercises ``"fri"`` and asserts the result is ``"5"`` because
        Friday is the sixth day of the cron week.
        """
        assert CronSchedule._normalize_dow("fri") == "5"

    def test_verify_sat_maps_to_six(self):
        """Validate that ``sat`` normalizes to ``"6"``.

        The test exercises ``"sat"`` and asserts the result is ``"6"`` because
        Saturday is the seventh and final day of the cron week.
        """
        assert CronSchedule._normalize_dow("sat") == "6"

    def test_verify_case_insensitive_conversion(self):
        """Validate that uppercase and mixed-case inputs normalize correctly.

        The test exercises ``"MON"`` and ``"Fri"`` and asserts they map to
        ``"1"`` and ``"5"`` respectively because user-supplied cron expressions
        must be case-insensitive for day-name tokens.
        """
        assert CronSchedule._normalize_dow("MON") == "1"
        assert CronSchedule._normalize_dow("Fri") == "5"

    def test_verify_mixed_range_normalized_correctly(self):
        """Validate that ``mon-fri`` normalizes to ``"1-5"``.

        The test exercises a named-day range and asserts the result is ``"1-5"``
        because range expressions must have all constituent names replaced in
        order so the numeric matcher can evaluate the resulting range.
        """
        result = CronSchedule._normalize_dow("mon-fri")
        assert result == "1-5"

    def test_verify_numeric_strings_pass_through_unchanged(self):
        """Validate that an already-numeric day string is returned as-is.

        The test exercises ``"5"`` and asserts the result is ``"5"`` because
        numeric day-of-week values require no conversion and must not be
        modified by the normalizer.
        """
        assert CronSchedule._normalize_dow("5") == "5"


# ===========================================================================
# CronSchedule._weekday_cron
# ===========================================================================

class TestWeekdayCron:
    """Engineered to validate ``CronSchedule._weekday_cron`` calendar mapping.

    This test class exercises the Python ``datetime`` weekday-to-cron-weekday
    conversion across 3 scenarios using known calendar dates. The design
    ensures the helper correctly maps Python's ``weekday()`` (Monday=0) to
    cron's ``wday`` (Sunday=0) so that next-fire calculations align with
    the cron day-of-week field semantics.
    """

    def test_verify_known_monday_date(self):
        """Validate that 2024-01-01 (Monday) maps to cron weekday 1.

        The test exercises the known date and asserts the result is 1 because
        Python's Monday=0 must become cron's Monday=1 to align with the
        cron day-of-week numbering where Sunday is 0.
        """
        wday = CronSchedule._weekday_cron(2024, 1, 1)
        assert wday == 1  # Monday is numbered 1 in cron

    def test_verify_known_sunday_date(self):
        """Validate that 2024-01-07 (Sunday) maps to cron weekday 0.

        The test exercises the known date and asserts the result is 0 because
        Sunday is the zero-indexed anchor in cron's weekday numbering.
        """
        wday = CronSchedule._weekday_cron(2024, 1, 7)
        assert wday == 0  # Sunday is numbered 0 in cron

    def test_verify_known_friday_date(self):
        """Validate that 2026-05-15 (Friday) maps to cron weekday 5.

        The test exercises the known date and asserts the result is 5 because
        Friday is the sixth day in cron's week starting from Sunday=0.
        """
        wday = CronSchedule._weekday_cron(2026, 5, 15)
        assert wday == 5  # Friday is numbered 5 in cron


# ===========================================================================
# CronSchedule.next_fire()
# ===========================================================================

class TestNextFire:
    """Engineered to validate ``CronSchedule.next_fire`` future-tick computation.

    This test class exercises the next-fire calculation across 6 scenarios
    covering every-minute, specific-minute, daily-9am, weekdays-only, Monday-only,
    and future-only guarantees. The design ensures the scheduler computes the
    next valid cron tick strictly after the reference timestamp so that jobs
    never fire on past occurrences and never return a time in the past.
    """

    def test_verify_next_fire_every_minute_is_within_62_seconds(self):
        """Validate that ``* * * * *`` returns a future timestamp within 62 seconds.

        The test exercises an every-minute schedule against the current time and
        asserts the result is non-None, greater than now, and within 62 seconds
        because the next minute boundary is at most one minute plus one second
        away from any arbitrary reference point.
        """
        cs = CronSchedule.parse("* * * * *")
        now = time.time()
        nf = cs.next_fire(now)
        assert nf is not None
        assert nf > now
        assert nf - now <= 62

    def test_verify_next_fire_specific_minute(self):
        """Validate that ``7 * * * *`` lands on minute 7 of the next hour.

        The test exercises a schedule firing at minute 7 of every hour and
        asserts the returned timestamp has ``tm_min == 7`` and is in the future
        because the next-fire algorithm must advance to the next occurrence of
        the target minute, not the current one if it has already passed.
        """
        cs = CronSchedule.parse("7 * * * *")
        now = time.time()
        nf = cs.next_fire(now)
        assert nf is not None
        assert nf > now
        t = time.localtime(nf)
        assert t.tm_min == 7

    def test_verify_next_fire_daily_9am(self):
        """Validate that ``0 9 * * *`` lands at 9:00 AM on the next day.

        The test exercises a daily-9am schedule and asserts the result has
        ``tm_hour == 9`` and ``tm_min == 0`` because the next occurrence of
        a fixed-hour schedule must align precisely to that wall-clock time.
        """
        cs = CronSchedule.parse("0 9 * * *")
        now = time.time()
        nf = cs.next_fire(now)
        assert nf is not None
        t = time.localtime(nf)
        assert t.tm_hour == 9
        assert t.tm_min == 0

    def test_verify_next_fire_weekdays_only(self):
        """Validate that ``0 9 * * 1-5`` lands on a weekday at 9:00 AM.

        The test exercises a weekday-only schedule and asserts the result is
        a weekday (Python ``tm_wday`` 0-4) at hour 9 because the ``1-5``
        day-of-week filter must exclude Saturday and Sunday from firing.
        """
        cs = CronSchedule.parse("0 9 * * 1-5")
        now = time.time()
        nf = cs.next_fire(now)
        assert nf is not None
        t = time.localtime(nf)
        assert 0 <= t.tm_wday <= 4  # Python indexes Monday as 0 and Friday as 4
        assert t.tm_hour == 9

    def test_verify_next_fire_on_monday(self):
        """Validate that ``0 12 * * mon`` lands on Monday at 12:00 PM.

        The test exercises a Monday-only schedule and asserts the result has
        ``tm_wday == 0`` (Python Monday) and ``tm_hour == 12`` because named
        day normalization must correctly restrict firing to the target weekday.
        """
        cs = CronSchedule.parse("0 12 * * mon")
        now = time.time()
        nf = cs.next_fire(now)
        assert nf is not None
        t = time.localtime(nf)
        assert t.tm_wday == 0  # Python indexes Monday as 0
        assert t.tm_hour == 12

    def test_verify_next_fire_always_returns_future(self):
        """Validate that ``next_fire`` never returns a timestamp at or before now.

        The test exercises an every-minute schedule and asserts the result is
        strictly greater than ``now`` because the next-fire algorithm must
        skip the current tick and return the next occurrence in the future.
        """
        cs = CronSchedule.parse("* * * * *")
        now = time.time()
        nf = cs.next_fire(now)
        assert nf > now


# ===========================================================================
# Leap year handling
# ===========================================================================

class TestLeapYearHandling:
    """Engineered to validate leap-year-aware next-fire computation.

    This test class exercises February 29 scheduling across 3 scenarios
    covering leap-year matches, non-leap-year skipping, and February 28
    regular firing. The design ensures the scheduler correctly handles the
    4-year leap-cycle boundary so that ``29 2 *`` only fires in years
    divisible by 4 (with century rules), preventing infinite loops or
    missed occurrences when the target date does not exist in a given year.
    """

    def test_verify_feb_29_matches_in_leap_year(self):
        """Validate that ``0 12 29 2 *`` fires on 2024-02-29.

        The test exercises a schedule targeting Feb 29 starting from 2024-02-28
        11:59 and asserts the next fire lands on 2024-02-29 at noon because
        2024 is a leap year and the date exists in that calendar year.
        """
        cs = CronSchedule.parse("0 12 29 2 *")
        from datetime import datetime
        ts = datetime(2024, 2, 28, 11, 59).timestamp()
        nf = cs.next_fire(ts)
        assert nf is not None
        t = time.localtime(nf)
        assert t.tm_mon == 2
        assert t.tm_mday == 29
        assert t.tm_year == 2024

    def test_verify_feb_29_skips_non_leap_year(self):
        """Validate that ``0 12 29 2 *`` skips 2023 and fires in 2024.

        The test exercises a Feb 29 schedule starting from 2023-12-01 and
        asserts the next fire lands in 2024 (the next leap year) because
        2023 is not a leap year and Feb 29 does not exist there.
        """
        cs = CronSchedule.parse("0 12 29 2 *")
        from datetime import datetime
        ts = datetime(2023, 12, 1, 0, 0).timestamp()
        nf = cs.next_fire(ts)
        assert nf is not None
        t = time.localtime(nf)
        assert t.tm_year == 2024
        assert t.tm_mon == 2
        assert t.tm_mday == 29

    def test_verify_feb_28_fires_in_all_years(self):
        """Validate that ``0 12 28 2 *`` fires normally in a leap year.

        The test exercises a Feb 28 schedule starting from 2024-02-27 and
        asserts the next fire lands on 2024-02-28 at noon because Feb 28
        exists in every year and is unaffected by leap-year rules.
        """
        cs = CronSchedule.parse("0 12 28 2 *")
        from datetime import datetime
        ts = datetime(2024, 2, 27, 0, 0).timestamp()
        nf = cs.next_fire(ts)
        assert nf is not None
        t = time.localtime(nf)
        assert t.tm_mon == 2
        assert t.tm_mday == 28


# ===========================================================================
# ScheduledJob serialization
# ===========================================================================

class TestScheduledJob:
    """Engineered to validate ``ScheduledJob`` serialization and construction.

    This test class exercises job creation, dict serialization, and dict
    deserialization across 6 scenarios covering one-shot jobs, recurring
    jobs, full metadata, missing fields, and round-trip fidelity. The design
    ensures that jobs can be persisted to and restored from JSON without
    losing schedule type, cron expression, fire-at timestamp, or metadata,
    which is critical for durable scheduler restarts.
    """

    def test_verify_create_one_shot_job(self):
        """Validate that a one-shot job initializes with ``JobState.PENDING``.

        The test exercises construction of a ``ONE_SHOT`` job with an explicit
        ``fire_at`` timestamp and asserts the job ID, state, and ``cron=None``
        because one-shot jobs carry no recurring schedule and start pending.
        """
        job = ScheduledJob(
            id="abc123",
            name="Reminder",
            prompt="Check the deploy",
            schedule_type=ScheduleType.ONE_SHOT,
            fire_at=time.time() + 300,
        )
        assert job.id == "abc123"
        assert job.state == JobState.PENDING
        assert job.cron is None

    def test_verify_create_recurring_job(self):
        """Validate that a recurring job stores the parsed ``CronSchedule``.

        The test exercises construction of a ``RECURRING`` job with a parsed
        cron expression and asserts ``schedule_type`` is ``RECURRING`` and
        ``cron`` is non-None because recurring jobs must carry a valid
        schedule object to compute next-fire times.
        """
        cs = CronSchedule.parse("0 9 * * 1-5")
        job = ScheduledJob(
            id="rec1",
            name="Daily report",
            prompt="Generate daily report",
            schedule_type=ScheduleType.RECURRING,
            cron=cs,
        )
        assert job.schedule_type == ScheduleType.RECURRING
        assert job.cron is not None

    def test_verify_to_dict_serializes_all_fields(self):
        """Validate that ``to_dict`` serializes all job fields correctly.

        The test exercises a fully-populated recurring job and asserts the
        resulting dict contains the correct id, name, cron expression, fail
        counts, metadata, and state because serialization must preserve every
        field for disk persistence and network transmission.
        """
        cs = CronSchedule.parse("0 9 * * *")
        job = ScheduledJob(
            id="test1",
            name="Test job",
            prompt="Run tests",
            schedule_type=ScheduleType.RECURRING,
            cron=cs,
            fail_count=2,
            max_failures=5,
            metadata={"key": "value"},
        )
        d = job.to_dict()
        assert d["id"] == "test1"
        assert d["name"] == "Test job"
        assert d["cron"] == "0 9 * * *"
        assert d["fail_count"] == 2
        assert d["max_failures"] == 5
        assert d["metadata"]["key"] == "value"
        assert d["state"] == "PENDING"

    def test_verify_from_dict_recurring_job(self):
        """Validate that ``from_dict`` reconstructs a recurring job from JSON.

        The test exercises a full dict representation of a recurring job and
        asserts the reconstructed object has the correct id, schedule type,
        and parsed cron minute field because deserialization must restore
        the job to an operational state identical to its construction.
        """
        data = {
            "id": "test2",
            "name": "Cron job",
            "prompt": "do stuff",
            "schedule_type": "RECURRING",
            "cron": "*/10 * * * *",
            "fire_at": None,
            "state": "PENDING",
            "created_at": 1700000000.0,
            "last_fired": None,
            "last_result": None,
            "fail_count": 0,
            "max_failures": 3,
            "metadata": {},
            "agent_config": None,
        }
        job = ScheduledJob.from_dict(data)
        assert job.id == "test2"
        assert job.schedule_type == ScheduleType.RECURRING
        assert job.cron is not None
        assert job.cron.minute == "*/10"

    def test_verify_from_dict_one_shot_job(self):
        """Validate that ``from_dict`` reconstructs a one-shot job from JSON.

        The test exercises a full dict representation of a one-shot job and
        asserts the reconstructed object has ``ONE_SHOT`` type, ``cron=None``,
        and the correct ``fire_at`` timestamp because one-shot jobs must not
        carry a cron schedule after deserialization.
        """
        data = {
            "id": "os1",
            "name": "One-shot",
            "prompt": "Do it once",
            "schedule_type": "ONE_SHOT",
            "cron": None,
            "fire_at": 1700001000.0,
            "state": "PENDING",
            "created_at": 1700000000.0,
            "last_fired": None,
            "last_result": None,
            "fail_count": 0,
            "max_failures": 3,
            "metadata": {},
            "agent_config": None,
        }
        job = ScheduledJob.from_dict(data)
        assert job.schedule_type == ScheduleType.ONE_SHOT
        assert job.cron is None
        assert job.fire_at == 1700001000.0

    def test_verify_from_dict_missing_cron_is_safe(self):
        """Validate that ``from_dict`` handles a missing cron field gracefully.

        The test exercises a dict with ``schedule_type=RECURRING`` but
        ``cron=None`` and asserts ``job.cron`` is ``None`` because durable
        storage must tolerate partially-written or corrupted entries without
        raising on job creation.
        """
        data = {
            "id": "x",
            "name": "x",
            "prompt": "x",
            "schedule_type": "RECURRING",
            "cron": None,
        }
        job = ScheduledJob.from_dict(data)
        assert job.cron is None


# ===========================================================================
# EncreScheduler: scheduling and cancellation
# ===========================================================================

class TestEncreSchedulerBasic:
    """Engineered to validate ``EncreScheduler`` core scheduling operations.

    This test class exercises job scheduling, cancellation, listing, and
    retrieval across 11 scenarios to ensure the in-memory scheduler maintains
    correct job state transitions and supports filtered queries. The design
    validates that recurring and one-shot jobs are created with proper IDs,
    that cancellation transitions jobs to ``CANCELLED``, and that listing
    with state filters returns only matching jobs so the UI can present
    an accurate job roster.
    """

    def test_verify_schedule_recurring_job(self):
        """Validate that scheduling a recurring job returns a non-empty ID.

        The test exercises ``schedule()`` with a cron expression and asserts
        the returned ID is non-empty and the job can be retrieved with the
        correct name and ``RECURRING`` type because recurring job creation
        must persist the schedule and make the job queryable immediately.
        """
        sched = EncreScheduler()
        job_id = sched.schedule(
            name="Test recurring",
            prompt="Run something",
            cron="0 9 * * *",
        )
        assert job_id is not None
        assert len(job_id) > 0
        job = sched.get_job(job_id)
        assert job is not None
        assert job.name == "Test recurring"
        assert job.schedule_type == ScheduleType.RECURRING

    def test_verify_schedule_one_shot_job(self):
        """Validate that scheduling a one-shot job stores the fire_at time.

        The test exercises ``schedule()`` with an explicit ``fire_at`` and
        asserts the retrieved job has ``ONE_SHOT`` type and a non-None
        ``fire_at`` because one-shot jobs must record their execution time
        so the scheduler loop can fire them at the correct moment.
        """
        sched = EncreScheduler()
        job_id = sched.schedule(
            name="Test one-shot",
            prompt="Run once",
            fire_at=time.time() + 3600,
        )
        job = sched.get_job(job_id)
        assert job is not None
        assert job.schedule_type == ScheduleType.ONE_SHOT
        assert job.fire_at is not None

    def test_verify_schedule_no_cron_no_fire_at_defaults_to_immediate(self):
        """Validate that omitting both cron and fire_at creates an immediate one-shot.

        The test exercises ``schedule()`` with only name and prompt and asserts
        the job is ``ONE_SHOT`` with a ``fire_at`` within 5 seconds of now
        because the scheduler must provide a sensible default so users can
        schedule ad-hoc jobs without specifying a future time.
        """
        sched = EncreScheduler()
        job_id = sched.schedule(name="Immediate", prompt="Go")
        job = sched.get_job(job_id)
        assert job is not None
        assert job.schedule_type == ScheduleType.ONE_SHOT
        assert job.fire_at is not None
        assert abs(job.fire_at - time.time()) < 5

    def test_verify_cancel_existing_job(self):
        """Validate that canceling an existing job transitions it to ``CANCELLED``.

        The test exercises ``cancel()`` on a freshly scheduled recurring job
        and asserts the method returns ``True`` and the job state becomes
        ``CANCELLED`` because cancellation must be observable via both the
        return value and the persisted state transition.
        """
        sched = EncreScheduler()
        job_id = sched.schedule(name="Cancel me", prompt="...", cron="0 9 * * *")
        assert sched.cancel(job_id) is True
        job = sched.get_job(job_id)
        assert job.state == JobState.CANCELLED

    def test_verify_cancel_nonexistent_job_returns_false(self):
        """Validate that canceling a non-existent job returns ``False``.

        The test exercises ``cancel()`` with an unknown ID and asserts ``False``
        is returned because the cancel operation must be a no-op for missing
        jobs rather than raising an exception, allowing idempotent cancel calls.
        """
        sched = EncreScheduler()
        assert sched.cancel("nonexistent") is False

    def test_verify_cancel_all_clears_all_jobs(self):
        """Validate that ``cancel_all()`` transitions every job to ``CANCELLED``.

        The test exercises scheduling five jobs then calling ``cancel_all()``
        and asserts the return count is 5 and every job's state is ``CANCELLED``
        because bulk cancellation must be exhaustive and return the number of
        jobs it affected so the caller knows how many were actually present.
        """
        sched = EncreScheduler()
        ids = [sched.schedule(name=f"job{i}", prompt="...", cron="0 9 * * *") for i in range(5)]
        count = sched.cancel_all()
        assert count == 5
        for jid in ids:
            assert sched.get_job(jid).state == JobState.CANCELLED

    def test_verify_list_jobs_returns_all_active_jobs(self):
        """Validate that ``list_jobs()`` returns all non-cancelled jobs.

        The test exercises scheduling two jobs and asserts ``list_jobs()``
        returns exactly 2 because the default list should include all jobs
        that are not in a terminal state so the UI shows active work.
        """
        sched = EncreScheduler()
        sched.schedule(name="A", prompt="A", cron="* * * * *")
        sched.schedule(name="B", prompt="B", cron="0 0 * * *")
        jobs = sched.list_jobs()
        assert len(jobs) == 2

    def test_verify_list_jobs_filtered_by_state(self):
        """Validate that ``list_jobs(state=...)`` returns only matching jobs.

        The test exercises scheduling a job, canceling it, then filtering by
        ``CANCELLED`` and ``PENDING`` states and asserts the counts are 1 and
        0 respectively because state-filtered queries must isolate jobs by
        their lifecycle position for dashboard grouping.
        """
        sched = EncreScheduler()
        jid = sched.schedule(name="A", prompt="A", cron="* * * * *")
        sched.cancel(jid)
        cancelled = sched.list_jobs(state=JobState.CANCELLED)
        assert len(cancelled) == 1
        pending = sched.list_jobs(state=JobState.PENDING)
        assert len(pending) == 0

    def test_verify_list_jobs_can_include_finished_one_shot_history(self):
        """Validate that ``include_finished=True`` surfaces completed one-shots.

        The test schedules a one-shot, sets its state to ``COMPLETED``, and
        asserts it is excluded from the default list but included when
        ``include_finished=True`` because finished jobs must be opt-in to
        keep the default view focused on active work.
        """
        sched = EncreScheduler()
        job_id = sched.schedule(name="Finished", prompt="...", fire_at=time.time() + 60)
        job = sched.get_job(job_id)
        assert job is not None
        job.state = JobState.COMPLETED

        assert sched.list_jobs() == []
        assert sched.list_jobs(include_finished=True) == [job]

    def test_verify_get_job_nonexistent_returns_none(self):
        """Validate that querying a non-existent job returns ``None``.

        The test exercises ``get_job()`` with an unknown ID and asserts the
        result is ``None`` because missing-job lookups must be safe no-ops
        that callers can check with a simple truthiness test.
        """
        sched = EncreScheduler()
        assert sched.get_job("nonexistent") is None


# ===========================================================================
# EncreScheduler: durable persistence
# ===========================================================================

class TestEncreSchedulerDurability:
    """Engineered to validate ``EncreScheduler`` durable JSON persistence.

    This test class exercises save-load cycles, cancellation persistence,
    parent-directory creation, missing-file handling, corrupted-JSON recovery,
    and bad-entry skipping across 6 scenarios. The design ensures that job
    state survives process restarts by writing to a JSON file on every
    mutation and reading it back on construction, so that long-running
    scheduler instances recover their full job roster after a crash or deploy.
    """

    def test_verify_durable_save_and_load(self):
        """Validate that jobs survive a scheduler restart via disk persistence.

        The test exercises creating two jobs (one recurring, one one-shot) in
        ``sched1``, then constructing ``sched2`` with the same ``durable_path``
        and asserting both jobs are retrievable with their original names,
        types, and cron values because disk persistence must round-trip all
        job metadata through the JSON store.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "jobs.json")
            sched1 = EncreScheduler(durable_path=path)
            jid1 = sched1.schedule(name="Persistent", prompt="Run forever", cron="0 9 * * 1-5")
            jid2 = sched1.schedule(name="One-off", prompt="Run once", fire_at=time.time() + 99999)

            sched2 = EncreScheduler(durable_path=path)
            job1 = sched2.get_job(jid1)
            job2 = sched2.get_job(jid2)

            assert job1 is not None
            assert job1.name == "Persistent"
            assert job1.schedule_type == ScheduleType.RECURRING
            assert job1.cron is not None

            assert job2 is not None
            assert job2.name == "One-off"
            assert job2.schedule_type == ScheduleType.ONE_SHOT

    def test_verify_durable_persists_cancel_state(self):
        """Validate that a canceled job remains canceled after restart.

        The test exercises scheduling a job, canceling it in ``sched1``, then
        loading from the same path in ``sched2`` and asserting the job state
        is still ``CANCELLED`` because cancellation is a state mutation that
        must be persisted to disk just like any other state transition.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "jobs2.json")
            sched1 = EncreScheduler(durable_path=path)
            jid = sched1.schedule(name="Cancel me", prompt="...", cron="* * * * *")
            sched1.cancel(jid)

            sched2 = EncreScheduler(durable_path=path)
            job = sched2.get_job(jid)
            assert job is not None
            assert job.state == JobState.CANCELLED

    def test_verify_durable_creates_parent_directory(self):
        """Validate that the scheduler creates missing parent directories for the store file.

        The test exercises a ``durable_path`` inside a non-existent nested
        directory and asserts the JSON file is created after scheduling a
        job because the persistence layer must be self-initializing so users
        do not need to pre-create directory structures.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "subdir", "nested", "jobs.json")
            sched = EncreScheduler(durable_path=path)
            sched.schedule(name="Nested save", prompt="...", cron="* * * * *")
            assert os.path.exists(path)

    def test_verify_durable_no_file_no_error(self):
        """Validate that a missing store file does not raise on scheduler construction.

        The test exercises a ``durable_path`` pointing to a non-existent JSON
        file and asserts ``_jobs`` is an empty dict because a fresh scheduler
        must start cleanly even when no prior persistence file exists.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "nonexistent.json")
            sched = EncreScheduler(durable_path=path)
            assert sched._jobs == {}

    def test_verify_durable_corrupted_json_recovers_gracefully(self):
        """Validate that a corrupted JSON file is treated as an empty store.

        The test exercises a store file containing invalid JSON text and asserts
        ``_jobs`` is an empty dict because the persistence loader must catch
        ``JSONDecodeError`` and fall back to an empty schedule rather than
        crashing the scheduler on startup.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "corrupt.json")
            Path(path).write_text("not valid json at all", encoding="utf-8")
            sched = EncreScheduler(durable_path=path)
            assert sched._jobs == {}

    def test_verify_durable_bad_entry_is_skipped(self):
        """Validate that incomplete job entries are skipped without crashing.

        The test exercises a store file containing a dict missing required keys
        and asserts ``_jobs`` is empty because the loader must catch ``KeyError``
        from ``ScheduledJob.from_dict`` and skip malformed entries so that a
        single corrupt record does not prevent recovery of valid jobs.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "partial.json")
            data = [{"id": "x"}]  # Entry deliberately missing required fields
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            sched = EncreScheduler(durable_path=path)
            assert sched._jobs == {}


# ===========================================================================
# EncreScheduler: missed recurring occurrences must not fire late
# ===========================================================================

class TestRecurringMissedOccurrences:
    """Engineered to validate that missed recurring occurrences are skipped, not fired late.

    This test class exercises three post-startup scenarios to ensure the
    scheduler never executes a recurring job for a time that passed before
    the process was running. The design enforces that ``_started_at`` acts
    as a lower bound: any occurrence whose scheduled time is before the
    session start is considered missed and the reference advances past it
    without spawning the job, preventing backlog explosions on restart.
    """

    def _run_one_poll(self, sched: EncreScheduler, monkeypatch: pytest.MonkeyPatch) -> list[ScheduledJob]:
        """Run a single scheduler poll iteration and collect spawned jobs.

        Args:
            sched: The scheduler instance to poll.
            monkeypatch: Pytest fixture for patching asyncio.sleep and _spawn_job.

        Returns:
            A list of ``ScheduledJob`` instances that were spawned during the poll.
        """
        spawned: list[ScheduledJob] = []
        monkeypatch.setattr(sched, "_spawn_job", lambda j: spawned.append(j))

        async def _stop(seconds: float) -> None:
            """Replace ``asyncio.sleep`` so a single poll iteration exits instead of blocking."""
            sched._running = False

        monkeypatch.setattr("asyncio.sleep", _stop)
        sched._running = True
        import asyncio
        asyncio.run(sched._loop())
        return spawned

    def test_verify_occurrence_before_session_start_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Validate that a missed 10:00 occurrence is skipped when the server started at 12:05.

        The test sets the reference time to 12:05 on 2026-08-07, schedules a
        daily 10:00 job created at 09:30 that never fired, and runs one poll
        iteration. It asserts no job is spawned, the state remains ``PENDING``,
        and ``last_fired`` advances to 10:00 because the missed occurrence
        must be skipped and the internal reference must move past it.
        """
        now = time.mktime(time.strptime("2026-08-07 12:05:30", "%Y-%m-%d %H:%M:%S"))
        monkeypatch.setattr("encre.scheduler.time.time", lambda: now)

        sched = EncreScheduler(durable_path=str(tmp_path / "jobs.json"))
        job_id = sched.schedule(name="10am job", prompt="...", cron="0 10 * * *")
        job = sched.get_job(job_id)
        assert job is not None
        job.created_at = time.mktime(time.strptime("2026-08-07 09:30:00", "%Y-%m-%d %H:%M:%S"))
        job.last_fired = None
        sched._started_at = now

        spawned = self._run_one_poll(sched, monkeypatch)

        assert spawned == []
        assert job.state == JobState.PENDING
        assert job.last_fired == time.mktime(time.strptime("2026-08-07 10:00:00", "%Y-%m-%d %H:%M:%S"))

    def test_verify_occurrence_before_session_start_with_previous_run_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Validate that a missed today-10:00 occurrence is skipped when the last fire was yesterday.

        The test sets ``last_fired`` to 2026-08-06 10:00 and the session start
        to 2026-08-07 12:05, then runs one poll. It asserts no job is spawned,
        state stays ``PENDING``, and ``last_fired`` advances to today's 10:00
        because even a job with a prior fire history must skip occurrences
        that fall before the current session started.
        """
        now = time.mktime(time.strptime("2026-08-07 12:05:30", "%Y-%m-%d %H:%M:%S"))
        monkeypatch.setattr("encre.scheduler.time.time", lambda: now)

        sched = EncreScheduler(durable_path=str(tmp_path / "jobs.json"))
        job_id = sched.schedule(name="10am job", prompt="...", cron="0 10 * * *")
        job = sched.get_job(job_id)
        assert job is not None
        job.created_at = time.mktime(time.strptime("2026-08-05 09:00:00", "%Y-%m-%d %H:%M:%S"))
        job.last_fired = time.mktime(time.strptime("2026-08-06 10:00:00", "%Y-%m-%d %H:%M:%S"))
        sched._started_at = now

        spawned = self._run_one_poll(sched, monkeypatch)

        assert spawned == []
        assert job.state == JobState.PENDING
        assert job.last_fired == time.mktime(time.strptime("2026-08-07 10:00:00", "%Y-%m-%d %H:%M:%S"))

    def test_verify_occurrence_within_session_fires_normally(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Validate that an occurrence that fell during the active session fires normally.

        The test sets the session start to 09:00 on 2026-08-07 (before the
        10:00 target) and runs one poll. It asserts the job is spawned because
        the 10:00 occurrence happened while the scheduler was online and must
        be executed as part of normal catch-up behavior.
        """
        now = time.mktime(time.strptime("2026-08-07 12:05:30", "%Y-%m-%d %H:%M:%S"))
        monkeypatch.setattr("encre.scheduler.time.time", lambda: now)

        sched = EncreScheduler(durable_path=str(tmp_path / "jobs.json"))
        job_id = sched.schedule(name="10am job", prompt="...", cron="0 10 * * *")
        job = sched.get_job(job_id)
        assert job is not None
        job.created_at = time.mktime(time.strptime("2026-08-06 09:00:00", "%Y-%m-%d %H:%M:%S"))
        job.last_fired = time.mktime(time.strptime("2026-08-06 10:00:00", "%Y-%m-%d %H:%M:%S"))
        sched._started_at = time.mktime(time.strptime("2026-08-07 09:00:00", "%Y-%m-%d %H:%M:%S"))

        spawned = self._run_one_poll(sched, monkeypatch)

        assert spawned == [job]


# ===========================================================================
# Job lifecycle callbacks
# ===========================================================================

class TestJobCallbacks:
    """Engineered to validate ``EncreScheduler`` lifecycle callbacks and history.

    This test class exercises on-job-complete callbacks, metadata and agent-config
    persistence, automation stream session-ID allocation, execution history
    retention across job deletion, and history override precedence across 6
    scenarios. The design ensures that external observers can hook into job
    completion, that scheduled jobs carry arbitrary metadata and agent
    configuration, and that execution history is preserved independently of
    job lifecycle so that audit logs survive job deletion.
    """

    def test_verify_on_job_complete_callback_registered(self):
        """Validate that ``on_job_complete`` stores the callback for later invocation.

        The test exercises registering a callback and asserts that ``_on_complete``
        holds the exact same function object because the callback registry must
        preserve the reference so it can be invoked when a job transitions to
        a terminal state.
        """
        sched = EncreScheduler()
        results: list[ScheduledJob] = []

        def callback(job):
            """Append the completed job to the results collector."""
            results.append(job)

        sched.on_job_complete(callback)
        assert sched._on_complete is callback

    def test_verify_schedule_with_metadata(self):
        """Validate that metadata dict is preserved on the scheduled job.

        The test exercises scheduling a job with ``metadata={"priority": "high",
        "tags": ["critical"]}`` and asserts both fields are accessible on the
        retrieved job because arbitrary metadata must be stored and retrievable
        for job tagging and filtering in the UI.
        """
        sched = EncreScheduler()
        jid = sched.schedule(
            name="Meta job",
            prompt="...",
            cron="0 0 * * *",
            metadata={"priority": "high", "tags": ["critical"]},
        )
        job = sched.get_job(jid)
        assert job.metadata["priority"] == "high"
        assert "critical" in job.metadata["tags"]

    def test_verify_schedule_with_agent_config(self):
        """Validate that agent_config is stored and accessible on the job.

        The test exercises scheduling a job with an ``agent_config`` dict and
        asserts ``_agent_config`` is non-None and contains the model name because
        agent-specific configuration must travel with the job so the executor
        knows which model and turn limit to use at fire time.
        """
        sched = EncreScheduler()
        jid = sched.schedule(
            name="Agent job",
            prompt="...",
            cron="0 0 * * *",
            agent_config={"model": "claude-sonnet-4-20250514", "max_turns": 15},
        )
        job = sched.get_job(jid)
        assert job._agent_config is not None
        assert job._agent_config["model"] == "claude-sonnet-4-20250514"

    def test_verify_execute_job_streams_snapshot_with_preallocated_session_id(self, tmp_path: Path):
        """Validate that job execution allocates a stable session ID and streams events.

        The test exercises a one-shot job with a fake agent loop that records the
        received session ID, then runs ``_execute_job`` and asserts the job state
        becomes ``COMPLETED``, the execution history contains the same session ID,
        and the ``start`` and ``snapshot`` progress events both carry that session
        ID because automation streams must maintain a single session identity from
        job start through message snapshot delivery.
        """
        class FakeLoop:
            """Stand-in agent loop that records the injected session ID and streams a fixed message pair."""

            def __init__(self):
                """Initialize with an empty recorded session ID."""
                self.received_session_id = ""

            async def _run_sub_agent(self, **kwargs):
                """Capture the passed session ID, emit a two-message snapshot, and return it."""
                self.received_session_id = kwargs["session_id"]
                await kwargs["progress_callback"]([
                    {"role": "user", "content": "Run report"},
                    {"role": "assistant", "content": "Report complete"},
                ])
                return {
                    "content": "Report complete",
                    "messages": [],
                    "session_id": self.received_session_id,
                }

        fake_loop = FakeLoop()
        scheduler = EncreScheduler(durable_path=str(tmp_path / "jobs.json"))
        job_id = scheduler.schedule(name="Report", prompt="Run report", fire_at=time.time())
        job = scheduler.get_job(job_id)
        assert job is not None
        scheduler._agent_factory = lambda _config: SimpleNamespace(loop=fake_loop)

        events: list[tuple[str, dict[str, object]]] = []

        async def capture(_job, event_type, event_data):
            """Record each progress event as a (type, data) pair for later assertion."""
            events.append((event_type, event_data))

        scheduler.on_job_progress(capture)
        import asyncio
        asyncio.run(scheduler._execute_job(job))

        execution = scheduler.get_execution_history()[0]
        assert job.state == JobState.COMPLETED
        assert execution.session_id == fake_loop.received_session_id
        assert execution.session_id
        assert scheduler.list_jobs(include_finished=True) == [job]

        start = next(data for event_type, data in events if event_type == "start")
        snapshot = next(data for event_type, data in events if event_type == "snapshot")
        assert start["session_id"] == execution.session_id
        assert snapshot["session_id"] == execution.session_id
        assert snapshot["messages"] == [
            {"role": "user", "content": "Run report"},
            {"role": "assistant", "content": "Report complete"},
        ]

    def test_verify_deleting_job_preserves_global_execution_history(self, tmp_path: Path):
        """Validate that deleting a job does not remove its execution history records.

        The test exercises creating a job, appending a ``JobExecution`` directly
        to the scheduler's history, deleting the job, and asserting the history
        still contains the execution record with the correct job ID and name
        because execution history is a global audit log that must outlive individual
        job records for post-mortem analysis.
        """
        scheduler = EncreScheduler(durable_path=str(tmp_path / "jobs.json"))
        job_id = scheduler.schedule(name="Keep history", prompt="Run", fire_at=time.time() + 60)
        scheduler._executions.append(JobExecution(
            time=time.time(),
            state="COMPLETED",
            result="Done",
            name="Keep history",
            job_id=job_id,
        ))

        assert scheduler.delete_job(job_id)
        history = scheduler.get_execution_history()
        assert len(history) == 1
        assert history[0].job_id == job_id
        assert history[0].name == "Keep history"

    def test_verify_reloading_history_preserves_renamed_execution(self, tmp_path: Path):
        """Validate that dedicated automation history overrides legacy job-execution titles.

        The test exercises writing both a legacy job with an execution entry titled
        ``"Original name"`` and a separate ``automation_history.json`` with a
        matching entry titled ``"Renamed execution"``, then loading the scheduler
        and asserting the returned history uses the dedicated-history title because
        the explicit automation history file must win over legacy embedded data
        to allow users to rename executions without touching the job store.
        """
        path = tmp_path / "jobs.json"
        timestamp = 1234.5
        job_id = "daily_report"
        legacy_job = {
            "id": job_id,
            "name": "Original name",
            "prompt": "Run report",
            "schedule_type": "ONE_SHOT",
            "cron": None,
            "fire_at": timestamp + 60,
            "state": "COMPLETED",
            "executions": [{
                "time": timestamp,
                "state": "COMPLETED",
                "result": "Done",
                "name": "Original name",
                "job_id": job_id,
            }],
        }
        path.write_text(json.dumps([legacy_job]), encoding="utf-8")
        history_path = path.with_name("automation_history.json")
        history_path.write_text(json.dumps([{
            "time": timestamp,
            "state": "COMPLETED",
            "result": "Done",
            "name": "Renamed execution",
            "job_id": job_id,
        }]), encoding="utf-8")

        scheduler = EncreScheduler(durable_path=str(path))

        history = scheduler.get_execution_history()
        assert len(history) == 1
        assert history[0].name == "Renamed execution"


# ===========================================================================
# Enums
# ===========================================================================

class TestEnums:
    """Engineered to validate ``ScheduleType`` and ``JobState`` enum definitions.

    This test class exercises enum value existence, inequality, and string-based
    lookup across 3 scenarios to ensure the enum members are well-formed and
    accessible. The design validates that both enums have all expected members
    and that ``JobState["NAME"]`` bracket-lookup works, which is used throughout
    the codebase for state transition checks and JSON serialization round-trips.
    """

    def test_verify_schedule_type_values_are_distinct(self):
        """Validate that ``ScheduleType`` members are non-None and distinct.

        The test asserts ``ONE_SHOT`` and ``RECURRING`` are both non-None and
        not equal to each other because enum members must form a valid disjoint
        set so that schedule-type checks are unambiguous.
        """
        assert ScheduleType.ONE_SHOT is not None
        assert ScheduleType.RECURRING is not None
        assert ScheduleType.ONE_SHOT != ScheduleType.RECURRING

    def test_verify_job_state_values_are_non_null(self):
        """Validate that all ``JobState`` members are non-None.

        The test asserts ``PENDING``, ``RUNNING``, ``COMPLETED``, ``FAILED``,
        and ``CANCELLED`` are all non-None because every lifecycle state must
        be a valid enum member so state-transition logic can compare against them.
        """
        assert JobState.PENDING is not None
        assert JobState.RUNNING is not None
        assert JobState.COMPLETED is not None
        assert JobState.FAILED is not None
        assert JobState.CANCELLED is not None

    def test_verify_job_state_from_string_lookup(self):
        """Validate that ``JobState["NAME"]`` bracket lookup returns the correct member.

        The test exercises string-based lookup for ``"PENDING"`` and ``"CANCELLED"``
        and asserts they resolve to the corresponding enum members because the
        scheduler deserializes state from JSON strings and must convert them back
        to enum values without raising ``KeyError``.
        """
        assert JobState["PENDING"] == JobState.PENDING
        assert JobState["CANCELLED"] == JobState.CANCELLED
