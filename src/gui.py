"""The window: choose DSD files, pick settings, convert, and read the log."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from converter import SAMPLE_RATES, Cancelled, Settings, convert_file
from dsd_reader import DsdFileError, read_info

APP_TITLE = "DSD to FLAC"
VERSION = "1.0"

RATE_LABELS = ("88.2 kHz", "176.4 kHz", "352.8 kHz")       # same order as SAMPLE_RATES
BIT_CHOICES = (24, 20, 16)
BIT_LABELS = ("24-bit", "20-bit", "16-bit")
DSD_NAMES = {2822400: "DSD64", 5644800: "DSD128", 11289600: "DSD256"}

BG = "#EEF2F7"
CARD = "#FFFFFF"
HEADER = "#1E293B"
INK = "#1E293B"
MUTED = "#64748B"
ACCENT = "#0F766E"
ACCENT_DARK = "#115E59"
LINE = "#D9E0EA"
LOG_BG = "#0F172A"
LOG_FG = "#E2E8F0"

HELP_TEXT = """Choose one or more .dsf or .dff files. Each one is saved as a .flac file in the same folder. The original file is never changed.

Sample rate: the rate of the FLAC output. 88.2 kHz is the usual choice; 176.4 and 352.8 kHz keep more of the top of the range but make bigger files.

Bit depth: 24-bit is best. 20-bit is written inside a 24-bit FLAC file, with the low four bits empty. 16-bit is the smallest.

Volume: a gain in dB applied before saving. DSD recordings are quiet when converted, so the default is +4 dB. Lower it if the output clips.

No dither: by default a little noise is added before rounding so quiet passages sound smoother. Tick this to round plainly instead.

DoP mode: keeps the raw DSD data inside the FLAC file instead of converting it to PCM. Sample rate, bit depth and volume are not used in this mode. Only some players can play it back as DSD.

Artist, album, title, track and year tags are copied across when the file has them. DST-compressed DFF files cannot be converted."""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x780")
        self.minsize(860, 680)
        self.configure(background=BG)
        self._files: dict[str, str] = {}
        self._events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._apply_style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)

    # ----- look -----------------------------------------------------------

    def _apply_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Card.TFrame", background=CARD)
        style.configure("Card.TLabel", background=CARD, foreground=INK)
        style.configure("Head.TLabel", background=CARD, foreground=INK, font=("Segoe UI Semibold", 12))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TButton", background=CARD, foreground=INK, bordercolor=LINE,
                        padding=(12, 6), focusthickness=0)
        style.map("TButton", background=[("active", "#E2E8F0"), ("disabled", "#F1F5F9")],
                  foreground=[("disabled", "#94A3B8")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                        padding=(20, 9), font=("Segoe UI Semibold", 10), focusthickness=0)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#94A3B8")],
                  bordercolor=[("disabled", "#94A3B8")])
        style.configure("Header.TButton", background="#334155", foreground="white", bordercolor="#334155")
        style.map("Header.TButton", background=[("active", "#475569")])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                        rowheight=28, bordercolor=LINE, borderwidth=0)
        style.configure("Treeview.Heading", background="#F1F5F9", foreground=MUTED,
                        font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#CCFBF1")], foreground=[("selected", INK)])
        style.configure("Horizontal.TProgressbar", troughcolor="#E2E8F0", background=ACCENT,
                        bordercolor="#E2E8F0", lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TCheckbutton", background=CARD, foreground=INK)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, arrowcolor=INK)
        style.configure("TSpinbox", fieldbackground=CARD, background=CARD, arrowcolor=INK)

    # ----- layout ---------------------------------------------------------

    def _build(self) -> None:
        header = tk.Frame(self, background=HEADER)
        header.pack(fill="x")
        titles = tk.Frame(header, background=HEADER)
        titles.pack(side="left", padx=24, pady=16)
        tk.Label(titles, text="DSD to FLAC", background=HEADER, foreground="white",
                 font=("Segoe UI Semibold", 18)).pack(anchor="w")
        tk.Label(titles, text="Convert DSF and DFF recordings to FLAC", background=HEADER,
                 foreground="#94A3B8", font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Button(header, text="Help", style="Header.TButton",
                   command=self._show_help).pack(side="right", padx=24, pady=22)

        body = ttk.Frame(self)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=320)
        body.rowconfigure(0, weight=1)

        self._build_files(body).grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self._build_settings(body).grid(row=0, column=1, sticky="nsew")

        bottom = ttk.Frame(self)
        self._bar = ttk.Progressbar(bottom, style="Horizontal.TProgressbar", maximum=100)
        self._bar.pack(fill="x", side="top")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(8, 0))
        self._status = tk.StringVar(value="Add DSF or DFF files to begin.")
        ttk.Label(row, textvariable=self._status, style="Status.TLabel").pack(side="left")
        self._convert_btn = ttk.Button(row, text="Convert", style="Accent.TButton", command=self._start)
        self._convert_btn.pack(side="right")
        self._stop_btn = ttk.Button(row, text="Stop", command=self._request_stop, state="disabled")
        self._stop_btn.pack(side="right", padx=(0, 10))

        log_card = tk.Frame(self, background=CARD, highlightbackground=LINE, highlightthickness=1)
        tk.Label(log_card, text="Log", background=CARD, foreground=INK,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=14, pady=(10, 4))
        self._log_box = scrolledtext.ScrolledText(
            log_card, height=7, wrap="word", relief="flat", borderwidth=0,
            background=LOG_BG, foreground=LOG_FG, insertbackground=LOG_FG,
            font=("Consolas", 9), padx=10, pady=8)
        self._log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._log_box.configure(state="disabled")

        # Pack the bottom strips first so the file/settings area never squeezes them.
        log_card.pack(side="bottom", fill="x", padx=20, pady=(12, 16))
        bottom.pack(side="bottom", fill="x", padx=20, pady=(14, 0))
        body.pack(side="top", fill="both", expand=True, padx=20, pady=(18, 0))

    def _build_files(self, parent) -> tk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=18)
        ttk.Label(card, text="Files", style="Head.TLabel").pack(anchor="w")
        ttk.Label(card, text="Each file is saved as a .flac beside it, unless a \"Save to\" folder is set.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        table = ttk.Frame(card, style="Card.TFrame")
        self.tree = ttk.Treeview(table, columns=("type", "length"), show="tree headings",
                                 selectmode="extended", height=9)
        self.tree.heading("#0", text="File", anchor="w")
        self.tree.heading("type", text="Format", anchor="w")
        self.tree.heading("length", text="Length", anchor="w")
        self.tree.column("#0", width=330, stretch=True)
        self.tree.column("type", width=190, stretch=False)
        self.tree.column("length", width=90, stretch=False)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Buttons are packed before the list so the list can't squeeze them out.
        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Add files…", command=self._add_files).pack(side="left")
        ttk.Button(buttons, text="Remove selected", command=self._remove_selected).pack(side="left", padx=8)
        ttk.Button(buttons, text="Clear list", command=self._clear).pack(side="left")
        table.pack(side="top", fill="both", expand=True)
        return card

    def _build_settings(self, parent) -> tk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=18)
        ttk.Label(card, text="Settings", style="Head.TLabel").pack(anchor="w")
        ttk.Label(card, text="Used for every file in the list.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        form = ttk.Frame(card, style="Card.TFrame")
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Sample rate", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=6)
        self.rate_box = ttk.Combobox(form, values=RATE_LABELS, state="readonly", width=14)
        self.rate_box.current(0)
        self.rate_box.grid(row=0, column=1, sticky="e", pady=6)

        ttk.Label(form, text="Bit depth", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        self.bit_box = ttk.Combobox(form, values=BIT_LABELS, state="readonly", width=14)
        self.bit_box.current(0)
        self.bit_box.grid(row=1, column=1, sticky="e", pady=6)

        ttk.Label(form, text="Volume", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=6)
        volume_row = ttk.Frame(form, style="Card.TFrame")
        volume_row.grid(row=2, column=1, sticky="e", pady=6)
        self.volume = tk.StringVar(value="4")
        ttk.Spinbox(volume_row, from_=-6, to=6, increment=1, width=6, textvariable=self.volume,
                    justify="right").pack(side="left")
        ttk.Label(volume_row, text=" dB", style="Card.TLabel").pack(side="left")

        ttk.Label(form, text="Save to", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=6)
        self.out_dir = tk.StringVar(value="")
        out_row = ttk.Frame(form, style="Card.TFrame")
        out_row.grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Entry(out_row, textvariable=self.out_dir, width=18).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse…", command=self._choose_out_dir).pack(side="left", padx=(6, 0))
        self._save_note = ttk.Label(card, text="Blank: each FLAC is saved beside its source file.",
                                    style="Muted.TLabel", wraplength=280, justify="left")
        self._save_note.pack(anchor="w", pady=(4, 0))

        ttk.Separator(card).pack(fill="x", pady=12)

        self.no_dither = tk.BooleanVar(value=False)
        ttk.Checkbutton(card, text="No dither (plain rounding)", variable=self.no_dither).pack(anchor="w")
        self.dop = tk.BooleanVar(value=False)
        ttk.Checkbutton(card, text="DoP mode (keep the DSD data in the FLAC)", variable=self.dop,
                        command=self._update_dop).pack(anchor="w", pady=(6, 0))
        self._dop_note = ttk.Label(card, text="", style="Muted.TLabel", wraplength=280, justify="left")
        self._dop_note.pack(anchor="w", pady=(10, 0))
        return card

    # ----- file list ------------------------------------------------------

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose DSD files",
            filetypes=[("DSD audio", "*.dsf *.dff"), ("All files", "*.*")])
        for path in paths:
            self._add_one(path)
        if paths:
            self._status.set(f"{len(self._files)} file(s) in the list.")

    def _add_one(self, path: str) -> None:
        if path in self._files.values():
            return
        try:
            info = read_info(path)
        except (DsdFileError, OSError) as err:
            self._log(f"Skipped {os.path.basename(path)}: {err}")
            return
        rate = DSD_NAMES.get(info.dsd_rate, f"{info.dsd_rate / 1e6:.4f} MHz")
        kind = "stereo" if info.channels == 2 else f"{info.channels} channels"
        seconds = info.bits_per_channel / info.dsd_rate
        length = f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
        iid = self.tree.insert("", "end", text=os.path.basename(path),
                               values=(f"{info.kind} · {rate} · {kind}", length))
        self._files[iid] = path

    def _remove_selected(self) -> None:
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self._files.pop(iid, None)

    def _clear(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._files.clear()

    # ----- conversion -----------------------------------------------------

    def _update_dop(self) -> None:
        if self.dop.get():
            self._dop_note.configure(text="DoP mode ignores the sample rate, bit depth and volume.")
        else:
            self._dop_note.configure(text="")

    def _choose_out_dir(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder for the FLAC files")
        if folder:
            self.out_dir.set(folder)

    def _read_settings(self) -> Settings:
        try:
            volume = int(float(self.volume.get()))
        except ValueError:
            raise ValueError("Volume must be a whole number of dB from -6 to 6.")
        if not -6 <= volume <= 6:
            raise ValueError("Volume must be from -6 to 6 dB.")
        out_dir = self.out_dir.get().strip() or None
        if out_dir and not os.path.isdir(out_dir):
            raise ValueError("The 'Save to' folder does not exist. Choose another one or leave it blank.")
        return Settings(
            sample_rate=SAMPLE_RATES[self.rate_box.current()],
            bit_depth=BIT_CHOICES[self.bit_box.current()],
            scale_db=float(volume),
            dither=not self.no_dither.get(),
            dop=self.dop.get(),
            out_dir=out_dir,
        )

    def _start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        paths = [self._files[iid] for iid in self.tree.get_children()]
        if not paths:
            messagebox.showinfo(APP_TITLE, "Add some DSF or DFF files first.")
            return
        try:
            settings = self._read_settings()
        except ValueError as err:
            messagebox.showerror(APP_TITLE, str(err))
            return
        self._stop.clear()
        self._set_busy(True)
        self._log(f"Starting {len(paths)} file(s).")
        if settings.bit_depth == 20 and not settings.dop:
            self._log("Note: 20-bit files are saved as 24-bit FLAC (the low four bits are empty).")
        self._worker = threading.Thread(target=self._work, args=(paths, settings), daemon=True)
        self._worker.start()

    def _work(self, paths: list[str], settings: Settings) -> None:
        ok = failed = 0
        for number, path in enumerate(paths, 1):
            if self._stop.is_set():
                break
            name = os.path.basename(path)
            self._events.put(("start", number, len(paths), name))
            try:
                result = convert_file(
                    path, settings,
                    progress=lambda fraction: self._events.put(("progress", fraction)),
                    cancel=self._stop)
            except Cancelled:
                self._events.put(("log", "Stopped. The unfinished file was removed."))
                break
            except DsdFileError as err:
                failed += 1
                self._events.put(("log", f"{name}: {err}"))
                continue
            except Exception as err:  # keep going with the rest of the list
                failed += 1
                self._events.put(("log", f"{name}: unexpected error: {err}"))
                continue
            ok += 1
            self._events.put(("done", path, result.output, result.clipped_samples, result.warning))
        self._events.put(("finished", ok, failed))

    def _request_stop(self) -> None:
        self._stop.set()
        self._status.set("Stopping after the current block…")
        self._stop_btn.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self._convert_btn.configure(state="disabled" if busy else "normal")
        self._stop_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self._bar["value"] = 0

    def _poll(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "start":
                    _, number, total, name = event
                    self._status.set(f"Converting {name} ({number} of {total})")
                    self._bar["value"] = 0
                elif kind == "progress":
                    self._bar["value"] = event[1] * 100
                elif kind == "done":
                    _, path, output, clipped, warning = event
                    self._log(f"Saved {os.path.basename(output)}")
                    if clipped:
                        self._log(f"  {clipped} samples were too loud and were trimmed. "
                                  "Try a lower volume.")
                    if warning:
                        self._log(f"  {warning}")
                elif kind == "log":
                    self._log(event[1])
                elif kind == "finished":
                    _, ok, failed = event
                    self._set_busy(False)
                    self._bar["value"] = 100 if failed == 0 else self._bar["value"]
                    self._status.set(f"Finished: {ok} converted, {failed} failed.")
                    self._log(f"Finished: {ok} converted, {failed} failed.")
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # ----- helpers --------------------------------------------------------

    def _log(self, text: str) -> None:
        self._log_box.configure(state="normal")
        self._log_box.insert("end", text + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _show_help(self) -> None:
        messagebox.showinfo(f"{APP_TITLE} help", HELP_TEXT)

    def _on_close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "A conversion is still running. Stop it and quit?"):
                return
            self._stop.set()
        self.destroy()
