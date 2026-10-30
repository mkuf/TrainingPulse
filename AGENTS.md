# TrainingPulse

Before adding or changing a provider, a mapper, a stored field, a sample metric, a derived score, or a data kind, read [docs/data-rules.md](docs/data-rules.md) and check the change against its checklist. In short:

- Store only standard concepts that most wearables report (Garmin, Fitbit/Google, Oura, Whoop, Polar, Apple). A provider without a field leaves it `None`.
- Never map a provider's own scores, statuses, or indexes.
- Mappers only translate; anything derivable from samples or streams is computed in the core, from visible rows only.

Architecture, provider APIs, and tests are in [docs/developers.md](docs/developers.md).
