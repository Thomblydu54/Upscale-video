"""Contrat du modèle Upscayl Digital Art expérimental, sans matériel GPU requis."""
from __future__ import annotations

import unittest
from unittest import mock

from upscaler import engine


class ExperimentalModelTests(unittest.TestCase):
    def test_model_is_separate_x4_image_choice_and_routes_to_its_own_weights(self):
        key = "digital-art-4x"
        self.assertEqual(engine.Settings().model, "upscayl-lite-4x")  # le modèle par défaut reste inchangé
        self.assertIn(key, engine.MODELS_BY_KEY)
        self.assertEqual(engine.MODELS_BY_KEY[key].scales, (4,))
        info = engine.VideoInfo("illustration.png", 1000, 1422, "0/1", 0, 0, 1, None,
                                "unknown", "", "rgba", "png", is_image=True, has_alpha=True)
        settings = engine.Settings(model=key, output_size="x4")
        plan = engine.make_image_plan(info, settings)
        self.assertEqual((plan.ai_scale, plan.out_w, plan.out_h), (4, 4000, 5688))

        with mock.patch.object(engine.tools, "find_tool", return_value="realesrgan-ncnn-vulkan"), \
             mock.patch.object(engine.tools, "models_dir", return_value="/models"):
            command = engine.build_ai_cmd(settings, plan, "in.png", "out.png")
        self.assertEqual(command[command.index("-n") + 1], key)
        self.assertEqual(command[command.index("-s") + 1], "4")


if __name__ == "__main__":
    unittest.main()
