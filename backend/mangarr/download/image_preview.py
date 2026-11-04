"""A local test page for comparing the actual download encoder's output.

No uploads, remote image URLs, library files or settings writes are involved.
"""

import asyncio
import base64
from functools import lru_cache
from io import BytesIO
from random import Random
from weakref import WeakKeyDictionary

from PIL import Image, ImageDraw, ImageFont

from .cbz import guess_extension
from .images import ImageOptimization, optimize_page_async, run_image_work

_locks: WeakKeyDictionary = WeakKeyDictionary()


class PreviewBusyError(Exception):
    pass


@lru_cache(maxsize=1)
def sample_page() -> bytes:
    """Generate our own sample: fine text/lines, halftones and color gradients."""
    with Image.new("RGB", (1200, 1600), "white") as image:
        draw = ImageDraw.Draw(image)
        def text(x, y, value, size=26, fill="#151923"):
            draw.text((x, y), value, font=ImageFont.load_default(size=size), fill=fill)

        draw.rectangle((0, 0, 1200, 155), fill="#182034")
        text(55, 35, "MANGARR / IMAGE QUALITY SAMPLE", 44, "white")
        text(58, 100, "Compare text, fine lines, shading and color at actual size.", 23, "#ccd8eb")
        draw.rounded_rectangle((50, 195, 1150, 460), radius=45, outline="#151923", width=4)
        text(90, 225, "THE SMALL DETAILS MATTER.", 39)
        text(90, 290, "Can you still read every word in the speech bubble?", 26)
        text(90, 340, "Fine print: 0123456789  ABCDEFGHIJKLMNOPQRSTUVWXYZ", 18)
        text(90, 378, "Small lettering and thin strokes reveal compression artifacts.", 14)
        text(90, 411, "Original quality preserves the source image bytes.", 22)

        # Dense hatching and curved lines resemble detailed monochrome artwork.
        draw.rectangle((50, 500, 575, 970), outline="#151923", width=3)
        for x in range(-400, 600, 7):
            draw.line((max(55, x), 510, min(568, x + 450), 960), fill="#252525", width=1)
        draw.ellipse((110, 550, 515, 920), fill="white", outline="black", width=4)
        for inset in range(0, 140, 7):
            draw.arc((130+inset, 565+inset, 500-inset, 910-inset), 15, 335, fill="black", width=2)
        draw.rectangle((625, 500, 1150, 970), outline="#151923", width=3)
        for y in range(520, 950, 8):
            for x in range(645, 1130, 8):
                radius = 1 + (y - 520) // 145
                draw.ellipse((x, y, x+radius, y+radius), fill="#292929")
        text(70, 990, "Fine lines and curves", 24)
        text(645, 990, "Halftone and texture", 24)

        # Smooth gradients plus a small deterministic grain challenge codecs
        # without depending on copyrighted artwork or an external image service.
        grain_bytes = Random(84).randbytes(1100 * 380)
        for y in range(1060, 1440):
            row = y - 1060
            for x in range(50, 1150):
                grain = grain_bytes[row * 1100 + x - 50] % 13 - 6
                color = tuple(max(0, min(255, v + grain)) for v in (
                    round(245 - 180 * (x-50) / 1100),
                    round(80 + 140 * row / 380),
                    round(105 + 135 * (x-50) / 1100),
                ))
                draw.point((x, y), fill=color)
        text(65, 1465, "Smooth gradients / subtle color grain", 25)
        text(65, 1520, "Sample only. Savings and acceptable quality vary between chapter pages.", 20)
        with BytesIO() as output:
            image.save(output, "PNG")
            return output.getvalue()


def describe_image(data: bytes, original: bytes) -> dict:
    media = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[guess_extension(data)]
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
    return {
        "data_url": f"data:{media};base64," + base64.b64encode(data).decode("ascii"),
        "size": len(data), "width": width, "height": height,
        "original_kept": data == original,
    }


async def preview_image(policy: ImageOptimization) -> dict:
    # Do not accumulate codec requests when multiple browsers drag sliders.
    # The browser also debounces and serializes its own preview requests.
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    if lock.locked():
        raise PreviewBusyError
    async with lock:
        original = await run_image_work(sample_page)
        result = await optimize_page_async(original, policy)
        return describe_image(result, original)
