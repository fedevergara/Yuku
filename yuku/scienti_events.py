"""Normalize explicitly declared event types without guessing from titles."""

import re

from kahi_impactu_type_catalog import exact_key

EVENT_TYPE_LABEL = re.compile(r"\bTipo\s+de\s+evento\s*:\s*", re.IGNORECASE)
EVENT_TYPES = {exact_key(value): value for value in
               ("Congreso", "Encuentro", "Seminario", "Simposio", "Taller", "Otro")}


def normalize_event_type(value):
    """Use the last explicit declaration only if it names a known event type.

    Some HTML fields repeat ``Tipo de evento:`` inside their value. The final
    declaration before Ámbito is the structural value; its free-text prefix is
    still preserved in the record's raw_text. Unknown values stay untouched.
    """
    raw = str(value or "").strip()
    candidate = EVENT_TYPE_LABEL.split(raw)[-1].strip(" ,.;:")
    return EVENT_TYPES.get(exact_key(candidate), raw)
