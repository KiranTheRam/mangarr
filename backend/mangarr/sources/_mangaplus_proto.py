"""Minimal decoder for the public MANGA Plus protobuf responses.

The web API publishes protobuf messages but does not publish a Python client.
Mangarr only needs a small, stable subset of those messages, so decoding that
subset locally avoids carrying generated bindings for the much larger schema.
Unknown fields and wire types are deliberately ignored for forward
compatibility.
"""

from collections.abc import Iterator


class ProtobufDecodeError(ValueError):
    pass


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ProtobufDecodeError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
    raise ProtobufDecodeError("varint is too long")


def _fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if not number:
            raise ProtobufDecodeError("invalid field number")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed64 field")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ProtobufDecodeError("truncated length-delimited field")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed32 field")
            value, offset = data[offset:end], end
        else:
            raise ProtobufDecodeError(f"unsupported wire type {wire_type}")
        yield number, wire_type, value


def _messages(data: bytes, field_number: int) -> Iterator[bytes]:
    for number, wire_type, value in _fields(data):
        if number == field_number and wire_type == 2:
            assert isinstance(value, bytes)
            yield value


def _message(data: bytes, field_number: int) -> bytes | None:
    return next(_messages(data, field_number), None)


def _integer(data: bytes, field_number: int, default: int = 0) -> int:
    for number, wire_type, value in _fields(data):
        if number == field_number and wire_type == 0:
            assert isinstance(value, int)
            return value
    return default


def _text(data: bytes, field_number: int, default: str = "") -> str:
    value = _message(data, field_number)
    if value is None:
        return default
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtobufDecodeError("invalid UTF-8 string") from exc


def _decode_title(data: bytes) -> dict:
    return {
        "titleId": _integer(data, 1),
        "name": _text(data, 2),
        "language": _integer(data, 7),
    }


def _decode_all_titles(data: bytes) -> dict:
    groups = []
    for group_data in _messages(data, 1):
        groups.append(
            {"titles": [_decode_title(item) for item in _messages(group_data, 2)]}
        )
    return {"AllTitlesGroup": groups}


def _decode_chapter(data: bytes) -> dict:
    return {
        "chapterId": _integer(data, 2),
        "name": _text(data, 3),
        "subTitle": _text(data, 4),
    }


def _decode_chapter_group(data: bytes) -> dict:
    return {
        "firstChapterList": [
            _decode_chapter(item) for item in _messages(data, 2)
        ],
        "midChapterList": [_decode_chapter(item) for item in _messages(data, 3)],
        "lastChapterList": [_decode_chapter(item) for item in _messages(data, 4)],
    }


def _decode_title_detail(data: bytes) -> dict:
    return {
        "chapterListGroup": [
            _decode_chapter_group(item) for item in _messages(data, 28)
        ]
    }


def _decode_manga_page(data: bytes) -> dict:
    return {"imageUrl": _text(data, 1), "encryptionKey": _text(data, 5)}


def _decode_page(data: bytes) -> dict:
    manga_page = _message(data, 1)
    return {"mangaPage": _decode_manga_page(manga_page)} if manga_page else {}


def _decode_viewer(data: bytes) -> dict:
    return {
        "pages": [_decode_page(item) for item in _messages(data, 1)],
        "vwToken": _text(data, 19),
    }


def _decode_error(data: bytes) -> str:
    popup = _message(data, 2)
    if popup:
        return _text(popup, 1) or _text(popup, 2) or "MangaPlus API error"
    return _text(data, 4) or "MangaPlus API error"


def decode_response(data: bytes) -> dict:
    """Decode the response subset consumed by :class:`MangaPlusSource`."""
    error = _message(data, 2)
    if error is not None:
        return {"error": _decode_error(error)}

    success = _message(data, 1)
    if success is None:
        raise ProtobufDecodeError("MangaPlus response has no result")

    result: dict = {}
    title_detail = _message(success, 8)
    manga_viewer = _message(success, 10)
    all_titles = _message(success, 25)
    if title_detail is not None:
        result["titleDetailView"] = _decode_title_detail(title_detail)
    if manga_viewer is not None:
        result["mangaViewer"] = _decode_viewer(manga_viewer)
    if all_titles is not None:
        result["allTitlesViewV2"] = _decode_all_titles(all_titles)
    return {"success": result}
