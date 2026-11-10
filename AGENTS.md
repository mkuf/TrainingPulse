# TrainingPulse

Before adding or changing a provider, a mapper, a stored field, a sample metric, a derived score, or a data kind, read [docs/data-rules.md](docs/data-rules.md) and check the change against its checklist. In short:

- Store only standard concepts that most wearables report (Garmin, Fitbit/Google, Oura, Whoop, Polar, Apple). A provider without a field leaves it `None`.
- Never map a provider's own scores, statuses, or indexes.
- Mappers only translate; anything derivable from samples or streams is computed in the core, from visible rows only.

Before adding or changing an MCP tool or a field it returns, read "Explaining values to agents" in [docs/developers.md](docs/developers.md#explaining-values-to-agents). An agent sees only the server instructions, the tool descriptions, and the field names.

On the status page, text appears only when the user needs to act:

- A working account shows no hint, because its status already says so.
- Mention `config.yaml` only when something is not set up yet and it is what to do next. Other explanations of settings belong in the docs.

Architecture, provider APIs, and tests are in [docs/developers.md](docs/developers.md).

Add every change that users notice to the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md).
