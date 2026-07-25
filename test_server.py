"""The encoder does its one job: a readable image becomes smaller WebP; garbage is refused.

Two layers: encode() as a function, and the real HTTP boundary — a live ThreadingHTTPServer
on an ephemeral port, talked to with http.client — where the status codes are the contract."""

import http.client
import io
import threading
import unittest
from unittest import mock

from PIL import Image

import server
from server import Handler, encode
from http.server import ThreadingHTTPServer


def _png(width=200, height=200, mode="RGB"):
    color = (120, 30, 30) if mode == "RGB" else (120, 200)
    image = Image.new(mode, (width, height), color)
    if mode == "RGB":
        for x in range(width):
            image.putpixel((x, x % height), (0, 200, 200))   # some detail so WebP has work to do
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class EncodeTest(unittest.TestCase):

    def test_webp_is_a_valid_smaller_image(self):
        png = _png()
        webp = encode(png, "webp", 80)
        self.assertLess(len(webp), len(png), "WebP is the point: fewer bytes than the PNG")
        # it decodes, and to the same dimensions
        back = Image.open(io.BytesIO(webp))
        self.assertEqual("WEBP", back.format)
        self.assertEqual((200, 200), back.size)

    def test_an_unreadable_image_is_refused(self):
        with self.assertRaises(ValueError):
            encode(b"not an image", "webp", 80)

    def test_an_unsupported_format_is_refused(self):
        with self.assertRaises(ValueError):
            encode(_png(), "tiff", 80)

    def test_deterministic(self):
        png = _png()
        self.assertEqual(encode(png, "webp", 75), encode(png, "webp", 75))

    def test_a_non_whitelisted_decoder_is_refused(self):
        gif = io.BytesIO()
        Image.new("P", (10, 10)).save(gif, "GIF")   # a real image, but GIF is not decoded here
        with self.assertRaises(ValueError):
            encode(gif.getvalue(), "webp", 80)

    def test_quality_out_of_range_is_refused(self):
        for bad in (-5, 101, 100000):
            with self.assertRaises(ValueError):
                encode(_png(), "jpeg", bad)

    def test_too_many_pixels_is_refused_before_load(self):
        with mock.patch.object(server, "MAX_IMAGE_PIXELS", 100_000):
            with self.assertRaisesRegex(ValueError, "too large"):
                encode(_png(400, 400), "webp", 80)   # 160k px > lowered 100k cap

    def test_la_png_survives_jpeg_encoding(self):
        # the confirmed bug: LA-mode PNG -> JPEG used to raise OSError from save() and tear
        # the connection; the mode is now flattened to RGB first
        jpeg = encode(_png(mode="LA"), "jpeg", 80)
        self.assertEqual("JPEG", Image.open(io.BytesIO(jpeg)).format)

    def test_a_save_failure_is_a_value_error_not_a_crash(self):
        png = _png()   # built before the patch: this helper calls save() too
        with mock.patch.object(Image.Image, "save", side_effect=OSError("cannot write mode X")):
            with self.assertRaisesRegex(ValueError, "cannot encode"):
                encode(png, "webp", 80)


class HttpTest(unittest.TestCase):
    """The boundary itself: a live server, real sockets, status codes as the contract."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=body)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read()
        finally:
            conn.close()

    def test_health_is_up(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertIn(b'"UP"', resp.read())
        finally:
            conn.close()

    def test_happy_path_png_to_webp(self):
        status, ctype, body = self._post("/encode?format=webp&quality=80", _png())
        self.assertEqual(200, status)
        self.assertEqual("image/webp", ctype)
        self.assertEqual("WEBP", Image.open(io.BytesIO(body)).format)

    def test_garbage_body_is_400(self):
        status, _, body = self._post("/encode?format=webp", b"definitely not an image")
        self.assertEqual(400, status)
        self.assertIn(b"unreadable", body)

    def test_unsupported_format_is_400(self):
        status, _, _ = self._post("/encode?format=tiff", _png())
        self.assertEqual(400, status)

    def test_bad_quality_is_400(self):
        for bad in ("-5", "100000", "abc"):
            with self.subTest(quality=bad):
                status, _, _ = self._post(f"/encode?format=jpeg&quality={bad}", _png())
                self.assertEqual(400, status)

    def test_la_png_to_jpeg_is_flattened_not_a_torn_connection(self):
        # before the save() barrier + mode flattening this OSError'd mid-request and the
        # client saw a dropped connection; now the encode simply succeeds
        status, ctype, body = self._post("/encode?format=jpeg", _png(mode="LA"))
        self.assertEqual(200, status)
        self.assertEqual("image/jpeg", ctype)
        self.assertEqual("JPEG", Image.open(io.BytesIO(body)).format)

    def test_oversized_declared_body_is_413_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/encode?format=webp")
            conn.putheader("Content-Length", str(server.MAX_UPLOAD_BYTES + 1))
            conn.endheaders()
            conn.send(b"x")   # the server must answer without waiting for 12 MiB
            self.assertEqual(413, conn.getresponse().status)
        finally:
            conn.close()

    def test_missing_content_length_is_411(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/encode?format=webp")
            conn.endheaders()
            self.assertEqual(411, conn.getresponse().status)
        finally:
            conn.close()

    def test_bad_quality_with_huge_declared_body_answers_and_closes(self):
        # early refusal must not wait for the declared body: declare 8 MiB, send one byte,
        # and the 400 must come back immediately with the connection closed behind it
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/encode?format=webp&quality=abc")
            conn.putheader("Content-Length", str(8 * 1024 * 1024))
            conn.endheaders()
            conn.send(b"x")   # 8 MiB - 1 never arrives; the server must answer anyway
            resp = conn.getresponse()
            self.assertEqual(400, resp.status)
            self.assertIn(b"quality", resp.read())
            self.assertTrue(resp.will_close, "an unread body means the connection must close")
            # under HTTP/1.0 will_close is ALWAYS true, so the line above alone proves
            # nothing — the explicit header is the non-tautological witness that the
            # refusal path really closes, and it would keep failing after an HTTP/1.1 bump
            self.assertEqual("close", resp.getheader("Connection"),
                             "an early refusal must say Connection: close on the wire")
        finally:
            conn.close()

    def test_out_of_range_quality_with_huge_declared_body_answers_and_closes(self):
        # the twin of the parse-failure test above: quality=101 parses fine but is out of
        # range, and the refusal must be just as early — declare 8 MiB, send one byte, and
        # the 400 must come back immediately (3s client timeout) with the connection closed
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.putrequest("POST", "/encode?format=webp&quality=101")
            conn.putheader("Content-Length", str(8 * 1024 * 1024))
            conn.endheaders()
            conn.send(b"x")   # 8 MiB - 1 never arrives; the server must answer anyway
            resp = conn.getresponse()
            self.assertEqual(400, resp.status)
            self.assertIn(b"quality", resp.read())
            self.assertTrue(resp.will_close, "an unread body means the connection must close")
            # see test_bad_quality...: will_close is tautological under HTTP/1.0; the
            # explicit header is what proves the refusal path closes on purpose
            self.assertEqual("close", resp.getheader("Connection"),
                             "an early refusal must say Connection: close on the wire")
        finally:
            conn.close()

    def test_get_404_closes_the_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/no-such-path")
            resp = conn.getresponse()
            self.assertEqual(404, resp.status)
            self.assertTrue(resp.will_close, "an unknown path is refused early and closed")
            # will_close is always true under HTTP/1.0 — the explicit header is the
            # assertion with teeth (it survives, and matters after, a bump to HTTP/1.1)
            self.assertEqual("close", resp.getheader("Connection"),
                             "an early refusal must say Connection: close on the wire")
        finally:
            conn.close()

    def test_malformed_content_length_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/encode?format=webp")
            conn.putheader("Content-Length", "banana")
            conn.endheaders()
            self.assertEqual(400, conn.getresponse().status)
        finally:
            conn.close()

    def test_image_over_pixel_cap_is_400(self):
        with mock.patch.object(server, "MAX_IMAGE_PIXELS", 100_000):
            status, _, body = self._post("/encode?format=webp", _png(400, 400))
        self.assertEqual(400, status)
        self.assertIn(b"too large", body)


if __name__ == "__main__":
    unittest.main()
