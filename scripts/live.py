#!/usr/bin/env python
"""Live CSI viewer: amplitude heatmap, motion-variance trace and threshold line.

    live.py --simulate                                  # demo, no hardware
    live.py --port /dev/ttyUSB0 --threshold-file calibration.json   # real port: threshold required
    live.py --simulate --save-frame frame.png --duration 6   # headless snapshot

Keys: up / + raise the threshold, down / - lower it.
Close the window or press Ctrl+C to exit; the serial port is released.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.common import add_source_args, add_threshold_args, resolve_threshold  # noqa: E402
from csi.detect import SIM_THRESHOLD  # noqa: E402
from csi.parser import Stream, serial_stream  # noqa: E402
from csi.simulate import simulate_stream  # noqa: E402
from csi.viewer import HISTORY_S, LiveView  # noqa: E402

REDRAW_MS = 100
SAVE_DEFAULT_DURATION_S = 6.0
KEY_STEPS = {"up": 1, "+": 1, "=": 1, "down": -1, "-": -1, "_": -1}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    add_source_args(ap)
    add_threshold_args(ap)
    o = ap.add_argument_group("output")
    o.add_argument("--duration", type=float, help="stop after this many seconds")
    o.add_argument("--save-frame", type=Path, metavar="PNG",
                   help="headless: render one frame to PNG after --duration (Agg backend)")
    return ap


def open_lines(args: argparse.Namespace) -> Stream:
    if args.simulate:
        return simulate_stream(rate_hz=args.rate, seed=args.seed, realtime=True, loop=True)
    if args.port:
        return serial_stream(args.port, args.baud)
    raise SystemExit("error: pass --port <serial port> or --simulate")


class StreamReader(threading.Thread):
    """Feeds a LiveView from a line stream on a background thread."""

    def __init__(self, stream: Stream, view: LiveView, lock: threading.Lock) -> None:
        super().__init__(daemon=True)
        self._stream, self._view, self._lock = stream, view, lock
        self._stop_event = threading.Event()
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            for host_ts, line in self._stream:
                if self._stop_event.is_set():
                    break
                with self._lock:
                    self._view.ingest(host_ts, line)
        except Exception as e:  # e.g. serial port unplugged; report at exit
            self.error = e
        finally:
            close = getattr(self._stream, "close", None)  # releases the serial port
            if close:
                close()

    def stop(self) -> None:
        self._stop_event.set()


@dataclass
class Artists:
    fig: Any
    im: Any
    line: Any
    thr: Any


def make_figure(threshold: float) -> Artists:
    import matplotlib.pyplot as plt

    fig, (ax_heat, ax_var) = plt.subplots(
        2, 1, figsize=(10, 7), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    im = ax_heat.imshow(np.zeros((1, 1)), aspect="auto", origin="lower", cmap="viridis",
                        extent=[-HISTORY_S, 0, 0, 1], interpolation="nearest")
    ax_heat.set_ylabel("subcarrier")
    (line,) = ax_var.plot([], [], lw=1.2, label="motion score (1 s variance)")
    thr = ax_var.axhline(threshold, color="red", ls="--", label="threshold")
    ax_var.set_xlim(-HISTORY_S, 0)
    ax_var.set_ylim(0, 10)
    ax_var.set_xlabel("seconds relative to latest packet")
    ax_var.set_ylabel("motion score")
    ax_var.legend(loc="upper left")
    return Artists(fig, im, line, thr)


def refresh(view: LiveView, lock: threading.Lock, art: Artists) -> None:
    with lock:
        heat = view.heatmap()
        heat_t = view.heatmap_times()
        times, scores = view.variance_trace()
        threshold = view.threshold
        title = view.title()
    if heat.size:
        lo, hi = np.percentile(heat, [1, 99])
        art.im.set_data(heat)
        art.im.set_extent([heat_t[0], heat_t[-1], 0, heat.shape[0]])
        art.im.set_clim(lo, hi if hi > lo else lo + 1)
    art.line.set_data(times, scores)
    art.thr.set_ydata([threshold, threshold])
    top = max(float(scores.max()) if scores.size else 0.0, threshold) * 1.2
    art.fig.axes[1].set_ylim(0, max(top, 1.0))
    art.fig.suptitle(title)


def on_key(event: Any, view: LiveView, lock: threading.Lock) -> None:
    steps = KEY_STEPS.get(event.key)
    if steps:
        with lock:
            view.adjust_threshold(steps)


def run(args: argparse.Namespace, threshold: float) -> int:
    import matplotlib

    if args.save_frame:
        matplotlib.use("Agg")  # headless: no window
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    view = LiveView(threshold=threshold, send_rate_hz=args.rate)
    lock = threading.Lock()
    reader = StreamReader(open_lines(args), view, lock)
    reader.start()
    art = make_figure(threshold)
    art.fig.canvas.mpl_connect("key_press_event", lambda e: on_key(e, view, lock))
    print(f"threshold {threshold:.3f}; close the window or Ctrl+C to stop", flush=True)

    anim = None
    try:
        if args.save_frame:
            time.sleep(args.duration or SAVE_DEFAULT_DURATION_S)
            refresh(view, lock, art)
            art.fig.savefig(args.save_frame, dpi=110)
            print(f"saved {args.save_frame}")
        else:
            if args.duration:
                timer = art.fig.canvas.new_timer(interval=args.duration * 1000)
                timer.add_callback(plt.close, art.fig)
                timer.start()
            anim = FuncAnimation(art.fig, lambda _f: refresh(view, lock, art),
                                 interval=REDRAW_MS, cache_frame_data=False)
            plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        reader.join(timeout=3)
        plt.close(art.fig)
        del anim

    s = view.parser.stats
    print(f"packets {s.parsed}, drops {view.health.drops}, malformed {s.malformed}, "
          f"layout-skipped {s.layout_skipped}")
    if reader.error is not None:
        print(f"stream stopped: {reader.error}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if not (args.simulate or args.port):
        raise SystemExit("error: pass --port <serial port> or --simulate")
    threshold = resolve_threshold(args, SIM_THRESHOLD)  # raises before any port is opened
    return run(args, threshold)


if __name__ == "__main__":
    sys.exit(main())
