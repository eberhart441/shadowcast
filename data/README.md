# Recorded CSI sessions (single link)

Real recordings from the first two-board setup: one ESP32-C5 receiver
(`csi_recv`) and one ESP32-S3 sender (`csi_send`), channel 6, about 2 m apart,
sender at a nominal 100 Hz (about 90 packets per second actually received).
Each session folder has `session.json` (metadata) and one CSV per recording,
named `<label>_<UTC timestamp>.csv`. Load them with `csi.recording.load_session`.

CSV columns: `host_ts, device_ts, rssi, channel, amp_0 .. amp_113`
(114 usable subcarriers; the three guard subcarriers are removed).

## hw3 (receiver reset in place, gain not yet locked)

| file | label | notes |
|---|---|---|
| `empty_...155236Z.csv` | empty | 30 s, room clear |
| `walking_...155324Z.csv` | walking | about 25 s, walking across the line |
| `still_standing_...155955Z.csv` | still_standing | 30 s, standing mid-line |

`calib.json` is the motion threshold calibrated on the empty recording.
The receiver gain still drifted a little in this session.

## hw5 (best session: `CONFIG_FORCE_GAIN` enabled, gain locked)

| file | label | notes |
|---|---|---|
| `empty_...160706Z.csv` | empty | 30 s, clean; used for calibration |
| `walking_...160810Z.csv` | walking | 30 s of continuous walking |
| `empty_...160923Z.csv` | empty | validation; quiet until about 29 s, where a spike is probably someone returning to the laptop |

`calib.json` is the threshold calibrated on the first empty recording (13.15).
Evaluating with `python scripts/evaluate.py data/hw5`:
empty 95% EMPTY, walking 100% MOTION. The 5% empty errors come from the
spike near the end of the second empty recording.

## Not included

Earlier sessions were recorded before two bugs were fixed (a wrong
subcarrier mask and a gain baseline taken at the wrong placement) and one
empty recording was contaminated, so they are not useful.

## Caveats

- Single link only; the four-link setup (2 senders, 2 receivers) has not been recorded yet.
- Breathing detection is unreliable on real data (it false-fires on an empty
  room), so the still_standing recording is only useful for experiments.
