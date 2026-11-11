# DSD to FLAC

A Windows app that converts DSF and DFF recordings (DSD, Direct Stream Digital) to FLAC. It has a simple window, so you don't need a command line.

It is a Python port of the original Linux `dsf2flac` converter. The conversion follows that program's rules, and the filter tables are copied from it. Output has been checked against the original on test tones; see [MECHANICS.md](MECHANICS.md) for every rule and where it comes from.

## Features

- Converts `.dsf` and `.dff` files to `.flac`.
- **PCM mode** (default): sample rates 88.2, 176.4 and 352.8 kHz; 16, 20 or 24-bit output; gain from -6 to +6 dB; optional triangular dither.
- **DoP mode**: keeps the DSD data inside a 24-bit FLAC. Rate, bit depth and gain are not used.
- Copies artist, album, title, track and year tags when the source has them.
- Never overwrites an existing file. A number is added instead, e.g. `track (2).flac`.
- Output goes beside each source file, or into a folder you choose.

## Use it

Download or build `DSD2FLAC.exe` (see below), then run it.

1. Click **Add files…** and choose one or more `.dsf` or `.dff` files.
2. Choose the sample rate, bit depth and volume. The defaults match the original: 88.2 kHz, 24-bit, +4 dB.
3. Optionally set **Save to** to a folder. Leave it blank to save beside each source file.
4. Click **Convert**.

Options:
- **No dither** rounds plainly instead of adding a little noise first.
- **DoP mode** keeps the DSD data inside the FLAC.

Notes and limits:
- **20-bit output is a 24-bit FLAC file.** The FLAC writer used here cannot store 20 bits, so the low four bits are empty. The sound is the same, but the file reports 24-bit.
- **PCM rate combinations:** DSD64 input works at 88.2, 176.4 and 352.8 kHz. DSD128 at 352.8 kHz is untested. DSD128 at 88.2 kHz is refused.
- **DoP** works only for DSD64 (output 176.4 kHz) and DSD128 (output 352.8 kHz).
- **DST-compressed DFF files are refused** with a message. The original can read them.
- The last ~11.6 ms of each recording is kept. The original drops it.

## Build it

Requirements: Windows and Python 3.10 or newer.

From the project folder, run:

    build_windows.bat

The script runs the tests, then creates `dist\DSD2FLAC.exe`. Its Python environment and PyInstaller work files are kept in `%LOCALAPPDATA%\DSD2FLAC-build`, so nothing extra ends up in the project folder.

## Run the tests

Install the dependencies, then run the tests from the project folder:

    pip install -r requirements.txt
    python -m unittest discover -s tests

The tests write small test recordings, convert them and check the results: file format, tone level, streaming in pieces, DoP bit content, the output-folder option and the refusal cases. They do **not** compare against the original program's output. Those comparisons were done by hand, and the reference files are in `checks/ref/` (see its [README](checks/ref/README.md)).

## How it matches the original

Checked by hand against the original's output on a generated test tone, with dither off:

- PCM at 88.2 kHz, 24-bit, +4 dB and 0 dB; 88.2 kHz, 16-bit, +4 dB; 176.4 kHz, 24-bit; 352.8 kHz, 24-bit. Matched sample for sample.
- DoP from DSF input: every frame matches except the original's final frame.

Not verified:
- DSD128 PCM output
- DoP from DFF input (the lead-in byte is predicted, not checked)
- 20-bit output against the original (the audio library used here cannot read the original's 20-bit file)
- The FLAC verify setting used by the original

## Project layout

| Path | What it is |
|---|---|
| `src/main.py` | Starts the window |
| `src/gui.py` | The window |
| `src/converter.py` | The conversion: filter, dither, rounding, FLAC writing, tags |
| `src/dsd_reader.py` | Reads DSF and DFF headers and audio |
| `src/original_taps.py` | Filter tables copied from the original program |
| `tools/extract_taps.py` | Extracts the filter tables from the original binary |
| `tests/test_dsd2flac.py` | Self-checks |
| `checks/` | Reference recordings and window screenshots |
| `MECHANICS.md` | How each rule works, with its address in the original program |

## Differences from the original

- The original calls a Linux converter, which cannot run on Windows. This version does the conversion in Python.
- The original's "Check for Updates" button is not included.
- An existing `.flac` file is never overwritten.
- Dither uses the same triangular noise as the original, but not the same random sequence.
- DST-compressed DFF files are refused.

## License

No license has been chosen yet.
