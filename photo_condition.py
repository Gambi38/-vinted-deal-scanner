"""Analyse limitée des photos et inférence ONNX optionnelle.

Sans fichier de modèle entraîné, le module mesure uniquement la qualité de la
photo (résolution, exposition, contraste, flou). Il ne prétend pas déduire
l'état physique d'un objet à partir de ces seules métriques.
"""

from __future__ import annotations

import asyncio
import io
from functools import lru_cache
from pathlib import Path


def _softmax(values):
    import numpy as np

    values = np.asarray(values, dtype=np.float32).reshape(-1)
    # Certains modèles exportent des probabilités, d'autres des logits.
    if (values.size and np.all(values >= 0) and np.all(values <= 1) and
            abs(float(values.sum()) - 1.0) <= 1e-3):
        return values
    values = values - float(values.max())
    exp = np.exp(values)
    return exp / max(float(exp.sum()), 1e-9)


@lru_cache(maxsize=2)
def _load_onnx_session(model_path: str):
    path = Path(model_path)
    if not path.is_file():
        return None
    import onnxruntime as ort

    return ort.InferenceSession(
        str(path), providers=["CPUExecutionProvider"],
    )


class PhotoConditionAnalyzer:
    def __init__(self, model_path=None, labels=(), confidence_threshold=0.70,
                 min_resolution=320, blur_threshold=4.0,
                 max_download_bytes=5_000_000):
        self.model_path = str(model_path or "")
        self.labels = tuple(str(label) for label in labels)
        self.confidence_threshold = float(confidence_threshold)
        self.min_resolution = max(64, int(min_resolution))
        self.blur_threshold = max(0.1, float(blur_threshold))
        self.max_download_bytes = max(100_000, int(max_download_bytes))
        try:
            self.onnx_session = _load_onnx_session(self.model_path)
        except (ImportError, OSError, RuntimeError, ValueError):
            self.onnx_session = None

    @property
    def model_loaded(self):
        return self.onnx_session is not None

    def _onnx_predict(self, image):
        if self.onnx_session is None:
            return "unknown", 0.0
        import numpy as np

        input_meta = self.onnx_session.get_inputs()[0]
        shape = list(input_meta.shape)
        channel_first = len(shape) == 4 and shape[1] in (1, 3)
        if channel_first:
            height = shape[2] if isinstance(shape[2], int) else 224
            width = shape[3] if isinstance(shape[3], int) else 224
        else:
            height = shape[1] if len(shape) == 4 and isinstance(shape[1], int) else 224
            width = shape[2] if len(shape) == 4 and isinstance(shape[2], int) else 224
        expected_channels = shape[1] if channel_first else (
            shape[3] if len(shape) == 4 else 3
        )
        resized = image.convert("L" if expected_channels == 1 else "RGB").resize(
            (width, height),
        )
        array = np.asarray(resized, dtype=np.float32) / 255.0
        if expected_channels == 1:
            array = np.expand_dims(array, axis=-1)
        if channel_first:
            array = np.transpose(array, (2, 0, 1))
        array = np.expand_dims(array, axis=0).astype(np.float32)
        output = self.onnx_session.run(None, {input_meta.name: array})[0]
        probabilities = _softmax(output)
        index = int(probabilities.argmax())
        label = self.labels[index] if index < len(self.labels) else f"class_{index}"
        return label, round(float(probabilities[index]), 4)

    def analyze_bytes(self, content: bytes) -> dict:
        import numpy as np
        from PIL import Image, UnidentifiedImageError

        if not content or len(content) > self.max_download_bytes:
            return {"status": "invalid", "risks": ["photo absente ou trop volumineuse"]}
        try:
            image = Image.open(io.BytesIO(content)).convert("RGB")
        except (OSError, UnidentifiedImageError):
            return {"status": "invalid", "risks": ["photo illisible"]}

        gray = np.asarray(image.convert("L"), dtype=np.float32)
        brightness = float(gray.mean())
        contrast = float(gray.std())
        dx = float(np.abs(np.diff(gray, axis=1)).mean()) if gray.shape[1] > 1 else 0.0
        dy = float(np.abs(np.diff(gray, axis=0)).mean()) if gray.shape[0] > 1 else 0.0
        sharpness = dx + dy
        risks = []
        if min(image.size) < self.min_resolution:
            risks.append("photo basse résolution")
        if brightness < 25:
            risks.append("photo très sombre")
        elif brightness > 235:
            risks.append("photo surexposée")
        if contrast < 12:
            risks.append("photo peu contrastée")
        if sharpness < self.blur_threshold:
            risks.append("photo possiblement floue")

        label, confidence = self._onnx_predict(image)
        if (self.model_loaded and confidence >= self.confidence_threshold and
                label.lower() in {"damaged", "poor", "broken", "parts", "mauvais"}):
            risks.append(f"modèle photo: état {label} ({confidence:.0%})")
        return {
            "status": "ok",
            "width": image.width,
            "height": image.height,
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "sharpness": round(sharpness, 1),
            "condition": label,
            "confidence": confidence,
            "model_used": self.model_loaded,
            "risks": risks,
        }

    async def analyze_url(self, session, url: str, timeout_seconds=5.0) -> dict:
        if not url:
            return {"status": "missing", "risks": ["photo absente"]}
        try:
            async with session.get(url, timeout=float(timeout_seconds)) as response:
                if response.status != 200:
                    return {
                        "status": "http_error",
                        "risks": [f"photo HTTP {response.status}"],
                    }
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_download_bytes:
                    return {"status": "invalid", "risks": ["photo trop volumineuse"]}
                content = await response.read()
        except (OSError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            return {"status": "error", "risks": [f"analyse photo impossible: {exc}"]}
        return await asyncio.to_thread(self.analyze_bytes, content)


async def enrich_rows_with_photos(rows, session, analyzer, max_images=6,
                                  concurrency=2, timeout_seconds=5.0):
    selected = [row for row in rows if row.get("image_url")][:max(0, int(max_images))]
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(row):
        async with semaphore:
            return await analyzer.analyze_url(
                session, row.get("image_url", ""), timeout_seconds,
            )

    results = await asyncio.gather(*(one(row) for row in selected), return_exceptions=True)
    summary = {"analysed": 0, "failed": 0, "flagged": 0, "model_used": analyzer.model_loaded}
    for row, result in zip(selected, results):
        if isinstance(result, Exception):
            summary["failed"] += 1
            continue
        if result.get("status") != "ok":
            summary["failed"] += 1
        else:
            summary["analysed"] += 1
        risks = result.get("risks", [])
        if risks:
            summary["flagged"] += 1
            row["photo_risk"] = "; ".join(risks)
            row["risk"] = "; ".join(filter(None, (row.get("risk"), row["photo_risk"])))
        row["photo_condition"] = result.get("condition", "unknown")
        row["photo_confidence"] = result.get("confidence", 0)
        row["photo_quality"] = {
            key: result.get(key) for key in (
                "width", "height", "brightness", "contrast", "sharpness",
            ) if result.get(key) is not None
        }
        if (result.get("model_used") and risks and
                result.get("confidence", 0) >= analyzer.confidence_threshold):
            row["photo_condition_penalty"] = 100
    return summary
