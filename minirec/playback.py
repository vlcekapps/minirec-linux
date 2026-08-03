"""GTK-independent GStreamer playback for MiniRec recordings.

``Player`` prefers ``playbin3`` and falls back to ``playbin``.  GStreamer is
loaded lazily, so importing this module remains safe in storage tools and
offline tests.  Bus messages drive typed asynchronous events; callers only need
a running GLib main context, not a GTK widget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from urllib.parse import urlsplit


SUPPORTED_PLAYBACK_SPEEDS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
"""Fixed rates exposed by the accessible playback-speed control."""

DEFAULT_PLAYBACK_SPEED = 1.0
_NANOSECONDS_PER_SECOND = 1_000_000_000


class PlaybackError(RuntimeError):
    """Base class for an unsuccessful player operation."""


class PlaybackUnavailableError(PlaybackError):
    """Raised when GStreamer or a playbin implementation is unavailable."""


class PlaybackPhase(Enum):
    """Lifecycle state of :class:`Player`."""

    IDLE = auto()
    PREPARING = auto()
    READY = auto()
    PLAYING = auto()
    PAUSED = auto()
    ENDED = auto()
    ERROR = auto()
    CLOSED = auto()


class PlaybackEventType(Enum):
    """Events delivered asynchronously from the GStreamer bus."""

    STATE_CHANGED = auto()
    POSITION_CHANGED = auto()
    SPEED_CHANGED = auto()
    SPEED_ERROR = auto()
    END_OF_STREAM = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    """Immutable player state suitable for GTK presentation code."""

    phase: PlaybackPhase
    uri: str | None
    position_seconds: float
    duration_seconds: float
    speed: float
    pitch_preserved: bool
    backend_name: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    """One player event and the state at the instant it was emitted."""

    type: PlaybackEventType
    snapshot: PlaybackSnapshot
    detail: str | None = None


class Player:
    """Own a GStreamer playbin and provide accessible playback operations.

    ``open`` accepts a local path or an already formed URI and prepares without
    autoplay.  ``seek_to`` and ``seek_by`` use seconds and clamp to the known
    media duration.  ``set_speed`` accepts only
    :data:`SUPPORTED_PLAYBACK_SPEEDS`; when the ``scaletempo`` plugin is
    installed, the requested tempo is applied without changing voice pitch.

    ``on_event`` executes on the GLib thread dispatching the bus watch.  Callback
    exceptions are isolated from the media state machine.
    """

    def __init__(
        self,
        on_event: Callable[[PlaybackEvent], None] | None = None,
        *,
        gst: object | None = None,
    ) -> None:
        self._gst = _load_gst() if gst is None else gst
        self._on_event = on_event or _ignore_playback_event
        self._phase = PlaybackPhase.IDLE
        self._uri: str | None = None
        self._position_seconds = 0.0
        self._duration_seconds = 0.0
        self._speed = DEFAULT_PLAYBACK_SPEED
        self._error: str | None = None

        playbin = self._gst.ElementFactory.make("playbin3", "minirec-player")
        backend_name = "playbin3"
        if playbin is None:
            playbin = self._gst.ElementFactory.make("playbin", "minirec-player")
            backend_name = "playbin"
        if playbin is None:
            raise PlaybackUnavailableError(
                "GStreamer could not create playbin3 or playbin"
            )
        self._playbin = playbin
        self._backend_name = backend_name

        self._scaletempo = self._gst.ElementFactory.make(
            "scaletempo", "minirec-pitch-preserving-tempo"
        )
        if self._scaletempo is not None:
            try:
                self._playbin.set_property("audio-filter", self._scaletempo)
            except (AttributeError, TypeError, ValueError):
                self._scaletempo = None

        bus = self._playbin.get_bus()
        if bus is None:
            self._playbin.set_state(self._gst.State.NULL)
            raise PlaybackUnavailableError("GStreamer playbin has no message bus")
        self._bus = bus
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._on_bus_message)

    @property
    def phase(self) -> PlaybackPhase:
        """Return the player's lifecycle phase."""

        return self._phase

    @property
    def pitch_preserved(self) -> bool:
        """Return whether rate changes use a ``scaletempo`` filter."""

        return self._scaletempo is not None

    @property
    def snapshot(self) -> PlaybackSnapshot:
        """Return an immutable state snapshot without querying GStreamer."""

        return PlaybackSnapshot(
            phase=self._phase,
            uri=self._uri,
            position_seconds=self._position_seconds,
            duration_seconds=self._duration_seconds,
            speed=self._speed,
            pitch_preserved=self.pitch_preserved,
            backend_name=self._backend_name,
            error=self._error,
        )

    @property
    def position_seconds(self) -> float:
        """Query and return the current non-negative playback position."""

        self._position_seconds = self._query_time("position")
        return self._position_seconds

    @property
    def duration_seconds(self) -> float:
        """Query and return the current non-negative media duration."""

        self._duration_seconds = self._query_time("duration")
        return self._duration_seconds

    def set_event_callback(self, callback: Callable[[PlaybackEvent], None]) -> None:
        """Replace the callback used for future player events."""

        self._on_event = callback

    def open(self, media: str | Path) -> bool:
        """Prepare a local file or URI asynchronously without autoplay."""

        self._require_open()
        uri = media_uri(media)
        # A failed second open must describe that attempt, never retain the
        # previous recording's URI, position or rate in its ERROR snapshot.
        self._uri = uri
        self._position_seconds = 0.0
        self._duration_seconds = 0.0
        self._speed = DEFAULT_PLAYBACK_SPEED
        self._error = None
        try:
            self._playbin.set_state(self._gst.State.NULL)
            self._playbin.set_property("uri", uri)
        except Exception as error:
            self._fail(f"could not open recording: {error}")
            return False
        self._set_phase(PlaybackPhase.PREPARING)
        try:
            result = self._playbin.set_state(self._gst.State.PAUSED)
        except Exception as error:
            self._fail(f"could not prepare recording: {error}")
            return False
        if result == self._gst.StateChangeReturn.FAILURE:
            self._fail("GStreamer could not prepare the recording")
            return False
        if result in {
            self._gst.StateChangeReturn.SUCCESS,
            self._gst.StateChangeReturn.NO_PREROLL,
        }:
            self._report_ready()
        return True

    def play(self) -> bool:
        """Start or resume playback; return ``False`` in an invalid state."""

        if self._phase not in {
            PlaybackPhase.READY,
            PlaybackPhase.PAUSED,
            PlaybackPhase.ENDED,
        }:
            return False
        if self._phase is PlaybackPhase.ENDED and not self.seek_to(0.0):
            return False
        if not self._set_gst_state(self._gst.State.PLAYING, "start playback"):
            return False
        self._set_phase(PlaybackPhase.PLAYING)
        return True

    def pause(self) -> bool:
        """Pause active playback without changing position or speed."""

        if self._phase is not PlaybackPhase.PLAYING:
            return False
        if not self._set_gst_state(self._gst.State.PAUSED, "pause playback"):
            return False
        self.refresh()
        self._set_phase(PlaybackPhase.PAUSED)
        return True

    def toggle(self) -> bool:
        """Pause while playing, otherwise play a ready or paused recording."""

        return self.pause() if self._phase is PlaybackPhase.PLAYING else self.play()

    def seek_to(self, position_seconds: float) -> bool:
        """Seek to an absolute position in seconds, clamped to the duration."""

        if self._phase not in {
            PlaybackPhase.READY,
            PlaybackPhase.PLAYING,
            PlaybackPhase.PAUSED,
            PlaybackPhase.ENDED,
        }:
            return False
        try:
            requested = float(position_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("position_seconds must be a finite number") from error
        if requested != requested or requested in {float("inf"), float("-inf")}:
            raise ValueError("position_seconds must be a finite number")
        target = max(0.0, requested)
        if self._duration_seconds > 0.0:
            target = min(target, self._duration_seconds)
        ended = self._phase is PlaybackPhase.ENDED
        # playbin may remain internally PLAYING after posting EOS.  Keeping it
        # paused makes a user-selected post-EOS seek silent until Play.
        if ended and not self._set_gst_state(
            self._gst.State.PAUSED,
            "pause finished playback before seeking",
        ):
            return False
        accepted = self._seek(target, self._speed)
        if not accepted:
            return False
        self._position_seconds = target
        if ended:
            self._set_phase(PlaybackPhase.PAUSED)
        self._emit(PlaybackEventType.POSITION_CHANGED)
        return True

    def seek_by(self, delta_seconds: float) -> bool:
        """Seek relatively by positive or negative seconds."""

        try:
            delta = float(delta_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("delta_seconds must be a finite number") from error
        if delta != delta or delta in {float("inf"), float("-inf")}:
            raise ValueError("delta_seconds must be a finite number")
        return self.seek_to(self.position_seconds + delta)

    def seek_forward(self, seconds: float = 10.0) -> bool:
        """Convenience action for an accessible forward-seek button."""

        return self.seek_by(abs(float(seconds)))

    def seek_back(self, seconds: float = 10.0) -> bool:
        """Convenience action for an accessible backward-seek button."""

        return self.seek_by(-abs(float(seconds)))

    def set_speed(self, speed: float) -> bool:
        """Apply one supported playback rate at the current position."""

        normalized = _supported_speed(speed)
        if normalized is None:
            choices = ", ".join(str(value) for value in SUPPORTED_PLAYBACK_SPEEDS)
            raise ValueError(f"speed must be one of: {choices}")
        if self._phase not in {
            PlaybackPhase.READY,
            PlaybackPhase.PLAYING,
            PlaybackPhase.PAUSED,
            PlaybackPhase.ENDED,
        }:
            return False
        position = self.position_seconds
        previous = self._speed
        if not self._seek(position, normalized, fatal=False):
            self._speed = previous
            return False
        self._speed = normalized
        self._emit(PlaybackEventType.SPEED_CHANGED)
        return True

    def refresh(self) -> PlaybackSnapshot:
        """Refresh position/duration queries and emit a position event."""

        if self._phase not in {PlaybackPhase.CLOSED, PlaybackPhase.IDLE}:
            self._position_seconds = self._query_time("position")
            duration = self._query_time("duration")
            if duration > 0.0:
                self._duration_seconds = duration
            self._emit(PlaybackEventType.POSITION_CHANGED)
        return self.snapshot

    def close(self) -> None:
        """Set playbin to ``NULL`` and disconnect its bus; idempotent."""

        if self._phase is PlaybackPhase.CLOSED:
            return
        try:
            self._playbin.set_state(self._gst.State.NULL)
        finally:
            try:
                self._bus.disconnect(self._bus_handler)
            except (AttributeError, TypeError, ValueError):
                pass
            try:
                self._bus.remove_signal_watch()
            except (AttributeError, TypeError, ValueError):
                pass
            self._set_phase(PlaybackPhase.CLOSED)

    def _report_ready(self) -> None:
        if self._phase is not PlaybackPhase.PREPARING:
            return
        self._duration_seconds = self._query_time("duration")
        self._set_phase(PlaybackPhase.READY)

    def _seek(
        self,
        position_seconds: float,
        speed: float,
        *,
        fatal: bool = True,
    ) -> bool:
        target_ns = round(position_seconds * _NANOSECONDS_PER_SECOND)
        try:
            accepted = self._playbin.seek(
                speed,
                self._gst.Format.TIME,
                self._gst.SeekFlags.FLUSH | self._gst.SeekFlags.ACCURATE,
                self._gst.SeekType.SET,
                target_ns,
                self._gst.SeekType.NONE,
                -1,
            )
        except Exception as error:
            detail = f"could not seek: {error}"
            if fatal:
                self._fail(detail)
            else:
                self._emit(PlaybackEventType.SPEED_ERROR, detail)
            return False
        if not accepted:
            detail = "GStreamer rejected the playback-speed change"
            if fatal:
                self._fail("GStreamer rejected the seek request")
            else:
                self._emit(PlaybackEventType.SPEED_ERROR, detail)
            return False
        return True

    def _query_time(self, which: str) -> float:
        if self._phase is PlaybackPhase.CLOSED:
            return 0.0
        try:
            if which == "position":
                success, value = self._playbin.query_position(self._gst.Format.TIME)
            else:
                success, value = self._playbin.query_duration(self._gst.Format.TIME)
        except Exception:
            return 0.0
        if not success or value is None or value < 0:
            return 0.0
        return float(value) / _NANOSECONDS_PER_SECOND

    def _set_gst_state(self, state: object, operation: str) -> bool:
        try:
            result = self._playbin.set_state(state)
        except Exception as error:
            self._fail(f"could not {operation}: {error}")
            return False
        if result == self._gst.StateChangeReturn.FAILURE:
            self._fail(f"GStreamer could not {operation}")
            return False
        return True

    def _on_bus_message(self, _bus: object, message: object) -> None:
        if self._phase is PlaybackPhase.CLOSED:
            return
        if message.type == self._gst.MessageType.ASYNC_DONE:
            self._report_ready()
        elif message.type == self._gst.MessageType.EOS:
            if self._duration_seconds <= 0.0:
                self._duration_seconds = self._query_time("duration")
            self._position_seconds = self._duration_seconds
            if not self._set_gst_state(
                self._gst.State.PAUSED,
                "pause finished playback",
            ):
                return
            self._set_phase(PlaybackPhase.ENDED)
            self._emit(PlaybackEventType.END_OF_STREAM)
        elif message.type == self._gst.MessageType.ERROR:
            error, debug = message.parse_error()
            detail = str(error).strip() or "GStreamer playback failed"
            if debug:
                detail = f"{detail} ({debug})"
            self._fail(detail)

    def _fail(self, detail: str) -> None:
        self._error = detail
        try:
            self._playbin.set_state(self._gst.State.NULL)
        except Exception:
            pass
        self._set_phase(PlaybackPhase.ERROR)
        self._emit(PlaybackEventType.ERROR, detail)

    def _require_open(self) -> None:
        if self._phase is PlaybackPhase.CLOSED:
            raise PlaybackError("player is closed")

    def _set_phase(self, phase: PlaybackPhase) -> None:
        self._phase = phase
        self._emit(PlaybackEventType.STATE_CHANGED)

    def _emit(self, event_type: PlaybackEventType, detail: str | None = None) -> None:
        try:
            self._on_event(PlaybackEvent(event_type, self.snapshot, detail))
        except Exception:
            # Presentation callbacks must never corrupt playback state.
            pass


def media_uri(media: str | Path) -> str:
    """Return a safely escaped URI for a local path or existing absolute URI."""

    if isinstance(media, Path):
        return media.expanduser().resolve().as_uri()
    raw = str(media).strip()
    if not raw:
        raise ValueError("media must not be empty")
    parsed = urlsplit(raw)
    if parsed.scheme:
        return raw
    return Path(raw).expanduser().resolve().as_uri()


def _supported_speed(value: float) -> float | None:
    try:
        requested = float(value)
    except (TypeError, ValueError):
        return None
    if requested != requested:
        return None
    return next(
        (
            supported
            for supported in SUPPORTED_PLAYBACK_SPEEDS
            if abs(supported - requested) < 0.001
        ),
        None,
    )


def _load_gst() -> object:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as error:
        raise PlaybackUnavailableError(
            "GStreamer 1.0 Python bindings are unavailable"
        ) from error
    Gst.init(None)
    return Gst


def _ignore_playback_event(_event: PlaybackEvent) -> None:
    return


# Descriptive alias for integrations which prefer a domain-specific class name.
RecordingPlayer = Player
