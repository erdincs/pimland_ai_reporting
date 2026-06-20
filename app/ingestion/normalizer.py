"""Column / field name normalisation utilities.

Centralised here so both the Excel loader and the connector pipeline share
the same snake_case, ASCII-safe normalisation logic.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Turkish → ASCII transliteration table (lower-case)
_TR_MAP = str.maketrans(
    "çğıiöşü",
    "cgiiösu",
)
_TR_MAP.update(str.maketrans("ÇĞIİÖŞÜ", "CGIIOSU"))
_TR_MAP = str.maketrans(
    "çğışüöÇĞİŞÜÖ",
    "cgisuo CGISUO".replace(" ", ""),
)

# Build a clean translation table
_TRANSLITERATE = {
    # Transliterate BEFORE lower() to avoid Python's İ → i̇ combining-char issue
    ord("Ç"): "C", ord("ç"): "c",
    ord("Ğ"): "G", ord("ğ"): "g",
    ord("İ"): "I", ord("ı"): "i",   # İ=U+0130 → I before lower(); ı=U+0131 → i
    ord("Ö"): "O", ord("ö"): "o",
    ord("Ş"): "S", ord("ş"): "s",
    ord("Ü"): "U", ord("ü"): "u",
    ord("̇"): None,              # strip combining dot above (remnant of İ.lower())
}


def normalise_column_name(name: str) -> str:
    """Convert any string to a valid, ASCII-safe, snake_case SQL identifier.

    Turkish characters are transliterated BEFORE lowercasing to avoid Python's
    combining-character issue with İ (U+0130).
    """
    name = str(name).strip()
    name = name.translate(_TRANSLITERATE)   # transliterate first
    name = name.lower()
    name = re.sub(r"[^\w]+", "_", name)     # no UNICODE flag → ASCII \w only
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "col"


def apply_field_map(
    record: Dict[str, Any],
    field_map: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Rename keys according to field_map, then normalise remaining names.

    Lookup tries exact match first, then normalised-key fallback so YAML keys
    with Turkish characters match regardless of Unicode encoding differences.
    """
    if field_map:
        norm_field_map = {normalise_column_name(fk): tv for fk, tv in field_map.items()}

        def _map(k: str) -> str:
            if k in field_map:
                return field_map[k]
            return norm_field_map.get(normalise_column_name(k), k)

        record = {_map(k): v for k, v in record.items()}
    return {normalise_column_name(k): v for k, v in record.items()}


def normalise_records(
    records: List[Dict[str, Any]],
    field_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Apply field_map + name normalisation to a list of raw records."""
    return [apply_field_map(r, field_map) for r in records]
