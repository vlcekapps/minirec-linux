"""GTK application controller for MiniRec.

The controller is intentionally thin: recording, playback, persistence and
recovery remain usable without GTK.  This module binds their immutable events
to the presentation callbacks, desktop notifications and suspend inhibition.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Final, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__
from .i18n import Translator
from .gtk_helpers import (
    content_box,
    description,
    heading,
    scrolled_content,
    wrapping_button,
)
from .models import RecordingFormat, RecordingSettings
from .playback import (
    PlaybackEvent,
    PlaybackEventType,
    PlaybackPhase,
    Player,
)
from .recording import (
    Recorder,
    RecordingEvent,
    RecordingEventType,
    RecordingPhase,
)
from .settings import AppSettings, SettingsError, SettingsStore
from .storage import (
    DeleteResult,
    EmptyRecordingNameError,
    InvalidRecordingNameError,
    MAX_SELECTED_RECORDINGS,
    PendingRecording,
    RecordingItem,
    RecordingNameConflictError,
    RecordingNameTooLongError,
    RecordingRecoveryStatus,
    RecordingStorage,
    StorageIdentityError,
    StorageProcessLock,
    StorageProcessLockError,
)
from .ui import MainWindow, PlayerView, RecorderView, RecordingView


APPLICATION_ID: Final = "cz.pvlcek.minirec"
RECORDING_NOTIFICATION_ID: Final = "active-recording"
SUPPORT_URI: Final = (
    "https://obchod.pvlcek.cz/produkt/"
    "kupte-autorovi-kavu-podpora-tvorby-podcastu-a-karosy/"
)
TICK_INTERVAL_MS: Final = 250
DISK_CHECK_INTERVAL_SECONDS: Final = 10.0
STOP_WATCHDOG_SECONDS: Final = 15.0

_ACTIVE_PHASES: Final = frozenset(
    {
        RecordingPhase.STARTING,
        RecordingPhase.RECORDING,
        RecordingPhase.PAUSING,
        RecordingPhase.PAUSED,
        RecordingPhase.RESUMING,
        RecordingPhase.STOPPING,
    }
)
_PUBLISHABLE_RECOVERY: Final = frozenset(
    {RecordingRecoveryStatus.RECOVERED, RecordingRecoveryStatus.COMPLETED}
)

_T = TypeVar("_T")


def _dispatch_on_main(callback: Callable[[], None]) -> None:
    """Schedule *callback* in GTK's owning main context exactly once."""

    def invoke() -> bool:
        callback()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(invoke)


def _recording_view(item: RecordingItem) -> RecordingView:
    """Copy every user-visible storage field into the presentation model."""

    return RecordingView(
        identifier=item.id,
        name=item.name,
        duration_seconds=item.duration_seconds,
        size_bytes=item.size_bytes,
        modified_ns=item.modified_ns,
        format=item.format,
    )


def data_file(name: str) -> Path:
    """Return a package data path without relying on the process directory."""

    return Path(__file__).resolve().parent / "data" / name


def preserve_invalid_settings(path: Path) -> Path | None:
    """Move a malformed settings file aside without replacing another file."""

    try:
        path.lstat()
    except FileNotFoundError:
        return None
    for _attempt in range(128):
        backup = path.with_name(
            f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-"
            f"{secrets.token_hex(4)}"
        )
        if backup.exists():
            continue
        try:
            os.rename(path, backup)
        except FileExistsError:
            continue
        return backup
    raise OSError(f"Could not allocate a settings backup beside {path}")


class MiniRecApplication(Adw.Application):
    """Coordinate one foreground-only MiniRec process."""

    version = __version__

    def __init__(
        self,
        *,
        settings_store: SettingsStore | None = None,
        storage: RecordingStorage | None = None,
        recorder_factory: Callable[..., Recorder] = Recorder,
        player_factory: Callable[..., Player] = Player,
        window_factory: Callable[..., MainWindow] = MainWindow,
        clock: Callable[[], float] = time.monotonic,
        storage_executor: Executor | None = None,
        dispatcher: Callable[[Callable[[], None]], None] = _dispatch_on_main,
        application_id: str = APPLICATION_ID,
        non_unique: bool = False,
    ) -> None:
        flags = (
            Gio.ApplicationFlags.NON_UNIQUE
            if non_unique
            else Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        super().__init__(application_id=application_id, flags=flags)
        self.settings_store = settings_store or SettingsStore()
        self.storage = storage or RecordingStorage()
        self.recorder_factory = recorder_factory
        self.player_factory = player_factory
        self.window_factory = window_factory
        self.clock = clock
        self._owns_storage_executor = storage_executor is None
        self._storage_executor = storage_executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="minirec-storage",
        )
        self._dispatcher = dispatcher
        self._storage_generation: dict[str, int] = {}
        self._storage_futures: set[Future[object]] = set()
        self._storage_futures_lock = threading.Lock()
        self._settings_ui_generation = 0
        self._pending_settings_ui: tuple[
            AppSettings, Translator | None
        ] | None = None

        self.settings = AppSettings()
        self.window: MainWindow | None = None
        self.recorder: Recorder | None = None
        self.pending: PendingRecording | None = None
        self.player: Player | None = None
        self.player_identifier: str | None = None
        self.recording_items: dict[str, RecordingItem] = {}

        self._initialized = False
        self._process_lock: StorageProcessLock | None = None
        self._instance_lock_error: str | None = None
        self._instance_lock_fatal = False
        self._busy_window: InstanceBusyWindow | None = None
        self._startup_messages: list[str] = []
        self._startup_recovery_started = False
        self._startup_recovery_complete = False
        self._recording_storage_busy = False
        self._recording_prepare_busy = False
        self._cancel_start_after_prepare = False
        self._library_mutation_busy = False
        self._recording_held = False
        self._inhibit_cookie = 0
        self._tick_source_id = 0
        self._remaining_base: float | None = None
        self._remaining_elapsed_at_check = 0.0
        self._wav_session_limit: float | None = None
        self._disk_check_due = 0.0
        self._stop_requested_at: float | None = None
        self._quit_after_stop = False
        self._shutting_down = False
        self._toggle_recording_action: Gio.SimpleAction | None = None
        self._stop_recording_action: Gio.SimpleAction | None = None

    # -- GApplication lifecycle -------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        present_action = Gio.SimpleAction.new("present", None)
        present_action.connect("activate", self._notification_present)
        self.add_action(present_action)
        self._toggle_recording_action = Gio.SimpleAction.new(
            "recording-toggle", None
        )
        self._toggle_recording_action.connect(
            "activate", self._notification_toggle
        )
        self.add_action(self._toggle_recording_action)
        self._stop_recording_action = Gio.SimpleAction.new("recording-stop", None)
        self._stop_recording_action.connect(
            "activate", lambda _action, _parameter: self.on_stop()
        )
        self.add_action(self._stop_recording_action)
        self._sync_recording_actions()

        css = data_file("style.css")
        display = Gdk.Display.get_default()
        if display is not None and css.is_file():
            provider = Gtk.CssProvider()
            provider.load_from_path(str(css))
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            # Keep the provider alive for the application's lifetime.
            self._css_provider = provider

    def do_activate(self) -> None:
        self._initialize_once()
        if self._instance_lock_error is not None:
            if self._busy_window is None:
                self._busy_window = InstanceBusyWindow(
                    self,
                    self._translator(),
                    self._instance_lock_error,
                    fatal=self._instance_lock_fatal,
                )
            self._busy_window.present()
            return
        if self.window is None:
            self.window = self.window_factory(
                self,
                self,
                translator=self._translator(),
                settings=self.settings,
                version=self.version,
                recordings_path=str(self.storage.recordings_dir),
            )
            self.window.set_icon_name(APPLICATION_ID)
            if self._startup_recovery_complete:
                self._measure_remaining()
                self._render_idle(announce=False)
                self.on_refresh_recordings()
            else:
                self.window.set_recorder_view(
                    RecorderView(phase="recovering"),
                    announce=False,
                )
        self.window.present()
        if self._startup_recovery_complete:
            self._show_startup_messages()

    def do_shutdown(self) -> None:
        self._shutting_down = True
        self._invalidate_settings_ui_callbacks()
        self._invalidate_storage_callbacks()
        self._withdraw_recording_notification()
        if self.player is not None:
            self._dispose_player()
        if self.recorder is not None:
            # A normal window close cannot reach shutdown while recording.
            # Forced session/process termination may; close requests EOS, and
            # the already-durable journal remains authoritative if the main
            # loop is no longer able to deliver completion.
            recorder = self.recorder
            recorder.set_event_callback(lambda _event: None)
            recorder.emergency_close_for_process_exit()
            self.recorder = None
        # Complete any already-authorized storage transaction while the
        # cross-session process lock, application hold and suspend inhibit are
        # still owned.  A queued main-context delivery is intentionally stale
        # after ``_invalidate_storage_callbacks``.
        self._wait_for_storage_workers()
        self.pending = None
        self._recording_storage_busy = False
        self._recording_prepare_busy = False
        self._cancel_start_after_prepare = False
        self._library_mutation_busy = False
        self._release_inhibit()
        self._release_recording_hold()
        if self._process_lock is not None:
            self._process_lock.close()
            self._process_lock = None
        Adw.Application.do_shutdown(self)

    def _initialize_once(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        try:
            process_lock = StorageProcessLock(self.storage.state_dir)
            process_lock.acquire()
        except OSError as error:
            self._instance_lock_error = str(error)
            self._instance_lock_fatal = not isinstance(
                error, StorageProcessLockError
            )
            try:
                self.settings = self.settings_store.load()
            except Exception:
                # The read is best-effort only; a broken settings backend must
                # never suppress the accessible lock-failure window.
                self.settings = AppSettings()
            return
        self._process_lock = process_lock
        try:
            self.settings = self.settings_store.load()
        except SettingsError as error:
            try:
                backup = preserve_invalid_settings(self.settings_store.path)
                self.settings = self.settings_store.reset()
            except Exception as recovery_error:
                self.settings = AppSettings()
                self._startup_messages.append(
                    self._translator()(
                        "startup_settings_failed", message=str(recovery_error)
                    )
                )
            else:
                suffix = f" ({backup})" if backup is not None else ""
                self._startup_messages.append(
                    self._translator()(
                        "startup_settings_restored",
                        message=f"{error}{suffix}",
                    )
                )
        self._start_startup_recovery()

    # -- Serialized storage work ----------------------------------------------

    def _invalidate_settings_ui_callbacks(self) -> None:
        self._settings_ui_generation += 1
        self._pending_settings_ui = None

    def _queue_settings_ui_apply(
        self,
        settings: AppSettings,
        *,
        translator: Translator | None = None,
    ) -> None:
        """Apply a saved snapshot after the current GTK signal has returned.

        A ``Gtk.DropDown`` emits ``notify::selected`` while its popup list is
        still activating an item.  Replacing that dropdown's model from the
        callback invalidates GTK objects which the activation code still owns.
        The production dispatcher uses ``GLib.idle_add``; generations also
        coalesce rapid slider/dropdown updates without losing a pending real
        language change.
        """

        self._settings_ui_generation += 1
        generation = self._settings_ui_generation
        window = self.window
        if window is None:
            self._pending_settings_ui = None
            return
        pending = self._pending_settings_ui
        if translator is None and pending is not None:
            translator = pending[1]
        self._pending_settings_ui = (settings, translator)

        def deliver() -> None:
            if generation != self._settings_ui_generation:
                return
            pending_apply = self._pending_settings_ui
            self._pending_settings_ui = None
            if (
                pending_apply is None
                or self._shutting_down
                or self.window is not window
            ):
                return
            saved, saved_translator = pending_apply
            window.apply_settings(saved, translator=saved_translator)

        self._dispatcher(deliver)

    def _next_storage_generation(self, domain: str) -> int:
        generation = self._storage_generation.get(domain, 0) + 1
        self._storage_generation[domain] = generation
        return generation

    def _submit_storage(
        self,
        domain: str,
        work: Callable[[], _T],
        completed: Callable[[_T | None, BaseException | None], None],
    ) -> int:
        """Run storage work serially and deliver only its current generation."""

        generation = self._next_storage_generation(domain)

        def deliver(result: _T | None, error: BaseException | None) -> None:
            if self._shutting_down:
                return
            if self._storage_generation.get(domain) != generation:
                return
            completed(result, error)

        try:
            future = self._storage_executor.submit(work)
        except BaseException as error:
            self._dispatcher(lambda error=error: deliver(None, error))
            return generation

        with self._storage_futures_lock:
            self._storage_futures.add(future)

        def finished(done: Future[_T]) -> None:
            with self._storage_futures_lock:
                self._storage_futures.discard(done)
            try:
                result = done.result()
            except BaseException as error:
                self._dispatcher(lambda error=error: deliver(None, error))
            else:
                self._dispatcher(lambda: deliver(result, None))

        future.add_done_callback(finished)
        return generation

    def _invalidate_storage_callbacks(self) -> None:
        for domain in tuple(self._storage_generation):
            self._next_storage_generation(domain)

    def _wait_for_storage_workers(self) -> None:
        while True:
            with self._storage_futures_lock:
                futures = tuple(self._storage_futures)
            if not futures:
                break
            for future in futures:
                try:
                    future.result()
                except BaseException:
                    # The worker result is reported in the normal main-context
                    # callback unless shutdown has invalidated it.
                    pass
        if self._owns_storage_executor:
            self._storage_executor.shutdown(wait=True)

    def _start_startup_recovery(self) -> None:
        if self._startup_recovery_started or self._instance_lock_error is not None:
            return
        self._startup_recovery_started = True
        if self.window is not None:
            # Startup recovery is busy but not an active capture.  Settings and
            # the library remain keyboard reachable while this phase is shown.
            self.window.set_recorder_view(
                RecorderView(phase="recovering"),
                announce=False,
            )
        self._submit_storage(
            "startup",
            self.storage.recover_startup,
            self._startup_recovery_finished,
        )

    def _startup_recovery_finished(
        self,
        report: object | None,
        error: BaseException | None,
    ) -> None:
        self._startup_recovery_complete = True
        translator = self._translator()
        if error is not None:
            self._startup_messages.append(
                translator("startup_recovery_failed", message=str(error))
            )
        elif report is not None:
            recovered = report.recovered_paths
            if recovered:
                names = ", ".join(path.name for path in recovered[:3])
                if len(recovered) > 3:
                    names += translator("and_more", count=len(recovered) - 3)
                self._startup_messages.append(
                    translator("startup_recordings_recovered", name=names)
                )
            uncertain = [
                outcome
                for outcome in report.recordings
                if outcome.status is RecordingRecoveryStatus.UNCERTAIN
            ]
            uncertain.extend(
                item
                for item in report.deletions
                if getattr(item.status, "value", item.status) == "uncertain"
            )
            if uncertain:
                self._startup_messages.append(translator("startup_uncertain"))

        self._measure_remaining()
        self._render_idle(announce=False)
        self.on_refresh_recordings()
        self._show_startup_messages()

    def _show_startup_messages(self) -> None:
        if self.window is None or not self._startup_messages:
            return
        message = "\n".join(self._startup_messages)
        self._startup_messages.clear()
        self.window.status.set_status(message, announce=True)

    # -- Recording presentation and timing -------------------------------------

    def _translator(self) -> Translator:
        return Translator(self.settings.language.value)

    def _render_idle(self, *, announce: bool) -> None:
        if self.window is None:
            return
        self.window.set_recorder_view(
            RecorderView(
                phase="idle",
                elapsed_seconds=0.0,
                remaining_seconds=self._remaining_for_display(),
            ),
            announce=announce,
        )

    def _render_recorder(self, *, announce: bool = False) -> None:
        if self.window is None or self.recorder is None:
            return
        snapshot = self.recorder.snapshot
        phase: object = snapshot.phase
        error = snapshot.error
        # Backend STOPPED means the fd is finalized; publication and directory
        # fsync still happen in the FINALIZED callback below.
        if self._recording_storage_busy and self.pending is not None:
            phase = "finalizing"
            error = None
        elif snapshot.phase is RecordingPhase.STOPPED and self.pending is not None:
            phase = "finalizing"
        self.window.set_recorder_view(
            RecorderView(
                phase=phase,
                elapsed_seconds=snapshot.elapsed_seconds,
                remaining_seconds=self._remaining_for_display(),
                error=error,
            ),
            announce=announce,
        )

    def _measure_remaining(self) -> float:
        try:
            value = float(self.storage.remaining_seconds(self.settings.recording))
        except Exception:
            value = 0.0
        elapsed = self.recorder.elapsed_seconds if self.recorder is not None else 0.0
        if self.settings.recording.format is RecordingFormat.WAV:
            if self._wav_session_limit is None and self.recorder is not None:
                self._wav_session_limit = value
            if self._wav_session_limit is not None:
                value = min(value, max(0.0, self._wav_session_limit - elapsed))
        self._remaining_base = max(0.0, value)
        self._remaining_elapsed_at_check = elapsed
        self._disk_check_due = self.clock() + DISK_CHECK_INTERVAL_SECONDS
        return self._remaining_base

    def _remaining_for_display(self) -> float | None:
        if self._remaining_base is None:
            return None
        if self.recorder is None:
            return self._remaining_base
        captured = max(
            0.0,
            self.recorder.elapsed_seconds - self._remaining_elapsed_at_check,
        )
        return max(0.0, self._remaining_base - captured)

    def _ensure_tick(self) -> None:
        if self._tick_source_id == 0:
            self._tick_source_id = GLib.timeout_add(
                TICK_INTERVAL_MS, self._tick
            )

    def _tick(self) -> bool:
        now = self.clock()
        recorder = self.recorder
        if recorder is not None:
            if (
                recorder.phase is RecordingPhase.STOPPING
                and self._stop_requested_at is not None
                and now - self._stop_requested_at >= STOP_WATCHDOG_SECONDS
            ):
                recorder.timeout_stalled_stop()
                recorder = self.recorder
            if (
                recorder is not None
                and recorder.phase in _ACTIVE_PHASES
                and recorder.phase is not RecordingPhase.STOPPING
                and now >= self._disk_check_due
            ):
                if self._measure_remaining() <= 0.0:
                    if self.window is not None:
                        self.window.status.set_status(
                            self._translator()("status_storage_reserve_stop"),
                            announce=True,
                        )
                    self.on_stop()
            self._render_recorder(announce=False)

        player = self.player
        if player is not None and player.phase not in {
            PlaybackPhase.CLOSED,
            PlaybackPhase.IDLE,
            PlaybackPhase.ERROR,
        }:
            player.refresh()

        if self.recorder is None and self.player is None:
            self._tick_source_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # -- PresentationCallbacks: recording --------------------------------------

    def on_record(self) -> None:
        self._require_writable_instance()
        if not self._startup_recovery_complete:
            raise StorageProcessLockError(
                self._translator()("error_startup_busy")
            )
        if self.pending is not None or self._recording_storage_busy:
            return
        if self.recorder is not None and self.recorder.phase in _ACTIVE_PHASES:
            return
        if self.window is not None:
            self.window.close_player()
        self._dispose_player()
        self._wav_session_limit = None
        self._measure_remaining()
        if (self._remaining_base or 0.0) <= 0.0:
            if self.window is not None:
                self.window.set_recorder_view(
                    RecorderView(
                        phase="error",
                        remaining_seconds=0.0,
                        error=self._translator()("error_not_enough_space"),
                    ),
                    announce=True,
                )
            return

        recording_settings = self.settings.recording
        self._quit_after_stop = False
        self._cancel_start_after_prepare = False
        self._recording_prepare_busy = True
        self._recording_storage_busy = True
        self._hold_recording()
        if self.window is not None:
            self.window.set_recorder_view(
                RecorderView(
                    phase="starting",
                    remaining_seconds=self._remaining_for_display(),
                ),
                announce=False,
            )

        def prepared(
            pending: PendingRecording | None,
            error: BaseException | None,
        ) -> None:
            self._recording_prepare_busy = False
            if error is not None or pending is None:
                self._recording_storage_busy = False
                self._release_recording_hold()
                message = self._translator()(
                    "error_recording_prepare",
                    message=str(error or "unknown preparation failure"),
                )
                if self.window is not None:
                    self.window.set_recorder_view(
                        RecorderView(
                            phase="error",
                            remaining_seconds=self._remaining_for_display(),
                            error=message,
                        ),
                        announce=True,
                    )
                    if self._quit_after_stop:
                        self.window.complete_stop_and_quit()
                self._quit_after_stop = False
                return

            self.pending = pending
            if self._cancel_start_after_prepare:
                self._cancel_prepared_recording(pending)
                return
            self._recording_storage_busy = False
            self._start_prepared_recording(pending, recording_settings)

        self._submit_storage(
            "recording",
            lambda: self.storage.create_pending(
                recording_settings.format
            ),
            prepared,
        )

    def _start_prepared_recording(
        self,
        pending: PendingRecording,
        recording_settings: RecordingSettings,
    ) -> None:
        try:
            recorder = self.recorder_factory(
                recording_settings,
                self._on_recording_event,
            )
        except Exception as error:
            self._recover_failed_recording(
                self._translator()(
                    "error_recording_start_detail", message=str(error)
                )
            )
            return
        self.recorder = recorder
        if recording_settings.format is RecordingFormat.WAV:
            self._wav_session_limit = self._remaining_base
        self._remaining_elapsed_at_check = 0.0
        self._disk_check_due = self.clock() + DISK_CHECK_INTERVAL_SECONDS
        self._stop_requested_at = None
        self._sync_inhibit()
        self._ensure_tick()
        try:
            started = recorder.start(pending.path, prepared=pending.prepared)
        except Exception as error:
            # start() may have adopted the pending descriptor before raising;
            # retain the exact journal and let format-aware recovery decide.
            self._recover_failed_recording(
                self._translator()(
                    "error_recording_start_detail", message=str(error)
                )
            )
            return
        if not started and self.recorder is not None:
            self._recover_failed_recording(
                self._translator()("error_recording_start")
            )

    def _cancel_prepared_recording(self, pending: PendingRecording) -> None:
        self._recording_storage_busy = True
        if self.window is not None:
            self.window.set_recorder_view(
                RecorderView(
                    phase="finalizing",
                    remaining_seconds=self._remaining_for_display(),
                ),
                announce=False,
            )

        def aborted(
            _result: object | None,
            error: BaseException | None,
        ) -> None:
            self._recording_storage_busy = False
            if error is not None:
                self._recover_failed_recording(
                    self._translator()(
                        "error_recording_cancel", message=str(error)
                    )
                )
                return
            if self.pending is pending:
                self.pending = None
            quit_after_cleanup = self._quit_after_stop
            self._finish_recording_session()
            self._measure_remaining()
            self._render_idle(announce=False)
            if self.window is not None:
                self.window.status.set_status(
                    self._translator()("status_recording_cancelled"),
                    announce=True,
                )
                if quit_after_cleanup:
                    self.window.complete_stop_and_quit()
            self._quit_after_stop = False

        self._submit_storage(
            "recording",
            lambda: self.storage.abort(pending),
            aborted,
        )

    def on_pause(self) -> None:
        if self.recorder is not None:
            self.recorder.pause()

    def on_resume(self) -> None:
        if self.recorder is not None:
            self.recorder.resume()

    def on_stop(self) -> None:
        recorder = self.recorder
        if recorder is None:
            if self._recording_prepare_busy:
                self._cancel_start_after_prepare = True
            return
        if recorder.phase is not RecordingPhase.STOPPING:
            self._stop_requested_at = self.clock()
        stopped = recorder.stop()
        if (
            not stopped
            and self.recorder is recorder
            and recorder.phase is not RecordingPhase.ERROR
        ):
            self._stop_requested_at = None

    def on_stop_and_quit(self) -> None:
        self._quit_after_stop = True
        if self.recorder is None:
            if self._recording_prepare_busy:
                self._cancel_start_after_prepare = True
            elif self._recording_storage_busy:
                # Publication/recovery already owns the safe completion path.
                return
            elif self.window is not None:
                self.window.complete_stop_and_quit()
            return
        self.on_stop()

    def _on_recording_event(self, event: RecordingEvent) -> None:
        if self.recorder is None or self._shutting_down:
            return
        if event.type is RecordingEventType.STATE_CHANGED:
            self._render_recorder(announce=False)
            self._sync_recording_actions()
            self._sync_recording_notification()
            self._sync_inhibit()
            return
        if event.type in {
            RecordingEventType.SOURCE_FALLBACK,
            RecordingEventType.CHANNEL_FALLBACK,
            RecordingEventType.SIGNAL_ERROR,
        }:
            if self.window is not None:
                key = {
                    RecordingEventType.SOURCE_FALLBACK: "status_source_fallback",
                    RecordingEventType.CHANNEL_FALLBACK: "status_channel_fallback",
                    RecordingEventType.SIGNAL_ERROR: "status_signal_error",
                }[event.type]
                message = self._translator()(key)
                if event.detail:
                    message = self._translator()(
                        "status_with_technical_detail",
                        message=message,
                        details=event.detail,
                    )
                self.window.status.set_status(message, announce=True)
            return
        if event.type is RecordingEventType.FINALIZED:
            self._publish_finalized_recording(event)
            return
        if event.type is RecordingEventType.ERROR:
            self._recover_failed_recording(
                event.detail
                or event.snapshot.error
                or self._translator()("error_recording_generic")
            )

    def _publish_finalized_recording(self, event: RecordingEvent) -> None:
        pending = self.pending
        if pending is None:
            self._recover_failed_recording(
                self._translator()("error_publication_missing")
            )
            return
        if self._recording_storage_busy:
            return
        self._recording_storage_busy = True
        elapsed = float(event.snapshot.elapsed_seconds)
        if self.window is not None:
            self.window.set_recorder_view(
                RecorderView(
                    phase="finalizing",
                    elapsed_seconds=elapsed,
                    remaining_seconds=self._remaining_for_display(),
                ),
                announce=False,
            )
        self._sync_inhibit()

        def completed(
            final_path: Path | None,
            error: BaseException | None,
        ) -> None:
            self._recording_storage_busy = False
            if error is not None or final_path is None:
                self._recover_failed_recording(
                    self._translator()(
                        "error_publication",
                        message=str(error or "unknown publication failure"),
                    )
                )
                return
            # Keep the exact pending identity, hold and inhibit through the
            # durable publish worker; clear them only on this main-context
            # completion.
            if self.pending is not pending:
                return
            self.pending = None
            self._finish_recording_session()
            self._measure_remaining()
            self.on_refresh_recordings()
            if self.window is not None:
                self.window.set_recorder_view(
                    RecorderView(
                        phase="stopped",
                        elapsed_seconds=elapsed,
                        remaining_seconds=self._remaining_for_display(),
                    ),
                    announce=False,
                )
                self.window.status.set_status(
                    self._translator()(
                        "status_recording_saved", name=final_path.name
                    ),
                    announce=True,
                )
            if self._quit_after_stop and self.window is not None:
                self.window.complete_stop_and_quit()

        self._submit_storage(
            "recording",
            lambda: self.storage.complete(pending),
            completed,
        )

    def _recover_failed_recording(self, detail: str) -> None:
        pending = self.pending
        if self._recording_storage_busy:
            return
        self._recording_storage_busy = True
        elapsed = self.recorder.elapsed_seconds if self.recorder is not None else 0.0
        if self.window is not None:
            self.window.set_recorder_view(
                RecorderView(
                    phase="finalizing",
                    elapsed_seconds=elapsed,
                    remaining_seconds=self._remaining_for_display(),
                ),
                announce=False,
            )
        self._sync_inhibit()

        def recovered(
            report: object | None,
            recovery_error: BaseException | None,
        ) -> None:
            self._recording_storage_busy = False
            published: Path | None = None
            if recovery_error is None and report is not None and pending is not None:
                for outcome in report.recordings:
                    if (
                        outcome.journal_path == pending.journal_path
                        and outcome.status in _PUBLISHABLE_RECOVERY
                        and outcome.final_path is not None
                    ):
                        published = outcome.final_path
                        break

            if self.pending is pending:
                self.pending = None
            self._finish_recording_session()
            self._measure_remaining()
            self.on_refresh_recordings()
            if self.window is None:
                return
            if published is not None:
                self.window.set_recorder_view(
                    RecorderView(
                        phase="stopped",
                        elapsed_seconds=elapsed,
                        remaining_seconds=self._remaining_for_display(),
                    ),
                    announce=False,
                )
                self.window.status.set_status(
                    self._translator()(
                        "status_recording_recovered", name=published.name
                    ),
                    announce=True,
                )
            else:
                if recovery_error is not None:
                    final_detail = self._translator()(
                        "error_recovery_failed",
                        message=detail,
                        details=str(recovery_error),
                    )
                else:
                    final_detail = self._translator()(
                        "error_pending_retained", message=detail
                    )
                self.window.set_recorder_view(
                    RecorderView(
                        phase="error",
                        elapsed_seconds=elapsed,
                        remaining_seconds=self._remaining_for_display(),
                        error=final_detail,
                    ),
                    announce=True,
                )
            if published is not None and self._quit_after_stop:
                self.window.complete_stop_and_quit()
            elif published is None:
                self._quit_after_stop = False

        self._submit_storage(
            "recording",
            self.storage.recover_startup,
            recovered,
        )

    def _finish_recording_session(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder is not None:
            recorder.set_event_callback(lambda _event: None)
            recorder.close()
        self._stop_requested_at = None
        self._wav_session_limit = None
        self._recording_storage_busy = False
        self._recording_prepare_busy = False
        self._cancel_start_after_prepare = False
        self._withdraw_recording_notification()
        self._release_inhibit()
        self._release_recording_hold()
        self._sync_recording_actions()

    # -- Settings and recording library ----------------------------------------

    def on_settings_changed(self, settings: AppSettings) -> None:
        self._require_writable_instance()
        previous = self.settings
        try:
            saved = self.settings_store.save(settings)
        except Exception:
            self._queue_settings_ui_apply(previous)
            raise
        self.settings = saved
        translator = (
            self._translator()
            if saved.language is not previous.language
            else None
        )
        self._queue_settings_ui_apply(saved, translator=translator)
        self._measure_remaining()
        if self.recorder is None:
            if self._startup_recovery_complete:
                self._render_idle(announce=False)
            elif self.window is not None:
                self.window.set_recorder_view(
                    RecorderView(phase="recovering"),
                    announce=False,
                )
        self._sync_inhibit()
        self._sync_recording_notification()

    def on_refresh_recordings(self) -> None:
        if self.window is None:
            return

        def completed(
            listed: list[RecordingItem] | None,
            error: BaseException | None,
        ) -> None:
            if self.window is None:
                return
            if error is not None or listed is None:
                self.recording_items = {}
                self.window.set_recordings(
                    (),
                    error=str(error or "unknown listing failure"),
                )
                return
            items = tuple(listed)
            self.recording_items = {item.id: item for item in items}
            self.window.set_recordings(
                tuple(_recording_view(item) for item in items)
            )

        self._submit_storage(
            "recordings-list",
            self.storage.list_recordings,
            completed,
        )

    def on_open_recordings_folder(self) -> None:
        self._require_writable_instance()
        self.storage.recordings_dir.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(
            self.storage.recordings_dir.as_uri(), None
        )

    def on_thank_author(self) -> None:
        Gio.AppInfo.launch_default_for_uri(SUPPORT_URI, None)

    def _require_item(self, identifier: str) -> RecordingItem:
        try:
            return self.recording_items[identifier]
        except KeyError as error:
            raise FileNotFoundError(
                self._translator()("error_recording_missing")
            ) from error

    def _set_library_busy(self, busy: bool) -> None:
        self._library_mutation_busy = busy
        if self.window is not None:
            setter = getattr(self.window, "set_library_busy", None)
            if setter is not None:
                setter(busy)

    def _library_error(
        self,
        key: str,
        error: BaseException,
        completed: Callable[[str | None], None] | None,
    ) -> None:
        translator = self._translator()
        if isinstance(error, (StorageIdentityError, FileNotFoundError)):
            message = translator("error_recording_changed")
        else:
            message = translator(key, message=str(error))
        if completed is not None:
            completed(message)
        elif self.window is not None:
            self.window.set_recordings(
                tuple(
                    _recording_view(item)
                    for item in self.recording_items.values()
                ),
                error=message,
            )

    def _rename_error_message(self, error: BaseException) -> str:
        translator = self._translator()
        if isinstance(error, EmptyRecordingNameError):
            return translator("error_rename_empty")
        if isinstance(error, InvalidRecordingNameError):
            return translator("error_rename_invalid")
        if isinstance(error, RecordingNameTooLongError):
            return translator("error_rename_too_long")
        if isinstance(error, RecordingNameConflictError):
            return translator("error_rename_conflict")
        if isinstance(error, StorageIdentityError):
            return translator("error_recording_changed")
        if isinstance(error, FileNotFoundError):
            return translator("error_recording_changed")
        return translator("error_rename", message=str(error))

    def _require_player_media_stable(self, identifiers: tuple[str, ...]) -> None:
        """Reject pathname mutations while playbin is still opening that file."""

        if (
            self.player is not None
            and self.player.phase is PlaybackPhase.PREPARING
            and self.player_identifier in identifiers
        ):
            raise RuntimeError(self._translator()("error_player_preparing"))

    def _complete_library_message(
        self,
        message: str,
        completed: Callable[[str | None], None] | None,
    ) -> None:
        if completed is not None:
            completed(message)
        elif self.window is not None:
            announcer = getattr(self.window, "announce_library_status", None)
            if announcer is not None:
                announcer(message)
            else:
                self.window.status.set_status(message, announce=True)

    def on_rename_recording(
        self,
        identifier: str,
        new_name: str,
        completed: Callable[[str | None], None] | None = None,
    ) -> None:
        self._require_writable_instance()
        try:
            if self._library_mutation_busy:
                raise RuntimeError(self._translator()("error_library_busy"))
            self._require_player_media_stable((identifier,))
            item = self._require_item(identifier)
        except Exception as error:
            self._complete_library_message(
                self._rename_error_message(error), completed
            )
            return
        self._set_library_busy(True)

        def finished(
            renamed: Path | None,
            error: BaseException | None,
        ) -> None:
            self._set_library_busy(False)
            if error is not None or renamed is None:
                failure = error or RuntimeError(
                    self._translator()("error_rename_no_result")
                )
                self._complete_library_message(
                    self._rename_error_message(failure), completed
                )
                return
            updated = replace(item, path=renamed, name=renamed.name)
            self.recording_items.pop(identifier, None)
            self.recording_items[updated.id] = updated
            player_was_renamed = self.player_identifier == identifier
            if player_was_renamed:
                # Linux keeps the already-open inode valid across rename.  Do
                # not reset playbin's URI: doing so would discard phase,
                # position and speed and could unexpectedly alter playback.
                self.player_identifier = updated.id
            if completed is not None:
                completed(None)
            if self.window is not None:
                updater = getattr(self.window, "update_recording", None)
                if updater is not None:
                    updater(
                        identifier,
                        _recording_view(updated),
                        focus_recordings=not player_was_renamed,
                    )
                announcer = getattr(self.window, "announce_library_status", None)
                message = self._translator()(
                    "status_recording_renamed", name=updated.name
                )
                if announcer is not None:
                    announcer(message)
                else:
                    self.window.status.set_status(message, announce=True)
            self.on_refresh_recordings()

        self._submit_storage(
            "library-mutation",
            lambda: self.storage.rename_recording(item, new_name),
            finished,
        )

    def on_delete_recordings(
        self,
        identifiers: tuple[str, ...],
        completed: Callable[[str | None], None] | None = None,
    ) -> None:
        self._require_writable_instance()
        try:
            if self._library_mutation_busy:
                raise RuntimeError(self._translator()("error_library_busy"))
            unique = tuple(dict.fromkeys(identifiers))
            self._require_player_media_stable(unique)
            if len(unique) > MAX_SELECTED_RECORDINGS:
                raise ValueError(
                    self._translator()(
                        "error_delete_limit", count=MAX_SELECTED_RECORDINGS
                    )
                )
            items = tuple(self._require_item(identifier) for identifier in unique)
        except Exception as error:
            self._library_error("error_delete", error, completed)
            return
        close_player = self.player_identifier in unique
        self._set_library_busy(True)

        def finished(
            result: DeleteResult | None,
            error: BaseException | None,
        ) -> None:
            self._set_library_busy(False)
            if error is not None or result is None:
                failure = error or RuntimeError(
                    self._translator()("error_delete_no_result")
                )
                self._library_error("error_delete", failure, completed)
                return
            deleted_paths = frozenset(result.deleted_paths)
            player_item = (
                self.recording_items.get(self.player_identifier)
                if self.player_identifier is not None
                else None
            )
            player_path = (
                player_item.path
                if player_item is not None
                else Path(self.player_identifier)
                if self.player_identifier is not None
                else None
            )
            if (
                close_player
                and player_path is not None
                and player_path in deleted_paths
            ):
                if self.window is not None:
                    closer = getattr(
                        self.window, "close_player_after_delete", None
                    )
                    if closer is not None:
                        closer(self.player_identifier)
                    else:
                        self.window.close_player()
                self._dispose_player()
            self.recording_items = {
                item_id: current
                for item_id, current in self.recording_items.items()
                if current.path not in deleted_paths
            }
            self._measure_remaining()
            if self.recorder is not None:
                self._render_recorder(announce=False)
            elif self._startup_recovery_complete:
                self._render_idle(announce=False)

            if result.skipped_paths:
                message = self._translator()(
                    "status_delete_partial",
                    deleted=result.deleted_count,
                    skipped=len(result.skipped_paths),
                )
            elif result.deleted_count == 1:
                message = self._translator()("status_delete_one")
            else:
                message = self._translator()(
                    "status_delete_many", count=result.deleted_count
                )
            if completed is not None:
                completed(None)
            if self.window is not None:
                announcer = getattr(self.window, "announce_library_status", None)
                if announcer is not None:
                    announcer(message)
                else:
                    self.window.status.set_status(message, announce=True)
            self.on_refresh_recordings()

        self._submit_storage(
            "library-mutation",
            lambda: self.storage.delete_recordings(items),
            finished,
        )

    # -- PresentationCallbacks: player -----------------------------------------

    def on_open_player(self, identifier: str) -> None:
        item = self._require_item(identifier)
        self._dispose_player()
        try:
            player = self.player_factory(self._on_playback_event)
        except Exception as error:
            if self.window is not None:
                self.window.player_error(str(error))
            return
        self.player = player
        self.player_identifier = identifier
        self._ensure_tick()
        if not player.open(item.path) and self.window is not None:
            self.window.player_error(
                player.snapshot.error
                or self._translator()("error_playback_start")
            )

    def on_player_play(self, identifier: str) -> None:
        if identifier == self.player_identifier and self.player is not None:
            self.player.play()

    def on_player_pause(self, identifier: str) -> None:
        if identifier == self.player_identifier and self.player is not None:
            self.player.pause()

    def on_player_seek(self, identifier: str, position_seconds: float) -> None:
        if identifier == self.player_identifier and self.player is not None:
            self.player.seek_to(position_seconds)

    def on_player_seek_by(self, identifier: str, delta_seconds: float) -> None:
        if identifier == self.player_identifier and self.player is not None:
            self.player.seek_by(delta_seconds)

    def on_player_speed(self, identifier: str, speed: float) -> None:
        if identifier == self.player_identifier and self.player is not None:
            self.player.set_speed(speed)

    def on_player_closed(self, identifier: str) -> None:
        if identifier == self.player_identifier:
            self._dispose_player()

    def _on_playback_event(self, event: PlaybackEvent) -> None:
        if self.player is None or self.window is None or self._shutting_down:
            return
        snapshot = event.snapshot
        detail = (
            event.detail
            if event.type in {PlaybackEventType.ERROR, PlaybackEventType.SPEED_ERROR}
            else snapshot.error
        )
        self.window.set_player_view(
            PlayerView(
                phase=snapshot.phase,
                position_seconds=snapshot.position_seconds,
                duration_seconds=snapshot.duration_seconds,
                speed=snapshot.speed,
                error=detail,
            )
        )

    def _dispose_player(self) -> None:
        player = self.player
        self.player = None
        self.player_identifier = None
        if player is not None:
            player.set_event_callback(lambda _event: None)
            player.close()

    # -- Desktop session integration -------------------------------------------

    def _hold_recording(self) -> None:
        if not self._recording_held:
            self.hold()
            self._recording_held = True

    def _release_recording_hold(self) -> None:
        if self._recording_held:
            self._recording_held = False
            self.release()

    def _require_writable_instance(self) -> None:
        if self._process_lock is None:
            raise StorageProcessLockError(
                self._instance_lock_error
                or self._translator()("instance_busy_detail")
            )

    def _sync_inhibit(self) -> None:
        should_inhibit = (
            self.settings.prevent_sleep
            and self.window is not None
            and self.pending is not None
            and (self.recorder is not None or self._recording_storage_busy)
        )
        if should_inhibit and not self._inhibit_cookie:
            flags = (
                Gtk.ApplicationInhibitFlags.IDLE
                | Gtk.ApplicationInhibitFlags.SUSPEND
            )
            self._inhibit_cookie = self.inhibit(
                self.window,
                flags,
                "MiniRec is recording audio",
            )
        elif not should_inhibit:
            self._release_inhibit()

    def _release_inhibit(self) -> None:
        if self._inhibit_cookie:
            cookie = self._inhibit_cookie
            self._inhibit_cookie = 0
            self.uninhibit(cookie)

    def _notification_toggle(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        if self.recorder is None:
            return
        if self.recorder.phase is RecordingPhase.RECORDING:
            self.on_pause()
        elif self.recorder.phase is RecordingPhase.PAUSED:
            self.on_resume()

    def _notification_present(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        if self.window is not None:
            self.window.present()
        else:
            self.activate()

    def _sync_recording_actions(self) -> None:
        phase = self.recorder.phase if self.recorder is not None else None
        if self._toggle_recording_action is not None:
            self._toggle_recording_action.set_enabled(
                phase in {RecordingPhase.RECORDING, RecordingPhase.PAUSED}
            )
        if self._stop_recording_action is not None:
            self._stop_recording_action.set_enabled(phase in _ACTIVE_PHASES)

    def _sync_recording_notification(self) -> None:
        recorder = self.recorder
        if recorder is None or recorder.phase not in _ACTIVE_PHASES:
            self._withdraw_recording_notification()
            return
        translator = self._translator()
        phase = recorder.phase
        status_key = {
            RecordingPhase.STARTING: "status_starting",
            RecordingPhase.RECORDING: "status_recording",
            RecordingPhase.PAUSING: "status_pausing",
            RecordingPhase.PAUSED: "status_paused",
            RecordingPhase.RESUMING: "status_resuming",
            RecordingPhase.STOPPING: "status_stopping",
        }[phase]
        notification = Gio.Notification.new(translator("app_name"))
        notification.set_body(translator(status_key))
        notification.set_priority(Gio.NotificationPriority.HIGH)
        notification.set_default_action("app.present")
        if phase is RecordingPhase.RECORDING:
            notification.add_button(
                translator("action_pause"), "app.recording-toggle"
            )
        elif phase is RecordingPhase.PAUSED:
            notification.add_button(
                translator("action_resume"), "app.recording-toggle"
            )
        notification.add_button(
            translator("action_stop"), "app.recording-stop"
        )
        try:
            self.send_notification(RECORDING_NOTIFICATION_ID, notification)
        except GLib.Error:
            # Notifications are optional desktop integration; recording must
            # remain functional in minimal sessions and smoke tests.
            pass

    def _withdraw_recording_notification(self) -> None:
        try:
            self.withdraw_notification(RECORDING_NOTIFICATION_ID)
        except GLib.Error:
            pass


class InstanceBusyWindow(Adw.ApplicationWindow):
    """Accessible read-only response when another process owns recovery state."""

    def __init__(
        self,
        application: Adw.Application,
        translator: Translator,
        detail: str,
        *,
        fatal: bool = False,
    ) -> None:
        super().__init__(application=application)
        title = translator(
            "instance_fatal_title" if fatal else "instance_busy_title"
        )
        generic_detail = translator(
            "instance_fatal_detail" if fatal else "instance_busy_detail"
        )
        self.set_title(title)
        self.set_default_size(620, 320)
        body = content_box()
        body.append(heading(title))
        body.append(
            description(
                f"{generic_detail}\n\n{detail}",
                readable=True,
            )
        )
        close = wrapping_button(translator("close"))
        close.connect("clicked", lambda _button: self.close())
        body.append(close)
        self.set_content(scrolled_content(body))


__all__ = [
    "APPLICATION_ID",
    "InstanceBusyWindow",
    "MiniRecApplication",
    "STOP_WATCHDOG_SECONDS",
    "SUPPORT_URI",
    "data_file",
    "preserve_invalid_settings",
]
