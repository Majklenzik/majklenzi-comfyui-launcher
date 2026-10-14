"""Stand-in for ComfyUI in tests: answers /system_stats on --port, prints a line, exits on SIGTERM."""
import argparse
import http.server
import signal
import sys
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--exit", type=int, default=None, help="exit with this code after 0.3 s")
ap.add_argument("--delay", type=float, default=0.0, help="seconds before the server starts answering")
args, _ = ap.parse_known_args()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"system": {}}'
        self.send_response(200 if self.path == "/system_stats" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


print("fake comfy booting", flush=True)
if args.exit is not None:
    time.sleep(0.3)
    print("fake comfy dying", flush=True)
    sys.exit(args.exit)
time.sleep(args.delay)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), H)
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
print("fake comfy ready", flush=True)
srv.serve_forever()
