# How the original dsf2flac converter works

Everything here was read from the original converter, `dsf2flac` (Linux x86_64 program, sha256 `6d725a815398…`), with its function code from the Deep engine (Ghidra 12.1.4, `native_function_pseudocode`) and its constants and tables from the binary's bytes. Every PCM rule was then checked against the original's output on a generated test tone (`checks/ref/`). Addresses are in the original binary.

The pseudo-C is Ghidra's reconstruction, not the original source. Where a rule rests on the output rather than on the code alone, it says so.

## 1. Command line and dispatch

| Rule | Where |
|---|---|
| `main` reads the options (`cmdline_parser_internal`), then picks PCM or DoP. | `main` 0x401410, `cmdline_parser_internal` 0x4102c7 |
| Gain: `scale_linear = pow(10, dB / 20)` (constants 10.0 at 0x584978, 20.0 at 0x584970). | `main` |
| Dither is on unless `-n` is given. | `main` (`local_c8 == 0`) |
| Inputs must end in `.dsf`/`.DSF` or `.dff`/`.DFF`. | `main` |
| Output name: the input path with `.flac` (`-o` overrides). The original **overwrites** an existing file. | `main` |
| Options and their defaults (from the help text): sample rate `-r` 88200 (choices 88200, 176400, 352800), bit depth `-b` 24 (choices 16, 20, 24), volume `-s` 4 dB, `-n` no dither, `-d` DoP, `-i` input, `-o` output. | strings at 0x585648–0x585a08 |

## 2. PCM mode (default)

### 2a. Choosing the filter

`DsdDecimator` constructor, 0x407000. The factor is `DSD rate / output rate`, and it picks the table by factor:

| Factor | Output rate (DSD64 input) | Taps | Table address |
|---|---|---|---|
| 8 | 352800 | 96 | 0x582760 (`coefs_352`) |
| 16 | 176400 | 240 | 0x582a60 (`coefs_176`) |
| 32 | 88200 | 575 | 0x5831e0 (`coefs_88`) |

Any other factor fails with `Sorry, incompatible sample rate combination`. So DSD64 works at all three rates; DSD128 gives factor 16 at 352800 (uses the 240-tap table; not tested here), and factor 64 (DSD128 at 88200) is refused.

The taps are copied into `src/original_taps.py` by `tools/extract_taps.py` (reads the bytes at those addresses). Each table sums to about 1.0 (the 96-tap one to 0.999997).

### 2b. The lookup table

`DsdDecimator::initLookupTable`, 0x406dd0. There are `ceil(taps/8)` lanes; each lane has a 256-entry table:

    table[lane][byte] = sum over j = 0..min(8, taps-8*lane)-1 of
                        (bit = time bit (7-j) of the byte ? +1 : -1) * taps[8*lane + j]

summed in order of `j`. "Time bit" means the bit that comes earlier in time: for LSB-first files (DSF) that is bit `7-j` of the raw byte; for MSB-first files (DFF) it is bit `j`.

### 2c. One output sample

`DsdDecimator::getSamples<int>`, 0x4073b0. For output `m`:

1. `sum = 0`; for each lane in order `0 .. L-1`, add `table[lane][byte]`, where `byte` is the DSD byte `(factor/8)*m - lane` of that channel.
2. Bytes before the start are the **idle byte 0x69** (`DsdSampleReader::getIdleSample`, 0x404360), not zero. The original's buffer starts full of idle.
3. `v = sum * gain`, where `gain = 10^(dB/20) * 2^(bits-1)` (the constant 2.0 is at 0x584968).
4. If dither is on: `v += rand()/RAND_MAX - rand()/RAND_MAX` (1 LSB peak triangular; RAND_MAX at 0x584430, -1.0 at 0x584420). Dither is added before the clip.
5. Clip `v` to `±(2^(bits-1) - 1)`.
6. `round(v)` (half away from zero), stored as the integer.

### 2d. Where the output starts and ends

These were fixed by comparing with the original's output, since the code only hints at them (`pcm_track_helper`, `getFirstValidSample` 0x406d20, `getLastValidSample` 0x406d50, `getPosition` 0x406cf0 and `getLength` 0x406cd0 clamp the start and end).

- The file starts with `preroll = floor((32768 - taps) / factor)` zero samples. 32768 bits is 4096 bytes, the reader's pre-roll buffer.
- Output `m` (after the zeros) is the sum above with the newest bit at `7 + factor*m`. The offset 7 is the same for all three rates.
- Total length: `floor((bits - taps) / factor) + 1` samples. The original writes exactly this many, so the **last ~4096 bytes (≈11.6 ms) of audio are not written** (see section 5).

Checked: 88200 Hz 24-bit with +4 dB; 88200 Hz 16-bit with +4 dB; 88200 Hz 24-bit with 0 dB; 176400 Hz 24-bit; 352800 Hz 24-bit. Each matched sample for sample (0 differing samples) with dither off.

### 2e. FLAC writing

`pcm_track_helper`, 0x408740 (and `FLAC::Encoder::File`, which wraps libFLAC):

- Channels, bits per sample (the chosen bit depth), sample rate, compression level **5** (the setting call with 5).
- Total-samples estimate from the length above.
- One PADDING block of 2048 bytes (`FLAC__metadata_object_new(1)`, length 0x800).
- ID3 tags as Vorbis comments (section 4).
- Blocks of 1024 frames while there is at least that much, then the remainder.

Not confirmed: which setting the call with value 1 at slot 0x20 is (verify or streamable subset), and whether verify is on.

## 3. DoP mode

`do_dop_conversion` 0x409d50 → `dop_track_helper` 0x4095b0.

- Only two rates: DSD64 (2822400) gives 176400 Hz; DSD128 (5644800) gives 352800 Hz. Anything else: `ERROR: sample rate not supported by DoP`.
- Sample rate, bit depth, volume and dither are ignored. The FLAC is 24-bit, with compression level 5 and the same 2048-byte padding.
- `DopPacker::pack_buffer`, 0x402ce0. Each output sample is one 24-bit word per channel: `marker << 16 | byteA << 8 | byteB`, where `byteA` is the earlier DSD byte in time. LSB-first (DSF) bytes are bit-reversed first (reverse table).
- Markers: `odd_marker` at 0x81f0e0 is 0x05 on the first frame; `even_marker` at 0x81f0d0 is 0xFA (stored as 0xFFFA0000). They alternate every frame. Checked against the output.
- Lead-in: 2048 frames (4096 bytes) of idle before the audio. Idle is 0x69 bit-reversed for DSF, so the DSF lead-in bytes are 0x96 (checked). For DFF the lead-in should be 0x69 (not checked; no DFF reference yet).
- Length: the original writes `floor(bits/16) + 1` frames, so it drops the last 2047 frames of audio. This converter keeps them (section 5).
- Checked against the original for DSF input: every frame matches except the original's final frame.

## 4. Tags

`id3v2_to_flac`, 0x4026f0. Only these ID3 fields are copied, and they are written as Vorbis comments:

| ID3 field | FLAC name |
|---|---|
| artist (`ID3_GetArtist`) | `ARTIST` |
| album (`ID3_GetAlbum`) | `ALBUM` |
| title (`ID3_GetTitle`) | `TITLE` |
| track (`ID3_GetTrack`) | `TRACKNUMBER` |
| year (`ID3_GetYear`) | `DATE` (string at 0x580e23) |

No genre and no album artist.

## 5. Differences this converter keeps on purpose

- **The last ~11.6 ms of audio.** The original drops it. Keeping it is a deliberate change: it is data loss, not a rule to copy.
- **Overwriting.** The original overwrites an existing `.flac`; this converter writes `name (2).flac` instead.
- **20-bit.** The original writes a real 20-bit FLAC. The audio library here can only write 16 or 24 bits, so 20-bit output is a 24-bit FLAC with the low 4 bits empty. This is also unchecked: the audio library cannot read the original's 20-bit file, so its values could not be compared.
- **Dither.** The original uses C `rand()` from a fixed start, so its dither sequence is fixed. This converter uses the same triangular distribution from a random generator, so the dither noise differs from the original even though its size and shape match.
- **DST-compressed DFF.** The original has DST chunk readers. Whether it decodes DST was not checked, and this converter refuses such files.

## 6. Not yet known

- The origin of the 2048-frame DoP lead-in (it matches the 4096-byte pre-roll, but the code path that inserts it was not traced).
- DFF DoP lead-in byte (predicted 0x69, not checked).
- DSD128 PCM output (predicted from the same rule, not checked).
- The FLAC verify setting.
