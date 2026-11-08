"""Guided accuracy run: spoken prompts lead one person through the marks and a walk while the dashboard records.

    python scripts/accuracy_run.py --url http://localhost:8000 --out runs/accuracy-1

Before starting: the dashboard runs with --raw-log (and ideally --log), every board is in place, and the nine
marks are taped (csi/accuracy.py MARKS: x from the left wall, y from the receivers' wall). The laptop must be
audible in the room. The protocol takes about 11 minutes; see csi/accuracy.py.

Writes to --out:
  labels.jsonl      every prompt, with the local time it was played (ground truth for the analysis)
  live.jsonl        /api/state at 5 Hz with local send and receive times (clock offset, live-vs-replay check)
  thresholds.json   the calibrated thresholds the run used
  run.json          settings and a summary
Windows only for the voice (System.Speech renders the prompts, winsound plays them); --silent skips the voice.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csi import accuracy as A  # noqa: E402

NUMBERS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
PHRASES = {
    "intro": "Accuracy test. Everyone please leave the room now. Calibration starts in twenty seconds.",
    "cal_start": "Calibrating. Keep the room empty.",
    "cal_retry": "One more calibration try. Keep the room empty.",
    "cal_done": "Calibration done.",
    "cal_partial": "Calibration incomplete for some links. Continuing.",
    "empty": "Measuring the empty room for one minute. Stay outside.",
    "first": "Come in now.",
    "hold": "Hold here. Keep moving on the spot.",
    "still": "Now stand completely still.",
    "move_again": "Move on the spot again.",
    "walk_intro": "Walking test. Stand on mark one. When you hear a number, walk to that mark, "
                  "arriving as the next number is called.",
    "walk_end": "Stop. Walking test done.",
    "leave": "Almost done. Please leave the room for thirty seconds.",
    "done": "Test complete. Thank you.",
    "check": "Audio check. If you can hear this inside the room, the test is ready.",
    **{f"goto{k}": f"Go to mark {w}." for k, w in NUMBERS.items()},
    **{f"num{k}": w for k, w in NUMBERS.items()},
}


class Voice:
    def __init__(self, folder: Path, silent: bool) -> None:
        self.folder, self.silent = folder, silent
        if silent:
            return
        folder.mkdir(parents=True, exist_ok=True)
        if all((folder / (key + ".wav")).exists() for key in PHRASES):
            return   # rendered by an earlier run
        script = ["Add-Type -AssemblyName System.Speech",
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer", "$s.Rate = 0"]
        for key, text in PHRASES.items():
            script.append(f"$s.SetOutputToWaveFile('{folder / (key + '.wav')}'); $s.Speak(\"{text}\")")
        script.append("$s.SetOutputToNull(); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(script)], check=True)

    def say(self, key: str) -> None:
        if not self.silent:
            import winsound
            winsound.PlaySound(str(self.folder / (key + ".wav")), winsound.SND_FILENAME | winsound.SND_ASYNC)


class Run:
    def __init__(self, url: str, out: Path, voice: Voice) -> None:
        self.url, self.out, self.voice = url.rstrip("/"), out, voice
        out.mkdir(parents=True, exist_ok=True)
        self.labels = open(out / "labels.jsonl", "a", encoding="utf-8")
        self.live = open(out / "live.jsonl", "a", encoding="utf-8")
        self.stop = threading.Event()

    # ---- dashboard API
    def get(self, path: str, tries: int = 4) -> dict:
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(self.url + path, timeout=5) as r:
                    return json.load(r)
            except (OSError, ValueError):   # connection reset, timeout, half a body: try again
                if attempt == tries - 1:
                    raise
                time.sleep(0.5)

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return {"http_error": e.code, "body": e.read().decode(errors="replace")[:500]}
        except (OSError, ValueError) as e:   # the reply was lost on the way back; the command may have run
            return {"lost_reply": str(e)}

    # ---- logging
    def label(self, event: str, **kw) -> None:
        self.labels.write(json.dumps({"t": time.time(), "event": event, **kw}) + "\n")
        self.labels.flush()
        print(f"{time.strftime('%H:%M:%S')}  {event} {kw if kw else ''}", flush=True)

    def prompt(self, key: str, event: str, **kw) -> None:
        self.voice.say(key)
        self.label(event, **kw)

    def poll(self) -> None:
        while not self.stop.is_set():
            t0 = time.time()
            try:
                d = self.get("/api/state")
                t1 = time.time()
                st = d["status"]
                self.live.write(json.dumps({
                    "t0": t0, "t1": t1, "time_s": st.get("time_s"), "track": d.get("track"),
                    "room": st.get("room"), "calibration": (st.get("calibration") or {}).get("state"),
                    "links": [[l["id"], l["state"], l["score"], l["pkt_rate"], l.get("evidence")] for l in d["links"]],
                    "heat_max": d["heatmap"]["max"]}) + "\n")
            except Exception as e:  # keep polling through a dashboard restart
                self.live.write(json.dumps({"t0": t0, "error": str(e)}) + "\n")
            self.live.flush()
            self.stop.wait(max(0.0, 0.2 - (time.time() - t0)))

    # ---- protocol steps
    def calibrate(self, attempts: int = 3) -> dict:
        expected = {l["id"] for l in self.get("/api/state")["links"]}
        for attempt in range(1, attempts + 1):
            cal = self.get("/api/state")["status"].get("calibration") or {}
            if cal.get("state") in ("countdown", "recording", "processing"):
                self.label("calibration_already_running", attempt=attempt, state=cal.get("state"))
                r = {}
            else:
                self.prompt("cal_start" if attempt == 1 else "cal_retry", "calibration_start", attempt=attempt)
                r = self.post("/api/calibrate",
                              {"clips": 3, "seconds": 10, "margin": 1.25, "lead_s": 3, "noisy": True})
            if "lost_reply" in r:
                self.label("calibration_reply_lost", detail=r["lost_reply"])
            if "http_error" in r:
                self.label("calibration_error", response=r)
                time.sleep(5)
                continue
            deadline = time.time() + 120
            cal = {}
            while time.time() < deadline:
                time.sleep(1)
                cal = self.get("/api/state")["status"].get("calibration") or {}
                if cal.get("state") not in ("countdown", "recording", "processing", "running", "lead"):
                    break
            have = set(self.get("/api/thresholds").get("thresholds", {}))
            self.label("calibration_end", attempt=attempt, state=cal.get("state"), message=cal.get("message"),
                       result=cal.get("result"), calibrated=sorted(have))
            if expected <= have:
                break
        thresholds = self.get("/api/thresholds").get("thresholds", {})
        saved = self.post("/api/thresholds/save", {})
        self.label("thresholds_saved", thresholds=thresholds, response=saved)
        (self.out / "thresholds.json").write_text(json.dumps(thresholds, indent=2))
        self.voice.say("cal_done" if expected <= set(thresholds) else "cal_partial")
        return thresholds

    def protocol(self, skip_intro: bool = False) -> None:
        threading.Thread(target=self.poll, daemon=True).start()
        if not skip_intro:
            self.prompt("intro", "intro")
            time.sleep(A.INTRO_S)
        self.calibrate()
        time.sleep(4)
        self.prompt("empty", "empty_start")
        time.sleep(A.EMPTY_S)
        self.label("empty_end")
        self.prompt("first", "come_in")
        time.sleep(3)
        for k in sorted(A.MARKS):
            self.prompt(f"goto{k}", "goto", mark=k)
            time.sleep(A.TRANSIT_S)
            self.prompt("hold", "hold", mark=k)
            time.sleep(A.HOLD_S)
            self.label("hold_end", mark=k)
        self.prompt("goto5", "goto", mark=5)
        time.sleep(A.STILL_TRANSIT_S)
        self.prompt("still", "still_start")
        time.sleep(A.STILL_S)
        self.prompt("move_again", "move_again")
        time.sleep(A.MOVE_AGAIN_S)
        self.label("still_end")
        self.prompt("walk_intro", "walk_intro")
        time.sleep(A.WALK_INTRO_S)
        for i, k in enumerate(A.WALK_ROUTE):
            self.prompt(f"num{k}", "walk_call", mark=k, index=i)
            time.sleep(A.WALK_STEP_S)
        self.prompt("walk_end", "walk_end")
        time.sleep(3)
        self.prompt("leave", "empty_start", final=True)
        time.sleep(A.LEAVE_S)
        self.label("empty_end", final=True)
        self.prompt("done", "end")
        time.sleep(3)
        self.stop.set()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--silent", action="store_true", help="no voice (for a dry run)")
    ap.add_argument("--audio-check", action="store_true", help="only play one test prompt, then exit")
    ap.add_argument("--skip-intro", action="store_true",
                    help="resume: the room is already empty; go straight to (or wait for) the calibration")
    a = ap.parse_args()
    voice = Voice(a.out / "voice", a.silent)
    if a.audio_check:
        import winsound
        winsound.PlaySound(str(a.out / "voice" / "check.wav"), winsound.SND_FILENAME)
        return
    run = Run(a.url, a.out, voice)
    (a.out / "run.json").write_text(json.dumps({"started": time.time(), "url": a.url, "marks": A.MARKS,
                                                "walk_route": A.WALK_ROUTE}, indent=2))
    run.protocol(skip_intro=a.skip_intro)
    print("run complete:", a.out)


if __name__ == "__main__":
    main()
