"""The "Add documents" window: put PDFs (and .txt / .md files) into the local library without the shell.

  From this computer   pick files or a folder
  From AWS             list the PDFs in your S3 bucket and tick the ones to copy in (read-only key; see the help button)

Open it with library-add.ps1. Themed like Othos; resizable; Escape closes a dialog (never means yes).
"""
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import library  # noqa: E402
import library_add  # noqa: E402
import s3_source  # noqa: E402

BG, PANEL, INK, DIM = "#020705", "#050C08", "#C8E6D2", "#788C82"
FILL, LINE, HOVER = "#0E1019", "#403569", "#19182B"
GOLD, GREEN, RED = "#E7C368", "#3DDC84", "#E58A8A"


def theme(root: tk.Misc) -> None:
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=INK, fieldbackground=FILL, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                 troughcolor=PANEL, focuscolor=LINE, font=("Segoe UI", 10))
    st.configure("TButton", background=FILL, foreground=INK, bordercolor=LINE, padding=(12, 6))
    st.map("TButton", background=[("active", HOVER), ("disabled", PANEL)], foreground=[("disabled", DIM)])
    st.configure("Gold.TButton", foreground=GOLD)
    st.configure("TCheckbutton", background=BG, foreground=INK)
    st.configure("TRadiobutton", background=BG, foreground=INK)
    st.map("TRadiobutton", background=[("active", BG)])
    st.map("TCheckbutton", background=[("active", BG)])
    st.configure("TEntry", fieldbackground=FILL, foreground=INK, insertcolor=INK)
    st.configure("TCombobox", fieldbackground=FILL, foreground=INK, arrowcolor=INK, background=FILL)
    st.map("TCombobox", fieldbackground=[("readonly", FILL)], foreground=[("readonly", INK)])
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=INK, rowheight=24, bordercolor=LINE)
    st.map("Treeview", background=[("selected", HOVER)], foreground=[("selected", GOLD)])
    st.configure("Treeview.Heading", background=FILL, foreground=GOLD, relief="flat")
    st.configure("Dim.TLabel", foreground=DIM)
    st.configure("Head.TLabel", foreground=GOLD, font=("Segoe UI Semibold", 13))
    st.configure("Vertical.TScrollbar", background=FILL, arrowcolor=INK)
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", FILL)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", HOVER)


def themed_box(parent, title: str, message: str, buttons=("OK",), default=0, cancel=None) -> int:
    """A themed message box. Returns the index of the button pressed; Escape and X give `cancel` (the last button if None)."""
    cancel = len(buttons) - 1 if cancel is None else cancel
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(bg=BG)
    top.transient(parent)
    top.minsize(360, 140)
    top.resizable(True, True)
    result = [cancel]
    body = ttk.Frame(top, padding=16)
    body.pack(fill="both", expand=True)
    lab = ttk.Label(body, text=message, wraplength=420, justify="left")
    lab.pack(fill="both", expand=True)
    row = ttk.Frame(body)
    row.pack(fill="x", pady=(14, 0))
    btns = []
    for i, text in enumerate(buttons):
        def press(i=i):
            result[0] = i
            top.destroy()
        b = ttk.Button(row, text=text, command=press)
        b.pack(side="right", padx=(8, 0))
        btns.append(b)
    top.protocol("WM_DELETE_WINDOW", top.destroy)
    top.bind("<Escape>", lambda e: top.destroy())
    top.bind("<Return>", lambda e: btns[default].invoke())
    top.update_idletasks()
    top.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 60}")
    top.grab_set()
    btns[default].focus_set()
    parent.wait_window(top)
    return result[0]


class AwsHelp(tk.Toplevel):
    """How to make an AWS key that can only read one bucket, with a "Don't show this again" tick."""

    def __init__(self, parent, bucket: str = ""):
        super().__init__(parent)
        self.title("Set up an AWS key for Othos")
        self.configure(bg=BG)
        self.transient(parent)
        self.minsize(560, 420)
        self.geometry("680x560")
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="A read-only AWS key, just for Othos", style="Head.TLabel").pack(anchor="w")
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(8, 4))
        ttk.Label(top, text="Your bucket name:").pack(side="left")
        self.bucket = tk.StringVar(value=bucket or "YOUR-BUCKET")
        ent = ttk.Entry(top, textvariable=self.bucket, width=32)
        ent.pack(side="left", padx=8)
        ttk.Button(top, text="Copy policy", command=self.copy_policy).pack(side="left")
        txt_frame = ttk.Frame(body)
        txt_frame.pack(fill="both", expand=True, pady=6)
        self.text = tk.Text(txt_frame, wrap="word", bg=PANEL, fg=INK, insertbackground=INK, relief="flat", padx=10, pady=8,
                            font=("Segoe UI", 10), highlightthickness=1, highlightbackground=LINE)
        sb = ttk.Scrollbar(txt_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("code", font=("Consolas", 10), foreground=GOLD)
        self.bucket.trace_add("write", lambda *a: self.fill())
        self.fill()
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(8, 0))
        self.hide = tk.BooleanVar(value=library_add.ui_state().get("hide_aws_help", False))
        ttk.Checkbutton(foot, text="Don't show this again", variable=self.hide).pack(side="left")
        ttk.Button(foot, text="Close", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.grab_set()
        self.focus_set()

    def fill(self) -> None:
        bucket = (self.bucket.get() or "YOUR-BUCKET").strip()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", s3_source.IAM_HELP.replace("YOUR-BUCKET", bucket) + "\nThe policy (read-only, this bucket only):\n\n")
        self.text.insert("end", s3_source.iam_policy(bucket), "code")
        self.text.configure(state="disabled")

    def copy_policy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(s3_source.iam_policy((self.bucket.get() or "YOUR-BUCKET").strip()))

    def close(self) -> None:
        library_add.save_ui_state(hide_aws_help=bool(self.hide.get()))
        self.destroy()


class Window(tk.Tk):
    def __init__(self):
        super().__init__()
        s3_source.load_env()
        self.title("Add documents to the library")
        self.minsize(640, 460)
        self.geometry("820x620")
        ico = HERE / "icons" / "othos-tray.ico"
        try:
            self.iconbitmap(str(ico))
        except tk.TclError:
            pass
        theme(self)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.source_mode = tk.StringVar(value="local")
        self.folder = tk.StringVar(value="mine")
        self.licence = tk.StringVar(value="")
        self.rows: dict[str, dict] = {}      # tree item -> {"path"|"key"}

        pad = ttk.Frame(self, padding=14)
        pad.pack(fill="both", expand=True)
        pad.columnconfigure(0, weight=1)
        pad.rowconfigure(3, weight=1)
        pad.rowconfigure(5, weight=1)
        ttk.Label(pad, text="Add documents to the library", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(pad, text="PDFs, text and markdown files are turned into searchable pages kept on this PC. "
                            "Your own files are never moved or changed.", style="Dim.TLabel", wraplength=700).grid(row=1, column=0, sticky="we", pady=(2, 8))

        pick = ttk.Frame(pad)
        pick.grid(row=2, column=0, sticky="we")
        ttk.Radiobutton(pick, text="From this computer", value="local", variable=self.source_mode, command=self.mode_changed).pack(side="left")
        ttk.Radiobutton(pick, text="From AWS (my S3 bucket)", value="aws", variable=self.source_mode, command=self.mode_changed).pack(side="left", padx=16)
        self.buttons = ttk.Frame(pick)
        self.buttons.pack(side="right")

        box = ttk.Frame(pad)
        box.grid(row=3, column=0, sticky="nsew", pady=8)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(box, columns=("where", "info"), selectmode="extended", show="tree headings")
        self.tree.heading("#0", text="File")
        self.tree.heading("where", text="Where")
        self.tree.heading("info", text="Size / date")
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("where", width=260, stretch=True)
        self.tree.column("info", width=110, stretch=False)
        sb = ttk.Scrollbar(box, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        opts = ttk.Frame(pad)
        opts.grid(row=4, column=0, sticky="we")
        opts.columnconfigure(3, weight=1)
        ttk.Label(opts, text="Library folder:").grid(row=0, column=0, sticky="w")
        self.combo = ttk.Combobox(opts, textvariable=self.folder, width=30, values=self.folders())
        self.combo.grid(row=0, column=1, padx=8, pady=2)
        ttk.Label(opts, text="Licence (if known):").grid(row=0, column=2, sticky="e", padx=(12, 0))
        ttk.Entry(opts, textvariable=self.licence).grid(row=0, column=3, sticky="we", padx=8)
        self.add_btn = ttk.Button(opts, text="Add to library", style="Gold.TButton", command=self.start_add)
        self.add_btn.grid(row=0, column=4)

        self.log = tk.Text(pad, height=7, wrap="word", bg=PANEL, fg=INK, relief="flat", padx=8, pady=6, state="disabled",
                           font=("Segoe UI", 9), highlightthickness=1, highlightbackground=LINE)
        self.log.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        self.log.tag_configure("ok", foreground=GREEN)
        self.log.tag_configure("bad", foreground=RED)
        self.bind("<Escape>", lambda e: None)     # Escape never adds anything; the X closes the window
        self.mode_changed()
        self.after(150, self.poll)

    # -- helpers
    def folders(self) -> list[str]:
        try:
            names = sorted(set(library.sources()) | {"mine", "mine/manuals"})
        except Exception:
            names = ["mine", "mine/manuals"]
        return names

    def say(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()

    def mode_changed(self) -> None:
        for w in self.buttons.winfo_children():
            w.destroy()
        self.clear_tree()
        if self.source_mode.get() == "local":
            ttk.Button(self.buttons, text="Add files...", command=self.pick_files).pack(side="left", padx=2)
            ttk.Button(self.buttons, text="Add folder...", command=self.pick_folder).pack(side="left", padx=2)
            ttk.Button(self.buttons, text="Remove from list", command=self.remove_rows).pack(side="left", padx=2)
        else:
            ttk.Button(self.buttons, text="List my PDFs", command=self.list_aws).pack(side="left", padx=2)
            ttk.Button(self.buttons, text="Select all", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="left", padx=2)
            ttk.Button(self.buttons, text="How to set up a key", command=lambda: self.show_help(force=True)).pack(side="left", padx=2)
            self.after(100, self.show_help)

    def show_help(self, force: bool = False) -> None:
        s3_source.load_env()
        hidden = library_add.ui_state().get("hide_aws_help", False)
        if force or (not hidden and s3_source.missing()):
            AwsHelp(self, s3_source.settings()["bucket"])
        elif s3_source.missing():
            self.say("AWS isn't set up yet. Missing in .env: " + ", ".join(s3_source.missing()) + ". Press \"How to set up a key\".", "bad")

    # -- from this computer
    def add_rows(self, files) -> None:
        for f in library_add.expand(files):
            if f.suffix.lower() not in library_add.KINDS:
                self.say(f"Skipped {f.name}: only .pdf, .txt and .md files can be added.", "bad")
                continue
            size = f"{f.stat().st_size / 1048576:.1f} MB" if f.stat().st_size > 1048576 else f"{max(1, f.stat().st_size // 1024)} KB"
            item = self.tree.insert("", "end", text=f.name, values=(str(f.parent), size))
            self.rows[item] = {"path": f}

    def pick_files(self) -> None:
        files = filedialog.askopenfilenames(parent=self, title="Choose documents", filetypes=[("Documents", "*.pdf *.txt *.md"), ("All files", "*.*")])
        self.add_rows(files)

    def pick_folder(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Choose a folder of documents")
        if d:
            self.add_rows([d])

    def remove_rows(self) -> None:
        for item in self.tree.selection():
            self.rows.pop(item, None)
            self.tree.delete(item)

    # -- from AWS
    def list_aws(self) -> None:
        s3_source.load_env()
        if s3_source.missing():
            self.show_help(force=True)
            return
        self.run_bg(self._list_job, "Asking AWS for your PDFs...")

    def _list_job(self):
        return ("listed", s3_source.list_pdfs())

    # -- background work
    def run_bg(self, job, note: str) -> None:
        if self.busy:
            return
        self.busy = True
        self.add_btn.state(["disabled"])
        self.say(note)

        def work():
            try:
                self.q.put(job())
            except Exception as e:
                self.q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def start_add(self) -> None:
        picked = self.tree.selection() or self.tree.get_children()
        if not picked:
            self.say("Nothing to add yet. " + ("Add files or a folder first." if self.source_mode.get() == "local" else "Press \"List my PDFs\" first."), "bad")
            return
        folder = self.folder.get().strip()
        try:
            folder = library.clean_source(folder or "mine")
        except ValueError as e:
            self.say(str(e), "bad")
            return
        licence = self.licence.get().strip() or "unknown"
        items = [self.rows[i] for i in picked if i in self.rows]
        say = lambda t: self.q.put(("say", t))   # noqa: E731
        if self.source_mode.get() == "local":
            job = lambda: ("done", library_add.add_local([r["path"] for r in items], folder, licence, say))   # noqa: E731
        else:
            job = lambda: ("done", library_add.add_s3([r["key"] for r in items], folder, licence, say))   # noqa: E731
        self.run_bg(job, f"Adding {len(items)} file(s) to {folder}...")

    def poll(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "say":
                    self.say(data)
                    continue
                self.busy = False
                self.add_btn.state(["!disabled"])
                if kind == "error":
                    self.say(data, "bad")
                elif kind == "listed":
                    self.clear_tree()
                    for d in data:
                        size = f"{d['size'] / 1048576:.1f} MB" if d["size"] > 1048576 else f"{max(1, d['size'] // 1024)} KB"
                        item = self.tree.insert("", "end", text=Path(d["key"]).name, values=(d["key"], f"{size}  {d['modified']}"))
                        self.rows[item] = {"key": d["key"]}
                    self.say(f"{len(data)} PDF(s) found. Select the ones to copy in (Ctrl or Shift for several), then press Add to library."
                             if data else "No PDFs found in that bucket (or folder prefix).")
                elif kind == "done":
                    good = [r for r in data if r[1]]
                    for name, ok, msg in data:
                        self.say(f"{'Added' if ok else 'Not added'}: {name}" + ("" if msg == "added" else f" ({msg})"), "ok" if ok else "bad")
                    self.say(f"Done: {len(good)} added, {len(data) - len(good)} not added.")
                    self.combo.configure(values=self.folders())
        except queue.Empty:
            pass
        self.after(150, self.poll)


def main() -> int:
    Window().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
