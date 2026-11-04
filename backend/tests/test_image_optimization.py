import asyncio
import json
import threading
import zipfile
from dataclasses import FrozenInstanceError
from io import BytesIO
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import pytest
from PIL import Image

from mangarr import settings_service
from mangarr.download import direct, images
from mangarr.download.cbz import guess_extension, write_cbz
from mangarr.download.images import ImageOptimization as Policy, optimize_page
from mangarr.library.matcher import (
    PAGE_PROVENANCE_NAME, archive_page_signature, archive_source_page_signature,
    page_bytes_signature,
)
from mangarr.models import Chapter, Series
from mangarr.sources.base import DirectSource


def page(size=(120, 200), mode="RGB", fmt="PNG", **options):
    # Distinct pixels and gradients exercise lossy encoding without random tests.
    image = Image.new(mode, size)
    if mode == "RGB":
        image.putdata([(x % 256, y % 256, (x + y) % 256)
                       for y in range(size[1]) for x in range(size[0])])
    elif mode == "RGBA":
        image.paste((20, 40, 60, 100), (0, 0, *size))
    output = BytesIO()
    image.save(output, format=fmt, **options)
    return output.getvalue()


def decoded(data):
    image = Image.open(BytesIO(data))
    image.load()
    return image


def test_off_preserves_bytes_without_opening_decoder(monkeypatch):
    monkeypatch.setattr(images.Image, "open", lambda *a, **kw: pytest.fail("decoder called"))
    original = b"original bytes, including unsupported data"
    assert optimize_page(original, Policy()) is original
    assert Policy.from_settings({}) == Policy()
    with pytest.raises(FrozenInstanceError):
        Policy().quality = 1


@pytest.mark.parametrize("fmt,extension", [("jpeg", ".jpg"), ("webp", ".webp")])
def test_format_quality_resize_aspect_and_no_upscale(fmt, extension):
    original = page((400, 800))
    result = optimize_page(original, Policy(True, fmt, 80, 200))
    assert guess_extension(result) == extension
    assert decoded(result).size == (200, 400)
    small = page((80, 120))
    assert decoded(optimize_page(small, Policy(True, fmt, 80, 200))).size == (80, 120)
    low = optimize_page(original, Policy(True, fmt, 20, 200))
    high = optimize_page(original, Policy(True, fmt, 95, 200))
    assert len(low) < len(high)


@pytest.mark.parametrize("fmt", ["jpeg", "webp"])
def test_smaller_file_fallback_and_resize_precedence(fmt, monkeypatch):
    original = page((2, 2), mode="1")
    real_save = Image.Image.save
    def larger_encoding(image, output, *args, **kwargs):
        real_save(image, output, *args, **kwargs)
        output.write(b"padding" * 100)  # deterministic larger-output codec result
    monkeypatch.setattr(Image.Image, "save", larger_encoding)
    assert optimize_page(original, Policy(True, fmt, 100)) is original
    resized = optimize_page(original, Policy(True, fmt, 100, 1))
    assert len(resized) > len(original)
    assert decoded(resized).size == (1, 1)


def test_orientation_before_width_limit():
    exif = Image.Exif()
    exif[274] = 6
    original = page((80, 160), fmt="JPEG", exif=exif)
    result = optimize_page(original, Policy(True, "jpeg", 85, 100))
    image = decoded(result)
    assert image.size == (100, 50)
    assert image.getexif().get(274, 1) == 1


def test_transparency_is_preserved_and_jpeg_keeps_original():
    original = page((80, 120), mode="RGBA")
    assert optimize_page(original, Policy(True, "jpeg", 85, 40)) is original
    image = decoded(optimize_page(original, Policy(True, "webp", 85, 40)))
    assert image.size == (40, 60)
    assert image.mode == "RGBA"
    assert image.getchannel("A").getextrema() == (100, 100)
    palette = Image.new("P", (80, 120))
    output = BytesIO()
    palette.save(output, format="PNG", transparency=0)
    assert optimize_page(output.getvalue(), Policy(True, "jpeg")) == output.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "GIF"])
def test_animation_keeps_all_frames_original(fmt):
    first = Image.new("RGB", (20, 40), "white")
    second = Image.new("RGB", (20, 40), "black")
    output = BytesIO()
    first.save(output, format=fmt, save_all=True, append_images=[second], duration=100, loop=0)
    original = output.getvalue()
    assert optimize_page(original, Policy(True, "webp", 10, 5)) is original
    assert decoded(original).n_frames == 2


@pytest.mark.parametrize("original", [b"\xff\xd8\xffinvalid", b"GIF89aunknown", b"\0\0\0\x20ftypavif", b"not an image"])
def test_unsupported_or_invalid_decoder_input_keeps_original(original):
    assert optimize_page(original, Policy(True)) is original


def test_static_gif_avif_and_cmyk_are_passthrough():
    for original in (page(fmt="GIF"), page(fmt="AVIF"), page(mode="CMYK", fmt="JPEG")):
        assert optimize_page(original, Policy(True, "jpeg", 1, 5)) is original


def test_tall_webtoon_encoder_limit_and_supported_resize():
    original = page((10, 17000), mode="L")
    assert optimize_page(original, Policy(True, "webp")) is original
    resized = optimize_page(original, Policy(True, "webp", 85, 5))
    assert decoded(resized).size == (5, 8500)
    assert decoded(optimize_page(original, Policy(True, "jpeg", 85, 5))).size == (5, 8500)


def test_size_guards_happen_before_pixel_decode(monkeypatch):
    original = page()
    monkeypatch.setattr(images, "MAX_INPUT_BYTES", len(original) - 1)
    assert optimize_page(original, Policy(True)) is original
    monkeypatch.setattr(images, "MAX_INPUT_BYTES", len(original) + 1)
    monkeypatch.setattr(images, "MAX_PIXELS", 1)
    monkeypatch.setattr(Image.Image, "load", lambda *a: pytest.fail("pixel decode called"))
    assert optimize_page(original, Policy(True)) is original


def test_decompression_bomb_and_encoder_failure_keep_original(monkeypatch):
    original = page()
    monkeypatch.setattr(images.Image, "MAX_IMAGE_PIXELS", 1)
    assert optimize_page(original, Policy(True)) is original
    monkeypatch.setattr(images.Image, "MAX_IMAGE_PIXELS", 1000000)
    def fail(*args, **kwargs):
        raise OSError("encoder failure")
    monkeypatch.setattr(Image.Image, "save", fail)
    assert optimize_page(original, Policy(True)) is original


@pytest.mark.parametrize("key,value", [
    ("enabled", "yes"), ("format", "png"), ("quality", "0"),
    ("quality", "101"), ("quality", "85.5"), ("quality", ""),
    ("max_width", "-1"), ("max_width", "10001"), ("max_width", "no"),
])
def test_invalid_settings_are_rejected_even_when_disabled(key, value):
    with pytest.raises(ValueError, match=f"image_optimization_{key}"):
        settings_service.validate({f"image_optimization_{key}": value})


def test_codec_availability_checked_only_when_enabled(monkeypatch):
    monkeypatch.setattr(images.features, "check", lambda name: False)
    settings_service.validate({})
    with pytest.raises(ValueError, match="WebP encoding is unavailable"):
        settings_service.validate({"image_optimization_enabled": "true"})
    with pytest.raises(ValueError, match="JPEG encoding is unavailable"):
        settings_service.validate({"image_optimization_enabled": "true", "image_optimization_format": "jpeg"})


class Source(DirectSource):
    name = "fixture"
    def __init__(self, pages):
        self.pages = pages
    async def search_series(self, query): return []
    async def list_chapters(self, external_id): return []
    async def get_pages(self, external_id):
        return list(range(len(self.pages)))
    async def download_page(self, client, url):
        await asyncio.sleep(0)
        return self.pages[url]


def media():
    return (SimpleNamespace(title="Test", description="Summary"),
            SimpleNamespace(number=1, volume=2, title="Title"))


@pytest.mark.parametrize("policy", [Policy(), Policy(True, "jpeg", 85, 60), Policy(True, "webp", 85, 60)])
async def test_direct_archive_order_metadata_and_signatures(tmp_path, policy):
    pages = [page((120, 200)), page((160, 240)), page((200, 280))]
    source = Source(pages)
    series, chapter = media()
    dest = tmp_path / "chapter.cbz"
    original_signature = page_bytes_signature(pages)
    await direct.download_chapter_to_cbz(source, "chapter", series, chapter, dest,
                                         optimization=policy)
    with zipfile.ZipFile(dest) as archive:
        names = [n for n in archive.namelist() if n[0].isdigit()]
        expected_ext = ".jpg" if policy.format == "jpeg" else ".webp"
        assert names == [f"{i:03d}{expected_ext if policy.enabled else '.png'}" for i in range(1, 4)]
        for i, name in enumerate(names):
            data = archive.read(name)
            if policy.enabled:
                image = decoded(data)
                assert image.width == 60
                assert abs(image.height / image.width - decoded(pages[i]).height / decoded(pages[i]).width) < .02
            else:
                assert data == pages[i]
        xml = fromstring(archive.read("ComicInfo.xml"))
        assert [xml.findtext(k) for k in ["Series", "Number", "Volume", "Title", "PageCount"]] == ["Test", "1", "2", "Title", "3"]
        assert (PAGE_PROVENANCE_NAME in archive.namelist()) == policy.enabled
    assert archive_source_page_signature(dest) == original_signature
    if policy.enabled:
        assert archive_page_signature(dest) != original_signature
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("existing_policy", [Policy(), Policy(True, "webp", 90, 60), Policy(True, "jpeg", 70, 40)])
@pytest.mark.parametrize("new_policy", [Policy(), Policy(True, "webp", 50, 80), Policy(True, "jpeg", 80, 50)])
async def test_duplicate_rejection_across_policy_changes(tmp_path, existing_policy, new_policy):
    existing = Chapter(id=1, series_id=1, number=1)
    candidate = Chapter(id=2, series_id=1, number=2)
    series = Series(id=1, title="Test", description="", chapters=[existing, candidate])
    path = tmp_path / "first.cbz"
    await direct.download_chapter_to_cbz(Source([page()]), "c1", series, existing, path, optimization=existing_policy)
    existing.downloaded, existing.file_path = True, str(path)
    dest = tmp_path / "second.cbz"
    with pytest.raises(direct.DuplicateChapterPagesError):
        await direct.download_chapter_to_cbz(Source([page()]), "c2", series, candidate, dest, optimization=new_policy)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.partial"))


async def test_distinct_originals_with_identical_lossy_output_are_not_duplicates(tmp_path, monkeypatch):
    existing = Chapter(id=1, series_id=1, number=1)
    candidate = Chapter(id=2, series_id=1, number=2)
    series = Series(id=1, title="Test", description="", chapters=[existing, candidate])
    compressed = page((10, 20), fmt="JPEG")
    async def same_output(data, policy): return compressed
    monkeypatch.setattr(direct, "optimize_page_async", same_output)
    path = tmp_path / "first.cbz"
    await direct.download_chapter_to_cbz(Source([page()]), "c1", series, existing, path, optimization=Policy(True))
    existing.downloaded, existing.file_path = True, str(path)
    dest = tmp_path / "second.cbz"
    await direct.download_chapter_to_cbz(Source([page((120, 201))]), "c2", series, candidate, dest, optimization=Policy(True))
    assert dest.exists()
    assert archive_page_signature(dest) == archive_page_signature(path)
    assert archive_source_page_signature(dest) != archive_source_page_signature(path)


@pytest.mark.parametrize("damage", ["archived", "original", "version", "oversize", "duplicate"])
def test_invalid_or_stale_provenance_not_used(tmp_path, damage):
    path = tmp_path / "bad.cbz"
    pages = [page()]
    payload = {"version": 1, "original": [[123, 456]], "archived": page_bytes_signature(pages)}
    if damage == "archived": payload["archived"] = [[4, 5]]
    if damage == "original": payload["original"] = [["123", 456]]
    if damage == "version": payload["version"] = 10
    data = json.dumps(payload) if damage != "oversize" else " " * (1024 * 1024 + 1)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", pages[0])
        archive.writestr(PAGE_PROVENANCE_NAME, data)
        if damage == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr(PAGE_PROVENANCE_NAME, data)
    assert archive_source_page_signature(path) is None


async def test_cancellation_during_optimization_never_publishes(tmp_path, monkeypatch):
    checks = 0
    cancelled = False
    async def cancel():
        nonlocal checks
        checks += 1
        if cancelled: raise RuntimeError("removed")
    async def optimize(data, policy):
        nonlocal cancelled
        cancelled = True
        return data
    monkeypatch.setattr(direct, "optimize_page_async", optimize)
    dest = tmp_path / "cancel.cbz"
    with pytest.raises(RuntimeError, match="removed"):
        await direct.download_chapter_to_cbz(Source([page()]), "c", *media(), dest,
            optimization=Policy(True), cancel_cb=cancel)
    assert checks > 1
    assert not dest.exists()
    assert not list(tmp_path.glob("*.partial"))


async def test_transform_failure_never_publishes(tmp_path, monkeypatch):
    async def fail(data, policy): raise RuntimeError("unexpected worker error")
    monkeypatch.setattr(direct, "optimize_page_async", fail)
    dest = tmp_path / "failed.cbz"
    with pytest.raises(RuntimeError, match="worker error"):
        await direct.download_chapter_to_cbz(Source([page()]), "c", *media(), dest, optimization=Policy(True))
    assert not dest.exists()
    assert not list(tmp_path.glob("*.partial"))


async def test_worker_off_loop_bounded_and_repeated_cancellation_holds_slot(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    loop_thread = threading.get_ident()
    def blocked(data, policy):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return data
    monkeypatch.setattr(images, "optimize_page", blocked)
    first = asyncio.create_task(images.optimize_page_async(b"one", Policy(True)))
    try:
        for _ in range(200):
            if entered.is_set(): break
            await asyncio.sleep(.005)
        assert entered.is_set()
        second = asyncio.create_task(images.optimize_page_async(b"two", Policy(True)))
        first.cancel()
        await asyncio.sleep(.01)
        first.cancel()
        await asyncio.sleep(.01)
        assert not first.done()
        assert len(calls) == 1
        assert calls[0] != loop_thread
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await first
    assert await second == b"two"
    assert len(calls) == 2


def test_volume_merge_preserves_original_identities_in_reading_order(tmp_path):
    from mangarr.library.volume_merge import merge_chapter_archives
    first, second, merged = [tmp_path / name for name in ['first.cbz','second.cbz','merged.cbz']]
    originals = [page((120, 200)), page((120, 201)), page((120, 202))]
    policy = Policy(True, 'jpeg', 85, 60)
    write_cbz(first, [optimize_page(p, policy) for p in originals[:2]], '<ComicInfo/>',
              original_signature=page_bytes_signature(originals[:2]))
    write_cbz(second, [originals[2]], '<ComicInfo/>')
    assert merge_chapter_archives([(2, second), (1, first)], merged, 'Test', 1) == 3
    assert archive_source_page_signature(merged) == page_bytes_signature(originals)
    assert archive_page_signature(merged) != page_bytes_signature(originals)
    # A single-chapter volume must still catch source chapter aliases.
    single = tmp_path / 'single.cbz'
    merge_chapter_archives([(1, first)], single, 'Test', 1)
    assert archive_source_page_signature(single) == page_bytes_signature(originals[:2])


def test_volume_merge_remaps_provenance_to_natural_filename_order(tmp_path):
    from mangarr.library.volume_merge import merge_chapter_archives
    original = tmp_path / 'unordered.cbz'
    merged = tmp_path / 'merged.cbz'
    pages = [page((30, 30)), page((40, 40))]
    original_ids = ((12, 123), (34, 345))
    with zipfile.ZipFile(original, 'w') as archive:
        archive.writestr('10.png', pages[0])
        archive.writestr('2.png', pages[1])
        archive.writestr(PAGE_PROVENANCE_NAME, json.dumps({
            'version': 1, 'original': original_ids, 'archived': page_bytes_signature(pages),
        }))
    merge_chapter_archives([(1, original)], merged, 'Test', 1)
    assert archive_source_page_signature(merged) == original_ids[::-1]


def test_color_profile_retained_and_non_rgb_profile_keeps_original():
    profile = b'profile fixture'
    original = page(icc_profile=profile)
    output = optimize_page(original, Policy(True, 'webp', 85, 60))
    assert decoded(output).info['icc_profile'] == profile
    grayscale = page(mode='L', icc_profile=profile)
    assert optimize_page(grayscale, Policy(True, 'webp', 85, 60)) is grayscale


def test_jpeg_very_tall_pages_use_original_or_safe_resize():
    original = page((10, 66000), mode='L')
    assert optimize_page(original, Policy(True, 'jpeg')) is original
    result = optimize_page(original, Policy(True, 'jpeg', 85, 5))
    assert decoded(result).size == (5, 33000)


async def test_passthrough_pages_do_not_add_provenance_overhead(tmp_path):
    original = page(fmt='GIF')
    dest = tmp_path / 'passthrough.cbz'
    await direct.download_chapter_to_cbz(Source([original]), 'c', *media(), dest,
                                         optimization=Policy(True))
    with zipfile.ZipFile(dest) as archive:
        assert archive.namelist() == ['ComicInfo.xml', '001.gif']
        assert archive.read('001.gif') == original
    assert archive_source_page_signature(dest) == page_bytes_signature([original])
