# Data rules

The rules every provider, data kind and column in TrainingPulse follows. They apply to activities and to sleep, wellness and intraday data alike. Read this before you add a provider, a field, a score or a data kind, and check a change against the [checklist](#checklist) at the end.

The goal: a provider is a replaceable source of measurements. Switching from one wearable to another, or running two side by side, must not change what a column means, which dashboards work, or how a score is calculated.

## 1. The core stores a common baseline

**A field exists only for a standard concept that most current wearables report.** The reference set is Garmin, Fitbit/Google, Oura, Whoop, Polar and Apple. If most of them have it in their API, it can be a column. If only one or two do, it can't, however useful it looks in that provider's app.

- The baseline is defined by the core, not by the first provider that sends a field. For activities it is the `Activity` columns in [`models.py`](../app/models.py); for the other kinds it is the record dataclasses in [`providers/feeds.py`](../app/providers/feeds.py).
- A new provider maps onto the baseline. It never adds a column of its own.
- Each field has one meaning and one unit for every provider. Where providers disagree, the mapper converts. Example: cadence is revolutions per minute when cycling and steps per minute (both legs) on foot; per-leg values from FIT, Strava and intervals.icu are doubled by `mapping.steps_per_minute`.
- Before adding a field, check it against real API responses, not only documentation, and note in the docs which reference providers report it.
- Descriptive metadata (name, description, notes, device, gear) is not a measurement. It can be a nullable column when it helps the user, but it is a deliberate decision, not a default.

## 2. A provider without a field leaves it empty

**Missing data is `None`, never a guess, a zero or a stand-in from a related field.**

- Garmin has no skin temperature, so `skin_temp_deviation_c` stays empty for Garmin. Google has no max heart rate on exercises, so the mapper leaves `max_heartrate` empty and the core fills it from the stream (rule 4).
- A flag is taken from the provider's own field or left at its default. Example: intervals.icu `device_watts` comes only from the provider's `device_watts`, not from whether any power value exists.
- Days or nights a provider returns with every field `null` are skipped, not stored as empty rows.
- Dashboards and MCP tools must handle an empty field for every provider. An empty optional value (for example a night without a stage timeline) is stored as a database `NULL`, not JSON `null`, so SQL that expands it doesn't fail.

## 3. Proprietary scores never become columns

**A provider's own interpretation of the data is not imported.** This covers scores, statuses and indexes such as Garmin's sleep score, stress, Body Battery and HRV status, Strava's relative effort (`suffer_score`) and intervals.icu's training load.

Why:
- Each provider calculates them differently, often on different scales (Garmin stress: higher is worse; Fitbit stress management: higher is better). One column would mean different things per row.
- Most providers don't expose them in their API, so the column would be filled by one provider only.
- They are black boxes. TrainingPulse can't explain or check them.

If the concept is useful, the core builds its own version from baseline data (rule 4), documents the formula and stores the parts next to the score. The sleep and recovery scores in [`wellness_scores.py`](../app/wellness_scores.py) replace provider sleep scores and stress this way; TRIMP, CTL, ATL and TSB replace provider training load. If the core can't build it from baseline data (a stress score needs daytime HRV, which no API provides), it doesn't exist in TrainingPulse.

## 4. The core computes, providers only measure

**Anything the core can derive from samples or streams, the core derives. Mappers pass on what the provider reports and never calculate.**

- Mappers are pure and only translate: field names, units, time zones. No formulas. Example: Garmin and Hammerhead mappers used to calculate kJ from average power × time; that now comes only from `computed_kilojoules` in the stream phase.
- **Activities:** normalized power, work, TRIMP, HR and power zones, best 20-minute power, the power curve and computed calories come from the stream phase in [`ingest/activities.py`](../app/ingest/activities.py) and [`metrics.py`](../app/metrics.py). Provider values for these are not mapped.
- **Wellness:** sleeping heart rate, the sleep score and the recovery score come from samples and daily rows. Provider-reported sleep heart rate, min/max HR and weekly averages are not mapped.
- **Resting heart rate for calculations:** the 7-day median of imported resting HR, from any provider ([`resting_hr.py`](../app/resting_hr.py)). `athlete.rest_hr` is only the fallback when there are no imported values.
- **Athlete profile:** zones and FTP come from `athlete.max_hr` and `athlete.ftp` in the config. A provider's profile is never read for them.
- Derived values are computed from **visible rows only** (after deduplication, rule 6), so they don't depend on which provider supplied the data.

### Exception: activity summary values

Summary values a provider reports for an activity (elapsed and moving time, distance, average and max HR, average and max power, average and max speed, cadence, elevation gain and descent, calories) keep the provider's number. The stream phase (`_missing_summary`) only fills fields the provider left empty.

This is a deliberate choice, not a gap:
- Reconcile compares re-listed provider values with stored values (`_DIFF_FIELDS`). If the stored value were ours, every reconcile would see a change and download details and streams again, which the Strava budget can't afford.
- The device's own summary uses every recorded sample and knows about auto-pause. Streams can be resampled (Strava, intervals.icu) or incomplete (Google's TCX drops trackpoints without GPS).

So: values that summarize a single recording keep the provider's figure; values that interpret it (load, zones, NP, curves, scores) are always ours. Some summary values are not derived even when missing: total descent is not summed from the altitude stream, because raw altitude noise inflates it.

If stream-based summaries are ever wanted, they go in separate `computed_*` columns next to the provider value (like `computed_calories` next to `calories`), and reconcile keeps comparing only the provider columns.

## 5. Samples have a defined resolution

**Every sample metric states how comparable it is across providers.**

- **Comparable metrics** are normalized to one resolution for every provider. Heart rate is stored as 2-minute averages (`HEART_RATE_WINDOW_SECONDS`); finer streams are averaged down.
- **Display-only metrics** keep each device's native resolution and are listed in `DISPLAY_ONLY_METRICS`. SpO2 is one: Google sends per-minute values, Garmin hourly averages. Nothing derives a minimum, maximum or average from them. MCP marks those series `display_only` and reports `interval_seconds`. Comparable numbers come from the daily fields (`spo2_avg`, `spo2_min`).
- A new sample metric needs one of the two decisions before it is imported.
- Raw high-frequency data (for example Google's per-second heart rate) is never stored as is.

## 6. One provider wins each record

**Data from several providers is deduplicated, never merged field by field.**

- Activities use the overlap rule: activities that start within 15 minutes of each other and overlap in time are one activity. Sleep, wellness, intraday, body and nutrition data use the per-day rule: one provider wins each local day.
- The winner is the highest provider in `data.<kind>.precedence` (defaults in `DEFAULT_PRECEDENCE` in [`catalog.py`](../app/catalog.py)). Losing rows are kept and marked `superseded_by`; dashboards and MCP read the `active_*` views.
- A day's intraday samples come from one provider only, so a curve never mixes two devices.
- Known consequence: on the day a user switches devices, the higher-ranked provider wins the whole day even if it was only worn for part of it. This is accepted.
- An activity that disappears upstream is hidden (`deleted_upstream_at`), not deleted, and comes back if it is listed again.

## 7. Days and times are unambiguous

- A night belongs to the day it ends (a 22:45–07:00 sleep is the morning's). Night values in daily wellness (HRV, respiration, SpO2, skin temperature) belong to that same day.
- Per-day data is keyed by the local day in `athlete.timezone`; intraday samples by the local day of their timestamp.
- The main sleep is the night's row. Naps are summed into `nap_seconds` on their day and are not part of `asleep_seconds`.

## Checklist

For a new provider, feed, field or derived value:

- [ ] Is the field a standard concept that most of Garmin, Fitbit/Google, Oura, Whoop, Polar and Apple report? (rule 1)
- [ ] Does it have the same meaning and unit for every provider, with conversions in the mapper? (rule 1)
- [ ] Do providers without it leave it `None`, and do dashboards and MCP cope with that? (rule 2)
- [ ] Is it a measurement, not a provider's score, status or index? (rule 3)
- [ ] Is the mapper pure, with no calculations? Could the core derive the value instead? (rule 4)
- [ ] If it's a new derived value, is it computed in the core, from visible rows only, with its formula documented? (rules 3, 4)
- [ ] If it's a sample metric, is it normalized or listed in `DISPLAY_ONLY_METRICS`? (rule 5)
- [ ] Is the provider placed in `DEFAULT_PRECEDENCE` for every kind it supplies? (rule 6)
- [ ] Are fixtures taken from real responses, with personal data removed? Do `test_mappers_agree_on_the_column_set` and `test_feeds_match_the_catalog_and_their_contracts` pass?
- [ ] Are [data.md](data.md), the provider page and, for a schema change, [upgrading.md](upgrading.md) updated?
