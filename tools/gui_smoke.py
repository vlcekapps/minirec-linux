#!/usr/bin/env python3
"""Exercise every MiniRec GTK window without opening a microphone.

The normal mode runs the complete smoke test once in English and once in
Czech, plus a crash-isolated production-controller settings regression.
``--atspi-harness`` is an intentionally idle, microphone-free process used by
``accessibility_smoke.py`` to inspect the real GTK accessibility tree.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from minirec.application import MiniRecApplication  # noqa: E402
from minirec.i18n import (  # noqa: E402
    LANGUAGE_CHOICES,
    Translator,
    format_duration,
    format_file_size,
)
from minirec.models import BITRATE_OPTIONS_KBPS, RecordingFormat  # noqa: E402
from minirec.settings import (  # noqa: E402
    AppLanguage,
    AppSettings,
    SettingsStore,
)
from minirec.storage import RecordingStorage  # noqa: E402
from minirec.ui import (  # noqa: E402
    MainWindow,
    PlayerView,
    RecorderView,
    RecordingView,
    RenameWindow,
)


RECORDING_MODIFIED_NS = 1_785_751_872_000_000_000
RECORDINGS = (
    RecordingView(
        "one",
        "Morning notes.oga",
        65.0,
        123_456,
        RECORDING_MODIFIED_NS,
        RecordingFormat.OGG_OPUS,
    ),
    RecordingView(
        "two",
        "",
        None,
        None,
        RECORDING_MODIFIED_NS,
        RecordingFormat.WAV,
    ),
)


class TrackingSettingsStore(SettingsStore):
    """Persist normally while exposing production save calls to this gate."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.saved_states: list[AppSettings] = []

    def save(
        self, settings: AppSettings | Mapping[str, object]
    ) -> AppSettings:
        saved = super().save(settings)
        self.saved_states.append(saved)
        return saved


def widget_children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    """Return direct GTK children without relying on private CSS nodes."""

    result: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        result.append(child)
        child = child.get_next_sibling()
    return result


def widget_descendants(widget: Gtk.Widget) -> list[Gtk.Widget]:
    """Return all GTK descendants in deterministic breadth-first order."""

    result: list[Gtk.Widget] = []
    pending = widget_children(widget)
    while pending:
        current = pending.pop(0)
        result.append(current)
        pending.extend(widget_children(current))
    return result


def is_focus_within(focus: Gtk.Widget | None, container: Gtk.Widget) -> bool:
    while focus is not None:
        if focus is container:
            return True
        focus = focus.get_parent()
    return False


def activate_button(button: Gtk.Button) -> None:
    """Emit the native activation result shared by Enter, Space and AT-SPI."""

    button.emit("clicked")


class FakeCallbacks:
    """Synchronous presentation port which never opens audio or the network."""

    def __init__(self, language: str) -> None:
        self.language = language
        self.main: MainWindow | None = None
        self.events: list[tuple[object, ...]] = []
        self.settings = AppSettings(
            language=(
                AppLanguage.CZECH
                if language == "cs"
                else AppLanguage.ENGLISH
            )
        )
        self.recordings = list(RECORDINGS)
        self.player = PlayerView(
            phase="ready",
            position_seconds=0.0,
            duration_seconds=125.0,
            speed=1.0,
        )

    def bind(self, main: MainWindow) -> None:
        self.main = main

    def _require_main(self) -> MainWindow:
        assert self.main is not None
        return self.main

    def on_record(self) -> None:
        self.events.append(("record",))
        self._require_main().set_recorder_view(
            RecorderView("recording", 1.0, 3_600.0), announce=False
        )

    def on_pause(self) -> None:
        self.events.append(("pause",))
        self._require_main().set_recorder_view(
            RecorderView("paused", 2.0, 3_599.0), announce=False
        )

    def on_resume(self) -> None:
        self.events.append(("resume",))
        self._require_main().set_recorder_view(
            RecorderView("recording", 2.0, 3_599.0), announce=False
        )

    def on_stop(self) -> None:
        self.events.append(("stop",))
        self._require_main().set_recorder_view(
            RecorderView("stopped", 3.0, 3_598.0), announce=False
        )

    def on_stop_and_quit(self) -> None:
        self.events.append(("stop-and-quit",))

    def on_settings_changed(self, settings: AppSettings) -> None:
        self.events.append(("settings", settings))
        language_changed = settings.language is not self.settings.language
        self.settings = settings
        if language_changed:
            language = settings.language.value
            translator = Translator(
                language,
                system_locale="cs_CZ" if language == "cs" else "en_US",
            )
            self._require_main().apply_settings(settings, translator=translator)
        else:
            self._require_main().apply_settings(settings)

    def on_refresh_recordings(self) -> None:
        self.events.append(("refresh",))
        self._require_main().set_recordings(self.recordings)

    def on_open_recordings_folder(self) -> None:
        self.events.append(("open-folder",))

    def on_thank_author(self) -> None:
        self.events.append(("thank-author",))

    def on_open_player(self, identifier: str) -> None:
        self.events.append(("open-player", identifier))
        self._require_main().set_player_view(self.player)

    def on_rename_recording(self, identifier: str, new_name: str) -> None:
        self.events.append(("rename", identifier, new_name))
        self.recordings = [
            replace(item, name=new_name) if item.identifier == identifier else item
            for item in self.recordings
        ]
        self._require_main().set_recordings(self.recordings)

    def on_delete_recordings(self, identifiers: tuple[str, ...]) -> None:
        self.events.append(("delete", identifiers))
        removed = set(identifiers)
        self.recordings = [
            item for item in self.recordings if item.identifier not in removed
        ]
        self._require_main().set_recordings(self.recordings)

    def on_player_play(self, identifier: str) -> None:
        self.events.append(("player-play", identifier))
        self.player = replace(self.player, phase="playing")
        self._require_main().set_player_view(self.player)

    def on_player_pause(self, identifier: str) -> None:
        self.events.append(("player-pause", identifier))
        self.player = replace(self.player, phase="paused")
        self._require_main().set_player_view(self.player)

    def on_player_seek(self, identifier: str, position_seconds: float) -> None:
        self.events.append(("player-seek", identifier, position_seconds))
        self.player = replace(self.player, position_seconds=position_seconds)
        self._require_main().set_player_view(self.player)

    def on_player_seek_by(self, identifier: str, delta_seconds: float) -> None:
        self.events.append(("player-seek-by", identifier, delta_seconds))
        position = max(
            0.0,
            min(
                self.player.duration_seconds or 0.0,
                self.player.position_seconds + delta_seconds,
            ),
        )
        self.player = replace(self.player, position_seconds=position)
        self._require_main().set_player_view(self.player)

    def on_player_speed(self, identifier: str, speed: float) -> None:
        self.events.append(("player-speed", identifier, speed))
        self.player = replace(self.player, speed=speed)
        self._require_main().set_player_view(self.player)

    def on_player_closed(self, identifier: str) -> None:
        self.events.append(("player-closed", identifier))


class SmokeApplication(Adw.Application):
    """One language run of the deterministic graphical gate."""

    def __init__(self, language: str) -> None:
        suffix = "Cs" if language == "cs" else "En"
        super().__init__(
            application_id=f"cz.pvlcek.minirec.GuiSmoke{suffix}",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.language = language
        self.callbacks = FakeCallbacks(language)
        self.main_window: MainWindow | None = None
        self.extra_windows: list[Gtk.Window] = []
        self.passed = False

    def do_activate(self) -> None:
        try:
            translator = Translator(
                self.language,
                system_locale="cs_CZ" if self.language == "cs" else "en_US",
            )
            self.main_window = MainWindow(
                self,
                self.callbacks,
                translator=translator,
                settings=self.callbacks.settings,
                version="0.1.23-smoke",
                recordings_path="/tmp/minirec-smoke/Recordings/MiniRec",
            )
            self.callbacks.bind(self.main_window)
            self.main_window.set_recordings(RECORDINGS)
            self.main_window.present()
            GLib.timeout_add(300, self.exercise)
        except BaseException:
            traceback.print_exc()
            self.quit()

    def exercise(self) -> bool:
        try:
            assert self.main_window is not None
            main = self.main_window
            t = main.t

            assert main.get_title() == "MiniRec"
            assert main.main_heading.get_text() == t("main_heading")
            assert main.primary_button.get_label() == t("action_record")
            assert main.primary_button.get_focusable()
            assert not main.stop_button.get_sensitive()
            menu_focusables = [
                widget
                for widget in (main.menu_button, *widget_descendants(main.menu_button))
                if widget.get_focusable()
            ]
            assert menu_focusables
            assert main.menu_button.get_accessible_role() in {
                Gtk.AccessibleRole.BUTTON,
                Gtk.AccessibleRole.MENU,
            }

            # ``clicked`` is GtkButton's native semantic result for Enter,
            # Space and AT-SPI; no pointer-only gesture is involved here.
            activate_button(main.primary_button)
            assert self.callbacks.events[-1] == ("record",)
            assert main.primary_button.get_label() == t("action_pause")
            assert main.stop_button.get_sensitive()
            assert not main.window_actions["settings"].get_enabled()
            assert not main.window_actions["recordings"].get_enabled()
            activate_button(main.primary_button)
            assert self.callbacks.events[-1] == ("pause",)
            assert main.primary_button.get_label() == t("action_resume")
            activate_button(main.primary_button)
            assert self.callbacks.events[-1] == ("resume",)
            activate_button(main.stop_button)
            assert self.callbacks.events[-1] == ("stop",)
            assert main.window_actions["settings"].get_enabled()
            assert main.window_actions["recordings"].get_enabled()

            main.window_actions["open-recordings-folder"].activate(None)
            main.window_actions["thank-author"].activate(None)
            assert ("open-folder",) in self.callbacks.events
            assert ("thank-author",) in self.callbacks.events

            main.show_settings()
            settings = main._settings_window
            assert settings is not None
            self.extra_windows.append(settings)
            assert settings.language.get_accessible_role() == Gtk.AccessibleRole.COMBO_BOX
            assert settings.format.get_accessible_role() == Gtk.AccessibleRole.COMBO_BOX
            assert settings.bitrate.get_accessible_role() == Gtk.AccessibleRole.COMBO_BOX
            assert settings.gain.get_accessible_role() == Gtk.AccessibleRole.SLIDER
            assert settings.mono.get_accessible_role() == Gtk.AccessibleRole.RADIO
            assert settings.stereo.get_accessible_role() == Gtk.AccessibleRole.RADIO
            assert settings.prevent_sleep.get_accessible_role() == Gtk.AccessibleRole.CHECKBOX
            settings.format.set_selected(tuple(RecordingFormat).index(RecordingFormat.WAV))
            assert self.callbacks.settings.recording.format is RecordingFormat.WAV
            assert not settings.bitrate.get_sensitive()
            assert settings.bitrate_note.get_visible()
            settings.mono.set_active(True)
            assert self.callbacks.settings.stereo is False
            settings.gain.set_value(4)
            assert self.callbacks.settings.gain_db == 4
            settings.prevent_sleep.set_active(True)
            assert self.callbacks.settings.prevent_sleep
            settings.close_button.grab_focus()
            assert is_focus_within(settings.get_focus(), settings.close_button)
            activate_button(settings.close_button)

            main.show_recordings()
            browser = main._recordings_window
            assert browser is not None
            self.extra_windows.append(browser)
            assert browser.list.get_accessible_role() == Gtk.AccessibleRole.LIST
            assert browser.list.get_tab_behavior() == Gtk.ListTabBehavior.ALL
            assert browser.list._minirec_store.get_n_items() == 2
            first_row = browser.list._minirec_store.get_item(0)
            assert isinstance(first_row, Gtk.Widget)
            expected_details = t(
                "recording_details",
                date=main.translator.format_recording_date(
                    RECORDINGS[0].modified_ns
                ),
                duration=format_duration(RECORDINGS[0].duration_seconds),
                size=format_file_size(RECORDINGS[0].size_bytes),
                format=t("format_ogg_opus"),
            )
            assert expected_details in {
                widget.get_text()
                for widget in widget_descendants(first_row)
                if isinstance(widget, Gtk.Label)
            }
            assert browser.list._minirec_metadata[first_row] == (
                t("recording_open", name="Morning notes.oga"),
                t(
                    "recording_row",
                    name="Morning notes.oga",
                    details=expected_details,
                ),
            )
            assert not browser.clear_button.get_sensitive()
            assert not browser.delete_selected_button.get_sensitive()
            first_selector = browser._selection_buttons["one"]
            first_selector.grab_focus()
            assert is_focus_within(browser.get_focus(), first_selector)
            first_selector.set_active(True)
            assert browser.selected == ("one",)
            assert browser.clear_button.get_sensitive()
            activate_button(browser.clear_button)
            assert browser.selected == ()

            rename = RenameWindow(
                browser,
                main.translator,
                RECORDINGS[0],
                browser.refresh_button,
                lambda name: self.callbacks.on_rename_recording("one", name),
            )
            self.extra_windows.append(rename)
            rename.present()
            rename.entry.set_text("")
            rename.entry.emit("activate")
            assert rename.error.get_visible()
            assert rename.error.get_text() == t("rename_empty")
            rename.entry.set_text("Keyboard renamed.oga")
            rename.entry.emit("activate")
            assert ("rename", "one", "Keyboard renamed.oga") in self.callbacks.events
            renamed = next(
                item for item in self.callbacks.recordings if item.identifier == "one"
            )
            assert renamed.modified_ns == RECORDINGS[0].modified_ns
            assert renamed.format is RECORDINGS[0].format

            # Activating the native list row is its Enter/Space path.
            browser.list.emit("activate", 0)
            player = main._player_window
            assert player is not None
            self.extra_windows.append(player)
            assert ("open-player", "one") in self.callbacks.events
            assert player.play_button.get_sensitive()
            assert player.seek.get_accessible_role() == Gtk.AccessibleRole.SLIDER
            assert player.speed.get_accessible_role() == Gtk.AccessibleRole.COMBO_BOX
            player.play_button.grab_focus()
            assert is_focus_within(player.get_focus(), player.play_button)
            activate_button(player.play_button)
            assert self.callbacks.events[-1] == ("player-play", "one")
            assert player.play_button.get_label() == t("action_player_pause")
            activate_button(player.play_button)
            assert self.callbacks.events[-1] == ("player-pause", "one")
            activate_button(player.forward_button)
            assert self.callbacks.events[-1] == ("player-seek-by", "one", 10.0)
            activate_button(player.rewind_button)
            assert self.callbacks.events[-1] == ("player-seek-by", "one", -10.0)
            player.seek.emit("change-value", Gtk.ScrollType.JUMP, 23.0)
            assert self.callbacks.events[-1] == ("player-seek", "one", 23.0)
            player.speed.set_selected(4)
            assert self.callbacks.events[-1] == ("player-speed", "one", 1.5)
            activate_button(player.close_button)
            assert ("player-closed", "one") in self.callbacks.events

            # Every visible text action must retain a wrapped label at large
            # text sizes; the detailed 22 pt geometry lives in the next gate.
            for root in (main, browser, settings, player):
                for widget in (root, *widget_descendants(root)):
                    if not isinstance(widget, (Gtk.Button, Gtk.CheckButton)):
                        continue
                    label = widget.get_label()
                    if not label:
                        continue
                    labels = [
                        item
                        for item in widget_descendants(widget)
                        if isinstance(item, Gtk.Label) and item.get_text() == label
                    ]
                    assert labels, (self.language, type(widget).__name__, label)
                    assert all(item.get_wrap() for item in labels)

            self.passed = True
        except BaseException:
            traceback.print_exc()
        finally:
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def _close_and_quit(self) -> None:
        for window in reversed(self.extra_windows):
            if window.get_visible():
                window.close()
        if self.main_window is not None:
            self.main_window.close()
        self.quit()


class ProductionSettingsRegressionApplication(MiniRecApplication):
    """Exercise the real settings controller while keeping audio unopened."""

    def __init__(
        self,
        settings_store: TrackingSettingsStore,
        storage: RecordingStorage,
    ) -> None:
        super().__init__(
            settings_store=settings_store,
            storage=storage,
            application_id="cz.pvlcek.minirec.GuiSettingsRegression",
            non_unique=True,
        )
        self.tracking_settings_store = settings_store
        self.passed = False
        self._started_regression = False
        self._settings_window = None
        self._bitrate_model = None
        self._bitrate_model_notifications = 0
        self._language_signal_snapshots: list[tuple[str, str]] = []
        self._language_waits = 0

    def do_activate(self) -> None:
        try:
            super().do_activate()
            if not self._started_regression:
                self._started_regression = True
                GLib.timeout_add(25, self._wait_for_startup)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()

    def _wait_for_startup(self) -> bool:
        try:
            assert self._instance_lock_error is None, self._instance_lock_error
            if not self._startup_recovery_complete:
                return GLib.SOURCE_CONTINUE
            assert self.window is not None
            assert self.settings.language is AppLanguage.ENGLISH
            assert self.settings.recording.format is RecordingFormat.OGG_OPUS
            assert self.settings.recording.bitrate_kbps == 128

            self.window.show_settings()
            settings_window = self.window._settings_window
            assert settings_window is not None
            self._settings_window = settings_window
            self._bitrate_model = settings_window.bitrate.get_model()
            assert self._bitrate_model is not None
            settings_window.bitrate.connect(
                "notify::model", self._bitrate_model_changed
            )

            # This is the exact production path which previously replaced the
            # active drop-down model re-entrantly and crashed in GObject.
            settings_window.bitrate.set_selected(
                BITRATE_OPTIONS_KBPS.index(320)
            )
            GLib.idle_add(self._verify_bitrate_then_change_language)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def _bitrate_model_changed(self, *_args: object) -> None:
        self._bitrate_model_notifications += 1

    def _language_signal_observed(self, *_args: object) -> None:
        assert self.window is not None
        self._language_signal_snapshots.append(
            (
                self.window.translator.resolved_language,
                self.window.main_heading.get_text(),
            )
        )

    def _verify_bitrate_then_change_language(self) -> bool:
        try:
            assert self.window is not None
            settings_window = self._settings_window
            assert settings_window is not None
            assert settings_window.bitrate.get_model() is self._bitrate_model
            assert self._bitrate_model_notifications == 0
            assert self.settings.recording.format is RecordingFormat.OGG_OPUS
            assert self.settings.recording.bitrate_kbps == 320
            assert settings_window.settings.recording.bitrate_kbps == 320

            saved_states = self.tracking_settings_store.saved_states
            assert len(saved_states) == 1, saved_states
            assert saved_states[0].language is AppLanguage.ENGLISH
            assert saved_states[0].recording.format is RecordingFormat.OGG_OPUS
            assert saved_states[0].recording.bitrate_kbps == 320
            persisted = self.tracking_settings_store.load()
            assert persisted == saved_states[0]

            # The production callback is connected first.  This observer sees
            # the end of that same notify emission: translations must still be
            # English here and may be applied only on a later main-loop turn.
            settings_window.language.connect(
                "notify::selected", self._language_signal_observed
            )
            settings_window.language.set_selected(
                LANGUAGE_CHOICES.index(AppLanguage.CZECH.value)
            )
            assert self._language_signal_snapshots == [
                ("en", "Audio recorder")
            ]
            assert self.window.translator.resolved_language == "en"
            assert len(saved_states) == 2, saved_states
            assert saved_states[-1].language is AppLanguage.CZECH
            assert saved_states[-1].recording.bitrate_kbps == 320
            GLib.timeout_add(10, self._verify_deferred_language)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def _verify_deferred_language(self) -> bool:
        try:
            assert self.window is not None
            if self.window.translator.resolved_language != "cs":
                self._language_waits += 1
                assert self._language_waits < 100, "deferred language update timed out"
                return GLib.SOURCE_CONTINUE
            settings_window = self._settings_window
            assert settings_window is not None
            assert self.window.main_heading.get_text() == "Záznam zvuku"
            assert settings_window.settings.language is AppLanguage.CZECH
            assert (
                settings_window.language.get_selected()
                == LANGUAGE_CHOICES.index(AppLanguage.CZECH.value)
            )
            assert len(self.tracking_settings_store.saved_states) == 2
            assert self.tracking_settings_store.load().language is AppLanguage.CZECH
            self.passed = True
            print(
                "Production settings regression passed: Ogg 128→320 without "
                "model replacement; language update deferred",
                flush=True,
            )
        except BaseException:
            traceback.print_exc()
        self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def _close_and_quit(self) -> None:
        if self._settings_window is not None and self._settings_window.get_visible():
            self._settings_window.close()
        if self.window is not None:
            self.window.close()
        self.quit()


class AtspiHarnessApplication(Adw.Application):
    """Stable collection of mapped windows for external AT-SPI inspection."""

    def __init__(self, language: str, token: str) -> None:
        suffix = "Cs" if language == "cs" else "En"
        super().__init__(
            application_id=f"cz.pvlcek.minirec.AccessibilitySmoke{suffix}",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.language = language
        self.token = token
        self.callbacks = FakeCallbacks(language)
        self.main_window: MainWindow | None = None
        self.rename_window: RenameWindow | None = None
        self.windows: list[Gtk.Window] = []

    def do_activate(self) -> None:
        try:
            GLib.set_application_name(self.token)
            translator = Translator(
                self.language,
                system_locale="cs_CZ" if self.language == "cs" else "en_US",
            )
            main = MainWindow(
                self,
                self.callbacks,
                translator=translator,
                settings=self.callbacks.settings,
                version="accessibility-smoke",
                recordings_path="/tmp/minirec-accessibility/Recordings/MiniRec",
            )
            self.main_window = main
            self.callbacks.bind(main)
            main.set_recordings(RECORDINGS)
            main.present()
            main.show_settings()
            main.show_recordings()
            main.show_player(RECORDINGS[0])
            assert main._settings_window is not None
            assert main._recordings_window is not None
            assert main._player_window is not None
            rename = RenameWindow(
                main._recordings_window,
                translator,
                RECORDINGS[0],
                main._recordings_window.refresh_button,
                lambda _name: None,
            )
            # The production rename form is modal.  The inspection harness
            # makes only this instance non-modal so AT-SPI can move focus
            # across all five simultaneously mapped windows.
            rename.set_modal(False)
            rename.present()
            self.rename_window = rename
            self.windows = [
                main._settings_window,
                main._recordings_window,
                main._player_window,
                rename,
            ]
            GLib.timeout_add(700, self._ready)
        except BaseException:
            traceback.print_exc()
            self.quit()

    def _ready(self) -> bool:
        if self.rename_window is not None:
            self.rename_window.present()
            self.rename_window.entry.grab_focus()
        print(f"MINIREC_ATSPI_READY {self.token}", flush=True)
        return GLib.SOURCE_REMOVE

    def do_shutdown(self) -> None:
        for window in reversed(self.windows):
            window.close()
        if self.main_window is not None:
            self.main_window.close()
        Adw.Application.do_shutdown(self)


def run_language(language: str) -> bool:
    app = SmokeApplication(language)
    status = app.run([sys.argv[0]])
    return status == 0 and app.passed


def run_production_settings_regression_child() -> bool:
    """Run the real controller against private temporary state and storage."""

    with tempfile.TemporaryDirectory(prefix="minirec-gui-settings-") as directory:
        root = Path(directory)
        settings_path = root / "config" / "settings.json"
        # Establish a deterministic starting language without adding a call to
        # the tracking store used for the actual UI interaction.
        SettingsStore(settings_path).save(
            AppSettings(language=AppLanguage.ENGLISH)
        )
        settings_store = TrackingSettingsStore(settings_path)
        app = ProductionSettingsRegressionApplication(
            settings_store,
            RecordingStorage(root / "Recordings" / "MiniRec", root / "state"),
        )
        status = app.run([sys.argv[0]])
        return status == 0 and app.passed


def run_production_settings_regression() -> bool:
    """Keep a native GTK crash isolated from the remaining GUI gates."""

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "faulthandler",
            str(Path(__file__).resolve()),
            "--settings-regression-child",
        ],
        check=False,
    )
    if completed.returncode != 0:
        print(
            "Production settings regression failed with status "
            f"{completed.returncode}",
            file=sys.stderr,
        )
        return False
    return True


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--atspi-harness", action="store_true")
    parser.add_argument("--settings-regression-child", action="store_true")
    parser.add_argument("--language", choices=("en", "cs"), default="en")
    parser.add_argument("--token", default="MiniRec accessibility smoke")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    if arguments.settings_regression_child:
        raise SystemExit(0 if run_production_settings_regression_child() else 1)
    if arguments.atspi_harness:
        GLib.set_application_name(arguments.token)
        harness = AtspiHarnessApplication(arguments.language, arguments.token)
        raise SystemExit(harness.run([sys.argv[0]]))
    if not run_production_settings_regression():
        raise SystemExit(1)
    if not all(run_language(language) for language in ("en", "cs")):
        raise SystemExit(1)
    print("GUI smoke test passed for production settings, English, and Czech")
