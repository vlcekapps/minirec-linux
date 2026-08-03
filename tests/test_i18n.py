from __future__ import annotations

from datetime import timezone
from string import Formatter
import math
import unittest

from minirec.i18n import (
    LANGUAGE_CZECH,
    LANGUAGE_ENGLISH,
    LANGUAGE_SYSTEM,
    TRANSLATIONS,
    Translator,
    format_duration,
    format_file_size,
    format_gain_db,
    format_player_time,
    format_recording_date,
    format_speed,
    resolve_language,
    translation_keys,
    validate_translation_catalogs,
)


class TranslationCatalogTest(unittest.TestCase):
    def test_czech_and_english_catalogs_are_exactly_complete(self) -> None:
        validate_translation_catalogs()
        self.assertEqual(
            translation_keys(LANGUAGE_ENGLISH),
            translation_keys(LANGUAGE_CZECH),
        )
        self.assertGreaterEqual(len(translation_keys(LANGUAGE_ENGLISH)), 100)

    def test_every_template_accepts_its_documented_named_fields(self) -> None:
        samples: dict[str, object] = {
            "bitrate": 128,
            "count": 3,
            "date": "August 3, 2026, 10:11 AM",
            "deleted": 1,
            "details": "August 3, 2026, 10:11 AM, 0:12, 2.0 KiB, MP3 (.mp3)",
            "duration": "1:02",
            "format": "MP3 (.mp3)",
            "message": "test detail",
            "name": "recording.oga",
            "position": "0:10",
            "size": "2.0 KiB",
            "speed": 1.25,
            "skipped": 2,
        }
        formatter = Formatter()
        for language, catalog in TRANSLATIONS.items():
            for key, template in catalog.items():
                fields = {
                    field_name
                    for _literal, field_name, _spec, _conversion in formatter.parse(template)
                    if field_name
                }
                with self.subTest(language=language, key=key):
                    self.assertLessEqual(fields, samples.keys())
                    self.assertTrue(template.format(**samples))

    def test_system_language_resolves_czech_spelling_variants(self) -> None:
        for locale_name in ("cs_CZ", "cs-CZ", "CS_cz.UTF-8", "cs"):
            self.assertEqual(
                LANGUAGE_CZECH,
                resolve_language(LANGUAGE_SYSTEM, locale_name),
            )
        for locale_name in (None, "en_US", "de_DE", ""):
            if locale_name is None:
                continue
            self.assertEqual(
                LANGUAGE_ENGLISH,
                resolve_language(LANGUAGE_SYSTEM, locale_name),
            )

    def test_explicit_language_ignores_system_locale(self) -> None:
        self.assertEqual(
            LANGUAGE_ENGLISH, resolve_language(LANGUAGE_ENGLISH, "cs_CZ")
        )
        self.assertEqual(
            LANGUAGE_CZECH, resolve_language(LANGUAGE_CZECH, "en_US")
        )
        with self.assertRaises(ValueError):
            resolve_language("sk", "sk_SK")

    def test_translator_changes_language_without_process_locale_mutation(self) -> None:
        translator = Translator(LANGUAGE_SYSTEM, system_locale="cs_CZ")
        self.assertEqual("Nahrávky", translator("recordings_heading"))
        translator.set_language(LANGUAGE_ENGLISH)
        self.assertEqual("Recordings", translator("recordings_heading"))
        with self.assertRaises(KeyError):
            translator("missing_key")

    def test_count_and_unknown_remaining_are_localized(self) -> None:
        english = Translator(LANGUAGE_ENGLISH)
        czech = Translator(LANGUAGE_CZECH)
        self.assertEqual("No recordings selected", english.format_recording_count(0))
        self.assertEqual("Vybrána 1 nahrávka", czech.format_recording_count(1))
        self.assertEqual("3 recordings selected", english.format_recording_count(3))
        self.assertEqual("Není k dispozici", czech.format_remaining(math.inf))


class FormatterTest(unittest.TestCase):
    def test_duration_is_bounded_and_compact(self) -> None:
        self.assertEqual("--:--", format_duration(None))
        self.assertEqual("--:--", format_duration(math.nan))
        self.assertEqual("0:00", format_duration(-5))
        self.assertEqual("0:59", format_duration(59.99))
        self.assertEqual("1:00", format_duration(60))
        self.assertEqual("1:01:01", format_duration(3661))

    def test_file_size_uses_deterministic_iec_units(self) -> None:
        self.assertEqual("—", format_file_size(None))
        self.assertEqual("—", format_file_size(-1))
        self.assertEqual("0 B", format_file_size(0))
        self.assertEqual("1023 B", format_file_size(1023))
        self.assertEqual("1.0 KiB", format_file_size(1024))
        self.assertEqual("1.5 MiB", format_file_size(1572864))

    def test_recording_date_is_localized_without_process_locale(self) -> None:
        modified_ns = 1_785_751_872_000_000_000
        self.assertEqual(
            "August 3, 2026, 10:11 AM",
            format_recording_date(
                modified_ns,
                LANGUAGE_ENGLISH,
                timezone=timezone.utc,
            ),
        )
        self.assertEqual(
            "3. 8. 2026 10:11",
            format_recording_date(
                modified_ns,
                LANGUAGE_CZECH,
                timezone=timezone.utc,
            ),
        )
        czech = Translator(LANGUAGE_SYSTEM, system_locale="cs_CZ")
        self.assertEqual(
            "3. 8. 2026 10:11",
            czech.format_recording_date(modified_ns, timezone=timezone.utc),
        )

    def test_invalid_recording_dates_have_a_stable_placeholder(self) -> None:
        for value in (None, -1, True, 10**100):
            with self.subTest(value=value):
                self.assertEqual(
                    "—",
                    format_recording_date(value, LANGUAGE_ENGLISH),
                )
        with self.assertRaises(ValueError):
            format_recording_date(0, LANGUAGE_SYSTEM, timezone=timezone.utc)

    def test_gain_and_speed_have_unambiguous_units(self) -> None:
        self.assertEqual("−12 dB", format_gain_db(-12))
        self.assertEqual("0 dB", format_gain_db(0))
        self.assertEqual("+6 dB", format_gain_db(6))
        self.assertEqual("+1.5 dB", format_gain_db(1.5))
        self.assertEqual("0.5×", format_speed(0.5))
        self.assertEqual("2×", format_speed(2.0))
        with self.assertRaises(ValueError):
            format_gain_db(math.nan)
        with self.assertRaises(ValueError):
            format_speed(0)

    def test_visual_player_pair_is_independent_of_localized_sentence(self) -> None:
        self.assertEqual("0:12 / 1:02", format_player_time(12, 62))
        self.assertEqual("0:12 / --:--", format_player_time(12, None))


if __name__ == "__main__":
    unittest.main()
