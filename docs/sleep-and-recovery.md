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

The recovery score is a number from 0 to 100 for each day. It shows how the night's HRV and the day's resting heart rate compare with your usual values. **70 means a usual day for you.**

| Score | Status | Meaning |
|---|---|---|
| 85–100 | Above normal | Clearly better than your usual night |
| 60–84 | Normal | Inside your usual range |
| 40–59 | Below normal | Outside your usual range |
| 0–39 | Well below normal | Rare for you, for example when you are ill or very tired |

The score has two parts:

| Part | Weight | Better is |
|---|---|---|
| HRV of the night | 60 % | higher |
| Resting heart rate | 40 % | lower |

For each part, TrainingPulse uses the previous 30 days:

- Your **usual value** is the median of these days.
- Your **usual variation** is the standard deviation of these days. It is at least 5 % of the usual value for HRV and at least 1 bpm for the resting heart rate.

A value equal to your usual value gives 70 points. Each usual variation in the better direction adds 15 points. Each usual variation in the worse direction subtracts 15 points. Thus, the same change counts less for a person whose values change much from day to day.

An example: your usual HRV is 80 ms and it usually varies by 8 ms. A night with 72 ms gives 55 points for the HRV part. A night with 88 ms gives 85 points.

The recovery score does not include the sleep score. The sleep score has a fixed scale, and the dashboards show it beside the recovery score.

The recovery score does not know how much you trained, so it is not a training readiness. To decide how hard to train, use it together with your form (TSB).

## Missing data

If a part has no data, TrainingPulse does not use it. It divides the other weights so that their sum is 100 %. Some examples:

- Regularity needs at least 3 nights in the last 7 days.
- The HRV part of the sleep score needs at least 7 of the last 30 days.
- The recovery score needs at least 14 of the last 30 days. A new device has no recovery score in its first two weeks.
- The heart-rate dip needs heart-rate samples (the `intraday` data kind) for about 2 hours before you sleep.
