"""GTK-independent data models shared by MiniRec's audio backends.

The classes in this module deliberately import neither PyGObject nor GTK.  They
are safe to use from settings, storage, recovery code and offline unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


BITRATE_OPTIONS_KBPS: tuple[int, ...] = (
    32,
    48,
    64,
    96,
    128,
    160,
    192,
    256,
    320,
)
"""The fixed accessible bitrate choices offered for compressed recordings."""

MIN_GAIN_DB = -12
MAX_GAIN_DB = 12
DEFAULT_BITRATE_KBPS = 128


class RecordingFormat(Enum):
    """A supported recording container and codec combination.

    ``OGG_OPUS`` is the Linux default.  ``.oga`` is used rather than ``.ogg``
    so file managers can identify the result as audio without inspecting it.
    """

    OGG_OPUS = ("opus", ".oga", "audio/ogg")
    MP3 = ("mp3", ".mp3", "audio/mpeg")
    WAV = ("wav", ".wav", "audio/wav")

    def __init__(self, storage_value: str, extension: str, mime_type: str) -> None:
        self.storage_value = storage_value
        self.extension = extension
        self.mime_type = mime_type

    @property
    def is_compressed(self) -> bool:
        """Return whether the format uses a bitrate-controlled encoder."""

        return self is not RecordingFormat.WAV

    def matches_filename(self, filename: str | Path) -> bool:
        """Return whether *filename* has this format's extension."""

        return str(filename).casefold().endswith(self.extension)

    @classmethod
    def from_storage_value(cls, value: str | None) -> RecordingFormat:
        """Read a persisted value, falling back to the Linux default."""

        normalized = (value or "").strip().casefold()
        # Accept descriptive aliases used by early development builds.
        if normalized in {"ogg", "oga", "ogg_opus", "opus"}:
            return cls.OGG_OPUS
        for recording_format in cls:
            if normalized == recording_format.storage_value:
                return recording_format
        return cls.OGG_OPUS

    @classmethod
    def from_filename(cls, filename: str | Path | None) -> RecordingFormat | None:
        """Return the format identified by a filename, or ``None``."""

        if filename is None:
            return None
        return next((item for item in cls if item.matches_filename(filename)), None)

    @classmethod
    def from_mime_type(cls, mime_type: str | None) -> RecordingFormat | None:
        """Return the format identified by a common MIME spelling."""

        normalized = (mime_type or "").partition(";")[0].strip().casefold()
        aliases = {
            "audio/ogg": cls.OGG_OPUS,
            "audio/opus": cls.OGG_OPUS,
            "application/ogg": cls.OGG_OPUS,
            "audio/mpeg": cls.MP3,
            "audio/mp3": cls.MP3,
            "audio/wav": cls.WAV,
            "audio/wave": cls.WAV,
            "audio/x-wav": cls.WAV,
        }
        return aliases.get(normalized)


class ChannelMode(Enum):
    """Requested microphone channel layout."""

    MONO = 1
    STEREO = 2

    @property
    def channels(self) -> int:
        """Return the number of channels represented by the mode."""

        return int(self.value)

    @classmethod
    def from_channels(cls, channels: int) -> ChannelMode:
        """Create a mode from an exact channel count."""

        if type(channels) is not int:
            raise TypeError("channels must be an integer")
        if channels == 1:
            return cls.MONO
        if channels == 2:
            return cls.STEREO
        raise ValueError("MiniRec supports exactly one or two channels")


@dataclass(frozen=True, slots=True)
class RecordingSettings:
    """Validated settings for one recording pipeline.

    ``bitrate_kbps`` is retained for WAV so changing formats in the UI does not
    discard the user's compressed-format preference.  WAV itself is always
    interleaved signed 16-bit PCM.  ``gain_db`` is converted to a linear
    GStreamer ``volume`` value by :attr:`linear_gain`.
    """

    format: RecordingFormat = RecordingFormat.OGG_OPUS
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS
    channel_mode: ChannelMode = ChannelMode.STEREO
    gain_db: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.format, RecordingFormat):
            raise TypeError("format must be a RecordingFormat")
        if type(self.bitrate_kbps) is not int:
            raise TypeError("bitrate_kbps must be an integer")
        if self.bitrate_kbps not in BITRATE_OPTIONS_KBPS:
            choices = ", ".join(str(value) for value in BITRATE_OPTIONS_KBPS)
            raise ValueError(f"bitrate_kbps must be one of: {choices}")
        if not isinstance(self.channel_mode, ChannelMode):
            raise TypeError("channel_mode must be a ChannelMode")
        if isinstance(self.gain_db, bool) or not isinstance(self.gain_db, int):
            raise TypeError("gain_db must be an integer number of decibels")
        if not MIN_GAIN_DB <= self.gain_db <= MAX_GAIN_DB:
            raise ValueError(
                f"gain_db must be between {MIN_GAIN_DB} and {MAX_GAIN_DB}"
            )

    @property
    def channels(self) -> int:
        """Return the requested channel count."""

        return self.channel_mode.channels

    @property
    def linear_gain(self) -> float:
        """Return the amplitude multiplier represented by ``gain_db``."""

        return 10.0 ** (self.gain_db / 20.0)

    def with_channels(self, channels: int) -> RecordingSettings:
        """Return a copy configured for an exact mono or stereo layout."""

        return RecordingSettings(
            format=self.format,
            bitrate_kbps=self.bitrate_kbps,
            channel_mode=ChannelMode.from_channels(channels),
            gain_db=self.gain_db,
        )


# A short alias is useful in UI code while retaining the explicit enum member.
DEFAULT_RECORDING_FORMAT = RecordingFormat.OGG_OPUS
