"""Build csi_transceiver once per chip and flash every board in parallel, each with its own node ID.

    python firmware/flash_all.py COM6 COM7 COM8:s3 COM9 COM10:s3
    python firmware/flash_all.py --no-build COM6 COM7 ...      # reflash without rebuilding
    python firmware/flash_all.py --no-build --num 5 --id 3 COM11   # one board at a time: node 3 of 5

Port order = node ID (0, 1, 2, ...); the node count is the number of ports. Chip defaults to c5, add
":s3" for an S3. The ID and count go into the NVS partition, so no per-board build is needed.
Run from an ESP-IDF shell (IDF_PATH set, IDF's python on PATH).
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PROJECT = Path(__file__).resolve().parent / "csi_transceiver"
IDF = os.environ.get("IDF_PATH")
PY = sys.executable


def run(cmd, cwd=None, tag=""):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        print(f"{tag} FAILED: {' '.join(map(str, cmd))}\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
    return p.returncode == 0


def nvs_partition(build: Path):
    """(offset, size) of the 'nvs' partition from the built partition table."""
    raw = (build / "partition_table" / "partition-table.bin").read_bytes()
    for i in range(0, len(raw), 32):
        magic, _type, _sub, off, size, label = struct.unpack("<HBBII16s", raw[i:i + 28])
        if magic == 0x50AA and label.rstrip(b"\0") == b"nvs":
            return off, size
    sys.exit(f"no nvs partition in {build}")


def flash(port, chip, node_id, num, baud, results):
    build = PROJECT / f"build_{chip}"
    off, size = nvs_partition(build)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "node.csv").write_text(f"key,type,encoding,value\nnode,namespace,,\nid,data,u8,{node_id}\nnum,data,u8,{num}\n")
    ok = run([PY, "-m", "esp_idf_nvs_partition_gen", "generate", tmp / "node.csv", tmp / "node.bin", hex(size)],
             tag=port)
    ok = ok and run([PY, "-m", "esptool", "--chip", f"esp32{chip}", "-p", port, "-b", str(baud), "write-flash",
                     "@flash_args", hex(off), tmp / "node.bin"], cwd=build, tag=port)
    results[port] = ok
    print(f"{port}: node {node_id} ({chip}) {'OK' if ok else 'FAILED'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ports", nargs="+", help="PORT or PORT:s3, in node-ID order")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--num", type=int, help="node count, if not every board is flashed now (default: number of ports)")
    ap.add_argument("--id", type=int, default=0, help="ID of the first port (to flash boards one at a time)")
    a = ap.parse_args()
    if not IDF:
        sys.exit("IDF_PATH not set: run this from an ESP-IDF shell")

    boards = [(p.split(":")[0], (p.split(":") + ["c5"])[1].lower()) for p in a.ports]
    if not a.no_build:
        for chip in sorted({c for _, c in boards}):
            build = PROJECT / f"build_{chip}"
            idf = [PY, Path(IDF) / "tools" / "idf.py", "-C", PROJECT, "-B", build, f"-DSDKCONFIG={build / 'sdkconfig'}"]
            print(f"building for esp32{chip} ...")
            if not build.exists() and not run(idf + ["set-target", f"esp32{chip}"], tag=chip):
                sys.exit(1)
            if not run(idf + ["build"], tag=chip):
                sys.exit(1)

    results = {}
    threads = [threading.Thread(target=flash, args=(port, chip, a.id + i, a.num or len(boards), a.baud, results))
               for i, (port, chip) in enumerate(boards)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    failed = [p for p, ok in results.items() if not ok]
    print(f"\n{len(boards) - len(failed)}/{len(boards)} flashed" + (f", failed: {' '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
