from __future__ import annotations

import codecs
import re


HEX_SEPARATORS = re.compile(r"[\s,;:_-]+")


class CodecError(ValueError):
    """Raised when user input cannot be encoded or parsed."""


def normalize_encoding(name: str) -> str:
    """Return Python's canonical codec name, with a friendly error for users.

    Python already knows common legacy encodings such as gbk, gb2312 and ascii.
    We validate here so switching encodings fails before any serial write occurs.
    """

    try:
        return codecs.lookup(name).name
    except LookupError as exc:
        raise CodecError(f"unknown encoding: {name}") from exc


def decode_bytes(data: bytes, encoding: str) -> str:
    """Decode Rx bytes using the selected terminal encoding.

    Replacement mode is deliberate: serial streams may split multibyte
    characters or contain arbitrary binary bytes, and the terminal should keep
    running instead of crashing on a malformed sequence.
    """

    return data.decode(normalize_encoding(encoding), errors="replace")


class StreamDecoder:
    """Incrementally decode serial Rx bytes without corrupting split characters.

    Serial reads are byte streams, not text frames. UTF-8 and GBK characters can
    be split across two reads; an incremental decoder keeps the unfinished tail
    until the next chunk arrives instead of rendering U+FFFD too early.
    """

    def __init__(self, encoding: str) -> None:
        self._encoding = normalize_encoding(encoding)
        self._decoder = self._make_decoder(self._encoding)

    @property
    def encoding(self) -> str:
        return self._encoding

    def set_encoding(self, encoding: str) -> None:
        normalized = normalize_encoding(encoding)
        if normalized == self._encoding:
            return
        self._encoding = normalized
        self._decoder = self._make_decoder(normalized)

    def reset(self) -> None:
        self._decoder = self._make_decoder(self._encoding)

    def decode(self, data: bytes, final: bool = False) -> str:
        return self._decoder.decode(data, final=final)

    @staticmethod
    def _make_decoder(encoding: str):
        return codecs.getincrementaldecoder(encoding)(errors="replace")


def encode_text(text: str, encoding: str) -> bytes:
    try:
        return text.encode(normalize_encoding(encoding), errors="strict")
    except UnicodeEncodeError as exc:
        raise CodecError(f"text cannot be encoded as {encoding}: {exc}") from exc


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def parse_hex(text: str) -> bytes:
    """Parse HEX input in forms like 'AA BB', 'AA:BB', or 'AABB'.

    Separators are ignored. A final odd nibble is rejected because it would be
    ambiguous for hardware protocols where every byte must be explicit.
    """

    compact = "".join(part for part in HEX_SEPARATORS.split(text.strip()) if part)
    if compact.startswith("0x") or compact.startswith("0X"):
        compact = compact[2:]
    compact = compact.replace("0x", "").replace("0X", "")
    if not compact:
        return b""
    if len(compact) % 2:
        raise CodecError("HEX input has an odd number of digits")
    if not re.fullmatch(r"[0-9a-fA-F]+", compact):
        raise CodecError("HEX input may only contain 0-9, A-F and separators")
    return bytes.fromhex(compact)
