from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# Qt's video backend prints the ffmpeg stream banner — codec, bitrate, the
# whole signed googlevideo URL — to the console for every stream it opens. That
# is two screenfuls per video, it looks like an error to anyone reading the log,
# and it puts a URL carrying the viewer's IP address into a terminal people
# paste into bug reports. Silenced here, before Qt initialises its logging.
# An explicit setting from the user is left alone.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping JARVIS from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

try:
    from core.avatar import HoloAvatar
except Exception:      # pragma: no cover — HUD must never die over cosmetics
    HoloAvatar = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "MARK LV"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 1220, 780
_MIN_W,     _MIN_H     = 880, 610
_LEFT_W  = 198
_RIGHT_W = 282

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    # JARVIS Command Center palette from dashboard/static/app.html.
    BG        = "#05070d"
    PANEL     = "#0a0f1c"
    PANEL2    = "#0f1627"
    BORDER    = "#141c33"
    BORDER_B  = "#00e5ff"
    BORDER_A  = "#1a2340"
    PRI       = "#00e5ff"
    PRI_DIM   = "#3d7eff"
    PRI_GHO   = "#0a2434"
    VIOLET    = "#8b6bff"
    ACC       = "#ff6b6b"   # crimson alert
    ACC2      = "#ffb84e"   # amber warn
    GREEN     = "#40e39f"   # mint ok
    GREEN_D   = "#32bf7f"   # mint deep
    RED       = "#ff6b6b"   # crimson
    MUTED_C   = "#ff9a9a"   # muted crimson
    TEXT      = "#e8ecf4"   # ink
    TEXT_DIM  = "#5f6a82"
    TEXT_MED  = "#9aa3b7"
    WHITE     = "#ffffff"
    DARK      = "#030509"   # near-black backdrop
    BAR_BG    = "#141c33"   # metric bar track


def _sans_family() -> str:
    """Pick the best available sans-serif family for the OS."""
    try:
        have = set(QFontDatabase.families())
    except Exception:
        have = set()
    # Spec: Inter / Plus Jakarta Sans first, then platform natives.
    for p in ("Inter", "Plus Jakarta Sans", "SF Pro Display", "SF Pro Text",
              ".AppleSystemUIFont", "Helvetica Neue",
              "Segoe UI Variable Display", "Segoe UI",
              "Roboto", "Ubuntu", "DejaVu Sans", "Arial"):
        if p in have:
            return p
    return "Arial"


_HEAD_FAMILY: str | None = None
def HEAD() -> str:
    """Display face used by the Command Center's Space Grotesk headings."""
    global _HEAD_FAMILY
    if _HEAD_FAMILY is not None:
        return _HEAD_FAMILY
    try:
        have = set(QFontDatabase.families())
    except Exception:
        have = set()
    for p in ("Space Grotesk", "Inter", "Plus Jakarta Sans"):
        if p in have:
            _HEAD_FAMILY = p
            return p
    _HEAD_FAMILY = SANS()
    return _HEAD_FAMILY


_SANS = None
def SANS() -> str:
    global _SANS
    if _SANS is None:
        _SANS = _sans_family()
    return _SANS


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A", "VIOLET",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


def _glass_card_style(selector: str, radius: int = 16) -> str:
    return f"""
        {selector} {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(255,255,255,12), stop:1 rgba(255,255,255,4));
            border: 1px solid rgba(255,255,255,18);
            border-radius: {radius}px;
        }}
    """


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._get_gpu()
            tmp = self._get_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(240, 190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        # The holographic head that fills the HUD. If it could not be imported
        # we fall back to the old glowing core so the panel is never empty.
        self._avatar = None
        if HoloAvatar is not None:
            try:
                self._avatar = HoloAvatar()
            except Exception:
                self._avatar = None

        # Which centrepiece to draw. Read once here and changed live by the
        # settings toggle; the avatar object is kept either way so switching
        # back is instant and costs no reload.
        try:
            from memory.config_manager import get_hud_style
            self.hud_style = get_hud_style() or "reactor"
        except Exception:
            self.hud_style = "reactor"
        self._core_phase = 0.0
        # Dashboard metrics — pushed in by MainWindow after each activity.
        self._m_msgs = 0
        self._m_tokens = 0
        self._m_latency = 0.0
        self._m_tokens_hist = [0] * 32
        self._m_msgs_hist = [0] * 32
        self._m_latency_hist = [0.0] * 32
        self._m_session_start = time.time()

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._step_t     = time.time()
        self._blink      = True
        self._blink_tick = 0

        # Rescaled-face cache: the smooth rescale is expensive, so we keep the
        # last result and only rebuild it when the (quantised) size changes.

        # Static grid-dot layer, pre-rendered once per size/theme into a pixmap
        # so paintEvent blits it in one call instead of thousands of drawPoint()s.
        self._grid_cache: QPixmap | None = None
        self._grid_key = None
        # Repaint throttle counter (idle frames drop to ~20 Hz — see _step()).
        self._paint_tick = 0

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        self._audio_history = [0.0] * 64
        # (frames, start_time, hop) posted by the playback thread — see
        # push_visemes(). None means "no schedule; use the plain level".
        self._visemes = None
        self._vis_i = None        # first schedule frame not yet handed to the mouth
        self._base_scale = 1.0    # slow "breathing" target; amp is added per-frame
        self._base_halo  = 55.0

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def set_metrics(self, *, msgs=0, tokens=0, latency_ms=0.0,
                    tokens_hist=None, msgs_hist=None, latency_hist=None,
                    session_start=None) -> None:
        self._m_msgs = int(msgs)
        self._m_tokens = int(tokens)
        self._m_latency = float(latency_ms)
        if tokens_hist is not None:
            self._m_tokens_hist = list(tokens_hist)[-32:]
        if msgs_hist is not None:
            self._m_msgs_hist = list(msgs_hist)[-32:]
        if latency_hist is not None:
            self._m_latency_hist = list(latency_hist)[-32:]
        if session_start is not None:
            self._m_session_start = float(session_start)
        self.update()

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: hand over a schedule of (level, openness, width) frames.

        The playback thread writes up to 200 ms of audio in one go, so a single
        averaged level would only move the mouth five times a second — enough to
        flap, nowhere near enough to articulate. It instead posts the whole
        slice's worth of 20 ms frames here and `_step()` plays them out against
        the wall clock, in step with the audio going to the speakers.

        `at` is the wall-clock time this batch will *begin to sound*, which the
        caller tracks as a playback cursor. It is not the time of the call, and
        the difference is the whole point: `stream.write` returns once the buffer
        accepts the samples, so consecutive batches are handed over far faster
        than they play. Anchoring each one to "now" made every batch start while
        its predecessor was still sounding, so each schedule replaced the last
        after a couple of frames and the mouth only ever played the opening
        instant of every 200 ms — the reason it did not match the words.

        Successive batches are therefore *appended* into one continuous
        timeline, not swapped in. A paragraph is one schedule; the mouth stops
        falling into a gap at every chunk boundary and having to climb back out.
        """
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    # Where in the existing timeline does this batch land?
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        # Continues (or slightly overlaps) what is already
                        # queued: extend rather than restart. Drop whatever has
                        # already been played so the list cannot grow without
                        # bound over a long reply.
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                            if self._vis_i is not None:
                                self._vis_i = max(0, self._vis_i - played)
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
            self._vis_i = None
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _make_grid(self, W: int, H: int) -> QPixmap:
        """Cached ambient field matching the Command Center's layered backdrop."""
        pm = QPixmap(max(1, W), max(1, H))
        pm.fill(qcol(C.BG))
        gp = QPainter(pm)
        gp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for x, y, radius, color, alpha in (
            (W * 0.20, H * 0.30, max(W, H) * 0.62, C.PRI, 22),
            (W * 0.82, H * 0.73, max(W, H) * 0.58, C.PRI_DIM, 20),
            (W * 0.58, H * 0.20, max(W, H) * 0.36, C.VIOLET, 10),
        ):
            glow = QRadialGradient(x, y, radius)
            center = QColor(color)
            center.setAlpha(alpha)
            glow.setColorAt(0.0, center)
            middle = QColor(color)
            middle.setAlpha(alpha // 3)
            glow.setColorAt(0.55, middle)
            edge = QColor(C.BG)
            edge.setAlpha(0)
            glow.setColorAt(1.0, edge)
            gp.fillRect(0, 0, W, H, QBrush(glow))
        grid_color = QColor(255, 255, 255, 6)
        gp.setPen(QPen(grid_color, 1))
        for x in range(0, W, 48):
            gp.drawLine(x, 0, x, H)
        for y in range(0, H, 48):
            gp.drawLine(0, y, W, y)
        gp.end()
        return pm

    def _step(self):
        self._tick += 1
        now = time.time()

        # ── Live audio reactivity ────────────────────────────────────────────
        # A viseme schedule, if one is playing, gives both the level and the
        # mouth shape for this exact instant; otherwise fall back to the peak
        # level the audio threads pushed in.
        v_open = v_wide = v_level = None
        v_seq = None
        sched = self._visemes
        if sched is not None:
            frames, t0, hop = sched
            i = int((now - t0) / hop)
            if 0 <= i < len(frames):
                # Hand over *every* frame since the last tick, not just the one
                # under the cursor. This timer runs at 60 Hz but the paint is
                # throttled and the machine may be busy, so a tick can span two
                # or three 20 ms frames — and a consonant closure is only two
                # frames long. Sampling one and discarding the rest is how the
                # closures between words went missing.
                j = self._vis_i if self._vis_i is not None else i
                v_seq = frames[max(0, j):i + 1]
                self._vis_i = max(j, i + 1)
                v_level, v_open, v_wide = frames[i]
                if v_seq:
                    peak = max(f[0] for f in v_seq)
                    if peak > self._live_amp:
                        self._live_amp = peak
            elif i >= len(frames):
                self._visemes = None        # schedule spent
                self._vis_i = None

        # Audio threads push peaks into _live_amp; decay it toward silence so
        # gaps between chunks fade out instead of freezing, then smooth it.
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        amp = self._amp_disp
        self._audio_history.append(0.0 if self.muted else amp)
        del self._audio_history[:-64]

        # The avatar animates off the very same smoothed level the waveform
        # uses — one audio source, so the mouth can never drift out of sync.
        dt = now - self._step_t
        self._step_t = now
        # Integrated, not derived from absolute time: multiplying wall-clock by
        # a rate that changes with state jumps the rings the instant JARVIS
        # starts talking. Same lesson the head's sway taught.
        self._core_phase += min(0.10, max(0.0, dt))

        if self._avatar is not None and self.hud_style == "face":
            self._avatar.step(dt, amp, speaking=self.speaking,
                              muted=self.muted, state=self.state,
                              v_open=v_open, v_wide=v_wide or 0.0,
                              v_level=v_level, v_seq=v_seq,
                              v_hop=(sched[2] if sched is not None else 0.02))
        else:
            # Fallback core: slow "breathing" base target, lifted by the level.
            if now - self._last_t > (0.12 if self.speaking else 0.5):
                if self.speaking:
                    self._base_scale = 1.03
                    self._base_halo  = 122.0
                elif self.muted:
                    self._base_scale = random.uniform(0.998, 1.002)
                    self._base_halo  = random.uniform(15, 28)
                else:
                    self._base_scale = random.uniform(1.001, 1.008)
                    self._base_halo  = random.uniform(48, 68)
                self._last_t = now

            if self.muted:
                self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
            elif self.speaking:
                self._tgt_scale = self._base_scale + amp * 0.13
                self._tgt_halo  = self._base_halo  + amp * 95.0
            else:
                self._tgt_scale = self._base_scale + amp * 0.06
                self._tgt_halo  = self._base_halo  + amp * 75.0

            sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.15)
            self._scale += (self._tgt_scale - self._scale) * sp
            self._halo  += (self._tgt_halo  - self._halo)  * sp

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Repaint throttling — advancing the animation state above is cheap at
        # 60 Hz, but the paint is heavy. Active (speaking, audio, thinking) runs
        # at ~30 Hz, which is the frame rate animation has used for talking
        # characters forever and is indistinguishable here; idle drops to ~20 Hz
        # so a sleeping HUD stops pinning a CPU core. The visuals stay smooth
        # either way because the animation state keeps stepping at 60 Hz.
        self._paint_tick = (self._paint_tick + 1) % 6
        active = (self.speaking or amp > 0.02
                  or self.state in ("THINKING", "PROCESSING"))
        if _blinked or (self._paint_tick % 2 == 0 if active
                        else self._paint_tick % 3 == 0):
            # Nothing is on screen when the window is hidden or minimised, so
            # rendering the avatar into it is pure waste — and this app is meant
            # to sit running all day. The animation state above keeps stepping,
            # so it picks up mid-motion instead of snapping when you come back.
            if self._on_screen():
                self.update()

    def _on_screen(self) -> bool:
        """True only when this canvas can actually be seen by the user."""
        try:
            if not self.isVisible():
                return False
            win = self.window()
            return not (win.isMinimized() or win.isHidden())
        except Exception:
            return True      # never let a visibility check stop the HUD drawing

    # ── reactor core ─────────────────────────────────────────────────────────
    # The centrepiece for anyone who did not want a face looking back at them.
    # Built from the same budget as the head — software QPainter, no GPU — and
    # from the same principle: everything on it means something. The rings turn
    # at a rate the state sets, the spectrum ring is the real audio level, and
    # the core brightens with the voice. Nothing here is decoration that moves
    # for its own sake, which is what made the old glowing orb feel dead.

    def _core_colours(self):
        if self.muted:
            return qcol(C.MUTED_C), qcol(C.MUTED_C)
        if self.speaking:
            return qcol(C.PRI), qcol(C.ACC)
        if self.state in ("THINKING", "PROCESSING"):
            return qcol(C.PRI), qcol(C.ACC2)
        if self.state == "LISTENING":
            return qcol(C.PRI), qcol(C.GREEN)
        return qcol(C.PRI), qcol(C.PRI_DIM)

    def _paint_tech_interface(
        self, p: QPainter, cx: float, cy: float, radius: float,
        primary: QColor, accent: QColor,
    ) -> None:
        level = 0.0 if self.muted else max(0.0, min(1.0, self._amp_disp))
        phase = self._core_phase
        scale = 1.0 + min(0.035, level * 0.035)
        r = radius * scale

        aura = QRadialGradient(cx, cy, r * 1.6)
        center = QColor(primary)
        center.setAlpha(32 + int(level * 48))
        aura.setColorAt(0.0, center)
        mid = QColor(primary)
        mid.setAlpha(14)
        aura.setColorAt(0.55, mid)
        aura.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(aura))
        p.drawEllipse(QPointF(cx, cy), r * 1.6, r * 1.6)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # A calibrated outer dial frames the live signal without implying
        # readings the app does not actually collect.
        for i in range(72):
            angle = math.radians(i * 5 - 90)
            major = i % 6 == 0
            inner = r * (0.96 if major else 0.985)
            outer = r * (1.055 if major else 1.025)
            color = QColor(primary)
            color.setAlpha(120 if major else 34)
            p.setPen(QPen(color, 1.2 if major else 0.7))
            p.drawLine(
                QPointF(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                QPointF(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer),
            )

        ring_rect = QRectF(cx - r * 0.90, cy - r * 0.90, r * 1.8, r * 1.8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(primary.red(), primary.green(), primary.blue(), 38), 1))
        p.drawEllipse(ring_rect)

        # Segmented rings rotate at a state-dependent rate; the actual current
        # audio envelope controls the separate signal trace below.
        rate = 48.0 if self.state in ("THINKING", "PROCESSING") else 17.0
        if self.speaking:
            rate = 30.0
        rotation = phase * rate
        for ring_index, (ring_radius, count, span, direction) in enumerate((
            (0.81, 24, 7.0, 1.0),
            (0.69, 16, 10.0, -0.65),
        )):
            rect = QRectF(
                cx - r * ring_radius, cy - r * ring_radius,
                r * ring_radius * 2, r * ring_radius * 2,
            )
            for i in range(count):
                start = rotation * direction + i * (360.0 / count)
                col = QColor(accent if (i + ring_index) % 4 == 0 else primary)
                col.setAlpha(160 if (i + ring_index) % 4 == 0 else 76)
                p.setPen(QPen(col, 2.0 if ring_index == 0 else 1.5))
                p.drawArc(rect, int(start * 16), int(span * 16))

        # This trace uses the same smoothed microphone/playback level as the
        # assistant's audio pipeline; silence stays visually quiet.
        trace = QPainterPath()
        samples = self._audio_history
        sample_count = len(samples)
        for i, sample in enumerate(samples):
            angle = (i / max(1, sample_count - 1)) * math.tau - math.pi / 2
            displacement = min(r * 0.105, max(0.0, sample) * r * 0.42)
            distance = r * 0.565 + displacement
            point = QPointF(
                cx + math.cos(angle) * distance,
                cy + math.sin(angle) * distance,
            )
            if i == 0:
                trace.moveTo(point)
            else:
                trace.lineTo(point)
        trace.closeSubpath()
        trace_color = QColor(primary)
        trace_color.setAlpha(220 if level > 0.015 else 82)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(trace_color, 1.6))
        p.drawPath(trace)

        # A faceted processor core replaces the human face. Its glow and
        # breathing respond only to real assistant state and audio activity.
        core_radius = r * (0.39 + level * 0.025)
        core = QPainterPath()
        for i in range(6):
            angle = math.radians(-90 + i * 60)
            point = QPointF(
                cx + math.cos(angle) * core_radius,
                cy + math.sin(angle) * core_radius,
            )
            if i == 0:
                core.moveTo(point)
            else:
                core.lineTo(point)
        core.closeSubpath()
        core_fill = QRadialGradient(cx - core_radius * 0.25, cy - core_radius * 0.3,
                                    core_radius * 1.7)
        fill = QColor(primary)
        fill.setAlpha(38 + int(level * 46))
        core_fill.setColorAt(0.0, fill)
        deep = QColor(C.PANEL)
        deep.setAlpha(245)
        core_fill.setColorAt(1.0, deep)
        p.setPen(QPen(QColor(primary.red(), primary.green(), primary.blue(), 200), 1.2))
        p.setBrush(QBrush(core_fill))
        p.drawPath(core)

        for i in range(3):
            inner_radius = core_radius * (0.77 - i * 0.19)
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(),
                                 max(60, 170 - i * 35)), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), inner_radius, inner_radius)

        center_radius = core_radius * (0.18 + level * 0.04)
        center_glow = QRadialGradient(cx, cy, core_radius * 0.48)
        center_glow.setColorAt(0.0, QColor(225, 252, 255, 230))
        center_glow.setColorAt(0.35, QColor(primary.red(), primary.green(),
                                            primary.blue(), 190))
        center_glow.setColorAt(1.0, QColor(primary.red(), primary.green(),
                                            primary.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(center_glow))
        p.drawEllipse(QPointF(cx, cy), core_radius * 0.48, core_radius * 0.48)
        p.setBrush(QBrush(accent))
        p.drawEllipse(QPointF(cx, cy), center_radius, center_radius)

        # Four short circuit traces connect the core to the measurement dial.
        for angle_deg in (0, 90, 180, 270):
            angle = math.radians(angle_deg + rotation * 0.12)
            start = QPointF(
                cx + math.cos(angle) * core_radius * 1.08,
                cy + math.sin(angle) * core_radius * 1.08,
            )
            end = QPointF(
                cx + math.cos(angle) * r * 0.54,
                cy + math.sin(angle) * r * 0.54,
            )
            p.setPen(QPen(QColor(primary.red(), primary.green(),
                                 primary.blue(), 92), 1.0))
            p.drawLine(start, end)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(primary))
            p.drawEllipse(end, 2.0, 2.0)

        p.setPen(QPen(qcol(C.TEXT), 1))
        name_font = QFont(HEAD(), max(7, int(r * 0.12)), QFont.Weight.DemiBold)
        p.setFont(name_font)
        p.drawText(QRectF(cx - r * 0.48, cy + r * 1.17, r * 0.96, 17),
                   Qt.AlignmentFlag.AlignCenter, "JARVIS")
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.setFont(QFont(SANS(), max(6, int(r * 0.067))))
        p.drawText(QRectF(cx - r * 0.66, cy + r * 1.34, r * 1.32, 14),
                   Qt.AlignmentFlag.AlignCenter, "VOICE INTERFACE")

    # ── glass tile helpers ────────────────────────────────────────────────
    def _card_font(self, size: int, bold: bool = False) -> QFont:
        f = QFont(SANS(), size)
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        if bold:
            f.setWeight(QFont.Weight.DemiBold)
        return f

    def _paint_card(self, p: QPainter, rect: QRectF, title: str = "",
                     accent: QColor | None = None) -> QRectF:
        """Frosted-glass card: rounded rect, sheen gradient, border with
        gradient top-highlight (glass gleam), title in caps. Returns
        the inner content rect below the title."""
        r = 16.0
        # Soft outer shadow — draw two very faint shadow rects offset.
        for k, a in ((3, 8), (6, 4)):
            sh = QColor(0, 0, 0, a)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(sh))
            p.drawRoundedRect(QRectF(rect.left(), rect.top() + k,
                                       rect.width(), rect.height()),
                                r, r)
        # Fill — subtle top-to-bottom sheen so the card reads as glass.
        g = QLinearGradient(rect.left(), rect.top(),
                             rect.left(), rect.bottom())
        g.setColorAt(0.00, QColor(255, 255, 255, 20))
        g.setColorAt(0.55, QColor(255, 255, 255, 10))
        g.setColorAt(1.00, QColor(255, 255, 255, 5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(rect, r, r)
        # Border — gradient hairline (brighter top, dimmer bottom) reads
        # as ambient light hitting a glass edge.
        bg = QLinearGradient(rect.left(), rect.top(),
                              rect.left(), rect.bottom())
        bg.setColorAt(0.00, QColor(255, 255, 255, 65))
        bg.setColorAt(0.50, QColor(255, 255, 255, 26))
        bg.setColorAt(1.00, QColor(255, 255, 255, 16))
        pen = QPen(QBrush(bg), 1)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, r, r)
        # Inner top-edge gleam — 1-px bright line just inside the top border.
        gleam = QPen(QColor(255, 255, 255, 60), 1)
        p.setPen(gleam)
        # Approximate the rounded top with a small horizontal segment.
        p.drawLine(QLineF(rect.left() + r * 0.85,
                           rect.top() + 1.4,
                           rect.right() - r * 0.85,
                           rect.top() + 1.4))
        # Optional accent dot next to title.
        pad = 18.0
        title_h = 18.0
        if title:
            if accent is not None:
                # Pulsing dot ring around the accent for extra life.
                p.setPen(Qt.PenStyle.NoPen)
                halo = QColor(accent); halo.setAlpha(90)
                p.setBrush(QBrush(halo))
                p.drawEllipse(QRectF(rect.left() + pad - 3,
                                      rect.top() + pad + 0,
                                      12, 12))
                p.setBrush(QBrush(accent))
                p.drawEllipse(QRectF(rect.left() + pad,
                                      rect.top() + pad + 3, 6, 6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                tx = rect.left() + pad + 14
            else:
                tx = rect.left() + pad
            p.setPen(QPen(qcol(C.TEXT_DIM), 1))
            f = self._card_font(9, bold=True)
            f.setCapitalization(QFont.Capitalization.AllUppercase)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6)
            p.setFont(f)
            p.drawText(QPointF(tx, rect.top() + pad + 9), title)
        inner = QRectF(rect.left() + pad,
                        rect.top() + pad + title_h + 4,
                        rect.width() - pad * 2,
                        rect.height() - pad * 2 - title_h - 4)
        return inner

    def _paint_sparkline(self, p: QPainter, rect: QRectF,
                          values, color: QColor, filled: bool = True):
        if not values or rect.width() < 4 or rect.height() < 4:
            return
        vs = list(values)
        lo, hi = min(vs), max(vs)
        if hi - lo < 1e-6:
            hi = lo + 1.0
        n = len(vs)
        pts = []
        for i, v in enumerate(vs):
            x = rect.left() + (i / max(1, n - 1)) * rect.width()
            y = rect.bottom() - 3 - (v - lo) / (hi - lo) * (rect.height() - 6)
            pts.append(QPointF(x, y))
        # Smoothed path — Catmull-Rom-ish curve via cubic beziers so
        # sparkline reads as flowing rather than saw-tooth.
        line_path = QPainterPath()
        line_path.moveTo(pts[0])
        for i in range(1, len(pts)):
            prev = pts[i - 1]
            cur = pts[i]
            mid_x = (prev.x() + cur.x()) / 2
            line_path.cubicTo(mid_x, prev.y(), mid_x, cur.y(),
                                cur.x(), cur.y())
        if filled and len(pts) > 1:
            fill_path = QPainterPath(line_path)
            fill_path.lineTo(rect.right(), rect.bottom())
            fill_path.lineTo(rect.left(), rect.bottom())
            fill_path.closeSubpath()
            fg = QLinearGradient(rect.left(), rect.top(),
                                    rect.left(), rect.bottom())
            fc_top = QColor(color); fc_top.setAlpha(90)
            fc_bot = QColor(color); fc_bot.setAlpha(0)
            fg.setColorAt(0.00, fc_top)
            fg.setColorAt(1.00, fc_bot)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fg))
            p.drawPath(fill_path)
        p.setBrush(Qt.BrushStyle.NoBrush)
        line = QColor(color); line.setAlpha(240)
        pen = QPen(line, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPath(line_path)
        # Trailing dot with halo
        end = pts[-1]
        halo = QColor(color); halo.setAlpha(80)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(end, 6.0, 6.0)
        p.setBrush(QBrush(line))
        p.drawEllipse(end, 3.0, 3.0)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_gauge(self, p: QPainter, rect: QRectF, pct: float,
                     color: QColor):
        cx = rect.center().x()
        cy = rect.bottom() - 4
        r = min(rect.width() / 2 - 8, rect.height() - 8)
        if r < 10:
            return
        # Track
        p.setPen(QPen(QColor(255, 255, 255, 34), 6,
                       cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2),
                   180 * 16, -180 * 16)
        # Fill
        ang = -180.0 * max(0.0, min(1.0, pct / 100.0))
        p.setPen(QPen(color, 6, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2),
                   180 * 16, int(ang * 16))
        # Value text
        p.setFont(self._card_font(20, bold=True))
        p.setPen(QPen(qcol(C.TEXT), 1))
        p.drawText(QRectF(cx - r, cy - r * 0.55, r * 2, r * 0.6),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{int(pct)}%")

    def _paint_line_chart(self, p: QPainter, rect: QRectF, values,
                           color: QColor):
        if not values or len(values) < 2:
            return
        vs = list(values)
        lo, hi = min(vs), max(vs)
        if hi - lo < 1e-6:
            hi = lo + 1.0
        n = len(vs)
        pts = []
        for i, v in enumerate(vs):
            x = rect.left() + (i / max(1, n - 1)) * rect.width()
            y = rect.bottom() - (v - lo) / (hi - lo) * rect.height()
            pts.append(QPointF(x, y))
        # Baseline
        p.setPen(QPen(QColor(255, 255, 255, 20), 1))
        p.drawLine(QLineF(rect.left(), rect.bottom(),
                           rect.right(), rect.bottom()))
        # Line
        p.setPen(QPen(color, 1.8))
        for i in range(1, len(pts)):
            p.drawLine(pts[i - 1], pts[i])
        # Recent-point dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(pts[-1], 2.5, 2.5)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _state_display(self):
        """(label, color, subtitle) for the big status tile."""
        if self.muted:
            return "Muted", qcol(C.MUTED_C), "microphone off"
        if self.speaking:
            return "Speaking", qcol(C.PRI), "delivering response"
        if self.state == "THINKING":
            return "Thinking", qcol(C.ACC2), "analysing request"
        if self.state == "PROCESSING":
            return "Working", qcol(C.ACC2), "executing task"
        if self.state == "LISTENING":
            return "Listening", qcol(C.GREEN), "ready for input"
        if self.state == "INITIALISING":
            return "Starting", qcol(C.PRI_DIM), "loading models"
        return self.state.title(), qcol(C.PRI), ""

    def _paint_status_tile(self, p: QPainter, rect: QRectF):
        label, col, sub = self._state_display()
        inner = self._paint_card(p, rect, title="Status", accent=col)
        # Big label
        p.setFont(self._card_font(30, bold=True))
        p.setPen(QPen(qcol(C.TEXT), 1))
        p.drawText(QRectF(inner.left(), inner.top() - 2,
                           inner.width(), 40),
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter, label)
        # Subtitle
        p.setFont(self._card_font(12))
        p.setPen(QPen(qcol(C.TEXT_MED), 1))
        p.drawText(QRectF(inner.left(), inner.top() + 42,
                           inner.width(), 18),
                    Qt.AlignmentFlag.AlignLeft, sub)
        # Since HH:MM (from session start).
        since = time.strftime("%H:%M",
                                time.localtime(getattr(self,
                                    "_m_session_start", time.time())))
        p.setFont(self._card_font(9, bold=True))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(inner.left(), inner.top() + 64,
                           inner.width(), 14),
                    Qt.AlignmentFlag.AlignLeft,
                    f"session since {since}")
        # Assistant name + protocol tag at bottom.
        name = (self._assistant_name or "JARVIS")
        p.setFont(self._card_font(10, bold=True))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(inner.left(), inner.bottom() - 16,
                           inner.width(), 16),
                    Qt.AlignmentFlag.AlignLeft,
                    f"{name} · {APP_PROTOCOL}")
        # ── Pulsing halo indicator, concentric rings + core ─────────────
        r0 = min(inner.height() / 2.0, 46.0)
        rx = inner.right() - r0 - 8
        ry = inner.top() + inner.height() / 2
        pulse = 0.5 + 0.5 * math.sin(self._core_phase * 2.6)
        # Outer ring (breathing)
        p.setPen(Qt.PenStyle.NoPen)
        for k, (rr, a) in enumerate(((1.00, 22 + 34 * pulse),
                                        (0.82, 44 + 60 * pulse),
                                        (0.60, 90 + 100 * pulse))):
            hc = QColor(col); hc.setAlpha(int(a))
            p.setBrush(QBrush(hc))
            p.drawEllipse(QPointF(rx, ry), r0 * rr, r0 * rr)
        # Bright core
        core_c = QColor(col); core_c.setAlpha(245)
        p.setBrush(QBrush(core_c))
        p.drawEllipse(QPointF(rx, ry), r0 * 0.30, r0 * 0.30)
        # White highlight spot on the core (glass sheen).
        hi = QColor(255, 255, 255, 180)
        p.setBrush(QBrush(hi))
        p.drawEllipse(QPointF(rx - r0 * 0.09, ry - r0 * 0.10),
                       r0 * 0.10, r0 * 0.10)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_metric_tile(self, p: QPainter, rect: QRectF, *,
                            title: str, value: str, unit: str,
                            color: QColor, history, chart: str = "spark"):
        inner = self._paint_card(p, rect, title=title, accent=color)
        from PyQt6.QtGui import QFontMetrics
        # Value (large) + unit (inline muted).
        vf = self._card_font(30, bold=True)
        p.setFont(vf)
        p.setPen(QPen(qcol(C.TEXT), 1))
        p.drawText(QRectF(inner.left(), inner.top() - 4,
                           inner.width(), 42),
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter, value)
        fm_val = QFontMetrics(vf)
        vw = fm_val.horizontalAdvance(value)
        if unit:
            p.setFont(self._card_font(11))
            p.setPen(QPen(qcol(C.TEXT_MED), 1))
            p.drawText(QPointF(inner.left() + vw + 6,
                                inner.top() + 22), unit)
        # Trend indicator top-right (except gauge — no history).
        if chart != "gauge":
            trend_txt, trend_col = self._trend(history)
            if trend_txt:
                tf = self._card_font(9, bold=True)
                p.setFont(tf)
                p.setPen(QPen(trend_col, 1))
                fm_t = QFontMetrics(tf)
                tw = fm_t.horizontalAdvance(trend_txt) + 14
                pill_rect = QRectF(inner.right() - tw,
                                     inner.top() - 4,
                                     tw, 22)
                pill_fill = QColor(trend_col); pill_fill.setAlpha(30)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(pill_fill))
                p.drawRoundedRect(pill_rect, 11, 11)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(trend_col, 1))
                p.setFont(tf)
                p.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter,
                            trend_txt)
        # Chart in the bottom half.
        chart_top = inner.top() + 44
        chart_rect = QRectF(inner.left(), chart_top,
                             inner.width(),
                             max(20.0, inner.bottom() - chart_top))
        if chart == "line":
            self._paint_line_chart(p, chart_rect, history, color)
        elif chart == "gauge":
            self._paint_gauge(p, chart_rect, float(value.rstrip("% ")
                                                     or "0"), color)
        else:
            self._paint_sparkline(p, chart_rect, history, color, filled=True)

    def _paint_waveform_tile(self, p: QPainter, rect: QRectF):
        col = qcol(C.PRI)
        if self.muted:
            col = qcol(C.MUTED_C)
        elif self.speaking:
            col = qcol(C.PRI)
        elif self.state == "LISTENING":
            col = qcol(C.GREEN)
        inner = self._paint_card(p, rect, title="Audio", accent=col)
        N = max(28, int(inner.width() // 10))
        bw = max(4.0, inner.width() / (N * 1.35))
        gap = bw * 0.35
        total = N * (bw + gap) - gap
        x0 = inner.left() + (inner.width() - total) / 2
        amp = self._amp_disp
        mid = (N - 1) / 2.0
        for i in range(N):
            if self.muted:
                hgt = 3.0
            else:
                env = (1.0 - abs(i - mid) / mid) ** 0.7
                shim = 0.55 + 0.45 * math.sin(self._tick * 0.18 + i * 0.7)
                idle = 3.0 + 2.0 * math.sin(self._tick * 0.09 + i * 0.6)
                hgt = max(3.0, min(inner.height() - 8,
                                     idle + amp * 42.0 * env * shim))
            r = QRectF(x0 + i * (bw + gap),
                        inner.bottom() - hgt, bw, hgt)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(r, bw / 2, bw / 2)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _fmt_int(self, n: int) -> str:
        """Compact int formatting — 12,345 → 12.3k, 4,300,000 → 4.3M."""
        n = int(n)
        if n < 1000:
            return f"{n}"
        if n < 1_000_000:
            v = n / 1000.0
            return f"{v:.1f}k" if v < 100 else f"{v:.0f}k"
        v = n / 1_000_000.0
        return f"{v:.1f}M" if v < 100 else f"{v:.0f}M"

    def _trend(self, hist) -> tuple[str, QColor]:
        """Compute a compact trend indicator (▲/▼ + %) from a rolling series."""
        vs = [v for v in (hist or []) if isinstance(v, (int, float))]
        if len(vs) < 4:
            return "", qcol(C.TEXT_DIM)
        recent = sum(vs[-4:]) / 4.0
        prior  = sum(vs[-8:-4]) / 4.0 if len(vs) >= 8 else vs[0]
        if prior < 1e-6:
            if recent > 0:
                return "▲ new", qcol(C.GREEN)
            return "flat", qcol(C.TEXT_DIM)
        delta = (recent - prior) / prior * 100.0
        if delta >= 1.5:
            return f"▲ {delta:.0f}%", qcol(C.GREEN)
        if delta <= -1.5:
            return f"▼ {abs(delta):.0f}%", qcol(C.ACC)
        return "flat", qcol(C.TEXT_DIM)

    def _fmt_dur(self, seconds: float) -> str:
        s = int(max(0, seconds))
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}"

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        # Soft stage lighting; all motion in the center is driven by the live
        # assistant state and audio pipeline.
        _gkey = (W, H, C.PRI, C.BG)
        if self._grid_cache is None or self._grid_key != _gkey:
            self._grid_cache = self._make_grid(W, H)
            self._grid_key = _gkey
        p.drawPixmap(0, 0, self._grid_cache)
        cx, cy = W / 2, H * 0.43
        primary, accent = self._core_colours()
        bg = qcol(C.BG)
        radius = max(46.0, min(W * 0.31, H * 0.27, 132.0))
        self._paint_tech_interface(p, cx, cy, radius, primary, accent)

        label, state_color, _ = self._state_display()
        p.setPen(QPen(state_color, 1))
        p.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
        status_rect = QRectF(0, H - 56, W, 18)
        p.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, label.upper())

        meter_w = min(360.0, W * 0.52)
        meter_x = (W - meter_w) / 2
        meter_y = H - 24
        p.setPen(Qt.PenStyle.NoPen)
        track = QColor(primary)
        track.setAlpha(48)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(QRectF(meter_x, meter_y, meter_w, 2), 1, 1)
        history = self._audio_history
        wave = QPainterPath()
        mid_y = meter_y + 1
        for i, sample in enumerate(history):
            x = meter_x + (i / (len(history) - 1)) * meter_w
            envelope = min(1.0, max(0.0, sample * 1.8))
            y = mid_y - envelope * min(18.0, H * 0.075)
            if i == 0:
                wave.moveTo(x, y)
            else:
                wave.lineTo(x, y)
        wave_pen = QPen(state_color, 1.5)
        wave_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        wave_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(wave_pen)
        p.drawPath(wave)
        p.end()

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return          # unchanged — skip the repaint
        self._value = v
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # Glass row
        p.setBrush(QBrush(QColor(255, 255, 255, 10)))
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 10, 10)

        bar_h   = 4
        bar_y   = H - bar_h - 7
        bar_w   = W - 16
        bar_x   = 8
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(QColor(255, 255, 255, 18)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.ACC)
        elif self._value > 65:
            bar_col = qcol(C.ACC2)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        lf = QFont(SANS(), 9, QFont.Weight.DemiBold)
        lf.setCapitalization(QFont.Capitalization.AllUppercase)
        lf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(lf)
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(10, 4, 60, 18),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    self._label)

        p.setFont(QFont(SANS(), 11, QFont.Weight.DemiBold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 10, 18),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    self._text)

        p.end()

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # Cap scrollback so an hours-long session can't grow the document
        # without bound — keeps memory flat and every insert cheap. Oldest
        # lines drop off the top automatically.
        self.document().setMaximumBlockCount(600)
        self.setFont(QFont(SANS(), 10))
        self.setPlaceholderText(
            "No activity yet.\nType a prompt below or use one of the quick actions to start."
        )
        self.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(255,255,255,4);
                color: {C.TEXT};
                border: 1px solid rgba(255,255,255,14);
                border-radius: 14px;
                padding: 12px;
                selection-background-color: {C.PRI_DIM};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255,255,255,30);
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(255,255,255,60);
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "jarvis"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                # SYS lines are the bulk of the log. Amber fought the cyan HUD
                # and, being a fixed status colour rather than a hue-linked one,
                # stayed amber even after the accent picker retinted everything
                # else. TEXT_MED follows the theme and drops the contrast to a
                # level you can read past.
                "sys":  qcol(C.TEXT_MED),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering or dragging a file over the zone. When idle, skip the repaint
        # entirely instead of redrawing the whole zone 25×/s forever — that idle
        # repaint held the GIL and stole time from the audio/response threads.
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None
        self._canvas.update()
        self.file_selected.emit("")

    def open_file_dialog(self):
        self._browse()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 1
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol(C.PRI_GHO if z._drag_over else (C.PANEL if z._hovering else C.BG))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 9, 9)

        border = C.PRI if z._drag_over or z._hovering else C.BORDER
        p.setPen(QPen(qcol(border), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 9, 9)
        p.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
        p.setPen(QPen(qcol(C.PRI if z._current_file or z._drag_over else C.TEXT_MED), 1))
        if z._drag_over:
            text = "Release to add"
        elif z._current_file:
            name = Path(z._current_file).name
            text = name if len(name) <= 14 else name[:11] + "..."
        else:
            text = "+  Add file"
        text_width = W - 31 if z._current_file else W - 14
        p.drawText(QRectF(7, 0, text_width, H), Qt.AlignmentFlag.AlignCenter, text)
        if z._current_file:
            p.setFont(QFont(SANS(), 9, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.TEXT_DIM), 1))
            p.drawText(
                QRectF(W - 25, 0, 20, H),
                Qt.AlignmentFlag.AlignCenter,
                "×",
            )

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 24:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    # Payload: {"gemini_api_key","os_system","llm_provider","llm_api_key","llm_model"}.
    done = pyqtSignal(dict)

    # Provider → (label, default model, needs_api_key, placeholder).
    _PROVIDERS: list[tuple[str, str, str, bool, str]] = [
        # id,          label,                     default model,                  key?,  placeholder
        ("ollama",     "Ollama (local, default)", "llama3.2",                     False, "no key — local"),
        ("openai",     "OpenAI",                  "gpt-4o-mini",                  True,  "sk-…"),
        ("openrouter", "OpenRouter",              "anthropic/claude-3.5-sonnet",  True,  "sk-or-v1-…"),
        ("groq",       "Groq",                    "llama-3.3-70b-versatile",      True,  "gsk_…"),
        ("anthropic",  "Anthropic",               "claude-opus-4-5",              True,  "sk-ant-…"),
    ]

    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        existing = existing or {}

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = existing.get("os_system") or detected
        self._sel_provider = (existing.get("llm_provider") or "ollama").strip().lower()
        if self._sel_provider not in {p[0] for p in self._PROVIDERS}:
            self._sel_provider = "ollama"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(6)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        def _line_edit(placeholder: str, password: bool = False, prefill: str = "") -> QLineEdit:
            le = QLineEdit()
            if password:
                le.setEchoMode(QLineEdit.EchoMode.Password)
            le.setPlaceholderText(placeholder)
            le.setFont(QFont("Courier New", 10))
            le.setFixedHeight(30)
            le.setText(prefill)
            le.setStyleSheet(f"""
                QLineEdit {{
                    background: #000d12; color: {C.TEXT};
                    border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
                }}
                QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
            """)
            return le

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(4)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)

        # ── Gemini key (voice path, Live API) ──────────────────────────────
        layout.addWidget(_lbl("GEMINI API KEY (voice)", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = _line_edit("AIza…", password=True,
                                     prefill=existing.get("gemini_api_key", ""))
        layout.addWidget(self._key_input)

        # ── Text-LLM backend selector ──────────────────────────────────────
        layout.addSpacing(6)
        sep_llm = QFrame(); sep_llm.setFrameShape(QFrame.Shape.HLine)
        sep_llm.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep_llm)
        layout.addWidget(_lbl("TEXT LLM BACKEND (planner/tools)", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))

        self._provider_combo = QComboBox()
        self._provider_combo.setFont(QFont("Courier New", 9))
        self._provider_combo.setFixedHeight(28)
        self._provider_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        for pid, label, _model, _needs, _ph in self._PROVIDERS:
            self._provider_combo.addItem(label, pid)
        self._provider_combo.setStyleSheet(
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; selection-background-color: {C.PRI_GHO}; }}"
        )
        # Restore current selection.
        for i in range(self._provider_combo.count()):
            if self._provider_combo.itemData(i) == self._sel_provider:
                self._provider_combo.setCurrentIndex(i)
                break
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        layout.addWidget(self._provider_combo)

        self._llm_key_input = _line_edit("", password=True,
                                         prefill=existing.get("llm_api_key", ""))
        layout.addWidget(self._llm_key_input)

        self._llm_model_input = _line_edit("", password=False,
                                           prefill=existing.get("llm_model", ""))
        layout.addWidget(self._llm_model_input)

        self._on_provider_changed(self._provider_combo.currentIndex())

        # ── OS row ─────────────────────────────────────────────────────────
        layout.addSpacing(6)
        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(self._sel_os)
        layout.addSpacing(8)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(34)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _provider_spec(self, pid: str) -> tuple[str, str, str, bool, str]:
        for row in self._PROVIDERS:
            if row[0] == pid:
                return row
        return self._PROVIDERS[0]

    def _on_provider_changed(self, _idx: int):
        pid = self._provider_combo.currentData()
        _pid, _label, default_model, needs_key, placeholder = self._provider_spec(pid)
        self._sel_provider = pid

        # API-key field visibility.
        self._llm_key_input.setVisible(needs_key)
        self._llm_key_input.setPlaceholderText(placeholder if needs_key else "")
        if not needs_key:
            self._llm_key_input.clear()

        # Model field: suggest default, but let user override.
        current = self._llm_model_input.text().strip()
        if not current:
            self._llm_model_input.setText(default_model)
        self._llm_model_input.setPlaceholderText(default_model)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _flash_red(self, widget: QLineEdit):
        widget.setStyleSheet(
            widget.styleSheet() + f" QLineEdit {{ border: 1px solid {C.RED}; }}"
        )

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._flash_red(self._key_input)
            return

        pid = self._sel_provider
        _p, _l, default_model, needs_key, _ph = self._provider_spec(pid)
        llm_key   = self._llm_key_input.text().strip()
        llm_model = self._llm_model_input.text().strip() or default_model

        if needs_key and not llm_key:
            self._flash_red(self._llm_key_input)
            return

        self.done.emit({
            "gemini_api_key": key,
            "os_system":      self._sel_os,
            "llm_provider":   pid,
            "llm_api_key":    llm_key,   # empty string for ollama; fine
            "llm_model":      llm_model,
        })


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (small white circle)
    around the wheel to choose from ALL hues. The filled circle in the centre
    is a live preview of the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y goes down — flip to math axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # centre preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # draggable handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str)   # assistant_name, user_name, ui_color, voice
    _OW, _OH = 400, 588

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from memory.config_manager import AVAILABLE_VOICES, DEFAULT_VOICE
        lay.addSpacing(4)
        lay.addWidget(_lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice   = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self._on_voice_pick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        lay.addLayout(voice_row)
        self._refresh_voice_btns()

        # ── UI colour — colour wheel ─────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — live preview; MainWindow wires it

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── voice selection ──────────────────────────────────────────────────────
    def _on_voice_pick(self, name: str):
        self._sel_voice = name
        self._refresh_voice_btns()

    def _refresh_voice_btns(self):
        """Highlight the selected voice pill; dim the rest."""
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── colour flow ──────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Updates the selected colour; hex box + wheel stay in sync, theme is live-previewed."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # While dragging: update the hex box, don't apply the theme yet
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Handle released → live-preview the whole interface
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # If a preview was applied, revert to the colour from launch
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR, self._sel_voice)
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)
        return row

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone JARVIS listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'JARVIS can't hear me' is usually 'JARVIS is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed
    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)
            lay.addWidget(box)
            return box

        self._in_box  = _row("MICROPHONE — what JARVIS hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row("SPEAKERS — what JARVIS talks through",
                             "output", get_output_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything JARVIS has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel("🧠  WHAT JARVIS REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"memory/long_term.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self._rebuild)


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self._lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings (like the 3D-printer suite) into the plugins folder and "
                "it will show up here.", 9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont("Courier New", fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Courier New", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Courier New", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config
        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)                 # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class _AmbientCanvas(QWidget):
    """Paint the ambient Command Center backdrop below translucent surfaces."""

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        try:
            gradient = QLinearGradient(0.0, 0.0, 0.0, float(self.height()))
            gradient.setColorAt(0.0, QColor("#05070d"))
            gradient.setColorAt(1.0, QColor("#03040a"))
            p.fillRect(self.rect(), QBrush(gradient))
            for x, y, radius, color, alpha in (
                (0.10, 0.0, 0.75, C.PRI, 18),
                (0.95, 1.0, 0.8, C.PRI_DIM, 20),
                (0.50, 0.50, 0.5, C.VIOLET, 8),
            ):
                glow = QRadialGradient(
                    self.width() * x, self.height() * y,
                    max(self.width(), self.height()) * radius,
                )
                center = QColor(color)
                center.setAlpha(alpha)
                glow.setColorAt(0.0, center)
                edge = QColor(C.BG)
                edge.setAlpha(0)
                glow.setColorAt(1.0, edge)
                p.fillRect(self.rect(), QBrush(glow))
            p.setPen(QPen(QColor(255, 255, 255, 5), 1))
            for x in range(0, self.width(), 48):
                p.drawLine(x, 0, x, self.height())
            for y in range(0, self.height(), 48):
                p.drawLine(0, y, self.width(), y)
        finally:
            p.end()


# ── Chat bubble widgets ───────────────────────────────────────────────────────

class _BubbleLabel(QLabel):
    """Message surface for the live conversation transcript."""

    def __init__(self, speaker: str, text: str, parent=None):
        super().__init__(text, parent)
        self._speaker = speaker
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        if speaker == "jarvis":
            self.setContentsMargins(15, 11, 15, 11)
            self.setFont(QFont(SANS(), 11))
            self.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            self._bg = QColor(C.PANEL2)
            self._bd = QColor(C.BORDER_A)
        elif speaker == "user":
            self.setContentsMargins(15, 11, 15, 11)
            self.setFont(QFont(SANS(), 11))
            self.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            self._bg = QColor(C.PANEL)
            self._bd = QColor(C.BORDER)
        else:
            self.setContentsMargins(5, 5, 5, 5)
            self.setFont(QFont(SANS(), 9))
            self.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._bg = QColor(0, 0, 0, 0)
            self._bd = QColor(0, 0, 0, 0)

        self.setMaximumWidth(680)

    def paintEvent(self, ev):  # noqa: N802
        if self._speaker in ("jarvis", "user"):
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
                path = QPainterPath()
                path.addRoundedRect(r, 10, 10)
                p.fillPath(path, QBrush(self._bg))
                p.strokePath(path, QPen(self._bd, 1))
            finally:
                p.end()
        super().paintEvent(ev)


class _BubbleWidget(QWidget):
    """One clearly attributed, readable row in the conversation."""

    def __init__(self, speaker: str, text: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 5, 0, 5)
        lay.setSpacing(5)

        if speaker not in ("sys",):
            name = QLabel(self._speaker_label(speaker))
            nf = QFont(SANS(), 9, QFont.Weight.DemiBold)
            name.setFont(nf)
            name.setStyleSheet(
                f"color: {C.PRI if speaker == 'jarvis' else C.TEXT_MED};"
                " background: transparent;"
            )
            if speaker == "user":
                name.setAlignment(Qt.AlignmentFlag.AlignRight)
            lay.addWidget(name)

        bubble = _BubbleLabel(speaker, text)
        if speaker == "user":
            row = QHBoxLayout(); row.setContentsMargins(48, 0, 0, 0)
            row.addStretch(); row.addWidget(bubble)
            lay.addLayout(row)
        elif speaker == "sys":
            row = QHBoxLayout()
            row.addStretch(); row.addWidget(bubble); row.addStretch()
            lay.addLayout(row)
        else:
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 48, 0)
            row.addWidget(bubble); row.addStretch()
            lay.addLayout(row)

    @staticmethod
    def _speaker_label(speaker: str) -> str:
        if speaker == "user":
            return "You"
        try:
            return _read_full_config().get("assistant_name") or "JARVIS"
        except Exception:
            return "JARVIS"


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _video_open_sig  = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _wake_btns_sig   = pyqtSignal()          # wake state resolved off-thread
    _video_close_sig = pyqtSignal()
    _video_mute_sig  = pyqtSignal(bool)
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig    = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig  = pyqtSignal()
    _review_sig     = pyqtSignal(str, str, object, object)  # document review payload

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        screen = QApplication.primaryScreen().availableGeometry()
        window_w = min(_DEFAULT_W, max(_MIN_W, screen.width() - 32))
        window_h = min(_DEFAULT_H, max(_MIN_H, screen.height() - 32))
        self.resize(window_w, window_h)
        self.move(
            screen.x() + (screen.width() - window_w) // 2,
            screen.y() + (screen.height() - window_h) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen

        # Dashboard metrics — populated from user activity, kept as short
        # rolling histories so the sparklines have something to draw.
        self._msg_count      = 0
        self._token_count    = 0
        self._tokens_hist    = [0] * 32
        self._msgs_hist      = [0] * 32
        self._latency_hist   = [0.0] * 32
        self._last_lat_ms    = 0.0
        self._last_user_t    = None
        self._session_start  = time.time()
        self.get_core_tools    = None
        self.get_actions       = None
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self.get_mcp_tools     = None   # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle    = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual    = None   # callable: () -> None — manual sleep/wake
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self.wake_get_state    = None   # callable: () -> dict {enabled, awake, ready}
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = _AmbientCanvas()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        # Match the remote Command Center shell: a collapsible workspace rail,
        # a compact live topbar, and one shared native workspace surface.
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._sidebar = self._build_dashboard_sidebar()
        outer.addWidget(self._sidebar)
        self._main_shell = QWidget()
        self._main_shell.setStyleSheet("background: transparent;")
        main_layout = QVBoxLayout(self._main_shell)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._build_header())

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # ── Video surface ────────────────────────────────────────────────
        # Built exactly like the camera page above and stacked beside it,
        # because it is the same idea: something takes the centre of the HUD
        # for a while and then gives it back. Sharing the stack rather than
        # inventing a second mechanism means the avatar, the camera and a video
        # can never be on screen at once.
        self._video_split = False        # is the sound a separate stream?
        self._video_auto_muted = False   # did JARVIS close the mic, or the user?
        # A plain flag rather than reading the widget. video_is_playing() is
        # called from plugin threads, and reading a widget's state from one is
        # not something to rely on; an attribute is.
        self._video_on = False
        self._video_cont = QWidget()
        self._video_cont.setStyleSheet(f"background: {C.BG};")
        _vid_v = QVBoxLayout(self._video_cont)
        _vid_v.setContentsMargins(8, 6, 8, 8)
        _vid_v.setSpacing(4)

        _vid_hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _vid_hdr.addWidget(self._video_title)
        _vid_hdr.addStretch()

        def _vid_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        # Muted is the default and the button says so, because a soundtrack
        # talking over JARVIS is the one way this feature could make the
        # assistant worse rather than better.
        self._video_mute_btn = _vid_btn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        _vid_hdr.addWidget(self._video_mute_btn)

        _vid_x = _vid_btn("✕  CLOSE")
        _vid_x.clicked.connect(self.stop_video)
        _vid_hdr.addWidget(_vid_x)
        _vid_v.addLayout(_vid_hdr)

        if HAVE_VIDEO:
            # A GRAPHICS ITEM, NOT A QVideoWidget.
            #
            # QVideoWidget gets a native window of its own, and a native window
            # is painted by the compositor above every ordinary widget that
            # shares the screen with it — so the settings drawer opened
            # *underneath* the video and raise_() could not help, because the
            # two are not in the same painting order at all. A video item drawn
            # into a QGraphicsView goes through Qt's own painter like any other
            # widget, and anything laid over it stays over it.
            self._video_scene = QGraphicsScene(self)
            self._video_item = QGraphicsVideoItem()
            self._video_scene.addItem(self._video_item)
            self._video_widget = QGraphicsView(self._video_scene)
            self._video_widget.setStyleSheet("background: #000; border: none;")
            self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
            self._video_widget.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            _vid_v.addWidget(self._video_widget, stretch=1)

            self._video_audio = QAudioOutput()
            self._video_audio.setMuted(True)
            self._video_player = QMediaPlayer()
            self._video_player.setVideoOutput(self._video_item)
            # Keep the picture filling the view as the window is resized.
            self._video_item.nativeSizeChanged.connect(self._fit_video)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.errorOccurred.connect(self._on_video_error)

            # A SECOND player, for sound that arrives separately.
            #
            # YouTube no longer serves a single stream carrying both picture and
            # sound — not for new uploads and not for old ones; checked against
            # three videos including the oldest on the site, and every one of
            # them offered zero combined formats. Picture and sound are two
            # URLs, so they are two players, started together and nudged back
            # into line by the timer below.
            #
            # A local file or a direct video URL still uses the first player
            # alone: one stream, one player, nothing to synchronise.
            self._video_sound = QMediaPlayer()
            self._video_sound_out = QAudioOutput()
            self._video_sound_out.setMuted(True)
            self._video_sound.setAudioOutput(self._video_sound_out)

            self._video_sync = QTimer(self)
            self._video_sync.setInterval(1000)
            self._video_sync.timeout.connect(self._sync_video_sound)
        else:
            self._video_scene = None
            self._video_item = None
            self._video_widget = None
            self._video_player = None
            self._video_audio = None
            self._video_sound = None
            self._video_sound_out = None
            self._video_sync = None
            _miss = QLabel("Video playback is not available in this Qt install.")
            _miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _miss.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            _vid_v.addWidget(_miss, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera, 2 = video
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)
        self._hud_cam_stack.addWidget(self._video_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        # Create the shared activity log early so both journal view and right panel
        # trace can reference the same widget — only one can own it, so the right
        # panel gets a *separate* compact trace; write_log routes to both.
        self._log = LogWidget()
        self._log.setStyleSheet(
            f"background: transparent; color: {C.TEXT}; border: none; padding: 4px;"
        )

        # Independent workspaces; the assistant is the primary interaction.
        self._view_stack = QStackedWidget()
        self._view_stack.setStyleSheet("background: transparent;")
        self._assistant_split = QSplitter(Qt.Orientation.Horizontal)
        self._assistant_split.setHandleWidth(5)
        self._assistant_split.setStyleSheet(f"""
            QSplitter::handle {{ background: {C.BORDER}; width: 5px; }}
            QSplitter::handle:hover {{ background: {C.BORDER_A}; }}
        """)
        self._assistant_split.addWidget(self._build_chat_view())
        self._assistant_split.addWidget(self._center_split)
        self._assistant_split.setStretchFactor(0, 2)
        self._assistant_split.setStretchFactor(1, 1)
        self._assistant_split.setSizes([620, 320])
        self._view_stack.addWidget(self._assistant_split)
        self._view_stack.addWidget(self._build_presence_view())
        self._view_stack.addWidget(self._build_tasks_view())
        self._view_stack.addWidget(self._build_journal_view())
        self._view_stack.addWidget(self._build_memory_view())
        self._view_stack.addWidget(self._build_plugins_view())
        self._view_stack.addWidget(self._build_settings_view())
        self._view_stack.setCurrentIndex(0)
        main_layout.addWidget(self._view_stack, stretch=1)
        main_layout.addWidget(self._build_command_bar())
        main_layout.addWidget(self._build_footer())
        outer.addWidget(self._main_shell, stretch=1)
        self._right_panel = None

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._ctrl_drawer = self._build_controls_drawer()
        self._sidebar_user_toggled = False
        self._set_sidebar_collapsed(self.width() < 1000)

        # Highlight the active workspace after every destination exists.
        try:
            self._select_nav("chat")
        except Exception:
            pass
        self._warm_wake_state()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._log_sig.connect(self._maybe_add_chat_message)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._wake_btns_sig.connect(self._refresh_wake_btns)
        self._video_open_sig.connect(self._on_video_open)
        self._video_close_sig.connect(self._on_video_close)
        self._video_mute_sig.connect(self._on_video_mute)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._quiz_sig.connect(self._show_quiz)
        self._quiz_hide_sig.connect(self._hide_quiz)
        self._review_sig.connect(self._show_review)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # --- Video in the HUD area ---------------------------------------------
    #
    # Everything below runs on the Qt thread. Plugins and the assistant reach it
    # through the signals above, the same way every other panel is driven, so a
    # background thread never touches a widget.
    def _on_video_open(self, source: str, title: str, muted: bool,
                       audio_source: str = "") -> None:
        if not HAVE_VIDEO or not self._video_player:
            self.write_log("SYS: Video playback is not available in this Qt "
                           "install.")
            return
        # A video and the live camera cannot share the centre of the HUD.
        self._cam_stop.set()

        self._video_title.setText(f"▶  {(title or 'VIDEO')[:44].upper()}")
        self._video_split = bool(audio_source)
        self._set_video_muted(bool(muted))

        url = (QUrl.fromLocalFile(source) if Path(source).exists()
               else QUrl(source))
        self._video_player.setSource(url)
        if self._video_split:
            self._video_sound.setSource(QUrl(audio_source))
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        # Re-run now that the video counts as playing: _set_video_muted ran
        # before this line and saw no video, so its mic check was a no-op.
        self._sync_mic_for_video()
        self._video_player.play()
        if self._video_split:
            self._video_sound.play()
            self._video_sync.start()

    def _fit_video(self, *_a) -> None:
        """Size the picture to the panel, keeping its shape."""
        if not (self._video_item and self._video_widget):
            return
        try:
            native = self._video_item.nativeSize()
            if native.isEmpty():
                return
            view = self._video_widget.viewport().size()
            scale = min(view.width() / native.width(),
                        view.height() / native.height())
            w, h = native.width() * scale, native.height() * scale
            self._video_item.setSize(QSizeF(w, h))
            self._video_scene.setSceneRect(0, 0, w, h)
            self._video_widget.centerOn(self._video_item)
        except Exception:
            pass

    def _on_video_close(self) -> None:
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        self._video_on = False
        self._hud_cam_stack.setCurrentIndex(0)
        self._sync_mic_for_video()      # gives the microphone back

    def _set_video_muted(self, muted: bool) -> None:
        """Silence whichever output is carrying the sound for this video."""
        muted = bool(muted)
        if self._video_audio:
            # When the sound is a separate stream this player has none, but
            # muting it too costs nothing and keeps the two paths identical.
            self._video_audio.setMuted(muted)
        if self._video_sound_out:
            self._video_sound_out.setMuted(muted)
        self._sync_video_mute_btn()
        self._sync_mic_for_video()

    def _on_video_mute(self, muted: bool) -> None:
        self._set_video_muted(muted)

    def _sync_mic_for_video(self) -> None:
        """Close the microphone while the video is making sound.

        JARVIS subtracts its OWN output from the microphone — that is what
        core/echo.py does — but a video plays through a different output
        entirely, so the guard has never heard of it and the assistant answers
        the film. There is no arrangement in which an open microphone and a
        loudspeaker in the same room do not do that.

        So the microphone closes for exactly as long as the sound is on, and
        opens again by itself the moment it goes off or the video is closed.
        Only if JARVIS closed it: a microphone the user muted themselves stays
        muted, and pressing the mute key during a video hands the decision back
        to them for good.
        """
        sound_on = bool(self._video_on and self._video_sound_out
                        and not self._video_sound_out.isMuted())
        if sound_on and not self._muted:
            self._video_auto_muted = True
            self._set_muted(True, "The video's sound is on — silence it, close "
                                  "it, or press F4 to talk. Typing still works.")
        elif not sound_on and self._video_auto_muted:
            self._video_auto_muted = False
            self._set_muted(False, "The video is quiet again.")

    def _sync_video_sound(self) -> None:
        """Keep the separate soundtrack in step with the picture.

        Two players started at the same moment do not stay together: they buffer
        independently, and measured on a real stream they were about half a
        second apart after nine seconds. So the sound is nudged back to the
        picture whenever it drifts far enough to hear — and only then, because
        correcting a smaller gap is itself audible.
        """
        if not (self._video_split and self._video_sound and self._video_player):
            return
        try:
            if self._video_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            drift = self._video_sound.position() - self._video_player.position()
            if abs(drift) > 300:
                self._video_sound.setPosition(self._video_player.position())
        except Exception:
            pass

    def _sync_video_mute_btn(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        muted = bool(out and out.isMuted())
        self._video_mute_btn.setText("🔇  SOUND OFF" if muted else "🔊  SOUND ON")

    def _toggle_video_mute(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        if out:
            self._set_video_muted(not out.isMuted())

    def _on_video_error(self, *_a) -> None:
        err = ""
        try:
            err = self._video_player.errorString()
        except Exception:
            pass
        self.write_log(f"SYS: The video could not be played{(' — ' + err) if err else ''}.")
        self._on_video_close()

    def stop_video(self) -> None:
        self._video_close_sig.emit()

    def video_is_playing(self) -> bool:
        return bool(self._video_on)

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_sidebar") and not getattr(
            self, "_sidebar_user_toggled", False
        ):
            compact = self.width() < 1000
            if compact != self._sidebar_collapsed:
                self._set_sidebar_collapsed(compact)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()
        if hasattr(self, '_ctrl_drawer') and self._ctrl_drawer.isVisible():
            self._position_ctrl_drawer()
        self._fit_video()

    def _update_metrics(self):
        self._refresh_system_view()
        if self._view_stack.currentIndex() == 2:
            self._refresh_tasks_view()

    def _build_dashboard_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("DashboardSidebar")
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(
            f"QWidget#DashboardSidebar {{ background: rgba(10,15,28,225); "
            f"border-right: 1px solid rgba(255,255,255,16); }}"
        )
        root = QVBoxLayout(sidebar)
        root.setContentsMargins(14, 18, 14, 14)
        root.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(2, 0, 2, 18)
        brand_row.setSpacing(11)
        mark = QFrame()
        mark.setFixedSize(44, 44)
        mark.setObjectName("BrandMark")
        mark.setStyleSheet(
            "QFrame#BrandMark { background: qconicalgradient(cx:0.5, cy:0.5, "
            "angle:210, stop:0 #00e5ff, stop:0.32 #3d7eff, "
            "stop:0.68 #8b6bff, stop:1 #00e5ff); border-radius: 15px; }"
        )
        mark_layout = QVBoxLayout(mark)
        mark_layout.setContentsMargins(3, 3, 3, 3)
        brand_mark = QLabel("J·A")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setFont(QFont("Orbitron", 10, QFont.Weight.Black))
        brand_mark.setStyleSheet(
            f"color: {C.PRI}; background: qradialgradient(cx:0.3, cy:0.3, "
            f"radius:1, stop:0 {C.PANEL}, stop:1 {C.BG}); "
            "border-radius: 12px; letter-spacing: 2px;"
        )
        mark_layout.addWidget(brand_mark)
        brand_row.addWidget(mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(2)
        self._title_lbl = QLabel(self._assistant_name.upper())
        self._title_lbl.setFont(QFont("Orbitron", 12, QFont.Weight.Black))
        self._title_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: transparent; letter-spacing: 2px;"
        )
        brand_text.addWidget(self._title_lbl)
        self._sub_lbl = QLabel(f"COMMAND CENTER · {APP_VERSION}")
        self._sub_lbl.setFont(QFont(SANS(), 7, QFont.Weight.DemiBold))
        self._sub_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; letter-spacing: 1px;"
        )
        brand_text.addWidget(self._sub_lbl)
        brand_row.addLayout(brand_text, 1)
        root.addLayout(brand_row)

        self._sidebar_search = QLineEdit()
        self._sidebar_search.setPlaceholderText("Find workspace…")
        self._sidebar_search.setFixedHeight(38)
        self._sidebar_search.setFont(QFont(SANS(), 9))
        self._sidebar_search.setStyleSheet(f"""
            QLineEdit {{
                color: {C.TEXT}; background: rgba(255,255,255,8);
                border: 1px solid rgba(255,255,255,25);
                border-radius: 10px; padding: 0 11px;
            }}
            QLineEdit:focus {{
                border-color: rgba(0,229,255,140);
                background: rgba(255,255,255,15);
            }}
            QLineEdit::placeholder {{ color: {C.TEXT_DIM}; }}
        """)
        self._sidebar_search.textChanged.connect(self._filter_sidebar_nav)
        root.addWidget(self._sidebar_search)
        root.addSpacing(15)

        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { width: 5px; background: transparent; border: none; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        nav_host = QWidget()
        nav_host.setStyleSheet("background: transparent;")
        nav_layout = QVBoxLayout(nav_host)
        nav_layout.setContentsMargins(0, 0, 0, 4)
        nav_layout.setSpacing(0)
        self._nav_pills: dict[str, QPushButton] = {}
        self._nav_labels: dict[str, str] = {}
        self._nav_sections: list[tuple[QLabel, tuple[str, ...]]] = []

        groups = (
            ("WORKSPACE", C.PRI, (
                ("Assistant", "chat", "◉"),
                ("Agent tasks", "tasks", "◇"),
            )),
            ("EXECUTIVE", C.VIOLET, (
                ("Activity", "history", "≡"),
                ("Memory", "memory", "◌"),
            )),
            ("SYSTEM", C.GREEN, (
                ("Tools · MCP", "plugins", "⊞"),
                ("System", "stats", "⌁"),
                ("Settings", "settings", "⚙"),
            )),
        )
        self._nav_section_widgets = []
        self._nav_entries = []
        for section_name, accent, entries in groups:
            section = QLabel(f"●  {section_name}")
            section.setFont(QFont(SANS(), 7, QFont.Weight.Bold))
            section.setStyleSheet(
                f"color: {accent}; background: transparent; "
                "letter-spacing: 1.5px; padding: 9px 8px 6px;"
            )
            nav_layout.addWidget(section)
            keys = []
            self._nav_section_widgets.append((section, tuple(k for _, k, _ in entries)))
            for label, key, glyph in entries:
                button = QPushButton(f"{glyph}   {label}")
                button.setObjectName("WorkspaceNav")
                button.setFixedHeight(38)
                button.setFont(QFont(SANS(), 9, QFont.Weight.Medium))
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setToolTip(label)
                button.clicked.connect(
                    lambda _checked=False, k=key: self._select_nav(k)
                )
                nav_layout.addWidget(button)
                self._nav_pills[key] = button
                self._nav_labels[key] = label
                self._nav_entries.append((button, label, glyph, key))
                keys.append(key)
            self._nav_sections.append((section, tuple(keys)))
            nav_layout.addSpacing(9)
        nav_layout.addStretch(1)
        nav_scroll.setWidget(nav_host)
        root.addWidget(nav_scroll, 1)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: rgba(255,255,255,18); border: none;")
        root.addWidget(divider)
        profile = QHBoxLayout()
        profile.setContentsMargins(3, 12, 3, 8)
        profile.setSpacing(10)
        profile_mark = QLabel("JA")
        profile_mark.setFixedSize(36, 36)
        profile_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        profile_mark.setFont(QFont("Orbitron", 8, QFont.Weight.Bold))
        profile_mark.setStyleSheet(
            f"color: {C.TEXT}; background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {C.PRI}, stop:1 {C.VIOLET}); border: 2px solid {C.PANEL}; "
            "border-radius: 11px;"
        )
        profile.addWidget(profile_mark)
        profile_meta = QVBoxLayout()
        profile_meta.setSpacing(2)
        profile_title = QLabel(f"{self._assistant_name} runtime")
        profile_title.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
        profile_title.setStyleSheet(
            f"color: {C.TEXT}; background: transparent;"
        )
        self._sidebar_profile_title = profile_title
        self._sidebar_state_lbl = QLabel("Starting")
        self._sidebar_state_lbl.setFont(QFont("JetBrains Mono", 7))
        self._sidebar_state_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )
        profile_meta.addWidget(profile_title)
        profile_meta.addWidget(self._sidebar_state_lbl)
        profile.addLayout(profile_meta, 1)
        root.addLayout(profile)

        utility_row = QHBoxLayout()
        utility_row.setSpacing(5)
        utility_style = f"""
            QPushButton {{
                color: {C.TEXT_MED}; background: rgba(255,255,255,8);
                border: 1px solid rgba(255,255,255,20);
                border-radius: 8px; padding: 0 7px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border-color: rgba(0,229,255,95);
                background: rgba(0,229,255,12);
            }}
        """
        self._drawer_btn = QPushButton("⚙  Setup")
        self._drawer_btn.setToolTip("Open setup and connection actions")
        self._ctrl_btn = QPushButton("◉  Controls")
        self._ctrl_btn.setToolTip("Open voice and assistant controls")
        self._remote_btn = QPushButton("↗")
        self._remote_btn.setToolTip("Open remote dashboard pairing")
        self._drawer_btn.setCheckable(True)
        self._ctrl_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        self._ctrl_btn.clicked.connect(self._toggle_controls)
        self._remote_btn.clicked.connect(self._open_remote)
        self._sidebar_utility_buttons = (
            (self._drawer_btn, "⚙  Setup", "⚙"),
            (self._ctrl_btn, "◉  Controls", "◉"),
            (self._remote_btn, "↗", "↗"),
        )
        for button, _, _ in self._sidebar_utility_buttons:
            button.setMinimumHeight(30)
            button.setFont(QFont(SANS(), 7, QFont.Weight.DemiBold))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(utility_style)
            utility_row.addWidget(button, 1)
        root.addLayout(utility_row)

        collapse = QPushButton("‹  Collapse")
        collapse.setFixedHeight(28)
        collapse.setFont(QFont(SANS(), 8))
        collapse.setCursor(Qt.CursorShape.PointingHandCursor)
        collapse.setStyleSheet(
            f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent; "
            f"border: none; text-align: left; padding-left: 8px; }}"
            f"QPushButton:hover {{ color: {C.TEXT}; }}"
        )
        collapse.clicked.connect(self._toggle_sidebar_width)
        root.addWidget(collapse)
        self._sidebar_collapse_btn = collapse
        self._sidebar_collapsed = False
        self._active_nav = "chat"
        return sidebar

    def _filter_sidebar_nav(self, text: str) -> None:
        query = text.strip().casefold()
        for button, label, _glyph, _key in self._nav_entries:
            button.setVisible(not query or query in label.casefold())
        for section, keys in self._nav_section_widgets:
            section.setVisible(
                not query or any(
                    query in self._nav_labels[key].casefold() for key in keys
                )
            )

    def _set_sidebar_collapsed(self, collapsed: bool) -> None:
        self._sidebar_collapsed = bool(collapsed)
        collapsed = self._sidebar_collapsed
        self._sidebar.setFixedWidth(78 if collapsed else 260)
        self._sidebar_search.setVisible(not collapsed)
        self._title_lbl.setVisible(not collapsed)
        self._sub_lbl.setVisible(not collapsed)
        self._sidebar_profile_title.setVisible(not collapsed)
        self._sidebar_state_lbl.setVisible(not collapsed)
        for section, _keys in self._nav_section_widgets:
            section.setVisible(
                not collapsed and (
                    not self._sidebar_search.text().strip()
                    or any(
                        self._sidebar_search.text().strip().casefold()
                        in self._nav_labels[key].casefold()
                        for key in _keys
                    )
                )
            )
        for button, label, glyph, _key in self._nav_entries:
            button.setText(glyph if collapsed else f"{glyph}   {label}")
            button.setStyleSheet(self._nav_style(button is self._nav_pills.get(self._active_nav)))
            button.setToolTip(label)
        for button, full_label, glyph in self._sidebar_utility_buttons:
            button.setText(glyph if collapsed else full_label)
        self._sidebar_collapse_btn.setText("›" if collapsed else "‹  Collapse")
        self._sidebar_collapse_btn.setToolTip(
            "Expand workspace navigation" if collapsed else "Collapse workspace navigation"
        )

    def _toggle_sidebar_width(self) -> None:
        self._sidebar_user_toggled = True
        self._set_sidebar_collapsed(not self._sidebar_collapsed)

    def _nav_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton#WorkspaceNav {{
                    color: {C.TEXT}; background: qlineargradient(
                        x1:0, y1:0, x2:1, y2:0,
                        stop:0 rgba(0,229,255,26), stop:1 rgba(0,229,255,5));
                    border: 1px solid rgba(0,229,255,18);
                    border-left: 3px solid {C.PRI};
                    border-radius: 11px; text-align: left; padding: 0 10px;
                }}
            """
        return f"""
            QPushButton#WorkspaceNav {{
                color: {C.TEXT_MED}; background: transparent;
                border: 1px solid transparent; border-left: 3px solid transparent;
                border-radius: 11px; text-align: left; padding: 0 10px;
            }}
            QPushButton#WorkspaceNav:hover {{
                color: {C.TEXT}; background: rgba(255,255,255,10);
            }}
        """

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(62)
        header.setStyleSheet(
            f"background: rgba(10,15,28,195); border-bottom: 1px solid rgba(255,255,255,16);"
        )
        row = QHBoxLayout(header)
        row.setContentsMargins(24, 0, 24, 0)
        row.setSpacing(11)

        crumb = QLabel()
        crumb.setFont(QFont("JetBrains Mono", 8, QFont.Weight.Medium))
        crumb.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        row.addWidget(QLabel("WORKSPACE"))
        row.itemAt(0).widget().setFont(QFont("JetBrains Mono", 7))
        row.itemAt(0).widget().setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; letter-spacing: 1px;"
        )
        separator = QLabel("›")
        separator.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        row.addWidget(separator)
        self._crumb_lbl = crumb
        row.addWidget(crumb)

        self._status_chip = QLabel()
        self._status_chip.setTextFormat(Qt.TextFormat.RichText)
        self._status_chip.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
        row.addWidget(self._status_chip)
        self._header_eq = QWidget()
        eq_row = QHBoxLayout(self._header_eq)
        eq_row.setContentsMargins(0, 0, 0, 0)
        eq_row.setSpacing(3)
        self._header_eq_bars = []
        for height in (7, 12, 17, 10, 14):
            bar = QFrame()
            bar.setFixedSize(3, height)
            bar.setStyleSheet(
                f"background: qlineargradient(x1:0,y1:1,x2:0,y2:0,"
                f"stop:0 {C.PRI_DIM},stop:1 {C.PRI}); border-radius: 2px;"
            )
            self._header_eq_bars.append(bar)
            eq_row.addWidget(bar, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._header_eq)
        row.addStretch(1)

        clock = QWidget()
        clock.setObjectName("ClockBadge")
        clock.setStyleSheet(
            "QWidget#ClockBadge { background: rgba(255,255,255,4); "
            "border: 1px solid rgba(255,255,255,14); border-radius: 9px; }"
        )
        clock_row = QHBoxLayout(clock)
        clock_row.setContentsMargins(10, 5, 10, 5)
        clock_row.setSpacing(7)
        clock_icon = QLabel("◷")
        clock_icon.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        clock_row.addWidget(clock_icon)
        self._clock_lbl = QLabel("--:--:--")
        self._clock_lbl.setFont(QFont("JetBrains Mono", 8))
        self._clock_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        clock_row.addWidget(self._clock_lbl)
        date_separator = QLabel("·")
        date_separator.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        clock_row.addWidget(date_separator)
        self._date_lbl = QLabel("--")
        self._date_lbl.setFont(QFont(SANS(), 7, QFont.Weight.DemiBold))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        clock_row.addWidget(self._date_lbl)
        row.addWidget(clock)

        self._header_wake_btn = QPushButton("⚡  WAKE")
        self._header_wake_btn.setToolTip("Send a manual wake signal")
        self._header_wake_btn.setFixedHeight(32)
        self._header_wake_btn.setFont(QFont(SANS(), 8, QFont.Weight.Bold))
        self._header_wake_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_wake_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT}; background: rgba(255,255,255,8);
                border: 1px solid rgba(255,255,255,24);
                border-radius: 10px; padding: 0 12px; letter-spacing: 1px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border-color: rgba(0,229,255,100);
                background: rgba(0,229,255,12);
            }}
        """)
        self._header_wake_btn.clicked.connect(self._tap_wake_manual)
        row.addWidget(self._header_wake_btn)
        self._paint_status_chip("INITIALISING")
        return header

    def _select_nav(self, key: str):
        """Switch between the application's real workspaces."""
        view_map = {
            "chat": 0, "stats": 1, "tasks": 2, "history": 3,
            "memory": 4, "plugins": 5, "settings": 6,
        }
        if key == "controls":
            self._toggle_controls(not self._ctrl_btn.isChecked())
            return
        if key not in view_map:
            return
        self._active_nav = key
        self._crumb_lbl.setText(self._nav_labels[key])
        for nav_key, btn in self._nav_pills.items():
            btn.setStyleSheet(self._nav_style(nav_key == key))
        self._view_stack.setCurrentIndex(view_map[key])
        if key == "chat":
            self._input.setFocus()
        elif key == "settings":
            self._refresh_settings_summary()
        elif key == "tasks":
            self._refresh_tasks_view()
        elif key == "history":
            sb = self._log.verticalScrollBar()
            sb.setValue(sb.maximum())
            self._log.setFocus()
        elif key == "stats":
            self._refresh_system_view()
        elif key == "memory":
            self._refresh_memory_view()
        elif key == "plugins":
            self._refresh_plugins_view()

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a · %d %b").upper())
        elapsed = max(0, int(time.time() - self._session_start))
        h, remainder = divmod(elapsed, 3600)
        m, s = divmod(remainder, 60)
        self._sess_time_lbl2.setText(f"Session {h:02d}:{m:02d}:{s:02d}")

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.PANEL}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        brand = QHBoxLayout()
        brand.setContentsMargins(3, 5, 3, 9)
        brand.setSpacing(10)
        logo = QLabel("J")
        logo.setFixedSize(36, 36)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFont(QFont(HEAD(), 17, QFont.Weight.Bold))
        logo.setStyleSheet(
            f"color: {C.BG}; background: {C.PRI}; border-radius: 10px;"
        )
        brand.addWidget(logo)
        identity = QVBoxLayout()
        identity.setSpacing(1)
        self._title_lbl = QLabel(self._assistant_name.upper())
        self._title_lbl.setFont(QFont(HEAD(), 12, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: transparent;"
        )
        identity.addWidget(self._title_lbl)
        brand_tag = QLabel("Personal assistant")
        brand_tag.setFont(QFont(SANS(), 8))
        brand_tag.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )
        identity.addWidget(brand_tag)
        brand.addLayout(identity)
        brand.addStretch()
        lay.addLayout(brand)

        runtime = QFrame()
        runtime.setObjectName("RuntimeCard")
        runtime.setStyleSheet(f"""
            QFrame#RuntimeCard {{
                background: {C.BG};
                border: 1px solid {C.BORDER};
                border-radius: 10px;
            }}
        """)
        runtime_lay = QVBoxLayout(runtime)
        runtime_lay.setContentsMargins(12, 10, 12, 10)
        runtime_lay.setSpacing(8)
        runtime_head = QHBoxLayout()
        runtime_title = QLabel("Assistant state")
        runtime_title.setFont(QFont(SANS(), 9, QFont.Weight.DemiBold))
        runtime_title.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        runtime_head.addWidget(runtime_title)
        runtime_head.addStretch()
        self._core_dot = QLabel("●")
        self._core_dot.setFont(QFont(SANS(), 9))
        self._core_dot.setStyleSheet(
            f"color: {C.PRI_DIM}; background: transparent;"
        )
        runtime_head.addWidget(self._core_dot)
        runtime_lay.addLayout(runtime_head)
        self._core_state_lbl = QLabel("Starting")
        self._core_state_lbl.setFont(QFont(SANS(), 10, QFont.Weight.DemiBold))
        self._core_state_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: transparent;"
        )
        runtime_lay.addWidget(self._core_state_lbl)

        def _resource_row(label: str):
            row = QVBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            label_widget = QLabel(label)
            label_widget.setFont(QFont(SANS(), 8))
            label_widget.setStyleSheet(
                f"color: {C.TEXT_DIM}; background: transparent;"
            )
            value_widget = QLabel("—")
            value_widget.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
            value_widget.setStyleSheet(
                f"color: {C.TEXT_MED}; background: transparent;"
            )
            header.addWidget(label_widget)
            header.addStretch()
            header.addWidget(value_widget)
            row.addLayout(header)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setStyleSheet(f"""
                QProgressBar {{
                    background: {C.BAR_BG}; border: none; border-radius: 2px;
                }}
                QProgressBar::chunk {{
                    background: {C.PRI}; border-radius: 2px;
                }}
            """)
            row.addWidget(bar)
            runtime_lay.addLayout(row)
            return value_widget, bar

        self._core_cpu_lbl, self._core_cpu_bar = _resource_row("Processor")
        self._core_mem_lbl, self._core_mem_bar = _resource_row("Memory")
        lay.addWidget(runtime)

        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { width: 4px; background: transparent; border: none; }
            QScrollBar::handle:vertical { background: #273148; border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        nav_widget = QWidget()
        nav_widget.setStyleSheet("background: transparent;")
        nav_lay = QVBoxLayout(nav_widget)
        nav_lay.setContentsMargins(0, 4, 0, 8)
        nav_lay.setSpacing(3)

        self._nav_pills: dict[str, QPushButton] = {}

        def _section(text: str):
            label = QLabel(text)
            label.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
            label.setStyleSheet(
                f"color: {C.TEXT_DIM}; background: transparent; padding: 6px 8px 3px;"
            )
            nav_lay.addWidget(label)

        def _nav(label: str, key: str, glyph: str):
            button = QPushButton(f"{glyph}   {label}")
            button.setFixedHeight(36)
            button.setFont(QFont(SANS(), 9, QFont.Weight.Medium))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_MED}; background: transparent;
                    border: 1px solid transparent; border-radius: 8px;
                    text-align: left; padding: 0 10px;
                }}
                QPushButton:hover {{
                    color: {C.TEXT}; background: {C.PANEL2};
                }}
                QPushButton:focus-visible {{ border: 1px solid {C.PRI}; }}
            """)
            button.clicked.connect(lambda _checked=False, k=key: self._select_nav(k))
            nav_lay.addWidget(button)
            self._nav_pills[key] = button

        _section("Workspace")
        _nav("Assistant", "chat", "◉")
        _nav("System", "stats", "⌁")
        _nav("Agent tasks", "tasks", "◇")
        _nav("Activity", "history", "≡")
        _section("Knowledge")
        _nav("Memory", "memory", "◌")
        _section("Configuration")
        _nav("Tools and plugins", "plugins", "⊞")
        _nav("Controls", "controls", "⌘")
        _nav("Preferences", "settings", "⚙")
        nav_lay.addStretch()
        nav_scroll.setWidget(nav_widget)
        lay.addWidget(nav_scroll, stretch=1)

        profile = QFrame()
        profile.setStyleSheet(
            f"background: transparent; border-top: 1px solid {C.BORDER};"
        )
        profile_lay = QHBoxLayout(profile)
        profile_lay.setContentsMargins(3, 10, 3, 0)
        profile_lay.setSpacing(8)
        profile_mark = QLabel("◈")
        profile_mark.setFixedSize(28, 28)
        profile_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        profile_mark.setFont(QFont(SANS(), 12))
        profile_mark.setStyleSheet(
            f"color: {C.PRI}; background: {C.PRI_GHO}; border-radius: 8px;"
        )
        profile_lay.addWidget(profile_mark)
        profile_info = QVBoxLayout()
        profile_info.setSpacing(1)
        profile_name = _read_full_config().get("user_name") or "This device"
        profile_user = QLabel(profile_name)
        profile_user.setFont(QFont(SANS(), 9, QFont.Weight.DemiBold))
        profile_user.setStyleSheet(
            f"color: {C.TEXT}; background: transparent;"
        )
        profile_info.addWidget(profile_user)
        profile_detail = QLabel("Local session")
        profile_detail.setFont(QFont(SANS(), 8))
        profile_detail.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )
        profile_info.addWidget(profile_detail)
        profile_lay.addLayout(profile_info)
        profile_lay.addStretch()
        lay.addWidget(profile)
        return w
    # ── View builders ─────────────────────────────────────────────────────────

    def _build_presence_view(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        root = QVBoxLayout(page)
        root.setContentsMargins(30, 22, 30, 20)
        root.setSpacing(16)

        title = QLabel("System")
        title.setFont(QFont(HEAD(), 21, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        root.addWidget(title)
        intro = QLabel("Live state from this JARVIS session and the host device.")
        intro.setFont(QFont(SANS(), 9))
        intro.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        root.addWidget(intro)

        runtime = QFrame()
        runtime.setObjectName("SystemRuntime")
        runtime.setStyleSheet(_glass_card_style("QFrame#SystemRuntime", 16))
        row = QHBoxLayout(runtime)
        row.setContentsMargins(18, 14, 18, 14)
        row.setSpacing(12)
        state_box = QVBoxLayout()
        state_box.setSpacing(4)
        caption = QLabel("ASSISTANT STATE")
        caption.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
        caption.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        state_box.addWidget(caption)
        state_line = QHBoxLayout()
        self._core_dot = QLabel("●")
        self._core_dot.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        state_line.addWidget(self._core_dot)
        self._core_state_lbl = QLabel("Starting")
        self._core_state_lbl.setFont(QFont(HEAD(), 14, QFont.Weight.DemiBold))
        self._core_state_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        state_line.addWidget(self._core_state_lbl)
        state_line.addStretch()
        state_box.addLayout(state_line)
        row.addLayout(state_box, 1)
        row.addStretch()
        self._sys_runtime_meta = QLabel()
        self._sys_runtime_meta.setFont(QFont(SANS(), 9))
        self._sys_runtime_meta.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._sys_runtime_meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._sys_runtime_meta)
        root.addWidget(runtime)

        device = QFrame()
        device.setObjectName("SystemDevice")
        device.setStyleSheet(_glass_card_style("QFrame#SystemDevice", 16))
        device_layout = QVBoxLayout(device)
        device_layout.setContentsMargins(18, 15, 18, 15)
        device_layout.setSpacing(12)
        heading = QLabel("DEVICE TELEMETRY")
        heading.setFont(QFont(SANS(), 8, QFont.Weight.Bold))
        heading.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        device_layout.addWidget(heading)
        self._sys_metric_labels: dict[str, QLabel] = {}
        self._sys_metric_bars: dict[str, QProgressBar] = {}
        metric_grid = QGridLayout()
        metric_grid.setHorizontalSpacing(22)
        metric_grid.setVerticalSpacing(14)
        for index, (key, label) in enumerate((
            ("cpu", "Processor"), ("mem", "Memory"),
            ("net", "Network throughput"), ("gpu", "GPU"),
            ("tmp", "Temperature"),
        )):
            card = QWidget()
            card.setStyleSheet("background: transparent;")
            metric = QVBoxLayout(card)
            metric.setContentsMargins(0, 0, 0, 0)
            metric.setSpacing(5)
            head = QHBoxLayout()
            name = QLabel(label)
            name.setFont(QFont(SANS(), 8))
            name.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            value = QLabel("—")
            value.setFont(QFont(SANS(), 9, QFont.Weight.DemiBold))
            value.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            head.addWidget(name)
            head.addStretch()
            head.addWidget(value)
            metric.addLayout(head)
            self._sys_metric_labels[key] = value
            if key in ("cpu", "mem", "gpu"):
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setTextVisible(False)
                bar.setFixedHeight(4)
                bar.setStyleSheet(f"""
                    QProgressBar {{ background: {C.BAR_BG}; border: none; border-radius: 2px; }}
                    QProgressBar::chunk {{ background: {C.PRI}; border-radius: 2px; }}
                """)
                metric.addWidget(bar)
                self._sys_metric_bars[key] = bar
            metric_grid.addWidget(card, index // 2, index % 2)
        device_layout.addLayout(metric_grid)
        root.addWidget(device)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        for text, callback in (
            ("Voice and devices", self._open_audio_devices),
            ("Assistant controls", lambda: self._ctrl_btn.click()),
            ("Connection setup", self._show_setup),
        ):
            button = QPushButton(text)
            button.setFixedHeight(34)
            button.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(f"""
                QPushButton {{ color: {C.TEXT_MED}; background: {C.PANEL};
                    border: 1px solid {C.BORDER}; border-radius: 9px; padding: 0 12px; }}
                QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
            """)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)
        root.addStretch(1)
        self._refresh_system_view()
        return page

    def _refresh_system_view(self) -> None:
        if not hasattr(self, "_sys_metric_labels"):
            return
        snap = _metrics.snapshot()
        self._sys_metric_labels["cpu"].setText(f"{snap['cpu']:.0f}%")
        self._sys_metric_labels["mem"].setText(f"{snap['mem']:.0f}%")
        net = snap["net"]
        self._sys_metric_labels["net"].setText(
            f"{net * 1024:.0f} KB/s" if net < 1 else f"{net:.1f} MB/s"
        )
        self._sys_metric_labels["gpu"].setText(
            f"{snap['gpu']:.0f}%" if snap["gpu"] >= 0 else "Unavailable"
        )
        self._sys_metric_labels["tmp"].setText(
            f"{snap['tmp']:.0f}°C" if snap["tmp"] >= 0 else "Unavailable"
        )
        for key in ("cpu", "mem"):
            self._sys_metric_bars[key].setValue(int(snap[key]))
        self._sys_metric_bars["gpu"].setValue(
            max(0, int(snap["gpu"])) if snap["gpu"] >= 0 else 0
        )
        self._sys_runtime_meta.setText(
            f"{platform.system()} {platform.release()}   ·   {APP_VERSION}"
        )

    def _build_memory_view(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        root = QVBoxLayout(page)
        root.setContentsMargins(30, 22, 30, 18)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("Memory")
        title.setFont(QFont(HEAD(), 21, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        header.addWidget(title)
        header.addStretch()
        refresh = QPushButton("Refresh")
        refresh.setFixedHeight(32)
        refresh.clicked.connect(self._refresh_memory_view)
        refresh.setStyleSheet(self._secondary_button_style())
        header.addWidget(refresh)
        root.addLayout(header)
        self._memory_summary = QLabel()
        self._memory_summary.setFont(QFont(SANS(), 9))
        self._memory_summary.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        root.addWidget(self._memory_summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { width: 5px; background: transparent; border: none; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal { height: 0; }
        """)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self._memory_rows_layout = QVBoxLayout(content)
        self._memory_rows_layout.setContentsMargins(0, 0, 4, 0)
        self._memory_rows_layout.setSpacing(7)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self._refresh_memory_view()
        return page

    def _refresh_memory_view(self) -> None:
        if not hasattr(self, "_memory_rows_layout"):
            return
        while self._memory_rows_layout.count():
            item = self._memory_rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        try:
            from memory.memory_manager import all_entries_for_ui
            entries = all_entries_for_ui()
        except Exception as exc:
            self._memory_summary.setText(f"Memory could not be loaded: {exc}")
            self._log_sig.emit(f"ERR: Memory could not be loaded — {exc}")
            return
        self._memory_summary.setText(
            f"{len(entries)} stored entries · managed on this device"
        )
        if not entries:
            empty = QLabel("JARVIS has not stored any memories yet.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._memory_rows_layout.addWidget(empty)
        for entry in entries:
            card = QFrame()
            card.setObjectName("MemoryEntry")
            card.setStyleSheet(_glass_card_style("QFrame#MemoryEntry", 14))
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 10, 12, 10)
            text_box = QVBoxLayout()
            text_box.setSpacing(3)
            key = QLabel(str(entry.get("key", "")).replace("_", " "))
            key.setFont(QFont(SANS(), 9, QFont.Weight.DemiBold))
            key.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
            key.setTextFormat(Qt.TextFormat.PlainText)
            value = QLabel(str(entry.get("value", "")))
            value.setWordWrap(True)
            value.setFont(QFont(SANS(), 9))
            value.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
            value.setTextFormat(Qt.TextFormat.PlainText)
            meta = QLabel(f"{entry.get('category', 'other')}  ·  {entry.get('updated') or 'Date unavailable'}")
            meta.setFont(QFont(SANS(), 7))
            meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            text_box.addWidget(key)
            text_box.addWidget(value)
            text_box.addWidget(meta)
            row.addLayout(text_box, 1)
            forget = QPushButton("Forget")
            forget.setFixedHeight(28)
            forget.setCursor(Qt.CursorShape.PointingHandCursor)
            forget.setStyleSheet(self._secondary_button_style())
            forget.clicked.connect(
                lambda _checked=False, c=entry.get("category"), k=entry.get("key"):
                    self._forget_memory(c, k)
            )
            row.addWidget(forget)
            self._memory_rows_layout.addWidget(card)
        self._memory_rows_layout.addStretch(1)

    def _forget_memory(self, category: str, key: str) -> None:
        try:
            from memory.memory_manager import forget
            forget(key, category)
        except Exception as exc:
            self._log_sig.emit(f"ERR: Could not forget memory — {exc}")
            return
        self._refresh_memory_view()

    def _build_plugins_view(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        root = QVBoxLayout(page)
        root.setContentsMargins(30, 22, 30, 18)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("Tools & integrations")
        title.setFont(QFont(HEAD(), 21, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        header.addWidget(title)
        header.addStretch()
        refresh = QPushButton("Refresh")
        refresh.setFixedHeight(32)
        refresh.clicked.connect(self._refresh_plugins_view)
        refresh.setStyleSheet(self._secondary_button_style())
        header.addWidget(refresh)
        root.addLayout(header)
        self._plugins_summary = QLabel()
        self._plugins_summary.setFont(QFont(SANS(), 9))
        self._plugins_summary.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        root.addWidget(self._plugins_summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { width: 5px; background: transparent; border: none; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal { height: 0; }
        """)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self._plugins_rows_layout = QVBoxLayout(content)
        self._plugins_rows_layout.setContentsMargins(0, 0, 4, 0)
        self._plugins_rows_layout.setSpacing(7)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        manage = QPushButton("Configure plugin settings")
        manage.setFixedHeight(34)
        manage.setStyleSheet(self._secondary_button_style())
        manage.clicked.connect(self._open_plugin_settings)
        root.addWidget(manage, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh_plugins_view()
        return page

    def _refresh_plugins_view(self) -> None:
        if not hasattr(self, "_plugins_rows_layout"):
            return
        while self._plugins_rows_layout.count():
            item = self._plugins_rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        registry_errors = []

        def _read_registry(callback, title):
            if not callable(callback):
                return None
            try:
                return callback() or []
            except Exception as exc:
                registry_errors.append(f"{title}: {exc}")
                return []

        actions = _read_registry(self.get_actions, "Built-in actions")
        core_tools = _read_registry(self.get_core_tools, "Core tools")
        plugins = _read_registry(self.get_plugins, "Plugins")
        mcp_tools = _read_registry(self.get_mcp_tools, "MCP tools")
        if actions is None and core_tools is None and plugins is None and mcp_tools is None:
            self._plugins_summary.setText("Tool registries have not been loaded by the runtime.")
            return

        plugin_count = len(plugins or [])
        enabled_count = sum(bool(p.get("enabled")) for p in plugins or [] if p.get("valid"))
        self._plugins_summary.setText(
            f"{len(actions or [])} actions  ·  "
            f"{len(core_tools or [])} core tools  ·  "
            f"{plugin_count} plugins ({enabled_count} enabled)  ·  "
            f"{len(mcp_tools or [])} MCP tools"
        )
        if registry_errors:
            error = QLabel("  ·  ".join(registry_errors))
            error.setWordWrap(True)
            error.setStyleSheet(f"color: {C.ACC}; background: transparent;")
            self._plugins_rows_layout.addWidget(error)

        def _section(title: str):
            label = QLabel(title.upper())
            label.setFont(QFont(SANS(), 8, QFont.Weight.Bold))
            label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._plugins_rows_layout.addWidget(label)

        def _tool_card(name: str, description: str):
            card = QFrame()
            card.setObjectName("ToolEntry")
            card.setStyleSheet(_glass_card_style("QFrame#ToolEntry", 14))
            detail = QVBoxLayout(card)
            detail.setContentsMargins(14, 9, 14, 9)
            detail.setSpacing(3)
            name_label = QLabel(name)
            name_label.setFont(QFont(SANS(), 9, QFont.Weight.DemiBold))
            name_label.setStyleSheet(
                f"color: {C.TEXT}; background: transparent; border: none;"
            )
            name_label.setTextFormat(Qt.TextFormat.PlainText)
            preview = " ".join(description.split())
            if len(preview) > 180:
                preview = preview[:177].rsplit(" ", 1)[0] + "..."
            desc_label = QLabel(preview)
            desc_label.setWordWrap(True)
            desc_label.setFont(QFont(SANS(), 8))
            desc_label.setStyleSheet(
                f"color: {C.TEXT_DIM}; background: transparent; border: none;"
            )
            desc_label.setTextFormat(Qt.TextFormat.PlainText)
            desc_label.setToolTip(description)
            detail.addWidget(name_label)
            detail.addWidget(desc_label)
            self._plugins_rows_layout.addWidget(card)

        _section("Actions")
        if not actions:
            empty = QLabel("No actions are available.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._plugins_rows_layout.addWidget(empty)
        for action in actions or []:
            _tool_card(
                str(action.get("name", "Unnamed action")),
                str(action.get("description", "")),
            )

        _section("Core tools")
        if not core_tools:
            empty = QLabel("No core tools are available.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._plugins_rows_layout.addWidget(empty)
        for tool in core_tools or []:
            _tool_card(
                str(tool.get("name", "Unnamed core tool")),
                str(tool.get("description", "")),
            )

        _section("Plugins")
        if not plugins:
            empty = QLabel("No plugins were discovered by the runtime.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._plugins_rows_layout.addWidget(empty)
        for plugin in plugins or []:
            card = QFrame()
            card.setObjectName("PluginEntry")
            card.setStyleSheet(_glass_card_style("QFrame#PluginEntry", 14))
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 10, 12, 10)
            detail = QVBoxLayout()
            detail.setSpacing(3)
            name = QLabel(str(plugin.get("name", plugin.get("file", "Unknown plugin"))))
            name.setFont(QFont(SANS(), 9, QFont.Weight.DemiBold))
            name.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            description = QLabel(str(
                plugin.get("description") if plugin.get("valid")
                else plugin.get("error", "Plugin failed to load")
            ))
            description.setWordWrap(True)
            description.setFont(QFont(SANS(), 8))
            description.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            detail.addWidget(name)
            detail.addWidget(description)
            row.addLayout(detail, 1)
            if plugin.get("valid"):
                enabled = bool(plugin.get("enabled"))
                toggle = QPushButton("Enabled" if enabled else "Disabled")
                toggle.setCheckable(True)
                toggle.setChecked(enabled)
                toggle.setFixedHeight(30)
                toggle.setStyleSheet(self._toggle_button_style(enabled))
                toggle.clicked.connect(
                    lambda checked, n=plugin["name"], b=toggle:
                        self._set_plugin_enabled(n, checked, b)
                )
                row.addWidget(toggle)
            else:
                status = QLabel("Load error")
                status.setStyleSheet(f"color: {C.ACC}; background: transparent;")
                row.addWidget(status)
            self._plugins_rows_layout.addWidget(card)

        _section("MCP integrations")
        if not mcp_tools:
            empty = QLabel("No MCP tools are currently available.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._plugins_rows_layout.addWidget(empty)
        for tool in mcp_tools or []:
            _tool_card(
                str(tool.get("name", "Unnamed MCP tool")),
                str(tool.get("description", "")),
            )
        self._plugins_rows_layout.addStretch(1)

    def _set_plugin_enabled(self, name: str, enabled: bool, button: QPushButton) -> None:
        try:
            from memory.config_manager import save_plugin_enabled
            save_plugin_enabled(name, enabled)
        except Exception as exc:
            button.setChecked(not enabled)
            self._log_sig.emit(f"ERR: Could not update plugin '{name}' — {exc}")
            return
        button.setText("Enabled" if enabled else "Disabled")
        button.setStyleSheet(self._toggle_button_style(enabled))

    def _build_settings_view(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { width: 5px; background: transparent; border: none; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal { height: 0; }
        """)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        root = QVBoxLayout(content)
        root.setContentsMargins(30, 22, 30, 18)
        root.setSpacing(14)
        title = QLabel("Settings")
        title.setFont(QFont(HEAD(), 21, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        root.addWidget(title)
        self._settings_summary = QLabel()
        self._settings_summary.setWordWrap(True)
        self._settings_summary.setFont(QFont(SANS(), 9))
        self._settings_summary.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        root.addWidget(self._settings_summary)

        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(10)
        action_grid.setVerticalSpacing(10)
        actions = (
            ("Connection and providers", "Edit API providers and runtime configuration", self._show_setup),
            ("Voice and audio devices", "Choose the configured assistant voice and devices", self._open_audio_devices),
            ("Appearance and identity", "Customize assistant identity and interface accent", self._open_customize),
            ("Voice controls", "Wake word, push-to-talk, and microphone controls", lambda: self._ctrl_btn.click()),
            ("Tool settings", "Review settings exposed by installed plugins", self._open_plugin_settings),
            ("Activity journal", "Review events recorded by this application", lambda: self._select_nav("history")),
        )
        for index, (label, detail, callback) in enumerate(actions):
            card = QPushButton()
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setMinimumHeight(72)
            card.setStyleSheet(f"""
                QPushButton {{ text-align: left; color: {C.TEXT}; background: {C.PANEL};
                    border: 1px solid {C.BORDER}; border-radius: 11px; padding: 10px 13px; }}
                QPushButton:hover {{ border-color: {C.BORDER_B}; background: {C.PANEL2}; }}
            """)
            body = QVBoxLayout(card)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(4)
            title_label = QLabel(label)
            title_label.setFont(QFont(SANS(), 10, QFont.Weight.DemiBold))
            title_label.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            desc_label = QLabel(detail)
            desc_label.setWordWrap(True)
            desc_label.setFont(QFont(SANS(), 8))
            desc_label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            body.addWidget(title_label)
            body.addWidget(desc_label)
            title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            desc_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            card.clicked.connect(callback)
            action_grid.addWidget(card, index // 2, index % 2)
        root.addLayout(action_grid)

        preferences = QHBoxLayout()
        self._pref_brief_btn = QPushButton()
        self._pref_brief_btn.setFixedHeight(32)
        self._pref_brief_btn.clicked.connect(self._toggle_brief)
        self._pref_brief_btn.setStyleSheet(self._secondary_button_style())
        preferences.addWidget(self._pref_brief_btn)
        self._pref_autostart_btn = QPushButton()
        self._pref_autostart_btn.setFixedHeight(32)
        self._pref_autostart_btn.clicked.connect(self._toggle_autostart)
        self._pref_autostart_btn.setStyleSheet(self._secondary_button_style())
        preferences.addWidget(self._pref_autostart_btn)
        preferences.addStretch()
        root.addLayout(preferences)
        root.addStretch(1)
        page.setWidget(content)
        self._refresh_settings_summary()
        return page

    def _refresh_settings_summary(self) -> None:
        if not hasattr(self, "_settings_summary"):
            return
        try:
            from memory.config_manager import get_brief_enabled, get_voice
            cfg = _read_full_config()
            voice = get_voice()
            provider = str(cfg.get("llm_provider") or "ollama").strip()
            model = str(cfg.get("llm_model") or "Default").strip()
            live_key = bool(cfg.get("gemini_api_key"))
            wake = self._wake_state()
            self._settings_summary.setText(
                f"Live voice: {voice} · Text provider: {provider} · Model: {model} · "
                f"Gemini Live credentials: {'configured' if live_key else 'not configured'} · "
                f"Wake word: {'enabled' if wake['enabled'] else 'disabled'} · "
                f"Configuration: {'complete' if getattr(self, '_ready', False) else 'incomplete'}"
            )
            self._pref_brief_btn.setText(
                f"Morning briefing: {'On' if get_brief_enabled() else 'Off'}"
            )
            self._pref_autostart_btn.setText(
                f"Launch at sign-in: {'On' if self._check_autostart() else 'Off'}"
            )
        except Exception as exc:
            self._settings_summary.setText(f"Settings could not be read: {exc}")
            self._log_sig.emit(f"ERR: Settings could not be read — {exc}")

    @staticmethod
    def _secondary_button_style() -> str:
        return f"""
            QPushButton {{ color: {C.TEXT_MED}; background: {C.PANEL};
                border: 1px solid {C.BORDER}; border-radius: 8px; padding: 0 11px; }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

    @staticmethod
    def _toggle_button_style(enabled: bool) -> str:
        if enabled:
            return f"""
                QPushButton {{ color: {C.GREEN}; background: rgba(64,227,159,12);
                    border: 1px solid {C.GREEN_D}; border-radius: 8px; padding: 0 10px; }}
                QPushButton:hover {{ background: rgba(64,227,159,20); }}
            """
        return f"""
            QPushButton {{ color: {C.TEXT_DIM}; background: transparent;
                border: 1px solid {C.BORDER}; border-radius: 8px; padding: 0 10px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """

    def _build_command_bar(self) -> QWidget:
        """Primary command composer — the main interaction surface of the application."""
        w = QWidget()
        w.setFixedHeight(106)
        w.setStyleSheet(
            f"background: {C.BG}; border-top: 1px solid {C.BORDER};"
        )
        outer = QVBoxLayout(w)
        outer.setContentsMargins(22, 8, 22, 7)
        outer.setSpacing(6)

        composer = QWidget()
        composer.setObjectName("CommandComposer")
        composer.setFixedHeight(54)
        composer.setStyleSheet(f"""
            QWidget#CommandComposer {{
                background: {C.PANEL2};
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        comp_lay = QHBoxLayout(composer)
        comp_lay.setContentsMargins(8, 0, 9, 0)
        comp_lay.setSpacing(7)

        self._mute_btn = QPushButton("MIC")
        self._mute_btn.setFixedSize(42, 34)
        self._mute_btn.setFont(QFont(SANS(), 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.setToolTip("Microphone (F4)")
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        comp_lay.addWidget(self._mute_btn)

        self._drop_zone = FileDropZone()
        self._drop_zone.setFixedSize(114, 42)
        self._drop_zone.file_selected.connect(self._on_file_selected)
        self._drop_zone.setToolTip("Drop a file here or click to choose a file")
        comp_lay.addWidget(self._drop_zone)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Message JARVIS")
        self._input.setFont(QFont(SANS(), 11))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent; color: {C.TEXT};
                border: none; padding: 0 7px;
            }}
            QLineEdit::placeholder {{ color: {C.TEXT_DIM}; }}
        """)
        self._input.returnPressed.connect(self._send)
        comp_lay.addWidget(self._input, stretch=1)

        send = QPushButton("→"); send.setFixedSize(36, 36)
        send.setFont(QFont(SANS(), 16, QFont.Weight.DemiBold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {C.PRI},stop:1 {C.PRI_DIM});
                color: #07090E; border: none; border-radius: 10px; font-weight: 700;
            }}
            QPushButton:hover {{ background: {C.PRI}; }}
            QPushButton:pressed {{ background: {C.PRI_DIM}; }}
        """)
        send.clicked.connect(self._send)
        comp_lay.addWidget(send)
        outer.addWidget(composer)

        hints = QHBoxLayout()
        hints.setContentsMargins(3, 0, 2, 0)
        self._file_hint = QLabel("Drop or select a file to add context")
        self._file_hint.setFont(QFont(SANS(), 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._file_hint.setMinimumWidth(0)
        hints.addWidget(self._file_hint, 1)
        self._shortcut_hint = QLabel("Return to send  ·  F4 microphone  ·  Esc to interrupt")
        self._shortcut_hint.setFont(QFont(SANS(), 7))
        self._shortcut_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hints.addWidget(self._shortcut_hint)
        self._interrupt_btn = QPushButton("Stop")
        self._interrupt_btn.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM}; border: none; padding: 0 4px; }}
            QPushButton:hover {{ color: {C.ACC}; }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        hints.addWidget(self._interrupt_btn)
        outer.addLayout(hints)
        return w

    def _build_chat_view(self) -> QWidget:
        """Conversation card with the native assistant controls and message feed."""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(12, 12, 8, 12)
        vlay.setSpacing(0)

        card = QFrame()
        card.setObjectName("ConversationCard")
        card.setStyleSheet(
            "QFrame#ConversationCard { background: rgba(10,15,28,210); "
            "border: 1px solid rgba(255,255,255,18); border-radius: 16px; }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        header = QWidget()
        header.setStyleSheet(
            "background: rgba(0,229,255,5); "
            "border-bottom: 1px solid rgba(255,255,255,15);"
        )
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(16, 11, 14, 11)
        header_row.setSpacing(10)

        assistant_mark = QFrame()
        assistant_mark.setFixedSize(36, 36)
        assistant_mark.setStyleSheet(
            "QFrame { background: qconicalgradient(cx:0.5, cy:0.5, "
            "angle:210, stop:0 #00e5ff, stop:0.32 #3d7eff, "
            "stop:0.68 #8b6bff, stop:1 #00e5ff); border-radius: 11px; }"
        )
        assistant_mark_layout = QVBoxLayout(assistant_mark)
        assistant_mark_layout.setContentsMargins(2, 2, 2, 2)
        assistant_mark_text = QLabel("JA")
        assistant_mark_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        assistant_mark_text.setFont(QFont("Orbitron", 9, QFont.Weight.Black))
        assistant_mark_text.setStyleSheet(
            f"color: {C.PRI}; background: {C.BG}; border-radius: 9px;"
        )
        assistant_mark_layout.addWidget(assistant_mark_text)
        header_row.addWidget(assistant_mark)

        conversation_title = QVBoxLayout()
        conversation_title.setSpacing(2)
        self._conversation_title_lbl = QLabel(f"{self._assistant_name} Conversation")
        self._conversation_title_lbl.setFont(QFont(SANS(), 10, QFont.Weight.DemiBold))
        self._conversation_title_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: transparent;"
        )
        conversation_title.addWidget(self._conversation_title_lbl)
        state_row = QHBoxLayout()
        state_row.setSpacing(6)
        self._conversation_state_dot = QLabel("●")
        self._conversation_state_dot.setFont(QFont(SANS(), 7))
        self._conversation_state_dot.setStyleSheet(
            f"color: {C.PRI_DIM}; background: transparent;"
        )
        state_row.addWidget(self._conversation_state_dot)
        self._conversation_state_lbl = QLabel("Starting")
        self._conversation_state_lbl.setFont(QFont(SANS(), 8))
        self._conversation_state_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )
        state_row.addWidget(self._conversation_state_lbl)
        state_row.addStretch(1)
        conversation_title.addLayout(state_row)
        header_row.addLayout(conversation_title)
        header_row.addStretch(1)

        conversation_wake = QPushButton("⚡  WAKE")
        conversation_wake.setFixedHeight(30)
        conversation_wake.setFont(QFont(SANS(), 7, QFont.Weight.Bold))
        conversation_wake.setCursor(Qt.CursorShape.PointingHandCursor)
        conversation_wake.setToolTip("Send a manual wake signal")
        conversation_wake.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_MED}; background: rgba(255,255,255,8);
                border: 1px solid rgba(255,255,255,20);
                border-radius: 9px; padding: 0 10px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border-color: rgba(0,229,255,95);
                background: rgba(0,229,255,12);
            }}
        """)
        conversation_wake.clicked.connect(self._tap_wake_manual)
        header_row.addWidget(conversation_wake)
        card_layout.addWidget(header)

        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QWidget#chatContainer { background: transparent; }
            QScrollBar:vertical { background: transparent; width: 5px; border: none; margin: 0; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; min-height: 24px; }
            QScrollBar::handle:vertical:hover { background: rgba(0,229,255,80); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        self._chat_container = QWidget()
        self._chat_container.setObjectName("chatContainer")
        self._chat_container.setStyleSheet("background: transparent;")
        self._chat_layout = QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(16, 14, 16, 12)
        self._chat_layout.setSpacing(9)
        self._chat_layout.addStretch()

        self._chat_empty_lbl = QWidget()
        self._chat_empty_lbl.setStyleSheet("background: transparent;")
        _es = QVBoxLayout(self._chat_empty_lbl)
        _es.setContentsMargins(10, 12, 10, 12)
        _es.setSpacing(6)
        _es.addStretch(1)
        _ttl = QLabel("Start a conversation")
        _ttl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _ttl.setFont(QFont(HEAD(), 14, QFont.Weight.DemiBold))
        _ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        _es.addWidget(_ttl)
        _sub = QLabel("Type a message, use voice, or attach a file.")
        _sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _sub.setFont(QFont(SANS(), 9))
        _sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        _es.addWidget(_sub)
        _es.addStretch(1)
        self._chat_layout.addWidget(self._chat_empty_lbl)

        self._chat_scroll.setWidget(self._chat_container)
        card_layout.addWidget(self._chat_scroll, 1)
        vlay.addWidget(card, 1)
        return w

    def _build_tasks_view(self) -> QWidget:
        """Agent task list with live status."""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(24, 20, 24, 16)
        vlay.setSpacing(14)

        # Header
        hdr = QHBoxLayout()
        t = QLabel("Agent Tasks")
        _hf = QFont(HEAD(), 20, QFont.Weight.Bold)
        t.setFont(_hf)
        t.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        hdr.addWidget(t)
        hdr.addStretch()
        refresh = QPushButton("↻  Refresh")
        refresh.setFixedHeight(32)
        refresh.setFont(QFont(SANS(), 9))
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,255,255,8); color: {C.TEXT_MED};
                border: 1px solid rgba(255,255,255,26); border-radius: 10px; padding: 0 14px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; background: rgba(0,229,255,15);
                                 border-color: rgba(0,229,255,97); }}
        """)
        refresh.clicked.connect(self._refresh_tasks_view)
        hdr.addWidget(refresh)
        vlay.addLayout(hdr)

        # Scroll area for task rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 5px; border: none; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        self._tasks_view_container = QWidget()
        self._tasks_view_container.setStyleSheet("background: transparent;")
        self._tasks_view_layout = QVBoxLayout(self._tasks_view_container)
        self._tasks_view_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_view_layout.setSpacing(8)
        self._tasks_view_layout.addStretch()
        scroll.setWidget(self._tasks_view_container)
        vlay.addWidget(scroll, stretch=1)
        QTimer.singleShot(200, self._refresh_tasks_view)
        return w

    def _build_journal_view(self) -> QWidget:
        """Full-height activity journal wrapping the shared log widget."""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(24, 20, 24, 16)
        vlay.setSpacing(12)

        hdr = QHBoxLayout()
        t = QLabel("Activity Journal")
        t.setFont(QFont(HEAD(), 20, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        hdr.addWidget(t)
        hdr.addStretch()
        clr = QPushButton("Clear")
        clr.setFixedHeight(32)
        clr.setFont(QFont(SANS(), 9))
        clr.setCursor(Qt.CursorShape.PointingHandCursor)
        clr.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,255,255,8); color: {C.TEXT_MED};
                border: 1px solid rgba(255,255,255,26); border-radius: 10px; padding: 0 14px;
            }}
            QPushButton:hover {{ color: {C.ACC}; border-color: rgba(255,107,107,97); }}
        """)
        clr.clicked.connect(lambda: self._log.clear() if hasattr(self._log, "clear") else None)
        hdr.addWidget(clr)
        vlay.addLayout(hdr)

        # The shared log widget lives here in the journal view
        vlay.addWidget(self._log, stretch=1)
        return w

    # ── Message routing ───────────────────────────────────────────────────────

    def add_chat_message(self, speaker: str, text: str) -> None:
        """Add a styled bubble to the conversation view (main thread only)."""
        if not text.strip():
            return
        # Hide the empty-state hint
        first_message = False
        try:
            if not self._chat_empty_lbl.isHidden():
                first_message = True
                self._chat_empty_lbl.hide()
        except AttributeError:
            pass
        try:
            bubble = _BubbleWidget(speaker, text)
            # Insert before the trailing stretch (last item)
            count = self._chat_layout.count()
            self._chat_layout.insertWidget(count - 1, bubble)
            if first_message:
                available = self._assistant_split.width()
                self._assistant_split.setSizes(
                    [int(available * 0.66), int(available * 0.34)]
                )
            # Scroll to bottom after layout settles
            QTimer.singleShot(40, self._scroll_chat_bottom)
        except Exception:
            pass

    def _scroll_chat_bottom(self) -> None:
        try:
            sb = self._chat_scroll.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def _maybe_add_chat_message(self, text: str) -> None:
        """Parse a write_log string and route conversation turns to the chat view."""
        t = text.strip()
        if not t:
            return
        if t.startswith("You: "):
            self.add_chat_message("user", t[5:])
        elif t.startswith("SYS:"):
            self.add_chat_message("sys", t[4:].strip())
        elif t.startswith("ERR:"):
            self.add_chat_message("sys", f"⚠ {t[4:].strip()}")
        else:
            # Detect "{AssistantName}: text" pattern
            idx = t.find(": ")
            if 0 < idx < 25 and not any(t.startswith(p) for p in
                                         ("FILE:", "TOOL:", "WAKE", "SYS", "ERR")):
                self.add_chat_message("jarvis", t[idx + 2:])

    # ── Task view helpers ──────────────────────────────────────────────────────

    def _refresh_tasks_view(self) -> None:
        """Rebuild the agent tasks view from the database."""
        cont = getattr(self, "_tasks_view_layout", None)
        if cont is None:
            return
        # Clear all rows except the trailing stretch
        while cont.count() > 1:
            item = cont.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        task_store = None
        try:
            from core import tasks as task_store
            all_tasks = task_store.list_tasks(limit=50)
        except Exception as exc:
            if task_store is not None and getattr(task_store, "_DB", object()) is None:
                message = "Agent task storage is not connected in this runtime."
            else:
                message = f"Agent tasks could not be loaded: {exc}"
            error = QLabel(message)
            error.setWordWrap(True)
            error.setStyleSheet(f"color: {C.ACC}; background: transparent;")
            cont.insertWidget(0, error)
            return
        if not all_tasks:
            lbl = QLabel("No agent tasks have been recorded.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFont(QFont(SANS(), 10))
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            cont.insertWidget(0, lbl)
            return
        for task in all_tasks:
            row = self._make_task_row(task)
            cont.insertWidget(cont.count() - 1, row)

    def _make_task_row(self, task: dict) -> QWidget:
        status = task.get("status", "")
        _colors = {
            "running":       (C.PRI,      "rgba(0,229,255,15)",    "rgba(0,229,255,55)"),
            "planned":       (C.ACC2,     "rgba(255,184,78,12)",   "rgba(255,184,78,50)"),
            "done":          (C.GREEN,    "rgba(64,227,159,12)",   "rgba(64,227,159,50)"),
            "failed":        (C.ACC,      "rgba(255,107,107,12)",  "rgba(255,107,107,50)"),
            "awaiting_user": (C.ACC2,     "rgba(255,184,78,18)",   "rgba(255,184,78,65)"),
            "cancelled":     (C.TEXT_DIM, "rgba(255,255,255,5)",   "rgba(255,255,255,15)"),
        }
        col, bg, bd = _colors.get(status, (C.TEXT_DIM, "rgba(255,255,255,8)", "rgba(255,255,255,20)"))
        card = QWidget()
        card.setObjectName("TaskEntry")
        card.setStyleSheet(
            f"QWidget#TaskEntry {{ background: {bg}; border: 1px solid {bd}; "
            "border-radius: 14px; }"
        )
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        dot = QLabel("●")
        dot.setFont(QFont(SANS(), 9))
        dot.setStyleSheet(f"color: {col}; background: transparent; border: none;")
        lay.addWidget(dot)

        tid = QLabel(f"#{task.get('id','?')}")
        _mf = QFont("JetBrains Mono", 9, QFont.Weight.Bold) \
            if "JetBrains Mono" in QFontDatabase.families() \
            else QFont("Courier New", 9, QFont.Weight.Bold)
        tid.setFont(_mf)
        tid.setFixedWidth(38)
        tid.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        lay.addWidget(tid)

        goal_text = str(task.get("goal", ""))[:90]
        goal = QLabel(goal_text)
        goal.setFont(QFont(SANS(), 10))
        goal.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
        goal.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(goal, stretch=1)

        chip_text = status.upper().replace("_", " ").replace("AWAITING USER", "NEEDS APPROVAL")
        chip = QLabel(chip_text)
        _cf = QFont(SANS(), 7, QFont.Weight.ExtraBold)
        _cf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        chip.setFont(_cf)
        chip.setStyleSheet(
            f"color: {col}; border: 1px solid {col}; border-radius: 8px;"
            f" padding: 2px 8px; background: transparent;"
        )
        lay.addWidget(chip)
        return card

    def _build_right_panel(self) -> QWidget:
        _CARD_SS = _glass_card_style("QFrame#IntelligenceCard", 14)
        _SEC_FONT = QFont(SANS(), 7, QFont.Weight.ExtraBold)
        _SEC_FONT.setCapitalization(QFont.Capitalization.AllUppercase)
        _SEC_FONT.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)

        def _eyebrow(txt):
            l = QLabel(txt); l.setFont(_SEC_FONT)
            l.setStyleSheet("color: #3D4F6E; background: transparent;")
            return l

        def _chip_label(txt, color="#495068"):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: rgba(255,255,255,4); "
                            f"border: 1px solid rgba(255,255,255,8); border-radius: 6px; padding: 1px 7px;")
            return l

        w = QWidget(); w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(
            f"background: {C.BG}; border-left: 1px solid {C.BORDER};"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Live activity and actual session telemetry.
        heading = QHBoxLayout()
        title = _eyebrow("Live Intelligence")
        heading.addWidget(title)
        heading.addStretch()
        live = QLabel("● LIVE")
        live.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        live.setStyleSheet(
            f"color: {C.GREEN}; background: transparent; letter-spacing: 1px;"
        )
        heading.addWidget(live)
        lay.addLayout(heading)

        telemetry = QFrame()
        telemetry.setObjectName("IntelligenceCard")
        telemetry.setStyleSheet(_CARD_SS)
        telemetry_lay = QHBoxLayout(telemetry)
        telemetry_lay.setContentsMargins(9, 7, 9, 7)
        telemetry_lay.setSpacing(5)
        self._sess_state_lbl = QLabel("● IDLE")
        self._sess_state_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._sess_state_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )
        telemetry_lay.addWidget(self._sess_state_lbl)
        telemetry_lay.addStretch()
        self._lat_chip  = _chip_label("0ms",     "#00E5FF")
        self._tok_chip  = _chip_label("0 tok",   "#8B92A8")
        self._sess_time_lbl = _chip_label("00:00:00", "#495068")
        for c in (self._lat_chip, self._tok_chip, self._sess_time_lbl):
            telemetry_lay.addWidget(c)
        lay.addWidget(telemetry)

        # Agent tasks are read from the same task store as the full Tasks view.
        tasks_card = QFrame()
        tasks_card.setObjectName("IntelligenceCard")
        tasks_card.setStyleSheet(_CARD_SS)
        tasks_lay = QVBoxLayout(tasks_card)
        tasks_lay.setContentsMargins(9, 8, 9, 8)
        tasks_lay.setSpacing(6)
        tasks_hdr = QHBoxLayout(); tasks_hdr.setSpacing(6)
        tasks_hdr.addWidget(_eyebrow("Active Tasks"))
        tasks_hdr.addStretch()
        self._tasks_badge = QLabel("")
        self._tasks_badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._tasks_badge.setStyleSheet(
            f"color: {C.PRI}; background: {C.PRI_GHO}; "
            f"border: 1px solid {C.BORDER_B}; border-radius: 8px; padding: 1px 8px;"
        )
        self._tasks_badge.setVisible(False); tasks_hdr.addWidget(self._tasks_badge)
        all_tasks = QPushButton("View all  ›")
        all_tasks.setFont(QFont(SANS(), 7))
        all_tasks.setCursor(Qt.CursorShape.PointingHandCursor)
        all_tasks.setStyleSheet(
            f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent; border: none; }}"
            f"QPushButton:hover {{ color: {C.PRI}; }}"
        )
        all_tasks.clicked.connect(lambda: self._select_nav("tasks"))
        tasks_hdr.addWidget(all_tasks)
        tasks_lay.addLayout(tasks_hdr)

        self._tasks_container = QWidget()
        self._tasks_container.setStyleSheet("background: transparent;")
        tc_lay = QVBoxLayout(self._tasks_container)
        tc_lay.setContentsMargins(0,0,0,0); tc_lay.setSpacing(4)
        empty = QLabel("No active tasks"); empty.setFont(QFont(SANS(), 9))
        empty.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; padding: 2px 0;"
        )
        tc_lay.addWidget(empty); tc_lay.addStretch()
        tasks_lay.addWidget(self._tasks_container)
        lay.addWidget(tasks_card)

        # The compact trace is backed by the same log signal as the journal.
        trace_card = QFrame()
        trace_card.setObjectName("IntelligenceCard")
        trace_card.setStyleSheet(_CARD_SS)
        trace_lay = QVBoxLayout(trace_card)
        trace_lay.setContentsMargins(9, 8, 9, 7)
        trace_lay.setSpacing(4)
        trace_hdr = QHBoxLayout(); trace_hdr.setSpacing(6)
        trace_hdr.addWidget(_eyebrow("Live Trace")); trace_hdr.addStretch()
        clr = QPushButton("Clear"); clr.setFont(QFont(SANS(), 8))
        clr.setCursor(Qt.CursorShape.PointingHandCursor)
        clr.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; border: none; }}"
            f"QPushButton:hover {{ color: {C.TEXT}; }}"
        )
        clr.clicked.connect(lambda: self._trace_log.clear() if hasattr(self._trace_log, "clear") else None)
        trace_hdr.addWidget(clr)
        trace_lay.addLayout(trace_hdr)

        self._trace_log = LogWidget()
        self._trace_log.setStyleSheet(
            f"background: transparent; color: {C.TEXT_MED}; border: none; padding: 2px 0;"
        )
        self._trace_log.setMinimumHeight(76)
        trace_lay.addWidget(self._trace_log, stretch=1)
        lay.addWidget(trace_card, stretch=1)
        try:
            self._log_sig.connect(self._trace_log.append_log)
        except Exception:
            pass

        # Existing quick entry points stay available beside the live feed.
        quick_hdr = QHBoxLayout()
        quick_hdr.addWidget(_eyebrow("Quick Actions"))
        quick_hdr.addStretch()
        lay.addLayout(quick_hdr)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        quick_style = f"""
            QPushButton {{
                color: {C.TEXT_MED}; background: {C.PANEL};
                border: 1px solid {C.BORDER}; border-radius: 7px;
                padding: 6px 8px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border-color: {C.BORDER_B};
                background: {C.PRI_GHO};
            }}
        """
        new_command = QPushButton("+  New command")
        new_command.setFont(QFont(SANS(), 8))
        new_command.setCursor(Qt.CursorShape.PointingHandCursor)
        new_command.setStyleSheet(quick_style)
        new_command.clicked.connect(
            lambda: (self._select_nav("chat"), self._input.setFocus())
        )
        quick_row.addWidget(new_command, stretch=1)
        memory = QPushButton("Memory")
        memory.setFont(QFont(SANS(), 8))
        memory.setCursor(Qt.CursorShape.PointingHandCursor)
        memory.setStyleSheet(quick_style)
        memory.clicked.connect(self._open_memory_panel)
        quick_row.addWidget(memory)
        lay.addLayout(quick_row)

        # File drop remains available on the dashboard.
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)
        self._file_hint = QLabel("Drop a file or click to attach")
        self._file_hint.setFont(QFont(SANS(), 8))
        self._file_hint.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent;"
        )
        self._file_hint.setWordWrap(True)
        self._file_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._file_hint)
        return w

    def _quick_action(self, label: str):
        """Chip clicks pre-fill the prompt with a starter phrase."""
        starters = {
            "Ask":       "Tell me about ",
            "Summarise": "Summarise this: ",
            "Translate": "Translate to English: ",
            "Code":      "Write code that ",
        }
        try:
            self._input.setText(starters.get(label, ""))
            self._input.setFocus()
        except Exception:
            pass

    def _refresh_tasks_ui(self, tasks: list) -> None:
        """Repaint the agent tasks card from a fresh task list."""
        cont  = getattr(self, "_tasks_container", None)
        badge = getattr(self, "_tasks_badge", None)
        if cont is None:
            return
        # clear existing rows
        while cont.layout() and cont.layout().count():
            item = cont.layout().takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        active = [t for t in tasks if t.get("status") in ("running", "planned")]
        if badge:
            badge.setText(str(len(active)) if active else "")
            badge.setVisible(bool(active))

        if not active:
            empty = QLabel("No active tasks")
            empty.setFont(QFont(SANS(), 9))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; padding: 4px 0;")
            cont.layout().addWidget(empty)
            return

        dot_colors = {
            "running": C.PRI,
            "planned": C.ACC2,
            "done":    C.GREEN,
            "failed":  C.ACC,
        }
        for task in active[:4]:
            row = QHBoxLayout(); row.setSpacing(8)
            dot = QLabel("●")
            dot.setFont(QFont(SANS(), 7))
            col = dot_colors.get(task.get("status", ""), C.TEXT_DIM)
            dot.setStyleSheet(f"color: {col}; background: transparent;")
            row.addWidget(dot)

            tid = QLabel(f"#{task.get('id', '?')}")
            tid.setFont(QFont(SANS(), 8, QFont.Weight.DemiBold))
            tid.setFixedWidth(28)
            tid.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; font-family: monospace;")
            row.addWidget(tid)

            goal = str(task.get("goal", ""))[:52]
            goal_lbl = QLabel(goal)
            goal_lbl.setFont(QFont(SANS(), 9))
            goal_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            goal_lbl.setMinimumWidth(0)
            row.addWidget(goal_lbl, stretch=1)

            wrap = QWidget()
            wrap.setStyleSheet("background: transparent;")
            wrap.setFixedHeight(28)
            wrap.setLayout(row)
            cont.layout().addWidget(wrap)
        cont.layout().addStretch()

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        self._BTN_PRI, self._BTN_DIM = _BTN_STYLE_PRI, _BTN_STYLE_DIM
        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ SETUP")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(QFont("Courier New", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(QFont("Courier New", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(QFont("Courier New", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        audio_btn = QPushButton("🎧  AUDIO DEVICES")
        audio_btn.setFixedHeight(26)
        audio_btn.setFont(QFont("Courier New", 7))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = QPushButton("🧠  MEMORY")
        mem_btn.setFixedHeight(26)
        mem_btn.setFont(QFont("Courier New", 7))
        mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mem_btn.setStyleSheet(_BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = QPushButton("🧩  PLUGINS")
        plugin_btn.setFixedHeight(26)
        plugin_btn.setFont(QFont("Courier New", 7))
        plugin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugin_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        settings_btn = QPushButton("⚙  PLUGIN SETTINGS")
        settings_btn.setFixedHeight(26)
        settings_btn.setFont(QFont("Courier New", 7))
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(_BTN_STYLE_DIM)
        settings_btn.clicked.connect(self._open_plugin_settings)
        lay.addWidget(settings_btn)

        w.adjustSize()
        return w

    def _build_controls_drawer(self) -> QWidget:
        """The switches that get flicked every day, in their own panel."""
        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(self._quick_drawer.styleSheet())
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        def _row(button, style=None, height=26):
            button.setFixedHeight(height)
            button.setFont(QFont("Courier New", 7))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if style:
                button.setStyleSheet(style)
            lay.addWidget(button)
            return button

        fs_btn = _row(QPushButton("⛶  FULLSCREEN  [F11]"), self._BTN_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)

        self._brief_btn = _row(QPushButton())
        self._brief_btn.clicked.connect(self._toggle_brief)

        self._wake_btn = _row(QPushButton())
        self._wake_btn.clicked.connect(self._toggle_wake_word)
        self._wake_sleep_btn = _row(QPushButton())
        self._wake_sleep_btn.clicked.connect(self._tap_wake_manual)
        # Neutral until the real state lands from the background warm-up.
        self._wake_btn.setText("🎙  WAKE WORD")
        self._wake_btn.setStyleSheet(self._BTN_DIM)
        self._wake_sleep_btn.hide()

        self._ptt_btn = _row(QPushButton())
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        self._refresh_talk_btns()

        self._hud_btn = _row(QLabel())
        self._hud_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._refresh_hud_btn()

        w.adjustSize()
        return w

    def _warm_wake_state(self) -> None:
        """Work out the wake-word state off the UI thread, once.

        It used to be resolved when the drawer opened, and that made opening the
        drawer take two seconds the first time — measured at 2.10s, all of it
        `import openwakeword` dragging in onnxruntime behind it. The button
        count had nothing to do with it; the Qt thread was simply waiting on an
        import. Afterwards the same check costs about a millisecond, so it only
        ever needed to happen somewhere other than in front of the user.
        """
        def work():
            try:
                self._wake_state()
            except Exception:
                pass
            self._wake_btns_sig.emit()
        threading.Thread(target=work, daemon=True, name="wake-state-warm").start()

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._close_controls()
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _toggle_controls(self, checked: bool):
        if checked:
            self._close_setup()
            self._refresh_wake_btns()   # cheap now: the state was warmed at boot
            self._position_ctrl_drawer()
            self._ctrl_drawer.show()
            self._ctrl_drawer.raise_()
        else:
            self._ctrl_drawer.hide()

    def _close_setup(self):
        if hasattr(self, "_quick_drawer"):
            self._quick_drawer.hide()
        if hasattr(self, "_drawer_btn"):
            self._drawer_btn.setChecked(False)

    def _close_controls(self):
        if hasattr(self, "_ctrl_drawer"):
            self._ctrl_drawer.hide()
        if hasattr(self, "_ctrl_btn"):
            self._ctrl_btn.setChecked(False)

    def _place_drawer(self, drawer, left: int):
        _W = 220
        drawer.setFixedWidth(_W)
        drawer.adjustSize()
        drawer.setGeometry(left, 54, _W, drawer.sizeHint().height())

    def _position_quick_drawer(self):
        if hasattr(self, "_quick_drawer"):
            self._position_sidebar_drawer(self._quick_drawer, self._drawer_btn)

    def _position_ctrl_drawer(self):
        if hasattr(self, "_ctrl_drawer"):
            self._position_sidebar_drawer(self._ctrl_drawer, self._ctrl_btn)

    def _position_sidebar_drawer(self, drawer, anchor: QWidget) -> None:
        drawer.setFixedWidth(220)
        drawer.adjustSize()
        origin = anchor.mapTo(self.centralWidget(), QPoint(0, 0))
        left = min(
            max(8, origin.x()),
            max(8, self.centralWidget().width() - drawer.width() - 8),
        )
        top = max(8, origin.y() - drawer.sizeHint().height() - 8)
        drawer.setGeometry(left, top, drawer.width(), drawer.sizeHint().height())

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(8)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask anything…")
        self._input.setFont(QFont(SANS(), 11))
        self._input.setFixedHeight(38)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255,255,255,6); color: {C.TEXT};
                border: 1px solid rgba(255,255,255,16);
                border-radius: 10px; padding: 4px 12px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; background: rgba(255,255,255,10); }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("→")
        send.setFixedSize(38, 38)
        send.setFont(QFont(SANS(), 15, QFont.Weight.DemiBold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI}; color: {C.WHITE};
                border: 1px solid {C.PRI}; border-radius: 10px;
            }}
            QPushButton:hover {{ background: {C.PRI_DIM}; border-color: {C.PRI_DIM}; }}
            QPushButton:pressed {{ background: {C.PRI_DIM}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        # The panel opens below the head, so the head looks down at it. It is a
        # tiny thing that answers "did that land?" before you read a word.
        self.hud.glance(0.0, -0.85, hold=1.3)
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. A review is read, not clicked, so QTextEdit
    # gives scrolling, selection and copy for nothing, and the HUD gains no
    # widget it has to lay out. Severity decides colour and order here because
    # that is presentation; the plugin supplies no styling and knows no palette,
    # which is also what lets a re-theme repaint a review correctly.

    # Severity is marked by a symbol and a colour, not by a word. The findings
    # themselves are in the user's language, and "[SERIOUS]" sitting inside a
    # Turkish sentence is the kind of seam this project tries not to have —
    # while translating the tag would mean a table per language, which is worse.
    # A shape carries it in every language, and shape plus colour still reads
    # for someone who cannot separate red from amber. What the marks mean
    # arrives the way everything else does: JARVIS says it out loud.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def _esc(s) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _show_review(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        e = self._esc
        parts = [f'<div style="color:{C.TEXT}; font-family:Courier New;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>')

        for f in (findings or []):
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f'{e(f.get("heading"))}</span>')
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f'&ldquo;{e(f["quote"])}&rdquo;</div>')
            if f.get("suggestion"):
                parts.append(
                    f'<div style="margin-left:12px; color:{C.PRI};">'
                    f'&rarr; {e(f["suggestion"])}</div>')
            parts.append('</div>')

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                'The document does not settle:</div>')
            for u in unclear:
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_MED};">'
                    f'&middot; {e(u)}</div>')
        parts.append('</div>')

        import time as _time
        self.hud.glance(0.0, -0.85, hold=1.3)
        # Left as written, not upper-cased. The other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules to
        # it: Turkish "Sözleşmesi" comes back "SÖZLEŞMESI", having lost the
        # dotted capital İ. Python has no locale-aware upper to reach for, and
        # imposing one language's rules on all of them is the bug, not the fix.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start)
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 260, 120), 260, 0])

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message JARVIS reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont("Courier New", 8))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {col};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont("Courier New", 7))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont("Courier New", 7))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        quit_btn.clicked.connect(self._hide_quiz)
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame(); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont("Courier New", 9))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont("Courier New", 8))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        self.hud.glance(0.0, -0.85, hold=1.3)
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 250, 120), 0, 250])
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont("Courier New", 9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are JARVIS's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to JARVIS as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _build_footer(self) -> QWidget:
        w = QWidget(); w.setFixedHeight(22)
        w.setStyleSheet("background: rgba(7,9,14,240); border-top: 1px solid rgba(255,255,255,6);")
        lay = QHBoxLayout(w); lay.setContentsMargins(14,0,14,0)

        def _fl(txt, col="#2A3245", fam="Courier New"):
            l = QLabel(txt); l.setFont(QFont(fam, 7))
            l.setStyleSheet(f"color: {col}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [Esc] Stop  ·  [F11] Fullscreen"))
        lay.addStretch()
        self._footer_name_lbl = _fl(
            f"{self._assistant_name}  ·  {APP_VERSION}", C.TEXT_DIM, SANS()
        )
        lay.addWidget(self._footer_name_lbl)
        lay.addStretch()
        self._sess_time_lbl2 = QLabel("Session 00:00:00")
        self._sess_time_lbl2.setFont(QFont("Courier New", 7))
        self._sess_time_lbl2.setStyleSheet("color: #2A3245; background: transparent;")
        lay.addWidget(self._sess_time_lbl2)
        return w

    def _on_file_selected(self, path: str):
        if not path:
            self._current_file = None
            self._file_hint.setText("Drop or select a file to add context")
            return
        self._current_file = path
        p = Path(path)
        try:
            size = _fmt_size(p.stat().st_size)
        except OSError as exc:
            self._current_file = None
            self._drop_zone.clear_file()
            self._file_hint.setText(f"Could not read the selected file: {exc}")
            self._log_sig.emit(f"ERR: Could not read selected file — {exc}")
            return
        self._file_hint.setText(f"{p.name} · {size} · Added as context")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._refresh_settings_summary()
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")
            self._refresh_settings_summary()

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)
        self._refresh_settings_summary()

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")),
                        "enabled": bool(s.get("enabled")),
                        "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled
            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, '_wake_btn'):
            return
        st = self._wake_state()
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("⬇  WAKE WORD: DOWNLOAD")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("🎙  WAKE WORD: ON")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("😴  SLEEP NOW" if st["awake"] else "👂  WAKE NOW")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("🎙  WAKE WORD: OFF")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        from core.hotkey import chord_label
        from memory.config_manager import get_push_to_talk_enabled
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        ptt = get_push_to_talk_enabled()
        self._ptt_btn.setText(f"🎚  PUSH-TO-TALK: {chord_label()}" if ptt
                              else "🎚  PUSH-TO-TALK: OFF")
        self._ptt_btn.setStyleSheet(_on if ptt else _off)
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent "
            "while you are not holding it." if ptt
            else "Hold a key to talk instead of streaming the mic continuously.")


    def _refresh_hud_btn(self):
        self._hud_btn.setText("◉  HUD: TECH INTERFACE")
        self._hud_btn.setToolTip(
            "The live interface reacts to JARVIS state and the actual audio signal."
        )
        self._hud_btn.setStyleSheet(
            f"color: {C.PRI}; background: {C.PANEL2}; "
            f"border: 1px solid {C.BORDER_A}; border-radius: 3px; "
            "text-align: left; padding: 0 8px;"
        )

    def _toggle_ptt(self):
        from memory.config_manager import (get_push_to_talk_enabled,
                                           save_push_to_talk_enabled)
        want = not get_push_to_talk_enabled()
        save_push_to_talk_enabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from core.hotkey import qt_sequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("⬇  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)
            def _work():
                try:
                    from core.wake_word import install_and_download
                    ok, msg = install_and_download(
                        logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)   # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)
            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        if hasattr(self, "_conversation_title_lbl"):
            self._conversation_title_lbl.setText(f"{display} Conversation")
        self._title_lbl.setText(display)
        if hasattr(self, "_footer_name_lbl"):
            self._footer_name_lbl.setText(f"{self._assistant_name}  ·  {APP_VERSION}")
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice
            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        # A deliberate press settles the question: whatever the video did, or is
        # about to do, the user has now said what they want.
        self._video_auto_muted = False
        self._set_muted(not self._muted)

    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("OFF")
            self._mute_btn.setToolTip("Microphone muted — click to unmute (F4)")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(240,100,100,25); color: {C.ACC};
                    border: 1px solid rgba(240,100,100,60); border-radius: 9px;
                }}
                QPushButton:hover {{ background: rgba(240,100,100,40); }}
            """)
        else:
            self._mute_btn.setText("MIC")
            self._mute_btn.setToolTip("Microphone live — click to mute (F4)")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 9px;
                }}
                QPushButton:hover {{ background: {C.PANEL}; color: {C.TEXT}; }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        self._track_user_msg(txt)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _paint_status_chip(self, state: str):
        chip = getattr(self, "_status_chip", None)
        if chip is None:
            return
        _map = {
            "LISTENING":    ("Active",    C.GREEN),
            "SPEAKING":     ("Speaking",  C.PRI),
            "THINKING":     ("Thinking",  C.ACC2),
            "PROCESSING":   ("Working",   C.ACC2),
            "MUTED":        ("Muted",     C.MUTED_C),
            "SLEEPING":     ("Sleeping",  C.TEXT_DIM),
            "INITIALISING": ("Starting",  C.PRI_DIM),
        }
        label, col = _map.get(state, ("Idle", C.TEXT_DIM))
        color = QColor(col)
        fill = f"rgba({color.red()},{color.green()},{color.blue()},20)"
        border = f"rgba({color.red()},{color.green()},{color.blue()},82)"
        chip.setStyleSheet(
            f"color: {col}; background: {fill}; border: 1px solid {border}; "
            "border-radius: 12px; padding: 4px 10px;"
        )
        chip.setText(f"<span style='color:{col}'>●</span>"
                     f"<span style='color:{col};font-weight:600'>  {label}</span>")
        if hasattr(self, "_header_eq"):
            self._header_eq.setVisible(state in ("LISTENING", "SPEAKING"))
        if hasattr(self, "_sidebar_state_lbl"):
            state_label = {
                "LISTENING": "ACTIVE",
                "SPEAKING": "SPEAKING",
                "THINKING": "THINKING",
                "PROCESSING": "WORKING",
                "MUTED": "MICROPHONE MUTED",
                "SLEEPING": "STANDBY",
                "INITIALISING": "CONNECTING",
            }.get(state, "IDLE")
            self._sidebar_state_lbl.setText(state_label)
        if hasattr(self, "_conversation_state_lbl"):
            self._conversation_state_lbl.setText(label)
            self._conversation_state_lbl.setStyleSheet(
                f"color: {col}; background: transparent;"
            )
            self._conversation_state_dot.setStyleSheet(
                f"color: {col}; background: transparent;"
            )

    def _track_user_msg(self, txt: str):
        """Update dashboard counters + histories on each user message."""
        self._msg_count += 1
        toks = max(1, len(txt.split()))
        self._token_count += toks
        self._msgs_hist.append(self._msg_count)
        self._tokens_hist.append(self._token_count)
        if len(self._msgs_hist) > 32:
            del self._msgs_hist[:-32]
        if len(self._tokens_hist) > 32:
            del self._tokens_hist[:-32]
        self._last_user_t = time.time()
        # Broadcast to HUD so it can repaint tiles.
        try:
            self.hud.update()
        except Exception:
            pass

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        self._paint_status_chip(state)
        if hasattr(self, "_core_state_lbl"):
            _core_state = {
                "LISTENING": ("VOICE ONLINE", C.GREEN),
                "SPEAKING": ("JARVIS SPEAKING", C.PRI),
                "THINKING": ("PROCESSING", C.ACC2),
                "PROCESSING": ("PROCESSING", C.ACC2),
                "MUTED": ("MICROPHONE MUTED", C.MUTED_C),
                "SLEEPING": ("WAKE WORD STANDBY", C.TEXT_DIM),
                "INITIALISING": ("CONNECTING", C.PRI_DIM),
            }
            text, color = _core_state.get(state, ("IDLE", C.TEXT_DIM))
            self._core_state_lbl.setText(text)
            self._core_state_lbl.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
            self._core_dot.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
        # Latency = user-msg-time → first SPEAKING transition.
        if state == "SPEAKING" and self._last_user_t is not None:
            lat_ms = (time.time() - self._last_user_t) * 1000.0
            self._last_lat_ms = lat_ms
            self._latency_hist.append(lat_ms)
            if len(self._latency_hist) > 32:
                del self._latency_hist[:-32]
            self._last_user_t = None
        # Push metrics onto the HUD so its tiles can read from one place.
        try:
            self.hud.set_metrics(
                msgs=self._msg_count,
                tokens=self._token_count,
                latency_ms=self._last_lat_ms,
                tokens_hist=self._tokens_hist,
                msgs_hist=self._msgs_hist,
                latency_hist=self._latency_hist,
                session_start=self._session_start,
            )
        except Exception:
            pass
        # Update session telemetry strip
        try:
            lbl = getattr(self, "_sess_state_lbl", None)
            if lbl:
                _sm = {
                    "LISTENING":    "● LISTENING",
                    "SPEAKING":     "▶ SPEAKING",
                    "THINKING":     "◎ THINKING",
                    "PROCESSING":   "◎ WORKING",
                    "MUTED":        "◉ MUTED",
                    "SLEEPING":     "◌ SLEEPING",
                    "INITIALISING": "◌ STARTING",
                }
                _sc = {
                    "LISTENING":    C.GREEN,
                    "SPEAKING":     C.PRI,
                    "THINKING":     C.ACC2,
                    "PROCESSING":   C.ACC2,
                    "MUTED":        C.MUTED_C,
                    "SLEEPING":     C.TEXT_DIM,
                    "INITIALISING": C.PRI_DIM,
                }
                lbl.setText(_sm.get(state, "◌ IDLE"))
                lbl.setStyleSheet(
                    f"color: {_sc.get(state, C.TEXT_DIM)}; "
                    f"background: transparent; font-weight: 700;"
                )
        except Exception:
            pass
        try:
            lat_lbl = getattr(self, "_lat_chip", None)
            if lat_lbl and self._last_lat_ms > 0:
                lat_lbl.setText(f"{self._last_lat_ms:.0f}ms")
        except Exception:
            pass
        try:
            tok_lbl = getattr(self, "_tok_chip", None)
            if tok_lbl:
                tok_lbl.setText(f"{self._token_count:,} tok")
        except Exception:
            pass

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        existing = _read_full_config()
        ov = SetupOverlay(self.centralWidget(), existing=existing)
        cw = self.centralWidget()
        ow, oh = 480, 560
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, payload: dict):
        os.makedirs(CONFIG_DIR, exist_ok=True)

        # Preserve any pre-existing keys (hud_style, assistant_name, …) by merging.
        existing: dict = {}
        if API_FILE.exists():
            try:
                existing = json.loads(API_FILE.read_text(encoding="utf-8")) or {}
            except Exception:
                existing = {}

        merged = {**existing}
        merged["gemini_api_key"] = payload.get("gemini_api_key", "")
        merged["os_system"]      = payload.get("os_system", "")

        provider  = (payload.get("llm_provider") or "").strip().lower()
        llm_key   = (payload.get("llm_api_key")  or "").strip()
        llm_model = (payload.get("llm_model")    or "").strip()

        if provider:
            merged["llm_provider"] = provider
        # Only persist llm_api_key when the provider needs one — otherwise
        # drop the field so leftover cloud keys don't linger when the user
        # switches back to Ollama.
        if provider == "ollama" or not provider:
            merged.pop("llm_api_key", None)
        elif llm_key:
            merged["llm_api_key"] = llm_key
        if llm_model:
            merged["llm_model"] = llm_model

        API_FILE.write_text(json.dumps(merged, indent=4), encoding="utf-8")

        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        os_name = payload.get("os_system", "?")
        self._log.append_log(
            f"SYS: Initialised. OS={os_name.upper()}. "
            f"LLM={provider or 'ollama'}. {self._assistant_name} online."
        )


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self.root = _RootShim(self._app)
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_core_tools(self):
        return self._win.get_core_tools

    @get_core_tools.setter
    def get_core_tools(self, cb):
        self._win.get_core_tools = cb

    @property
    def get_actions(self):
        return self._win.get_actions

    @get_actions.setter
    def get_actions(self, cb):
        self._win.get_actions = cb

    @property
    def get_mcp_tools(self):
        return self._win.get_mcp_tools

    @get_mcp_tools.setter
    def get_mcp_tools(self, cb):
        self._win.get_mcp_tools = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def on_wake_toggle(self):
        return self._win.on_wake_toggle

    @on_wake_toggle.setter
    def on_wake_toggle(self, cb):
        self._win.on_wake_toggle = cb

    @property
    def on_wake_manual(self):
        return self._win.on_wake_manual

    @on_wake_manual.setter
    def on_wake_manual(self, cb):
        self._win.on_wake_manual = cb

    @property
    def wake_get_state(self):
        return self._win.wake_get_state

    @wake_get_state.setter
    def wake_get_state(self, cb):
        self._win.wake_get_state = cb

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: post a schedule of (level, openness, width) mouth frames
        for JARVIS's own speech. `at` is the wall-clock time the batch begins to
        sound, not the time of the call. See HudCanvas.push_visemes()."""
        try:
            self._win.hud.push_visemes(frames, hop, at)
        except Exception:
            pass

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board.

        `grade(question, given)` decides each answer — the plugin supplies it so
        the marking rules live with the questions rather than being duplicated
        here. Returning None from it means "JARVIS should judge this one", which
        is how open answers and near-miss gap-fills are handled.

        Returns immediately: the user answers at their own pace and the finished
        result is delivered back through on_text_command.
        """
        self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)

    def hide_quiz(self) -> None:
        """Thread-safe: clear any quiz currently on the board."""
        self._win._quiz_hide_sig.emit()

    def show_review(self, title: str, summary: str, findings, unclear=None) -> None:
        """Thread-safe: lay a document review into the panel below the HUD.

        `findings` is a list of {heading, detail, severity, quote, suggestion};
        severity is one of 'serious' / 'caution' / 'note' and decides colour and
        order here, so the caller supplies no styling of its own.
        """
        self._win._review_sig.emit(str(title or ""), str(summary or ""),
                                   list(findings or []), list(unclear or []))

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def show_video(self, source: str, title: str = "", muted: bool = True,
                   audio_source: str = "") -> None:
        """Thread-safe: play a video where the avatar normally is.

        `source` is a local path or a direct media URL. `audio_source` is for
        the case where the sound arrives as its own stream — YouTube serves no
        combined format any more — and when it is given the two are played
        together and kept in step.

        Muted by default, and that is a decision rather than a default: a
        soundtrack talking over JARVIS is the one way this could make the
        assistant worse. The user turns sound on from the header button or by
        asking, and closes it the same two ways.
        """
        self._win._video_open_sig.emit(str(source or ""), str(title or ""),
                                       bool(muted), str(audio_source or ""))

    def stop_video(self) -> None:
        """Thread-safe: close the video and give the HUD back to the avatar."""
        self._win._video_close_sig.emit()

    def set_video_muted(self, muted: bool) -> None:
        """Thread-safe: silence or unsilence whatever is playing."""
        self._win._video_mute_sig.emit(bool(muted))

    def video_is_playing(self) -> bool:
        return bool(self._win.video_is_playing())

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")