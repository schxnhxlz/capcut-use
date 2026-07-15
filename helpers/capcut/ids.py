"""UUID generation and old->new id remapping.

CapCut ids are uppercase RFC-4122 UUIDs with hyphens, e.g.
"8FDC1F2C-DA40-48F7-A635-0C14BD1D72F6". We generate the same style so the
files are indistinguishable from CapCut's own output.
"""

from __future__ import annotations

import uuid


def new_uuid() -> str:
    """Return a fresh uppercase UUID in CapCut's style."""
    return str(uuid.uuid4()).upper()


class IdMap:
    """Stable old->new id mapping.

    Ensures that if the same old id is remapped twice we return the same new
    id, so cross-references (material_id / extra_material_refs / track ids)
    stay consistent after a clone.
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def get(self, old_id: str) -> str:
        """Return the new id for `old_id`, minting one on first use."""
        if not old_id:
            return old_id
        if old_id not in self._map:
            self._map[old_id] = new_uuid()
        return self._map[old_id]

    def fresh(self) -> str:
        """Return a brand-new id not tied to any old id."""
        return new_uuid()

    def __contains__(self, old_id: str) -> bool:
        return old_id in self._map

    def as_dict(self) -> dict[str, str]:
        return dict(self._map)
