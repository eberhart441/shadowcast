"""The image server the nodes pull from in maintenance mode, on the maintenance network.
"""
from __future__ import annotations

from ..room import board_serial
from .report import Report, ReportError, parse_report

MAX_REPORT_BYTES = 512


class ImageServer:
    """The HTTP server nodes pull from in maintenance mode, on the maintenance network.

        GET  /node.bin?serial=S   the signed image (``fetches`` lists the serials that asked)
        POST /report              one report line with the kit's tag; 200, or 400 when it does not verify

    It serves one public, signed image and takes tagged reports, nothing else; ``host`` is the maintenance
    network's address (default every interface, since the nodes come from the access point's subnet).
    """

    def __init__(self, image: bytes, kit_key: bytes, host: str = "0.0.0.0", port: int = 8070, log=None) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit

        self._image, self._key = bytes(image), bytes(kit_key)
        self._lock = threading.Lock()
        self._reports: list[Report] = []
        self.fetches: list[str] = []
        self.refused_reports = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (the stdlib's name)
                parts = urlsplit(self.path)
                if parts.path != "/node.bin":
                    return self._reply(404, b"not found\n")
                serial = board_serial(parse_qs(parts.query).get("serial", [""])[0]) or "?"
                with outer._lock:
                    outer.fetches.append(serial)
                if log:
                    log(f"{serial}: fetching the image")
                self._reply(200, outer._image, "application/octet-stream")

            def do_POST(self):  # noqa: N802
                if urlsplit(self.path).path != "/report":
                    return self._reply(404, b"not found\n")
                length = min(int(self.headers.get("Content-Length") or 0), MAX_REPORT_BYTES)
                text = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    report = parse_report(text, outer._key)
                except ReportError:
                    with outer._lock:
                        outer.refused_reports += 1
                    return self._reply(400, b"refused\n")
                with outer._lock:
                    outer._reports.append(report)
                self._reply(200, b"ok\n")

            def _reply(self, code, body, kind="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):   # quiet: the rollout logs what matters
                pass

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ota-image-server", daemon=True)

    def start(self) -> "ImageServer":
        self._thread.start()
        return self

    def take_reports(self) -> list[Report]:
        """The reports received since the last call."""
        with self._lock:
            reports, self._reports = self._reports, []
        return reports

    def close(self) -> None:
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
