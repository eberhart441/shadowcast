#!/usr/bin/env python
"""Write the node-core C tests' wire-format vectors from the golden JSON fixtures.

    gen_wire_vectors.py            # rewrite firmware/core/tests/vectors/{round_broadcast,round_auth,gateway_record}.txt

The golden bytes live in one place, the JSON fixtures under tests/fixtures/ (hand-written from the byte layout).
C has no JSON reader, so this script flattens them into one line per case for firmware/core/tests, and
tests/test_wire_vectors.py fails when a vector file no longer matches its fixture. Rerun this after changing a
fixture, then run firmware/core/test.sh.

round_broadcast.txt (fixture tests/fixtures/round_broadcast_v2.json, format docs/round-broadcast.md):

    valid <name> <hex> <version> <frame_type> <round> <slot> <slots> <schedule> <index> <membership>
          <node_id hex> <tag hex> <n_reports> then per report: <transmitter_index> <score> <rssi> <heard> <since> <flags>
    invalid <name> <hex> <reason>

The rssi of a report is the signed byte; -128 is "none" (null in the fixture). <flags> is the whole flags byte:
the fixture's flags (bits 0-2) plus its lateness code (default 0) in bits 3-7.

round_auth.txt (the same fixture: its key, valid, forged and freshness cases; docs/round-broadcast.md "Authentication"):

    key <kit key hex> <key check hex>
    signed <name> <hex>                     verifies under the key; signing it with its tag zeroed gives it back
    forged <name> <hex>                     decodes, but its tag does not verify
    fresh|replay <t_us> <node_id hex> <round>   one step of the freshness sequence, in order

gateway_record.txt (fixture tests/fixtures/gateway_record_v1.json, format docs/gateway-record.md):

    heard <name> <hex> <rx_time_us> <rssi> <payload hex or ->
    status <name> <hex> <serial hex> <uptime_us> <reset_reason> <role> <channel> <then the counters, in order>
    text <name> <hex> <utf-8 text as hex>
    bad <name> <hex> <reason>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
VECTORS = ROOT / "firmware" / "core" / "tests" / "vectors"
RSSI_NONE = -128


def round_broadcast_lines(fixture: dict) -> list[str]:
    lines = ["# Round broadcast v2 vectors, written by scripts/gen_wire_vectors.py from",
             "# tests/fixtures/round_broadcast_v2.json. Do not edit by hand; the format is in the script's docstring."]
    for case in fixture["valid"]:
        b = case["broadcast"]
        fields = ["valid", case["name"], case["hex"], b["version"], b["frame_type"], b["round"], b["slot"],
                  b["slots_per_round"], b["schedule_version"], b["node_index"], b["membership"],
                  b["node_id"].replace(":", "").lower(), b["tag"], len(b["reports"])]
        for r in b["reports"]:
            fields += [r["transmitter_index"], r["score"], RSSI_NONE if r["rssi"] is None else r["rssi"],
                       r["heard"], r["since"], r["flags"] | r.get("lateness", 0) << 3]
        lines.append(" ".join(str(f) for f in fields))
    for case in fixture["invalid"]:
        lines.append(f"invalid {case['name']} {case['hex']} {case['reason']}")
    return lines


def round_auth_lines(fixture: dict) -> list[str]:
    lines = ["# Round broadcast authentication vectors, written by scripts/gen_wire_vectors.py from",
             "# tests/fixtures/round_broadcast_v2.json. Do not edit by hand; the format is in the script's docstring.",
             f"key {fixture['key']} {fixture['key_check']}"]
    lines += [f"signed {case['name']} {case['hex']}" for case in fixture["valid"]]
    lines += [f"forged {case['name']} {case['hex']}" for case in fixture["forged"]]
    lines += [f"{step['expect']} {step['t_us']} {step['node_id'].replace(':', '').lower()} {step['round']}"
              for step in fixture["freshness"]]
    return lines


def gateway_record_lines(fixture: dict) -> list[str]:
    lines = ["# Gateway record v1 vectors, written by scripts/gen_wire_vectors.py from",
             "# tests/fixtures/gateway_record_v1.json. Do not edit by hand; the format is in the script's docstring."]
    for case in fixture["records"]:
        r = case["record"]
        kind = r["type"]
        if kind == "heard":
            fields = ["heard", case["name"], case["hex"], r["rx_time_us"], r["rssi"], r["payload"] or "-"]
        elif kind == "status":
            fields = ["status", case["name"], case["hex"], r["serial"].replace(":", "").lower(), r["uptime_us"],
                      r["reset_reason"], r["role"], r["channel"], *r["counters"]]
        elif kind == "text":
            fields = ["text", case["name"], case["hex"], r["text"].encode().hex()]
        else:
            raise ValueError(f"unknown record type {kind!r} in case {case['name']}")
        lines.append(" ".join(str(f) for f in fields))
    for case in fixture["bad"]:
        lines.append(f"bad {case['name']} {case['hex']} {case['reason']}")
    return lines


TARGETS = {
    "round_broadcast.txt": ("round_broadcast_v2.json", round_broadcast_lines),
    "round_auth.txt": ("round_broadcast_v2.json", round_auth_lines),
    "gateway_record.txt": ("gateway_record_v1.json", gateway_record_lines),
}


def render(name: str) -> str:
    fixture_name, build = TARGETS[name]
    fixture = json.loads((FIXTURES / fixture_name).read_text())
    return "\n".join(build(fixture)) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=VECTORS)
    args = ap.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name in TARGETS:
        if (FIXTURES / TARGETS[name][0]).exists():
            (args.out_dir / name).write_text(render(name), newline="\n")
            print(f"wrote {args.out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
