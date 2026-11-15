"""Read DSF and DSDIFF (.dff) files: format facts and the raw 1-bit audio.

Only uncompressed DSD is read. DST-compressed DSDIFF files are refused.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

import numpy as np


class DsdFileError(Exception):
    """The file is not a DSF/DSDIFF file this program can convert."""


@dataclass(frozen=True)
class DsdInfo:
    path: str
    kind: str              # "DSF" or "DFF"
    dsd_rate: int          # 1-bit samples per second per channel, e.g. 2822400
    channels: int
    bits_per_channel: int  # number of 1-bit samples in each channel
    data_offset: int       # where the audio bytes start in the file
    data_size: int         # bytes of audio, all channels together (DFF) or padded (DSF)
    block_size: int        # DSF: bytes per channel per block; DFF: 1
    lsb_first: bool        # DSF bits are least-significant first; DFF most-significant first


def read_info(path: str) -> DsdInfo:
    with open(path, "rb") as f:
        magic = f.read(4)
        f.seek(0)
        try:
            if magic == b"DSD ":
                info = _read_dsf_info(f, path)
            elif magic == b"FRM8":
                info = _read_dff_info(f, path)
            else:
                raise DsdFileError("This is not a DSF or DFF file.")
        except struct.error:  # a header ends in the middle of a field
            raise DsdFileError("The file is damaged or cut short.")
        f.seek(0, 2)
        file_size = f.tell()
    if info.data_offset + info.data_size > file_size:
        raise DsdFileError("The file is cut short: it ends before its audio does.")
    return info


def _read_dsf_info(f, path: str) -> DsdInfo:
    head = f.read(28)  # 'DSD ', chunk size, file size, metadata pointer
    if len(head) < 28:
        raise DsdFileError("The DSF file is cut short.")
    fmt = f.read(52)
    if fmt[:4] != b"fmt ":
        raise DsdFileError("The DSF file has no format block.")
    # Fields after the 12-byte chunk header: version, format id, channel type,
    # channel count, sample rate, bits per sample, sample count (8 bytes),
    # block size per channel, reserved.
    (_version, format_id, _chan_type, channels, rate, bits, sample_count,
     block_size, _reserved) = struct.unpack("<6IQ2I", fmt[12:52])
    if format_id != 0:
        raise DsdFileError("The DSF file is not raw DSD audio.")
    if bits not in (1, 8):
        raise DsdFileError("The DSF format block has an unsupported bits-per-sample value.")
    if channels < 1 or rate <= 0 or block_size <= 0 or sample_count <= 0:
        raise DsdFileError("The DSF format block has impossible values.")
    dhead = f.read(12)
    if dhead[:4] != b"data":
        raise DsdFileError("The DSF file has no audio data block.")
    (data_size,) = struct.unpack("<Q", dhead[4:12])
    return DsdInfo(
        path=path, kind="DSF", dsd_rate=rate, channels=channels,
        bits_per_channel=sample_count, data_offset=f.tell(),
        data_size=data_size - 12, block_size=block_size,
        lsb_first=(bits != 8),
    )


def _read_dff_info(f, path: str) -> DsdInfo:
    head = f.read(16)  # 'FRM8', 8-byte big-endian size, form type
    if len(head) < 16 or head[12:16] != b"DSD ":
        raise DsdFileError("The DFF file has an unknown form type.")
    (size,) = struct.unpack(">Q", head[4:12])
    end = 12 + size
    rate = channels = compression = None
    data_offset = data_size = None
    pos = 16
    while pos + 12 <= end:
        f.seek(pos)
        cid = f.read(4)
        (csize,) = struct.unpack(">Q", f.read(8))
        body = pos + 12
        if body + csize > end:
            raise DsdFileError("The DFF file has a chunk that runs past the end of the file.")
        if cid == b"PROP":
            f.seek(body)
            rate, channels, compression = _parse_prop(f.read(csize))
        elif cid == b"DSD ":
            data_offset, data_size = body, csize
        elif cid == b"DST ":
            raise DsdFileError("This DFF file is DST-compressed, which this version cannot convert.")
        pos = body + csize + (csize & 1)
    if rate is None or channels is None or data_offset is None:
        raise DsdFileError("The DFF file is missing its properties or audio.")
    if rate <= 0 or channels < 1 or data_size <= 0:
        raise DsdFileError("The DFF properties or audio have impossible values.")
    bits_per_channel = (data_size // channels) * 8
    if bits_per_channel <= 0:
        raise DsdFileError("The DFF file has no audio samples.")
    if compression not in (None, b"DSD "):
        raise DsdFileError("This DFF file is DST-compressed, which this version cannot convert.")
    return DsdInfo(
        path=path, kind="DFF", dsd_rate=rate, channels=channels,
        bits_per_channel=bits_per_channel, data_offset=data_offset,
        data_size=data_size, block_size=1, lsb_first=False,
    )


def _parse_prop(body: bytes):
    """Return (sample rate, channel count, compression id) from a DFF PROP chunk."""
    if body[:4] != b"SND ":
        raise DsdFileError("The DFF properties block is not a sound block.")
    rate = channels = compression = None
    p = 4
    while p + 12 <= len(body):
        sid = body[p:p + 4]
        (ssize,) = struct.unpack(">Q", body[p + 4:p + 12])
        sbody = body[p + 12:p + 12 + ssize]
        if sid == b"FS  ":
            (rate,) = struct.unpack(">I", sbody[:4])
        elif sid == b"CHNL":
            (channels,) = struct.unpack(">H", sbody[:2])
        elif sid == b"CMPR":
            compression = sbody[:4]
        p += 12 + ssize + (ssize & 1)
    return rate, channels, compression


def iter_channel_bytes(info: DsdInfo, chunk_bytes: int = 1 << 16) -> Iterator[np.ndarray]:
    """Yield the audio as uint8 arrays shaped (channels, n), each channel in time order.

    Bits are still packed eight to a byte; use info.lsb_first to unpack them.
    The final chunk may hold a few bytes past the real end (padding in DSF).
    """
    channels = info.channels
    bytes_per_channel = (info.bits_per_channel + 7) // 8
    with open(info.path, "rb") as f:
        f.seek(info.data_offset)
        done = 0
        if info.kind == "DSF":
            block = info.block_size
            rows_per_read = max(1, chunk_bytes // block)
            total_rows = (bytes_per_channel + block - 1) // block
            rows_done = 0
            while rows_done < total_rows:
                rows = min(rows_per_read, total_rows - rows_done)
                raw = f.read(rows * block * channels)
                raw = raw.ljust(rows * block * channels, b"\x00")  # cut-short file: pad
                arr = np.frombuffer(raw, dtype=np.uint8).reshape(rows, channels, block)
                yield np.ascontiguousarray(arr.transpose(1, 0, 2).reshape(channels, rows * block))
                rows_done += rows
        else:
            per_read = max(1, chunk_bytes)
            while done < bytes_per_channel:
                n = min(per_read, bytes_per_channel - done)
                raw = f.read(n * channels).ljust(n * channels, b"\x00")
                arr = np.frombuffer(raw, dtype=np.uint8).reshape(n, channels).T
                yield np.ascontiguousarray(arr)
                done += n
