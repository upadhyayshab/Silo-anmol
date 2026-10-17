"""In-memory lookup over the two 104-crop catalogs: `crop_reports` (dossiers,
`name_local` translations for hi/mr) and `crops` (scoring rows, `family`, and
names in all 13 supported languages).

The two catalogs spell 7 crops differently ('Lime/lemon' vs 'Lime / Lemon',
'Rajma' vs 'Rajma (Kidney Bean)', ...), and callers pass names from three
sources (the scoring engine, Agro's crop search, the farmer's language), so
an exact name match dropped those dossiers and 400'd on Agro crop names. Both
catalogs are loaded once per process and indexed by id, case-insensitive
name, slug, slug without brackets, and local-language names from both
catalogs (crops.json only has hi/mr; the crops table has every language, and
spells some Hindi/Marathi names differently).

The catalogs change only when the seed script runs on deploy, so a process
lifetime cache is safe; restart to pick up a reseed.
"""
import asyncio
import re
from typing import Any, Optional

# Common Indian / Agro-catalog names for crops the 104 list spells differently,
# keyed by slug -> crop_reports.crop_id. Only targets that exist are used.
_ALIASES = {
    "paddy": "rice", "corn": "maize", "bajra": "pearl_millet", "jowar": "sorghum",
    "ragi": "finger_millet", "arhar": "pigeon_pea", "tur": "pigeon_pea", "toor": "pigeon_pea",
    "red_gram": "pigeon_pea", "chana": "chickpea", "bengal_gram": "chickpea", "gram": "chickpea",
    "moong": "green_gram", "mung": "green_gram", "urad": "black_gram", "sarson": "mustard",
    "rapeseed": "mustard", "soya": "soybean", "soyabean": "soybean", "soybeans": "soybean",
    "peanut": "groundnut", "bhindi": "okra", "lady_finger": "okra", "ladies_finger": "okra",
    "chili": "chilli", "chillies": "chilli", "eggplant": "brinjal", "baingan": "brinjal",
    # Agro's crop-search spellings (FAO list) for crops the 104 list has.
    "lemon": "lime_lemon", "chili_fresh": "chilli", "chili_dry": "chilli", "cashew_nuts": "cashew",
    "areca": "arecanut", "cassava": "tapioca", "castor_bean": "castor", "alfalfa": "lucerne",
    "pea_dry": "field_pea", "beans_dry": "rajma",
}

# crops-table column -> language code ("name_hn" holds Hindi, ISO "hi").
_NAME_COLUMNS = {
    "name_hn": "hi", "name_kn": "kn", "name_ta": "ta", "name_te": "te", "name_ml": "ml", "name_mr": "mr",
    "name_gu": "gu", "name_bn": "bn", "name_pa": "pa", "name_ur": "ur", "name_ne": "ne", "name_or": "or",
    "name_as": "as",
}

# A key two different crops share (e.g. 'fenugreek' for Leaf and Seed) resolves
# to nobody rather than to whichever row was indexed first.
_AMBIGUOUS = object()


def crop_slug(name: str) -> str:
    """'Lime/lemon' and 'Lime / Lemon' -> 'lime_lemon'; 'Rice (Paddy)' -> 'rice_paddy'."""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _english_keys(name: Any) -> set[str]:
    if not isinstance(name, str) or not name.strip():
        return set()
    without_brackets = re.sub(r"\(.*?\)", " ", name)
    return {name.strip().lower(), crop_slug(name), crop_slug(without_brackets)} - {""}


def _scoring_local_names(row: Any) -> dict[str, str]:
    """{lang: name} from a crops-table row's name_* columns."""
    names = {}
    for column, lang in _NAME_COLUMNS.items():
        value = getattr(row, column, None)
        if isinstance(value, str) and value.strip():
            names[lang] = value.strip()
    return names


def _local_keys(name_local: Any) -> set[str]:
    """'सरसों / राय' is indexed whole and as 'सरसों' and 'राय'."""
    if not isinstance(name_local, dict):
        return set()
    keys: set[str] = set()
    for value in name_local.values():
        if isinstance(value, str) and value.strip():
            keys.add(value.strip().lower())
            keys.update(part.strip().lower() for part in value.split("/") if part.strip())
    return keys


def _query_keys(name: str) -> list[str]:
    """Most specific first, so an exact name wins over its bracket-less slug."""
    if not isinstance(name, str) or not name.strip():
        return []
    without_brackets = re.sub(r"\(.*?\)", " ", name)
    keys = [name.strip().lower(), crop_slug(name), crop_slug(without_brackets)]
    return [k for i, k in enumerate(keys) if k and k not in keys[:i]]


def _add(index: dict, key: str, row: Any) -> None:
    existing = index.get(key)
    if existing is None:
        index[key] = row
    elif existing is not row:
        index[key] = _AMBIGUOUS


class CropCatalog:
    def __init__(self, crop_report_manager, crop_manager=None):
        self.crop_report_manager = crop_report_manager
        self.crop_manager = crop_manager
        self._report_index: Optional[dict] = None
        self._scoring_index: Optional[dict] = None
        self._names_by_crop_id: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._report_index is not None:
            return
        async with self._lock:
            if self._report_index is not None:
                return
            report_rows = (await self.crop_report_manager.fetch_all(limit=0)).items
            report_index: dict = {}
            for row in report_rows:
                keys = _english_keys(row.name_en) | _local_keys(row.name_local)
                if isinstance(row.crop_id, str):
                    keys.add(row.crop_id.lower())
                for key in keys:
                    _add(report_index, key, row)
            scoring_index: dict = {}
            names_by_crop_id: dict[str, dict[str, str]] = {}
            if self.crop_manager is not None:
                scoring_rows = (await self.crop_manager.fetch_all(limit=0)).items
                for row in scoring_rows:
                    for key in _english_keys(row.name_en):
                        _add(scoring_index, key, row)
                # Local names from the crops table, for both lookups: a
                # Kannada name finds its scoring row and its dossier.
                local_keys: dict = {}
                report_of: dict = {}
                for row in scoring_rows:
                    local = _scoring_local_names(row)
                    report = self._lookup(report_index, _query_keys(row.name_en)) if row.name_en else None
                    report_of[id(row)] = report
                    if report is not None:
                        names_by_crop_id[report.crop_id] = local
                    for key in _local_keys(local):
                        _add(local_keys, key, row)
                # setdefault: an English name or a crops.json name already
                # indexed under the same key keeps its meaning.
                for key, row in local_keys.items():
                    scoring_index.setdefault(key, row)
                    report = _AMBIGUOUS if row is _AMBIGUOUS else report_of[id(row)]
                    if report is not None:
                        report_index.setdefault(key, report)
            self._scoring_index = scoring_index
            self._names_by_crop_id = names_by_crop_id
            self._report_index = report_index

    @staticmethod
    def _lookup(index: dict, keys: list[str]) -> Any:
        for key in keys:
            row = index.get(key)
            if row is not None and row is not _AMBIGUOUS:
                return row
        return None

    async def find_report(self, name: Optional[str]) -> Any:
        """crop_reports row for a crop id, English name (any spelling above)
        or local-language name; None when unknown."""
        await self._ensure_loaded()
        if not name:
            return None
        keys = _query_keys(name)
        row = self._lookup(self._report_index, keys)
        if row is None:
            alias = next((_ALIASES[k] for k in keys if k in _ALIASES), None)
            row = self._lookup(self._report_index, [alias]) if alias else None
        if row is None and "/" in name:
            # 'सरसों / राय' or 'Lime/lemon' typed differently: try each part.
            parts = [k for part in name.split("/") for k in _query_keys(part)]
            row = self._lookup(self._report_index, parts)
        return row

    async def find_scoring(self, name: Optional[str]) -> Any:
        """crops (scoring) row for the same kinds of names. Names only the
        dossier catalog knows (a local name, 'Lime / Lemon') are resolved
        through their crop_reports row's id and English name."""
        await self._ensure_loaded()
        if not name:
            return None
        row = self._lookup(self._scoring_index, _query_keys(name))
        if row is not None:
            return row
        report = await self.find_report(name)
        if report is None:
            return None
        return self._lookup(self._scoring_index, [report.crop_id.lower(), *_query_keys(report.name_en)])

    async def local_names(self, report_row: Any) -> dict[str, str]:
        """The crop's name in every language we have: crops.json's curated
        hi/mr names first, then the crops table's names for the rest."""
        await self._ensure_loaded()
        names = dict(self._names_by_crop_id.get(getattr(report_row, "crop_id", None), {}))
        name_local = getattr(report_row, "name_local", None)
        if isinstance(name_local, dict):
            names.update({k: v for k, v in name_local.items() if isinstance(v, str) and v.strip()})
        return names

    async def local_name(self, report_row: Any, lang: str) -> Optional[str]:
        return (await self.local_names(report_row)).get(lang)


__all__ = ["CropCatalog", "crop_slug"]
