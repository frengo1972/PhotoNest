"""Model files: where they come from and where they are kept.

Stored in %LOCALAPPDATA%\\photoorg\\models (not in work/, which can be wiped and rebuilt),
downloaded once on first use.
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Callable
from pathlib import Path

HF = "https://huggingface.co"
ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
FACEFUSION = f"{HF}/facefusion/models-3.0.0/resolve/main"  # ONNX exports of the retouching models

MODELS: dict[str, str] = {
    # faces (OpenCV zoo)
    "yunet.onnx": f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "sface.onnx": f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    # CLIP ViT-B/32 (OpenAI weights, ONNX export by Xenova): image encoder + English text encoder for tags
    "clip/vision_fp16.onnx": f"{HF}/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_fp16.onnx",
    "clip/text_fp16.onnx": f"{HF}/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model_fp16.onnx",
    "clip/tokenizer.json": f"{HF}/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json",
    # multilingual text encoder aligned to the same CLIP image space: search typed in Italian
    "mclip/model.onnx": f"{HF}/sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/onnx/model_quint8_avx2.onnx",
    "mclip/tokenizer.json": f"{HF}/sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/tokenizer.json",
    "mclip/dense.safetensors": f"{HF}/sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/2_Dense/model.safetensors",
    # retouching
    "retouch/gfpgan_1.4.onnx": f"{FACEFUSION}/gfpgan_1.4.onnx",
    "retouch/lama.onnx": f"{ZOO}/inpainting_lama/inpainting_lama_2025jan.onnx",
    "retouch/real_esrgan_x2_fp16.onnx": f"{FACEFUSION}/real_esrgan_x2_fp16.onnx",
    "retouch/real_esrgan_x4_fp16.onnx": f"{FACEFUSION}/real_esrgan_x4_fp16.onnx",
}
FACE_MODELS = ("yunet.onnx", "sface.onnx")
CLIP_MODELS = ("clip/vision_fp16.onnx", "clip/text_fp16.onnx", "clip/tokenizer.json")
SEARCH_MODELS = ("mclip/model.onnx", "mclip/tokenizer.json", "mclip/dense.safetensors")
RETOUCH_MODELS = {  # per tool, with the download size shown before the first use
    "faces": (("yunet.onnx", "retouch/gfpgan_1.4.onnx"), 340),
    "inpaint": (("retouch/lama.onnx",), 93),
    "upscale2": (("retouch/real_esrgan_x2_fp16.onnx",), 36),
    "upscale4": (("retouch/real_esrgan_x4_fp16.onnx",), 36),
}


def models_dir() -> Path:
    base = os.environ.get("PHOTOORG_MODELS") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "photoorg" / "models"
    return Path(base)


def path(name: str) -> Path:
    return models_dir() / name


def missing(names: tuple[str, ...]) -> list[str]:
    return [n for n in names if not path(n).is_file()]


def ensure(names: tuple[str, ...], log: Callable[[str], None] = print,
           progress: Callable[[int, int], None] | None = None) -> None:
    """Download the missing files (once). Partial downloads never take the final name."""
    for name in missing(names):
        dest = path(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        log(f"Scarico il modello {name}")
        with urllib.request.urlopen(MODELS[name], timeout=120) as r, open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            for chunk in iter(lambda: r.read(1 << 20), b""):
                out.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done, total)
        os.replace(tmp, dest)
