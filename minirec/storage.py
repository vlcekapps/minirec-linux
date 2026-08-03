"""Durable public recording storage independent of GTK and GStreamer.

MiniRec records into a private, pre-created pending file in the user's public
recordings directory.  A synchronized XDG-state journal binds that path to its
device and inode before capture starts.  Publication and destructive actions
therefore never rely on a reusable pathname alone and never overwrite another
file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import ctypes
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import threading
from typing import Final

from .models import RecordingFormat, RecordingSettings
from .recovery import (
    MAX_CLASSIC_WAV_FILE_BYTES,
    RecoveryIdentityError,
    inspect_recording,
    recover_recording,
)
from .settings import default_state_dir


RECORDINGS_DIRECTORY_ENV: Final = "MINIREC_RECORDINGS_DIR"
RECORDINGS_DIRECTORY_NAME: Final = "MiniRec"
MAX_RECORDING_NAME_BYTES: Final = 240
MAX_SELECTED_RECORDINGS: Final = 500
MINIMUM_SPACE_RESERVE_BYTES: Final = 64 * 1024 * 1024
JOURNAL_VERSION: Final = 1
DELETE_JOURNAL_VERSION: Final = 2
MAX_JOURNAL_BYTES: Final = 512 * 1024
PROCESS_LOCK_FILE_NAME: Final = "minirec.lock"
WAV_HEADER_ESTIMATE_BYTES: Final = 44
WAV_SAMPLE_RATE: Final = 48_000
PCM16_BYTES_PER_SAMPLE: Final = 2
_INVALID_FILENAME_CHARACTERS: Final = frozenset('"*/:<>?\\|')


class StorageError(OSError):
    """Base class for a recording storage operation that did not commit."""


class StorageIdentityError(StorageError):
    """A path no longer identifies the file reserved by MiniRec."""


class PendingJournalError(StorageError):
    """The durable pending-recording journal is missing or inconsistent."""


class RecordingNameError(StorageError, ValueError):
    """Base class for an invalid or conflicting recording name."""


class EmptyRecordingNameError(RecordingNameError):
    """The trimmed recording base name is empty."""


class InvalidRecordingNameError(RecordingNameError):
    """The name contains a control, path separator or reserved character."""


class RecordingNameTooLongError(RecordingNameError):
    """The complete UTF-8 filename exceeds the 240-byte policy limit."""


class RecordingNameConflictError(RecordingNameError, FileExistsError):
    """Another directory entry has the requested name, ignoring case."""


class SelectionLimitError(StorageError, ValueError):
    """A destructive operation exceeded the 500-recording selection limit."""


class StorageProcessLockError(StorageError):
    """Another MiniRec process owns the state directory."""


class StorageProcessLock:
    """Hold one non-blocking Linux advisory lock for a MiniRec state tree.

    D-Bus application uniqueness is scoped to one graphical session.  This
    lock additionally prevents a process in another session from treating a
    live pending recording as crash recovery.  The descriptor remains open
    until :meth:`close`; the small lock file itself intentionally persists.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = _absolute_normalized(Path(state_dir))
        self.path = self.state_dir / PROCESS_LOCK_FILE_NAME
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        _ensure_directory(self.state_dir)
        flags = (
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageProcessLockError(
                    "MiniRec process lock is not a regular file"
                )
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StorageProcessLockError(
                    "Another MiniRec process is already using this storage"
                ) from error
            self._descriptor = descriptor
        except Exception:
            os.close(descriptor)
            raise

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> StorageProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Filesystem identity persisted before a path may be mutated."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> FileIdentity:
        return cls(device=metadata.st_dev, inode=metadata.st_ino)

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == self.device
            and metadata.st_ino == self.inode
        )


@dataclass(frozen=True, slots=True)
class PendingRecording:
    """A journaled empty output prepared for ``Recorder(..., prepared=True)``."""

    path: Path
    final_path: Path
    journal_path: Path
    format: RecordingFormat
    identity: FileIdentity

    @property
    def prepared(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.final_path.name


@dataclass(frozen=True, slots=True)
class RecordingItem:
    """Presentation-neutral metadata for one published recording."""

    path: Path
    identity: FileIdentity
    name: str
    duration_seconds: float | None
    size_bytes: int
    modified_ns: int
    format: RecordingFormat

    @property
    def id(self) -> str:
        return str(self.path)

    @property
    def date_added_ms(self) -> int:
        return self.modified_ns // 1_000_000


class RecordingRecoveryStatus(str, Enum):
    RECOVERED = "recovered"
    COMPLETED = "completed"
    EMPTY_REMOVED = "empty_removed"
    MISSING = "missing"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class RecordingRecoveryOutcome:
    status: RecordingRecoveryStatus
    pending_path: Path | None
    final_path: Path | None
    journal_path: Path
    detail: str = ""


class DeleteReconciliationStatus(str, Enum):
    RECONCILED = "reconciled"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class DeleteReconciliation:
    """Observed result of an interrupted delete; no destructive step is retried."""

    status: DeleteReconciliationStatus
    journal_path: Path
    deleted_paths: tuple[Path, ...] = ()
    present_paths: tuple[Path, ...] = ()
    changed_paths: tuple[Path, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StartupRecoveryReport:
    recordings: tuple[RecordingRecoveryOutcome, ...]
    deletions: tuple[DeleteReconciliation, ...]

    @property
    def recovered_paths(self) -> tuple[Path, ...]:
        return tuple(
            outcome.final_path
            for outcome in self.recordings
            if outcome.status is RecordingRecoveryStatus.RECOVERED
            and outcome.final_path is not None
        )


@dataclass(frozen=True, slots=True)
class _JournaledPath:
    path: Path
    quarantine_path: Path | None
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class _RecordingJournal:
    pending_path: Path
    final_path: Path
    format: RecordingFormat
    identity: FileIdentity
    cleanup_path: Path | None = None
    cleanup_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _RenameJournal:
    source_path: Path
    target_path: Path
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class DeleteReservation:
    journal_path: Path
    entries: tuple[_JournaledPath, ...]


@dataclass(frozen=True, slots=True)
class DeleteResult:
    requested_count: int
    deleted_paths: tuple[Path, ...]
    skipped_paths: tuple[Path, ...]

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_paths)


def default_recordings_dir(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Resolve ``~/Recordings/MiniRec`` or an absolute test override."""

    values = os.environ if environment is None else environment
    override = values.get(RECORDINGS_DIRECTORY_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate
    resolved_home = Path.home() if home is None else Path(home).expanduser()
    return resolved_home / "Recordings" / RECORDINGS_DIRECTORY_NAME


def validate_recording_name(
    requested_name: str,
    recording_format: RecordingFormat,
) -> str:
    """Trim a user name, preserve the source format and return its filename."""

    if not isinstance(requested_name, str):
        raise TypeError("requested_name must be a string")
    if not isinstance(recording_format, RecordingFormat):
        raise TypeError("recording_format must be a RecordingFormat")
    base_name = requested_name.strip()
    supplied_format = RecordingFormat.from_filename(base_name)
    if supplied_format is not None:
        base_name = base_name[: -len(supplied_format.extension)].strip()
    if not base_name:
        raise EmptyRecordingNameError("Recording name must not be empty")
    if base_name.startswith(".") or any(
        character in _INVALID_FILENAME_CHARACTERS
        or ord(character) < 32
        or 127 <= ord(character) <= 159
        for character in base_name
    ):
        raise InvalidRecordingNameError("Recording name contains invalid characters")
    display_name = f"{base_name}{recording_format.extension}"
    if len(display_name.encode("utf-8")) > MAX_RECORDING_NAME_BYTES:
        raise RecordingNameTooLongError(
            f"Recording filename exceeds {MAX_RECORDING_NAME_BYTES} UTF-8 bytes"
        )
    return display_name


def space_reserve_bytes(available_bytes: int) -> int:
    """Keep the greater of 64 MiB and one percent of free space untouched."""

    if type(available_bytes) is not int or available_bytes < 0:
        raise ValueError("available_bytes must be a non-negative integer")
    return max(MINIMUM_SPACE_RESERVE_BYTES, available_bytes // 100)


def estimate_remaining_seconds(
    available_bytes: int,
    settings: RecordingSettings,
) -> int:
    """Conservatively estimate duration after reserve and classic-WAV limits."""

    if not isinstance(settings, RecordingSettings):
        raise TypeError("settings must be RecordingSettings")
    reserve = space_reserve_bytes(available_bytes)
    usable = max(0, available_bytes - reserve)
    if settings.format is not RecordingFormat.WAV:
        return usable // (settings.bitrate_kbps * 125)
    limited_file_size = min(usable, MAX_CLASSIC_WAV_FILE_BYTES)
    audio_bytes = max(0, limited_file_size - WAV_HEADER_ESTIMATE_BYTES)
    bytes_per_second = (
        WAV_SAMPLE_RATE * PCM16_BYTES_PER_SAMPLE * settings.channel_mode.channels
    )
    return audio_bytes // bytes_per_second


class RecordingStorage:
    """Own pending, published, recovery and delete policy for one directory."""

    def __init__(
        self,
        recordings_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        selected_recordings = (
            Path(recordings_dir)
            if recordings_dir is not None
            else default_recordings_dir(environment, home=home)
        )
        selected_state = (
            Path(state_dir)
            if state_dir is not None
            else default_state_dir(environment, home=home)
        )
        self.recordings_dir = _absolute_normalized(selected_recordings)
        self.state_dir = _absolute_normalized(selected_state)
        self._duration_cache: dict[
            tuple[int, int, int, int, RecordingFormat], float | None
        ] = {}
        self._duration_cache_lock = threading.RLock()

    def create_pending(
        self,
        recording_format: RecordingFormat = RecordingFormat.OGG_OPUS,
        *,
        name: str | None = None,
        now: datetime | None = None,
    ) -> PendingRecording:
        """Create an O_EXCL empty file and commit its identity before capture."""

        if not isinstance(recording_format, RecordingFormat):
            raise TypeError("recording_format must be a RecordingFormat")
        _ensure_directory(self.recordings_dir)
        _ensure_directory(self.state_dir)
        requested = name or self.default_recording_name(recording_format, now=now)
        display_name = validate_recording_name(requested, recording_format)
        final_path = self._unique_final_path(display_name)

        descriptor = -1
        pending_path: Path | None = None
        journal_path: Path | None = None
        identity: FileIdentity | None = None
        try:
            for _ in range(128):
                token = secrets.token_hex(12)
                pending_path = self.recordings_dir / f".minirec-{token}.pending"
                journal_path = self.state_dir / f"recording-{token}.json"
                if journal_path.exists():
                    continue
                flags = (
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_RDWR
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(pending_path, flags, 0o600)
                except FileExistsError:
                    continue
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
                    raise StorageError("Pending output is not an empty regular file")
                identity = FileIdentity.from_stat(metadata)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                _fsync_directory(self.recordings_dir)
                break
            else:
                raise StorageError("Could not allocate a unique pending recording")

            assert pending_path is not None
            assert journal_path is not None
            assert identity is not None
            payload = self._recording_journal_payload(
                pending_path, final_path, recording_format, identity
            )
            _atomic_json_write(journal_path, payload)
            return PendingRecording(
                path=pending_path,
                final_path=final_path,
                journal_path=journal_path,
                format=recording_format,
                identity=identity,
            )
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            # If the durable journal could not be committed there is no safe
            # pathname-based delete protocol to follow.  Fail closed and leave
            # the private, empty O_EXCL file behind instead of risking unlinking
            # a concurrently substituted path.  It is hidden and never listed.
            raise

    @staticmethod
    def default_recording_name(
        recording_format: RecordingFormat = RecordingFormat.OGG_OPUS,
        *,
        now: datetime | None = None,
    ) -> str:
        timestamp = datetime.now() if now is None else now
        return f"MiniRec_{timestamp:%Y-%m-%d_%H-%M-%S}{recording_format.extension}"

    def complete(self, pending: PendingRecording) -> Path:
        """After encoder EOS, fsync and atomically publish without replacement."""

        journal = self._matching_recording_journal(pending)
        self._sync_exact(pending.path, pending.identity)
        final_path = self._publish_pending(
            pending.path,
            pending.identity,
            pending.format,
            journal.final_path,
            pending.journal_path,
        )
        try:
            self._verify_exact_path(final_path, pending.identity, "Published recording")
        except StorageIdentityError:
            self._attempt_restore(final_path, pending.path, pending.identity)
            try:
                _fsync_directory(self.recordings_dir)
            except OSError:
                pass
            # Never clear the journal when the visible target is uncertain.
            raise
        self._remove_journal(pending.journal_path)
        return final_path

    def abort(self, pending: PendingRecording) -> None:
        """Remove only the exact unfinished file represented by *pending*."""

        journal = self._matching_recording_journal(pending)
        self._cleanup_recording_path(
            journal,
            pending.journal_path,
            reason="abort",
        )
        self._remove_journal(pending.journal_path)

    def recover_startup(self) -> StartupRecoveryReport:
        """Repair exact pending files and only reconcile interrupted deletes."""

        rename_outcomes = tuple(self._reconcile_rename_journals())
        deletions = tuple(self.reconcile_delete_journals())
        recordings = rename_outcomes + tuple(
            self._recover_recording_journal(journal_path)
            for journal_path in sorted(self.state_dir.glob("recording-*.json"))
        ) if self.state_dir.is_dir() else ()
        return StartupRecoveryReport(recordings=recordings, deletions=deletions)

    def list_recordings(self) -> list[RecordingItem]:
        """List supported regular files newest-first, excluding pending files."""

        if not self.recordings_dir.is_dir():
            return []
        result: list[RecordingItem] = []
        current_cache_keys: set[
            tuple[int, int, int, int, RecordingFormat]
        ] = set()
        try:
            entries = list(self.recordings_dir.iterdir())
        except OSError as error:
            raise StorageError(f"Could not list {self.recordings_dir}") from error
        for path in entries:
            if path.name.startswith("."):
                continue
            recording_format = RecordingFormat.from_filename(path.name)
            if recording_format is None:
                continue
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            cache_key = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                recording_format,
            )
            current_cache_keys.add(cache_key)
            with self._duration_cache_lock:
                cached = cache_key in self._duration_cache
                duration = self._duration_cache.get(cache_key)
            if not cached:
                plan = inspect_recording(path, recording_format)
                duration = plan.duration_seconds if plan else None
                with self._duration_cache_lock:
                    self._duration_cache[cache_key] = duration
            result.append(
                RecordingItem(
                    path=path,
                    identity=FileIdentity.from_stat(metadata),
                    name=path.name,
                    duration_seconds=duration,
                    size_bytes=max(0, metadata.st_size),
                    modified_ns=max(0, metadata.st_mtime_ns),
                    format=recording_format,
                )
            )
        result.sort(
            key=lambda item: (item.modified_ns, item.name.casefold(), item.name),
            reverse=True,
        )
        # Renames reuse the inode-based key, while replaced/removed recordings
        # do not grow the process cache forever.  Concurrent readers may cause
        # an occasional harmless rescan, never a stale duration.
        with self._duration_cache_lock:
            self._duration_cache = {
                key: value
                for key, value in self._duration_cache.items()
                if key in current_cache_keys
            }
        return result

    # Convenient spelling for controller code while retaining an explicit API.
    list = list_recordings

    def rename_recording(
        self,
        item_or_path: RecordingItem | str | Path,
        requested_name: str,
    ) -> Path:
        """Rename in place, preserve extension, and never replace a conflict."""

        path = self._item_path(item_or_path)
        expected_identity = self._item_identity(item_or_path)
        self._require_recording_path(path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise StorageIdentityError("Rename source is not a regular file")
        source_identity = FileIdentity.from_stat(metadata)
        if expected_identity is not None and expected_identity != source_identity:
            raise StorageIdentityError(
                "Refusing to rename a recording whose identity changed"
            )
        recording_format = RecordingFormat.from_filename(path.name)
        if recording_format is None:
            raise StorageError("Rename source has an unsupported format")
        display_name = validate_recording_name(requested_name, recording_format)
        if display_name == path.name:
            return path
        target = self.recordings_dir / display_name
        if display_name.casefold() in self._directory_names(exclude=path):
            raise RecordingNameConflictError(display_name)
        journal_path = self._create_rename_journal(path, target, source_identity)
        moved = False
        try:
            _rename_noreplace(path, target)
            moved = True
        except FileExistsError as error:
            self._remove_journal(journal_path)
            raise RecordingNameConflictError(display_name) from error
        except OSError as error:
            self._remove_journal(journal_path)
            raise StorageError(f"Could not rename {path.name}") from error
        try:
            self._verify_exact_path(target, source_identity, "Renamed recording")
            _fsync_directory(self.recordings_dir)
            self._verify_exact_path(
                target,
                source_identity,
                "Renamed recording before journal removal",
            )
        except StorageIdentityError:
            # A concurrent pathname replacement won a narrow interval.  Move
            # the target back only if it is still the exact expected inode;
            # an unexpected replacement is never moved under MiniRec's source
            # name.  In either case the journal remains for startup reporting.
            if moved:
                self._attempt_restore(target, path, source_identity)
            try:
                _fsync_directory(self.recordings_dir)
            except OSError:
                pass
            raise StorageIdentityError(
                "Refusing to complete a rename after the source identity changed"
            )
        # The journal is removed only after both the target identity and the
        # directory entry have been synchronized.
        self._remove_journal(journal_path)
        return target

    rename = rename_recording

    def available_bytes(self) -> int:
        """Return free bytes on the nearest existing recording-directory parent."""

        candidate = self.recordings_dir
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        try:
            return max(0, shutil.disk_usage(candidate).free)
        except OSError:
            return 0

    def remaining_seconds(
        self,
        settings: RecordingSettings,
        *,
        available_bytes: int | None = None,
    ) -> int:
        free = self.available_bytes() if available_bytes is None else available_bytes
        return estimate_remaining_seconds(free, settings)

    def prepare_delete(
        self,
        items_or_paths: Iterable[RecordingItem | str | Path],
    ) -> DeleteReservation:
        """Synchronously journal exact identities before any unlink is allowed."""

        selected = list(items_or_paths)
        paths = [self._item_path(item) for item in selected]
        if len(paths) > MAX_SELECTED_RECORDINGS:
            raise SelectionLimitError(
                f"At most {MAX_SELECTED_RECORDINGS} recordings may be selected"
            )
        if not paths:
            raise ValueError("At least one recording is required")
        if len(set(paths)) != len(paths):
            raise ValueError("Delete selection contains duplicate paths")
        entries: list[_JournaledPath] = []
        quarantine_paths: set[Path] = set()
        for selected_item, path in zip(selected, paths):
            self._require_recording_path(path)
            recording_format = RecordingFormat.from_filename(path.name)
            if recording_format is None:
                raise StorageError(f"Unsupported recording: {path.name}")
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageIdentityError(f"Not a regular recording: {path}")
            identity = FileIdentity.from_stat(metadata)
            expected_identity = self._item_identity(selected_item)
            if expected_identity is not None and expected_identity != identity:
                raise StorageIdentityError(
                    f"Refusing to delete a recording whose identity changed: {path}"
                )
            quarantine_path = self._new_quarantine_path()
            while quarantine_path in quarantine_paths:
                quarantine_path = self._new_quarantine_path()
            quarantine_paths.add(quarantine_path)
            entries.append(_JournaledPath(path, quarantine_path, identity))

        _ensure_directory(self.state_dir)
        for _ in range(128):
            token = secrets.token_hex(12)
            journal_path = self.state_dir / f"delete-{token}.json"
            if not journal_path.exists():
                break
        else:
            raise StorageError("Could not allocate a delete journal")
        payload = {
            "version": DELETE_JOURNAL_VERSION,
            "kind": "delete",
            "entries": [
                {
                    "path": str(entry.path),
                    "quarantine_path": str(entry.quarantine_path),
                    "device": entry.identity.device,
                    "inode": entry.identity.inode,
                }
                for entry in entries
            ],
        }
        _atomic_json_write(journal_path, payload)
        return DeleteReservation(journal_path, tuple(entries))

    def delete_prepared(self, reservation: DeleteReservation) -> DeleteResult:
        """Quarantine exact inodes, synchronize the moves, then remove them."""

        loaded = self._load_delete_journal(reservation.journal_path)
        if loaded is None or tuple(loaded) != reservation.entries:
            raise PendingJournalError("Delete reservation journal changed")
        deleted: list[Path] = []
        skipped: list[Path] = []
        try:
            for entry in reservation.entries:
                try:
                    removed = self._delete_journaled_entry(entry)
                except FileNotFoundError:
                    skipped.append(entry.path)
                else:
                    (deleted if removed else skipped).append(entry.path)
        except OSError as error:
            # Leave the durable mapping for non-destructive startup restore.
            raise StorageError("Could not complete the journaled delete") from error
        self._remove_journal(reservation.journal_path)
        return DeleteResult(
            requested_count=len(reservation.entries),
            deleted_paths=tuple(deleted),
            skipped_paths=tuple(skipped),
        )

    def delete_recordings(
        self,
        items_or_paths: Iterable[RecordingItem | str | Path],
    ) -> DeleteResult:
        items = list(items_or_paths)
        if not items:
            return DeleteResult(0, (), ())
        return self.delete_prepared(self.prepare_delete(items))

    delete = delete_recordings

    def reconcile_delete_journals(self) -> list[DeleteReconciliation]:
        """Restore quarantined inodes at startup and never call ``unlink``."""

        if not self.state_dir.is_dir():
            return []
        outcomes: list[DeleteReconciliation] = []
        for journal_path in sorted(self.state_dir.glob("delete-*.json")):
            entries = self._load_delete_journal(journal_path)
            if entries is None:
                outcomes.append(
                    DeleteReconciliation(
                        DeleteReconciliationStatus.UNCERTAIN,
                        journal_path,
                        detail="Delete journal is invalid or unreadable",
                    )
                )
                continue
            deleted: list[Path] = []
            present: list[Path] = []
            changed: list[Path] = []
            uncertain_detail: str | None = None
            for entry in entries:
                original_metadata, original_error = self._inspect_path(entry.path)
                if entry.quarantine_path is None:
                    # Legacy v1 journals predate quarantine.  They are observed
                    # exactly as before and are never used to retry a delete.
                    if original_error is not None:
                        uncertain_detail = "A legacy delete target could not be inspected"
                        break
                    if original_metadata is None:
                        deleted.append(entry.path)
                    elif entry.identity.matches(original_metadata):
                        present.append(entry.path)
                    else:
                        changed.append(entry.path)
                        uncertain_detail = (
                            "A legacy delete target has a different identity"
                        )
                        break
                    continue

                quarantine_metadata, quarantine_error = self._inspect_path(
                    entry.quarantine_path
                )
                if original_error is not None or quarantine_error is not None:
                    uncertain_detail = "A delete target or quarantine could not be inspected"
                    break
                if quarantine_metadata is None:
                    if original_metadata is None:
                        deleted.append(entry.path)
                    elif entry.identity.matches(original_metadata):
                        # The crash preceded the move, or startup restored it.
                        present.append(entry.path)
                    else:
                        changed.append(entry.path)
                        uncertain_detail = (
                            "A delete target has a different identity and no "
                            "matching quarantine"
                        )
                        break
                    continue
                if not entry.identity.matches(quarantine_metadata):
                    changed.append(entry.path)
                    uncertain_detail = "A delete quarantine contains a different inode"
                    break
                if original_metadata is not None:
                    if not entry.identity.matches(original_metadata):
                        changed.append(entry.path)
                    else:
                        present.append(entry.path)
                    uncertain_detail = "A quarantined inode conflicts with its original path"
                    break
                try:
                    _rename_noreplace(entry.quarantine_path, entry.path)
                    try:
                        self._verify_exact_path(
                            entry.path, entry.identity, "Restored recording"
                        )
                    except StorageIdentityError:
                        self._attempt_restore(
                            entry.path, entry.quarantine_path, entry.identity
                        )
                        raise
                    _fsync_directory(self.recordings_dir)
                except OSError as error:
                    uncertain_detail = f"A quarantined recording could not be restored: {error}"
                    break
                present.append(entry.path)

            if uncertain_detail is not None:
                outcomes.append(
                    DeleteReconciliation(
                        DeleteReconciliationStatus.UNCERTAIN,
                        journal_path,
                        tuple(deleted),
                        tuple(present),
                        tuple(changed),
                        uncertain_detail,
                    )
                )
                continue
            self._remove_journal(journal_path)
            outcomes.append(
                DeleteReconciliation(
                    DeleteReconciliationStatus.RECONCILED,
                    journal_path,
                    tuple(deleted),
                    tuple(present),
                    tuple(changed),
                )
            )
        return outcomes

    def _recover_recording_journal(
        self, journal_path: Path
    ) -> RecordingRecoveryOutcome:
        journal = self._load_recording_journal(journal_path)
        if journal is None:
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                None,
                None,
                journal_path,
                "Recording journal is invalid or unreadable",
            )
        pending_path = journal.pending_path
        final_path = journal.final_path
        recording_format = journal.format
        identity = journal.identity

        if journal.cleanup_path is not None:
            return self._reconcile_recording_cleanup(journal, journal_path)

        pending_metadata, pending_error = self._inspect_path(pending_path)
        if pending_error is not None:
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                pending_path,
                final_path,
                journal_path,
                "Pending target could not be inspected",
            )

        if pending_metadata is None:
            final_metadata, final_error = self._inspect_path(final_path)
            if final_error is None and final_metadata is None:
                self._remove_journal(journal_path)
                return RecordingRecoveryOutcome(
                    RecordingRecoveryStatus.MISSING,
                    pending_path,
                    final_path,
                    journal_path,
                )
            if final_error is None and final_metadata is not None and identity.matches(
                final_metadata
            ):
                try:
                    self._verify_exact_path(
                        final_path, identity, "Published recording"
                    )
                except StorageIdentityError as error:
                    self._attempt_restore(final_path, pending_path, identity)
                    try:
                        _fsync_directory(self.recordings_dir)
                    except OSError:
                        pass
                    return RecordingRecoveryOutcome(
                        RecordingRecoveryStatus.UNCERTAIN,
                        pending_path,
                        final_path,
                        journal_path,
                        str(error),
                    )
                self._remove_journal(journal_path)
                return RecordingRecoveryOutcome(
                    RecordingRecoveryStatus.COMPLETED,
                    pending_path,
                    final_path,
                    journal_path,
                )
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                pending_path,
                final_path,
                journal_path,
                "Published path has a different or unavailable identity",
            )

        if not identity.matches(pending_metadata):
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                pending_path,
                final_path,
                journal_path,
                "Pending path identity changed",
            )

        # Backward compatibility for a v1 journal left by the former hard-link
        # fallback.  Even the duplicate hidden link is removed only through a
        # newly journaled quarantine mapping.
        final_metadata, _final_error = self._inspect_path(final_path)
        if final_metadata is not None and identity.matches(final_metadata):
            try:
                self._cleanup_recording_path(
                    journal,
                    journal_path,
                    reason="duplicate",
                    required_survivor=final_path,
                )
                self._verify_exact_path(final_path, identity, "Published recording")
                self._remove_journal(journal_path)
            except OSError as error:
                return RecordingRecoveryOutcome(
                    RecordingRecoveryStatus.UNCERTAIN,
                    pending_path,
                    final_path,
                    journal_path,
                    str(error),
                )
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.COMPLETED,
                pending_path,
                final_path,
                journal_path,
            )

        if pending_metadata.st_size == 0:
            try:
                self._cleanup_recording_path(
                    journal,
                    journal_path,
                    reason="empty",
                )
                self._remove_journal(journal_path)
            except OSError as error:
                return RecordingRecoveryOutcome(
                    RecordingRecoveryStatus.UNCERTAIN,
                    pending_path,
                    final_path,
                    journal_path,
                    str(error),
                )
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.EMPTY_REMOVED,
                pending_path,
                final_path,
                journal_path,
            )

        try:
            plan = recover_recording(
                pending_path,
                recording_format,
                expected_device=identity.device,
                expected_inode=identity.inode,
            )
        except (OSError, RecoveryIdentityError) as error:
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                pending_path,
                final_path,
                journal_path,
                str(error),
            )
        if plan is None:
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                pending_path,
                final_path,
                journal_path,
                "No unambiguous complete audio prefix was found",
            )
        try:
            published = self._publish_pending(
                pending_path,
                identity,
                recording_format,
                final_path,
                journal_path,
            )
            try:
                self._verify_exact_path(published, identity, "Recovered recording")
            except StorageIdentityError:
                self._attempt_restore(published, pending_path, identity)
                try:
                    _fsync_directory(self.recordings_dir)
                except OSError:
                    pass
                raise
            self._remove_journal(journal_path)
        except OSError as error:
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.UNCERTAIN,
                pending_path,
                final_path,
                journal_path,
                str(error),
            )
        return RecordingRecoveryOutcome(
            RecordingRecoveryStatus.RECOVERED,
            pending_path,
            published,
            journal_path,
        )

    def _publish_pending(
        self,
        pending_path: Path,
        identity: FileIdentity,
        recording_format: RecordingFormat,
        desired_final: Path,
        journal_path: Path,
    ) -> Path:
        display_name = validate_recording_name(desired_final.name, recording_format)
        try:
            existing_final = desired_final.lstat()
        except FileNotFoundError:
            existing_final = None
        if existing_final is not None and identity.matches(existing_final):
            # The hard-link fallback published successfully but its source
            # unlink (or the caller immediately afterwards) was interrupted.
            # Retrying must converge to one visible name, not create a second
            # hard link with a collision suffix.
            journal = self._load_recording_journal(journal_path)
            if journal is None:
                raise PendingJournalError("Recording journal changed during publication")
            self._cleanup_recording_path(
                journal,
                journal_path,
                reason="duplicate",
                required_survivor=desired_final,
            )
            self._verify_exact_path(desired_final, identity, "Published recording")
            return desired_final
        while True:
            target = self._unique_final_path(
                display_name, exclude_journal=journal_path
            )
            payload = self._recording_journal_payload(
                pending_path, target, recording_format, identity
            )
            _atomic_json_write(journal_path, payload)
            try:
                _rename_noreplace(pending_path, target)
            except FileExistsError:
                # A concurrent creator won after the case-folded scan.  Its
                # data remains untouched and the next suffix is journaled.
                continue
            try:
                self._verify_exact_path(target, identity, "Published recording")
            except StorageIdentityError:
                self._attempt_restore(target, pending_path, identity)
                try:
                    _fsync_directory(self.recordings_dir)
                except OSError:
                    pass
                # The durable recording journal deliberately remains.
                raise
            _fsync_directory(self.recordings_dir)
            return target

    def _unique_final_path(
        self,
        display_name: str,
        *,
        exclude_journal: Path | None = None,
    ) -> Path:
        recording_format = RecordingFormat.from_filename(display_name)
        if recording_format is None:
            raise RecordingNameError("Recording filename has no supported extension")
        names = self._reserved_names(exclude_journal=exclude_journal)
        if display_name.casefold() not in names:
            return self.recordings_dir / display_name
        base = display_name[: -len(recording_format.extension)]
        suffix = 2
        while True:
            candidate = self._collision_name(base, suffix, recording_format)
            if candidate.casefold() not in names:
                return self.recordings_dir / candidate
            suffix += 1

    @staticmethod
    def _collision_name(
        base: str, suffix: int, recording_format: RecordingFormat
    ) -> str:
        decoration = f" ({suffix}){recording_format.extension}"
        allowed = MAX_RECORDING_NAME_BYTES - len(decoration.encode("utf-8"))
        trimmed = _truncate_utf8(base, allowed).rstrip()
        if not trimmed:
            raise RecordingNameTooLongError("No filename space remains for a suffix")
        return f"{trimmed}{decoration}"

    def _reserved_names(self, *, exclude_journal: Path | None = None) -> set[str]:
        names = self._directory_names()
        if not self.state_dir.is_dir():
            return names
        for journal_path in self.state_dir.glob("recording-*.json"):
            if exclude_journal is not None and journal_path == exclude_journal:
                continue
            journal = self._load_recording_journal(journal_path)
            if journal is not None:
                names.add(journal.final_path.name.casefold())
        return names

    def _directory_names(self, *, exclude: Path | None = None) -> set[str]:
        if not self.recordings_dir.is_dir():
            return set()
        try:
            return {
                path.name.casefold()
                for path in self.recordings_dir.iterdir()
                if exclude is None or path != exclude
            }
        except OSError as error:
            raise StorageError("Could not inspect recording name conflicts") from error

    def _matching_recording_journal(
        self, pending: PendingRecording
    ) -> _RecordingJournal:
        loaded = self._load_recording_journal(pending.journal_path)
        if loaded is None:
            raise PendingJournalError("Pending recording journal is unavailable")
        if (
            loaded.pending_path != pending.path
            or loaded.final_path != pending.final_path
            or loaded.format is not pending.format
            or loaded.identity != pending.identity
            or loaded.cleanup_path is not None
        ):
            raise PendingJournalError("Pending recording journal changed")
        return loaded

    def _recording_journal_payload(
        self,
        pending_path: Path,
        final_path: Path,
        recording_format: RecordingFormat,
        identity: FileIdentity,
        *,
        cleanup_path: Path | None = None,
        cleanup_reason: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": JOURNAL_VERSION,
            "kind": "recording",
            "pending_path": str(pending_path),
            "final_path": str(final_path),
            "format": recording_format.storage_value,
            "device": identity.device,
            "inode": identity.inode,
        }
        if cleanup_path is not None:
            payload["cleanup_path"] = str(cleanup_path)
            payload["cleanup_reason"] = cleanup_reason or "unknown"
        return payload

    def _load_recording_journal(
        self, journal_path: Path
    ) -> _RecordingJournal | None:
        value = _read_json_object(journal_path)
        if (
            value is None
            or type(value.get("version")) is not int
            or value.get("version") != JOURNAL_VERSION
            or value.get("kind") != "recording"
        ):
            return None
        pending_path = _strict_path(value.get("pending_path"), self.recordings_dir)
        final_path = _strict_path(value.get("final_path"), self.recordings_dir)
        recording_format = next(
            (
                candidate
                for candidate in RecordingFormat
                if value.get("format") == candidate.storage_value
            ),
            None,
        )
        device = _strict_nonnegative_int(value.get("device"))
        inode = _strict_positive_int(value.get("inode"))
        raw_cleanup_path = value.get("cleanup_path")
        raw_cleanup_reason = value.get("cleanup_reason")
        cleanup_path = (
            _strict_path(raw_cleanup_path, self.recordings_dir)
            if raw_cleanup_path is not None
            else None
        )
        cleanup_reason = (
            raw_cleanup_reason if isinstance(raw_cleanup_reason, str) else None
        )
        if (
            pending_path is None
            or final_path is None
            or not pending_path.name.startswith(".minirec-")
            or not pending_path.name.endswith(".pending")
            or recording_format is None
            or not recording_format.matches_filename(final_path)
            or device is None
            or inode is None
            or (raw_cleanup_path is not None and cleanup_path is None)
            or (cleanup_path is None) != (cleanup_reason is None)
            or (
                cleanup_path is not None
                and not _is_quarantine_name(cleanup_path.name)
            )
            or (
                cleanup_reason is not None
                and cleanup_reason not in {"abort", "empty", "duplicate"}
            )
        ):
            return None
        return _RecordingJournal(
            pending_path,
            final_path,
            recording_format,
            FileIdentity(device, inode),
            cleanup_path,
            cleanup_reason,
        )

    def _load_delete_journal(
        self, journal_path: Path
    ) -> list[_JournaledPath] | None:
        value = _read_json_object(journal_path)
        if (
            value is None
            or type(value.get("version")) is not int
            or value.get("version") not in {JOURNAL_VERSION, DELETE_JOURNAL_VERSION}
            or value.get("kind") != "delete"
        ):
            return None
        raw_entries = value.get("entries")
        if (
            not isinstance(raw_entries, list)
            or not raw_entries
            or len(raw_entries) > MAX_SELECTED_RECORDINGS
        ):
            return None
        result: list[_JournaledPath] = []
        version = value.get("version")
        for raw in raw_entries:
            if not isinstance(raw, dict):
                return None
            path = _strict_path(raw.get("path"), self.recordings_dir)
            quarantine_path = None
            if version == DELETE_JOURNAL_VERSION:
                quarantine_path = _strict_path(
                    raw.get("quarantine_path"), self.recordings_dir
                )
            device = _strict_nonnegative_int(raw.get("device"))
            inode = _strict_positive_int(raw.get("inode"))
            if (
                path is None
                or device is None
                or inode is None
                or (
                    version == DELETE_JOURNAL_VERSION
                    and (
                        quarantine_path is None
                        or not _is_quarantine_name(quarantine_path.name)
                    )
                )
            ):
                return None
            result.append(
                _JournaledPath(path, quarantine_path, FileIdentity(device, inode))
            )
        if (
            len({entry.path for entry in result}) != len(result)
            or len(
                {
                    entry.quarantine_path
                    for entry in result
                    if entry.quarantine_path is not None
                }
            )
            != len([entry for entry in result if entry.quarantine_path is not None])
        ):
            return None
        return result

    def _new_quarantine_path(self) -> Path:
        """Return an unallocated high-entropy hidden name on the recording FS."""

        for _ in range(128):
            candidate = self.recordings_dir / (
                f".minirec-quarantine-{secrets.token_hex(18)}.trash"
            )
            try:
                candidate.lstat()
            except FileNotFoundError:
                return candidate
            except OSError as error:
                raise StorageError("Could not inspect a quarantine path") from error
        raise StorageError("Could not allocate a recording quarantine path")

    @staticmethod
    def _inspect_path(
        path: Path,
    ) -> tuple[os.stat_result | None, OSError | None]:
        try:
            return path.lstat(), None
        except FileNotFoundError:
            return None, None
        except OSError as error:
            return None, error

    @staticmethod
    def _verify_exact_path(
        path: Path,
        identity: FileIdentity,
        description: str,
    ) -> os.stat_result:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise StorageIdentityError(f"{description} could not be verified") from error
        if not identity.matches(metadata):
            raise StorageIdentityError(f"{description} identity changed")
        return metadata

    def _attempt_restore(
        self,
        source: Path,
        target: Path,
        expected_identity: FileIdentity,
    ) -> bool:
        """Move back only the exact expected inode, never a replacement."""

        try:
            self._verify_exact_path(
                source, expected_identity, "Rollback source"
            )
        except StorageIdentityError:
            return False
        try:
            _rename_noreplace(source, target)
            self._verify_exact_path(
                target, expected_identity, "Restored rollback target"
            )
        except OSError:
            return False
        return True

    def _move_exact_to_quarantine(
        self,
        source: Path,
        quarantine: Path,
        identity: FileIdentity,
    ) -> None:
        self._verify_exact_path(source, identity, "Quarantine source")
        _rename_noreplace(source, quarantine)
        try:
            self._verify_exact_path(quarantine, identity, "Quarantined recording")
        except StorageIdentityError:
            self._attempt_restore(quarantine, source, identity)
            try:
                _fsync_directory(self.recordings_dir)
            except OSError:
                pass
            raise
        _fsync_directory(self.recordings_dir)

    def _unlink_quarantine(
        self,
        quarantine: Path,
        identity: FileIdentity,
        *,
        restore_path: Path,
        required_survivor: Path | None = None,
    ) -> None:
        """Remove one verified random quarantine entry, never a public path.

        Holding a no-follow descriptor plus a final identity check prevents the
        ordinary scan/replace race.  Linux cannot make pathname unlink fully
        hostile-proof against another process with the same UID and directory
        permissions; the unpredictable private name narrows that residual
        boundary, while every detected mismatch fails closed with its journal.
        """

        flags = (
            getattr(os, "O_PATH", os.O_RDONLY)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(quarantine, flags)
        except OSError as error:
            raise StorageIdentityError(
                "Quarantined recording could not be opened"
            ) from error
        survivor_descriptor = -1
        try:
            try:
                if not identity.matches(os.fstat(descriptor)):
                    raise StorageIdentityError(
                        "Quarantined recording identity changed"
                    )
                _quarantine_unlink_hook(quarantine)
                self._verify_exact_path(
                    quarantine, identity, "Quarantined recording before removal"
                )
                if required_survivor is not None:
                    try:
                        survivor_descriptor = os.open(required_survivor, flags)
                    except OSError as error:
                        raise StorageIdentityError(
                            "Required published recording could not be opened"
                        ) from error
                    if not identity.matches(os.fstat(survivor_descriptor)):
                        raise StorageIdentityError(
                            "Required published recording identity changed"
                        )
                    self._verify_exact_path(
                        required_survivor,
                        identity,
                        "Required published recording before duplicate cleanup",
                    )
                try:
                    os.unlink(quarantine)
                except OSError as error:
                    raise StorageIdentityError(
                        "Quarantine removal raced with a pathname change"
                    ) from error
                remaining, inspection_error = self._inspect_path(quarantine)
                if inspection_error is not None:
                    raise StorageIdentityError(
                        "Could not verify quarantine removal"
                    ) from inspection_error
                if remaining is not None:
                    raise StorageIdentityError(
                        "Quarantine path was concurrently recreated"
                    )
            except StorageIdentityError:
                if survivor_descriptor >= 0:
                    os.close(survivor_descriptor)
                    survivor_descriptor = -1
                os.close(descriptor)
                descriptor = -1
                self._attempt_restore(quarantine, restore_path, identity)
                try:
                    _fsync_directory(self.recordings_dir)
                except OSError:
                    pass
                raise
        finally:
            if survivor_descriptor >= 0:
                os.close(survivor_descriptor)
            if descriptor >= 0:
                os.close(descriptor)
        _fsync_directory(self.recordings_dir)

    def _delete_journaled_entry(self, entry: _JournaledPath) -> bool:
        if entry.quarantine_path is None:
            raise PendingJournalError("A legacy delete cannot be executed")
        metadata, error = self._inspect_path(entry.path)
        if error is not None:
            raise error
        if metadata is None:
            return False
        if not entry.identity.matches(metadata):
            raise StorageIdentityError(
                "Delete target identity changed after journaling"
            )
        try:
            self._move_exact_to_quarantine(
                entry.path, entry.quarantine_path, entry.identity
            )
        except FileNotFoundError:
            return False
        self._unlink_quarantine(
            entry.quarantine_path,
            entry.identity,
            restore_path=entry.path,
        )
        return True

    def _cleanup_recording_path(
        self,
        journal: _RecordingJournal,
        journal_path: Path,
        *,
        reason: str,
        required_survivor: Path | None = None,
    ) -> None:
        """Journal, quarantine and remove one pending/duplicate source name."""

        self._verify_exact_path(
            journal.pending_path, journal.identity, "Pending cleanup source"
        )
        quarantine = self._new_quarantine_path()
        payload = self._recording_journal_payload(
            journal.pending_path,
            journal.final_path,
            journal.format,
            journal.identity,
            cleanup_path=quarantine,
            cleanup_reason=reason,
        )
        _atomic_json_write(journal_path, payload)
        self._move_exact_to_quarantine(
            journal.pending_path, quarantine, journal.identity
        )
        self._unlink_quarantine(
            quarantine,
            journal.identity,
            restore_path=journal.pending_path,
            required_survivor=required_survivor,
        )

    def _reconcile_recording_cleanup(
        self,
        journal: _RecordingJournal,
        journal_path: Path,
    ) -> RecordingRecoveryOutcome:
        """Resolve a cleanup crash without ever retrying its destructive step."""

        assert journal.cleanup_path is not None
        pending_metadata, pending_error = self._inspect_path(journal.pending_path)
        quarantine_metadata, quarantine_error = self._inspect_path(
            journal.cleanup_path
        )
        if pending_error is not None or quarantine_error is not None:
            detail = "Cleanup source or quarantine could not be inspected"
        elif quarantine_metadata is not None:
            if not journal.identity.matches(quarantine_metadata):
                detail = "Cleanup quarantine contains a different inode"
            elif pending_metadata is not None:
                detail = "Cleanup quarantine conflicts with its pending path"
            else:
                try:
                    _rename_noreplace(journal.cleanup_path, journal.pending_path)
                    try:
                        self._verify_exact_path(
                            journal.pending_path,
                            journal.identity,
                            "Restored pending recording",
                        )
                    except StorageIdentityError:
                        self._attempt_restore(
                            journal.pending_path,
                            journal.cleanup_path,
                            journal.identity,
                        )
                        raise
                    _fsync_directory(self.recordings_dir)
                    detail = (
                        "Interrupted cleanup was restored; destructive cleanup "
                        "was not retried"
                    )
                except OSError as error:
                    detail = f"Cleanup quarantine could not be restored: {error}"
        elif pending_metadata is not None:
            detail = (
                "Cleanup was journaled but not committed; destructive cleanup "
                "was not retried"
            )
        elif journal.cleanup_reason == "duplicate":
            final_metadata, final_error = self._inspect_path(journal.final_path)
            if (
                final_error is None
                and final_metadata is not None
                and journal.identity.matches(final_metadata)
            ):
                self._remove_journal(journal_path)
                return RecordingRecoveryOutcome(
                    RecordingRecoveryStatus.COMPLETED,
                    journal.pending_path,
                    journal.final_path,
                    journal_path,
                )
            detail = "Duplicate cleanup finished but the published inode is uncertain"
        else:
            self._remove_journal(journal_path)
            return RecordingRecoveryOutcome(
                RecordingRecoveryStatus.EMPTY_REMOVED
                if journal.cleanup_reason == "empty"
                else RecordingRecoveryStatus.MISSING,
                journal.pending_path,
                journal.final_path,
                journal_path,
            )
        return RecordingRecoveryOutcome(
            RecordingRecoveryStatus.UNCERTAIN,
            journal.pending_path,
            journal.final_path,
            journal_path,
            detail,
        )

    def _create_rename_journal(
        self,
        source: Path,
        target: Path,
        identity: FileIdentity,
    ) -> Path:
        _ensure_directory(self.state_dir)
        for _ in range(128):
            journal_path = self.state_dir / f"rename-{secrets.token_hex(12)}.json"
            if not journal_path.exists():
                break
        else:
            raise StorageError("Could not allocate a rename journal")
        _atomic_json_write(
            journal_path,
            {
                "version": JOURNAL_VERSION,
                "kind": "rename",
                "source_path": str(source),
                "target_path": str(target),
                "device": identity.device,
                "inode": identity.inode,
            },
        )
        return journal_path

    def _load_rename_journal(self, journal_path: Path) -> _RenameJournal | None:
        value = _read_json_object(journal_path)
        if (
            value is None
            or type(value.get("version")) is not int
            or value.get("version") != JOURNAL_VERSION
            or value.get("kind") != "rename"
        ):
            return None
        source = _strict_path(value.get("source_path"), self.recordings_dir)
        target = _strict_path(value.get("target_path"), self.recordings_dir)
        source_format = (
            RecordingFormat.from_filename(source.name) if source is not None else None
        )
        target_format = (
            RecordingFormat.from_filename(target.name) if target is not None else None
        )
        device = _strict_nonnegative_int(value.get("device"))
        inode = _strict_positive_int(value.get("inode"))
        if (
            source is None
            or target is None
            or source == target
            or source.name.startswith(".")
            or target.name.startswith(".")
            or source_format is None
            or source_format is not target_format
            or device is None
            or inode is None
        ):
            return None
        return _RenameJournal(source, target, FileIdentity(device, inode))

    def _reconcile_rename_journals(self) -> list[RecordingRecoveryOutcome]:
        if not self.state_dir.is_dir():
            return []
        uncertain: list[RecordingRecoveryOutcome] = []
        for journal_path in sorted(self.state_dir.glob("rename-*.json")):
            journal = self._load_rename_journal(journal_path)
            if journal is None:
                uncertain.append(
                    RecordingRecoveryOutcome(
                        RecordingRecoveryStatus.UNCERTAIN,
                        None,
                        None,
                        journal_path,
                        "Rename journal is invalid or unreadable",
                    )
                )
                continue
            source, source_error = self._inspect_path(journal.source_path)
            target, target_error = self._inspect_path(journal.target_path)
            if source_error is not None or target_error is not None:
                uncertain.append(
                    RecordingRecoveryOutcome(
                        RecordingRecoveryStatus.UNCERTAIN,
                        journal.source_path,
                        journal.target_path,
                        journal_path,
                        "Rename source or target could not be inspected",
                    )
                )
                continue
            if (
                target is not None
                and journal.identity.matches(target)
                and not (
                    source is not None and journal.identity.matches(source)
                )
            ):
                try:
                    self._verify_exact_path(
                        journal.target_path,
                        journal.identity,
                        "Renamed recording during startup",
                    )
                    _fsync_directory(self.recordings_dir)
                    self._verify_exact_path(
                        journal.target_path,
                        journal.identity,
                        "Renamed recording before startup journal removal",
                    )
                    self._remove_journal(journal_path)
                except OSError as error:
                    self._attempt_restore(
                        journal.target_path,
                        journal.source_path,
                        journal.identity,
                    )
                    try:
                        _fsync_directory(self.recordings_dir)
                    except OSError:
                        pass
                    uncertain.append(
                        RecordingRecoveryOutcome(
                            RecordingRecoveryStatus.UNCERTAIN,
                            journal.source_path,
                            journal.target_path,
                            journal_path,
                            str(error),
                        )
                    )
            elif (
                source is not None
                and journal.identity.matches(source)
                and target is None
            ):
                self._remove_journal(journal_path)
            else:
                uncertain.append(
                    RecordingRecoveryOutcome(
                        RecordingRecoveryStatus.UNCERTAIN,
                        journal.source_path,
                        journal.target_path,
                        journal_path,
                        "Interrupted rename has conflicting file identities",
                    )
                )
        return uncertain

    def _sync_exact(self, path: Path, identity: FileIdentity) -> None:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not identity.matches(os.fstat(descriptor)):
                raise StorageIdentityError("Pending recording identity changed")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_journal(self, journal_path: Path) -> None:
        try:
            journal_path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(journal_path.parent)

    def _require_recording_path(self, path: Path) -> None:
        if not path.is_absolute():
            path = _absolute_normalized(path)
        if path.parent != self.recordings_dir or path.name.startswith("."):
            raise StorageIdentityError("Recording path is outside the public directory")

    @staticmethod
    def _item_path(item_or_path: RecordingItem | str | Path) -> Path:
        if isinstance(item_or_path, RecordingItem):
            return _absolute_normalized(item_or_path.path)
        return _absolute_normalized(Path(item_or_path))

    @staticmethod
    def _item_identity(
        item_or_path: RecordingItem | str | Path,
    ) -> FileIdentity | None:
        return item_or_path.identity if isinstance(item_or_path, RecordingItem) else None


def _absolute_normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir():
            raise StorageError(f"Not a directory: {path}")
    except OSError as error:
        raise StorageError(f"Could not create directory {path}") from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise StorageError("Journal value is not JSON serializable") from error
    if not payload or len(payload) > MAX_JOURNAL_BYTES:
        raise StorageError("Journal exceeds its byte limit")
    _ensure_directory(path.parent)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise StorageError(f"Could not atomically write journal {path}") from error


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as source:
            payload = source.read(MAX_JOURNAL_BYTES + 1)
    except OSError:
        return None
    if not payload or len(payload) > MAX_JOURNAL_BYTES:
        return None
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return value


def _strict_path(value: object, expected_parent: Path) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    normalized = _absolute_normalized(candidate)
    return normalized if normalized.parent == expected_parent else None


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _strict_nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _strict_positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _is_quarantine_name(name: str) -> bool:
    prefix = ".minirec-quarantine-"
    suffix = ".trash"
    token = name[len(prefix) : -len(suffix)] if name.startswith(prefix) and name.endswith(suffix) else ""
    return len(token) == 36 and all(character in "0123456789abcdef" for character in token)


def _quarantine_unlink_hook(_path: Path) -> None:
    """Deterministic test seam immediately before the final identity check."""


def _rename_noreplace(source: Path, target: Path) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE), or fail closed."""

    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOSYS,
            "renameat2(RENAME_NOREPLACE) is unavailable",
            target,
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number), target)


_LIBC: Final = ctypes.CDLL(None, use_errno=True)


__all__ = [
    "DeleteReconciliation",
    "DeleteReconciliationStatus",
    "DeleteReservation",
    "DeleteResult",
    "EmptyRecordingNameError",
    "FileIdentity",
    "InvalidRecordingNameError",
    "MAX_RECORDING_NAME_BYTES",
    "MAX_SELECTED_RECORDINGS",
    "MINIMUM_SPACE_RESERVE_BYTES",
    "PendingJournalError",
    "PendingRecording",
    "RecordingItem",
    "RecordingNameConflictError",
    "RecordingNameError",
    "RecordingNameTooLongError",
    "RecordingRecoveryOutcome",
    "RecordingRecoveryStatus",
    "RecordingStorage",
    "SelectionLimitError",
    "StorageProcessLock",
    "StorageProcessLockError",
    "StartupRecoveryReport",
    "StorageError",
    "StorageIdentityError",
    "default_recordings_dir",
    "estimate_remaining_seconds",
    "space_reserve_bytes",
    "validate_recording_name",
]
