"""
Supported language codes for crop name translation.
Matches the 14 languages used across Kisaan Sampurna services.
"""

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hn": "Hindi",
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

__all__ = ["SUPPORTED_LANGUAGES", "DEFAULT_LANG"]
