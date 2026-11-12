# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.7.0] - 2026-11-12

### Added

- A **weekly load target**: the training load (TRIMP) of a Monday-to-Sunday week against 7 × your fitness (CTL) at the start of the week. At the target, your fitness stays the same. Up to the upper limit (130 % of the target), it increases. Above it, the load increases faster than you can usually sustain. The target shows after 6 weeks of training history.
- The Today summary's **Form** tile is now **Training**, with a **Week** view: this week's load, how much is still missing to the target, and a bar that marks the load against the target and the upper limit.
- The MCP tool `get_training_weeks` returns the weekly load, the target, the upper limit, and the status (`in_progress`, `below_target`, `on_target`, `above_limit`) for each week of a date range.

### Changed

- The grey band of the **Weekly training load by sport** panel on the Fitness dashboard shows the weekly load target instead of 80 % to 125 % of the average of the four weeks before. The **This week vs corridor** panel is now **This week vs target**. The old band followed whatever you trained in the last four weeks, so a rest week made the next weeks easier to reach, and it did not agree with fitness (CTL).
- The **Form** tile in the Today summary shows form in percent instead of TSB, with its band (for example **Optimal training**) and a bar that marks the value in the bands, like the Recovery tile.
- The Fitness dashboard no longer has the **Reference** section. Its fitness bands, form bands and ramp rate came from the TSS scale and disagreed with the panels above it. The panel descriptions now give the bands that apply.

## [1.6.0] - 2026-11-11

### Changed

- The **recovery score** has a new scale. It now measures HRV (60 %) and resting heart rate (40 %) against your usual values in units of your own day-to-day variation: 70 is a usual day, and each standard deviation better or worse moves the score 15 points. Before, 50 was a usual day and fixed limits (±20 % HRV, ±5 bpm) applied to everyone. The sleep score is no longer part of it. All stored recovery scores are calculated again on the next sync, so past values change. See [Sleep and recovery scores](docs/sleep-and-recovery.md).
- The recovery score needs 14 of the previous 30 days with data instead of 7, so a new device has no recovery score in its first two weeks.
- The recovery score has a status: **Above normal** (85 or more), **Normal** (60–84), **Below normal** (40–59), or **Well below normal**. The Today summary shows it under the score, with a bar that marks the score in the range from 0 to 100, the MCP tools return it as `status`, and the **Recovery** panel on the Sleep and recovery dashboard takes its color from it.
- The Sleep and recovery dashboard has a **Recovery score** panel that shows the score over its status bands, and the **HRV** panel shows your usual range as a band instead of the 30-day average.
- The **Asleep** panel on the Sleep and recovery dashboard shows hours and minutes (for example `7h:37m`) instead of rounding to the full hour.
- The MCP server explains its values to agents: the server instructions define TRIMP, CTL/ATL/TSB, the sleep and recovery scores with their parts, and the calorie methods, and the tool descriptions give units, value lists, and how averages are formed.
- An MCP tool that gets an invalid argument now says what is wrong, for example `metric must be one of heart_rate, hrv, spo2`. Before, the client got only `Error executing tool`.
- The MCP sleep tools return `null` instead of `0` for a value the device does not report, for example deep and REM minutes from a device without sleep stages.
- The Grafana panel descriptions are shorter and use simple English (ASD-STE100). They tell what a panel shows and leave out how TrainingPulse calculates it.
- The **Form** panel on the Fitness dashboard shows form in percent of fitness, so the bands fit every athlete: above +25 % **Transition**, +5 to +25 % **Fresh**, −10 to +5 % **Neutral**, −30 to −10 % **Optimal training**, below −30 % **High risk**. Before, the panel used fixed TSB values from the TSS scale, and its colors, its state name, and its description used three different sets of bands.
- The MCP training-load values give form in percent of fitness and its band, with the same limits as the Form panel. `get_training_load` returns `form_pct` and `form_status` for each day and `lowest_form_pct` in the summary. The `day_training_load` of `get_activity` returns them before and after the day. The server instructions tell agents to use `form_pct`, not TSB, for limits such as "fresh enough for a race", because TSB depends on the heart-rate zones.
- The **Fitness (CTL)** panel no longer shows the bands Low, Intermediate, Advanced and Elite. They came from the TSS scale, and TRIMP values are much higher, so most athletes showed as Elite.
- TRIMP weights a minute in heart-rate zone 5 with 6 instead of 8. Activities processed from now on use the new weight; stored TRIMP, CTL, ATL and TSB change only when an activity is processed again (a full refresh or resync).

### Fixed

- FDDB sugar and fiber were always stored as `0`. They are now read from the diary page. A value the page does not show is stored as empty, not as `0`. Days inside `refresh_days` correct themselves on the next sync; older days need a resync of nutrition.

## [1.5.1] - 2026-11-06

### Changed

- Connected accounts on the status page have **Disconnect** beside their status, so a card without a quota takes one row instead of two.

## [1.5.0] - 2026-11-06

### Added

- The Sleep tile on the status page shows last night's deep, REM, light and awake time as one bar. Nights without stage data show no bar.

### Changed

- The status page has a calmer design: consistent spacing and type, a status dot instead of colored pills on the accounts, one highlighted button, and aligned Today tiles. Faint text is easier to read.
- Account cards on the status page pack together without gaps, whatever their size.
- Accounts that are turned off no longer have a card on the status page. Until the first account works, they share one row under the account cards.
- The status page shows less text: accounts that work have no hint, and turned-off accounts, data source order and athlete settings are explained in the [documentation](docs/README.md). The last 15 minutes of API quota appear only near a limit.
- The sync status on the status page is part of **Your accounts**. **Where your data comes from** is now **Data sources**, and starts closed.
- The small controls on the status page are easier to tap on a phone.
- Numbers on the status page use one format, for example `38 of 2,500` and `104% of usual`.

## [1.4.0] - 2026-11-04

### Added

- The status page starts with a Today summary for a quick look on a phone: last night's sleep, recovery, form, fitness and fatigue, and today's calories and macros, with a switch to this week's daily average.

## [1.3.0] - 2026-11-04

### Added

- The arrow next to **Open dashboards** on the status page opens a menu with a link to each dashboard.

### Changed

- **Open dashboards** shows only the TrainingPulse dashboards, not every dashboard of the Grafana instance.
- The dashboards are in a **TrainingPulse** folder in Grafana. `grafana/sync_dashboards.py` creates the folder; `GRAFANA_FOLDER` sets its name.
- The Sleep and recovery dashboard has the same **Dashboards** menu as the other dashboards.
- The route map on the Activity detail dashboard zooms in to fit the GPS track instead of stopping at city level.

## [1.2.0] - 2026-11-03

### Changed

- MCP: activities report a single `calories` value; `imported_calories` is removed. `calories_method` says whether the value was computed or imported.

## [1.1.1] - 2026-11-03

### Fixed

- Garmin: strength, yoga and other gym sessions recorded with a GPS profile no longer get a moving time of a few minutes of GPS drift, which also made their computed calories far too low.
- Gym sessions no longer show a map, distance, speed, or elevation; for a stationary sport these are only indoor GPS drift and are not stored.
- Garmin: casual and speed walks, pilates, elliptical, and stair stepper sessions no longer have an empty sport. Other unlisted running, cycling, walking, and swimming sub-types get the sport of their Garmin parent type.

## [1.1.0] - 2026-11-02

### Added

- Withings: sleep, daily wellness, and intraday data from Withings watches and sleep trackers. Existing Withings connections must reconnect to grant the new access; see [Upgrading](docs/upgrading.md#from-a-release-with-withings-body-data-only).
- New data kind `blood_pressure`, filled by Withings monitors, with MCP tools and a dashboard panel.
- Bone mass, body water, and visceral fat on body measurements, from Withings and Garmin scales.
- `config.example.yaml` lists the default imports of every provider.

### Changed

- Data rules: device measurements are checked against the vendors of that kind of device, not against wearables.

## [1.0.0] - 2026-10-31

### Added

- First release.
- Activities from intervals.icu, Strava, Google Health, Hammerhead, and Garmin Connect.
- Body weight from Withings and Garmin Connect, nutrition from FDDB.
- Sleep, HRV, resting heart rate, and SpO2 from Garmin Connect and Google Health, with sleep and recovery scores that work the same for every device.
- Training load metrics (TRIMP, CTL, ATL, TSB, FTP estimates) in PostgreSQL.
- Grafana dashboards, a status page, an HTTP API, and an MCP server.
- Container image on `ghcr.io/mkuf/trainingpulse`.

[Unreleased]: https://github.com/mkuf/TrainingPulse/compare/v1.7.0...HEAD
[1.7.0]: https://github.com/mkuf/TrainingPulse/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/mkuf/TrainingPulse/compare/v1.5.1...v1.6.0
[1.5.1]: https://github.com/mkuf/TrainingPulse/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/mkuf/TrainingPulse/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/mkuf/TrainingPulse/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/mkuf/TrainingPulse/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/mkuf/TrainingPulse/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/mkuf/TrainingPulse/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/mkuf/TrainingPulse/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mkuf/TrainingPulse/releases/tag/v1.0.0
