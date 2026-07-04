"""The encoder does its one job: a readable image becomes smaller WebP; garbage is refused."""

import io
import unittest

from PIL import Image

from server import encode


def _png(width=200, height=200):
    image = Image.new("RGB", (width, height), (120, 30, 30))
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


if __name__ == "__main__":
    unittest.main()
