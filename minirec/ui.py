"""Accessible GTK 4/libadwaita presentation layer for MiniRec.

This module owns widgets and presentation state only.  Audio, storage,
settings persistence, URI launching and suspend inhibition are reached through
the :class:`PresentationCallbacks` port implemented by ``application.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
import math
from typing import Protocol

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Pango  # noqa: E402

from .gtk_helpers import (
    CONTENT_WIDTH,
    MAX_RECORDING_SELECTION,
    MIN_CONTROL_HEIGHT,
    PRIMARY_CONTROL_HEIGHT,
    ChildWindow,
    LiveStatus,
    action_group,
    alert,
    append_list_item,
    clear_list,
    confirm,
    content_box,
    description,
    focus_index_after_removal,
    focus_later,
    focus_list_item_later,
    heading,
    index_for_value,
    install_escape_handler,
    labelled,
    navigable_list,
    normalize_selection,
    phase_name,
    record_action_for_state,
    scrolled_content,
    set_accessible_description,
    set_accessible_label,
    set_invalid,
    set_value_text,
    string_dropdown,
    toggle_selection,
    wrapping_button,
    wrapping_check_button,
)
from .i18n import (
    LANGUAGE_CHOICES,
    Translator,
    format_duration,
    format_file_size,
    format_gain_db,
    format_speed,
)
from .models import (
    BITRATE_OPTIONS_KBPS,
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    ChannelMode,
    RecordingFormat,
)
from .playback import SUPPORTED_PLAYBACK_SPEEDS
from .settings import AppLanguage, AppSettings


FORMAT_VALUES: tuple[RecordingFormat, ...] = tuple(RecordingFormat)
FORMAT_LABEL_KEYS: dict[RecordingFormat, str] = {
    RecordingFormat.OGG_OPUS: "format_ogg_opus",
    RecordingFormat.MP3: "format_mp3",
    RecordingFormat.WAV: "format_wav",
}
LANGUAGE_VALUES: tuple[str, ...] = LANGUAGE_CHOICES
ACTIVE_RECORDING_PHASES = frozenset(
    {
        "starting",
        "recording",
        "pausing",
        "paused",
        "resuming",
        "stopping",
        "finalizing",
    }
)
OperationCompletion = Callable[[str | None], None]


def _invoke_async_operation(
    callback: Callable[..., None],
    *values: object,
    completed: OperationCompletion,
) -> None:
    """Invoke the async callback contract, retaining smoke-adapter support."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        callback(*values, completed)
        return
    try:
        signature.bind(*values, completed)
    except TypeError:
        # Early presentation adapters completed mutations synchronously.  The
        # compatibility path is selected before invocation, so a TypeError
        # raised *inside* a modern callback can never retry a mutation.
        callback(*values)
        completed(None)
    else:
        callback(*values, completed)


@dataclass(frozen=True, slots=True)
class RecordingView:
    """Storage-independent metadata used by the recordings window."""

    identifier: str
    name: str
    duration_seconds: float | None
    size_bytes: int | None
    modified_ns: int
    format: RecordingFormat


@dataclass(frozen=True, slots=True)
class RecorderView:
    """Complete recorder state needed by :class:`MainWindow`."""

    phase: object = "idle"
    elapsed_seconds: float = 0.0
    remaining_seconds: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerView:
    """Complete player state needed by :class:`PlayerWindow`."""

    phase: object = "preparing"
    position_seconds: float = 0.0
    duration_seconds: float | None = None
    speed: float = 1.0
    error: str | None = None


class PresentationCallbacks(Protocol):
    """Application services consumed by the presentation layer.

    Implementations may complete asynchronously.  They update the windows with
    ``set_recorder_view``, ``set_recordings`` and ``set_player_view`` when a
    new immutable domain snapshot is available.
    """

    def on_record(self) -> None: ...

    def on_pause(self) -> None: ...

    def on_resume(self) -> None: ...

    def on_stop(self) -> None: ...

    def on_stop_and_quit(self) -> None: ...

    def on_settings_changed(self, settings: AppSettings) -> None: ...

    def on_refresh_recordings(self) -> None: ...

    def on_open_recordings_folder(self) -> None: ...

    def on_thank_author(self) -> None: ...

    def on_open_player(self, identifier: str) -> None: ...

    def on_rename_recording(
        self,
        identifier: str,
        new_name: str,
        completed: OperationCompletion,
    ) -> None: ...

    def on_delete_recordings(
        self,
        identifiers: tuple[str, ...],
        completed: OperationCompletion,
    ) -> None: ...

    def on_player_play(self, identifier: str) -> None: ...

    def on_player_pause(self, identifier: str) -> None: ...

    def on_player_seek(self, identifier: str, position_seconds: float) -> None: ...

    def on_player_seek_by(self, identifier: str, delta_seconds: float) -> None: ...

    def on_player_speed(self, identifier: str, speed: float) -> None: ...

    def on_player_closed(self, identifier: str) -> None: ...


# Short alias for application code written before the final public name settled.
UICallbacks = PresentationCallbacks


def _finite_nonnegative(value: int | float | None) -> float:
    if value is None:
        return 0.0
    numeric = float(value)
    return numeric if math.isfinite(numeric) and numeric > 0.0 else 0.0


def _recording_format_label(
    translator: Translator,
    recording_format: RecordingFormat,
) -> str:
    """Return the same localized format label used by settings."""

    return translator(FORMAT_LABEL_KEYS[recording_format])


def _set_button_text(button: Gtk.Button | Gtk.CheckButton, text: str) -> None:
    button.set_label(text)
    set_accessible_label(button, text)


def _set_form_label(container: Gtk.Box, text: str) -> None:
    label = container.get_first_child()
    if isinstance(label, Gtk.Label):
        label.set_text(text)


class MainWindow(Adw.ApplicationWindow):
    """Adaptive recorder window and owner of MiniRec's child windows."""

    def __init__(
        self,
        application: Adw.Application,
        callbacks: PresentationCallbacks,
        *,
        translator: Translator | None = None,
        settings: AppSettings | None = None,
        version: str = "",
        recordings_path: str = "",
    ) -> None:
        super().__init__(application=application)
        self.callbacks = callbacks
        self.translator = translator or Translator()
        self.settings = settings or AppSettings()
        self.version = version
        self.recordings_path = recordings_path
        self._recorder_view = RecorderView()
        self._recordings: tuple[RecordingView, ...] = ()
        self._recordings_error: str | None = None
        self._settings_window: SettingsWindow | None = None
        self._recordings_window: RecordingsWindow | None = None
        self._player_window: PlayerWindow | None = None
        self._allow_close = False
        self._quit_question_open = False
        self._library_busy = False
        self._player_delete_close = False
        self._suppress_player_focus_return = False

        self.set_title(self.t("app_name"))
        self.set_default_size(CONTENT_WIDTH, 680)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.header_title = Gtk.Label(
            label=self.t("app_name"),
            ellipsize=Pango.EllipsizeMode.END,
        )
        self.header_title.set_tooltip_text(self.t("app_name"))
        header.set_title_widget(self.header_title)
        self.menu_button = Gtk.MenuButton(label=self.t("main_menu"))
        self.menu_button.set_size_request(-1, MIN_CONTROL_HEIGHT)
        set_accessible_label(self.menu_button, self.t("main_menu"))
        header.pack_end(self.menu_button)
        toolbar.add_top_bar(header)

        self._install_actions()
        self._build_menu()

        body = content_box()
        self.main_heading = heading(self.t("main_heading"))
        body.append(self.main_heading)

        self.remaining_value = description("", readable=True)
        self.remaining_field = labelled(
            self.t("remaining_label"), self.remaining_value
        )
        body.append(self.remaining_field)

        self.elapsed_value = description("", readable=True)
        self.elapsed_field = labelled(self.t("elapsed_label"), self.elapsed_value)
        body.append(self.elapsed_field)

        self.status = LiveStatus()
        self.status_field = labelled(self.t("status_label"), self.status)
        body.append(self.status_field)

        self.primary_button = wrapping_button(self.t("action_record"))
        self.primary_button.set_size_request(-1, PRIMARY_CONTROL_HEIGHT)
        self.primary_button.add_css_class("suggested-action")
        self.primary_button.connect("clicked", self._primary_clicked)
        self.stop_button = wrapping_button(self.t("action_stop"))
        self.stop_button.set_size_request(-1, PRIMARY_CONTROL_HEIGHT)
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.connect("clicked", lambda _button: self._invoke(self.callbacks.on_stop))
        body.append(action_group(self.primary_button, self.stop_button))

        scroll = scrolled_content(body)
        scroll.set_vexpand(True)
        toolbar.set_content(scroll)
        self.set_content(toolbar)
        self.connect("close-request", self._close_requested)
        self.set_recorder_view(self._recorder_view, announce=False)

    def t(self, key: str, **values: object) -> str:
        return self.translator(key, **values)

    def _install_actions(self) -> None:
        actions: tuple[tuple[str, Callable[[], None]], ...] = (
            ("settings", self.show_settings),
            ("recordings", self.show_recordings),
            ("open-recordings-folder", self.callbacks.on_open_recordings_folder),
            ("thank-author", self.callbacks.on_thank_author),
        )
        self.window_actions: dict[str, Gio.SimpleAction] = {}
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect(
                "activate",
                lambda _action, _parameter, selected=callback: self._invoke(selected),
            )
            self.add_action(action)
            self.window_actions[name] = action

    def _build_menu(self) -> None:
        menu = Gio.Menu()
        menu.append(self.t("menu_settings"), "win.settings")
        menu.append(self.t("menu_recordings"), "win.recordings")
        menu.append(self.t("menu_open_folder"), "win.open-recordings-folder")
        menu.append(self.t("menu_thank_author"), "win.thank-author")
        self.main_menu_model = menu
        self.menu_button.set_menu_model(menu)

    def _invoke(self, callback: Callable[..., None], *values: object) -> bool:
        try:
            callback(*values)
        except Exception as error:
            alert(self, self.t("error"), str(error))
            return False
        return True

    def _primary_clicked(self, _button: Gtk.Button) -> None:
        action = record_action_for_state(phase_name(self._recorder_view.phase))
        callback = {
            "record": self.callbacks.on_record,
            "pause": self.callbacks.on_pause,
            "resume": self.callbacks.on_resume,
        }.get(action)
        if callback is not None:
            self._invoke(callback)

    def set_recorder_view(
        self,
        view: RecorderView,
        *,
        announce: bool = True,
    ) -> None:
        """Render a recorder snapshot without touching the audio backend."""

        self._recorder_view = view
        phase = phase_name(view.phase)
        action = record_action_for_state(phase)
        action_key = {
            "record": "action_record",
            "pause": "action_pause",
            "resume": "action_resume",
        }.get(action, "action_record")
        _set_button_text(self.primary_button, self.t(action_key))
        self.primary_button.set_sensitive(action != "none")
        self.stop_button.set_sensitive(
            phase in {"starting", "recording", "pausing", "paused", "resuming"}
        )
        recording_active = phase in ACTIVE_RECORDING_PHASES
        self.window_actions["settings"].set_enabled(not recording_active)
        self.window_actions["recordings"].set_enabled(not recording_active)
        if recording_active:
            # A settings or browser window opened immediately before capture
            # must not remain an alternate route to mutate recording policy.
            if self._settings_window is not None:
                self._settings_window.close()
            if self._recordings_window is not None:
                self._recordings_window.close()

        elapsed = format_duration(view.elapsed_seconds)
        remaining = self.translator.format_remaining(view.remaining_seconds)
        self.elapsed_value.set_text(elapsed)
        self.remaining_value.set_text(remaining)
        set_accessible_label(
            self.elapsed_value, f"{self.t('elapsed_label')}: {elapsed}"
        )
        set_accessible_label(
            self.remaining_value, f"{self.t('remaining_label')}: {remaining}"
        )

        if view.error:
            status_text = self.t("status_error_detail", message=view.error)
        else:
            status_key = {
                "idle": "status_idle",
                "ready": "status_idle",
                "starting": "status_starting",
                "recording": "status_recording",
                "pausing": "status_pausing",
                "paused": "status_paused",
                "resuming": "status_resuming",
                "stopping": "status_stopping",
                "finalizing": "status_finalizing",
                "recovering": "status_startup_recovery",
                "stopped": "status_stopped",
                "error": "status_error",
            }.get(phase, "status_idle")
            status_text = self.t(status_key)
        set_accessible_label(
            self.status, f"{self.t('status_label')}: {status_text}"
        )
        self.status.set_status(status_text, announce=announce)

    # A convenient flat spelling for adapters receiving backend snapshots.
    def update_recorder(
        self,
        phase: object,
        elapsed_seconds: float,
        remaining_seconds: float | None,
        error: str | None = None,
    ) -> None:
        self.set_recorder_view(
            RecorderView(phase, elapsed_seconds, remaining_seconds, error)
        )

    def apply_settings(
        self,
        settings: AppSettings,
        *,
        translator: Translator | None = None,
    ) -> None:
        """Reflect an application-confirmed settings snapshot."""

        self.settings = settings
        if translator is not None:
            self.translator = translator
            self._retranslate_main()
            if self._settings_window is not None:
                self._settings_window.retranslate()
            if self._recordings_window is not None:
                self._recordings_window.retranslate()
            if self._player_window is not None:
                self._player_window.retranslate()
        if self._settings_window is not None:
            self._settings_window.set_settings(settings)

    def _retranslate_main(self) -> None:
        self.set_title(self.t("app_name"))
        self.header_title.set_text(self.t("app_name"))
        self.header_title.set_tooltip_text(self.t("app_name"))
        self.main_heading.set_text(self.t("main_heading"))
        _set_button_text(self.menu_button, self.t("main_menu"))
        _set_button_text(self.stop_button, self.t("action_stop"))
        _set_form_label(self.remaining_field, self.t("remaining_label"))
        _set_form_label(self.elapsed_field, self.t("elapsed_label"))
        _set_form_label(self.status_field, self.t("status_label"))
        self._build_menu()
        self.set_recorder_view(self._recorder_view, announce=False)

    def show_settings(self) -> None:
        if phase_name(self._recorder_view.phase) in ACTIVE_RECORDING_PHASES:
            return
        if self._settings_window is None:
            child = SettingsWindow(
                self,
                self.callbacks,
                self.settings,
                version=self.version,
                recordings_path=self.recordings_path,
            )
            child.connect("close-request", self._settings_closed)
            self._settings_window = child
        self._settings_window.present()
        focus_later(self._settings_window.language)

    def _settings_closed(self, window: SettingsWindow) -> bool:
        if self._settings_window is window:
            self._settings_window = None
        return False

    def show_recordings(self) -> None:
        if phase_name(self._recorder_view.phase) in ACTIVE_RECORDING_PHASES:
            return
        if self._recordings_window is None:
            child = RecordingsWindow(self, self.callbacks)
            child.connect("close-request", self._recordings_closed)
            self._recordings_window = child
            child.set_recordings(self._recordings, error=self._recordings_error)
            child.set_busy(self._library_busy)
        self._recordings_window.present()
        self._recordings_window.refresh()

    def _recordings_closed(self, window: RecordingsWindow) -> bool:
        if self._recordings_window is window:
            self._recordings_window = None
        return False

    def set_recordings(
        self,
        recordings: Sequence[RecordingView],
        *,
        error: str | None = None,
    ) -> None:
        self._recordings = tuple(recordings)
        self._recordings_error = error
        if self._recordings_window is not None:
            self._recordings_window.set_recordings(self._recordings, error=error)

    def show_player(self, recording: RecordingView) -> None:
        if (
            self._player_window is not None
            and self._player_window.recording.identifier == recording.identifier
        ):
            self._player_window.present()
            return
        if self._player_window is not None:
            self._suppress_player_focus_return = True
            self._player_window.close()
        child = PlayerWindow(self, self.callbacks, recording)
        child.connect("close-request", self._player_closed)
        self._player_window = child
        child.set_library_busy(self._library_busy)
        child.present()
        focus_later(child.play_button)
        self._invoke(self.callbacks.on_open_player, recording.identifier)

    # Alias used naturally by application adapters.
    open_player = show_player

    def _player_closed(self, window: PlayerWindow) -> bool:
        if self._player_window is window:
            self._player_window = None
            if self._suppress_player_focus_return:
                self._suppress_player_focus_return = False
            elif self._player_delete_close:
                self._player_delete_close = False
            elif self._recordings_window is not None:
                self._recordings_window.focus_recording(
                    window.recording.identifier
                )
        return False

    def set_player_view(self, view: PlayerView) -> None:
        if self._player_window is not None:
            self._player_window.set_player_view(view)

    def close_player(self) -> None:
        """Close the current player and let its close callback release audio."""

        if self._player_window is not None:
            self._player_window.close()

    def close_player_after_delete(self, identifier: str | None) -> None:
        if self._recordings_window is not None and identifier is not None:
            self._recordings_window.prepare_focus_after_player_delete(identifier)
        self._player_delete_close = True
        if self._player_window is not None:
            self._player_window.close()
        else:
            self._player_delete_close = False

    def update_recording(
        self,
        old_identifier: str,
        recording: RecordingView,
        *,
        focus_recordings: bool = True,
    ) -> None:
        self._recordings = tuple(
            recording if item.identifier == old_identifier else item
            for item in self._recordings
        )
        if self._recordings_window is not None:
            self._recordings_window.update_recording(
                old_identifier,
                recording,
                focus=focus_recordings,
            )
        if (
            self._player_window is not None
            and self._player_window.recording.identifier == old_identifier
        ):
            self._player_window.update_recording(recording)

    def announce_library_status(self, message: str) -> None:
        """Announce one library result in the user's active UI context."""

        if self._player_window is not None:
            self._player_window.announce_library_status(message)
        elif self._recordings_window is not None:
            self._recordings_window.message.set_status(message, announce=True)
        else:
            self.status.set_status(message, announce=True)

    def set_library_busy(self, busy: bool) -> None:
        """Disable conflicting library actions until an async mutation ends."""

        self._library_busy = bool(busy)
        if self._recordings_window is not None:
            self._recordings_window.set_busy(self._library_busy)
        if self._player_window is not None:
            self._player_window.set_library_busy(self._library_busy)

    def player_error(self, message: str) -> None:
        if self._player_window is not None:
            self._player_window.set_player_view(
                PlayerView(phase="error", error=message)
            )

    def _close_requested(self, _window: Gtk.Window) -> bool:
        if self._allow_close:
            return False
        if phase_name(self._recorder_view.phase) not in ACTIVE_RECORDING_PHASES:
            return False
        if self._quit_question_open:
            return True
        self._quit_question_open = True

        def stop_and_quit() -> None:
            self._quit_question_open = False
            self.primary_button.set_sensitive(False)
            self.stop_button.set_sensitive(False)
            if not self._invoke(self.callbacks.on_stop_and_quit):
                self.set_recorder_view(self._recorder_view, announce=False)

        dialog = Gtk.AlertDialog(
            message=self.t("quit_recording_title"),
            detail=self.t("quit_recording_body"),
            modal=True,
        )
        dialog.set_buttons([self.t("keep_recording"), self.t("stop_and_quit")])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(source: Gtk.AlertDialog, result: Gio.AsyncResult) -> None:
            self._quit_question_open = False
            try:
                response = source.choose_finish(result)
            except Exception:
                focus_later(self.primary_button)
                return
            if response == 1:
                stop_and_quit()
            else:
                focus_later(self.primary_button)

        dialog.choose(self, None, answered)
        return True

    def complete_stop_and_quit(self) -> None:
        """Close only after the application has finalized the recording."""

        self._allow_close = True
        self.close()


class SettingsWindow(ChildWindow):
    """Native, keyboard-complete recording settings editor."""

    def __init__(
        self,
        parent: MainWindow,
        callbacks: PresentationCallbacks,
        settings: AppSettings,
        *,
        version: str,
        recordings_path: str,
    ) -> None:
        super().__init__(
            parent,
            parent.t("settings_title"),
            parent.t("close"),
            width=CONTENT_WIDTH,
        )
        self.parent_window = parent
        self.callbacks = callbacks
        self.settings = settings
        self._updating = True
        self._version = version
        self._recordings_path = recordings_path

        self.settings_heading = heading(parent.t("settings_heading"))
        self.page.append(self.settings_heading)
        self.recording_heading = heading(
            parent.t("recording_settings_heading"), level=2
        )
        self.page.append(self.recording_heading)

        language_index = index_for_value(
            LANGUAGE_VALUES, settings.language.value
        )
        self.language = string_dropdown(
            parent.t("language_label"),
            [
                parent.t("language_system"),
                parent.t("language_en"),
                parent.t("language_cs"),
            ],
            selected=language_index,
        )
        self.language.connect("notify::selected", self._language_changed)
        self.language_field = labelled(parent.t("language_label"), self.language)
        self.page.append(self.language_field)

        format_index = FORMAT_VALUES.index(settings.recording.format)
        self.format = string_dropdown(
            parent.t("format_label"),
            [
                _recording_format_label(parent.translator, item)
                for item in FORMAT_VALUES
            ],
            selected=format_index,
        )
        self.format.connect("notify::selected", self._format_changed)
        self.format_field = labelled(parent.t("format_label"), self.format)
        self.page.append(self.format_field)

        bitrate_index = index_for_value(
            BITRATE_OPTIONS_KBPS, settings.recording.bitrate_kbps
        )
        self.bitrate = string_dropdown(
            parent.t("bitrate_label"),
            [parent.t("bitrate_value", bitrate=value) for value in BITRATE_OPTIONS_KBPS],
            selected=bitrate_index,
        )
        self.bitrate.connect("notify::selected", self._bitrate_changed)
        self.bitrate_field = labelled(parent.t("bitrate_label"), self.bitrate)
        self.page.append(self.bitrate_field)
        self.bitrate_note = description(
            parent.t("bitrate_unavailable_wav"), readable=True
        )
        self.page.append(self.bitrate_note)

        self.channels_frame = Gtk.Frame()
        self.channels_label = Gtk.Label(
            label=parent.t("channels_heading"), xalign=0, wrap=True
        )
        self.channels_frame.set_label_widget(self.channels_label)
        channels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mono = wrapping_check_button(parent.t("channel_mono"))
        self.stereo = wrapping_check_button(parent.t("channel_stereo"))
        self.stereo.set_group(self.mono)
        self.mono.set_active(settings.recording.channel_mode is ChannelMode.MONO)
        self.stereo.set_active(settings.recording.channel_mode is ChannelMode.STEREO)
        self.mono.connect("toggled", self._channel_changed, ChannelMode.MONO)
        self.stereo.connect("toggled", self._channel_changed, ChannelMode.STEREO)
        channels_box.append(self.mono)
        channels_box.append(self.stereo)
        self.channels_frame.set_child(channels_box)
        self.page.append(self.channels_frame)

        self.gain = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, MIN_GAIN_DB, MAX_GAIN_DB, 1
        )
        self.gain.set_hexpand(True)
        self.gain.set_digits(0)
        self.gain.set_draw_value(True)
        self.gain.set_value(settings.recording.gain_db)
        self.gain.set_size_request(-1, MIN_CONTROL_HEIGHT)
        set_accessible_label(self.gain, parent.t("gain_label"))
        set_accessible_description(self.gain, parent.t("gain_range"))
        set_value_text(self.gain, format_gain_db(settings.recording.gain_db))
        self.gain.set_format_value_func(
            lambda _scale, value: format_gain_db(value)
        )
        self.gain.connect("value-changed", self._gain_changed)
        self.gain_field = labelled(parent.t("gain_label"), self.gain)
        self.page.append(self.gain_field)

        self.prevent_sleep = wrapping_check_button(
            parent.t("prevent_sleep"), active=settings.prevent_sleep
        )
        self.prevent_sleep.connect("toggled", self._prevent_sleep_changed)
        self.page.append(self.prevent_sleep)

        self.details_heading = heading(
            parent.t("application_details_heading"), level=2
        )
        self.page.append(self.details_heading)
        version_value = version or parent.t("not_available")
        path_value = recordings_path or parent.t("not_available")
        self.version_value = description(version_value, readable=True)
        self.version_field = labelled(parent.t("version_label"), self.version_value)
        set_accessible_label(
            self.version_value,
            f"{parent.t('version_label')}: {version_value}",
        )
        self.page.append(self.version_field)
        self.path_value = description(path_value, readable=True)
        self.path_field = labelled(
            parent.t("recordings_location_label"), self.path_value
        )
        set_accessible_label(
            self.path_value,
            f"{parent.t('recordings_location_label')}: {path_value}",
        )
        self.page.append(self.path_field)

        self._updating = False
        self._sync_format_policy()

    def retranslate(self) -> None:
        """Update every visible/static string after a language change."""

        parent = self.parent_window
        self.set_title(parent.t("settings_title"))
        self.title_widget.set_text(parent.t("settings_title"))
        self.title_widget.set_tooltip_text(parent.t("settings_title"))
        _set_button_text(self.close_button, parent.t("close"))
        self.settings_heading.set_text(parent.t("settings_heading"))
        self.recording_heading.set_text(parent.t("recording_settings_heading"))
        self.details_heading.set_text(parent.t("application_details_heading"))
        self._updating = True
        try:
            self.language.set_model(
                Gtk.StringList.new(
                    [
                        parent.t("language_system"),
                        parent.t("language_en"),
                        parent.t("language_cs"),
                    ]
                )
            )
            self.language.set_selected(
                index_for_value(LANGUAGE_VALUES, self.settings.language.value)
            )
            self.format.set_model(
                Gtk.StringList.new(
                    [
                        _recording_format_label(parent.translator, item)
                        for item in FORMAT_VALUES
                    ]
                )
            )
            self.format.set_selected(
                FORMAT_VALUES.index(self.settings.recording.format)
            )
            self.bitrate.set_model(
                Gtk.StringList.new(
                    [
                        parent.t("bitrate_value", bitrate=value)
                        for value in BITRATE_OPTIONS_KBPS
                    ]
                )
            )
            self.bitrate.set_selected(
                index_for_value(
                    BITRATE_OPTIONS_KBPS,
                    self.settings.recording.bitrate_kbps,
                )
            )
        finally:
            self._updating = False
        _set_form_label(self.language_field, parent.t("language_label"))
        _set_form_label(self.format_field, parent.t("format_label"))
        _set_form_label(self.bitrate_field, parent.t("bitrate_label"))
        _set_form_label(self.gain_field, parent.t("gain_label"))
        _set_form_label(self.version_field, parent.t("version_label"))
        _set_form_label(
            self.path_field, parent.t("recordings_location_label")
        )
        set_accessible_label(self.language, parent.t("language_label"))
        set_accessible_label(self.format, parent.t("format_label"))
        set_accessible_label(self.bitrate, parent.t("bitrate_label"))
        set_accessible_label(self.gain, parent.t("gain_label"))
        set_accessible_description(self.gain, parent.t("gain_range"))
        self.channels_label.set_text(parent.t("channels_heading"))
        _set_button_text(self.mono, parent.t("channel_mono"))
        _set_button_text(self.stereo, parent.t("channel_stereo"))
        _set_button_text(self.prevent_sleep, parent.t("prevent_sleep"))
        self.bitrate_note.set_text(parent.t("bitrate_unavailable_wav"))
        if not self._version:
            self.version_value.set_text(parent.t("not_available"))
        if not self._recordings_path:
            self.path_value.set_text(parent.t("not_available"))
        version_value = self._version or parent.t("not_available")
        path_value = self._recordings_path or parent.t("not_available")
        set_accessible_label(
            self.version_value,
            f"{parent.t('version_label')}: {version_value}",
        )
        set_accessible_label(
            self.path_value,
            f"{parent.t('recordings_location_label')}: {path_value}",
        )
        self._sync_format_policy()

    def _submit(self, updated: AppSettings) -> bool:
        if updated == self.settings:
            return True
        try:
            self.callbacks.on_settings_changed(updated)
        except Exception as error:
            alert(self, self.parent_window.t("error"), str(error))

            def restore() -> bool:
                self.set_settings(self.settings)
                return GLib.SOURCE_REMOVE

            # A failed save can also originate inside GtkDropDown's live item
            # activation.  Restore the prior selection only after GTK has
            # finished that signal, for the same reason successful controller
            # echoes are dispatched on the next main-context turn.
            GLib.idle_add(restore)
            return False
        self.settings = updated
        self.parent_window.settings = updated
        return True

    def _language_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        if self._updating:
            return
        index = dropdown.get_selected()
        if index >= len(LANGUAGE_VALUES):
            return
        self._submit(self.settings.with_changes(language=LANGUAGE_VALUES[index]))

    def _format_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        if self._updating:
            return
        index = dropdown.get_selected()
        if index >= len(FORMAT_VALUES):
            return
        chosen = FORMAT_VALUES[index]
        if self._submit(
            self.settings.with_changes(recording_format=chosen.storage_value)
        ):
            self._sync_format_policy()

    def _sync_format_policy(self) -> None:
        compressed = self.settings.recording.format.is_compressed
        self.bitrate.set_sensitive(compressed)
        self.bitrate_note.set_visible(not compressed)
        set_accessible_description(
            self.bitrate,
            self.parent_window.t(
                "bitrate_compressed_help"
                if compressed
                else "bitrate_unavailable_wav"
            ),
        )

    def _bitrate_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        if self._updating:
            return
        index = dropdown.get_selected()
        if index < len(BITRATE_OPTIONS_KBPS):
            self._submit(
                self.settings.with_changes(
                    bitrate_kbps=BITRATE_OPTIONS_KBPS[index]
                )
            )

    def _channel_changed(
        self, button: Gtk.CheckButton, mode: ChannelMode
    ) -> None:
        if self._updating or not button.get_active():
            return
        self._submit(
            self.settings.with_changes(channel_mode=mode.name.casefold())
        )

    def _gain_changed(self, scale: Gtk.Scale) -> None:
        value = int(round(scale.get_value()))
        set_value_text(scale, format_gain_db(value))
        if not self._updating:
            self._submit(self.settings.with_changes(gain_db=value))

    def _prevent_sleep_changed(self, button: Gtk.CheckButton) -> None:
        if not self._updating:
            self._submit(
                self.settings.with_changes(prevent_sleep=button.get_active())
            )

    def set_settings(self, settings: AppSettings) -> None:
        """Reflect a persisted snapshot, including an application's rollback."""

        self._updating = True
        try:
            self.settings = settings
            self.language.set_selected(
                index_for_value(LANGUAGE_VALUES, settings.language.value)
            )
            self.format.set_selected(FORMAT_VALUES.index(settings.recording.format))
            self.bitrate.set_selected(
                index_for_value(BITRATE_OPTIONS_KBPS, settings.recording.bitrate_kbps)
            )
            self.mono.set_active(settings.recording.channel_mode is ChannelMode.MONO)
            self.stereo.set_active(settings.recording.channel_mode is ChannelMode.STEREO)
            self.gain.set_value(settings.recording.gain_db)
            self.prevent_sleep.set_active(settings.prevent_sleep)
        finally:
            self._updating = False
        self._sync_format_policy()


class RecordingsWindow(ChildWindow):
    """Keyboard and Orca accessible recording browser with multi-selection."""

    def __init__(
        self,
        parent: MainWindow,
        callbacks: PresentationCallbacks,
    ) -> None:
        super().__init__(
            parent,
            parent.t("recordings_title"),
            parent.t("close"),
            width=CONTENT_WIDTH,
        )
        self.parent_window = parent
        self.callbacks = callbacks
        self.recordings: tuple[RecordingView, ...] = ()
        self.current_error: str | None = None
        self.selected: tuple[str, ...] = ()
        self._selection_buttons: dict[str, Gtk.CheckButton] = {}
        self._pending_focus_index: int | None = None
        self._updating_selection = False
        self._busy = False

        self.recordings_heading = heading(parent.t("recordings_heading"))
        self.page.append(self.recordings_heading)
        self.refresh_button = wrapping_button(parent.t("refresh"))
        self.refresh_button.connect("clicked", lambda _button: self.refresh())
        self.open_folder_button = wrapping_button(parent.t("open_folder"))
        self.open_folder_button.connect(
            "clicked", lambda _button: self._invoke(callbacks.on_open_recordings_folder)
        )
        self.page.append(action_group(self.refresh_button, self.open_folder_button))

        self.selection_status = LiveStatus(parent.translator.format_recording_count(0))
        self.page.append(self.selection_status)
        self.clear_button = wrapping_button(parent.t("clear_selection"))
        self.clear_button.connect("clicked", lambda _button: self.clear_selection())
        self.delete_selected_button = wrapping_button(parent.t("delete_selected"))
        self.delete_selected_button.add_css_class("destructive-action")
        self.delete_selected_button.connect(
            "clicked", self._delete_selected_requested
        )
        self.page.append(action_group(self.clear_button, self.delete_selected_button))

        self.message = LiveStatus()
        self.page.append(self.message)
        self.list = navigable_list(parent.t("recordings_list_label"))
        self.page.append(self.list)
        self._sync_selection_controls(announce=False)

    def _invoke(self, callback: Callable[..., None], *values: object) -> bool:
        try:
            callback(*values)
        except Exception as error:
            self.message.set_status(
                self.parent_window.t("recordings_error", message=str(error))
            )
            return False
        return True

    def refresh(self) -> None:
        if self._busy:
            return
        self.message.set_status(self.parent_window.t("recordings_loading"))
        self.refresh_button.set_sensitive(False)
        if not self._invoke(self.callbacks.on_refresh_recordings):
            self.refresh_button.set_sensitive(True)

    def set_recordings(
        self,
        recordings: Sequence[RecordingView],
        *,
        error: str | None = None,
    ) -> None:
        self.recordings = tuple(recordings)
        self.current_error = error
        self.selected = normalize_selection(
            self.selected,
            available=(item.identifier for item in self.recordings),
        )
        self.refresh_button.set_sensitive(not self._busy)
        clear_list(self.list)
        self._selection_buttons.clear()
        if error:
            self.message.set_status(
                self.parent_window.t("recordings_error", message=error)
            )
            self.list.set_visible(False)
        elif not self.recordings:
            self.message.set_status(self.parent_window.t("recordings_empty"))
            self.list.set_visible(False)
        else:
            self.message.set_status("", announce=False)
            self.list.set_visible(True)
            for index, recording in enumerate(self.recordings):
                self._append_recording(recording, index)
        self._sync_selection_controls(announce=False)
        if self._pending_focus_index is not None:
            target = focus_index_after_removal(
                self._pending_focus_index, len(self.recordings)
            )
            self._pending_focus_index = None
            if target is not None:
                focus_list_item_later(self.list, target)
            else:
                focus_later(self.refresh_button)

    def update_recording(
        self,
        old_identifier: str,
        recording: RecordingView,
        *,
        focus: bool,
    ) -> None:
        try:
            index = next(
                position
                for position, item in enumerate(self.recordings)
                if item.identifier == old_identifier
            )
        except StopIteration:
            return
        updated = list(self.recordings)
        updated[index] = recording
        self.selected = tuple(
            recording.identifier if value == old_identifier else value
            for value in self.selected
        )
        if focus:
            self._pending_focus_index = index
        self.set_recordings(updated, error=self.current_error)

    def prepare_focus_after_player_delete(self, identifier: str) -> None:
        try:
            self._pending_focus_index = next(
                position
                for position, item in enumerate(self.recordings)
                if item.identifier == identifier
            )
        except StopIteration:
            self._pending_focus_index = 0

    def focus_recording(self, identifier: str) -> None:
        if not self.recordings:
            focus_later(self.refresh_button)
            return
        try:
            index = next(
                position
                for position, item in enumerate(self.recordings)
                if item.identifier == identifier
            )
        except StopIteration:
            index = 0
        focus_list_item_later(self.list, index)

    def retranslate(self) -> None:
        parent = self.parent_window
        self.set_title(parent.t("recordings_title"))
        self.title_widget.set_text(parent.t("recordings_title"))
        self.title_widget.set_tooltip_text(parent.t("recordings_title"))
        _set_button_text(self.close_button, parent.t("close"))
        self.recordings_heading.set_text(parent.t("recordings_heading"))
        _set_button_text(self.refresh_button, parent.t("refresh"))
        _set_button_text(self.open_folder_button, parent.t("open_folder"))
        _set_button_text(self.clear_button, parent.t("clear_selection"))
        _set_button_text(
            self.delete_selected_button, parent.t("delete_selected")
        )
        set_accessible_label(self.list, parent.t("recordings_list_label"))
        self.set_recordings(self.recordings, error=self.current_error)

    def _append_recording(self, recording: RecordingView, index: int) -> None:
        name = recording.name.strip() or self.parent_window.t("recording_untitled")
        details = self.parent_window.t(
            "recording_details",
            date=self.parent_window.translator.format_recording_date(
                recording.modified_ns
            ),
            duration=format_duration(recording.duration_seconds),
            size=format_file_size(recording.size_bytes),
            format=_recording_format_label(
                self.parent_window.translator,
                recording.format,
            ),
        )
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row.set_focusable(False)
        row.set_margin_top(10)
        row.set_margin_bottom(10)
        row.set_margin_start(10)
        row.set_margin_end(10)
        name_label = Gtk.Label(label=name, xalign=0, wrap=True)
        name_label.add_css_class("heading")
        detail_label = description(details)
        row.append(name_label)
        row.append(detail_label)

        rename_button = wrapping_button(self.parent_window.t("rename"))
        rename_button.connect(
            "clicked",
            lambda button, item=recording: self._rename_requested(item, button),
        )
        delete_button = wrapping_button(self.parent_window.t("delete"))
        delete_button.add_css_class("destructive-action")
        delete_button.connect(
            "clicked",
            lambda button, item=recording, position=index: self._delete_requested(
                item, position, button
            ),
        )
        selected = recording.identifier in self.selected
        select_button = wrapping_check_button(
            self.parent_window.t("deselect" if selected else "select"),
            active=selected,
        )
        select_button.connect(
            "toggled",
            lambda button, identifier=recording.identifier: self._selection_toggled(
                button, identifier
            ),
        )
        self._selection_buttons[recording.identifier] = select_button
        row.append(action_group(rename_button, delete_button, select_button))
        accessible = self.parent_window.t(
            "recording_row", name=name, details=details
        )
        append_list_item(
            self.list,
            row,
            label=self.parent_window.t("recording_open", name=name),
            description_text=accessible,
            callback=lambda item=recording: self.parent_window.show_player(item),
        )

    def _selection_toggled(
        self, button: Gtk.CheckButton, identifier: str
    ) -> None:
        if self._updating_selection:
            return
        updated, accepted = toggle_selection(
            self.selected, identifier, limit=MAX_RECORDING_SELECTION
        )
        if not accepted:
            self._updating_selection = True
            try:
                button.set_active(False)
            finally:
                self._updating_selection = False
            self.message.set_status(
                self.parent_window.t("selection_limit_reached")
            )
            return
        self.selected = updated
        _set_button_text(
            button,
            self.parent_window.t(
                "deselect" if identifier in self.selected else "select"
            ),
        )
        self._sync_selection_controls()

    def _sync_selection_controls(self, *, announce: bool = True) -> None:
        count = len(self.selected)
        self.selection_status.set_status(
            self.parent_window.translator.format_recording_count(count),
            announce=announce,
        )
        self.clear_button.set_sensitive(count > 0 and not self._busy)
        self.delete_selected_button.set_sensitive(count > 0 and not self._busy)

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.refresh_button.set_sensitive(not self._busy)
        self.open_folder_button.set_sensitive(not self._busy)
        self.list.set_sensitive(not self._busy)
        self._sync_selection_controls(announce=False)
        if self._busy:
            self.message.set_status(
                self.parent_window.t("library_operation_in_progress")
            )

    def clear_selection(self) -> None:
        self.selected = ()
        self._updating_selection = True
        try:
            for button in self._selection_buttons.values():
                button.set_active(False)
                _set_button_text(button, self.parent_window.t("select"))
        finally:
            self._updating_selection = False
        self._sync_selection_controls()

    def _rename_requested(
        self, recording: RecordingView, origin: Gtk.Widget
    ) -> None:
        try:
            index = self.recordings.index(recording)
        except ValueError:
            index = 0

        def submit(name: str, completed: OperationCompletion) -> None:
            self._pending_focus_index = index

            def operation_completed(error: str | None) -> None:
                if error is not None:
                    self._pending_focus_index = None
                    self.message.set_status(error)
                completed(error)

            try:
                _invoke_async_operation(
                    self.callbacks.on_rename_recording,
                    recording.identifier,
                    name,
                    completed=operation_completed,
                )
            except Exception:
                self._pending_focus_index = None
                raise

        RenameWindow(
            self,
            self.parent_window.translator,
            recording,
            origin,
            submit,
        ).present()

    def _delete_requested(
        self,
        recording: RecordingView,
        index: int,
        origin: Gtk.Widget,
    ) -> None:
        name = recording.name.strip() or self.parent_window.t("recording_untitled")
        confirm(
            self,
            self.parent_window.t("delete_title"),
            self.parent_window.t("delete_body", name=name),
            self.parent_window.t("cancel"),
            self.parent_window.t("delete_confirm"),
            lambda: self._delete_with_focus((recording.identifier,), index),
            return_to=origin,
            destructive=True,
        )

    def _delete_selected_requested(self, button: Gtk.Button) -> None:
        identifiers = tuple(self.selected)
        if not identifiers:
            return
        selected_set = set(identifiers)
        selected_positions = tuple(
            index
            for index, recording in enumerate(self.recordings)
            if recording.identifier in selected_set
        )
        focus_index = min(selected_positions, default=0)
        confirm(
            self,
            self.parent_window.t("delete_selected_title"),
            self.parent_window.t("delete_selected_body", count=len(identifiers)),
            self.parent_window.t("cancel"),
            self.parent_window.t("delete_confirm"),
            lambda: self._delete_with_focus(identifiers, focus_index),
            return_to=button,
            destructive=True,
        )

    def _delete_with_focus(
        self, identifiers: tuple[str, ...], index: int
    ) -> None:
        self._pending_focus_index = index

        def completed(error: str | None) -> None:
            if error is not None:
                self._pending_focus_index = None
                self.message.set_status(error)

        try:
            _invoke_async_operation(
                self.callbacks.on_delete_recordings,
                identifiers,
                completed=completed,
            )
        except Exception as error:
            self._pending_focus_index = None
            self.message.set_status(
                self.parent_window.t("error_delete", message=str(error))
            )


class RenameWindow(Adw.ApplicationWindow):
    """Small modal rename form with validation, Escape and focus return."""

    def __init__(
        self,
        parent: Gtk.Window,
        translator: Translator,
        recording: RecordingView,
        return_to: Gtk.Widget,
        submit: Callable[[str, OperationCompletion], None],
    ) -> None:
        application = parent.get_application()
        super().__init__(
            application=application,
            transient_for=parent,
            modal=True,
        )
        self.translator = translator
        self.return_to = return_to
        self.submit_callback = submit
        self._focus_returned = False
        self._submitting = False
        self.set_title(translator("rename_title"))
        self.set_default_size(480, 360)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Label(
            label=translator("rename_title"),
            ellipsize=Pango.EllipsizeMode.END,
        )
        title.set_tooltip_text(translator("rename_title"))
        header.set_title_widget(title)
        self.cancel_button = wrapping_button(translator("cancel"))
        self.cancel_button.connect("clicked", lambda _button: self.close())
        header.pack_start(self.cancel_button)
        toolbar.add_top_bar(header)

        body = content_box()
        body.append(heading(translator("rename_heading")))
        body.append(description(translator("rename_help"), readable=True))
        self.entry = Gtk.Entry(text=recording.name, hexpand=True)
        self.entry.set_size_request(-1, MIN_CONTROL_HEIGHT)
        set_accessible_label(self.entry, translator("rename_name_label"))
        self.entry_field = labelled(translator("rename_name_label"), self.entry)
        body.append(self.entry_field)
        self.error = LiveStatus()
        body.append(self.error)
        self.rename_button = wrapping_button(translator("rename_submit"))
        self.rename_button.add_css_class("suggested-action")
        self.rename_button.connect("clicked", lambda _button: self._submit())
        body.append(self.rename_button)
        self.entry.connect("activate", lambda _entry: self._submit())
        self.set_default_widget(self.rename_button)

        scroll = scrolled_content(body)
        scroll.set_vexpand(True)
        toolbar.set_content(scroll)
        self.set_content(toolbar)
        install_escape_handler(self, self.close)
        self.connect("close-request", self._closing)
        self.connect("map", lambda _window: focus_later(self.entry))

    def _submit(self) -> None:
        if self._submitting:
            return
        name = self.entry.get_text().strip()
        invalid = not name
        set_invalid(self.entry, invalid)
        if invalid:
            self.error.set_status(self.translator("rename_empty"))
            focus_later(self.entry)
            return
        self._submitting = True
        self.entry.set_sensitive(False)
        self.rename_button.set_sensitive(False)
        self.cancel_button.set_sensitive(False)
        self.error.set_status(self.translator("rename_in_progress"))

        def completed(error: str | None) -> None:
            if error is None:
                self.close()
                return
            self._submitting = False
            self.entry.set_sensitive(True)
            self.rename_button.set_sensitive(True)
            self.cancel_button.set_sensitive(True)
            self.error.set_status(error)
            focus_later(self.entry)

        try:
            _invoke_async_operation(
                self.submit_callback,
                name,
                completed=completed,
            )
        except Exception as error:
            self._submitting = False
            self.entry.set_sensitive(True)
            self.rename_button.set_sensitive(True)
            self.cancel_button.set_sensitive(True)
            self.error.set_status(str(error))
            focus_later(self.entry)

    def _closing(self, _window: Gtk.Window) -> bool:
        if not self._focus_returned:
            self._focus_returned = True
            focus_later(self.return_to)
        return False


class PlayerWindow(ChildWindow):
    """Accessible player controls for one recording."""

    def __init__(
        self,
        parent: MainWindow,
        callbacks: PresentationCallbacks,
        recording: RecordingView,
    ) -> None:
        super().__init__(
            parent,
            parent.t("player_title"),
            parent.t("close"),
            width=CONTENT_WIDTH,
        )
        self.parent_window = parent
        self.callbacks = callbacks
        self.recording = recording
        self.view = PlayerView()
        self._updating_seek = False
        self._updating_speed = False
        self._closed_reported = False
        self._library_busy = False

        self.player_heading = heading(parent.t("player_heading"))
        self.page.append(self.player_heading)
        name = recording.name.strip() or parent.t("recording_untitled")
        self.recording_name = description(name, readable=True)
        self.page.append(self.recording_name)
        self.status = LiveStatus(parent.t("player_status_loading"))
        self.page.append(self.status)
        self.controls_heading = heading(
            parent.t("playback_controls_heading"), level=2
        )
        self.page.append(self.controls_heading)

        self.play_button = wrapping_button(parent.t("action_play"))
        self.play_button.set_size_request(-1, PRIMARY_CONTROL_HEIGHT)
        self.play_button.add_css_class("suggested-action")
        self.play_button.connect("clicked", self._play_clicked)
        self.rewind_button = wrapping_button(parent.t("action_rewind_10"))
        self.rewind_button.connect(
            "clicked",
            lambda _button: self._invoke(
                callbacks.on_player_seek_by,
                self.recording.identifier,
                -10.0,
            ),
        )
        self.forward_button = wrapping_button(parent.t("action_forward_10"))
        self.forward_button.connect(
            "clicked",
            lambda _button: self._invoke(
                callbacks.on_player_seek_by,
                self.recording.identifier,
                10.0,
            ),
        )
        self.page.append(
            action_group(self.play_button, self.rewind_button, self.forward_button)
        )

        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_hexpand(True)
        self.seek.set_draw_value(False)
        self.seek.set_size_request(-1, MIN_CONTROL_HEIGHT)
        set_accessible_label(self.seek, parent.t("seek_label"))
        self.seek.connect("change-value", self._seek_changed)
        self.seek_field = labelled(parent.t("seek_label"), self.seek)
        self.page.append(self.seek_field)
        self.time = description("", readable=True)
        self.page.append(self.time)

        self.speed = string_dropdown(
            parent.t("speed_label"),
            [format_speed(value) for value in SUPPORTED_PLAYBACK_SPEEDS],
            selected=index_for_value(SUPPORTED_PLAYBACK_SPEEDS, 1.0),
        )
        self.speed.connect("notify::selected", self._speed_changed)
        self.speed_field = labelled(parent.t("speed_label"), self.speed)
        self.page.append(self.speed_field)

        self.rename_button = wrapping_button(parent.t("rename"))
        self.rename_button.connect("clicked", self._rename_requested)
        self.delete_button = wrapping_button(parent.t("delete"))
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._delete_requested)
        self.page.append(action_group(self.rename_button, self.delete_button))
        self.connect("close-request", self._closing)
        self.set_player_view(self.view, announce=False)

    def set_library_busy(self, busy: bool) -> None:
        self._library_busy = bool(busy)
        self._sync_library_controls()
        if busy:
            self.status.set_status(
                self.parent_window.t("library_operation_in_progress")
            )

    def _sync_library_controls(self) -> None:
        # While playbin is still resolving the old URI, a filesystem rename
        # could race its first open.  Once prepared, Linux rename preserves
        # the already-open inode and playback state safely.
        stable_media = phase_name(self.view.phase) in {
            "ready",
            "playing",
            "paused",
            "ended",
            "error",
        }
        sensitive = stable_media and not self._library_busy
        self.rename_button.set_sensitive(sensitive)
        self.delete_button.set_sensitive(sensitive)

    def update_recording(self, recording: RecordingView) -> None:
        """Update renamed metadata without rebuilding playback controls."""

        self.recording = recording
        name = recording.name.strip() or self.parent_window.t(
            "recording_untitled"
        )
        self.recording_name.set_text(name)
        set_accessible_label(self.recording_name, name)

    def announce_library_status(self, message: str) -> None:
        self.status.set_status(message, announce=True)

    def retranslate(self) -> None:
        parent = self.parent_window
        self.set_title(parent.t("player_title"))
        self.title_widget.set_text(parent.t("player_title"))
        self.title_widget.set_tooltip_text(parent.t("player_title"))
        _set_button_text(self.close_button, parent.t("close"))
        self.player_heading.set_text(parent.t("player_heading"))
        self.controls_heading.set_text(parent.t("playback_controls_heading"))
        name = self.recording.name.strip() or parent.t("recording_untitled")
        self.recording_name.set_text(name)
        _set_form_label(self.seek_field, parent.t("seek_label"))
        _set_form_label(self.speed_field, parent.t("speed_label"))
        set_accessible_label(self.seek, parent.t("seek_label"))
        set_accessible_label(self.speed, parent.t("speed_label"))
        _set_button_text(self.rewind_button, parent.t("action_rewind_10"))
        _set_button_text(self.forward_button, parent.t("action_forward_10"))
        _set_button_text(self.rename_button, parent.t("rename"))
        _set_button_text(self.delete_button, parent.t("delete"))
        self.set_player_view(self.view, announce=False)

    def _invoke(self, callback: Callable[..., None], *values: object) -> bool:
        try:
            callback(*values)
        except Exception as error:
            self.status.set_status(
                self.parent_window.t("player_status_error", message=str(error))
            )
            return False
        return True

    def _play_clicked(self, _button: Gtk.Button) -> None:
        callback = (
            self.callbacks.on_player_pause
            if phase_name(self.view.phase) == "playing"
            else self.callbacks.on_player_play
        )
        self._invoke(callback, self.recording.identifier)

    def _seek_changed(
        self,
        _scale: Gtk.Scale,
        _scroll: Gtk.ScrollType,
        value: float,
    ) -> bool:
        if not self._updating_seek:
            self._invoke(
                self.callbacks.on_player_seek,
                self.recording.identifier,
                value,
            )
        return False

    def _speed_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        if self._updating_speed:
            return
        index = dropdown.get_selected()
        if index < len(SUPPORTED_PLAYBACK_SPEEDS):
            self._invoke(
                self.callbacks.on_player_speed,
                self.recording.identifier,
                SUPPORTED_PLAYBACK_SPEEDS[index],
            )

    def set_player_view(
        self, view: PlayerView, *, announce: bool = True
    ) -> None:
        self.view = view
        phase = phase_name(view.phase)
        status_key = {
            "idle": "player_status_ready",
            "preparing": "player_status_loading",
            "ready": "player_status_ready",
            "playing": "player_status_playing",
            "paused": "player_status_paused",
            "ended": "player_status_ended",
            "error": "player_status_error",
        }.get(phase, "player_status_ready")
        if view.error:
            status_text = self.parent_window.t(
                "player_status_error", message=view.error
            )
        elif status_key == "player_status_error":
            status_text = self.parent_window.t(
                "player_status_error", message=self.parent_window.t("not_available")
            )
        else:
            status_text = self.parent_window.t(status_key)
        self.status.set_status(status_text, announce=announce)

        playing = phase == "playing"
        _set_button_text(
            self.play_button,
            self.parent_window.t(
                "action_player_pause" if playing else "action_play"
            ),
        )
        playable = phase in {"ready", "playing", "paused", "ended"}
        duration = _finite_nonnegative(view.duration_seconds)
        raw_position = _finite_nonnegative(view.position_seconds)
        position = min(raw_position, duration) if duration else raw_position
        seekable = playable and duration > 0.0
        self.play_button.set_sensitive(playable)
        self.seek.set_sensitive(seekable)
        self.rewind_button.set_sensitive(seekable)
        self.forward_button.set_sensitive(seekable)
        self.speed.set_sensitive(playable)
        self._sync_library_controls()

        self._updating_seek = True
        try:
            self.seek.set_range(0.0, max(1.0, duration, position))
            self.seek.set_value(position)
        finally:
            self._updating_seek = False
        position_text = format_duration(position)
        duration_text = format_duration(view.duration_seconds)
        if duration > 0.0:
            time_text = self.parent_window.t(
                "player_time", position=position_text, duration=duration_text
            )
        else:
            time_text = self.parent_window.t(
                "player_time_unknown", position=position_text
            )
        self.time.set_text(time_text)
        set_value_text(self.seek, time_text)
        set_accessible_label(
            self.time, f"{self.parent_window.t('seek_label')}: {time_text}"
        )

        speed = view.speed if view.speed in SUPPORTED_PLAYBACK_SPEEDS else 1.0
        self._updating_speed = True
        try:
            self.speed.set_selected(
                index_for_value(SUPPORTED_PLAYBACK_SPEEDS, speed)
            )
        finally:
            self._updating_speed = False

    def _rename_requested(self, button: Gtk.Button) -> None:
        RenameWindow(
            self,
            self.parent_window.translator,
            self.recording,
            button,
            lambda name, completed: _invoke_async_operation(
                self.callbacks.on_rename_recording,
                self.recording.identifier,
                name,
                completed=completed,
            ),
        ).present()

    def _delete_requested(self, button: Gtk.Button) -> None:
        name = self.recording.name.strip() or self.parent_window.t(
            "recording_untitled"
        )
        confirm(
            self,
            self.parent_window.t("delete_title"),
            self.parent_window.t("delete_body", name=name),
            self.parent_window.t("cancel"),
            self.parent_window.t("delete_confirm"),
            lambda: self._delete_confirmed(),
            return_to=button,
            destructive=True,
        )

    def _delete_confirmed(self) -> None:
        def completed(error: str | None) -> None:
            if error is not None:
                self.status.set_status(error)

        try:
            _invoke_async_operation(
                self.callbacks.on_delete_recordings,
                (self.recording.identifier,),
                completed=completed,
            )
        except Exception as error:
            self.status.set_status(
                self.parent_window.t("error_delete", message=str(error))
            )

    def _closing(self, _window: Gtk.Window) -> bool:
        if not self._closed_reported:
            self._closed_reported = True
            self._invoke(
                self.callbacks.on_player_closed, self.recording.identifier
            )
        return False


__all__ = [
    "ACTIVE_RECORDING_PHASES",
    "MainWindow",
    "OperationCompletion",
    "PlayerView",
    "PlayerWindow",
    "PresentationCallbacks",
    "RecorderView",
    "RecordingView",
    "RecordingsWindow",
    "RenameWindow",
    "SettingsWindow",
    "UICallbacks",
]
