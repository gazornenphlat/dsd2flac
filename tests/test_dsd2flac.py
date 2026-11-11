"""Self-checks: write small DSF/DFF files, convert them, and check the results.

Run from the app folder:  python -m unittest discover -s tests
"""

import os
import shutil
import struct
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from converter import (DOP_LEAD_IN_FRAMES, FILTERS, PHASE, PREROLL_BITS, Settings,  # noqa: E402
                       _pick_filter, convert_file, unique_output_path)
from dsd_reader import DsdFileError, iter_channel_bytes, read_info  # noqa: E402

DSD64 = 2822400


def sigma_delta(signal: np.ndarray) -> np.ndarray:
    """Second-order 1-bit modulator. Input in [-0.5, 0.5]; returns 0/1 bits."""
    v1 = v2 = 0.0
    y = 1.0
    out = np.empty(len(signal), np.uint8)
    for n, s in enumerate(signal):
        v1 += s - y
        v2 += v1 - y
        y = 1.0 if v2 >= 0 else -1.0
        out[n] = 1 if y > 0 else 0
    return out


def tone_bits(seconds: float, freq: float, channels: int = 2) -> np.ndarray:
    n = int(DSD64 * seconds)
    t = np.arange(n) / DSD64
    bits = [sigma_delta(0.5 * np.sin(2 * np.pi * freq * t)) for _ in range(channels)]
    return np.stack(bits)


def write_dsf(path: str, bits: np.ndarray, rate: int = DSD64) -> None:
    """DSF: bits least-significant first, channel blocks of 4096 bytes."""
    channels, n = bits.shape
    block = 4096
    per_channel = np.stack([np.packbits(b, bitorder="little") for b in bits])
    rows = -(-per_channel.shape[1] // block)
    padded = np.zeros((channels, rows * block), np.uint8)
    padded[:, : per_channel.shape[1]] = per_channel
    data = padded.reshape(channels, rows, block).transpose(1, 0, 2).tobytes()
    fmt = struct.pack("<6IQ2I", 1, 0, 2, channels, rate, 1, n, block, 0)
    data_chunk = b"data" + struct.pack("<Q", 12 + len(data)) + data
    head = b"DSD " + struct.pack("<QQQ", 28, 28 + 52 + len(data_chunk), 0)
    with open(path, "wb") as f:
        f.write(head + b"fmt " + struct.pack("<Q", 52) + fmt + data_chunk)


def write_dff(path: str, bits: np.ndarray, rate: int = DSD64, compression: bytes = b"DSD ") -> None:
    """DSDIFF: bits most-significant first, bytes interleaved across channels."""
    channels, _ = bits.shape
    payload = np.stack([np.packbits(b) for b in bits]).T.tobytes()
    ids = b"".join([b"SLFT", b"SRGT"][:channels])
    chnl = struct.pack(">H", channels) + ids
    snd = b"SND "
    snd += b"FS  " + struct.pack(">Q", 4) + struct.pack(">I", rate)
    snd += b"CHNL" + struct.pack(">Q", len(chnl)) + chnl
    snd += b"CMPR" + struct.pack(">Q", 4) + compression
    props = b"PROP" + struct.pack(">Q", len(snd)) + snd
    dsd = b"DSD " + struct.pack(">Q", len(payload)) + payload
    dsd += b"\x00" if len(payload) % 2 else b""
    body = b"DSD " + props + dsd
    with open(path, "wb") as f:
        f.write(b"FRM8" + struct.pack(">Q", len(body)) + body)


class DsdConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bits = tone_bits(0.25, 1000.0, channels=2)
        cls.dsf = os.path.join(cls.tmp.name, "tone.dsf")
        cls.dff = os.path.join(cls.tmp.name, "tone.dff")
        write_dsf(cls.dsf, cls.bits)
        write_dff(cls.dff, cls.bits)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _out_copy(self, name):
        return os.path.join(self.tmp.name, name)

    def test_headers_read_back(self):
        dsf = read_info(self.dsf)
        dff = read_info(self.dff)
        self.assertEqual((dsf.kind, dsf.dsd_rate, dsf.channels), ("DSF", DSD64, 2))
        self.assertEqual((dff.kind, dff.dsd_rate, dff.channels), ("DFF", DSD64, 2))
        self.assertEqual(dsf.bits_per_channel, self.bits.shape[1])
        self.assertEqual(dff.bits_per_channel, self.bits.shape[1])

    def test_dsf_and_dff_hold_the_same_bits(self):
        def bits_of(info):
            parts = []
            for chunk in iter_channel_bytes(info, 4096):
                parts.append(np.unpackbits(chunk, axis=1,
                                           bitorder="little" if info.lsb_first else "big"))
            return np.concatenate(parts, axis=1)[:, : info.bits_per_channel]

        np.testing.assert_array_equal(bits_of(read_info(self.dsf)), self.bits)
        np.testing.assert_array_equal(bits_of(read_info(self.dff)), self.bits)

    def test_pcm_output_has_the_tone_at_the_right_level(self):
        out = self._out_copy("pcm.dsf")
        shutil.copy(self.dsf, out)
        result = convert_file(out, Settings(sample_rate=88200, bit_depth=24,
                                            scale_db=0.0, dither=False))
        data, rate = sf.read(result.output, dtype="float64")
        self.assertEqual(rate, 88200)
        self.assertEqual(data.shape[1], 2)
        self.assertEqual(sf.info(result.output).subtype, "PCM_24")
        mid = data[2000:-2000, 0]
        spectrum = np.abs(np.fft.rfft(mid * np.hanning(len(mid))))
        freqs = np.fft.rfftfreq(len(mid), 1 / rate)
        self.assertLess(abs(freqs[np.argmax(spectrum)] - 1000.0), 10.0)
        rms = np.sqrt(np.mean(mid ** 2))
        self.assertAlmostEqual(rms * np.sqrt(2), 0.5, delta=0.03)

    def test_streaming_in_small_pieces_gives_the_same_audio(self):
        small = convert_file(self.dff, Settings(dither=False), chunk_bytes=16,
                             rng=np.random.default_rng(1))
        whole = convert_file(self.dff, Settings(dither=False), chunk_bytes=1 << 16,
                             rng=np.random.default_rng(1))
        a, _ = sf.read(small.output, dtype="int32")
        b, _ = sf.read(whole.output, dtype="int32")
        self.assertEqual(a.shape, b.shape)
        np.testing.assert_array_equal(a, b)
        os.remove(small.output)
        os.remove(whole.output)

    def test_dop_keeps_every_dsd_bit(self):
        result = convert_file(self.dff, Settings(dop=True))
        raw, rate = sf.read(result.output, dtype="int32")
        self.assertEqual(rate, DSD64 // 16)
        # Left channel: each frame is a 24-bit word = marker byte, then two DSD bytes.
        word = (raw[:, 0].astype(np.int64) >> 8) & 0xFFFFFF
        markers = word >> 16
        self.assertTrue(set(np.unique(markers)) <= {0x05, 0xFA})
        self.assertTrue(np.all(markers[::2] == 0x05))
        self.assertTrue(np.all(markers[1::2] == 0xFA))
        lead = DOP_LEAD_IN_FRAMES
        self.assertTrue(np.all((word[:lead] & 0xFFFF) == 0x6969))  # DFF: idle 0x69 unreversed
        audio = word[lead:]
        dsd_bytes = np.stack([(audio >> 8) & 0xFF, audio & 0xFF], axis=1).reshape(-1)
        left = np.unpackbits(dsd_bytes.astype(np.uint8))   # DoP is most-significant first
        n = min(left.size, self.bits.shape[1])
        np.testing.assert_array_equal(left[:n], self.bits[0, :n])
        os.remove(result.output)

    def test_sixteen_bit_output_is_16_bit_flac(self):
        result = convert_file(self.dsf, Settings(sample_rate=176400, bit_depth=16))
        self.assertEqual(sf.info(result.output).subtype, "PCM_16")
        self.assertEqual(sf.info(result.output).samplerate, 176400)
        os.remove(result.output)

    def test_dst_compressed_dff_is_refused(self):
        path = self._out_copy("dst.dff")
        write_dff(path, self.bits[:, :800], compression=b"DST ")
        with self.assertRaises(DsdFileError):
            read_info(path)

    def test_output_goes_to_the_chosen_folder(self):
        import tempfile as _tf
        from converter import unique_output_path as _uop
        with _tf.TemporaryDirectory() as d:
            src = os.path.join(d, "song.dsf")
            open(src, "wb").close()
            out_dir = os.path.join(d, "out")
            os.mkdir(out_dir)
            self.assertEqual(_uop(src, out_dir), os.path.join(out_dir, "song.flac"))
            open(os.path.join(out_dir, "song.flac"), "wb").close()
            self.assertEqual(_uop(src, out_dir), os.path.join(out_dir, "song (2).flac"))

    def test_bad_sample_rate_is_refused(self):
        with self.assertRaises(DsdFileError):
            convert_file(self.dsf, Settings(sample_rate=96000))  # 2822400 is not a multiple

    def test_output_never_overwrites_an_existing_file(self):
        base = self._out_copy("keep.dsf")
        open(self._out_copy("keep.flac"), "wb").close()
        self.assertEqual(unique_output_path(base), self._out_copy("keep (2).flac"))

    def test_filter_tables_are_the_originals_sizes(self):
        self.assertEqual({k: len(v) for k, v in FILTERS.items()}, {8: 96, 16: 240, 32: 575})
        taps, factor = _pick_filter(DSD64, 88200)
        self.assertEqual(factor, 32)
        with self.assertRaises(DsdFileError):
            _pick_filter(DSD64 * 2, 88200)  # factor 64 has no table in the original

    def test_pcm_matches_the_original_formula_exactly(self):
        # Output m = round(2^23 * sum_k taps[k] * bit[7 + 32m - k]), zero for the pre-roll; bits
        # before the start are the idle byte 0x69. Checked here sample for sample, no dither.
        rng = np.random.default_rng(3)
        bits = rng.integers(0, 2, size=(1, 98800), dtype=np.uint8)
        path = self._out_copy("formula.dff")
        write_dff(path, bits)
        taps = FILTERS[32]
        n, d = len(taps), 32
        length = (98800 - n) // d + 1
        pre = min(length, (PREROLL_BITS - n) // d)
        pad_bytes = np.full(-(-(n - 1) // 8), 0x69, np.uint8)     # idle bytes before the start
        pad = np.unpackbits(pad_bytes)[-(n - 1):] * 2.0 - 1.0
        x = np.concatenate([pad, bits[0] * 2.0 - 1.0])
        sums = [float(np.dot(taps[::-1], x[PHASE + d * m: PHASE + d * m + n]))
                for m in range(length - pre)]
        scaled = np.array(sums) * 2 ** 23
        expected = np.clip(np.sign(scaled) * np.floor(np.abs(scaled) + 0.5), -2 ** 23 + 1, 2 ** 23 - 1)
        result = convert_file(path, Settings(scale_db=0.0, dither=False), chunk_bytes=16)
        got, _ = sf.read(result.output, dtype="int32")
        self.assertEqual(got.shape[0], length)
        np.testing.assert_array_equal(got[:pre], 0)                      # mono: 1-D array
        np.testing.assert_array_equal(got[pre:] >> 8, expected.astype(np.int64))


if __name__ == "__main__":
    unittest.main()
