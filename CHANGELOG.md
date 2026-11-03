# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Garmin: strength, yoga and other gym sessions recorded with a GPS profile no longer get a moving time of a few minutes of GPS drift, which also made their computed calories far too low.
- Gym sessions no longer show a map, distance, speed, or elevation; for a stationary sport these are only indoor GPS drift and are not stored.

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

[Unreleased]: https://github.com/mkuf/TrainingPulse/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/mkuf/TrainingPulse/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mkuf/TrainingPulse/releases/tag/v1.0.0
