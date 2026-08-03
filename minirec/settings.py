"""Typed, XDG-aware settings persistence for MiniRec.

The recording pipeline consumes :class:`~minirec.models.RecordingSettings`,
while this module adds the two desktop policy choices which do not belong in
the audio backend: application language and suspend inhibition.  Settings are
written as private UTF-8 JSON using a same-directory temporary file, ``fsync``
and an atomic replacement.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from enum import Enum
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Final

from .models import (
    BITRATE_OPTIONS_KBPS,
    DEFAULT_BITRATE_KBPS,
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    ChannelMode,
    RecordingFormat,
    RecordingSettings,
)


APPLICATION_DIRECTORY: Final = "minirec"
SETTINGS_FILE_NAME: Final = "settings.json"
MAX_SETTINGS_BYTES: Final = 256 * 1024
LANGUAGE_SYSTEM: Final = "system"
LANGUAGE_ENGLISH: Final = "en"
LANGUAGE_CZECH: Final = "cs"

_SETTINGS_LOCK = threading.RLock()
_KNOWN_KEYS = frozenset(
    {
        "version",
        "language",
        "recording_format",
        "format",  # accepted legacy/development alias
        "bitrate_kbps",
        "channel_mode",
        "stereo",  # accepted Android-compatible alias
        "gain_db",
        "prevent_sleep",
        "prevent_sleep_during_recording",
    }
)


class SettingsError(Exception):
    """Base class for a settings persistence failure."""


class SettingsReadError(SettingsError):
    """The settings file could not be read."""


class SettingsFormatError(SettingsError):
    """The settings file is not a bounded UTF-8 JSON object."""


class SettingsWriteError(SettingsError):
    """The settings file could not be durably replaced."""


class AppLanguage(str, Enum):
    """Stable values persisted for the user-interface language."""

    SYSTEM = LANGUAGE_SYSTEM
    ENGLISH = LANGUAGE_ENGLISH
    CZECH = LANGUAGE_CZECH

    @classmethod
    def from_storage_value(cls, value: object) -> AppLanguage:
        if isinstance(value, str):
            try:
                return cls(value.strip().casefold())
            except ValueError:
                pass
        return cls.SYSTEM


# A shorter name is convenient in UI code and remains backwards compatible
# with the Android terminology.
Language = AppLanguage


@dataclass(frozen=True, slots=True)
class XdgPaths:
    """Resolved MiniRec configuration and transient state directories."""

    config_dir: Path
    state_dir: Path

    @property
    def settings_json(self) -> Path:
        return self.config_dir / SETTINGS_FILE_NAME

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
    ) -> XdgPaths:
        """Resolve XDG paths, ignoring relative overrides as required by XDG."""

        values = os.environ if environment is None else environment
        resolved_home = Path.home() if home is None else Path(home).expanduser()
        config_home = _absolute_xdg_path(
            values.get("XDG_CONFIG_HOME"), resolved_home / ".config"
        )
        state_home = _absolute_xdg_path(
            values.get("XDG_STATE_HOME"), resolved_home / ".local" / "state"
        )
        return cls(
            config_dir=config_home / APPLICATION_DIRECTORY,
            state_dir=state_home / APPLICATION_DIRECTORY,
        )


def default_settings_path(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the default XDG settings JSON path."""

    return XdgPaths.from_environment(environment, home=home).settings_json


def default_state_dir(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the XDG state directory used for recovery journals."""

    return XdgPaths.from_environment(environment, home=home).state_dir


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Sanitized application settings plus preserved future JSON fields."""

    language: AppLanguage = AppLanguage.SYSTEM
    recording: RecordingSettings = field(default_factory=RecordingSettings)
    prevent_sleep: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def format(self) -> RecordingFormat:
        return self.recording.format

    @property
    def recording_format(self) -> RecordingFormat:
        return self.recording.format

    @property
    def bitrate_kbps(self) -> int:
        return self.recording.bitrate_kbps

    @property
    def channel_mode(self) -> ChannelMode:
        return self.recording.channel_mode

    @property
    def stereo(self) -> bool:
        return self.recording.channel_mode is ChannelMode.STEREO

    @property
    def gain_db(self) -> int:
        return self.recording.gain_db

    @property
    def prevent_sleep_during_recording(self) -> bool:
        return self.prevent_sleep

    def with_changes(self, **changes: object) -> AppSettings:
        """Return a new sanitized snapshot using flat UI-friendly field names."""

        values = self.to_dict()
        # Canonical persisted keys are already present in ``values``.  Merely
        # adding an accepted alias such as ``format`` or ``stereo`` would leave
        # that older canonical value with precedence in ``from_mapping``.  Map
        # all flat UI spellings onto canonical keys before sanitizing instead.
        remaining = dict(changes)

        if "recording_format" in remaining:
            format_value = remaining.pop("recording_format")
            remaining.pop("format", None)
            values["recording_format"] = _format_storage_value(format_value)
        elif "format" in remaining:
            values["recording_format"] = _format_storage_value(
                remaining.pop("format")
            )

        if "channel_mode" in remaining:
            channel_value = remaining.pop("channel_mode")
            remaining.pop("stereo", None)
            values["channel_mode"] = _channel_storage_value(channel_value)
        elif "stereo" in remaining:
            stereo_value = remaining.pop("stereo")
            if stereo_value is True:
                values["channel_mode"] = "stereo"
            elif stereo_value is False:
                values["channel_mode"] = "mono"
            else:
                values["channel_mode"] = stereo_value

        if "prevent_sleep" in remaining:
            values["prevent_sleep"] = remaining.pop("prevent_sleep")
            remaining.pop("prevent_sleep_during_recording", None)
        elif "prevent_sleep_during_recording" in remaining:
            values["prevent_sleep"] = remaining.pop(
                "prevent_sleep_during_recording"
            )

        if "language" in remaining:
            language_value = remaining.pop("language")
            values["language"] = (
                language_value.value
                if isinstance(language_value, AppLanguage)
                else language_value
            )

        values.update(remaining)
        return AppSettings.from_mapping(values)

    def to_dict(self) -> dict[str, Any]:
        """Serialize known values while retaining unknown future keys."""

        result = dict(self.extra)
        # Canonical keys deliberately replace accepted aliases from older
        # development snapshots, so each subsequent write converges.
        for alias in _KNOWN_KEYS:
            result.pop(alias, None)
        result.update(
            {
                "version": 1,
                "language": self.language.value,
                "recording_format": self.recording.format.storage_value,
                "bitrate_kbps": self.recording.bitrate_kbps,
                "channel_mode": self.recording.channel_mode.name.casefold(),
                "gain_db": self.recording.gain_db,
                "prevent_sleep": self.prevent_sleep,
            }
        )
        return result

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> AppSettings:
        """Sanitize a persisted object without discarding unfamiliar fields."""

        raw: dict[str, object] = dict(values or {})
        language = AppLanguage.from_storage_value(raw.get("language"))

        format_value = raw.get("recording_format", raw.get("format"))
        recording_format = (
            format_value
            if isinstance(format_value, RecordingFormat)
            else RecordingFormat.from_storage_value(
                format_value if isinstance(format_value, str) else None
            )
        )

        bitrate = _sanitize_bitrate(raw.get("bitrate_kbps"))
        channel_mode = _sanitize_channel_mode(raw)
        gain_db = _bounded_int(raw.get("gain_db"), MIN_GAIN_DB, MAX_GAIN_DB, 0)
        prevent_sleep = _strict_bool(
            raw.get(
                "prevent_sleep",
                raw.get("prevent_sleep_during_recording", False),
            ),
            False,
        )
        extras = {key: value for key, value in raw.items() if key not in _KNOWN_KEYS}
        return cls(
            language=language,
            recording=RecordingSettings(
                format=recording_format,
                bitrate_kbps=bitrate,
                channel_mode=channel_mode,
                gain_db=gain_db,
            ),
            prevent_sleep=prevent_sleep,
            extra=extras,
        )


# Familiar aliases make the domain model easy to consume from either the GTK
# layer or code ported from Android.
RecorderSettings = AppSettings
Settings = AppSettings


class SettingsStore:
    """Load and atomically save one typed settings snapshot."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        self.path = (
            Path(path)
            if path is not None
            else default_settings_path(environment, home=home)
        )

    def load(self) -> AppSettings:
        """Read a sanitized snapshot; a missing file means defaults."""

        with _SETTINGS_LOCK:
            try:
                with self.path.open("rb") as source:
                    payload = source.read(MAX_SETTINGS_BYTES + 1)
            except FileNotFoundError:
                return AppSettings()
            except OSError as error:
                raise SettingsReadError(f"Could not read settings from {self.path}") from error
        if len(payload) > MAX_SETTINGS_BYTES:
            raise SettingsFormatError("Settings file exceeds its byte limit")
        try:
            decoded = json.loads(
                payload.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
            raise SettingsFormatError("Settings file is not valid UTF-8 JSON") from error
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) for key in decoded
        ):
            raise SettingsFormatError("Settings root must be a JSON object")
        return AppSettings.from_mapping(decoded)

    def save(self, settings: AppSettings | Mapping[str, object]) -> AppSettings:
        """Sanitize and durably replace the complete settings object."""

        snapshot = (
            settings
            if isinstance(settings, AppSettings)
            else AppSettings.from_mapping(settings)
        )
        payload = _json_bytes(snapshot.to_dict(), maximum=MAX_SETTINGS_BYTES)
        with _SETTINGS_LOCK:
            _atomic_write(self.path, payload)
        return snapshot

    def update(
        self,
        values: Mapping[str, object] | None = None,
        **changes: object,
    ) -> AppSettings:
        """Merge changes with the current object and retain unknown keys."""

        requested = dict(values or {})
        requested.update(changes)
        with _SETTINGS_LOCK:
            current = self.load()
            updated = current.with_changes(**requested)
            return self.save(updated)

    def reset(self) -> AppSettings:
        """Persist defaults while retaining no obsolete or future fields."""

        return self.save(AppSettings())

    # Android-compatible convenience spelling.
    current_settings = load
    set_settings = save


SettingsRepository = SettingsStore


def _format_storage_value(value: object) -> object:
    return value.storage_value if isinstance(value, RecordingFormat) else value


def _channel_storage_value(value: object) -> object:
    return value.name.casefold() if isinstance(value, ChannelMode) else value


def _sanitize_bitrate(value: object) -> int:
    candidate = _coerce_finite_int(value)
    if candidate is None:
        return DEFAULT_BITRATE_KBPS
    # Persisted values from early builds accepted the complete 32..320 range;
    # snap them to the closest current UI choice, preferring the lower choice
    # exactly halfway between two values.
    return min(BITRATE_OPTIONS_KBPS, key=lambda option: (abs(option - candidate), option))


def _sanitize_channel_mode(values: Mapping[str, object]) -> ChannelMode:
    value = values.get("channel_mode")
    if isinstance(value, ChannelMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"mono", "1"}:
            return ChannelMode.MONO
        if normalized in {"stereo", "2"}:
            return ChannelMode.STEREO
    if type(value) is int and value in (1, 2):
        return ChannelMode.from_channels(value)
    stereo = values.get("stereo")
    if type(stereo) is bool:
        return ChannelMode.STEREO if stereo else ChannelMode.MONO
    return ChannelMode.STEREO


def _bounded_int(
    value: object,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    candidate = _coerce_finite_int(value)
    if candidate is None:
        return default
    return min(maximum, max(minimum, candidate))


def _coerce_finite_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # JSON can contain an integral float; accepting it is harmless and makes
    # sanitization resilient without accepting strings such as "128kbps".
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _strict_bool(value: object, default: bool) -> bool:
    return value if type(value) is bool else default


def _absolute_xdg_path(value: str | None, fallback: Path) -> Path:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    return fallback


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def _json_bytes(value: Mapping[str, object], *, maximum: int) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SettingsFormatError("Settings contain a non-JSON value") from error
    if len(payload) > maximum:
        raise SettingsFormatError("Settings file exceeds its byte limit")
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    """Durably replace *path* from a private same-directory temporary file."""

    descriptor = -1
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Existing XDG directories can have inherited permissions; files and
        # journals still remain private even in that case.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise SettingsWriteError(f"Could not atomically write {path}") from error


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "APPLICATION_DIRECTORY",
    "AppLanguage",
    "AppSettings",
    "Language",
    "RecorderSettings",
    "Settings",
    "SettingsError",
    "SettingsFormatError",
    "SettingsReadError",
    "SettingsRepository",
    "SettingsStore",
    "SettingsWriteError",
    "XdgPaths",
    "default_settings_path",
    "default_state_dir",
]
