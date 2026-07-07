"""Image-encoder microservice — the "encoder outside the JDK" the gallery needed.

A tiny, framework-free HTTP service (the same shape as the race sim): raw image bytes in, a
re-encoded image out. WebP is the reason it exists — ImageIO in the JVM cannot write it, Pillow
can — but the endpoint is format-generic, so AVIF or anything else Pillow grows is one line here.

    POST /encode?format=webp[&quality=82]   body: raw image bytes  -> 200 encoded bytes
    GET  /health                                                   -> 200 {"status": "UP"}

Stateless and deterministic per input. memes calls this; if it is down, memes serves the PNG it
already has, so an outage degrades quality, never availability.
"""

import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

SERVICE = "image-encoder"

def log(level, message):
    """The stack's shared log line (observability/README.md in the aggregator repo): ISO
    time, level, cid/trace placeholders (this stdlib stack sets neither), service, message."""
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    print(f"{stamp} {level:<5} [cid=-] [trace=-] {SERVICE} - {message}", flush=True)


SUPPORTED = {"webp", "png", "jpeg"}
DEFAULT_QUALITY = 82


def encode(data: bytes, fmt: str, quality: int) -> bytes:
    """Re-encode an image to the target format. Raises ValueError on an unreadable image or an
    unsupported format — the boundary turns that into a 400."""
    if fmt not in SUPPORTED:
        raise ValueError(f"unsupported format: {fmt}")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as unreadable:
        raise ValueError(f"unreadable image: {unreadable}")
    out = io.BytesIO()
    params = {"quality": quality} if fmt in ("webp", "jpeg") else {}
    if fmt == "jpeg" and image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    image.save(out, format=fmt.upper(), **params)
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self._json(200, {"status": "UP"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/encode":
            self._json(404, {"error": "not found"})
            return
        query = parse_qs(parsed.query)
        fmt = query.get("format", ["webp"])[0].lower()
        try:
            quality = int(query.get("quality", [DEFAULT_QUALITY])[0])
        except ValueError:
            quality = DEFAULT_QUALITY
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        try:
            encoded = encode(data, fmt, quality)
        except ValueError as bad:
            self._json(400, {"error": str(bad)})
            return
        self.send_response(200)
        self.send_header("Content-Type", f"image/{fmt}")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        log("INFO", f"{self.command} {self.path}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8087"))
    log("INFO", f"image-encoder listening on {port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
