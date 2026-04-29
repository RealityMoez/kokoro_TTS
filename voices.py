from __future__ import annotations

from typing import List


VOICES: List[str] = [
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_sarah",
    "af_sky",
    "af_kore",
    "af_river",
    "af_nova",
    "am_adam",
    "am_michael",
    "am_puck",
    "am_onyx",
    "am_santa",
    "am_liam",
    "am_fenrir",
    "am_eric",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_george",
    "bm_lewis",
    "bm_fable",
    "bm_daniel",
    "zf_xiaobei",
    "zm_yunjian"
    
]

DEFAULT_VOICE = "af_heart"


def lang_for_voice(voice: str) -> str:
    prefix = voice.split("_", 1)[0]
    if prefix in {"af", "am"}:
        return "a"
    if prefix in {"bf", "bm"}:
        return "b"
    return "a"
