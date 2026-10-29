# Athlete settings

The `athlete` block in `config.yaml` contains your personal values.

| Key | Necessary | Function |
|---|---|---|
| `max_hr` | yes | Maximum heart rate, for heart-rate zones and calories |
| `rest_hr` | yes | Resting heart rate, for calories. An imported value replaces it |
| `ftp` | yes | Functional threshold power, for power zones and calories |
| `weight_kg` | no | Body weight, if no provider gives weigh-ins |
| `sleep_need_hours` | no (default `8`) | Hours of sleep that give full points for duration in the [sleep score](sleep-and-recovery.md) |
| `timezone` | no (default `UTC`) | Time zone, for example `Europe/Berlin`. TrainingPulse uses it to put each record on the correct day |

```yaml
athlete:
  max_hr: 190
  rest_hr: 60
  ftp: 250
  weight_kg: 75
  timezone: Europe/Berlin
```

> **Caution:** `max_hr`, `rest_hr`, and `ftp` have no default values. TrainingPulse does not read a profile from a provider. `max_hr` and `ftp` are the only source of your zones. Thus, they control TRIMP, CTL, ATL, and TSB. Incorrect values give incorrect numbers that look correct. Make sure that the values are correct.

TrainingPulse calculates five heart-rate zones from `max_hr`. It uses the standard percentage model (50–60 %, 60–70 %, and so on).

## Resting heart rate

TrainingPulse uses the resting heart rate to estimate the calories of an activity from its heart rate. This estimate applies only to activities without power that are not a run or a walk.

If a provider gives daily wellness data (for example Garmin Connect), TrainingPulse uses the median of the imported resting heart rates of the last 7 days before each activity. One bad night does not change the value much. It does not matter which provider gives the values. `rest_hr` applies only if there are no imported values in these 7 days.

The status page shows the value that TrainingPulse uses now, and where it comes from. Activities that TrainingPulse calculated before the import keep their values. To calculate them again, do a full resync.

## Change a value

1. Change the value in `config.yaml`.
2. Restart the stack:

   ```bash
   docker compose up -d
   ```

The new value applies only to activities that TrainingPulse syncs after the change. Older activities keep their values.

## Correct a value that was incorrect from the start

Do this procedure only if the old value was incorrect. Do not do it if the old value was correct at that time. For example, do not do it after your FTP increases.

1. Change the value in `config.yaml`.
2. Restart the stack.
3. Open the status page.
4. Go to the athlete settings.
5. Click **Full resync**.

TrainingPulse downloads all activities again. It calculates all history again with the new value.

## Calories

TrainingPulse calculates calories itself. It does not use the device value first, because devices do not agree. At the same heart rate, a Fitbit showed approximately 1.9 times the value of a Galaxy Watch. The heart-rate formula of Strava gave approximately 1.7 times the value from measured power.

TrainingPulse uses the first rule that applies:

1. **Measured power:** kcal = work (kJ) / (4.184 × 0.22). This is the rule of intervals.icu, with 22% gross efficiency. The work is the sum of the power stream. If there is no stream, the work is average power × moving time. The Strava factor (× 1.115) gives a value that is 2.6% higher.
2. **Walk, hike, or run with a distance:** the ACSM metabolic equations. These use distance, elevation gain, moving time, and body weight.
3. **Heart rate, all other sports:** TrainingPulse changes heart rate into power with `ftp`. Power ≈ `ftp` × heart-rate reserve / 0.88. Then rule 1 applies. An incorrect `ftp` gives incorrect calories here.
4. **The imported value**, if the provider gives one.
5. **Last rule:** a MET value from the Compendium of Physical Activities × body weight × time.

On the activity page, a `~` shows a value that does not come from measured power. If the imported value is different, the page shows it adjacent to the calculated value.

The body weight comes from the weigh-in that is nearest to the activity date. If you do not import body data, set `weight_kg`.

TrainingPulse calculates calories one time, when it processes the activity. It uses the athlete values of that time. If you change `ftp` or `weight_kg`, older activities do not change. To calculate all history again, click **Full resync**.
