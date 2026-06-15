"""Tkinter UI for labeling collected Twitch clips.

Reads dataset_*.json files from data/training/ (produced by collect_training_data.py),
lets the owner accept/reject each clip (Y/N keys), auto-saves progress so sessions
can be resumed. Accepted clips are tagged with a category (Outplay / Funny / Reaction).

Output: data/training/review_state.json (session state) + final_<date>.json on exit.

Usage:
    python -m pipeline.tools.review_clips
"""

import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk

DATA_DIR   = Path(__file__).resolve().parents[2] / "data" / "training"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "review_state.json"


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        clips = state["clips"]
        date  = state["date"]
        index = next((i for i, c in enumerate(clips)
                      if c.get("review_status") == "pending"), len(clips))
        a = sum(1 for c in clips if c.get("review_status") == "accepted")
        r = sum(1 for c in clips if c.get("review_status") == "rejected")
        p = sum(1 for c in clips if c.get("review_status") == "pending")
        print(f"Resuming — {p} pending, {a} accepted, {r} rejected")
        return clips, date, index

    files = sorted(
        list(DATA_DIR.glob("dataset_*.json")) + list(DATA_DIR.glob("clips_*.json")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    seen, files = set(), [f for f in files if not (f.resolve() in seen or seen.add(f.resolve()))]
    if not files:
        print(f"No dataset_*.json or clips_*.json files found in {DATA_DIR}.")
        sys.exit(1)
    path = files[0]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    clips = data.get("clips", [])
    date  = data.get("date", path.stem)
    for c in clips:
        c["review_status"] = "pending"
    mode_tag = " [COLLECT MODE]" if data.get("mode") == "collect" else ""
    print(f"Starting fresh — {len(clips)} clips from {path.name}{mode_tag}")
    return clips, date, 0


def save_state(clips, date):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": date, "clips": clips}, f, indent=2, ensure_ascii=False)


def export_accepted(clips, date):
    accepted = [c for c in clips if c.get("review_status") == "accepted"]
    out = DATA_DIR / f"final_{date}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"reviewed_at": datetime.now().isoformat(), "clips": accepted},
                  f, indent=2, ensure_ascii=False)
    return out, len(accepted)


class App:
    def __init__(self, root, clips, date, start_index):
        self.root   = root
        self.clips  = clips
        self.date   = date
        self.index  = start_index

        root.title("Clip Review")
        root.configure(bg="#0e0e10")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        root.bind("<y>",         lambda _: self._accept())
        root.bind("<Y>",         lambda _: self._accept())
        root.bind("<n>",         lambda _: self._reject())
        root.bind("<N>",         lambda _: self._reject())
        root.bind("<z>",         lambda _: self._undo())
        root.bind("<Z>",         lambda _: self._undo())
        root.bind("<BackSpace>", lambda _: self._undo())

        self.lbl_prog   = tk.Label(root, bg="#0e0e10", fg="#bf94ff",
                                   font=("Helvetica", 10))
        self.lbl_title  = tk.Label(root, bg="#0e0e10", fg="#efeff1",
                                   font=("Helvetica", 13, "bold"),
                                   wraplength=540, justify="left")
        self.lbl_meta   = tk.Label(root, bg="#0e0e10", fg="#adadb8",
                                   font=("Helvetica", 10))
        self.lbl_scores = tk.Label(root, bg="#0e0e10", fg="#555",
                                   font=("Helvetica", 9))
        self.lbl_tx     = tk.Label(root, bg="#18181b", fg="#adadb8",
                                   font=("Helvetica", 9),
                                   wraplength=540, justify="left",
                                   padx=8, pady=4)

        for w in (self.lbl_prog, self.lbl_title, self.lbl_meta, self.lbl_scores):
            w.pack(anchor="w", padx=14, pady=(4, 0))

        btns = tk.Frame(root, bg="#0e0e10")
        btns.pack(pady=(10, 2))

        kw = dict(relief="flat", font=("Helvetica", 12, "bold"), pady=7)
        self.btn_undo = tk.Button(btns, text="Undo [Z]",
                                  bg="#3a3a3d", fg="#adadb8",
                                  padx=12, command=self._undo, **kw)
        self.btn_n    = tk.Button(btns, text="Reject [N]",
                                  bg="#c0392b", fg="white",
                                  padx=20, command=self._reject, **kw)
        self.btn_y    = tk.Button(btns, text="Accept [Y]",
                                  bg="#009e60", fg="white",
                                  padx=20, command=self._accept, **kw)

        self.btn_undo.pack(side="left", padx=6)
        self.btn_n.pack(side="left", padx=6)
        self.btn_y.pack(side="left", padx=6)

        tk.Label(root, bg="#0e0e10", fg="#3a3a3d", font=("Helvetica", 8),
                 text="Progress saves automatically — close any time and reopen to resume"
                 ).pack(pady=(4, 8))

        self._show()

    def _show(self):
        while (self.index < len(self.clips)
               and self.clips[self.index].get("review_status") != "pending"):
            self.index += 1

        if self.index >= len(self.clips):
            self._finish()
            return

        clip = self.clips[self.index]
        a, r, p = self._counts()

        self.lbl_prog.config(
            text=f"#{self.index + 1} / {len(self.clips)}"
                 f"   {a} accepted   {r} rejected   {p} left"
        )
        self.lbl_title.config(text=clip.get("title", ""))
        self.lbl_meta.config(
            text=f"{clip.get('broadcaster_name', '')}  ·  "
                 f"{clip.get('view_count', 0):,} views"
        )
        scores_parts = []
        if "audio_score" in clip:
            scores_parts.append(f"audio={clip['audio_score']:.2f}")
        if "motion_score" in clip:
            scores_parts.append(f"motion={clip['motion_score']:.3f}")
        if clip.get("keyword_match"):
            scores_parts.append(f"keyword={clip.get('keyword','')}")
        if clip.get("is_tournament"):
            scores_parts.append("TOURNAMENT")
        self.lbl_scores.config(text="  ".join(scores_parts) if scores_parts else "")

        desc = (clip.get("description") or clip.get("transcript") or "").strip()
        if desc:
            prefix = "Desc: " if clip.get("description") else "Tx: "
            self.lbl_tx.config(text=prefix + desc[:280])
            self.lbl_tx.pack(anchor="w", padx=14, pady=(0, 4), fill="x")
        else:
            self.lbl_tx.pack_forget()

        self.root.update_idletasks()
        self.btn_undo.config(state="normal" if self._last_reviewed_idx() >= 0 else "disabled")
        self.btn_n.config(state="normal")
        self.btn_y.config(state="normal")

        webbrowser.open(clip["url"])

    def _accept(self):
        if self.btn_y["state"] == "disabled":
            return
        self._set_buttons(False)
        self.clips[self.index]["review_status"] = "accepted"
        self.clips[self.index]["label"]         = "accept"
        cat = self._ask_category()
        self.clips[self.index]["category"] = cat
        save_state(self.clips, self.date)
        self.index += 1
        self._show()

    def _ask_category(self) -> str:
        win = tk.Toplevel(self.root)
        win.title("Category")
        win.configure(bg="#0e0e10")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        result = tk.StringVar(value="")

        tk.Label(win, text="Tag this clip (optional):",
                 bg="#0e0e10", fg="#efeff1", font=("Helvetica", 11)
                 ).pack(pady=(12, 6), padx=16)

        btn_kw = dict(bg="#3a3a3d", fg="white", relief="flat",
                      font=("Helvetica", 10, "bold"), padx=14, pady=6)
        row = tk.Frame(win, bg="#0e0e10")
        row.pack(padx=12, pady=(0, 10))

        for label, value in [("Outplay", "outplay"), ("Funny", "funny"),
                              ("Reaction", "reaction"), ("Skip", "")]:
            def _cb(v=value):
                result.set(v); win.destroy()
            tk.Button(row, text=label, command=_cb, **btn_kw).pack(side="left", padx=4)

        win.bind("<Return>", lambda _: (result.set(""), win.destroy()))
        win.bind("<Escape>", lambda _: (result.set(""), win.destroy()))
        self.root.wait_window(win)
        return result.get()

    def _reject(self):
        if self.btn_n["state"] == "disabled":
            return
        self._set_buttons(False)
        self.clips[self.index]["review_status"] = "rejected"
        self.clips[self.index]["label"]         = "reject"
        save_state(self.clips, self.date)
        self.index += 1
        self._show()

    def _undo(self):
        i = self._last_reviewed_idx()
        if i < 0:
            return
        self.clips[i]["review_status"] = "pending"
        save_state(self.clips, self.date)
        self.index = i
        self._show()

    def _last_reviewed_idx(self):
        for i in range(self.index - 1, -1, -1):
            if self.clips[i].get("review_status") != "pending":
                return i
        return -1

    def _counts(self):
        a = sum(1 for c in self.clips if c.get("review_status") == "accepted")
        r = sum(1 for c in self.clips if c.get("review_status") == "rejected")
        p = sum(1 for c in self.clips if c.get("review_status") == "pending")
        return a, r, p

    def _set_buttons(self, on):
        state = "normal" if on else "disabled"
        self.btn_n.config(state=state)
        self.btn_y.config(state=state)

    def _on_close(self):
        save_state(self.clips, self.date)
        a, _, _ = self._counts()
        if a:
            out, n = export_accepted(self.clips, self.date)
            print(f"Saved {n} accepted clips -> {out}")
        self.root.destroy()

    def _finish(self):
        out, n = export_accepted(self.clips, self.date)
        for w in self.root.winfo_children():
            w.destroy()
        a, r, _ = self._counts()
        tk.Label(self.root, text="All done", bg="#0e0e10", fg="#009e60",
                 font=("Helvetica", 18, "bold")).pack(pady=(28, 6))
        tk.Label(self.root,
                 text=f"Accepted {a}  ·  Rejected {r}\nSaved -> {out.name}",
                 bg="#0e0e10", fg="#adadb8",
                 font=("Helvetica", 11)).pack(pady=(0, 16))
        tk.Button(self.root, text="Reset state (start new session)",
                  bg="#3a3a3d", fg="#adadb8", relief="flat",
                  font=("Helvetica", 10), pady=5,
                  command=self._reset).pack()

    def _reset(self):
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        self.root.destroy()


def main():
    clips, date, index = load_state()
    root = tk.Tk()
    App(root, clips, date, index)
    root.mainloop()


if __name__ == "__main__":
    main()
