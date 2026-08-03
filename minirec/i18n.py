"""Small, deterministic Czech/English translation layer for MiniRec.

The UI deliberately does not depend on gettext catalogs.  Keeping both bundled
languages in one checked mapping makes an in-application language change
immediate and lets offline tests prove that neither language is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
import locale
import math
from typing import Final, Mapping


LANGUAGE_SYSTEM: Final = "system"
LANGUAGE_ENGLISH: Final = "en"
LANGUAGE_CZECH: Final = "cs"
LANGUAGE_CHOICES: Final[tuple[str, ...]] = (
    LANGUAGE_SYSTEM,
    LANGUAGE_ENGLISH,
    LANGUAGE_CZECH,
)

_ENGLISH_MONTHS: Final[tuple[str, ...]] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


_ENGLISH: Final[dict[str, str]] = {
    "app_name": "MiniRec",
    "close": "Close",
    "cancel": "Cancel",
    "error": "Error",
    "loading": "Loading…",
    "not_available": "Not available",
    "main_heading": "Audio recorder",
    "main_menu": "Main menu",
    "menu_settings": "Settings",
    "menu_recordings": "Recordings",
    "menu_open_folder": "Open recordings folder",
    "menu_thank_author": "Thank the author",
    "remaining_label": "Estimated time remaining",
    "elapsed_label": "Elapsed time",
    "status_label": "Recording status",
    "status_idle": "Ready to record",
    "status_starting": "Starting recording…",
    "status_recording": "Recording",
    "status_pausing": "Pausing recording…",
    "status_paused": "Recording paused",
    "status_resuming": "Resuming recording…",
    "status_stopping": "Stopping and saving recording…",
    "status_stopped": "Recording saved",
    "status_finalizing": "Saving recording…",
    "status_startup_recovery": "Checking interrupted recordings…",
    "status_error": "Recording failed",
    "status_error_detail": "Recording failed: {message}",
    "status_storage_reserve_stop": "Storage reserve reached; stopping and saving the recording.",
    "status_recording_saved": "Recording saved as {name}",
    "status_recording_recovered": "A safe prefix was recovered as {name}",
    "status_recording_cancelled": "Recording was cancelled before capture started",
    "status_recording_renamed": "Recording renamed to {name}",
    "status_delete_one": "Recording deleted",
    "status_delete_many": "{count} recordings deleted",
    "status_delete_partial": "Deleted recordings: {deleted}; changed or missing recordings left untouched: {skipped}.",
    "status_source_fallback": "The preferred microphone source was unavailable; MiniRec is using a compatible fallback.",
    "status_channel_fallback": "Stereo input was unavailable; recording continues in mono.",
    "status_signal_error": "The recording control sound could not be played; recording continues.",
    "status_with_technical_detail": "{message} Technical details: {details}",
    "error_not_enough_space": "There is not enough free space beyond the safety reserve.",
    "error_pending_retained": "{message} The pending file was retained when its safety could not be proven.",
    "error_recovery_failed": "{message} Recovery also failed: {details}",
    "error_recording_start": "Recording could not be started",
    "error_recording_start_detail": "Recording could not be started: {message}",
    "error_recording_prepare": "Recording storage could not be prepared: {message}",
    "error_recording_cancel": "The prepared recording could not be cancelled safely: {message}",
    "error_startup_busy": "Startup recovery is still in progress. Try recording again when it finishes.",
    "error_publication": "The finalized recording could not be published: {message}",
    "error_publication_missing": "Recording publication state is missing",
    "error_recording_generic": "Recording failed",
    "error_recording_missing": "The selected recording is no longer available",
    "error_delete_limit": "At most {count} recordings may be deleted at once",
    "error_library_busy": "Another recording-library operation is still in progress",
    "error_rename": "The recording could not be renamed: {message}",
    "error_rename_empty": "The recording name must not be empty.",
    "error_rename_invalid": "The recording name contains a reserved or invalid character.",
    "error_rename_too_long": "The recording name is too long.",
    "error_rename_conflict": "A recording with that name already exists.",
    "error_recording_changed": "The recording changed or is no longer available; it was left untouched.",
    "error_player_preparing": "Wait for the recording to finish loading before renaming or deleting it.",
    "error_rename_no_result": "The rename did not return a destination path",
    "error_delete": "The recording could not be deleted: {message}",
    "error_delete_no_result": "The delete operation did not return a result",
    "error_delete_partial": "{deleted} recordings were deleted, but {skipped} changed or missing recordings were left untouched.",
    "error_playback_start": "Playback could not start",
    "startup_settings_failed": "Settings could not be loaded or preserved: {message}",
    "startup_settings_restored": "Invalid settings were preserved and defaults restored: {message}",
    "startup_recovery_failed": "Recording recovery failed: {message}",
    "startup_recordings_recovered": "Recovered recordings: {name}",
    "and_more": " and {count} more",
    "startup_uncertain": "Some interrupted operations were left untouched because their file identity could not be verified.",
    "instance_busy_title": "MiniRec is already running",
    "instance_busy_detail": "Another MiniRec process is using the recording storage. Close that process before starting a second instance.",
    "instance_fatal_title": "MiniRec cannot access its storage",
    "instance_fatal_detail": "MiniRec could not safely lock its recovery state. No recording operation was started.",
    "action_record": "Record",
    "action_pause": "Pause",
    "action_resume": "Resume",
    "action_stop": "Stop",
    "quit_recording_title": "Recording is still in progress",
    "quit_recording_body": "Stop and safely save the recording before MiniRec quits?",
    "keep_recording": "Keep recording",
    "stop_and_quit": "Stop, save and quit",
    "settings_title": "MiniRec settings",
    "settings_heading": "Settings",
    "recording_settings_heading": "Recording",
    "language_label": "Language",
    "language_system": "System language",
    "language_en": "English",
    "language_cs": "Czech",
    "format_label": "Audio format",
    "format_ogg_opus": "Ogg Opus (.oga)",
    "format_mp3": "MP3 (.mp3)",
    "format_wav": "WAV PCM16 (.wav)",
    "bitrate_label": "Bitrate",
    "bitrate_value": "{bitrate} kb/s",
    "bitrate_compressed_help": "Target bitrate for Ogg Opus and MP3 recordings",
    "bitrate_unavailable_wav": "Bitrate is not used for uncompressed WAV",
    "channels_heading": "Channels",
    "channel_mono": "Mono",
    "channel_stereo": "Stereo",
    "gain_label": "Microphone gain",
    "gain_range": "From −12 dB to +12 dB",
    "prevent_sleep": "Prevent automatic sleep while recording",
    "application_details_heading": "Application details",
    "version_label": "Version",
    "recordings_location_label": "Recordings location",
    "recordings_title": "MiniRec recordings",
    "recordings_heading": "Recordings",
    "recordings_list_label": "Recordings; activate a row to open its player",
    "recordings_loading": "Loading recordings…",
    "library_operation_in_progress": "Updating the recording library…",
    "refresh": "Refresh",
    "open_folder": "Open folder",
    "clear_selection": "Clear selection",
    "delete_selected": "Delete selected",
    "selection_none": "No recordings selected",
    "selection_one": "1 recording selected",
    "selection_many": "{count} recordings selected",
    "selection_limit": "At most 500 recordings can be selected",
    "selection_limit_reached": "Selection limit reached. At most 500 recordings can be selected.",
    "recordings_empty": "There are no recordings yet.",
    "recordings_error": "Recordings could not be loaded: {message}",
    "recording_untitled": "Untitled recording",
    "recording_details": "{date}, {duration}, {size}, {format}",
    "recording_open": "Open player: {name}",
    "recording_row": "{name}. {details}. Activate the row to open its player.",
    "rename": "Rename",
    "delete": "Delete",
    "select": "Select",
    "deselect": "Deselect",
    "selected_suffix": "selected",
    "player_title": "MiniRec player",
    "player_heading": "Recording player",
    "playback_controls_heading": "Playback controls",
    "action_play": "Play",
    "action_player_pause": "Pause",
    "action_rewind_10": "Back 10 seconds",
    "action_forward_10": "Forward 10 seconds",
    "seek_label": "Playback position",
    "speed_label": "Playback speed",
    "speed_value": "{speed}×",
    "player_time": "{position} of {duration}",
    "player_time_unknown": "{position}, total duration unknown",
    "player_status_ready": "Ready to play",
    "player_status_playing": "Playing",
    "player_status_paused": "Playback paused",
    "player_status_ended": "Playback finished",
    "player_status_loading": "Loading recording…",
    "player_status_error": "Playback failed: {message}",
    "rename_title": "Rename recording",
    "rename_heading": "Rename recording",
    "rename_name_label": "New name",
    "rename_submit": "Rename",
    "rename_empty": "Enter a recording name.",
    "rename_help": "Enter a new file name. MiniRec preserves the recording format.",
    "rename_in_progress": "Renaming recording…",
    "delete_title": "Delete recording?",
    "delete_body": "Delete “{name}” permanently? This cannot be undone.",
    "delete_selected_title": "Delete selected recordings?",
    "delete_selected_body": "Delete {count} selected recordings permanently? This cannot be undone.",
    "delete_confirm": "Delete permanently",
}


_CZECH: Final[dict[str, str]] = {
    "app_name": "MiniRec",
    "close": "Zavřít",
    "cancel": "Zrušit",
    "error": "Chyba",
    "loading": "Načítání…",
    "not_available": "Není k dispozici",
    "main_heading": "Záznam zvuku",
    "main_menu": "Hlavní nabídka",
    "menu_settings": "Nastavení",
    "menu_recordings": "Nahrávky",
    "menu_open_folder": "Otevřít složku s nahrávkami",
    "menu_thank_author": "Poděkovat autorovi",
    "remaining_label": "Odhad zbývajícího času",
    "elapsed_label": "Uplynulý čas",
    "status_label": "Stav nahrávání",
    "status_idle": "Připraveno k nahrávání",
    "status_starting": "Spouštění nahrávání…",
    "status_recording": "Nahrávání probíhá",
    "status_pausing": "Pozastavování nahrávání…",
    "status_paused": "Nahrávání je pozastaveno",
    "status_resuming": "Obnovování nahrávání…",
    "status_stopping": "Zastavování a ukládání nahrávky…",
    "status_stopped": "Nahrávka byla uložena",
    "status_finalizing": "Ukládání nahrávky…",
    "status_startup_recovery": "Kontrola přerušených nahrávek…",
    "status_error": "Nahrávání se nezdařilo",
    "status_error_detail": "Nahrávání se nezdařilo: {message}",
    "status_storage_reserve_stop": "Byla dosažena bezpečnostní rezerva úložiště; nahrávání se zastavuje a ukládá.",
    "status_recording_saved": "Nahrávka byla uložena jako {name}",
    "status_recording_recovered": "Bezpečná část nahrávky byla obnovena jako {name}",
    "status_recording_cancelled": "Nahrávání bylo zrušeno před zahájením záznamu",
    "status_recording_renamed": "Nahrávka byla přejmenována na {name}",
    "status_delete_one": "Nahrávka byla smazána",
    "status_delete_many": "Smazané nahrávky: {count}",
    "status_delete_partial": "Smazané nahrávky: {deleted}; změněné nebo chybějící nahrávky ponechané beze změny: {skipped}.",
    "status_source_fallback": "Upřednostňovaný zdroj mikrofonu není dostupný; MiniRec používá kompatibilní náhradní zdroj.",
    "status_channel_fallback": "Stereofonní vstup není dostupný; nahrávání pokračuje monofonně.",
    "status_signal_error": "Ovládací zvuk nahrávání se nepodařilo přehrát; nahrávání pokračuje.",
    "status_with_technical_detail": "{message} Technické podrobnosti: {details}",
    "error_not_enough_space": "Nad rámec bezpečnostní rezervy není dostatek volného místa.",
    "error_pending_retained": "{message} Rozpracovaný soubor zůstal zachován, protože jeho bezpečnost nebylo možné prokázat.",
    "error_recovery_failed": "{message} Nezdařila se ani obnova: {details}",
    "error_recording_start": "Nahrávání se nepodařilo spustit",
    "error_recording_start_detail": "Nahrávání se nepodařilo spustit: {message}",
    "error_recording_prepare": "Úložiště se nepodařilo připravit pro nahrávání: {message}",
    "error_recording_cancel": "Připravené nahrávání se nepodařilo bezpečně zrušit: {message}",
    "error_startup_busy": "Obnova po spuštění stále probíhá. Zkuste nahrávání znovu po jejím dokončení.",
    "error_publication": "Dokončenou nahrávku se nepodařilo zveřejnit: {message}",
    "error_publication_missing": "Chybí stav potřebný ke zveřejnění nahrávky",
    "error_recording_generic": "Nahrávání se nezdařilo",
    "error_recording_missing": "Vybraná nahrávka již není k dispozici",
    "error_delete_limit": "Najednou lze smazat nejvýše {count} nahrávek",
    "error_library_busy": "Stále probíhá jiná operace s knihovnou nahrávek",
    "error_rename": "Nahrávku se nepodařilo přejmenovat: {message}",
    "error_rename_empty": "Název nahrávky nesmí být prázdný.",
    "error_rename_invalid": "Název nahrávky obsahuje rezervovaný nebo neplatný znak.",
    "error_rename_too_long": "Název nahrávky je příliš dlouhý.",
    "error_rename_conflict": "Nahrávka s tímto názvem již existuje.",
    "error_recording_changed": "Nahrávka se změnila nebo již není dostupná; zůstala beze změny.",
    "error_player_preparing": "Před přejmenováním nebo smazáním počkejte na dokončení načítání nahrávky.",
    "error_rename_no_result": "Přejmenování nevrátilo cílovou cestu",
    "error_delete": "Nahrávku se nepodařilo smazat: {message}",
    "error_delete_no_result": "Mazání nevrátilo výsledek operace",
    "error_delete_partial": "Smazané nahrávky: {deleted}; změněné nebo chybějící nahrávky ponechané beze změny: {skipped}.",
    "error_playback_start": "Přehrávání se nepodařilo spustit",
    "startup_settings_failed": "Nastavení se nepodařilo načíst ani zachovat: {message}",
    "startup_settings_restored": "Neplatné nastavení bylo zachováno a byly obnoveny výchozí hodnoty: {message}",
    "startup_recovery_failed": "Obnova nahrávek se nezdařila: {message}",
    "startup_recordings_recovered": "Obnovené nahrávky: {name}",
    "and_more": " a další ({count})",
    "startup_uncertain": "Některé přerušené operace zůstaly beze změny, protože nebylo možné ověřit identitu souborů.",
    "instance_busy_title": "MiniRec je již spuštěný",
    "instance_busy_detail": "Úložiště nahrávek používá jiný proces MiniRec. Před spuštěním druhé instance tento proces ukončete.",
    "instance_fatal_title": "MiniRec nemůže přistupovat ke svému úložišti",
    "instance_fatal_detail": "MiniRec nemohl bezpečně uzamknout stav obnovy. Nebyla spuštěna žádná operace s nahrávkou.",
    "action_record": "Nahrát",
    "action_pause": "Pozastavit",
    "action_resume": "Pokračovat",
    "action_stop": "Stop",
    "quit_recording_title": "Nahrávání stále probíhá",
    "quit_recording_body": "Před ukončením MiniRec nahrávání zastavit a bezpečně uložit?",
    "keep_recording": "Pokračovat v nahrávání",
    "stop_and_quit": "Zastavit, uložit a ukončit",
    "settings_title": "Nastavení MiniRec",
    "settings_heading": "Nastavení",
    "recording_settings_heading": "Nahrávání",
    "language_label": "Jazyk",
    "language_system": "Jazyk systému",
    "language_en": "Angličtina",
    "language_cs": "Čeština",
    "format_label": "Formát zvuku",
    "format_ogg_opus": "Ogg Opus (.oga)",
    "format_mp3": "MP3 (.mp3)",
    "format_wav": "WAV PCM16 (.wav)",
    "bitrate_label": "Datový tok",
    "bitrate_value": "{bitrate} kb/s",
    "bitrate_compressed_help": "Cílový datový tok pro nahrávky Ogg Opus a MP3",
    "bitrate_unavailable_wav": "Datový tok se pro nekomprimovaný WAV nepoužívá",
    "channels_heading": "Kanály",
    "channel_mono": "Mono",
    "channel_stereo": "Stereo",
    "gain_label": "Zesílení mikrofonu",
    "gain_range": "Od −12 dB do +12 dB",
    "prevent_sleep": "Během nahrávání zabránit automatickému uspání",
    "application_details_heading": "Informace o aplikaci",
    "version_label": "Verze",
    "recordings_location_label": "Umístění nahrávek",
    "recordings_title": "Nahrávky MiniRec",
    "recordings_heading": "Nahrávky",
    "recordings_list_label": "Nahrávky; aktivací řádku otevřete přehrávač",
    "recordings_loading": "Načítání nahrávek…",
    "library_operation_in_progress": "Aktualizace knihovny nahrávek…",
    "refresh": "Obnovit",
    "open_folder": "Otevřít složku",
    "clear_selection": "Zrušit výběr",
    "delete_selected": "Smazat vybrané",
    "selection_none": "Není vybrána žádná nahrávka",
    "selection_one": "Vybrána 1 nahrávka",
    "selection_many": "Vybrané nahrávky: {count}",
    "selection_limit": "Lze vybrat nejvýše 500 nahrávek",
    "selection_limit_reached": "Byl dosažen limit výběru. Lze vybrat nejvýše 500 nahrávek.",
    "recordings_empty": "Zatím zde nejsou žádné nahrávky.",
    "recordings_error": "Nahrávky se nepodařilo načíst: {message}",
    "recording_untitled": "Nahrávka bez názvu",
    "recording_details": "{date}, {duration}, {size}, {format}",
    "recording_open": "Otevřít přehrávač: {name}",
    "recording_row": "{name}. {details}. Aktivací řádku otevřete přehrávač.",
    "rename": "Přejmenovat",
    "delete": "Smazat",
    "select": "Vybrat",
    "deselect": "Zrušit výběr",
    "selected_suffix": "vybráno",
    "player_title": "Přehrávač MiniRec",
    "player_heading": "Přehrávač nahrávky",
    "playback_controls_heading": "Ovládání přehrávání",
    "action_play": "Přehrát",
    "action_player_pause": "Pozastavit",
    "action_rewind_10": "Zpět o 10 sekund",
    "action_forward_10": "Vpřed o 10 sekund",
    "seek_label": "Pozice přehrávání",
    "speed_label": "Rychlost přehrávání",
    "speed_value": "{speed}×",
    "player_time": "{position} z {duration}",
    "player_time_unknown": "{position}, celková délka není známa",
    "player_status_ready": "Připraveno k přehrávání",
    "player_status_playing": "Přehrávání probíhá",
    "player_status_paused": "Přehrávání je pozastaveno",
    "player_status_ended": "Přehrávání skončilo",
    "player_status_loading": "Načítání nahrávky…",
    "player_status_error": "Přehrávání se nezdařilo: {message}",
    "rename_title": "Přejmenovat nahrávku",
    "rename_heading": "Přejmenovat nahrávku",
    "rename_name_label": "Nový název",
    "rename_submit": "Přejmenovat",
    "rename_empty": "Zadejte název nahrávky.",
    "rename_help": "Zadejte nový název souboru. MiniRec zachová formát nahrávky.",
    "rename_in_progress": "Přejmenovávání nahrávky…",
    "delete_title": "Smazat nahrávku?",
    "delete_body": "Trvale smazat „{name}“? Tuto akci nelze vrátit.",
    "delete_selected_title": "Smazat vybrané nahrávky?",
    "delete_selected_body": "Trvale smazat vybrané nahrávky ({count})? Tuto akci nelze vrátit.",
    "delete_confirm": "Trvale smazat",
}


TRANSLATIONS: Final[Mapping[str, Mapping[str, str]]] = {
    LANGUAGE_ENGLISH: _ENGLISH,
    LANGUAGE_CZECH: _CZECH,
}


def translation_keys(language: str) -> frozenset[str]:
    """Return the immutable set of keys for a concrete bundled language."""

    try:
        return frozenset(TRANSLATIONS[language])
    except KeyError as error:
        raise ValueError(f"Unsupported concrete language: {language}") from error


def validate_translation_catalogs() -> None:
    """Raise if Czech and English do not expose exactly the same keys."""

    english = translation_keys(LANGUAGE_ENGLISH)
    czech = translation_keys(LANGUAGE_CZECH)
    if english != czech:
        missing_czech = sorted(english - czech)
        missing_english = sorted(czech - english)
        raise RuntimeError(
            "Translation key mismatch; "
            f"missing Czech={missing_czech}, missing English={missing_english}"
        )


def resolve_language(choice: str, system_locale: str | None = None) -> str:
    """Resolve ``system``/``en``/``cs`` to one bundled concrete language."""

    normalized_choice = (choice or "").strip().casefold()
    if normalized_choice not in LANGUAGE_CHOICES:
        raise ValueError(f"Unsupported language choice: {choice!r}")
    if normalized_choice != LANGUAGE_SYSTEM:
        return normalized_choice
    locale_name = system_locale
    if locale_name is None:
        try:
            locale_name = locale.getlocale()[0]
        except (ValueError, TypeError):
            locale_name = None
    normalized_locale = (locale_name or "").replace("-", "_").casefold()
    return LANGUAGE_CZECH if normalized_locale.split("_", 1)[0] == "cs" else LANGUAGE_ENGLISH


@dataclass(slots=True)
class Translator:
    """Translate keys using a persisted language choice.

    ``system_locale`` is injectable so tests and the application can resolve a
    stable desktop locale without mutating process-wide locale state.
    """

    language: str = LANGUAGE_SYSTEM
    system_locale: str | None = None

    def __post_init__(self) -> None:
        if self.language not in LANGUAGE_CHOICES:
            raise ValueError(f"Unsupported language choice: {self.language!r}")

    @property
    def resolved_language(self) -> str:
        return resolve_language(self.language, self.system_locale)

    def set_language(self, language: str) -> None:
        if language not in LANGUAGE_CHOICES:
            raise ValueError(f"Unsupported language choice: {language!r}")
        self.language = language

    def t(self, key: str, **values: object) -> str:
        """Translate *key* and interpolate named placeholders."""

        catalog = TRANSLATIONS[self.resolved_language]
        try:
            template = catalog[key]
        except KeyError as error:
            raise KeyError(f"Unknown translation key: {key}") from error
        return template.format(**values)

    __call__ = t

    def format_remaining(self, seconds: int | float | None) -> str:
        if seconds is None or not math.isfinite(float(seconds)):
            return self.t("not_available")
        return format_duration(seconds)

    def format_recording_count(self, count: int) -> str:
        if count <= 0:
            return self.t("selection_none")
        if count == 1:
            return self.t("selection_one")
        return self.t("selection_many", count=count)

    def format_recording_date(
        self,
        modified_ns: int | None,
        *,
        timezone: tzinfo | None = None,
    ) -> str:
        """Format a recording timestamp in the active bundled language."""

        return format_recording_date(
            modified_ns,
            self.resolved_language,
            timezone=timezone,
        )


def format_recording_date(
    modified_ns: int | None,
    language: str,
    *,
    timezone: tzinfo | None = None,
) -> str:
    """Format a nanosecond POSIX timestamp without consulting process locale.

    The default uses the user's local timezone, as recording lists normally
    do.  Supplying ``timezone`` makes offline tests and other deterministic
    consumers independent of the machine timezone.
    """

    if (
        modified_ns is None
        or isinstance(modified_ns, bool)
        or not isinstance(modified_ns, int)
        or modified_ns < 0
    ):
        return "—"
    if language not in {LANGUAGE_ENGLISH, LANGUAGE_CZECH}:
        raise ValueError(f"Unsupported resolved language: {language!r}")
    try:
        value = datetime.fromtimestamp(
            modified_ns // 1_000_000_000,
            tz=timezone,
        )
    except (OverflowError, OSError, ValueError):
        return "—"
    if language == LANGUAGE_CZECH:
        return (
            f"{value.day}. {value.month}. {value.year} "
            f"{value.hour:02d}:{value.minute:02d}"
        )
    hour = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return (
        f"{_ENGLISH_MONTHS[value.month - 1]} {value.day}, {value.year}, "
        f"{hour}:{value.minute:02d} {period}"
    )


def format_duration(seconds: int | float | None) -> str:
    """Format non-negative seconds as ``m:ss`` or ``h:mm:ss``."""

    if seconds is None:
        return "--:--"
    numeric = float(seconds)
    if not math.isfinite(numeric):
        return "--:--"
    total = max(0, int(numeric))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_file_size(size_bytes: int | None) -> str:
    """Format a byte count compactly using deterministic IEC units."""

    if size_bytes is None or size_bytes < 0:
        return "—"
    size = float(size_bytes)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024.0 or candidate == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def format_gain_db(gain_db: int | float) -> str:
    """Format microphone gain with an explicit sign and a true minus sign."""

    numeric = float(gain_db)
    if not math.isfinite(numeric):
        raise ValueError("gain_db must be finite")
    rounded = int(numeric) if numeric.is_integer() else numeric
    if numeric > 0:
        return f"+{rounded} dB"
    if numeric < 0:
        return f"−{abs(rounded)} dB"
    return "0 dB"


def format_speed(speed: float) -> str:
    """Format one of MiniRec's playback rates without redundant zeroes."""

    numeric = float(speed)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError("speed must be finite and positive")
    return f"{numeric:g}×"


def format_player_time(position_seconds: int | float, duration_seconds: int | float | None) -> str:
    """Return a compact visual position/total pair."""

    position = format_duration(position_seconds)
    duration = format_duration(duration_seconds)
    return f"{position} / {duration}"


validate_translation_catalogs()
