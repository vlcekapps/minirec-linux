from __future__ import annotations

import math
from pathlib import Path
import unittest

from minirec.models import (
    BITRATE_OPTIONS_KBPS,
    ChannelMode,
    DEFAULT_RECORDING_FORMAT,
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    RecordingFormat,
    RecordingSettings,
)


class RecordingFormatTest(unittest.TestCase):
    def test_linux_default_is_ogg_opus_with_audio_extension(self) -> None:
        self.assertIs(RecordingFormat.OGG_OPUS, DEFAULT_RECORDING_FORMAT)
        self.assertEqual(".oga", RecordingFormat.OGG_OPUS.extension)
        self.assertEqual("audio/ogg", RecordingFormat.OGG_OPUS.mime_type)

    def test_all_formats_have_stable_identity_and_expected_extension(self) -> None:
        self.assertEqual(
            {
                RecordingFormat.OGG_OPUS: ("opus", ".oga", "audio/ogg"),
                RecordingFormat.MP3: ("mp3", ".mp3", "audio/mpeg"),
                RecordingFormat.WAV: ("wav", ".wav", "audio/wav"),
            },
            {
                item: (item.storage_value, item.extension, item.mime_type)
                for item in RecordingFormat
            },
        )

    def test_storage_reader_accepts_opus_aliases_and_defaults_safely(self) -> None:
        for value in ("opus", "OGG", "oga", "ogg_opus"):
            self.assertIs(RecordingFormat.OGG_OPUS, RecordingFormat.from_storage_value(value))
        self.assertIs(RecordingFormat.MP3, RecordingFormat.from_storage_value("MP3"))
        self.assertIs(RecordingFormat.OGG_OPUS, RecordingFormat.from_storage_value("unknown"))
        self.assertIs(RecordingFormat.OGG_OPUS, RecordingFormat.from_storage_value(None))

    def test_filename_and_mime_detection_are_case_insensitive(self) -> None:
        self.assertIs(RecordingFormat.OGG_OPUS, RecordingFormat.from_filename("Voice.OGA"))
        self.assertIs(RecordingFormat.MP3, RecordingFormat.from_filename(Path("voice.MP3")))
        self.assertIsNone(RecordingFormat.from_filename("voice.txt"))
        self.assertIs(RecordingFormat.WAV, RecordingFormat.from_mime_type("Audio/X-Wav; rate=48000"))
        self.assertIs(RecordingFormat.OGG_OPUS, RecordingFormat.from_mime_type("audio/opus"))
        self.assertIsNone(RecordingFormat.from_mime_type("video/ogg"))

    def test_only_wav_is_uncompressed(self) -> None:
        self.assertTrue(RecordingFormat.OGG_OPUS.is_compressed)
        self.assertTrue(RecordingFormat.MP3.is_compressed)
        self.assertFalse(RecordingFormat.WAV.is_compressed)


class RecordingSettingsTest(unittest.TestCase):
    def test_exact_bitrate_options_match_android_parity_request(self) -> None:
        self.assertEqual((32, 48, 64, 96, 128, 160, 192, 256, 320), BITRATE_OPTIONS_KBPS)
        for bitrate in BITRATE_OPTIONS_KBPS:
            self.assertEqual(bitrate, RecordingSettings(bitrate_kbps=bitrate).bitrate_kbps)

    def test_default_requests_stereo_opus_at_128_kbps_and_zero_gain(self) -> None:
        settings = RecordingSettings()
        self.assertIs(RecordingFormat.OGG_OPUS, settings.format)
        self.assertEqual(128, settings.bitrate_kbps)
        self.assertIs(ChannelMode.STEREO, settings.channel_mode)
        self.assertEqual(2, settings.channels)
        self.assertEqual(0, settings.gain_db)
        self.assertEqual(1.0, settings.linear_gain)

    def test_gain_bounds_and_decibel_conversion(self) -> None:
        quiet = RecordingSettings(gain_db=MIN_GAIN_DB)
        loud = RecordingSettings(gain_db=MAX_GAIN_DB)
        self.assertTrue(math.isclose(10 ** (-12 / 20), quiet.linear_gain))
        self.assertTrue(math.isclose(10 ** (12 / 20), loud.linear_gain))
        with self.assertRaises(ValueError):
            RecordingSettings(gain_db=MIN_GAIN_DB - 1)
        with self.assertRaises(ValueError):
            RecordingSettings(gain_db=MAX_GAIN_DB + 1)

    def test_invalid_bitrate_is_rejected_instead_of_silently_changed(self) -> None:
        with self.assertRaisesRegex(ValueError, "bitrate_kbps"):
            RecordingSettings(bitrate_kbps=100)

    def test_channel_mode_conversion_is_exact(self) -> None:
        self.assertIs(ChannelMode.MONO, ChannelMode.from_channels(1))
        self.assertIs(ChannelMode.STEREO, ChannelMode.from_channels(2))
        with self.assertRaises(ValueError):
            ChannelMode.from_channels(6)

        stereo = RecordingSettings(channel_mode=ChannelMode.STEREO)
        mono = stereo.with_channels(1)
        self.assertIs(ChannelMode.MONO, mono.channel_mode)
        self.assertIs(ChannelMode.STEREO, stereo.channel_mode)

    def test_types_are_not_implicitly_coerced(self) -> None:
        with self.assertRaises(TypeError):
            RecordingSettings(format="opus")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RecordingSettings(channel_mode=2)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RecordingSettings(gain_db=True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RecordingSettings(bitrate_kbps=128.0)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RecordingSettings(bitrate_kbps=True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ChannelMode.from_channels(True)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
