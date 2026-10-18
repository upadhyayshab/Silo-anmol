"""
Supported language codes: the 12 product languages of Kisaan Sampurna
(English, Hindi, Kannada, Marathi, Tamil, Telugu, Malayalam, Gujarati,
Bengali, Odia, Assamese, Punjabi). Any other code falls back to English.

Hindi has two codes: phones and the crop_reports catalog use ISO "hi", while
the crops table stores Hindi in `name_hn` (migration c7d8e9f0a1b2). Both are
accepted; `normalize_lang` returns "hi", and `crop_name_column` maps it back
to the column. The crops table also has Urdu and Nepali names; they're still
indexed for search but aren't offered as UI languages.
"""
from typing import Optional

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "hn": "Hindi",  # legacy code, kept so existing clients sending it still work
    "kn": "Kannada",
    "mr": "Marathi",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "gu": "Gujarati",
    "bn": "Bengali",
    "or": "Odia",
    "as": "Assamese",
    "pa": "Punjabi",
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
