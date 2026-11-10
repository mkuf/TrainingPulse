# Setup

This procedure installs TrainingPulse with Docker Compose.

## Before you start

Make sure that you have these items:

- Docker and Docker Compose.
- Your maximum heart rate, your resting heart rate, and your FTP. These values are necessary. Refer to [Athlete settings](athlete-settings.md).
- An account with one or more providers:

| Provider | Data kind | Setup |
|---|---|---|
| intervals.icu | activities | [intervals.icu](providers/intervals-icu.md) |
| Strava | activities | [Strava](providers/strava.md) |
| Google Health | activities | [Google Health](providers/google-health.md) |
| Hammerhead | activities | [Hammerhead](providers/hammerhead.md) |
| Withings | body (weight, fat %, muscle mass, bone mass, body water, visceral fat), blood pressure, sleep, wellness, intraday | [Withings](providers/withings.md) |
| FDDB | nutrition (kcal, macros) | [FDDB](providers/fddb.md) |

## 1. Write the configuration

1. Go to the root directory of the repository.
2. Copy the example files:

   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

3. Open `config.yaml`.
4. Set `app.base_url` to the URL that you use to open the app, for example `http://homeserver:8000`. The OAuth providers use this URL for their callbacks.
5. Set the values in `athlete`. Refer to [Athlete settings](athlete-settings.md).
6. Set up each provider that you use. Refer to the provider pages in the table above.
7. Open `.env`.
8. Change the Postgres password.

`.env` contains only the Compose values: the Postgres credentials and the published MCP port. All other settings are in `config.yaml`.

> **Note:** Git ignores `config.yaml`. You can write credentials directly in the file. Alternatively, keep them in the environment and write `${VAR}` in the file. The example file shows the two methods.

You can override each key with an environment variable `TP_<SECTION>__<KEY>`. For example, `TP_DATA__ACTIVITIES__REFRESH_DAYS=7` sets `data.activities.refresh_days`.

## 2. Start the stack

1. Go to the root directory of the repository.
2. Run:

   ```bash
   docker compose up -d
   ```

The stack has four containers:

| Container | Port |
|---|---|
| FastAPI app | `8000` |
| Grafana | `3000` |
| MCP service | `8001` |
| PostgreSQL | internal only |

If the configuration has a problem, the app does not start. The log shows all problems together: unknown keys, incorrect types, missing values, and enabled providers without credentials. To see the log, run:

```bash
docker compose logs app
```

## 3. Connect the providers

1. Open the status page at `http://your-host:8000/`.
2. For Strava, Google Health, Hammerhead, and Withings, click **Connect** on the card of the provider.

intervals.icu and FDDB do not use OAuth. They start to sync after you set their credentials.

The sync runs all providers each 15 minutes. To change the interval, set `sync.interval_minutes`.

## 4. Open Grafana

Click **Open dashboards** on the status page for the list of TrainingPulse dashboards. To go straight to one dashboard, click the arrow next to the button and select it. In Grafana, they are in the **TrainingPulse** folder, and the **Dashboards** menu on each dashboard opens the others.

TrainingPulse adds the Postgres data source **`trainingpulse`** and the dashboards automatically.

If Grafana is not on port `3000` of the app host, set `app.grafana_url`. Then the dashboard links on the status page go to the correct address.

### Today summary

The top of the status page shows the most important values. It is easy to read on a phone:

- **Sleep:** the sleep score of the last night, the time asleep, the sleep stages, and the sleeping heart rate.
- **Recovery:** the recovery score with its status (for example **Normal**), a bar that shows where the score is in the range from 0 to 100, HRV, and resting heart rate. HRV and resting heart rate show the difference to your usual values of the last 30 days.
- **Form:** form (TSB), fitness (CTL), and fatigue (ATL).
- **Nutrition:** the calories, protein, carbohydrates, and fat that you logged today. Click **Week** to see the average per day of this week (Monday to Sunday). The average does not include today, because you are possibly still logging it. On Monday, it shows last week. The browser keeps your selection.

If a value is older than today, the date shows next to the name. Values older than 3 days do not show. The arrow in each box opens the related Grafana dashboard.

### Use your own Grafana

To show the dashboards in a Grafana you already run, add a Postgres data source for the TrainingPulse database. If Grafana has more than one Postgres data source, select it in the **PostgreSQL** menu at the top of a dashboard. Then push the dashboards with a service account token:

```bash
GRAFANA_URL=https://grafana.example.com GRAFANA_TOKEN=<token> python3 grafana/sync_dashboards.py
```

The script puts the dashboards in a **TrainingPulse** folder and creates it if necessary. To use another folder name, set `GRAFANA_FOLDER`. Run the script again after each upgrade.

## 5. Connect an MCP client (optional)

The MCP service is read-only. Its address is:

```text
http://your-host:8001/mcp
```

Add this address to your MCP client.
