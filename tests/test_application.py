from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from minirec.application import MiniRecApplication, STOP_WATCHDOG_SECONDS
from minirec.playback import PlaybackPhase
from minirec.recording import (
    RecordingEvent,
    RecordingEventType,
    RecordingPhase,
)
from minirec.settings import AppSettings, SettingsStore
from minirec.storage import (
    DeleteResult,
    RecordingStorage,
    StorageIdentityError,
    StorageProcessLock,
    StorageProcessLockError,
)


class ImmediateExecutor:
    """Executor test double that preserves the production Future contract."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def submit(self, callback, *args, **kwargs):
        future = Future()
        try:
            future.set_result(callback(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        self.shutdown_calls += 1


def dispatch_immediately(callback) -> None:
    callback()


class ManualExecutor:
    def __init__(self) -> None:
        self.jobs = []

    def submit(self, callback, *args, **kwargs):
        future = Future()
        self.jobs.append((future, callback, args, kwargs))
        return future

    def run_next(self) -> None:
        future, callback, args, kwargs = self.jobs.pop(0)
        try:
            future.set_result(callback(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        return None


class ManualDispatcher:
    def __init__(self) -> None:
        self.callbacks = []

    def __call__(self, callback) -> None:
        self.callbacks.append(callback)

    def flush(self) -> None:
        while self.callbacks:
            self.callbacks.pop(0)()


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStatus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    def set_status(self, text: str, *, announce: bool = True) -> None:
        self.messages.append((text, announce))


class FakeNotification:
    def __init__(self, title: str) -> None:
        self.title = title
        self.body = ""
        self.priority = None
        self.default_action = None
        self.buttons = []

    def set_body(self, body: str) -> None:
        self.body = body

    def set_priority(self, priority) -> None:
        self.priority = priority

    def set_default_action(self, action: str) -> None:
        self.default_action = action

    def add_button(self, label: str, action: str) -> None:
        self.buttons.append((label, action))


class FakeWindow:
    def __init__(self) -> None:
        self.status = FakeStatus()
        self.recorder_views = []
        self.recordings = ()
        self.recordings_error = None
        self.closed_players = 0
        self.quit_completed = 0
        self.applied_settings = []
        self.player_views = []
        self.library_busy = []
        self.updated_recordings = []
        self.library_messages = []
        self.deleted_player_ids = []
        self.present_calls = 0

    def set_recorder_view(self, view, *, announce: bool = True) -> None:
        self.recorder_views.append((view, announce))

    def set_recordings(self, recordings, *, error=None) -> None:
        self.recordings = tuple(recordings)
        self.recordings_error = error

    def close_player(self) -> None:
        self.closed_players += 1

    def complete_stop_and_quit(self) -> None:
        self.quit_completed += 1

    def apply_settings(self, settings, *, translator=None) -> None:
        self.applied_settings.append((settings, translator))

    def set_player_view(self, view) -> None:
        self.player_views.append(view)

    def player_error(self, message: str) -> None:
        self.player_views.append(message)

    def set_library_busy(self, busy: bool) -> None:
        self.library_busy.append(busy)

    def update_recording(
        self,
        old_identifier,
        recording,
        *,
        focus_recordings=True,
    ) -> None:
        self.updated_recordings.append(
            (old_identifier, recording, focus_recordings)
        )

    def announce_library_status(self, message: str) -> None:
        self.library_messages.append(message)

    def close_player_after_delete(self, identifier) -> None:
        self.deleted_player_ids.append(identifier)
        self.closed_players += 1

    def present(self) -> None:
        self.present_calls += 1


class FakeRecorder:
    def __init__(self, settings, callback, *, journal_assertion=None) -> None:
        self.settings = settings
        self.callback = callback
        self.phase = RecordingPhase.IDLE
        self.output_path: Path | None = None
        self.elapsed_seconds = 0.0
        self.error = None
        self.closed = False
        self.timeout_calls = 0
        self.journal_assertion = journal_assertion
        self.fail_stop_synchronously = False

    @property
    def snapshot(self):
        return SimpleNamespace(
            phase=self.phase,
            output_path=self.output_path,
            elapsed_seconds=self.elapsed_seconds,
            requested_channels=self.settings.channels,
            active_channels=self.settings.channels,
            source_factory="pulsesrc",
            error=self.error,
        )

    def set_event_callback(self, callback) -> None:
        self.callback = callback

    def _emit(self, kind, detail=None) -> None:
        self.callback(RecordingEvent(kind, self.snapshot, detail))

    def start(self, output_path, *, prepared=False) -> bool:
        self.output_path = Path(output_path)
        assert prepared
        assert self.output_path.is_file()
        assert self.output_path.stat().st_size == 0
        if self.journal_assertion is not None:
            self.journal_assertion()
        self.phase = RecordingPhase.STARTING
        self._emit(RecordingEventType.STATE_CHANGED)
        self.phase = RecordingPhase.RECORDING
        self._emit(RecordingEventType.STATE_CHANGED)
        return True

    def pause(self) -> bool:
        if self.phase is not RecordingPhase.RECORDING:
            return False
        self.phase = RecordingPhase.PAUSED
        self._emit(RecordingEventType.STATE_CHANGED)
        return True

    def resume(self) -> bool:
        if self.phase is not RecordingPhase.PAUSED:
            return False
        self.phase = RecordingPhase.RECORDING
        self._emit(RecordingEventType.STATE_CHANGED)
        return True

    def stop(self) -> bool:
        if self.fail_stop_synchronously:
            self.error = "synchronous stop failure"
            self.phase = RecordingPhase.ERROR
            self._emit(RecordingEventType.STATE_CHANGED)
            self._emit(RecordingEventType.ERROR, self.error)
            return False
        if self.phase is RecordingPhase.STOPPING:
            return True
        if self.phase not in {
            RecordingPhase.STARTING,
            RecordingPhase.RECORDING,
            RecordingPhase.PAUSED,
        }:
            return False
        self.phase = RecordingPhase.STOPPING
        self._emit(RecordingEventType.STATE_CHANGED)
        return True

    def finalize(self, payload: bytes = b"finalized audio") -> None:
        assert self.output_path is not None
        self.output_path.write_bytes(payload)
        self.elapsed_seconds = 3.5
        self.phase = RecordingPhase.STOPPED
        self._emit(RecordingEventType.STATE_CHANGED)
        self._emit(RecordingEventType.FINALIZED)

    def timeout_stalled_stop(self) -> bool:
        if self.phase is not RecordingPhase.STOPPING:
            return False
        self.timeout_calls += 1
        assert self.output_path is not None
        self.output_path.write_bytes(b"unverified interrupted bytes")
        self.error = "timed out while waiting for recording finalization"
        self.phase = RecordingPhase.ERROR
        self._emit(RecordingEventType.STATE_CHANGED)
        self._emit(RecordingEventType.ERROR, self.error)
        return True

    def close(self) -> None:
        self.closed = True
        self.phase = RecordingPhase.CLOSED

    def emergency_close_for_process_exit(self) -> None:
        self.close()


class FakePlayer:
    def __init__(self) -> None:
        self.closed = False
        self.position_seconds = 37.0
        self.speed = 1.25
        self.phase = PlaybackPhase.PAUSED

    def set_event_callback(self, _callback) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class HarnessApplication(MiniRecApplication):
    sequence = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).sequence += 1
        super().__init__(
            *args,
            application_id=f"cz.pvlcek.minirec.Unit{self.sequence}",
            non_unique=True,
            **kwargs,
        )
        self.holds = 0
        self.sent_notifications = []
        self.withdrawn_notifications = []
        self.inhibit_calls = 0
        self.uninhibit_calls = 0

    def hold(self) -> None:
        self.holds += 1

    def release(self) -> None:
        self.holds -= 1

    def send_notification(self, identifier, notification) -> None:
        self.sent_notifications.append((identifier, notification))

    def withdraw_notification(self, identifier) -> None:
        self.withdrawn_notifications.append(identifier)

    def inhibit(self, _window, _flags, _reason) -> int:
        self.inhibit_calls += 1
        return self.inhibit_calls

    def uninhibit(self, _cookie) -> None:
        self.uninhibit_calls += 1


class ApplicationRecordingIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.storage = RecordingStorage(root / "recordings", root / "state")
        self.settings_store = SettingsStore(root / "config" / "settings.json")
        self.clock = FakeClock()
        self.created_recorders: list[FakeRecorder] = []

        def make_recorder(settings, callback):
            recorder = FakeRecorder(
                settings,
                callback,
                journal_assertion=lambda: self.assertEqual(
                    1, len(list(self.storage.state_dir.glob("recording-*.json")))
                ),
            )
            self.created_recorders.append(recorder)
            return recorder

        self.app = HarnessApplication(
            settings_store=self.settings_store,
            storage=self.storage,
            recorder_factory=make_recorder,
            clock=self.clock,
            storage_executor=ImmediateExecutor(),
            dispatcher=dispatch_immediately,
        )
        self.window = FakeWindow()
        self.app.window = self.window
        self.app._initialize_once()
        self.app.settings = AppSettings()

    def test_journal_precedes_capture_and_finalize_publishes_without_pending(self) -> None:
        self.app.on_record()
        recorder = self.created_recorders[-1]
        pending = self.app.pending
        assert pending is not None
        self.assertEqual(1, self.app.holds)
        self.assertTrue(pending.path.name.startswith(".minirec-"))
        self.assertTrue(pending.journal_path.is_file())
        self.assertEqual(RecordingPhase.RECORDING, recorder.phase)

        self.app.on_stop()
        recorder.finalize()

        self.assertEqual(0, self.app.holds)
        self.assertIsNone(self.app.pending)
        self.assertIsNone(self.app.recorder)
        self.assertFalse(pending.path.exists())
        self.assertFalse(pending.journal_path.exists())
        published = list(self.storage.recordings_dir.glob("*.oga"))
        self.assertEqual(1, len(published))
        self.assertEqual(b"finalized audio", published[0].read_bytes())
        self.assertEqual("stopped", self.window.recorder_views[-1][0].phase)

    def test_stop_watchdog_leaves_unverifiable_pending_and_releases_session(self) -> None:
        self.app.on_record()
        recorder = self.created_recorders[-1]
        pending = self.app.pending
        assert pending is not None
        self.app.on_stop()

        self.clock.advance(STOP_WATCHDOG_SECONDS + 0.1)
        self.assertFalse(self.app._tick())

        self.assertEqual(1, recorder.timeout_calls)
        self.assertEqual(0, self.app.holds)
        self.assertIsNone(self.app.recorder)
        self.assertTrue(pending.path.exists())
        self.assertTrue(pending.journal_path.exists())
        self.assertEqual("error", self.window.recorder_views[-1][0].phase)

    def test_stop_and_quit_waits_for_successful_publication(self) -> None:
        self.app.on_record()
        recorder = self.created_recorders[-1]
        self.app.on_stop_and_quit()
        self.assertEqual(0, self.window.quit_completed)
        recorder.finalize()
        self.assertEqual(1, self.window.quit_completed)

    def test_settings_save_updates_the_live_translator(self) -> None:
        changed = AppSettings().with_changes(language="cs", prevent_sleep=True)
        self.app.on_settings_changed(changed)
        self.assertEqual("cs", self.app.settings.language.value)
        self.assertEqual(1, len(self.window.applied_settings))
        self.assertEqual(
            "cs", self.window.applied_settings[-1][1].resolved_language
        )

    def test_bitrate_save_does_not_retranslate_an_unchanged_language(self) -> None:
        changed = AppSettings().with_changes(bitrate_kbps=320)

        self.app.on_settings_changed(changed)

        self.assertEqual(320, self.app.settings.recording.bitrate_kbps)
        self.assertEqual(1, len(self.window.applied_settings))
        applied, translator = self.window.applied_settings[-1]
        self.assertEqual(320, applied.recording.bitrate_kbps)
        self.assertIsNone(translator)
        self.assertEqual(320, self.settings_store.load().recording.bitrate_kbps)

    def test_synchronous_stop_error_is_reentrant_safe(self) -> None:
        self.app.on_record()
        recorder = self.created_recorders[-1]
        recorder.fail_stop_synchronously = True

        self.app.on_stop()

        self.assertIsNone(self.app.recorder)
        self.assertIsNone(self.app.pending)
        self.assertEqual(0, self.app.holds)
        self.assertEqual("error", self.window.recorder_views[-1][0].phase)

    def test_backend_fallback_is_announced_with_localized_policy_text(self) -> None:
        self.app.settings = AppSettings().with_changes(language="cs")
        self.app.on_record()
        recorder = self.created_recorders[-1]

        recorder._emit(
            RecordingEventType.SOURCE_FALLBACK,
            "using autoaudiosrc after pulsesrc",
        )

        message, announced = self.window.status.messages[-1]
        self.assertTrue(announced)
        self.assertIn("náhradní zdroj", message)
        self.assertIn("autoaudiosrc", message)
        self.app.on_stop()
        recorder.finalize()

    def test_notification_opens_main_window_without_routine_tick_resends(self) -> None:
        recorder = FakeRecorder(
            self.app.settings.recording,
            lambda _event: None,
        )
        recorder.phase = RecordingPhase.RECORDING
        recorder.elapsed_seconds = 65.0
        self.app.recorder = recorder
        self.app._disk_check_due = float("inf")
        created: list[FakeNotification] = []

        def make_notification(title: str) -> FakeNotification:
            notification = FakeNotification(title)
            created.append(notification)
            return notification

        with patch(
            "minirec.application.Gio.Notification.new",
            side_effect=make_notification,
        ):
            self.app._sync_recording_notification()
            sent_before_tick = len(self.app.sent_notifications)
            self.clock.advance(1.0)
            recorder.elapsed_seconds = 66.0
            self.assertTrue(self.app._tick())

        self.assertEqual(1, len(created))
        self.assertEqual("app.present", created[0].default_action)
        self.assertEqual(sent_before_tick, len(self.app.sent_notifications))
        self.assertFalse(self.window.recorder_views[-1][1])

        self.app._notification_present(None, None)
        self.assertEqual(1, self.window.present_calls)


class ApplicationAsyncStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.storage = RecordingStorage(root / "recordings", root / "state")
        self.executor = ManualExecutor()
        self.dispatcher = ManualDispatcher()
        self.created_recorders: list[FakeRecorder] = []

        def make_recorder(settings, callback):
            recorder = FakeRecorder(settings, callback)
            self.created_recorders.append(recorder)
            return recorder

        self.app = HarnessApplication(
            settings_store=SettingsStore(root / "config" / "settings.json"),
            storage=self.storage,
            recorder_factory=make_recorder,
            storage_executor=self.executor,
            dispatcher=self.dispatcher,
        )
        self.window = FakeWindow()
        self.app.window = self.window
        self.app._initialize_once()

    def _finish_startup(self) -> None:
        self.assertEqual("recovering", self.window.recorder_views[-1][0].phase)
        self.executor.run_next()
        self.dispatcher.flush()
        # Startup completion requests the first library listing.
        self.executor.run_next()
        self.dispatcher.flush()

    def _start_recording(self) -> FakeRecorder:
        self.app.on_record()
        self.assertEqual("starting", self.window.recorder_views[-1][0].phase)
        self.assertEqual(1, self.app.holds)
        self.assertEqual([], self.created_recorders)
        self.executor.run_next()
        # Creating and fsyncing the pending file is not enough to touch GTK or
        # instantiate GStreamer until the dispatcher returns to main context.
        self.assertEqual([], self.created_recorders)
        self.assertIsNone(self.app.pending)
        self.dispatcher.flush()
        return self.created_recorders[-1]

    def test_settings_change_does_not_hide_startup_recovery_busy_state(self) -> None:
        self.app.on_settings_changed(AppSettings().with_changes(language="cs"))
        self.assertEqual("recovering", self.window.recorder_views[-1][0].phase)

    def test_deferred_settings_coalesce_without_losing_language_change(self) -> None:
        czech = AppSettings().with_changes(language="cs")
        final = czech.with_changes(bitrate_kbps=320)

        self.app.on_settings_changed(czech)
        self.app.on_settings_changed(final)

        self.assertEqual([], self.window.applied_settings)
        self.dispatcher.flush()
        self.assertEqual(1, len(self.window.applied_settings))
        applied, translator = self.window.applied_settings[0]
        self.assertEqual(320, applied.recording.bitrate_kbps)
        self.assertIsNotNone(translator)
        self.assertEqual("cs", translator.resolved_language)

    def test_publish_retains_identity_hold_inhibit_and_finalizing_until_dispatch(self) -> None:
        self._finish_startup()
        self.app.settings = AppSettings(prevent_sleep=True)
        recorder = self._start_recording()
        pending = self.app.pending
        self.assertIsNotNone(pending)
        self.assertEqual(1, self.app.holds)
        self.assertEqual(1, self.app.inhibit_calls)

        self.app.on_stop()
        recorder.finalize()

        self.assertEqual("finalizing", self.window.recorder_views[-1][0].phase)
        self.assertIs(self.app.pending, pending)
        self.assertEqual(1, self.app.holds)
        self.assertEqual(0, self.app.uninhibit_calls)

        self.executor.run_next()
        # The worker committed, but GTK has not consumed the result yet.
        self.assertIs(self.app.pending, pending)
        self.assertEqual(1, self.app.holds)
        self.assertEqual("finalizing", self.window.recorder_views[-1][0].phase)

        self.dispatcher.flush()
        self.assertIsNone(self.app.pending)
        self.assertEqual(0, self.app.holds)
        self.assertEqual(1, self.app.uninhibit_calls)
        self.assertEqual("stopped", self.window.recorder_views[-1][0].phase)

    def test_close_during_async_prepare_aborts_before_capture_then_quits(self) -> None:
        self._finish_startup()
        self.app.on_record()
        self.app.on_stop_and_quit()
        self.assertEqual(0, self.window.quit_completed)
        self.assertEqual([], self.created_recorders)

        self.executor.run_next()
        self.assertIsNone(self.app.pending)
        self.dispatcher.flush()
        self.assertEqual("finalizing", self.window.recorder_views[-1][0].phase)
        self.assertIsNotNone(self.app.pending)
        self.assertEqual([], self.created_recorders)
        self.assertEqual(1, self.app.holds)

        self.executor.run_next()
        self.assertIsNotNone(self.app.pending)
        self.dispatcher.flush()
        self.assertIsNone(self.app.pending)
        self.assertEqual(0, self.app.holds)
        self.assertEqual(1, self.window.quit_completed)
        self.assertEqual([], list(self.storage.state_dir.glob("recording-*.json")))

    def test_older_list_generation_cannot_replace_newer_result(self) -> None:
        self._finish_startup()
        self.app.on_refresh_recordings()
        self.app.on_refresh_recordings()

        self.executor.run_next()
        recording = self.storage.recordings_dir / "new.oga"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"not a complete ogg stream")
        self.executor.run_next()
        self.dispatcher.flush()

        self.assertEqual(("new.oga",), tuple(item.name for item in self.window.recordings))
        view = self.window.recordings[0]
        source = self.app.recording_items[view.identifier]
        self.assertEqual(source.modified_ns, view.modified_ns)
        self.assertIs(source.format, view.format)

    def test_library_error_retains_date_and_format_in_existing_views(self) -> None:
        self._finish_startup()
        recording = self.storage.recordings_dir / "voice.mp3"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"voice")
        item = self.storage.list_recordings()[0]
        self.app.recording_items = {item.id: item}

        self.app._library_error("error_delete", OSError("read-only"), None)

        self.assertIsNotNone(self.window.recordings_error)
        self.assertEqual(1, len(self.window.recordings))
        view = self.window.recordings[0]
        self.assertEqual(item.modified_ns, view.modified_ns)
        self.assertIs(item.format, view.format)

    def test_failed_rename_keeps_player_open_and_reports_to_completion(self) -> None:
        self._finish_startup()
        recording = self.storage.recordings_dir / "voice.oga"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"voice")
        item = self.storage.list_recordings()[0]
        self.app.recording_items = {item.id: item}
        player = FakePlayer()
        self.app.player = player
        self.app.player_identifier = item.id
        messages: list[str | None] = []

        with patch.object(
            self.storage,
            "rename_recording",
            side_effect=OSError("read-only filesystem"),
        ):
            self.app.on_rename_recording(item.id, "new name", messages.append)
            self.executor.run_next()
            self.dispatcher.flush()

        self.assertIs(self.app.player, player)
        self.assertFalse(player.closed)
        self.assertEqual(0, self.window.closed_players)
        self.assertIn("could not be renamed", messages[0].casefold())

    def test_preparing_player_blocks_mutations_from_every_window(self) -> None:
        self._finish_startup()
        recording = self.storage.recordings_dir / "voice.oga"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"voice")
        item = self.storage.list_recordings()[0]
        self.app.recording_items = {item.id: item}
        player = FakePlayer()
        player.phase = PlaybackPhase.PREPARING
        self.app.player = player
        self.app.player_identifier = item.id
        rename_messages: list[str | None] = []
        delete_messages: list[str | None] = []

        with patch.object(
            self.storage,
            "rename_recording",
            side_effect=AssertionError("preparing rename reached storage"),
        ), patch.object(
            self.storage,
            "delete_recordings",
            side_effect=AssertionError("preparing delete reached storage"),
        ):
            self.app.on_rename_recording(
                item.id, "renamed", rename_messages.append
            )
            self.app.on_delete_recordings((item.id,), delete_messages.append)

        self.assertEqual(PlaybackPhase.PREPARING, player.phase)
        self.assertFalse(player.closed)
        self.assertIn("finish loading", rename_messages[-1])
        self.assertIn("finish loading", delete_messages[-1])

    def test_successful_player_rename_preserves_backend_and_remaps_identity(self) -> None:
        self._finish_startup()
        recording = self.storage.recordings_dir / "voice.oga"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"voice")
        item = self.storage.list_recordings()[0]
        self.app.recording_items = {item.id: item}
        player = FakePlayer()
        self.app.player = player
        self.app.player_identifier = item.id
        messages: list[str | None] = []

        self.app.on_rename_recording(item.id, "renamed", messages.append)
        self.executor.run_next()
        self.assertEqual(item.id, self.app.player_identifier)
        self.dispatcher.flush()

        renamed = self.storage.recordings_dir / "renamed.oga"
        renamed_id = str(renamed)
        self.assertIs(self.app.player, player)
        self.assertFalse(player.closed)
        self.assertEqual(PlaybackPhase.PAUSED, player.phase)
        self.assertEqual(37.0, player.position_seconds)
        self.assertEqual(1.25, player.speed)
        self.assertEqual(renamed_id, self.app.player_identifier)
        self.assertNotIn(item.id, self.app.recording_items)
        self.assertEqual(item.identity, self.app.recording_items[renamed_id].identity)
        self.assertEqual([None], messages)
        self.assertEqual(0, self.window.closed_players)
        old_identifier, view, focus_recordings = self.window.updated_recordings[-1]
        self.assertEqual(item.id, old_identifier)
        self.assertEqual(renamed_id, view.identifier)
        self.assertEqual("renamed.oga", view.name)
        self.assertEqual(item.modified_ns, view.modified_ns)
        self.assertIs(item.format, view.format)
        self.assertFalse(focus_recordings)
        self.assertIn("renamed.oga", self.window.library_messages[-1])

    def test_rename_validation_errors_are_localized_by_failure_kind(self) -> None:
        self._finish_startup()
        first_path = self.storage.recordings_dir / "voice.oga"
        conflict_path = self.storage.recordings_dir / "existing.oga"
        first_path.parent.mkdir(parents=True, exist_ok=True)
        first_path.write_bytes(b"voice")
        conflict_path.write_bytes(b"existing")
        items = self.storage.list_recordings()
        item = next(current for current in items if current.name == "voice.oga")
        self.app.recording_items = {current.id: current for current in items}

        invalid_messages: list[str | None] = []
        self.app.on_rename_recording(
            item.id,
            "bad/name",
            invalid_messages.append,
        )
        self.executor.run_next()
        self.dispatcher.flush()
        self.assertIn("invalid character", invalid_messages[-1])

        conflict_messages: list[str | None] = []
        self.app.on_rename_recording(
            item.id,
            "existing",
            conflict_messages.append,
        )
        self.executor.run_next()
        self.dispatcher.flush()
        self.assertIn("already exists", conflict_messages[-1])

    def test_successful_delete_closes_player_only_after_worker_result(self) -> None:
        self._finish_startup()
        recording = self.storage.recordings_dir / "voice.oga"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"voice")
        item = self.storage.list_recordings()[0]
        self.app.recording_items = {item.id: item}
        player = FakePlayer()
        self.app.player = player
        self.app.player_identifier = item.id
        messages: list[str | None] = []

        with patch.object(self.storage, "remaining_seconds", return_value=987):
            self.app.on_delete_recordings((item.id,), messages.append)
            self.assertIs(self.app.player, player)
            self.executor.run_next()
            self.assertIs(self.app.player, player)
            self.dispatcher.flush()

        self.assertIsNone(self.app.player)
        self.assertTrue(player.closed)
        self.assertEqual([None], messages)
        self.assertEqual([item.id], self.window.deleted_player_ids)
        self.assertEqual(987.0, self.app._remaining_base)
        self.assertIn("deleted", self.window.library_messages[-1].casefold())

    def test_partial_delete_reports_skips_and_keeps_skipped_player_open(self) -> None:
        self._finish_startup()
        first_path = self.storage.recordings_dir / "first.oga"
        second_path = self.storage.recordings_dir / "second.oga"
        first_path.parent.mkdir(parents=True, exist_ok=True)
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        items = self.storage.list_recordings()
        by_name = {item.name: item for item in items}
        first = by_name["first.oga"]
        second = by_name["second.oga"]
        self.app.recording_items = {item.id: item for item in items}
        player = FakePlayer()
        self.app.player = player
        self.app.player_identifier = second.id
        messages: list[str | None] = []

        partial = DeleteResult(
            requested_count=2,
            deleted_paths=(first.path,),
            skipped_paths=(second.path,),
        )
        with patch.object(
            self.storage,
            "delete_recordings",
            return_value=partial,
        ):
            self.app.on_delete_recordings(
                (first.id, second.id), messages.append
            )
            self.executor.run_next()
            self.dispatcher.flush()

        self.assertIs(self.app.player, player)
        self.assertFalse(player.closed)
        self.assertEqual([None], messages)
        self.assertIn("left untouched", self.window.library_messages[-1])

    def test_delete_identity_failure_is_localized_without_internal_detail(self) -> None:
        self._finish_startup()
        recording = self.storage.recordings_dir / "voice.oga"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_bytes(b"voice")
        item = self.storage.list_recordings()[0]
        self.app.recording_items = {item.id: item}
        messages: list[str | None] = []

        with patch.object(
            self.storage,
            "delete_recordings",
            side_effect=StorageIdentityError("internal inode detail"),
        ):
            self.app.on_delete_recordings((item.id,), messages.append)
            self.executor.run_next()
            self.dispatcher.flush()

        self.assertIn("changed", messages[-1])
        self.assertNotIn("inode", messages[-1])

    def test_batch_delete_has_localized_success_announcement(self) -> None:
        self._finish_startup()
        first_path = self.storage.recordings_dir / "first.oga"
        second_path = self.storage.recordings_dir / "second.oga"
        first_path.parent.mkdir(parents=True, exist_ok=True)
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        items = self.storage.list_recordings()
        self.app.recording_items = {item.id: item for item in items}
        self.app.settings = AppSettings().with_changes(language="cs")
        messages: list[str | None] = []

        self.app.on_delete_recordings(
            tuple(item.id for item in items),
            messages.append,
        )
        self.executor.run_next()
        self.dispatcher.flush()

        self.assertEqual([None], messages)
        self.assertEqual({}, self.app.recording_items)
        self.assertEqual(
            "Smazané nahrávky: 2",
            self.window.library_messages[-1],
        )


class BlockingStartupStorage(RecordingStorage):
    def __init__(self, *args, entered: threading.Event, release: threading.Event):
        super().__init__(*args)
        self.entered = entered
        self.release = release

    def recover_startup(self):
        self.entered.set()
        if not self.release.wait(5.0):
            raise TimeoutError("test did not release startup recovery")
        return super().recover_startup()


class ApplicationLifecycleTest(unittest.TestCase):
    def test_shutdown_waits_for_startup_worker_before_process_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered = threading.Event()
            release = threading.Event()
            dispatcher = ManualDispatcher()
            storage = BlockingStartupStorage(
                root / "recordings",
                root / "state",
                entered=entered,
                release=release,
            )
            app = HarnessApplication(
                settings_store=SettingsStore(root / "config" / "settings.json"),
                storage=storage,
                dispatcher=dispatcher,
            )
            app.window = FakeWindow()
            app._initialize_once()
            self.assertTrue(entered.wait(2.0))
            self.assertIsNotNone(app._process_lock)

            shutdown = threading.Thread(target=app.do_shutdown)
            shutdown.start()
            time.sleep(0.05)
            self.assertTrue(shutdown.is_alive())
            contender = StorageProcessLock(storage.state_dir)
            with self.assertRaises(StorageProcessLockError):
                contender.acquire()

            release.set()
            shutdown.join(3.0)
            self.assertFalse(shutdown.is_alive())
            contender.acquire()
            contender.close()

    def test_every_oserror_from_lock_acquire_becomes_localized_fatal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "config" / "settings.json")
            store.save(AppSettings().with_changes(language="cs"))
            app = HarnessApplication(
                settings_store=store,
                storage=RecordingStorage(root / "recordings", root / "state"),
                storage_executor=ImmediateExecutor(),
                dispatcher=dispatch_immediately,
            )
            with patch(
                "minirec.application.StorageProcessLock.acquire",
                side_effect=PermissionError("permission denied"),
            ):
                app._initialize_once()

            self.assertTrue(app._instance_lock_fatal)
            self.assertEqual("cs", app._translator().resolved_language)
            self.assertIn(
                "nemůže přistupovat",
                app._translator()("instance_fatal_title"),
            )

    def test_explicit_process_lock_conflict_uses_instance_busy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = HarnessApplication(
                settings_store=SettingsStore(root / "config" / "settings.json"),
                storage=RecordingStorage(root / "recordings", root / "state"),
                storage_executor=ImmediateExecutor(),
                dispatcher=dispatch_immediately,
            )
            with patch(
                "minirec.application.StorageProcessLock.acquire",
                side_effect=StorageProcessLockError("already owned"),
            ):
                app._initialize_once()

            self.assertFalse(app._instance_lock_fatal)
            self.assertEqual(
                "MiniRec is already running",
                app._translator()("instance_busy_title"),
            )


class SettingsBackupTest(unittest.TestCase):
    def test_invalid_settings_are_preserved_before_defaults_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b"not-json")
            store = SettingsStore(path)
            storage = RecordingStorage(
                Path(directory) / "recordings", Path(directory) / "state"
            )
            app = HarnessApplication(
                settings_store=store,
                storage=storage,
                storage_executor=ImmediateExecutor(),
                dispatcher=dispatch_immediately,
            )
            app._initialize_once()
            self.assertEqual(AppSettings(), app.settings)
            backups = list(path.parent.glob("settings.json.corrupt-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(b"not-json", backups[0].read_bytes())
            self.assertEqual(AppSettings(), store.load())


if __name__ == "__main__":
    unittest.main()
