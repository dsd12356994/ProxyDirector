"""Camera, movement and framing vocabulary that the appearance channels must not carry.

The video prompt has two free-text channels next to the frozen template: the style block
(prompt_expander.py) and the director's emphasis sentence (tools.py). Camera behaviour belongs to
the camera plan and the reference clip alone, so both channels are checked against these lists.

English terms match whole words with their usual endings, so "pan" does not reject "Japanese" and
"spin" does not reject "spine". Chinese terms match as substrings. This is a lexical guard for the
plain wording a model or a user writes; it does not try to defeat deliberate evasion.
"""
from __future__ import annotations

import re

# label -> pattern. The label is what callers report back to the user or the model.
_CAMERA_EN = {
    "camera": r"cameras?",
    "lens": r"lens(?:es)?",
    "orbit": r"orbit(?:s|ed|ing|al)?",
    "rotate": r"rotat(?:e|es|ed|ing|ion|ions)",
    "spin": r"spin(?:s|ning)?|spun",
    "zoom": r"zoom(?:s|ed|ing)?",
    "pan": r"pan(?:s|ned|ning)?",
    "dolly": r"doll(?:y|ies|ied|ying)",
    "tracking shot": r"tracking[ -]shots?",
    "close-up": r"close[ -]?ups?",
    "wide shot": r"wide[ -]shots?",
    "wide-angle": r"wide[ -]angle",
    "telephoto": r"telephoto",
    "aerial": r"aerial",
    "top-down": r"top[ -]down",
    "crane": r"cranes?",
    "handheld": r"hand[ -]?held",
    "fisheye": r"fish[ -]?eye",
    "field of view": r"field[ -]of[ -]view",
    "fov": r"fov",
}
_CAMERA_ZH = ("镜头", "运镜", "环绕", "旋转", "推近", "拉远", "摇移", "特写", "俯拍", "仰拍",
              "切镜", "变焦", "广角", "长焦", "视角")

# Framing words are fine in a style note ("light from a low angle") but not in the director's
# emphasis sentence, where they would contradict the audited reference clip.
_FRAMING_EN = {
    "frame": r"(?:re)?fram(?:e|es|ed|ing)",
    "margin": r"margins?",
    "clearance": r"clearances?",
    "angle": r"angle[sd]?",
    "elevation": r"elevat(?:e|es|ed|ing|ion|ions)",
    "crop": r"crop(?:s|ped|ping)?",
    "tilt": r"tilt(?:s|ed|ing)?",
}
_FRAMING_ZH = ("取景", "入画", "构图", "边距", "机位", "俯角", "仰角")


def _compile(terms: dict[str, str]) -> list[tuple[str, re.Pattern]]:
    return [(label, re.compile(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])")) for label, pattern in terms.items()]


_CAMERA = _compile(_CAMERA_EN)
_FRAMING = _compile(_FRAMING_EN)


def _found(text: str, english: list[tuple[str, re.Pattern]], chinese: tuple[str, ...]) -> list[str]:
    folded = (text or "").casefold().replace("‐", "-").replace("‑", "-").replace("–", "-")
    return [label for label, pattern in english if pattern.search(folded)] + [t for t in chinese if t in folded]


def camera_terms(text: str) -> list[str]:
    """Camera, lens and movement terms in `text`, each reported once by its label."""
    return _found(text, _CAMERA, _CAMERA_ZH)


def framing_terms(text: str) -> list[str]:
    """Framing terms in `text` (frame, margin, angle, elevation, crop, ...), each reported once."""
    return _found(text, _FRAMING, _FRAMING_ZH)
