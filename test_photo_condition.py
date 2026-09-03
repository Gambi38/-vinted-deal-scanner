import asyncio
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from photo_condition import PhotoConditionAnalyzer, enrich_rows_with_photos


def image_bytes(color, size=(400, 400), image_format="JPEG"):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class PhotoConditionTests(unittest.TestCase):
    def test_dark_uniform_photo_is_flagged_as_quality_risk(self):
        analyzer = PhotoConditionAnalyzer(model_path="missing.onnx")
        result = analyzer.analyze_bytes(image_bytes((5, 5, 5)))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["model_used"])
        self.assertIn("photo très sombre", result["risks"])
        self.assertIn("photo peu contrastée", result["risks"])
        self.assertIn("photo possiblement floue", result["risks"])
        self.assertEqual(result["condition"], "unknown")

    def test_low_resolution_is_detected(self):
        analyzer = PhotoConditionAnalyzer(min_resolution=320)
        result = analyzer.analyze_bytes(image_bytes((120, 120, 120), (100, 200)))
        self.assertIn("photo basse résolution", result["risks"])

    def test_invalid_image_never_crashes(self):
        analyzer = PhotoConditionAnalyzer()
        result = analyzer.analyze_bytes(b"pas une image")
        self.assertEqual(result["status"], "invalid")

    def test_oversized_payload_is_rejected_before_decode(self):
        analyzer = PhotoConditionAnalyzer(max_download_bytes=100_000)
        result = analyzer.analyze_bytes(b"x" * 100_001)
        self.assertEqual(result["status"], "invalid")

    def test_onnx_result_is_used_only_with_loaded_model(self):
        class Input:
            name = "input"
            shape = [1, 3, 8, 8]

        class Session:
            def get_inputs(self):
                return [Input()]

            def run(self, outputs, inputs):
                return [[[8.0, 0.0, 0.0, 0.0]]]

        with patch("photo_condition._load_onnx_session", return_value=Session()):
            analyzer = PhotoConditionAnalyzer(
                model_path="condition.onnx",
                labels=("damaged", "used", "good", "new"),
                confidence_threshold=0.7,
            )
        result = analyzer.analyze_bytes(image_bytes((100, 120, 140)))
        self.assertTrue(result["model_used"])
        self.assertEqual(result["condition"], "damaged")
        self.assertGreater(result["confidence"], 0.9)
        self.assertTrue(any("état damaged" in risk for risk in result["risks"]))

    def test_async_enrichment_preserves_rows_on_failure(self):
        class Analyzer:
            model_loaded = False
            confidence_threshold = 0.7
            analyze_url = AsyncMock(return_value={
                "status": "ok", "condition": "unknown", "confidence": 0,
                "model_used": False, "risks": ["photo très sombre"],
                "width": 400, "height": 400,
            })

        rows = [{"image_url": "https://example/image.jpg", "risk": ""}]
        summary = asyncio.run(enrich_rows_with_photos(
            rows, object(), Analyzer(), max_images=1,
        ))
        self.assertEqual(summary["analysed"], 1)
        self.assertEqual(summary["flagged"], 1)
        self.assertIn("photo très sombre", rows[0]["risk"])


if __name__ == "__main__":
    unittest.main()

