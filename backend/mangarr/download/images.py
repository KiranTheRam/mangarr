"""Optional, bounded page optimization. Unsupported pages always keep their bytes."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from weakref import WeakKeyDictionary
from typing import TypeVar

from PIL import Image, ImageOps, features

log = logging.getLogger(__name__)

# One decoded page at a time across downloads; compressed chapter bytes are
# already held by the downloader. Reject oversized headers before pixel decode.
MAX_PIXELS = 20_000_000
MAX_INPUT_BYTES = 32 * 1024 * 1024
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="page-optimizer")
_slots: WeakKeyDictionary = WeakKeyDictionary()
_Result = TypeVar("_Result")


@dataclass(frozen=True)
class ImageOptimization:
    enabled: bool = False
    format: str = "webp"
    quality: int = 85
    max_width: int = 0

    @classmethod
    def from_settings(cls, values: dict[str, str]) -> "ImageOptimization":
        enabled = values.get("image_optimization_enabled", "false")
        if enabled not in {"true", "false"}:
            raise ValueError("image_optimization_enabled must be true or false")
        fmt = values.get("image_optimization_format", "webp")
        if fmt not in {"jpeg", "webp"}:
            raise ValueError("image_optimization_format must be jpeg or webp")
        numbers = {}
        for name, default, low, high in (("quality", "85", 1, 100), ("max_width", "0", 0, 10000)):
            key = f"image_optimization_{name}"
            try:
                number = int(values.get(key, default))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number") from None
            if not low <= number <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            numbers[name] = number
        if enabled == "true" and fmt == "webp" and not features.check("webp"):
            raise ValueError("WebP encoding is unavailable in this Pillow installation; choose JPEG")
        if enabled == "true" and fmt == "jpeg" and not features.check("jpg"):
            raise ValueError("JPEG encoding is unavailable in this Pillow installation")
        return cls(enabled == "true", fmt, **numbers)


def optimize_page(data: bytes, policy: ImageOptimization) -> bytes:
    """Keep originals on animation, unsafe size, unsupported mode or codec errors.

    Width is measured after EXIF orientation. A required resize takes priority
    over the smaller-file rule, but never over safety/format preservation.
    """
    if not policy.enabled:
        return data
    if len(data) > MAX_INPUT_BYTES:
        log.warning("Keeping original page: exceeds 32 MiB optimization input limit")
        return data
    try:
        # GIF/AVIF and other formats are intentionally passthrough. Pillow's
        # own decompression-bomb protection remains enabled as a second guard.
        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as image:
            if image.width * image.height > MAX_PIXELS:
                log.warning("Keeping original page: exceeds 20 megapixel optimization limit")
                return data
            if getattr(image, "is_animated", False):
                log.info("Keeping original animated page")
                return data
            if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                log.info("Keeping original page: unsupported color mode %s", image.mode)
                return data
            if image.info.get("icc_profile") and image.mode not in {"RGB", "RGBA", "P"}:
                log.info("Keeping original page: non-RGB color profile")
                return data
            image.load()
            ImageOps.exif_transpose(image, in_place=True)
            alpha = "A" in image.getbands() or "transparency" in image.info
            if alpha and policy.format == "jpeg":
                log.info("Keeping original transparent page: JPEG cannot preserve alpha")
                return data
            width, height = image.size
            resize = bool(policy.max_width and width > policy.max_width)
            if resize:
                width, height = policy.max_width, max(1, round(height * policy.max_width / width))
            # WebP's encoder cannot represent a very tall webtoon in one image.
            limit = 16383 if policy.format == "webp" else 65500
            if max(width, height) > limit:
                log.info("Keeping original page: dimensions exceed %s encoder limit", policy.format)
                return data
            with image.convert("RGBA" if alpha else "RGB") as converted:
                if resize:
                    output_image = converted.resize((width, height), Image.Resampling.LANCZOS)
                else:
                    output_image = converted
                try:
                    with BytesIO() as output:
                        options = {"quality": policy.quality, "icc_profile": image.info.get("icc_profile")}
                        if policy.format == "jpeg":
                            options.update(subsampling=0, optimize=True)
                        else:
                            options.update(method=4)
                        output_image.save(output, format=policy.format.upper(), **options)
                        result = output.getvalue()
                finally:
                    if output_image is not converted:
                        output_image.close()
            return result if resize or len(result) < len(data) else data
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # Never discard a page or flatten an animation because an optional
        # optimization failed. Log types only: decoder text may contain data.
        log.warning("Keeping original page: optimization unavailable (%s)", type(exc).__name__)
        return data


async def optimize_page_async(data: bytes, policy: ImageOptimization) -> bytes:
    if not policy.enabled:
        return data
    return await run_image_work(optimize_page, data, policy)


async def run_image_work(function: Callable[..., _Result], *args) -> _Result:
    """Share the bounded image worker with the Settings sample renderer."""
    loop = asyncio.get_running_loop()
    slot = _slots.setdefault(loop, asyncio.Semaphore(1))
    async with slot:
        future = loop.run_in_executor(_executor, function, *args)
        # Cancelling a Future cannot stop a running codec. Keep the slot until
        # it exits, so repeated cancellations cannot accumulate decoded pages.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve any worker exception without replacing cancellation.
            future.exception()
            raise
