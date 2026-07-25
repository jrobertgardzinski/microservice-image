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

A PRESENT-BUT-EMPTY parameter is a mistake, not an omission: `?format=` and `?quality=` are
both 400s, never the default. The query is parsed with `keep_blank_values=True` precisely so
those reach the checks — `parse_qs` drops blank values by default, which used to make
`?format=&quality=80` fall through to webp, i.e. silently encode to a format the caller never
asked for. Omitting a parameter entirely still means "use the default"; typing `=` and nothing
after it means the caller computed a value and got an empty string, and guessing on their
behalf is exactly the correction this service stopped doing.

Both values are `.strip()`ed before that check, so "empty" means empty to both parameters.
Without it `?quality=%2082` was accepted (int() ignores surrounding whitespace and silently
"corrected" the value) while the same padding on `?format=%20png` was a 400, and `?quality=%20`
was empty to int() but not to the emptiness rule — one rule, applied to whitespace the same way
on both. Padding is now trimmed for both and a value that is whitespace-only is empty, i.e. 400.

A REPEATED parameter (`?format=png&format=`) takes its FIRST occurrence, the long-standing
parse_qs `[0]` convention — the query string has no schema, so "the first one wins" is as good
a rule as any and is the one every caller of this service already gets. The interesting case is
`?format=png&format=`: the blank is in second position and is therefore harmless, while
`?format=&format=png` is a 400 because the blank came first. That asymmetry is deliberate — the
rule is about the value that is USED, not about every value that was typed.
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
    encoder cannot write — the boundary turns every one of those into a 400. The format
    whitelist and the quality range are already refused in do_POST (before the body is
    read); the checks here are defensive guards for direct callers of this function."""
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
    socket. Each early-refusal path below *also* closes explicitly (close=True sends a
    literal `Connection: close` header, which makes BaseHTTPRequestHandler set
    close_connection = True), so the invariant is stated where it matters, is visible on
    the wire, and survives a protocol bump. Under HTTP/1.0 the client would close anyway
    (will_close is always true), so the explicit header is what the tests assert on — a
    non-tautological check that would still fail if a refusal path lost its close after a
    switch to HTTP/1.1 (protocol_version = "HTTP/1.1"). The alternative would be draining
    Content-Length bytes before answering, which invites exactly the slow/oversized-body
    abuse the guard-rails exist to refuse."""

    timeout = SOCKET_TIMEOUT_SECONDS   # settimeout() on each connection (StreamRequestHandler)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self._json(200, {"status": "UP"})
        else:
            # same early-refusal rule as do_POST's unknown path: close, so any bytes a
            # confused client might still send die with the socket
            self._json(404, {"error": "not found"}, close=True)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/encode":
            # early refusal, body never read — close, don't leave unread bytes on the wire
            self._json(404, {"error": "not found"}, close=True)
            return
        # keep_blank_values: `?format=` must reach the whitelist check as the empty string and
        # be refused, not be dropped by parse_qs and quietly become the webp default (see the
        # module docstring — a present-but-empty parameter is a mistake, not an omission)
        query = parse_qs(parsed.query, keep_blank_values=True)
        # .strip(): whitespace is padding, never a value — `?format=%20png` is "png" and
        # `?format=%20` is empty, i.e. refused, exactly like `?format=`. [0] = first
        # occurrence wins for a repeated parameter (see the module docstring)
        fmt = query.get("format", ["webp"])[0].strip().lower()
        if fmt not in SUPPORTED:
            # the format is known from the query string alone — refuse it here, before a
            # single body byte is read, same early-refusal rule as quality below (it used
            # to be rejected only in encode(), AFTER the whole body had been read)
            # quoted on purpose: `?format=` is a real client mistake and "unsupported
            # format: " with nothing after it would read like a server bug
            self._json(400, {"error": f"unsupported format: {fmt!r}"}, close=True)
            return
        # an omitted quality means the default; a blank one (`?quality=`) reaches int() as ""
        # and is refused there, deliberately — see the module docstring. Stripped first, so
        # int() no longer gets to silently accept `?quality=%2082`: whitespace is trimmed by
        # the same rule that governs format, and a whitespace-only value is empty, not 82.
        raw_quality = query.get("quality", [str(DEFAULT_QUALITY)])[0].strip()
        try:
            quality = int(raw_quality)
        except ValueError:
            self._json(400, {"error": f"quality must be an integer in 0..100, got: {raw_quality!r}"},
                       close=True)
            return
        if not 0 <= quality <= 100:
            # the range check lives here, next to the parse, so an out-of-range quality is
            # refused before a single body byte is read — same early-refusal rule as above
            self._json(400, {"error": f"quality must be an integer in 0..100, got: {raw_quality!r}"},
                       close=True)
            return
        declared = self.headers.get("Content-Length")
        if declared is None:
            self._json(411, {"error": "Content-Length required"}, close=True)
            return
        try:
            length = int(declared)
        except ValueError:
            # quoted for the same reason as the format and quality messages: `Content-Length:`
            # with nothing after it is a real client mistake, and "malformed Content-Length: "
            # with an empty tail reads like the server lost the value it was complaining about
            self._json(400, {"error": f"malformed Content-Length: {declared!r}"}, close=True)
            return
        if length < 0:
            self._json(400, {"error": f"malformed Content-Length: {declared!r}"}, close=True)
            return
        if length > MAX_UPLOAD_BYTES:
            # refuse on the declared size, before reading a single body byte
            self._json(413, {"error": f"body too large: {length} > {MAX_UPLOAD_BYTES} bytes"},
                       close=True)
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

    def _json(self, status, body, close=False):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if close:
            # an explicit `Connection: close` on the wire for early refusals: send_header
            # also flips close_connection, so the socket really does die with the response
            # — stated per refusal path, independent of the HTTP/1.0 default close
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        log("INFO", f"{self.command} {self.path}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8087"))
    log("INFO", f"image-encoder listening on {port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
