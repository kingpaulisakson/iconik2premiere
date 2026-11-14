#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["tkinterdnd2"]
# ///
"""
GUI for iconik2premiere: select or drop iconik comment CSVs, get Premiere XML
next to each one.

Run:
    uv run iconik2premiere_gui.py
or:
    pip install tkinterdnd2
    python iconik2premiere_gui.py
"""

import contextlib
import io
import os
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from tkinterdnd2 import DND_FILES, TkinterDnD

import iconik2premiere


def convert(paths):
    for path in paths:
        if not path.lower().endswith(".csv"):
            log("Skipped (not a .csv): %s\n\n" % path)
            continue

        log("Working on %s ...\n" % os.path.basename(path))
        root.update_idletasks()

        # ponytail: runs on the UI thread, and find_media walks up to three
        # parent folders, so the window freezes on big trees. Thread it if
        # that gets annoying.
        media = iconik2premiere.find_media(path)
        if not media and messagebox.askyesno(
                "Media not found",
                "Couldn't find the reviewed media near\n%s\n\n"
                "Pick it now?\n\n(No = markers only, assuming 23.976 fps)"
                % os.path.basename(path)):
            media = filedialog.askopenfilename(title="Select the reviewed media")

        argv = [path] + (["--media", media] if media else [])
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                iconik2premiere.main(argv)
        except SystemExit as e:  # the script reports errors via sys.exit("msg")
            if e.code not in (None, 0):
                buf.write("%s\n" % e.code)
        except Exception as e:
            buf.write("Error: %s\n" % e)
        log(buf.getvalue() + "\n")


def log(text):
    out.configure(state="normal")
    out.insert("end", text)
    out.see("end")
    out.configure(state="disabled")


def build():
    global root, out
    root = TkinterDnD.Tk()
    root.title("iconik → Premiere")
    root.geometry("720x480")

    drop = tk.Label(root, text="Drop iconik comment .csv files here",
                    relief="groove", borderwidth=2, height=5)
    drop.pack(fill="x", padx=12, pady=(12, 6))

    tk.Button(root, text="Select .csv…", command=lambda: convert(
        filedialog.askopenfilenames(filetypes=[("CSV", "*.csv")]))).pack(pady=6)

    out = scrolledtext.ScrolledText(root, state="disabled", font="TkFixedFont")
    out.pack(fill="both", expand=True, padx=12, pady=(6, 12))

    # the whole window is a drop target, not just the label
    root.drop_target_register(DND_FILES)
    root.dnd_bind("<<Drop>>", lambda e: convert(root.tk.splitlist(e.data)))
    return root


if __name__ == "__main__":
    build().mainloop()
