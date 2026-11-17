"""The Connect Google window: connect or disconnect a Google account, and read what that means first.

Open it with google-connect.ps1. Themed like Othos; resizable; Escape closes a question (never means yes).
"""
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import google_tools  # noqa: E402
from library_window import BG, DIM, GOLD, GREEN, INK, LINE, PANEL, RED, theme, themed_box  # noqa: E402

SEPARATE_ACCOUNT = """Optional: a separate Google account just for Othos
Instead of connecting your main Google account, you can make a new free Google account only for Othos, and send it just what you want Othos to see: forward chosen mail to it with a Gmail filter, and share chosen calendars, docs and sheets with it (as Viewer). Then connect that account here.
Pros: Othos can never reach your main mail, calendar or files, even if this PC or its saved sign-in were misused; you decide item by item what goes in; mistakes stay small.
Cons: more set-up; forwarded mail is a copy (replies, deletions and new mail in your main account don't follow it); some things can't be forwarded or shared neatly; another account to keep safe.
Good choice if you want Othos to see only a few things, or you share this PC. Your main account is simpler if you want Othos to help with everything."""

NOW_ASKED = ("Right now Othos asks Google only for your email address (to show which account is connected) and to READ your calendar. "
             "Gmail, Sheets and Docs come later, one at a time, and each will ask you again.")


class Window(tk.Tk):
    def __init__(self):
        super().__init__()
        google_tools.load_env()
        self.title("Connect Google")
        self.minsize(560, 460)
        self.geometry("720x620")
        try:
            self.iconbitmap(str(HERE / "icons" / "othos-tray.ico"))
        except tk.TclError:
            pass
        theme(self)
        self.q: queue.Queue = queue.Queue()
        pad = ttk.Frame(self, padding=14)
        pad.pack(fill="both", expand=True)
        pad.columnconfigure(0, weight=1)
        pad.rowconfigure(3, weight=1)
        ttk.Label(pad, text="Connect Google", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        self.state_lbl = ttk.Label(pad, text="", wraplength=660)
        self.state_lbl.grid(row=1, column=0, sticky="we", pady=(4, 8))
        row = ttk.Frame(pad)
        row.grid(row=2, column=0, sticky="we")
        self.connect_btn = ttk.Button(row, text="Connect", style="Gold.TButton", command=self.do_connect)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(row, text="Disconnect", command=self.do_disconnect)
        self.disconnect_btn.pack(side="left", padx=8)
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")

        box = ttk.Frame(pad)
        box.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.text = tk.Text(box, wrap="word", bg=PANEL, fg=INK, relief="flat", padx=12, pady=10, font=("Segoe UI", 10),
                            highlightthickness=1, highlightbackground=LINE)
        sb = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.text.tag_configure("head", foreground=GOLD, font=("Segoe UI Semibold", 11))
        self.text.tag_configure("dim", foreground=DIM)
        self.fill_text()
        self.refresh()
        self.after(150, self.poll)

    def fill_text(self) -> None:
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", NOW_ASKED + "\n\n", "dim")
        try:
            body = (HERE / "google-prompt.txt").read_text(encoding="utf-8")
        except OSError:
            body = ""
        first = True
        for line in body.splitlines():
            if first:
                t.insert("end", line + "\n", "head")
                first = False
            elif line.strip() in ("What you get", "What to know first (the risks, in plain words)"):
                t.insert("end", "\n" + line.strip() + "\n", "head")
            else:
                t.insert("end", line.strip() + "\n")
        lines = SEPARATE_ACCOUNT.splitlines()
        t.insert("end", "\n" + lines[0] + "\n", "head")
        for line in lines[1:]:
            t.insert("end", line + "\n\n")
        t.insert("end", "When you press Connect, your browser opens Google's own sign-in page. Google will warn that the app is "
                        "\"unverified\": that's expected, because this is your own Google project. Choose your account, tick the "
                        "permissions, and come back here.\n", "dim")
        t.configure(state="disabled")

    def refresh(self) -> None:
        s = google_tools.status()
        if not s["configured"]:
            msg, colour = ("Not set up yet: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET aren't in the .env file "
                           "(your own Google Cloud project's Desktop client)."), RED
        elif s["connected"]:
            who = s["email"] or "your Google account"
            msg, colour = f"Connected: {who} (since {s['connected_on']}). Calendar: {'read-only' if s['calendar'] else 'not allowed'}.", GREEN
        else:
            msg, colour = "Not connected. Nothing from Google is read until you press Connect.", INK
        self.state_lbl.configure(text=msg, foreground=colour)
        self.connect_btn.state(["!disabled"] if s["configured"] else ["disabled"])
        self.disconnect_btn.state(["!disabled"] if s["connected"] else ["disabled"])

    def do_connect(self) -> None:
        self.connect_btn.state(["disabled"])
        self.state_lbl.configure(text="Waiting for you to finish signing in, in your browser (up to 5 minutes)...", foreground=GOLD)

        def work():
            try:
                google_tools.connect()
                self.q.put(("ok", "Google is connected."))
            except Exception as e:
                self.q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def do_disconnect(self) -> None:
        if themed_box(self, "Disconnect Google",
                      "Disconnect Google? Othos will stop reading your Google account at once, and the saved sign-in on this PC "
                      "is deleted. You can connect again any time.", ("Disconnect", "Cancel"), default=1) != 0:
            return
        self.q.put(("ok", google_tools.disconnect()))

    def poll(self) -> None:
        try:
            while True:
                kind, msg = self.q.get_nowait()
                self.refresh()
                if kind == "err":
                    themed_box(self, "Connect Google", msg, ("OK",))
                else:
                    self.state_lbl.configure(text=self.state_lbl.cget("text") + "   " + msg)
        except queue.Empty:
            pass
        self.after(200, self.poll)


def main() -> int:
    Window().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
