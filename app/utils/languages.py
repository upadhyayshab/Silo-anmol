"""
Supported language codes for crop name translation.
Matches the 14 languages used across Kisaan Sampurna services.

Hindi has two codes: phones and the crop_reports catalog use ISO "hi", while
the crops table stores Hindi in `name_hn` (migration c7d8e9f0a1b2). Both are
accepted; `normalize_lang` returns "hi", and `crop_name_column` maps it back
to the column.
"""
from typing import Optional

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "hn": "Hindi",  # legacy code, kept so existing clients sending it still work
    "kn": "Kannada",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "mr": "Marathi",
    "gu": "Gujarati",
    "bn": "Bengali",
    "or": "Odia",
    "pa": "Punjabi",
    "ur": "Urdu",
    "ne": "Nepali",
    "as": "Assamese",
}

DEFAULT_LANG = "en"


def canonical_lang(raw: Optional[str]) -> Optional[str]:
    """'hi', 'HI', 'hi-IN', 'hi_IN', 'hn' -> 'hi'; None for unsupported codes."""
    if not raw:
        return None
    code = raw.strip().lower().replace("_", "-").split("-")[0]
    if code == "hn":
        code = "hi"
    return code if code in SUPPORTED_LANGUAGES else None


def normalize_lang(raw: Optional[str], default: str = DEFAULT_LANG) -> str:
    """canonical_lang, falling back to `default` for missing/unsupported codes."""
    return canonical_lang(raw) or default


def crop_name_column(lang: str) -> str:
    """CropSchema column holding a crop's name in `lang`."""
    return "name_hn" if lang == "hi" else f"name_{lang}"


__all__ = ["SUPPORTED_LANGUAGES", "DEFAULT_LANG", "canonical_lang", "normalize_lang", "crop_name_column"]
