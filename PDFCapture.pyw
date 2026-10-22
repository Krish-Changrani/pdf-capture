"""
PDF Capture - windowed app
==========================
Captures every page of a document open in Adobe (or any viewer) and binds
them into one PDF. Uses only ctypes, tkinter, Pillow and numpy.

Two modes:
  Page flip (fast)          - presses Page Down, one screenshot per page.
  Scroll & stitch (sharpest)- Fit Width view. Measures exactly how far one Down
                              arrow scrolls, then scrolls by that fixed amount,
                              stitches the strip and splits it back into pages.

Neither mode uses a fixed delay: it watches the screen and captures the moment
Adobe has finished drawing.

Both modes auto-crop each page to its own edges, so portrait and landscape
pages mix cleanly. Drag the capture box around the whole grey viewing area.

Abort anytime: hold Esc, or move the mouse to the top-left corner.
"""

import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import re
import sys
import time
import traceback
import zlib
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    from PIL import Image
    import numpy as np
except ImportError as _e:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("PDF Capture", f"Missing library: {_e}\nOpen Command Prompt and run:  pip install pillow numpy")
    sys.exit(1)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

CONFIG_PATH = Path.home() / ".pdfcapture.json"
MODES = {"Page flip (fast)": "flip", "Scroll & stitch (sharpest)": "scroll"}
QUALITY = {"Lossless (sharpest, larger file)": "lossless", "High (smaller file)": "jpeg"}
ADVANCE_KEYS = {"Page Down": 0x22, "Right arrow": 0x27, "Down arrow": 0x28, "Space": 0x20}
POLL = 0.04          # seconds between screen checks while waiting for Adobe to finish drawing
MAX_WAIT = 3.0       # give up waiting for the view to change after this long (= end of document)
CAL_PRESSES = 3      # Down-arrow presses used to measure scroll distance
STEP_FRAC = 0.75     # scroll this fraction of the box height per step (rest = overlap check)
SHIFT_TOL = 12       # pixels either side of the expected scroll to check
VK_DOWN, VK_UP = 0x28, 0x26
MAX_REPAIRS = 25
VK_ESCAPE, VK_HOME = 0x1B, 0x24
SRCCOPY = 0x00CC0020
MAX_FLIP_PAGES = 1000
MAX_SCROLLS = 6000
COUNTDOWN_SECS = 5
BG_TOL = 14          # how close (0-255) a pixel must be to count as Adobe's grey background
GOOD_ERR = 1.5       # stitch match threshold
SAME_ERR = 1.0       # frames this similar = the view didn't move
NO_GAP_LIMIT = 6     # box-heights of strip with no page gap before forcing a cut
MIN_PAGE_ROWS = 60

HINTS = {
    "flip": ("Adobe setup: View > Page Display > Single Page View, then Ctrl+0 (Fit Page).\n"
             "Drag the box around the whole grey viewing area, not just the page."),
    "scroll": ("Adobe setup: View > Page Display > Enable Scrolling (Single Page Continuous), then\n"
               "Ctrl+2 (Fit Width). Ctrl+H (Read Mode) gives more room. Drag the box around the\n"
               "whole viewing area, leaving out the scrollbar. Scrolls with the Down arrow."),
}


# ============================ Win32 ============================
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def grab_region(left, top, width, height):
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    gdi32.SelectObject(mem_dc, bmp)
    gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, height, buffer, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)
    return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()


def press_key(vk, times=1):
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.005)
        user32.keybd_event(vk, 0, 0x0002, 0)
        time.sleep(0.005)


class Abort(Exception):
    pass


class UserError(Exception):
    pass


def abort_requested():
    if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
        return True
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x <= 2 and pt.y <= 2


# ============================ image analysis ============================
def near_bg(a, bg, tol=BG_TOL):
    return np.abs(a.astype(np.int16) - np.asarray(bg).astype(np.int16)).max(axis=-1) <= tol


def shave_shadow(a, bg, max_px=8):
    """Remove Adobe's thin grey drop-shadow lines left at the page edges."""
    bgv = float(np.mean(bg))

    def is_shadow(line):
        g = line.astype(np.float32).mean(axis=-1)
        if g.shape[0] > 24:
            g = g[10:-10]
        return g.std() < 4 and (bgv - 90) <= g.mean() <= (bgv + 3)

    t, b, l, r = 0, a.shape[0], 0, a.shape[1]
    for _ in range(max_px):
        if b - t > 4 * max_px and is_shadow(a[t, l:r]): t += 1
        else: break
    for _ in range(max_px):
        if b - t > 4 * max_px and is_shadow(a[b - 1, l:r]): b -= 1
        else: break
    for _ in range(max_px):
        if r - l > 4 * max_px and is_shadow(a[t:b, l]): l += 1
        else: break
    for _ in range(max_px):
        if r - l > 4 * max_px and is_shadow(a[t:b, r - 1]): r -= 1
        else: break
    return a[t:b, l:r]


def crop_to_content(a, bg):
    mask = ~near_bg(a, bg)
    rows = np.where(mask.mean(axis=1) > 0.002)[0]
    cols = np.where(mask.mean(axis=0) > 0.002)[0]
    if rows.size == 0 or cols.size == 0:
        return None
    a = a[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    return shave_shadow(a, bg)


def trim_flip(img):
    """Page-flip mode: crop to the page if the box corners show a uniform background."""
    a = np.array(img)
    k = 4
    patches = [a[:k, :k], a[:k, -k:], a[-k:, :k], a[-k:, -k:]]
    meds = [np.median(p.reshape(-1, 3), axis=0) for p in patches]
    bg = np.median(np.array(meds), axis=0)
    if max(np.abs(m - bg).max() for m in meds) > BG_TOL:
        return img
    c = crop_to_content(a, bg)
    return img if c is None else Image.fromarray(np.ascontiguousarray(c))


def find_bg_scroll(frame):
    """Find Adobe's background colour: grey side margins, or grey gap rows between pages."""
    cands = []
    for col in (frame[:, 0], frame[:, -1]):
        vals, counts = np.unique(col.reshape(-1, 3), axis=0, return_counts=True)
        mode = vals[np.argmax(counts)].astype(np.float32)   # most common colour, not an average
        if near_bg(col, mode).mean() > 0.95 and mode.mean() < 250:
            cands.append(mode)
    if len(cands) == 2 and np.abs(cands[0] - cands[1]).max() <= BG_TOL:
        return cands[0]
    sub = frame[:, ::4]
    med_rows = np.median(sub, axis=1)
    uniform = near_bg(sub, med_rows[:, None, :]).mean(axis=1) > 0.995
    nonwhite = med_rows.mean(axis=1) < 245
    idx = np.where(uniform & nonwhite)[0]
    if idx.size >= 3:
        return np.median(med_rows[idx], axis=0)
    return None


def row_sig(a):
    step = max(1, a.shape[1] // 160)
    return a[:, ::step].astype(np.float32).mean(axis=2)


def search_shift(p, n, lo, hi):
    """Best scroll distance d in [lo, hi] between two row signatures. Returns (d, error)."""
    H = p.shape[0]
    min_ov = max(20, H // 15)
    lo, hi = max(1, int(lo)), min(H - min_ov, int(hi))
    if hi < lo:
        return 0, 1e9
    best_d, best_e = lo, 1e9
    for d in range(lo, hi + 1):
        e = float(np.abs(p[d:] - n[:H - d]).mean())
        if e < best_e:
            best_d, best_e = d, e
    return best_d, best_e


def uniform_rows(a):
    g = a[:, ::2].astype(np.float32).mean(axis=2)
    return g.std(axis=1) < 2.0


def runs_of(mask):
    m = np.concatenate([[0], mask.astype(np.int8), [0]])
    d = np.diff(m)
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


# ============================ PDF writer ============================
class PdfWriter:
    """Minimal PDF writer: one image per page, lossless (Flate) or JPEG q95, streamed to disk."""

    def __init__(self, path, lossless=True):
        self.f = open(path, "wb")
        self.lossless = lossless
        self.offsets = {}
        self.kids = []
        self.next_id = 3  # 1 = catalog, 2 = pages
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def _obj(self, oid, body, stream=None):
        self.offsets[oid] = self.f.tell()
        self.f.write(f"{oid} 0 obj\n".encode() + body)
        if stream is not None:
            self.f.write(b"\nstream\n" + stream + b"\nendstream")
        self.f.write(b"\nendobj\n")

    def add(self, img):
        img = img.convert("RGB")
        w, h = img.size
        scale = 842.0 / max(w, h)  # longest side = A4 long edge
        pw, ph = w * scale, h * scale
        if self.lossless:
            data, filt = zlib.compress(img.tobytes(), 6), "/FlateDecode"
        else:
            bio = io.BytesIO()
            img.save(bio, "JPEG", quality=95, subsampling=0)
            data, filt = bio.getvalue(), "/DCTDecode"
        im_id, ct_id, pg_id = self.next_id, self.next_id + 1, self.next_id + 2
        self.next_id += 3
        self._obj(im_id, (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                          f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter {filt} "
                          f"/Length {len(data)} >>").encode(), data)
        content = f"q {pw:.3f} 0 0 {ph:.3f} 0 0 cm /Im0 Do Q".encode()
        self._obj(ct_id, f"<< /Length {len(content)} >>".encode(), content)
        self._obj(pg_id, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.3f} {ph:.3f}] "
                          f"/Resources << /XObject << /Im0 {im_id} 0 R >> >> "
                          f"/Contents {ct_id} 0 R >>").encode())
        self.kids.append(pg_id)

    def close(self):
        kids = " ".join(f"{k} 0 R" for k in self.kids)
        self._obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.kids)} >>".encode())
        self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        xref = self.f.tell()
        n = self.next_id
        self.f.write(f"xref\n0 {n}\n".encode())
        self.f.write(b"0000000000 65535 f \n")
        for oid in range(1, n):
            self.f.write(f"{self.offsets.get(oid, 0):010d} 00000 n \n".encode())
        self.f.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        self.f.close()


# ============================ misc ============================
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def default_folder():
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]', "", s).strip().rstrip(".")


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def rects_overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


# ============================ app ============================
class App:
    def __init__(self, root):
        self.root = root
        root.title("PDF Capture")
        root.resizable(False, False)
        root.report_callback_exception = self.on_error

        cfg = load_config()
        self.mode_var = tk.StringVar(value=cfg.get("mode", "Scroll & stitch (sharpest)"))
        if self.mode_var.get() not in MODES:
            self.mode_var.set("Scroll & stitch (sharpest)")
        self.folder = tk.StringVar(value=cfg.get("folder", str(default_folder())))
        self.name = tk.StringVar(value="")
        self.pages = tk.StringVar(value="")
        self.quality = tk.StringVar(value=cfg.get("quality", "Lossless (sharpest, larger file)"))
        if self.quality.get() not in QUALITY:
            self.quality.set("Lossless (sharpest, larger file)")
        self.key = tk.StringVar(value=cfg.get("key", "Page Down"))
        self.delay = tk.StringVar(value=str(cfg.get("settle", 0.2)))
        self.go_home = tk.BooleanVar(value=cfg.get("go_home", True))
        self.autotrim = tk.BooleanVar(value=cfg.get("autotrim", True))
        self.keep_png = tk.BooleanVar(value=cfg.get("keep_png", False))
        self.hint = tk.StringVar()
        self.status = tk.StringVar(value="Ready.")
        self.last_pdf = None
        self.pill = None
        self._build_ui()
        self.mode_var.trace_add("write", self.on_mode_change)
        self.on_mode_change()

    def _build_ui(self):
        f = ttk.Frame(self.root, padding=16)
        f.grid(sticky="nsew")
        pad = {"padx": 4, "pady": 4}
        gray = {"foreground": "gray"}

        ttk.Label(f, text="PDF Capture", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(f, text="Mode:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=self.mode_var, values=list(MODES), state="readonly",
                     width=30).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(f, text="Save to:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.folder, width=50).grid(row=2, column=1, sticky="we", **pad)
        ttk.Button(f, text="Browse...", command=self.browse).grid(row=2, column=2, **pad)

        ttk.Label(f, text="File name:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.name, width=50).grid(row=3, column=1, sticky="we", **pad)
        ttk.Label(f, text="blank = timestamp", **gray).grid(row=3, column=2, sticky="w", **pad)

        ttk.Label(f, text="Pages:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.pages, width=10).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(f, text="blank = whole document", **gray).grid(row=4, column=2, sticky="w", **pad)

        ttk.Label(f, text="PDF quality:").grid(row=5, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=self.quality, values=list(QUALITY), state="readonly",
                     width=30).grid(row=5, column=1, sticky="w", **pad)

        ttk.Label(f, text="Advance key:").grid(row=6, column=0, sticky="w", **pad)
        self.key_box = ttk.Combobox(f, textvariable=self.key, values=list(ADVANCE_KEYS),
                                    state="readonly", width=14)
        self.key_box.grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(f, text="page flip mode only", foreground="gray").grid(row=6, column=2, sticky="w", **pad)

        ttk.Label(f, text="Settle time (s):").grid(row=7, column=0, sticky="w", **pad)
        ttk.Spinbox(f, textvariable=self.delay, from_=0.05, to=3.0, increment=0.05,
                    width=8).grid(row=7, column=1, sticky="w", **pad)
        ttk.Label(f, text="how long the screen must stay still before capture; raise if parts of "
                  "pages come out blank", **gray, wraplength=230).grid(row=7, column=2, sticky="w", **pad)

        checks = ttk.Frame(f)
        checks.grid(row=8, column=1, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(checks, text="Start from the first page (presses Home)",
                        variable=self.go_home).pack(anchor="w")
        self.trim_chk = ttk.Checkbutton(checks, text="Auto-crop to page edges (page flip mode)",
                                        variable=self.autotrim)
        self.trim_chk.pack(anchor="w")
        ttk.Checkbutton(checks, text="Also keep individual page images (PNG)",
                        variable=self.keep_png).pack(anchor="w")

        ttk.Label(f, textvariable=self.hint, foreground="#555555", justify="left").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))

        btns = ttk.Frame(f)
        btns.grid(row=10, column=0, columnspan=3, sticky="we", pady=(12, 4))
        ttk.Button(btns, text="Select region & start", command=self.start).pack(side="left", padx=4)
        self.open_pdf_btn = ttk.Button(btns, text="Open PDF", command=self.open_pdf, state="disabled")
        self.open_pdf_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Open folder", command=self.open_folder).pack(side="left", padx=4)

        ttk.Label(f, textvariable=self.status, wraplength=560, foreground="#1a4f8b").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="To stop a capture: hold Esc, or move the mouse to the top-left corner.",
                  **gray).grid(row=12, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def on_mode_change(self, *_):
        mode = MODES[self.mode_var.get()]
        self.hint.set(HINTS[mode])
        self.trim_chk.state(["!disabled"] if mode == "flip" else ["disabled"])
        self.key_box.state(["!disabled"] if mode == "flip" else ["disabled"])

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or str(default_folder()),
                                    title="Choose where to save the PDF")
        if d:
            self.folder.set(os.path.normpath(d))

    def open_folder(self):
        p = Path(self.folder.get())
        if p.exists():
            os.startfile(str(p))
        else:
            messagebox.showwarning("PDF Capture", "That folder doesn't exist yet.")

    def open_pdf(self):
        if self.last_pdf and Path(self.last_pdf).exists():
            os.startfile(str(self.last_pdf))

    def on_error(self, exc, val, tb):
        self.close_pill()
        self.root.deiconify()
        messagebox.showerror("PDF Capture - error",
                             "".join(traceback.format_exception(exc, val, tb))[-1500:])

    # ---------------- start ----------------
    def start(self):
        folder = Path(self.folder.get().strip())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("PDF Capture", f"Can't use that folder:\n{e}")
            return
        p = self.pages.get().strip()
        if p and (not p.isdigit() or int(p) < 1):
            messagebox.showwarning("PDF Capture", "Pages must be a whole number, or blank.")
            return
        try:
            delay = float(self.delay.get())
            assert 0.02 <= delay <= 5
        except Exception:
            messagebox.showwarning("PDF Capture", "Settle time must be a number of seconds, e.g. 0.2.")
            return

        self.mode = MODES[self.mode_var.get()]
        self.target = int(p) if p else None
        self.delay_s = delay
        self.vk = ADVANCE_KEYS.get(self.key.get(), 0x22)
        self.out_folder = folder
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.base_name = safe_name(self.name.get()) or f"capture_{stamp}"

        save_config({"mode": self.mode_var.get(), "folder": str(folder), "quality": self.quality.get(),
                     "key": self.key.get(), "settle": delay, "go_home": self.go_home.get(),
                     "autotrim": self.autotrim.get(), "keep_png": self.keep_png.get()})
        self.root.withdraw()
        self.root.after(250, self.select_region)

    # ---------------- region overlay ----------------
    def select_region(self):
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-alpha", 0.30)
        ov.attributes("-topmost", True)
        ov.configure(bg="black")
        cv = tk.Canvas(ov, cursor="crosshair", bg="gray15", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        cv.create_text(ov.winfo_screenwidth() // 2, 40,
                       text="Drag a box around the whole viewing area   (Esc = cancel)",
                       fill="white", font=("Segoe UI", 16, "bold"))
        st = {"rect": None, "x0": 0, "y0": 0}

        def press(e):
            st["x0"], st["y0"] = e.x_root, e.y_root
            st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

        def drag(e):
            if st["rect"] is not None:
                cv.coords(st["rect"], st["x0"] - ov.winfo_rootx(), st["y0"] - ov.winfo_rooty(), e.x, e.y)

        def release(e):
            box = (min(st["x0"], e.x_root), min(st["y0"], e.y_root),
                   abs(e.x_root - st["x0"]), abs(e.y_root - st["y0"]))
            ov.destroy()
            if box[2] < 100 or box[3] < 100:
                self.reset("Region too small - try again.")
            else:
                self.root.after(150, lambda: self.countdown(box))

        def cancel(_=None):
            ov.destroy()
            self.reset("Cancelled.")

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        ov.bind("<Escape>", cancel)
        ov.focus_force()

    # ---------------- countdown + status pill ----------------
    def countdown(self, box):
        l, t, w, h = box
        self.box = (l, t, w - w % 2, h - h % 2)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        pw, ph = 380, 64
        spots = [(sw - pw - 20, 20), (sw - pw - 20, sh - ph - 80), (20, sh - ph - 80), (20, 40)]
        spot = next((s for s in spots if not rects_overlap((s[0], s[1], pw, ph), self.box)), None)

        self.pill = tk.Toplevel(self.root)
        self.pill.overrideredirect(True)
        self.pill.attributes("-topmost", True)
        self.pill.configure(bg="#1a4f8b")
        x, y = spot if spot else spots[0]
        self.pill.geometry(f"{pw}x{ph}+{x}+{y}")
        self.pill_lbl = tk.Label(self.pill, bg="#1a4f8b", fg="white", font=("Segoe UI", 12, "bold"))
        self.pill_lbl.pack(fill="both", expand=True)
        self.pill_persist = spot is not None

        def tick(n):
            if abort_requested():
                self.close_pill()
                self.reset("Cancelled.")
                return
            if n == 0:
                if not self.pill_persist:
                    self.close_pill()
                self.root.after(100, self.begin_capture)
                return
            self.pill_lbl.config(text=f"Click on the PDF page now...  {n}\n(hold Esc to cancel)")
            self.root.after(1000, lambda: tick(n - 1))

        tick(COUNTDOWN_SECS)

    def set_pill(self, text):
        if self.pill:
            self.pill_lbl.config(text=text)

    def close_pill(self):
        if self.pill:
            try:
                self.pill.destroy()
            except Exception:
                pass
            self.pill = None

    # ---------------- capture ----------------
    def begin_capture(self):
        self.work_dir = unique_path(self.out_folder / f"{self.base_name}_pages")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.png_paths, self.page_count = [], 0
        self.buf, self.gap, self.bg = None, None, None
        self.jumps = self.forced_cuts = self.repairs = 0
        try:
            if self.go_home.get():
                press_key(VK_HOME)
                time.sleep(0.4)
            reason = self.run_flip() if self.mode == "flip" else self.run_scroll()
        except Abort:
            reason = "Stopped early by you."
        except UserError as e:
            self.close_pill()
            self._cleanup_pngs()
            self.reset(str(e))
            return
        self.finish(reason)

    def check_abort(self):
        if abort_requested():
            raise Abort()

    def grab(self):
        return np.array(grab_region(*self.box))

    def wait_settled(self, prev_sig, need_change=True):
        """Poll the screen until it differs from prev_sig AND has stayed still for the settle time.
        Returns (frame, sig, status) with status 'ok', 'unchanged' or 'unstable'."""
        t0 = time.time()
        last_sig, frame, still_since, changed = None, None, None, False
        while True:
            self.check_abort()
            now = time.time()
            f = self.grab()
            sg = row_sig(f)
            moved = prev_sig is None or np.abs(sg - prev_sig).mean() >= SAME_ERR
            changed = changed or moved
            if last_sig is not None and np.abs(sg - last_sig).mean() < SAME_ERR and (moved or not need_change):
                if still_since is None:
                    still_since = last_t
                if now - still_since >= self.delay_s:
                    return f, sg, "ok"
            else:
                still_since = None
            last_sig, frame, last_t = sg, f, now
            if now - t0 > max(MAX_WAIT, self.delay_s * 3):
                back_to_start = prev_sig is not None and np.abs(last_sig - prev_sig).mean() < SAME_ERR
                return frame, last_sig, ("unchanged" if (back_to_start or not changed) else "unstable")
            self.root.update()
            time.sleep(POLL)

    def settle_and_match(self, prev_s, ranges):
        """Wait for the view to settle, then find the scroll distance, searching `ranges` in order.
        If nothing lines up, Adobe may still be drawing: keep re-checking until MAX_WAIT."""
        t0 = time.time()
        f, s, status = self.wait_settled(prev_s)
        while True:
            if status == "unchanged" or np.abs(s - prev_s).mean() < SAME_ERR:
                return f, s, None, "unchanged"
            for lo, hi in ranges:
                d, err = search_shift(prev_s, s, lo, hi)
                if err < GOOD_ERR:
                    return f, s, d, "ok"
            if time.time() - t0 > MAX_WAIT:
                return f, s, None, "nomatch"
            time.sleep(POLL * 2)
            f, s, status = self.wait_settled(s, need_change=True)
            if status == "unchanged":      # screen is genuinely stable and still doesn't match
                return f, s, None, "nomatch"

    def save_page(self, img):
        self.page_count += 1
        path = self.work_dir / f"page_{self.page_count:03d}.png"
        img.save(path)
        self.png_paths.append(path)

    # ---- page flip ----
    def run_flip(self):
        frame, sig, _ = self.wait_settled(None, need_change=False)
        limit = self.target or MAX_FLIP_PAGES
        while True:
            img = Image.fromarray(frame)
            self.save_page(trim_flip(img) if self.autotrim.get() else img)
            self.set_pill(f"Capturing page {self.page_count}...\n(hold Esc to stop)")
            if self.page_count >= limit:
                return "Captured all requested pages."
            press_key(self.vk)
            frame2, sig2, status = self.wait_settled(sig)
            if status == "unchanged" and self.target is None:
                return "Reached the last page."
            frame, sig = frame2, sig2

    # ---- scroll & stitch ----
    def start_buffer(self, rows):
        self.buf = rows
        self.gap = self.gap_of(rows)

    def append_rows(self, rows):
        self.buf = np.concatenate([self.buf, rows], axis=0)
        self.gap = np.concatenate([self.gap, self.gap_of(rows)])

    def gap_of(self, rows):
        return near_bg(rows[:, ::2], self.bg).mean(axis=1) >= 0.995

    def run_scroll(self):
        f, s, _ = self.wait_settled(None, need_change=False)
        H = f.shape[0]
        self.bg = find_bg_scroll(f)
        if self.bg is None:
            raise UserError("Couldn't find Adobe's grey background inside the box. Make sure the box "
                            "includes some grey around or between the pages (zoom out a touch from "
                            "Fit Width if the page fills the whole width), then try again.")

        # 1) Calibrate: how many pixels does one Down-arrow press scroll?
        for attempt in range(5):
            press_key(VK_DOWN, CAL_PRESSES)
            f2, s2, d, status = self.settle_and_match(s, [(1, H * 2 // 3)])
            if status == "unchanged":
                self.start_buffer(f)
                self.flush_pages(final=True)
                return "Reached the end of the document."
            if status == "ok":
                break
            # Probably caught Adobe mid-draw: go back, wait longer, re-take the first shot
            press_key(VK_UP, CAL_PRESSES)
            self.slow_down(2.0)
            f, s, _ = self.wait_settled(s2)
        else:
            raise UserError("Couldn't measure how far Adobe scrolls. Check the view is set to "
                            "Enable Scrolling (Single Page Continuous), nothing is covering the page, and try again.")
        self.bg = find_bg_scroll(f) if find_bg_scroll(f) is not None else self.bg
        self.start_buffer(f)
        self.append_rows(f2[-d:])
        line = d / CAL_PRESSES
        presses = max(1, int(STEP_FRAC * H / line))
        expected = int(round(presses * line))
        prev_f, prev_s = f2, s2

        # 2) Scroll by that fixed amount, verifying each join near the expected distance
        steps = 0
        while True:
            self.flush_pages(final=False)
            self.set_pill(f"Scrolling... {self.page_count} pages done\n(hold Esc to stop)")
            if self.target and self.page_count >= self.target:
                return "Captured all requested pages."
            steps += 1
            if steps > MAX_SCROLLS:
                return "Hit the safety limit."

            press_key(VK_DOWN, presses)
            f, s, d, status = self.settle_and_match(
                prev_s, [(expected - SHIFT_TOL, expected + SHIFT_TOL),   # normal step
                         (1, expected + SHIFT_TOL)])                     # shorter: end of document
            if status == "unchanged":
                self.finish_strip()
                return "Reached the end of the document."
            if status == "ok":
                if abs(d - expected) <= SHIFT_TOL:
                    expected = d                   # track tiny rounding drift
                self.append_rows(f[-d:])
            elif uniform_rows(prev_f[expected:]).mean() > 0.6 and self.repairs < MAX_REPAIRS:
                # The previous screenshot was taken before Adobe finished drawing (its lower part
                # is blank). Scroll back up by the same fixed amount, wait longer, re-take it and
                # overwrite the last screen-height of the strip with the good version.
                press_key(VK_UP, presses)
                self.slow_down()
                fp, sp, _ = self.wait_settled(s)
                self.buf[-H:] = fp
                self.gap[-H:] = self.gap_of(fp)
                prev_f, prev_s = fp, sp
                continue
            else:
                # No overlap at all: the view jumped. Keep everything, start a fresh strip.
                self.jumps += 1
                if self.jumps > 5:
                    self.flush_pages(final=True)
                    return "Stopped: the view kept jumping instead of scrolling smoothly."
                self.flush_pages(final=True)
                self.start_buffer(f)
            prev_f, prev_s = f, s

    def finish_strip(self):
        """At the end: the view hasn't moved, so re-take it once more (Adobe has had plenty of
        time to finish drawing) and refresh the last screen-height of the strip before cutting."""
        time.sleep(self.delay_s)
        H = self.box[3]
        last = self.grab()
        if self.buf.shape[0] >= H:
            self.buf[-H:] = last
            self.gap[-H:] = self.gap_of(last)
        self.flush_pages(final=True)

    def slow_down(self, factor=1.5):
        self.repairs += 1
        self.delay_s = min(1.5, self.delay_s * factor)

    def flush_pages(self, final):
        """Cut every complete page out of the stitched strip; keep the unfinished tail."""
        if self.buf is None or self.buf.shape[0] == 0:
            return
        content = ~self.gap
        n, H = len(content), self.box[3]
        consumed = n
        for s0, e in runs_of(content):
            # A page is complete once a gap follows it. Until the end, the last screen-height of
            # the strip is held back in case it needs repairing.
            if not final and (e >= n or e > n - H):
                consumed = s0
                break
            if e - s0 >= MIN_PAGE_ROWS:
                self.emit_page(self.buf[s0:e])
        if not final:
            consumed = min(consumed, n - H)
        self.buf, self.gap = self.buf[consumed:], self.gap[consumed:]

        # Safety valve: no page gaps found for a long stretch -> cut at a blank row
        while not final and self.buf.shape[0] > NO_GAP_LIMIT * H:
            target = int(self.buf.shape[1] * 1.414)
            lo, hi = int(target * 0.7), min(int(target * 1.3), self.buf.shape[0] - 1)
            cand = np.where(uniform_rows(self.buf[lo:hi]))[0] + lo
            cut = int(cand[np.argmin(np.abs(cand - target))]) if cand.size else target
            self.emit_page(self.buf[:cut])
            self.forced_cuts += 1
            self.buf, self.gap = self.buf[cut:], self.gap[cut:]

    def emit_page(self, rows):
        if self.target and self.page_count >= self.target:
            return
        c = crop_to_content(rows, self.bg)
        if c is None or c.shape[0] < MIN_PAGE_ROWS:
            return
        self.save_page(Image.fromarray(np.ascontiguousarray(c)))

    # ---------------- finish ----------------
    def finish(self, reason):
        self.close_pill()
        if self.mode == "scroll" and self.buf is not None and self.buf.shape[0]:
            self.flush_pages(final=True)
        if not self.png_paths:
            self._cleanup_pngs()
            self.reset(f"{reason} Nothing was captured.")
            return

        self.root.deiconify()
        pdf_path = unique_path(self.out_folder / f"{self.base_name}.pdf")
        writer = PdfWriter(pdf_path, lossless=QUALITY[self.quality.get()] == "lossless")
        for i, p in enumerate(self.png_paths, 1):
            with Image.open(p) as im:
                writer.add(im)
            if i % 5 == 0:
                self.status.set(f"Building PDF... {i}/{len(self.png_paths)}")
                self.root.update()
        writer.close()

        if not self.keep_png.get():
            self._cleanup_pngs()

        self.last_pdf = pdf_path
        self.open_pdf_btn.config(state="normal")
        warn = ""
        if self.mode == "scroll" and self.repairs:
            warn += (f"\nAdobe was drawing slowly, so the settle time was raised to "
                     f"{self.delay_s:.2f}s partway through. Set it there next time.")
        if self.mode == "scroll" and (self.jumps or self.forced_cuts):
            warn = (f"\nNote: {self.jumps} unexpected jump(s), {self.forced_cuts} forced page cut(s). "
                    "Check those pages. Make sure the view is Enable Scrolling (Single Page Continuous) with gaps "
                    "between pages visible.")
        self.reset(f"{reason}  Done: {len(self.png_paths)} pages saved to:\n{pdf_path}{warn}")

    def _cleanup_pngs(self):
        for p in getattr(self, "png_paths", []):
            try:
                p.unlink()
            except Exception:
                pass
        try:
            self.work_dir.rmdir()
        except Exception:
            pass

    def reset(self, msg):
        self.root.deiconify()
        self.root.lift()
        self.status.set(msg)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
