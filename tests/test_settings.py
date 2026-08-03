from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from minirec.models import ChannelMode, RecordingFormat
from minirec.settings import (
    AppLanguage,
    AppSettings,
    MAX_SETTINGS_BYTES,
    SettingsFormatError,
    SettingsStore,
    SettingsWriteError,
    XdgPaths,
    default_settings_path,
)


class SettingsModelTest(unittest.TestCase):
    def test_defaults_match_linux_recording_policy(self) -> None:
        settings = AppSettings()
        self.assertIs(AppLanguage.SYSTEM, settings.language)
        self.assertIs(RecordingFormat.OGG_OPUS, settings.recording_format)
        self.assertEqual(128, settings.bitrate_kbps)
        self.assertIs(ChannelMode.STEREO, settings.channel_mode)
        self.assertEqual(0, settings.gain_db)
        self.assertFalse(settings.prevent_sleep)

    def test_mapping_sanitizes_bounds_aliases_and_preserves_future_keys(self) -> None:
        settings = AppSettings.from_mapping(
            {
                "language": "CS",
                "format": "mp3",
                "bitrate_kbps": 100,
                "stereo": False,
                "gain_db": 999,
                "prevent_sleep_during_recording": True,
                "future": {"kept": True},
            }
        )
        self.assertIs(AppLanguage.CZECH, settings.language)
        self.assertIs(RecordingFormat.MP3, settings.format)
        self.assertEqual(96, settings.bitrate_kbps)
        self.assertIs(ChannelMode.MONO, settings.channel_mode)
        self.assertEqual(12, settings.gain_db)
        self.assertTrue(settings.prevent_sleep)
        self.assertEqual({"kept": True}, settings.to_dict()["future"])

    def test_mapping_accepts_typed_format_and_channel_values(self) -> None:
        settings = AppSettings.from_mapping(
            {
                "recording_format": RecordingFormat.WAV,
                "channel_mode": ChannelMode.MONO,
            }
        )
        self.assertIs(RecordingFormat.WAV, settings.format)
        self.assertIs(ChannelMode.MONO, settings.channel_mode)

    def test_every_flat_setter_overrides_the_existing_canonical_value(self) -> None:
        current = AppSettings()
        current = current.with_changes(format=RecordingFormat.MP3)
        self.assertIs(RecordingFormat.MP3, current.format)
        current = current.with_changes(recording_format=RecordingFormat.WAV)
        self.assertIs(RecordingFormat.WAV, current.format)
        current = current.with_changes(bitrate_kbps=320)
        self.assertEqual(320, current.bitrate_kbps)
        current = current.with_changes(stereo=False)
        self.assertIs(ChannelMode.MONO, current.channel_mode)
        current = current.with_changes(channel_mode=ChannelMode.STEREO)
        self.assertIs(ChannelMode.STEREO, current.channel_mode)
        current = current.with_changes(gain_db=-7)
        self.assertEqual(-7, current.gain_db)
        current = current.with_changes(language=AppLanguage.ENGLISH)
        self.assertIs(AppLanguage.ENGLISH, current.language)
        current = current.with_changes(prevent_sleep_during_recording=True)
        self.assertTrue(current.prevent_sleep)
        current = current.with_changes(prevent_sleep=False)
        self.assertFalse(current.prevent_sleep)

    def test_canonical_key_wins_if_both_spellings_are_supplied(self) -> None:
        settings = AppSettings().with_changes(
            format=RecordingFormat.MP3,
            recording_format=RecordingFormat.WAV,
            stereo=False,
            channel_mode=ChannelMode.STEREO,
            prevent_sleep_during_recording=True,
            prevent_sleep=False,
        )
        self.assertIs(RecordingFormat.WAV, settings.format)
        self.assertIs(ChannelMode.STEREO, settings.channel_mode)
        self.assertFalse(settings.prevent_sleep)


class XdgSettingsPathTest(unittest.TestCase):
    def test_xdg_paths_use_absolute_overrides_and_ignore_relative_values(self) -> None:
        home = Path("/users/tester")
        paths = XdgPaths.from_environment(
            {
                "XDG_CONFIG_HOME": "/config-root",
                "XDG_STATE_HOME": "relative-state",
            },
            home=home,
        )
        self.assertEqual(Path("/config-root/minirec"), paths.config_dir)
        self.assertEqual(home / ".local/state/minirec", paths.state_dir)
        self.assertEqual(
            Path("/config-root/minirec/settings.json"),
            default_settings_path(
                {"XDG_CONFIG_HOME": "/config-root"}, home=home
            ),
        )


class SettingsStoreTest(unittest.TestCase):
    def test_missing_loads_defaults_and_save_is_private_utf8_json(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "settings.json"
            store = SettingsStore(path)
            self.assertEqual(AppSettings(), store.load())
            stored = store.save(
                AppSettings().with_changes(
                    language=AppLanguage.CZECH,
                    format=RecordingFormat.MP3,
                    gain_db=4,
                )
            )
            self.assertEqual(stored, store.load())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            decoded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("cs", decoded["language"])
            self.assertEqual("mp3", decoded["recording_format"])

    def test_store_update_exercises_each_ui_setter_and_persists_it(self) -> None:
        with TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            expectations = (
                (
                    {"format": RecordingFormat.MP3},
                    lambda value: value.format is RecordingFormat.MP3,
                ),
                (
                    {"recording_format": RecordingFormat.WAV},
                    lambda value: value.format is RecordingFormat.WAV,
                ),
                ({"bitrate_kbps": 256}, lambda value: value.bitrate_kbps == 256),
                ({"stereo": False}, lambda value: value.channel_mode is ChannelMode.MONO),
                (
                    {"channel_mode": ChannelMode.STEREO},
                    lambda value: value.channel_mode is ChannelMode.STEREO,
                ),
                ({"gain_db": 8}, lambda value: value.gain_db == 8),
                (
                    {"language": AppLanguage.CZECH},
                    lambda value: value.language is AppLanguage.CZECH,
                ),
                ({"prevent_sleep_during_recording": True}, lambda value: value.prevent_sleep),
                ({"prevent_sleep": False}, lambda value: not value.prevent_sleep),
            )
            for change, predicate in expectations:
                with self.subTest(change=change):
                    updated = store.update(**change)
                    self.assertTrue(predicate(updated))
                    self.assertTrue(predicate(store.load()))

    def test_invalid_json_root_utf8_nonfinite_and_size_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            for payload in (b"[]", b"\xff", b'{"gain_db": NaN}', b"{"):
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(SettingsFormatError):
                        store.load()
            path.write_bytes(b" " * (MAX_SETTINGS_BYTES + 1))
            with self.assertRaises(SettingsFormatError):
                store.load()

    def test_load_requests_only_one_bounded_chunk(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b"{}")
            real_open = Path.open
            requested_sizes: list[int] = []

            class BoundedReader:
                def __init__(self, source):
                    self.source = source

                def __enter__(self):
                    return self

                def __exit__(self, *exc: object) -> None:
                    self.source.close()

                def read(self, size: int) -> bytes:
                    requested_sizes.append(size)
                    return self.source.read(size)

            def tracked_open(selected: Path, *args: object, **kwargs: object):
                return BoundedReader(real_open(selected, *args, **kwargs))

            with patch("minirec.settings.Path.open", tracked_open):
                self.assertEqual(AppSettings(), SettingsStore(path).load())
            self.assertEqual([MAX_SETTINGS_BYTES + 1], requested_sizes)

    def test_non_json_future_value_and_oversized_save_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            with self.assertRaises(SettingsFormatError):
                store.save(AppSettings(extra={"bad": object()}))
            with self.assertRaises(SettingsFormatError):
                store.save(AppSettings(extra={"huge": "x" * MAX_SETTINGS_BYTES}))

    def test_failed_replace_preserves_previous_file_and_removes_temporary(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            store.save(AppSettings().with_changes(language=AppLanguage.ENGLISH))
            original = path.read_bytes()
            with patch("minirec.settings.os.replace", side_effect=OSError("test")):
                with self.assertRaises(SettingsWriteError):
                    store.update(language=AppLanguage.CZECH)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([], list(path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
