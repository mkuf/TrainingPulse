<p align="center">
  <img src="app/static/assets/favicon.svg" width="200" alt="TrainingPulse logo">
</p>

# TrainingPulse

Self-hosted sync and dashboards for your training: activities from **intervals.icu**, **Strava**, **Google Health**, and **Hammerhead** (Karoo bike computers), body weight from **Withings**, and nutrition from **FDDB**. PostgreSQL storage, training-load metrics (**TRIMP**, CTL, ATL, TSB, FTP estimates), and Grafana visualization.

## Trademarks and API use

**TrainingPulse is not affiliated with, endorsed by, or sponsored by intervals.icu, Strava, Google, Hammerhead/SRAM, Withings, or FDDB.** Each person who runs this stack must use their own API credentials and comply with the respective terms of service. Activity data shown from Strava is **Powered by Strava**.

## Disclaimer

This project is largely AI-generated. The numbers it computes — including training load, fitness, fatigue, form, and FTP estimates — are informational only and **are not medical advice**. Verify anything you plan to act on, and consult a qualified professional for health or training decisions.

## Grafana dashboards

Example views from the bundled dashboards.

<table>
<tr>
<td align="center" valign="top" width="25%">
<a href="screenshots/activity_detail.png"><img src="screenshots/activity_detail.png" width="200" alt="Activity detail dashboard"></a><br />
<sub>Activity detail</sub>
</td>
<td align="center" valign="top" width="25%">
<a href="screenshots/fitness.png"><img src="screenshots/fitness.png" width="200" alt="Fitness and Freshness dashboard"></a><br />
<sub>Fitness &amp; Freshness</sub>
</td>
<td align="center" valign="top" width="25%">
<a href="screenshots/account_overview.png"><img src="screenshots/account_overview.png" width="200" alt="Account overview dashboard"></a><br />
<sub>Account overview</sub>
</td>
<td align="center" valign="top" width="25%">
<a href="screenshots/nutrition_training_weight.png"><img src="screenshots/nutrition_training_weight.png" width="200" alt="Nutrition, training and weight dashboard"></a><br />
<sub>Nutrition &amp; weight</sub>
</td>
</tr>
</table>

## Documentation

Start with [Setup](docs/setup.md). Then set up each provider that you use:

| Provider | Data kind | Setup |
|---|---|---|
| intervals.icu | activities | [intervals.icu](docs/providers/intervals-icu.md) |
| Strava | activities | [Strava](docs/providers/strava.md) |
| Google Health | activities | [Google Health](docs/providers/google-health.md) |
| Hammerhead | activities | [Hammerhead](docs/providers/hammerhead.md) |
| Withings | body (weight, fat %, muscle mass) | [Withings](docs/providers/withings.md) |
| FDDB | nutrition (kcal, macros) | [FDDB](docs/providers/fddb.md) |

More manuals:

- [Athlete settings](docs/athlete-settings.md): heart rate, FTP, time zone, and calories.
- [Data and duplicates](docs/data.md): data kinds, provider order, and stored data.
- [Upgrading](docs/upgrading.md): change from an old release.
- [Developer notes](docs/developers.md): architecture, HTTP API, configuration schema, and metric formulas.
