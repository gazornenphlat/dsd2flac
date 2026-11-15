"""Turn a DSF or DFF file into FLAC, the way the original dsf2flac program does.

Two modes, as in the original:
  * PCM (default): each output sample is the original's filter (tables in
    original_taps.py) summed over the DSD bits with a byte lookup table, then
    scaled, dithered, clipped and rounded.
  * DoP ("FLAC/DoP mode"): the DSD bits are packed into 24-bit samples, nothing is
    filtered, and the rate, bit depth and volume settings are not used.

The rules and the addresses they come from are in MECHANICS.md.
"""

from __future__ import annotations

import functools
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from dsd_reader import DsdFileError, DsdInfo, iter_channel_bytes, read_info
from original_taps import TAPS_88200, TAPS_176400, TAPS_352800

SAMPLE_RATES = (88200, 176400, 352800)
BIT_DEPTHS = (16, 20, 24)
# Filter by decimation factor (DSD rate / output rate), as DsdDecimator's constructor picks it.
FILTERS = {8: np.array(TAPS_352800), 16: np.array(TAPS_176400), 32: np.array(TAPS_88200)}
PHASE = 7               # output sample m is the filter at DSD bit 7 + factor*m
PREROLL_BITS = 32768    # 4096 bytes of idle DSD before the audio: the original's buffer
DOP_RATES = {2822400: 176400, 5644800: 352800}   # DSD rate -> DoP sample rate
DOP_LEAD_IN_FRAMES = 2048   # idle DoP frames before the audio


def supported_outputs(dsd_rate: int) -> tuple[list[int], bool]:
    """PCM output rates and DoP support for a DSD rate.

    A PCM combination works when the DSD rate divides by the output rate to a
    factor the original's filter provides (8, 16 or 32). DSD256 therefore
    converts, but only at 352.8 kHz; DSD128 refuses 88.2 kHz (factor 64).
    """
    pcm = [out for out in SAMPLE_RATES
           if dsd_rate % out == 0 and dsd_rate // out in FILTERS]
    return pcm, dsd_rate in DOP_RATES
CHUNK_BYTES = 1 << 16       # bytes per channel read at a time
IDLE_BYTE = 0x69            # the DSD idle pattern: the history before the start is this, not zero

# ID3 frames copied to FLAC tags: the original's id3v2_to_flac (artist, album, title, track, year).
TAG_MAP = {
    "TPE1": "ARTIST", "TALB": "ALBUM", "TIT2": "TITLE", "TRCK": "TRACKNUMBER",
    "TYER": "DATE", "TDRC": "DATE",
}


@dataclass(frozen=True)
class Settings:
    sample_rate: int = 88200
    bit_depth: int = 24
    scale_db: float = 4.0
    dither: bool = True
    dop: bool = False
    out_dir: Optional[str] = None   # None: save beside each source file


class Cancelled(Exception):
    """The person pressed Stop."""


@dataclass
class Result:
    output: str
    clipped_samples: int = 0
    warning: Optional[str] = None


def unique_output_path(source: str, out_dir: Optional[str] = None) -> str:
    """The FLAC name beside the source (or in out_dir). The original overwrites; this never does."""
    base = os.path.splitext(source)[0]
    if out_dir:
        base = os.path.join(out_dir, os.path.splitext(os.path.basename(source))[0])
    candidate = base + ".flac"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{base} ({n}).flac"
        n += 1
    return candidate


def _pick_filter(dsd_rate: int, out_rate: int):
    """Return (taps, decimation factor) or raise like the original does."""
    if dsd_rate % out_rate or dsd_rate // out_rate not in FILTERS:
        raise DsdFileError("Sorry, incompatible sample rate combination")
    factor = dsd_rate // out_rate
    return FILTERS[factor], factor


@functools.lru_cache(maxsize=None)
def _lookup_table(factor: int, lsb_first: bool) -> np.ndarray:
    """Per 8-tap lane, the sum for each byte value (the original's initLookupTable).

    Coefficient 8*lane + j is paired with time bit 7 - j of the byte: +coef for a 1 bit,
    -coef for a 0 bit, summed in order j = 0, 1, ...
    """
    taps = FILTERS[factor]
    lanes = (len(taps) + 7) // 8
    table = np.zeros((lanes, 256))
    for lane in range(lanes):
        count = min(8, len(taps) - 8 * lane)
        for value in range(256):
            total = 0.0
            for j in range(count):
                p = 7 - j                                   # time offset inside the byte
                bit = (value >> p) & 1 if lsb_first else (value >> j) & 1
                total += float(taps[8 * lane + j]) * (1.0 if bit else -1.0)
            table[lane, value] = total
    return table


class _Decimator:
    """Streams raw DSD bytes and returns output samples m = 0, 1, 2 ... as they complete.

    Output m sums lane l's table entry for byte factor/8*m - l, lanes in order 0..L-1.
    Bytes before the start are the idle byte. Chunk boundaries do not change the result.
    """

    def __init__(self, factor: int, lsb_first: bool, channels: int):
        self.table = _lookup_table(factor, lsb_first)
        self.lanes = self.table.shape[0]
        self.step = factor // 8
        self.base = -(self.lanes - 1)                 # byte index of buf[:, 0]
        self.buf = np.full((channels, self.lanes - 1), IDLE_BYTE, np.intp)
        self.next_m = 0

    def push(self, raw: np.ndarray) -> np.ndarray:
        """raw: (channels, k) uint8 bytes. Returns (channels, count) sums that are now complete."""
        self.buf = np.concatenate([self.buf, raw.astype(np.intp)], axis=1)
        end = self.base + self.buf.shape[1]           # bytes known: indices below end
        last_m = (end - 1) // self.step
        if last_m < self.next_m:
            return np.zeros((self.buf.shape[0], 0))
        ms = np.arange(self.next_m, last_m + 1)
        acc = np.zeros((self.buf.shape[0], len(ms)))
        for lane in range(self.lanes):
            values = self.buf[:, self.step * ms - lane - self.base]
            acc += self.table[lane][values]
        keep_from = self.step * (last_m + 1) - (self.lanes - 1)
        self.buf = self.buf[:, keep_from - self.base:]
        self.base = keep_from
        self.next_m = last_m + 1
        return acc


def _quantize(scaled: np.ndarray, bit_depth: int, dither: bool, rng: np.random.Generator):
    """Round scaled samples (already times 2^(bits-1) and the gain) to bit_depth.

    Same order as the original: add dither, clip to +-(2^(bits-1)-1), round half away from zero.
    Returns int32 values at 32-bit scale (what soundfile expects) and the clip count.
    """
    limit = 2 ** (bit_depth - 1) - 1
    v = scaled
    if dither:  # triangular dither, 1 LSB peak: the original's rand() pair
        v = v + (rng.random(v.shape) - rng.random(v.shape))
    clipped = int(np.count_nonzero(np.abs(v) > limit))
    v = np.clip(v, -limit, limit)
    q = (np.sign(v) * np.floor(np.abs(v) + 0.5)).astype(np.int64)
    return (q << (32 - bit_depth)).astype(np.int32), clipped


def _subtype(bit_depth: int) -> str:
    # FLAC here holds 16 or 24 bits; 20-bit output is written as 24-bit with the low bits empty.
    return "PCM_16" if bit_depth == 16 else "PCM_24"


def convert_file(
    source: str,
    settings: Settings,
    progress: Callable[[float], None] = lambda fraction: None,
    cancel: Optional[threading.Event] = None,
    rng: Optional[np.random.Generator] = None,
    chunk_bytes: int = CHUNK_BYTES,
) -> Result:
    """Convert one DSF/DFF file to a FLAC beside it. Raises DsdFileError or Cancelled."""
    if chunk_bytes % 2:
        raise ValueError("chunk_bytes must be even")
    info = read_info(source)
    rng = rng or np.random.default_rng()
    out_path = unique_output_path(source, settings.out_dir)

    if settings.dop:
        if info.dsd_rate not in DOP_RATES:
            raise DsdFileError("ERROR: sample rate not supported by DoP")
        rate = DOP_RATES[info.dsd_rate]
        subtype = "PCM_24"
    else:
        _pick_filter(info.dsd_rate, settings.sample_rate)  # raises for unsupported rates
        rate = settings.sample_rate
        subtype = _subtype(settings.bit_depth)

    clipped = 0
    created = False
    try:
        with sf.SoundFile(out_path, "w", samplerate=rate, channels=info.channels,
                          format="FLAC", subtype=subtype) as out:
            created = True
            if settings.dop:
                _write_dop(info, out, progress, cancel, chunk_bytes)
            else:
                clipped = _write_pcm(info, out, settings, progress, cancel, rng, chunk_bytes)
    except BaseException:
        if created and os.path.exists(out_path):
            os.remove(out_path)  # remove our own partial file
        raise
    warning = _copy_tags(source, out_path)
    progress(1.0)
    return Result(output=out_path, clipped_samples=clipped, warning=warning)


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _idle_byte(info: DsdInfo) -> int:
    """The idle DSD byte 0x69 as the original writes it: bit-reversed for LSB-first files."""
    return 0x96 if info.lsb_first else 0x69


def _write_pcm(info, out, settings, progress, cancel, rng, chunk_bytes) -> int:
    taps, factor = _pick_filter(info.dsd_rate, settings.sample_rate)
    n = len(taps)
    total_bits = info.bits_per_channel
    length = max(0, (total_bits - n) // factor + 1)     # samples the original writes
    preroll = min(length, max(0, (PREROLL_BITS - n) // factor))  # leading zeros
    audio = length - preroll
    gain = 10 ** (settings.scale_db / 20) * 2 ** (settings.bit_depth - 1)
    decimator = _Decimator(factor, info.lsb_first, info.channels)
    written = 0
    clipped = 0
    bytes_done = 0

    out.write(np.zeros((preroll, info.channels), np.int32))

    for chunk in iter_channel_bytes(info, chunk_bytes):
        _check_cancel(cancel)
        bytes_done += chunk.shape[1]
        y = decimator.push(chunk)
        keep = min(y.shape[1], audio - written)
        if keep > 0:
            block, n_clip = _quantize(y[:, :keep] * gain, settings.bit_depth, settings.dither, rng)
            out.write(block.T)
            written += keep
            clipped += n_clip
        progress(min(bytes_done * 8, total_bits) / total_bits * 0.98)
    if written < audio:  # cannot happen for whole files; keep the length exact anyway
        out.write(np.zeros((audio - written, info.channels), np.int32))
    _check_cancel(cancel)
    return clipped


def _dop_words(dop: np.ndarray, frame_index: int):
    """Pack (channels, 2n) DSD bytes into DoP frames. Returns (int32 (frames, channels), frames).

    Word = marker << 16 | byte_1 << 8 | byte_2. The marker is 0x05 on the first frame and
    alternates to 0xFA (odd_marker 0x050000, even_marker 0xFFFA0000 in the original).
    """
    hi = dop[:, 0::2].astype(np.int64)
    lo = dop[:, 1::2].astype(np.int64)
    frames = hi.shape[1]
    markers = np.where((frame_index + np.arange(frames)) % 2 == 0, 0x05, 0xFA)
    value = (markers[None, :] << 16) | (hi << 8) | lo              # 24-bit pattern
    value = np.where(value >= 1 << 23, value - (1 << 24), value)  # signed
    return (value << 8).astype(np.int32).T, frames               # soundfile wants 32-bit scale


def _write_dop(info, out, progress, cancel, chunk_bytes) -> None:
    order = "little" if info.lsb_first else "big"
    idle = _idle_byte(info)
    lead_in = np.full((info.channels, 2 * DOP_LEAD_IN_FRAMES), idle, np.uint8)
    block, frame_index = _dop_words(lead_in, 0)
    out.write(block)
    bits_done = 0
    for chunk in iter_channel_bytes(info, chunk_bytes):
        _check_cancel(cancel)
        bits = np.unpackbits(chunk, axis=1, bitorder=order)
        bits = bits[:, : info.bits_per_channel - bits_done]
        bits_done += bits.shape[1]
        # DoP carries the bits most-significant first, two bytes per output sample.
        dop = np.packbits(bits, axis=1)
        if dop.shape[1] % 2:
            dop = np.concatenate([dop, np.full((info.channels, 1), idle, np.uint8)], axis=1)
        block, frames = _dop_words(dop, frame_index)
        out.write(block)
        frame_index += frames
        progress(bits_done / info.bits_per_channel * 0.98)


def _copy_tags(source: str, flac_path: str) -> Optional[str]:
    """Copy the ID3 artist, album, title, track and year into the FLAC.

    Returns a warning for the log if tags could not be copied; the audio is kept either way.
    """
    try:
        import mutagen
        from mutagen.flac import FLAC
        audio = mutagen.File(source)
        tags = getattr(audio, "tags", None)
        if not tags:
            return None
        found = {}
        for key in tags.keys():
            frame_id = key.split(":")[0]
            if frame_id in TAG_MAP:
                found.setdefault(TAG_MAP[frame_id], [str(v) for v in tags[key].text])
        if not found:
            return None
        flac = FLAC(flac_path)
        for name, values in found.items():
            flac[name] = values
        flac.save()
        return None
    except Exception as err:
        return f"Tags were not copied: {err}"
