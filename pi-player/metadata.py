"""Track info: audio tags and cover art, and readable MIDI text."""

from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path

import lyrics

log = logging.getLogger(__name__)

_DB = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)")


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
    """Tags, stream details, cover art, lyrics and ReplayGain. Anything missing stays empty."""
    info = {key: "" for key in TAG_KEYS}
    info.update({"duration": 0.0, "art": None, "codec": "", "bitrate": 0, "sample_rate": 0,
                 "channels": 0, "bits": 0, "lyrics": None, "replaygain": None})
    try:
        info["size"] = path.stat().st_size if path is not None else len(data)
    except OSError:
        info["size"] = 0
    media = _open(path, data)
    info["lyrics"] = lyrics.find(path, media)  # a .lrc works even without readable tags
    if media is None:
        return info
    stream = media.info
    info["duration"] = float(getattr(stream, "length", 0.0) or 0.0)
    for key, attr in (("bitrate", "bitrate"), ("sample_rate", "sample_rate"),
                      ("channels", "channels"), ("bits", "bits_per_sample")):
        info[key] = int(getattr(stream, attr, 0) or 0)
    info["codec"] = _CODECS.get(type(media).__name__, type(media).__name__)

    try:
        import mutagen
        easy = mutagen.File(str(path) if path is not None else io.BytesIO(data), easy=True)
        tags = easy.tags if easy is not None else None
        for key in TAG_KEYS:
            value = tags.get(key) if tags else None
            if value:
                info[key] = str(value[0])
    except Exception as exc:
        log.debug("Could not read easy tags: %s", exc)

    info["art"] = _cover_art(media)
    info["replaygain"] = _replaygain(media)
    return info


def _open(path: Path | None, data: bytes):
    try:
        import mutagen
        return mutagen.File(str(path) if path is not None else io.BytesIO(data))
    except Exception as exc:  # ImportError included: tags are optional
        log.debug("Could not read tags: %s", exc)
        return None


def _db(text) -> float | None:
    match = _DB.match(str(text))
    return float(match.group(1)) if match else None


def _replaygain(media) -> tuple[float, float | None] | None:
    """(track gain dB, peak) from ReplayGain tags, if the file was tagged with them."""
    try:
        tags = getattr(media, "tags", None)
        if tags is None:
            return None
        if hasattr(tags, "getall"):  # ID3
            found = {f.desc.lower(): f.text[0] for f in tags.getall("TXXX") if f.text}
            gain, peak = found.get("replaygain_track_gain"), found.get("replaygain_track_peak")
            if gain is None:
                rva2 = next((f for f in tags.getall("RVA2") if f.desc.lower() == "track"), None)
                return (float(rva2.gain), float(rva2.peak) or None) if rva2 else None
        else:
            gain = (tags.get("replaygain_track_gain") or [None])[0]
            peak = (tags.get("replaygain_track_peak") or [None])[0]
        db = _db(gain) if gain is not None else None
        if db is None:
            return None
        return db, (_db(peak) if peak is not None else None) or None
    except Exception as exc:
        log.debug("Unreadable ReplayGain tags: %s", exc)
        return None


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
