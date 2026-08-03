from __future__ import annotations

from enum import Enum, auto
import math
import os
import unittest
from unittest.mock import patch

from minirec.models import RecordingFormat
from minirec.gtk_helpers import (
    MAX_RECORDING_SELECTION,
    MIN_CONTROL_HEIGHT,
    Adw,
    Gtk,
    HeadingLabel,
    LiveStatus,
    clamp,
    clamp_seek,
    focus_index_after_removal,
    index_for_value,
    navigable_list,
    normalize_selection,
    phase_name,
    record_action_for_state,
    seek_step_target,
    string_dropdown,
    toggle_selection,
    wrapping_button,
    wrapping_check_button,
)
from minirec.ui import (
    ACTIVE_RECORDING_PHASES,
    MainWindow,
    PlayerView,
    RecorderView,
    RecordingView,
)


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, _name: str):
        return lambda *_values: self.calls.append((_name, _values))


class _Phase(Enum):
    RECORDING = auto()


class RecorderPolicyTest(unittest.TestCase):
    def test_publication_is_active_but_startup_recovery_is_not_capture(self) -> None:
        self.assertIn("finalizing", ACTIVE_RECORDING_PHASES)
        self.assertNotIn("recovering", ACTIVE_RECORDING_PHASES)

    def test_phase_name_accepts_backend_enums_and_qualified_strings(self) -> None:
        self.assertEqual("recording", phase_name(_Phase.RECORDING))
        self.assertEqual("paused", phase_name("RecordingPhase.PAUSED"))
        self.assertEqual("", phase_name(None))

    def test_only_stable_recorder_states_expose_a_primary_action(self) -> None:
        expected = {
            "idle": "record",
            "ready": "record",
            "error": "record",
            "stopped": "record",
            "recording": "pause",
            "paused": "resume",
            "starting": "none",
            "pausing": "none",
            "resuming": "none",
            "stopping": "none",
            "closed": "none",
            "unknown": "none",
        }
        self.assertEqual(
            expected,
            {state: record_action_for_state(state) for state in expected},
        )


class SelectionPolicyTest(unittest.TestCase):
    def test_normalization_is_stable_deduplicated_and_filters_stale_ids(self) -> None:
        self.assertEqual(
            ("b", "a"),
            normalize_selection(
                ["b", "stale", "a", "b"], available=["a", "b", "c"]
            ),
        )

    def test_limit_is_exact_and_removal_is_always_allowed(self) -> None:
        selected = tuple(range(MAX_RECORDING_SELECTION))
        unchanged, accepted = toggle_selection(selected, 999)
        self.assertFalse(accepted)
        self.assertEqual(selected, unchanged)
        removed, accepted = toggle_selection(selected, 10)
        self.assertTrue(accepted)
        self.assertNotIn(10, removed)
        self.assertEqual(MAX_RECORDING_SELECTION - 1, len(removed))

    def test_negative_selection_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_selection([1], limit=-1)
        self.assertEqual((), normalize_selection([1], limit=0))

    def test_focus_after_removal_uses_nearest_surviving_row(self) -> None:
        self.assertEqual(2, focus_index_after_removal(2, 5))
        self.assertEqual(3, focus_index_after_removal(9, 4))
        self.assertEqual(0, focus_index_after_removal(-2, 4))
        self.assertIsNone(focus_index_after_removal(0, 0))


class RangePolicyTest(unittest.TestCase):
    def test_clamp_rejects_invalid_numbers_and_bounds_values(self) -> None:
        self.assertEqual(0.0, clamp(-2, 0, 10))
        self.assertEqual(7.0, clamp(7, 0, 10))
        self.assertEqual(10.0, clamp(12, 0, 10))
        with self.assertRaises(ValueError):
            clamp(math.nan, 0, 1)
        with self.assertRaises(ValueError):
            clamp(0, 2, 1)

    def test_seek_targets_are_bounded_for_absolute_and_relative_use(self) -> None:
        self.assertEqual(0.0, clamp_seek(-2, 30))
        self.assertEqual(30.0, clamp_seek(50, 30))
        self.assertEqual(0.0, seek_step_target(5, 30, -10))
        self.assertEqual(30.0, seek_step_target(25, 30, 10))

    def test_dropdown_index_has_a_safe_explicit_default(self) -> None:
        self.assertEqual(1, index_for_value(("a", "b"), "b"))
        self.assertEqual(1, index_for_value(("a", "b"), "x", default=1))
        with self.assertRaises(ValueError):
            index_for_value((), "x")
        with self.assertRaises(ValueError):
            index_for_value(("a",), "x", default=1)


class GtkAccessibilityContractTest(unittest.TestCase):
    """Widget checks run in GUI gates and skip cleanly in a displayless unit run."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("MINIREC_GUI_TESTS") != "1":
            raise unittest.SkipTest("set MINIREC_GUI_TESTS=1 in the GUI gate")
        if not Gtk.init_check():
            raise unittest.SkipTest("GTK display is unavailable")

    def test_custom_heading_and_status_roles_are_explicit(self) -> None:
        heading = HeadingLabel("Heading")
        status = LiveStatus("Ready")
        self.assertEqual(Gtk.AccessibleRole.HEADING, heading.get_accessible_role())
        self.assertEqual(Gtk.AccessibleRole.STATUS, status.get_accessible_role())
        self.assertTrue(status.get_focusable())

    def test_standard_controls_retain_native_roles_names_and_minimum_height(self) -> None:
        button = wrapping_button("Record")
        check = wrapping_check_button("Prevent sleep")
        dropdown = string_dropdown("Language", ["System", "English"])
        self.assertEqual(Gtk.AccessibleRole.BUTTON, button.get_accessible_role())
        self.assertEqual(Gtk.AccessibleRole.CHECKBOX, check.get_accessible_role())
        self.assertEqual(Gtk.AccessibleRole.COMBO_BOX, dropdown.get_accessible_role())
        self.assertEqual("Record", button.get_label())
        self.assertEqual("Prevent sleep", check.get_label())
        self.assertGreaterEqual(button.get_size_request()[1], MIN_CONTROL_HEIGHT)
        self.assertGreaterEqual(check.get_size_request()[1], MIN_CONTROL_HEIGHT)
        self.assertGreaterEqual(dropdown.get_size_request()[1], MIN_CONTROL_HEIGHT)

    def test_recordings_list_keeps_the_native_list_role(self) -> None:
        view = navigable_list("Recordings")
        self.assertEqual(Gtk.AccessibleRole.LIST, view.get_accessible_role())

    def test_settings_use_native_group_radio_slider_and_dropdown_roles(self) -> None:
        application = Adw.Application(
            application_id="cz.pvlcek.MiniRec.SettingsContractTest"
        )
        application.register(None)
        window = MainWindow(application, _Callbacks())
        window.show_settings()
        settings = window._settings_window
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(
            Gtk.AccessibleRole.COMBO_BOX,
            settings.language.get_accessible_role(),
        )
        self.assertEqual(
            Gtk.AccessibleRole.COMBO_BOX,
            settings.format.get_accessible_role(),
        )
        self.assertEqual(
            Gtk.AccessibleRole.COMBO_BOX,
            settings.bitrate.get_accessible_role(),
        )
        self.assertEqual(
            Gtk.AccessibleRole.GROUP,
            settings.channels_frame.get_accessible_role(),
        )
        self.assertEqual(Gtk.AccessibleRole.RADIO, settings.mono.get_accessible_role())
        self.assertEqual(
            Gtk.AccessibleRole.RADIO, settings.stereo.get_accessible_role()
        )
        self.assertEqual(Gtk.AccessibleRole.SLIDER, settings.gain.get_accessible_role())
        adjustment = settings.gain.get_adjustment()
        self.assertEqual(
            (-12.0, 12.0),
            (adjustment.get_lower(), adjustment.get_upper()),
        )
        settings.close()
        window.close()

    def test_active_recording_disables_and_closes_policy_windows(self) -> None:
        application = Adw.Application(
            application_id="cz.pvlcek.MiniRec.ActiveContractTest"
        )
        application.register(None)
        window = MainWindow(application, _Callbacks())
        window.show_settings()
        self.assertIsNotNone(window._settings_window)
        window.set_recorder_view(RecorderView("starting"), announce=False)
        self.assertIsNone(window._settings_window)
        self.assertFalse(window.window_actions["settings"].get_enabled())
        self.assertFalse(window.window_actions["recordings"].get_enabled())
        window.show_settings()
        window.show_recordings()
        self.assertIsNone(window._settings_window)
        self.assertIsNone(window._recordings_window)
        window.set_recorder_view(RecorderView("finalizing"), announce=False)
        self.assertFalse(window.window_actions["settings"].get_enabled())
        self.assertFalse(window.window_actions["recordings"].get_enabled())
        window.set_recorder_view(RecorderView("recovering"), announce=False)
        self.assertTrue(window.window_actions["settings"].get_enabled())
        self.assertTrue(window.window_actions["recordings"].get_enabled())
        window.close()

    def test_player_exposes_every_required_visible_text_action(self) -> None:
        application = Adw.Application(
            application_id="cz.pvlcek.MiniRec.PlayerContractTest"
        )
        application.register(None)
        callbacks = _Callbacks()
        window = MainWindow(application, callbacks)
        recording = RecordingView(
            "recording",
            "voice.oga",
            60.0,
            1024,
            1_785_751_872_000_000_000,
            RecordingFormat.OGG_OPUS,
        )
        window.show_player(recording)
        player = window._player_window
        self.assertIsNotNone(player)
        assert player is not None
        self.assertFalse(player.rename_button.get_sensitive())
        self.assertFalse(player.delete_button.get_sensitive())
        player.set_player_view(
            PlayerView("playing", 12.0, 60.0, 1.25), announce=False
        )
        self.assertTrue(player.rename_button.get_sensitive())
        self.assertTrue(player.delete_button.get_sensitive())
        player.rename_button.grab_focus()
        self.assertIs(player.rename_button, player.get_focus())
        original_view = player.view
        renamed = RecordingView(
            "renamed",
            "renamed.oga",
            60.0,
            1024,
            recording.modified_ns,
            recording.format,
        )
        window.update_recording(
            recording.identifier,
            renamed,
            focus_recordings=False,
        )
        self.assertIs(player, window._player_window)
        self.assertEqual(original_view, player.view)
        self.assertEqual(renamed, player.recording)
        self.assertEqual("renamed.oga", player.recording_name.get_text())
        self.assertIs(player.rename_button, player.get_focus())
        labels = {
            player.play_button.get_label(),
            player.rewind_button.get_label(),
            player.forward_button.get_label(),
            player.rename_button.get_label(),
            player.delete_button.get_label(),
            player.close_button.get_label(),
        }
        self.assertEqual(
            {"Pause", "Back 10 seconds", "Forward 10 seconds", "Rename", "Delete", "Close"},
            labels,
        )
        self.assertEqual(Gtk.AccessibleRole.SLIDER, player.seek.get_accessible_role())
        self.assertEqual(
            Gtk.AccessibleRole.COMBO_BOX, player.speed.get_accessible_role()
        )
        window.show_recordings()
        browser = window._recordings_window
        self.assertIsNotNone(browser)
        assert browser is not None
        browser.set_recordings((renamed,))
        with patch("minirec.ui.focus_list_item_later") as focus_row:
            window.close_player()
            focus_row.assert_called_once_with(browser.list, 0)
        self.assertIsNone(window._player_window)
        self.assertIn(
            ("on_player_closed", (renamed.identifier,)), callbacks.calls
        )
        window.close()

    def test_deleted_player_focus_waits_for_nearest_surviving_row(self) -> None:
        application = Adw.Application(
            application_id="cz.pvlcek.MiniRec.PlayerDeleteFocusTest"
        )
        application.register(None)
        callbacks = _Callbacks()
        window = MainWindow(application, callbacks)
        recordings = (
            RecordingView(
                "one", "one.oga", None, None, 1, RecordingFormat.OGG_OPUS
            ),
            RecordingView(
                "two", "two.oga", None, None, 2, RecordingFormat.OGG_OPUS
            ),
            RecordingView(
                "three", "three.oga", None, None, 3, RecordingFormat.OGG_OPUS
            ),
        )
        window.set_recordings(recordings)
        window.show_recordings()
        browser = window._recordings_window
        self.assertIsNotNone(browser)
        assert browser is not None
        window.show_player(recordings[1])

        window.close_player_after_delete("two")
        self.assertEqual(1, browser._pending_focus_index)
        with patch("minirec.ui.focus_list_item_later") as focus_row:
            window.set_recordings((recordings[0], recordings[2]))
            focus_row.assert_called_once_with(browser.list, 1)

        window.show_player(recordings[0])
        window.close_player_after_delete("one")
        with patch("minirec.ui.focus_later") as focus_refresh:
            window.set_recordings(())
            focus_refresh.assert_called_once_with(browser.refresh_button)
        window.close()


if __name__ == "__main__":
    unittest.main()
