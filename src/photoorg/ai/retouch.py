"""Photo retouching: colour, faces, stains and scratches, enlargement. Works on upright BGR uint8 arrays.

- colour: traditional (levels per channel, midtones, gentle local contrast), instant, no model;
- faces: GFPGAN 1.4 (Apache-2.0) on each face found by YuNet, aligned to the FFHQ template, GPU;
- stains, scratches, writing: LaMa inpainting (Apache-2.0, OpenCV zoo export) on the brushed areas, CPU
  (its FFT layers do not run on DirectML);
- enlargement: Real-ESRGAN x2 / x4 (BSD-3) in 256 px tiles, GPU.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

import numpy as np

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2  # noqa: E402  (after the log level)
import onnxruntime as ort  # noqa: E402

from photoorg.ai import models  # noqa: E402
from photoorg.ai.clip import providers  # noqa: E402

Progress = Callable[[int, int], None]

FACE_SIZE = 512
# five landmarks (eyes, nose, mouth corners) of the 512 px FFHQ crop GFPGAN was trained on
FFHQ_512 = np.array([[0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
                     [0.39308822, 0.72541100], [0.61150205, 0.72490465]], dtype=np.float32) * FACE_SIZE
FACE_DETECT_PX = 1600   # faces are found on a copy this size: enough for any face worth restoring
MIN_FACE_PX = 20        # in the detection copy
LAMA_SIZE = 512
TILE = 256              # Real-ESRGAN tiles: DirectML is fast on this fixed shape, much slower on larger ones
TILE_PAD = 16
MAX_UPSCALED_PIXELS = 40_000_000
CAST = 1.0               # share of the per-channel levels (removes casts) over the common ones (keeps tints)
UPSCALE_MODELS = {2: "retouch/real_esrgan_x2_fp16.onnx", 4: "retouch/real_esrgan_x4_fp16.onnx"}


def upscale_options(width: int, height: int) -> list[int]:
    """Enlargements that stay within a sensible size (a 12 MP phone photo has nothing to gain)."""
    return [s for s in UPSCALE_MODELS if width * height * s * s <= MAX_UPSCALED_PIXELS]


# ---------------------------------------------------------------- colour

def auto_color(bgr: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Levels per channel (removes the cast of faded prints and film), midtones, gentle local contrast."""
    img = bgr.astype(np.float32)
    h, w = bgr.shape[:2]
    k = min(1.0, 800 / max(h, w))
    small = cv2.resize(img, (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA).reshape(-1, 3)
    # pure black / white (scanner borders, blown highlights) would hide the real range of the photo
    keep = ~((small <= 4).all(axis=1) | (small >= 251).all(axis=1))
    if keep.mean() > 0.2:
        small = small[keep]
    lo_c, hi_c = np.percentile(small, 0.5, axis=0), np.percentile(small, 99.5, axis=0)
    lo_g, hi_g = lo_c.min(), hi_c.max()
    # mostly per channel (that is what removes a cast), a little of the common range to keep intended tints
    lo, hi = lo_g + CAST * (lo_c - lo_g), hi_g + CAST * (hi_c - hi_g)
    span = np.maximum(hi - lo, 48)  # at most ~5x: a flat channel is not stretched into noise
    out = np.clip((img - lo) / span, 0, 1)
    stretched = np.clip((small - lo) / span, 0, 1).astype(np.float32).reshape(-1, 1, 3)
    median = float(np.median(cv2.cvtColor(stretched, cv2.COLOR_BGR2GRAY)))
    # midtones towards 0.45; night and backlit shots must not turn into daylight
    gamma = float(np.clip(np.log(0.45) / np.log(min(max(median, 1e-3), 0.999)), 0.85, 1.3))
    out = (out ** gamma * 255).astype(np.uint8)

    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.addWeighted(l_ch, 0.5, cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(l_ch), 0.5, 0)
    a_f, b_f = a_ch.astype(np.float32) - 128, b_ch.astype(np.float32) - 128
    sat = float(np.clip(22 / max(float(np.hypot(a_f, b_f).mean()), 1.0), 1.0, 1.25))  # only faded colours get a boost
    a_ch = np.clip(a_f * sat + 128, 0, 255).astype(np.uint8)
    b_ch = np.clip(b_f * sat + 128, 0, 255).astype(np.uint8)
    res = cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2BGR).astype(np.float32)
    return np.clip(img + (res - img) * strength, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- models

def _session(name: str, gpu: bool) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(str(models.path(name)), sess_options=opts, providers=providers(gpu))


class Retoucher:
    """Models are loaded on first use and kept: the second retouch of a session is immediate."""

    def __init__(self) -> None:
        self._sessions: dict[str, ort.InferenceSession] = {}
        self._detector = None
        self._lock = threading.Lock()

    def _get(self, name: str, gpu: bool = True) -> ort.InferenceSession:
        with self._lock:  # a warm-up and the tool itself may ask at the same time
            if name not in self._sessions:
                self._sessions[name] = _session(name, gpu)
            return self._sessions[name]

    def warm(self, tool: str) -> None:
        """Load a tool's model ahead of use (LaMa takes seconds on the CPU) while the user is still brushing."""
        if tool == "inpaint":
            self._get("retouch/lama.onnx", gpu=False)
        elif tool == "faces":
            self._get("retouch/gfpgan_1.4.onnx")

    # ---------- faces

    def find_faces(self, bgr: np.ndarray) -> list[np.ndarray]:
        """Five landmarks (in full-size pixels) of each face."""
        h, w = bgr.shape[:2]
        k = min(1.0, FACE_DETECT_PX / max(h, w))
        small = cv2.resize(bgr, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else bgr
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(str(models.path("yunet.onnx")), "", (320, 320), 0.6, 0.3, 100)
        self._detector.setInputSize((small.shape[1], small.shape[0]))
        _, found = self._detector.detect(small)
        if found is None:
            return []
        return [f[4:14].reshape(5, 2).astype(np.float32) / k for f in found if min(f[2], f[3]) >= MIN_FACE_PX]

    def restore_faces(self, bgr: np.ndarray, strength: float = 0.8, progress: Progress | None = None) -> tuple[np.ndarray, int]:
        """GFPGAN on every face, blended back with a soft edge. Returns the image and how many faces."""
        faces = self.find_faces(bgr)
        if not faces:
            return bgr, 0
        session = self._get("retouch/gfpgan_1.4.onnx")
        h, w = bgr.shape[:2]
        out = bgr.astype(np.float32)
        edge = np.zeros((FACE_SIZE, FACE_SIZE), np.float32)
        edge[40:-40, 40:-40] = 1
        edge = cv2.GaussianBlur(edge, (0, 0), 18)
        for n, landmarks in enumerate(faces, 1):
            matrix, _ = cv2.estimateAffinePartial2D(landmarks, FFHQ_512, method=cv2.LMEDS)
            if matrix is None:
                continue
            crop = cv2.warpAffine(bgr, matrix, (FACE_SIZE, FACE_SIZE), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            x = ((crop[:, :, ::-1].astype(np.float32) / 255 - 0.5) / 0.5).transpose(2, 0, 1)[None]
            y = session.run(None, {"input": x})[0][0]
            face = ((y.clip(-1, 1) + 1) * 127.5).transpose(1, 2, 0)[:, :, ::-1]
            inverse = cv2.invertAffineTransform(matrix)
            back = cv2.warpAffine(face, inverse, (w, h), flags=cv2.INTER_LINEAR)
            mask = cv2.warpAffine(edge, inverse, (w, h), flags=cv2.INTER_LINEAR)[:, :, None] * strength
            out = out * (1 - mask) + back * mask
            if progress:
                progress(n, len(faces))
        return np.clip(out, 0, 255).astype(np.uint8), len(faces)

    # ---------- stains, scratches, writing

    def inpaint(self, bgr: np.ndarray, mask: np.ndarray, progress: Progress | None = None) -> np.ndarray:
        """Fill the masked areas (mask: uint8, >0 where to repaint) from their surroundings."""
        h, w = bgr.shape[:2]
        mask = (mask > 0).astype(np.uint8)
        if not mask.any():
            return bgr
        grow = max(3, round(max(h, w) / 600))
        mask = cv2.dilate(mask, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))  # cover the edge of a stain too
        session = self._get("retouch/lama.onnx", gpu=False)
        out = bgr.copy()
        boxes = _regions(mask, w, h)
        for n, (x0, y0, x1, y1) in enumerate(boxes, 1):
            crop, m = out[y0:y1, x0:x1], mask[y0:y1, x0:x1]
            ch, cw = crop.shape[:2]
            img_in = cv2.resize(crop, (LAMA_SIZE, LAMA_SIZE), interpolation=cv2.INTER_AREA if cw > LAMA_SIZE else cv2.INTER_CUBIC)
            m_in = cv2.resize(m, (LAMA_SIZE, LAMA_SIZE), interpolation=cv2.INTER_NEAREST)
            result = session.run(None, {
                "image": (img_in.astype(np.float32) / 255).transpose(2, 0, 1)[None],
                "mask": (m_in > 0).astype(np.float32)[None, None],
            })[0][0].transpose(1, 2, 0)
            filled = cv2.resize(np.clip(result, 0, 255).astype(np.uint8), (cw, ch), interpolation=cv2.INTER_CUBIC)
            soft = cv2.GaussianBlur(m.astype(np.float32), (0, 0), max(1.0, grow / 2))[:, :, None]
            out[y0:y1, x0:x1] = np.clip(crop * (1 - soft) + filled * soft, 0, 255).astype(np.uint8)
            if progress:
                progress(n, len(boxes))
        return out

    # ---------- enlargement

    def upscale(self, bgr: np.ndarray, scale: int, progress: Progress | None = None) -> np.ndarray:
        session = self._get(UPSCALE_MODELS[scale])
        h, w = bgr.shape[:2]
        rgb = np.ascontiguousarray(bgr[:, :, ::-1])
        step = TILE - 2 * TILE_PAD
        padded = cv2.copyMakeBorder(rgb, TILE_PAD, TILE_PAD + TILE, TILE_PAD, TILE_PAD + TILE, cv2.BORDER_REFLECT_101)
        out = np.zeros((h * scale, w * scale, 3), np.uint8)
        tiles = [(y, x) for y in range(0, h, step) for x in range(0, w, step)]
        for n, (y, x) in enumerate(tiles, 1):
            tile = padded[y:y + TILE, x:x + TILE].astype(np.float32).transpose(2, 0, 1)[None] / 255
            res = session.run(None, {"input": tile})[0][0].transpose(1, 2, 0)
            th, tw = min(step, h - y), min(step, w - x)
            p = TILE_PAD * scale
            out[y * scale:(y + th) * scale, x * scale:(x + tw) * scale] = \
                (res[p:p + th * scale, p:p + tw * scale].clip(0, 1) * 255 + 0.5).astype(np.uint8)
            if progress:
                progress(n, len(tiles))
        return np.ascontiguousarray(out[:, :, ::-1])


def _regions(mask: np.ndarray, w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Square boxes around groups of brushed strokes, with surroundings for context (merged when they overlap)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = []
    for x, y, bw, bh, _ in stats[1:n]:
        side = min(max(w, h), max(LAMA_SIZE // 2, round(max(bw, bh) * 1.8)))
        cx, cy = x + bw / 2, y + bh / 2
        boxes.append([cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2])
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    out = []
    for x0, y0, x1, y1 in boxes:
        x0, y0 = max(0, round(x0)), max(0, round(y0))
        out.append((x0, y0, min(w, max(x0 + 1, round(x1))), min(h, max(y0 + 1, round(y1)))))
    return out
