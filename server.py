"""Image-encoder microservice — the "encoder outside the JDK" the gallery needed.

A tiny, framework-free HTTP service (the same shape as the race sim): raw image bytes in, a
re-encoded image out. WebP is the reason it exists — ImageIO in the JVM cannot write it, Pillow
can — but the endpoint is format-generic, so AVIF or anything else Pillow grows is one line here.

    POST /encode?format=webp[&quality=82]   body: raw image bytes  -> 200 encoded bytes
    GET  /health                                                   -> 200 {"status": "UP"}

Stateless and deterministic per input. memes calls this; if it is down, memes serves the PNG it
already has, so an outage degrades quality, never availability.

Guard-rails (all env-tunable, still no framework):

    MAX_UPLOAD_BYTES         declared body cap, default 12 MiB (memes caps uploads at 10 MB,
                             this leaves headroom)          -> 413; missing Content-Length -> 411
    MAX_IMAGE_PIXELS         decoded-pixel cap, default 25_000_000; checked before load()
                             (Pillow only *errors* at 2x its own limit) -> 400
    SOCKET_TIMEOUT_SECONDS   per-connection socket timeout, default 30 — a slowloris client
                             gets hung up on instead of pinning a thread

Only PNG, JPEG and WEBP are *decoded* (Image.open formats whitelist); anything else is a 400.

Behaviour change (2026-07): `quality` is validated, not corrected. Anything that is not an
integer in 0..100 is now a 400 — a non-integer used to be silently replaced by the default,
and out-of-range values were passed through to Pillow (accepted for JPEG, accidental 400 for
WebP). One rule now, stated here.
"""

import io
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

SERVICE = "image-encoder"

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "25000000"))
SOCKET_TIMEOUT_SECONDS = float(os.environ.get("SOCKET_TIMEOUT_SECONDS", "30"))

# Pillow's own decompression-bomb tripwire, aligned with ours. It only raises at 2x this
# value (below that it merely warns), hence the explicit size check in encode().
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def log(level, message):
    """The stack's shared log line (observability/README.md in the aggregator repo): ISO
    time, level, cid/trace placeholders (this stdlib stack sets neither), service, message."""
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    print(f"{stamp} {level:<5} [cid=-] [trace=-] {SERVICE} - {message}", flush=True)


SUPPORTED = {"webp", "png", "jpeg"}
DECODERS = ["PNG", "JPEG", "WEBP"]   # Image.open whitelist — everything else is "unreadable"
DEFAULT_QUALITY = 82


def encode(data: bytes, fmt: str, quality: int) -> bytes:
    """Re-encode an image to the target format. Raises ValueError on an unreadable or
    oversized image, an unsupported format, an out-of-range quality, or an image the target
    encoder cannot write — the boundary turns every one of those into a 400."""
    if fmt not in SUPPORTED:
        raise ValueError(f"unsupported format: {fmt}")
    if not 0 <= quality <= 100:
        raise ValueError(f"quality must be in 0..100, got {quality}")
    try:
        image = Image.open(io.BytesIO(data), formats=DECODERS)
    except Exception as unreadable:
        raise ValueError(f"unreadable image: {unreadable}")
    width, height = image.size
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"image too large: {width}x{height} exceeds {MAX_IMAGE_PIXELS} pixels")
    try:
        image.load()
    except Exception as unreadable:
        raise ValueError(f"unreadable image: {unreadable}")
    if fmt == "jpeg" and image.mode != "RGB":
        # JPEG has no alpha and no 16-bit/paletted modes; LA, I, I;16, P et al. used to make
        # save() raise OSError mid-response and tear the connection. Flatten everything.
        image = image.convert("RGB")
    out = io.BytesIO()
    params = {"quality": quality} if fmt in ("webp", "jpeg") else {}
    try:
        image.save(out, format=fmt.upper(), **params)
    except (OSError, ValueError) as unwritable:
        raise ValueError(f"cannot encode as {fmt}: {unwritable}")
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    """Deliberately speaks HTTP/1.0 (BaseHTTPRequestHandler's default protocol_version).

    Under 1.0 every response closes the connection, so an early refusal that never reads
    the request body cannot poison a keep-alive stream — the unread bytes die with the
    socket. Each early-refusal path below *also* sets close_connection = True explicitly,
    so the invariant is stated where it matters and survives a protocol bump. If this is
    ever switched to HTTP/1.1 (protocol_version = "HTTP/1.1"), that explicit close is
    what keeps refused-before-read requests safe; the alternative would be draining
    Content-Length bytes before answering, which invites exactly the slow/oversized-body
    abuse the guard-rails exist to refuse."""

    timeout = SOCKET_TIMEOUT_SECONDS   # settimeout() on each connection (StreamRequestHandler)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self._json(200, {"status": "UP"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/encode":
            # early refusal, body never read — close, don't leave unread bytes on the wire
            self.close_connection = True
            self._json(404, {"error": "not found"})
            return
        query = parse_qs(parsed.query)
        fmt = query.get("format", ["webp"])[0].lower()
        raw_quality = query.get("quality", [str(DEFAULT_QUALITY)])[0]
        try:
            quality = int(raw_quality)
        except ValueError:
            self.close_connection = True
            self._json(400, {"error": f"quality must be an integer in 0..100, got: {raw_quality}"})
            return
        declared = self.headers.get("Content-Length")
        if declared is None:
            self.close_connection = True
            self._json(411, {"error": "Content-Length required"})
            return
        try:
            length = int(declared)
        except ValueError:
            self.close_connection = True
            self._json(400, {"error": f"malformed Content-Length: {declared}"})
            return
        if length < 0:
            self.close_connection = True
            self._json(400, {"error": f"malformed Content-Length: {declared}"})
            return
        if length > MAX_UPLOAD_BYTES:
            # refuse on the declared size, before reading a single body byte
            self.close_connection = True
            self._json(413, {"error": f"body too large: {length} > {MAX_UPLOAD_BYTES} bytes"})
            return
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
