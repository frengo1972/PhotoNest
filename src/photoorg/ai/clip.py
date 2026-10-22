"""CLIP ViT-B/32 through ONNX Runtime: image and text embeddings in the same 512-d space.

The image encoder runs on the GPU through DirectML when available. The multilingual text encoder
(sentence-transformers clip-ViT-B-32-multilingual-v1) maps Italian queries into the same space.
"""

from __future__ import annotations

import numpy as np
import onnxruntime as ort
from PIL import Image
from safetensors.numpy import load_file
from tokenizers import Tokenizer

from photoorg.ai import models

SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
# onnxruntime 1.24 breaks this fusion on the fp16 CLIP graphs; everything else stays optimised
DISABLED = ["SimplifiedLayerNormFusion"]


def providers(gpu: bool = True) -> list[str]:
    available = ort.get_available_providers()
    preferred = (["DmlExecutionProvider", "CUDAExecutionProvider"] if gpu else []) + ["CPUExecutionProvider"]
    return [p for p in preferred if p in available]


def _session(name: str, gpu: bool) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.log_severity_level = 3  # DirectML warns about shape ops kept on CPU: expected
    return ort.InferenceSession(str(models.path(name)), sess_options=opts, providers=providers(gpu),
                                disabled_optimizers=DISABLED)


def normalize(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-8)


def preprocess(image: Image.Image) -> np.ndarray:
    """Resize the short side to 224 (bicubic), centre crop, CLIP normalisation, CHW float32."""
    image = image.convert("RGB")
    w, h = image.size
    scale = SIZE / min(w, h)
    image = image.resize((max(SIZE, round(w * scale)), max(SIZE, round(h * scale))), Image.Resampling.BICUBIC)
    w, h = image.size
    left, top = (w - SIZE) // 2, (h - SIZE) // 2
    arr = np.asarray(image.crop((left, top, left + SIZE, top + SIZE)), dtype=np.float32) / 255.0
    return ((arr - MEAN) / STD).transpose(2, 0, 1)


class Clip:
    def __init__(self, gpu: bool = True):
        self.vision = _session("clip/vision_fp16.onnx", gpu)
        self._text = None
        self._tokenizer = None

    @property
    def device(self) -> str:
        return "GPU" if self.vision.get_providers()[0] != "CPUExecutionProvider" else "CPU"

    def embed_images(self, batch: list[np.ndarray]) -> np.ndarray:
        out = self.vision.run(["image_embeds"], {"pixel_values": np.stack(batch).astype(np.float32)})[0]
        return normalize(out.astype(np.float32))

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """English text encoder: used for the tag prompts (one at a time: no padding to get wrong)."""
        if self._text is None:
            self._text = _session("clip/text_fp16.onnx", gpu=False)
            self._tokenizer = Tokenizer.from_file(str(models.path("clip/tokenizer.json")))
        rows = []
        for t in texts:
            ids = self._tokenizer.encode(t).ids[:77]
            rows.append(self._text.run(["text_embeds"], {"input_ids": np.array([ids], dtype=np.int64)})[0][0])
        return normalize(np.array(rows, dtype=np.float32))


class MultilingualText:
    """Text in any language -> CLIP image space (mean pooling + linear projection 768 -> 512)."""

    def __init__(self):
        self.session = _session("mclip/model.onnx", gpu=False)
        self.tokenizer = Tokenizer.from_file(str(models.path("mclip/tokenizer.json")))
        self.tokenizer.enable_truncation(128)
        self.tokenizer.enable_padding()
        self.dense = load_file(str(models.path("mclip/dense.safetensors")))["linear.weight"]

    def embed(self, texts: list[str]) -> np.ndarray:
        enc = self.tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        hidden = self.session.run(["last_hidden_state"], {"input_ids": ids, "attention_mask": mask})[0]
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        return normalize(pooled @ self.dense.T)
