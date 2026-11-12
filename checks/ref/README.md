
Output of the original `dsf2flac` converter, and the DSD input, used for the hand comparisons in MECHANICS.md.
Names: `ref-<rate>-<bits>-<tag>`. The rate and bits come from the file header. The tag is the old file name, kept so nothing is lost.

| File | Rate | Bits | Notes |
|---|---|---|---|
| ref-176400-24-orig.flac | 176400 | 24 | original output |
| ref-352800-24-orig.flac | 352800 | 24 | original output |
| ref-88200-16-orig.flac | 88200 | 16 | original output |
| ref-88200-20-orig.flac | 88200 | 20 | original 20-bit output; the audio library here cannot read it |
| ref-88200-24-orig-s0.flac | 88200 | 24 | original output (the tag "s0" is not explained in the notes) |
| ref-88200-24-original.flac | 88200 | 24 | original output |
| ref-dop-original.flac | 176400 | 24 | original DoP output |
| ref-176400-24-tone-2.flac | 176400 | 24 | does not match this converter's output from ref-tone.dsf; source not recorded |
| ref-176400-24-tone-3.flac | 176400 | 24 | does not match this converter's output from ref-tone.dsf; source not recorded; length differs from the others |
| ref-88200-24-tone-4.flac | 88200 | 24 | this converter's output: matches sample for sample at +4 dB, no dither |
| ref-88200-24-tone.flac | 88200 | 24 | same as this converter's output with the polarity reversed (correlation -0.99) |
| ref-tone.dsf | DSD | 1 | DSD input for the tone |
