"""Does a media file carry an audio stream?

Importing a video has to decide, in one step, whether a linked audio clip belongs on the
timeline next to it. Decoding the file to find out would be slow and asynchronous, so this
module reads only the container headers:

* ISO-BMFF (mp4, m4v, mov, m4a, 3gp) - walks the box tree of ``moov`` and looks for a
  ``soun`` handler.
* Matroska / WebM - walks the EBML tree down to ``Tracks`` and reads every ``TrackType``.
* AVI - RIFF chunks: a ``strl`` whose ``strh`` says ``auds``.
* FLV - the header flags carry a "has audio" bit.
* ASF / WMV - the audio media-type GUID inside the header object.
* MPEG program stream (.mpg) - an audio stream id start code.

Anything else (and any file that cannot be parsed) reports no audio, which is the safe
answer: the clip is still imported, it just does not grow a silent partner.

The whole module is pure Python and never raises; it is meant to run on the import worker
thread, so a stall would only delay one import.
"""

import os
import struct

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
ISO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".m4a", ".3gp", ".3g2"}
MATROSKA_EXTENSIONS = {".mkv", ".webm"}
AVI_EXTENSIONS = {".avi"}
FLV_EXTENSIONS = {".flv"}
ASF_EXTENSIONS = {".wmv", ".asf", ".wma"}
MPEG_EXTENSIONS = {".mpg", ".mpeg", ".vob"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

# Containers that hold other boxes; anything else is a leaf and is skipped.
_BMFF_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf"}
# ASF audio media type GUID (MEDIATYPE_Audio) in little-endian byte order.
_ASF_AUDIO_GUID = bytes.fromhex("409e69f84d5bcf11a8fd00805f5c442b")
_MAX_HEADER = 8 * 1024 * 1024     # never read more than this while hunting a header


def _read(path, count, offset=0):
    try:
        with open(path, "rb") as handle:
            if offset:
                handle.seek(offset)
            return handle.read(count)
    except OSError:
        return b""


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --------------------------------------------------------------------- ISO-BMFF
def _bmff_boxes(data, start, end):
    """(type, payload_start, payload_end) for the boxes in data[start:end]."""
    position = start
    while position + 8 <= end:
        size = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        header = 8
        if size == 1:
            if position + 16 > end:
                return
            size = struct.unpack(">Q", data[position + 8:position + 16])[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            return
        yield kind, position + header, position + size
        position += size


def _bmff_has_audio(path, size):
    read = min(size, _MAX_HEADER)
    data = _read(path, read)
    if len(data) < 8:
        return False
    queue = list(_bmff_boxes(data, 0, len(data)))
    seen = 0
    while queue and seen < 4096:
        seen += 1
        kind, start, end = queue.pop()
        if kind == b"hdlr":
            # version/flags (4) + predefined (4) + handler_type (4)
            if end - start >= 12 and data[start + 8:start + 12] == b"soun":
                return True
        elif kind in _BMFF_CONTAINERS:
            queue.extend(_bmff_boxes(data, start, end))
    return False


# --------------------------------------------------------------- Matroska/WebM
def _ebml_vint(data, position, keep_marker):
    """(value, length) of an EBML variable-size integer, or (None, 0)."""
    if position >= len(data):
        return None, 0
    first = data[position]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or position + length > len(data):
        return None, 0
    value = first if keep_marker else first & (mask - 1)
    for index in range(1, length):
        value = (value << 8) | data[position + index]
    return value, length


def _matroska_has_audio(path, size):
    data = _read(path, min(size, _MAX_HEADER))
    if len(data) < 8:
        return False
    position = 0
    while position < len(data) - 2:
        element_id, id_len = _ebml_vint(data, position, True)
        if not element_id:
            break
        element_size, size_len = _ebml_vint(data, position + id_len, False)
        if element_size is None:
            break
        body = position + id_len + size_len
        end = min(len(data), body + element_size)
        if element_id == 0x18538067:            # Segment: descend
            position = body
            continue
        if element_id == 0x1654AE6B:            # Tracks: scan TrackEntry payloads
            return _matroska_tracks_have_audio(data[body:end])
        position = end if element_size else end + 1
    return False


def _matroska_tracks_have_audio(payload):
    """True when any TrackEntry in a Tracks element declares TrackType 2 (audio)."""
    position = 0
    while position < len(payload) - 2:
        element_id, id_len = _ebml_vint(payload, position, True)
        element_size, size_len = _ebml_vint(payload, position + id_len, False)
        if element_id is None or element_size is None:
            return False
        body = position + id_len + size_len
        end = min(len(payload), body + element_size)
        if element_id == 0xAE:                  # TrackEntry
            value = _matroska_track_type(payload[body:end])
            if value == 2:
                return True
            position = end
            continue
        position = end if element_size else body
    return False


def _matroska_track_type(entry):
    position = 0
    while position < len(entry) - 2:
        element_id, id_len = _ebml_vint(entry, position, True)
        element_size, size_len = _ebml_vint(entry, position + id_len, False)
        if element_id is None or element_size is None:
            return None
        body = position + id_len + size_len
        if element_id == 0x83:                  # TrackType
            value = 0
            for index in range(body, min(len(entry), body + element_size)):
                value = (value << 8) | entry[index]
            return value
        position = body + element_size
    return None


# --------------------------------------------------------------------------- AVI
def _avi_has_audio(path, size):
    data = _read(path, min(size, _MAX_HEADER))
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        return False
    return _avi_streams_audio(data[12:])


def _avi_streams_audio(data):
    """True when any 'strl' list in this chunk stream describes an audio stream."""
    position = 0
    while position + 8 <= len(data):
        tag = data[position:position + 4]
        length = struct.unpack("<I", data[position + 4:position + 8])[0]
        body = position + 8
        if length < 0:
            return False
        kind = data[body:body + 4]
        if tag == b"LIST" and kind == b"strl":
            inner = data[body + 4:body + 4 + length]
            offset = 0
            while offset + 8 <= len(inner):
                sub = inner[offset:offset + 4]
                sub_length = struct.unpack("<I", inner[offset + 4:offset + 8])[0]
                if sub == b"strh" and sub_length >= 4:
                    if inner[offset + 8:offset + 12] == b"auds":
                        return True
                    break
                if sub_length <= 0:
                    break
                offset += 8 + sub_length + (sub_length % 2)
        elif tag == b"LIST" and kind == b"hdrl":
            if _avi_streams_audio(data[body + 4:body + 4 + length]):
                return True
        if length <= 0:
            return False
        position = body + length + (length % 2)
    return False


# --------------------------------------------------------------- small formats
def _flv_has_audio(path):
    head = _read(path, 9)
    return len(head) >= 6 and head[:3] == b"FLV" and bool(head[4] & 0x04)


def _asf_has_audio(path, size):
    head = _read(path, min(size, 256 * 1024))
    return _ASF_AUDIO_GUID in head


def _mpeg_has_audio(path, size):
    head = _read(path, min(size, 512 * 1024))
    index = head.find(b"\x00\x00\x01")
    while index >= 0 and index + 3 < len(head):
        code = head[index + 3]
        if 0xC0 <= code <= 0xDF or code == 0xBD:
            return True
        index = head.find(b"\x00\x00\x01", index + 3)
    return False


# ------------------------------------------------------------------------ API
def has_audio_stream(path, cache=None):
    """True when `path` carries audio that the mixer could decode.

    Audio-only extensions are always True, still images always False, everything else is
    decided from the container header. `cache` (a dict) memoises answers per path.
    """
    if not path:
        return False
    key = os.path.normcase(os.path.abspath(path))
    if cache is not None and key in cache:
        return cache[key]
    result = _detect(path)
    if cache is not None:
        cache[key] = result
    return result


def _detect(path):
    extension = os.path.splitext(path)[1].lower()
    if extension in IMAGE_EXTENSIONS:
        return False
    if extension in AUDIO_EXTENSIONS:
        return True
    if not os.path.isfile(path):
        return False
    size = _file_size(path)
    if not size:
        return False
    try:
        if extension in ISO_EXTENSIONS:
            return _bmff_has_audio(path, size)
        if extension in MATROSKA_EXTENSIONS:
            return _matroska_has_audio(path, size)
        if extension in AVI_EXTENSIONS:
            return _avi_has_audio(path, size)
        if extension in FLV_EXTENSIONS:
            return _flv_has_audio(path)
        if extension in ASF_EXTENSIONS:
            return _asf_has_audio(path, size)
        if extension in MPEG_EXTENSIONS:
            return _mpeg_has_audio(path, size)
    except (OSError, struct.error, ValueError, IndexError):
        return False
    return False
