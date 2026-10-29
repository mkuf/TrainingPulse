# Sleep and recovery scores

TrainingPulse calculates two scores itself: a **sleep score** for each night and a **recovery score** for each day. It does not import the scores of a provider (for example the Garmin sleep score or Body Battery). These scores are different for each provider, and most providers do not give them in their API.

The scores use only the [common data](data.md#providers-and-data-kinds) that most wearables give. Thus, a score has the same meaning for all devices. If you change your device, your history stays comparable.

TrainingPulse calculates the scores again after each sync. The Grafana dashboard **Sleep and recovery** and the MCP tools show each score with its parts.

The scores are informational only. They are not medical advice.

## Sleep score

The sleep score is a number from 0 to 100. It has five parts. Each part is a number from 0 to 100.

| Part | Weight | 100 points | 0 points |
|---|---|---|---|
| Duration: time asleep | 35 % | `athlete.sleep_need_hours` (default 8 h) or more | half of it or less |
| Efficiency: time asleep ÷ time in bed | 20 % | 95 % or more | 75 % or less |
| Restfulness: time awake in the night | 15 % | 10 minutes or less | 60 minutes or more |
| Regularity: how much your bedtime changed in the last 7 nights (standard deviation) | 10 % | 15 minutes or less | 90 minutes or more |
| Physiology: average of two values | 20 % | see below | see below |

Physiology uses:

- **Heart-rate dip:** how much lower your heart rate is while you sleep than in the 16 hours before. 15 % or more gives 100 points, 0 % gives 0 points.
- **HRV:** the HRV of the night compared to the median of the last 30 days. The median or more gives 100 points, 80 % of it gives 0 points.

Between the limits, the points increase linearly.

The score does not use the time in deep or REM sleep. Devices detect sleep stages differently. A score that uses the stages would change when you change your device.

## Recovery score

The recovery score is a number from 0 to 100 for each day. 50 means a normal day for you.

| Part | Weight | 100 points | 50 points | 0 points |
|---|---|---|---|---|
| HRV of the night ÷ median of the last 30 days | 40 % | 120 % | 100 % | 80 % |
| Resting heart rate − median of the last 30 days | 30 % | 5 bpm lower | equal | 5 bpm higher |
| Sleep score of the night | 30 % | 100 | 50 | 0 |

## Missing data

If a part has no data, TrainingPulse does not use it. It divides the other weights so that their sum is 100 %. Some examples:

- Regularity needs at least 3 nights in the last 7 days.
- The comparisons with the last 30 days need at least 7 days of data. A new device has no recovery score in its first week.
- The heart-rate dip needs heart-rate samples (the `intraday` data kind) for about 2 hours before you sleep.
