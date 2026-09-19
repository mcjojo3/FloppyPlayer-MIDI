"""Track info: audio tags and cover art, and readable MIDI text."""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def decode_midi_text(text: str) -> str:
    """Recover MIDI text mido decoded as latin-1 - often UTF-8 or Shift-JIS really."""
    if not text:
        return ""
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text.strip()
    try:
        return raw.decode("utf-8").strip("\x00 ").strip()
    except UnicodeDecodeError:
        pass
    try:
        decoded = raw.decode("cp932")
    except UnicodeDecodeError:
        return text.strip("\x00 ").strip()
    # Without double-byte characters it's likely latin-1 ("©" is katakana in cp932).
    if all(ord(ch) < 0x80 or 0xFF61 <= ord(ch) <= 0xFF9F for ch in decoded):
        return text.strip("\x00 ").strip()
    return decoded.strip("\x00 ").strip()


_CODECS = {"OggVorbis": "Ogg Vorbis", "OggOpus": "Opus", "OggFLAC": "Ogg FLAC", "WAVE": "WAV",
           "EasyMP3": "MP3", "EasyMP4": "AAC"}
TAG_KEYS = ("title", "artist", "album", "date", "tracknumber", "genre")


def audio_info(path: Path | None, data: bytes) -> dict:
    """Tags, stream details and cover art. Anything missing stays empty or 0."""
    info = {key: "" for key in TAG_KEYS}
    info.update({"duration": 0.0, "art": None, "codec": "", "bitrate": 0, "sample_rate": 0,
                 "channels": 0, "bits": 0})
    try:
        info["size"] = path.stat().st_size if path is not None else len(data)
    except OSError:
        info["size"] = 0
    try:
        import mutagen
    except ImportError:
        return info

    def source():
        return str(path) if path is not None else io.BytesIO(data)

    try:
        media = mutagen.File(source())
    except Exception as exc:
        log.debug("Could not read tags: %s", exc)
        return info
    if media is None:
        return info
    stream = media.info
    info["duration"] = float(getattr(stream, "length", 0.0) or 0.0)
    for key, attr in (("bitrate", "bitrate"), ("sample_rate", "sample_rate"),
                      ("channels", "channels"), ("bits", "bits_per_sample")):
        info[key] = int(getattr(stream, attr, 0) or 0)
    info["codec"] = _CODECS.get(type(media).__name__, type(media).__name__)

    try:
        easy = mutagen.File(source(), easy=True)
        tags = easy.tags if easy is not None else None
        for key in TAG_KEYS:
            value = tags.get(key) if tags else None
            if value:
                info[key] = str(value[0])
    except Exception as exc:
        log.debug("Could not read easy tags: %s", exc)

    info["art"] = _cover_art(media)
    return info


def _cover_art(media) -> bytes | None:
    try:
        tags = getattr(media, "tags", None)
        if tags is not None and hasattr(tags, "getall"):  # MP3 (ID3)
            frames = tags.getall("APIC")
            if frames:
                return bytes(frames[0].data)
        pictures = getattr(media, "pictures", None)  # FLAC
        if pictures:
            return bytes(pictures[0].data)
        if tags is not None and "covr" in tags:  # MP4
            return bytes(tags["covr"][0])
        if tags is not None and "metadata_block_picture" in tags:  # Ogg
            from mutagen.flac import Picture
            block = base64.b64decode(tags["metadata_block_picture"][0])
            return bytes(Picture(block).data)
    except Exception as exc:
        log.debug("No usable cover art: %s", exc)
    return None
