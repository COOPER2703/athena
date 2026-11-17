"""Fichier d'état des Plugins : activation et configuration, sans secret.

Le format est celui de l'ADR-0014 : un objet JSON avec ``enabled`` (liste de
noms) et ``config`` (objet par plugin). Le fichier est **non versionné** et
partagé avec d'autres clés éventuelles : une écriture ne doit jamais en perdre
une. Toute écriture est atomique (fichier temporaire puis ``os.replace``) pour
qu'un arrêt brutal ne laisse jamais un JSON tronqué : au pire, l'ancien état
reste en place.

Le fichier n'est **jamais** relu par le serveur en marche : une modification
prend effet au redémarrage (ADR-0025). Ce module ne fait que lire et écrire ;
il ne décide ni de l'activation ni de la validation.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

_MODE = 0o600


class PluginStateError(RuntimeError):
    """Le fichier d'état est illisible ou n'a pas la forme attendue."""


def _enabled_names(raw: object) -> list[str]:
    """Noms activés, normalisés : chaînes non vides, élaguées, sans doublon."""
    if not isinstance(raw, list):
        return []
    names = [
        candidate.strip()
        for candidate in raw
        if isinstance(candidate, str) and candidate.strip()
    ]
    return list(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class PluginState:
    """Vue normalisée du fichier : noms activés et configuration par plugin."""

    enabled: tuple[str, ...] = ()
    config: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


class PluginStateStore:
    """Lecture et écriture tolérantes du fichier d'état des Plugins."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> PluginState:
        """Vue normalisée du fichier ; absent signifie « rien d'activé ».

        Un fichier présent mais illisible lève ``PluginStateError`` : la route
        qui le lit répond alors une erreur stable plutôt que d'écraser
        silencieusement un état qu'elle n'a pas compris.
        """
        raw = self._read_raw()
        if raw is None:
            return PluginState()
        enabled: list[str] = []
        raw_enabled = raw.get("enabled", [])
        if isinstance(raw_enabled, list):
            enabled = _enabled_names(raw_enabled)
        config: dict[str, Mapping[str, object]] = {}
        raw_config = raw.get("config", {})
        if isinstance(raw_config, dict):
            for name, values in raw_config.items():
                if isinstance(values, dict):
                    config[str(name)] = dict(values)
        return PluginState(
            enabled=tuple(dict.fromkeys(enabled)), config=config
        )

    def update(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        config: Mapping[str, str] | None = None,
    ) -> None:
        """Écrit l'activation et/ou la configuration d'un plugin, en une passe.

        Les clés inconnues du fichier — y compris la configuration des autres
        plugins et toute clé de premier niveau qu'un opérateur aurait ajoutée —
        sont préservées telles quelles.
        """
        raw = self._read_raw() or {}
        if enabled is not None:
            names = _enabled_names(raw.get("enabled", []))
            if enabled:
                if name not in names:
                    names.append(name)
            else:
                names = [candidate for candidate in names if candidate != name]
            raw["enabled"] = names
        if config is not None:
            existing = raw.get("config")
            configs = dict(existing) if isinstance(existing, dict) else {}
            merged = dict(configs.get(name, {})) if isinstance(
                configs.get(name), dict
            ) else {}
            merged.update({key: value for key, value in config.items()})
            configs[name] = merged
            raw["config"] = configs
        self._write_raw(raw)

    # -- Sérialisation -------------------------------------------------------

    def _read_raw(self) -> dict[str, object] | None:
        if not self._path.is_file():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PluginStateError(
                f"cannot read plugins state file {self._path}: {exc}"
            ) from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PluginStateError(
                f"plugins state file {self._path} is not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise PluginStateError(
                f"plugins state file {self._path} must be a JSON object"
            )
        return data

    def _write_raw(self, raw: Mapping[str, object]) -> None:
        """Écrit le fichier en une opération atomique, sans perdre de clé."""
        payload = json.dumps(dict(raw), indent=2, ensure_ascii=False) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
        except OSError as exc:
            raise PluginStateError(
                f"cannot write plugins state file {self._path}: {exc}"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, _MODE)
            os.replace(temporary, self._path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise PluginStateError(
                f"cannot write plugins state file {self._path}: {exc}"
            ) from exc
