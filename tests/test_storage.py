from __future__ import annotations

from datetime import datetime
import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from minirec import storage as storage_module
from minirec.models import ChannelMode, RecordingFormat, RecordingSettings
from minirec.storage import (
    EmptyRecordingNameError,
    InvalidRecordingNameError,
    MAX_SELECTED_RECORDINGS,
    MINIMUM_SPACE_RESERVE_BYTES,
    RecordingNameConflictError,
    RecordingNameTooLongError,
    RecordingRecoveryStatus,
    RecordingStorage,
    SelectionLimitError,
    StorageError,
    StorageIdentityError,
    StorageProcessLock,
    StorageProcessLockError,
    default_recordings_dir,
    estimate_remaining_seconds,
    space_reserve_bytes,
    validate_recording_name,
)
from tests.test_recovery import mp3_frame, ogg_page, opus_stream


def valid_wav(frame_count: int = 48_000, *, channels: int = 1) -> bytes:
    data = b"\x00" * (frame_count * channels * 2)
    sample_rate = 48_000
    block_align = channels * 2
    byte_rate = sample_rate * block_align
    file_size = 44 + len(data)
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", file_size - 8),
            b"WAVEfmt ",
            struct.pack(
                "<IHHIIHH",
                16,
                1,
                channels,
                sample_rate,
                byte_rate,
                block_align,
                16,
            ),
            b"data",
            struct.pack("<I", len(data)),
            data,
        )
    )


class StorageProcessLockTest(unittest.TestCase):
    def test_only_one_process_descriptor_can_own_state_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            first = StorageProcessLock(state)
            second = StorageProcessLock(state)
            first.acquire()
            self.assertTrue(first.acquired)
            self.assertEqual(0o600, stat.S_IMODE(first.path.stat().st_mode))
            with self.assertRaises(StorageProcessLockError):
                second.acquire()
            first.close()
            second.acquire()
            self.assertTrue(second.acquired)
            second.close()

    def test_symlink_lock_file_is_never_followed(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            target = Path(directory) / "outside"
            target.write_text("do not touch", encoding="utf-8")
            (state / "minirec.lock").symlink_to(target)
            with self.assertRaises(OSError):
                StorageProcessLock(state).acquire()
            self.assertEqual("do not touch", target.read_text(encoding="utf-8"))


class StoragePathAndPendingTest(unittest.TestCase):
    def test_default_directory_and_absolute_environment_override(self) -> None:
        home = Path("/test/home")
        self.assertEqual(
            home / "Recordings" / "MiniRec",
            default_recordings_dir({}, home=home),
        )
        self.assertEqual(
            Path("/tmp/minirec-recordings"),
            default_recordings_dir(
                {"MINIREC_RECORDINGS_DIR": "/tmp/minirec-recordings"},
                home=home,
            ),
        )
        self.assertEqual(
            home / "Recordings" / "MiniRec",
            default_recordings_dir(
                {"MINIREC_RECORDINGS_DIR": "relative/path"}, home=home
            ),
        )

    def test_pending_is_private_empty_regular_exclusive_and_journaled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            now = datetime(2026, 8, 3, 10, 11, 12)
            pending = storage.create_pending(RecordingFormat.MP3, now=now)

            metadata = pending.path.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(0, metadata.st_size)
            self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
            self.assertTrue(pending.prepared)
            self.assertEqual("MiniRec_2026-08-03_10-11-12.mp3", pending.display_name)
            journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
            self.assertEqual(str(pending.path), journal["pending_path"])
            self.assertEqual(str(pending.final_path), journal["final_path"])
            self.assertEqual(metadata.st_dev, journal["device"])
            self.assertEqual(metadata.st_ino, journal["inode"])

            second = storage.create_pending(RecordingFormat.MP3, now=now)
            self.assertEqual(
                "MiniRec_2026-08-03_10-11-12 (2).mp3", second.final_path.name
            )
            self.assertNotEqual(pending.path, second.path)

    def test_failed_first_journal_commit_fails_closed_with_private_empty_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            real_unlink = os.unlink
            unlinked: list[Path] = []

            def observed_unlink(path: str | bytes | Path, *args: object, **kwargs: object) -> None:
                unlinked.append(Path(os.fsdecode(path)))
                real_unlink(path, *args, **kwargs)

            with (
                patch("minirec.storage.os.replace", side_effect=OSError("fail")),
                patch("minirec.storage.os.unlink", side_effect=observed_unlink),
            ):
                with self.assertRaises(StorageError):
                    storage.create_pending()
            leftovers = list((root / "recordings").iterdir())
            self.assertEqual(1, len(leftovers))
            self.assertTrue(leftovers[0].name.startswith(".minirec-"))
            self.assertTrue(leftovers[0].name.endswith(".pending"))
            metadata = leftovers[0].lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(0, metadata.st_size)
            self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
            self.assertEqual([], list((root / "state").glob("recording-*.json")))
            self.assertFalse(
                any(path.parent == root / "recordings" for path in unlinked)
            )

    def test_complete_never_overwrites_a_late_collision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            pending.path.write_bytes(valid_wav(frame_count=1))
            pending.final_path.write_bytes(b"someone else's recording")

            published = storage.complete(pending)
            self.assertEqual("Voice (2).wav", published.name)
            self.assertEqual(b"someone else's recording", pending.final_path.read_bytes())
            self.assertEqual(valid_wav(frame_count=1), published.read_bytes())
            self.assertFalse(pending.path.exists())
            self.assertFalse(pending.journal_path.exists())

    def test_complete_retry_converges_after_partial_hardlink_publication(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            pending.path.write_bytes(valid_wav(frame_count=1))

            def publish_then_fail(source: Path, target: Path) -> None:
                os.link(source, target)
                raise OSError("simulated crash before source unlink")

            with patch("minirec.storage._rename_noreplace", side_effect=publish_then_fail):
                with self.assertRaises(OSError):
                    storage.complete(pending)
            self.assertTrue(pending.path.exists())
            self.assertTrue(pending.final_path.exists())

            published = storage.complete(pending)
            self.assertEqual(pending.final_path, published)
            self.assertFalse(pending.path.exists())
            self.assertEqual(
                ["Voice.wav"],
                [item.name for item in storage.list_recordings()],
            )

    def test_abort_refuses_a_replaced_path_and_preserves_replacement(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending()
            pending.path.unlink()
            pending.path.write_bytes(b"replacement")
            with self.assertRaises(StorageIdentityError):
                storage.abort(pending)
            self.assertEqual(b"replacement", pending.path.read_bytes())
            self.assertTrue(pending.journal_path.exists())


class StartupRecoveryTest(unittest.TestCase):
    def test_empty_pending_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending()
            report = RecordingStorage(root / "recordings", root / "state").recover_startup()
            self.assertEqual(1, len(report.recordings))
            self.assertIs(
                RecordingRecoveryStatus.EMPTY_REMOVED, report.recordings[0].status
            )
            self.assertFalse(pending.path.exists())
            self.assertFalse(pending.journal_path.exists())

    def test_interrupted_wav_is_repaired_in_place_then_published(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Recovered")
            # One complete mono frame and one torn byte; RIFF/data sizes are stale.
            payload = bytearray(valid_wav(frame_count=1, channels=1))
            payload[4:8] = b"\x00" * 4
            payload[40:44] = b"\x00" * 4
            pending.path.write_bytes(payload + b"\xff")

            report = RecordingStorage(root / "recordings", root / "state").recover_startup()
            outcome = report.recordings[0]
            self.assertIs(RecordingRecoveryStatus.RECOVERED, outcome.status)
            self.assertEqual((outcome.final_path,), report.recovered_paths)
            assert outcome.final_path is not None
            recovered = outcome.final_path.read_bytes()
            self.assertEqual(46, len(recovered))
            self.assertEqual(38, int.from_bytes(recovered[4:8], "little"))
            self.assertEqual(2, int.from_bytes(recovered[40:44], "little"))
            self.assertFalse(pending.path.exists())

    def test_interrupted_mp3_and_ogg_are_published_at_verified_boundaries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")

            mp3 = storage.create_pending(RecordingFormat.MP3, name="Recovered MP3")
            frame = mp3_frame()
            mp3.path.write_bytes(frame * 2 + frame[:80])

            ogg = storage.create_pending(RecordingFormat.OGG_OPUS, name="Recovered Ogg")
            stream, _ = opus_stream()
            next_page = ogg_page(
                b"next", sequence=3, granule=96_312, header_type=0x04
            )
            ogg.path.write_bytes(stream + next_page[:16])

            report = RecordingStorage(
                root / "recordings", root / "state"
            ).recover_startup()
            self.assertEqual(
                [RecordingRecoveryStatus.RECOVERED] * 2,
                [outcome.status for outcome in report.recordings],
            )
            by_suffix = {path.suffix: path for path in report.recovered_paths}
            self.assertEqual(frame * 2, by_suffix[".mp3"].read_bytes())
            self.assertEqual(stream, by_suffix[".oga"].read_bytes())

    def test_uncertain_nonempty_target_is_left_untouched_and_does_not_block_new(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.MP3, name="Uncertain")
            pending.path.write_bytes(b"not an mp3 stream")

            report = storage.recover_startup()
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, report.recordings[0].status)
            self.assertEqual(b"not an mp3 stream", pending.path.read_bytes())
            self.assertTrue(pending.journal_path.exists())
            another = storage.create_pending(RecordingFormat.MP3, name="Another")
            self.assertTrue(another.path.exists())

    def test_changed_identity_is_never_repaired_or_removed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV)
            pending.path.unlink()
            pending.path.write_bytes(valid_wav(frame_count=1))

            outcome = storage.recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, outcome.status)
            self.assertTrue(pending.path.exists())
            self.assertTrue(pending.journal_path.exists())


class RecordingListAndRenameTest(unittest.TestCase):
    def test_list_is_newest_first_with_metadata_and_ignores_nonregular_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            older = recordings / "Older.wav"
            newer = recordings / "Newer.wav"
            older.write_bytes(valid_wav(frame_count=48_000))
            newer.write_bytes(valid_wav(frame_count=96_000))
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
            (recordings / ".hidden.wav").write_bytes(valid_wav(frame_count=1))
            (recordings / "unsupported.txt").write_text("x")
            os.symlink(older, recordings / "link.wav")

            items = RecordingStorage(recordings, root / "state").list_recordings()
            self.assertEqual(["Newer.wav", "Older.wav"], [item.name for item in items])
            self.assertEqual(2.0, items[0].duration_seconds)
            self.assertEqual(str(newer), items[0].id)
            self.assertEqual(newer.stat().st_ino, items[0].identity.inode)
            self.assertEqual(len(newer.read_bytes()), items[0].size_bytes)

    def test_unchanged_recording_duration_is_cached_by_file_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "Long.oga"
            path.write_bytes(b"opaque codec payload")
            storage = RecordingStorage(recordings, root / "state")

            with patch(
                "minirec.storage.inspect_recording",
                return_value=SimpleNamespace(duration_seconds=12.5),
            ) as inspect:
                self.assertEqual(12.5, storage.list_recordings()[0].duration_seconds)
                self.assertEqual(12.5, storage.list_recordings()[0].duration_seconds)
                self.assertEqual(1, inspect.call_count)

                path.write_bytes(b"changed codec payload")
                self.assertEqual(12.5, storage.list_recordings()[0].duration_seconds)
                self.assertEqual(2, inspect.call_count)

    def test_validation_trims_preserves_extension_and_enforces_utf8_limit(self) -> None:
        self.assertEqual(
            "My voice.wav",
            validate_recording_name("  My voice.mp3  ", RecordingFormat.WAV),
        )
        with self.assertRaises(EmptyRecordingNameError):
            validate_recording_name(" .wav ", RecordingFormat.WAV)
        for invalid in (
            ".hidden",
            "a/b",
            "a\\b",
            "bad:name",
            "bad\x00name",
            "bad\x85name",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidRecordingNameError):
                    validate_recording_name(invalid, RecordingFormat.MP3)

        allowed = "ž" * 117  # 234 bytes plus .mp3 is 238.
        self.assertEqual(f"{allowed}.mp3", validate_recording_name(allowed, RecordingFormat.MP3))
        with self.assertRaises(RecordingNameTooLongError):
            validate_recording_name("ž" * 119, RecordingFormat.MP3)

    def test_rename_is_case_insensitive_no_clobber_and_same_extension(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            source = recordings / "Original.wav"
            source.write_bytes(valid_wav(frame_count=1))
            conflict = recordings / "VOICE.WAV"
            conflict.write_bytes(b"keep")
            storage = RecordingStorage(recordings, root / "state")

            with self.assertRaises(RecordingNameConflictError):
                storage.rename_recording(source, "voice")
            self.assertEqual(b"keep", conflict.read_bytes())
            renamed = storage.rename_recording(source, " New name.mp3 ")
            self.assertEqual("New name.wav", renamed.name)
            self.assertTrue(renamed.exists())
            self.assertFalse(source.exists())
            recased = storage.rename_recording(renamed, "NEW NAME")
            self.assertEqual("NEW NAME.wav", recased.name)
            self.assertTrue(recased.exists())
            self.assertFalse(renamed.exists())

    def test_rename_refuses_replacement_of_a_listed_recording(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            source = recordings / "Original.wav"
            source.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            item = storage.list_recordings()[0]

            held_original = root / "held-original.wav"
            source.rename(held_original)
            source.write_bytes(b"replacement must remain")

            with self.assertRaises(StorageIdentityError):
                storage.rename_recording(item, "Renamed")
            self.assertEqual(b"replacement must remain", source.read_bytes())
            self.assertTrue(held_original.exists())
            self.assertFalse((recordings / "Renamed.wav").exists())


class RemainingTimeTest(unittest.TestCase):
    def test_reserve_is_greater_of_64_mib_and_one_percent(self) -> None:
        self.assertEqual(MINIMUM_SPACE_RESERVE_BYTES, space_reserve_bytes(1_000_000_000))
        self.assertEqual(100_000_000, space_reserve_bytes(10_000_000_000))
        self.assertEqual(0, estimate_remaining_seconds(1_000, RecordingSettings()))
        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    space_reserve_bytes(invalid)  # type: ignore[arg-type]

    def test_compressed_estimate_uses_selected_total_bitrate(self) -> None:
        available = MINIMUM_SPACE_RESERVE_BYTES + 128_000
        settings = RecordingSettings(
            format=RecordingFormat.MP3, bitrate_kbps=128
        )
        self.assertEqual(8, estimate_remaining_seconds(available, settings))

    def test_wav_is_channel_aware_and_capped_by_classic_riff(self) -> None:
        huge = 100 * 1024**3
        mono = RecordingSettings(
            format=RecordingFormat.WAV, channel_mode=ChannelMode.MONO
        )
        stereo = RecordingSettings(
            format=RecordingFormat.WAV, channel_mode=ChannelMode.STEREO
        )
        mono_seconds = estimate_remaining_seconds(huge, mono)
        stereo_seconds = estimate_remaining_seconds(huge, stereo)
        self.assertEqual(mono_seconds // 2, stereo_seconds)
        self.assertEqual((0xFFFF_FFFF - 44) // 96_000, mono_seconds)


class DeleteJournalTest(unittest.TestCase):
    def test_selection_limit_is_checked_before_touching_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            paths = [
                root / "recordings" / f"{index}.wav"
                for index in range(MAX_SELECTED_RECORDINGS + 1)
            ]
            with self.assertRaises(SelectionLimitError):
                storage.prepare_delete(paths)

    def test_exact_selection_limit_can_be_journaled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            paths = []
            for index in range(MAX_SELECTED_RECORDINGS):
                path = recordings / f"{index}.wav"
                path.touch()
                paths.append(path)
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete(paths)
            self.assertTrue(reservation.journal_path.exists())
            reconciliation = storage.reconcile_delete_journals()[0]
            self.assertEqual(MAX_SELECTED_RECORDINGS, len(reconciliation.present_paths))

    def test_normal_delete_journals_then_unlinks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            first = recordings / "first.wav"
            second = recordings / "second.wav"
            first.write_bytes(valid_wav(frame_count=1))
            second.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            result = storage.delete_recordings((first, second))
            self.assertEqual(2, result.deleted_count)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertEqual([], list((root / "state").glob("delete-*.json")))

    def test_delete_identity_check_does_not_require_file_read_permission(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "private.wav"
            path.write_bytes(valid_wav(frame_count=1))
            path.chmod(0)
            storage = RecordingStorage(recordings, root / "state")

            result = storage.delete_recordings((path,))
            self.assertEqual((path,), result.deleted_paths)
            self.assertFalse(path.exists())

    def test_delete_refuses_replacement_of_a_listed_recording(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            path.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            item = storage.list_recordings()[0]

            held_original = root / "held-voice.wav"
            path.rename(held_original)
            path.write_bytes(b"replacement must remain")

            with self.assertRaises(StorageIdentityError):
                storage.delete_recordings((item,))
            self.assertEqual(b"replacement must remain", path.read_bytes())
            self.assertTrue(held_original.exists())
            self.assertEqual([], list((root / "state").glob("delete-*.json")))

    def test_prepared_delete_reports_replaced_target_uncertain_and_keeps_journal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            path.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            item = storage.list_recordings()[0]
            reservation = storage.prepare_delete((item,))

            held_original = root / "held-after-journal.wav"
            path.rename(held_original)
            path.write_bytes(b"late replacement must remain")

            with self.assertRaises(StorageError):
                storage.delete_prepared(reservation)
            self.assertEqual(b"late replacement must remain", path.read_bytes())
            self.assertTrue(held_original.exists())
            self.assertTrue(reservation.journal_path.exists())

            outcome = RecordingStorage(
                recordings, root / "state"
            ).recover_startup().deletions[0]
            self.assertIs(
                storage_module.DeleteReconciliationStatus.UNCERTAIN,
                outcome.status,
            )
            self.assertEqual((path,), outcome.changed_paths)
            self.assertTrue(reservation.journal_path.exists())

    def test_startup_reconciliation_never_redeletes_a_survivor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "survivor.wav"
            payload = valid_wav(frame_count=1)
            path.write_bytes(payload)
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))

            restarted = RecordingStorage(recordings, root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("startup must not retry delete"),
            ):
                report = restarted.recover_startup()
            self.assertEqual((path,), report.deletions[0].present_paths)
            self.assertEqual(payload, path.read_bytes())
            self.assertFalse(reservation.journal_path.exists())

    def test_reconciliation_observes_missing_and_preserves_changed_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            missing = recordings / "missing.wav"
            changed = recordings / "changed.wav"
            missing.write_bytes(valid_wav(frame_count=1))
            changed.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            missing_reservation = storage.prepare_delete((missing,))
            changed_reservation = storage.prepare_delete((changed,))
            missing.unlink()
            changed.unlink()
            changed.write_bytes(b"replacement")

            outcomes = RecordingStorage(recordings, root / "state").reconcile_delete_journals()
            by_journal = {outcome.journal_path: outcome for outcome in outcomes}
            self.assertEqual(
                (missing,), by_journal[missing_reservation.journal_path].deleted_paths
            )
            changed_outcome = by_journal[changed_reservation.journal_path]
            self.assertIs(
                storage_module.DeleteReconciliationStatus.UNCERTAIN,
                changed_outcome.status,
            )
            self.assertEqual((changed,), changed_outcome.changed_paths)
            self.assertEqual(b"replacement", changed.read_bytes())
            self.assertFalse(missing_reservation.journal_path.exists())
            self.assertTrue(changed_reservation.journal_path.exists())

    def test_v2_journal_durably_maps_random_hidden_quarantine_before_move(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            path.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")

            reservation = storage.prepare_delete((path,))
            payload = json.loads(
                reservation.journal_path.read_text(encoding="utf-8")
            )
            entry = payload["entries"][0]
            quarantine = Path(entry["quarantine_path"])
            self.assertEqual(2, payload["version"])
            self.assertEqual(path, Path(entry["path"]))
            self.assertEqual(path.stat().st_ino, entry["inode"])
            self.assertEqual(recordings, quarantine.parent)
            self.assertTrue(quarantine.name.startswith(".minirec-quarantine-"))
            self.assertTrue(quarantine.name.endswith(".trash"))
            self.assertFalse(quarantine.exists())

    def test_crash_before_quarantine_unlink_restores_exact_inode_on_startup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            payload = valid_wav(frame_count=2)
            path.write_bytes(payload)
            original_inode = path.stat().st_ino
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            quarantine = reservation.entries[0].quarantine_path
            assert quarantine is not None

            with patch.object(
                storage,
                "_unlink_quarantine",
                side_effect=OSError("simulated power loss before unlink"),
            ):
                with self.assertRaises(StorageError):
                    storage.delete_prepared(reservation)
            self.assertFalse(path.exists())
            self.assertEqual(original_inode, quarantine.stat().st_ino)
            self.assertTrue(reservation.journal_path.exists())

            report = RecordingStorage(recordings, root / "state").recover_startup()
            self.assertEqual((path,), report.deletions[0].present_paths)
            self.assertEqual(payload, path.read_bytes())
            self.assertEqual(original_inode, path.stat().st_ino)
            self.assertFalse(quarantine.exists())
            self.assertFalse(reservation.journal_path.exists())

    def test_crash_after_quarantine_unlink_reconciles_without_retry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            path.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            real_unlink = storage._unlink_quarantine

            def unlink_then_crash(
                quarantine: Path,
                identity: object,
                *,
                restore_path: Path,
            ) -> None:
                real_unlink(
                    quarantine,
                    identity,  # type: ignore[arg-type]
                    restore_path=restore_path,
                )
                raise OSError("simulated power loss after unlink")

            with patch.object(
                storage, "_unlink_quarantine", side_effect=unlink_then_crash
            ):
                with self.assertRaises(StorageError):
                    storage.delete_prepared(reservation)
            self.assertFalse(path.exists())
            self.assertTrue(reservation.journal_path.exists())

            restarted = RecordingStorage(recordings, root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("startup must never retry unlink"),
            ):
                report = restarted.recover_startup()
            self.assertEqual((path,), report.deletions[0].deleted_paths)
            self.assertFalse(reservation.journal_path.exists())

    def test_quarantine_restore_conflict_is_uncertain_and_preserves_both(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            original = valid_wav(frame_count=2)
            path.write_bytes(original)
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            quarantine = reservation.entries[0].quarantine_path
            assert quarantine is not None
            with patch.object(
                storage,
                "_unlink_quarantine",
                side_effect=OSError("simulated crash"),
            ):
                with self.assertRaises(StorageError):
                    storage.delete_prepared(reservation)
            path.write_bytes(b"new recording at original name")

            outcome = RecordingStorage(
                recordings, root / "state"
            ).recover_startup().deletions[0]
            self.assertIs(
                storage_module.DeleteReconciliationStatus.UNCERTAIN,
                outcome.status,
            )
            self.assertEqual(b"new recording at original name", path.read_bytes())
            self.assertEqual(original, quarantine.read_bytes())
            self.assertTrue(reservation.journal_path.exists())

    def test_source_replacement_during_quarantine_move_is_not_moved_again(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            original = valid_wav(frame_count=1)
            path.write_bytes(original)
            held_original = root / "held-original.wav"
            replacement = b"concurrent replacement"
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            real_rename = storage_module._rename_noreplace

            def replace_then_rename(source: Path, target: Path) -> None:
                if source == path:
                    source.rename(held_original)
                    source.write_bytes(replacement)
                real_rename(source, target)

            with patch(
                "minirec.storage._rename_noreplace",
                side_effect=replace_then_rename,
            ):
                with self.assertRaises(StorageError):
                    storage.delete_prepared(reservation)

            self.assertEqual(original, held_original.read_bytes())
            quarantine = reservation.entries[0].quarantine_path
            assert quarantine is not None
            self.assertFalse(path.exists())
            self.assertEqual(replacement, quarantine.read_bytes())
            self.assertTrue(reservation.journal_path.exists())

    def test_quarantine_replacement_at_unlink_seam_is_never_moved_as_expected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "voice.wav"
            original = valid_wav(frame_count=1)
            path.write_bytes(original)
            held_original = root / "concurrent-owner-held-original.wav"
            replacement = b"replacement inserted at random quarantine name"
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            quarantine = reservation.entries[0].quarantine_path
            assert quarantine is not None

            def exchange_quarantine(current: Path) -> None:
                current.rename(held_original)
                current.write_bytes(replacement)

            with patch(
                "minirec.storage._quarantine_unlink_hook",
                side_effect=exchange_quarantine,
            ):
                with self.assertRaises(StorageError):
                    storage.delete_prepared(reservation)
            self.assertEqual(original, held_original.read_bytes())
            self.assertFalse(path.exists())
            self.assertEqual(replacement, quarantine.read_bytes())
            self.assertTrue(reservation.journal_path.exists())

    def test_v1_delete_journal_remains_backward_readable_and_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "legacy.wav"
            payload = valid_wav(frame_count=1)
            path.write_bytes(payload)
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            journal = json.loads(
                reservation.journal_path.read_text(encoding="utf-8")
            )
            journal["version"] = 1
            for entry in journal["entries"]:
                entry.pop("quarantine_path")
            storage_module._atomic_json_write(reservation.journal_path, journal)

            restarted = RecordingStorage(recordings, root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("legacy startup must stay read-only"),
            ):
                outcome = restarted.recover_startup().deletions[0]
            self.assertEqual((path,), outcome.present_paths)
            self.assertEqual(payload, path.read_bytes())
            self.assertFalse(reservation.journal_path.exists())

    def test_v1_changed_identity_is_uncertain_and_keeps_legacy_journal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            path = recordings / "legacy.wav"
            path.write_bytes(valid_wav(frame_count=1))
            storage = RecordingStorage(recordings, root / "state")
            reservation = storage.prepare_delete((path,))
            journal = json.loads(
                reservation.journal_path.read_text(encoding="utf-8")
            )
            journal["version"] = 1
            for entry in journal["entries"]:
                entry.pop("quarantine_path")
            storage_module._atomic_json_write(reservation.journal_path, journal)
            path.unlink()
            path.write_bytes(b"legacy path replacement")

            restarted = RecordingStorage(recordings, root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("legacy startup must stay read-only"),
            ):
                outcome = restarted.recover_startup().deletions[0]
            self.assertIs(
                storage_module.DeleteReconciliationStatus.UNCERTAIN,
                outcome.status,
            )
            self.assertEqual((path,), outcome.changed_paths)
            self.assertEqual(b"legacy path replacement", path.read_bytes())
            self.assertTrue(reservation.journal_path.exists())


class NoReplaceAndCleanupSafetyTest(unittest.TestCase):
    def test_attempt_restore_never_moves_a_source_with_the_wrong_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending()
            pending.path.unlink()
            pending.path.write_bytes(b"replacement must stay at source")
            target = storage.recordings_dir / ".restored-target"

            with patch("minirec.storage._rename_noreplace") as rename:
                restored = storage._attempt_restore(
                    pending.path, target, pending.identity
                )
            self.assertFalse(restored)
            rename.assert_not_called()
            self.assertEqual(b"replacement must stay at source", pending.path.read_bytes())
            self.assertFalse(target.exists())

    def test_renameat2_unsupported_errors_fail_closed_without_link_fallback(self) -> None:
        class UnsupportedRenameAt2:
            argtypes: object = None
            restype: object = None

            def __init__(self, error_number: int) -> None:
                self.error_number = error_number

            def __call__(self, *_args: object) -> int:
                ctypes.set_errno(self.error_number)
                return -1

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.write_bytes(b"keep")
            for error_number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                with self.subTest(error_number=error_number):
                    fake_libc = SimpleNamespace(
                        renameat2=UnsupportedRenameAt2(error_number)
                    )
                    with (
                        patch("minirec.storage._LIBC", fake_libc),
                        patch("minirec.storage.os.link") as link,
                    ):
                        with self.assertRaises(OSError) as raised:
                            storage_module._rename_noreplace(source, target)
                    self.assertEqual(error_number, raised.exception.errno)
                    link.assert_not_called()
                    self.assertEqual(b"keep", source.read_bytes())
                    self.assertFalse(target.exists())

    def test_abort_crash_restores_quarantine_without_retrying_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending()
            pending.path.write_bytes(b"unfinished but valuable audio")

            with patch.object(
                storage,
                "_unlink_quarantine",
                side_effect=OSError("simulated crash before cleanup unlink"),
            ):
                with self.assertRaises(OSError):
                    storage.abort(pending)
            journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
            quarantine = Path(journal["cleanup_path"])
            self.assertEqual("abort", journal["cleanup_reason"])
            self.assertFalse(pending.path.exists())
            self.assertEqual(b"unfinished but valuable audio", quarantine.read_bytes())

            restarted = RecordingStorage(root / "recordings", root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("startup must not retry cleanup unlink"),
            ):
                outcome = restarted.recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, outcome.status)
            self.assertEqual(
                b"unfinished but valuable audio", pending.path.read_bytes()
            )
            self.assertFalse(quarantine.exists())
            self.assertTrue(pending.journal_path.exists())

            # A later startup remains non-destructive and keeps the explicit
            # cleanup journal for manual/data-first resolution.
            again = restarted.recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, again.status)
            self.assertTrue(pending.path.exists())

    def test_empty_cleanup_crash_restores_without_retrying_unlink(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending()

            with patch.object(
                storage,
                "_unlink_quarantine",
                side_effect=OSError("simulated empty-cleanup crash"),
            ):
                first = storage.recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, first.status)
            journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
            quarantine = Path(journal["cleanup_path"])
            self.assertEqual("empty", journal["cleanup_reason"])
            self.assertFalse(pending.path.exists())
            self.assertEqual(0, quarantine.stat().st_size)

            restarted = RecordingStorage(root / "recordings", root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("startup must not retry empty unlink"),
            ):
                second = restarted.recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, second.status)
            self.assertTrue(pending.path.exists())
            self.assertEqual(0, pending.path.stat().st_size)
            self.assertFalse(quarantine.exists())
            self.assertTrue(pending.journal_path.exists())

    def test_duplicate_cleanup_crash_restores_without_retrying_unlink(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            payload = valid_wav(frame_count=1)
            pending.path.write_bytes(payload)
            os.link(pending.path, pending.final_path)

            with patch.object(
                storage,
                "_unlink_quarantine",
                side_effect=OSError("simulated duplicate-cleanup crash"),
            ):
                with self.assertRaises(OSError):
                    storage.complete(pending)
            journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
            quarantine = Path(journal["cleanup_path"])
            self.assertEqual("duplicate", journal["cleanup_reason"])
            self.assertFalse(pending.path.exists())
            self.assertEqual(payload, quarantine.read_bytes())
            self.assertEqual(payload, pending.final_path.read_bytes())

            restarted = RecordingStorage(root / "recordings", root / "state")
            with patch.object(
                restarted,
                "_unlink_quarantine",
                side_effect=AssertionError("startup must not retry duplicate unlink"),
            ):
                outcome = restarted.recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.UNCERTAIN, outcome.status)
            self.assertEqual(payload, pending.path.read_bytes())
            self.assertEqual(payload, pending.final_path.read_bytes())
            self.assertFalse(quarantine.exists())
            self.assertTrue(pending.journal_path.exists())

    def test_publish_source_race_never_moves_replacement_as_rollback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            original = valid_wav(frame_count=1)
            pending.path.write_bytes(original)
            held_original = root / "held-original.wav"
            replacement = b"replacement after precondition"
            real_rename = storage_module._rename_noreplace

            def replace_then_publish(source: Path, target: Path) -> None:
                if source == pending.path:
                    source.rename(held_original)
                    source.write_bytes(replacement)
                real_rename(source, target)

            with patch(
                "minirec.storage._rename_noreplace",
                side_effect=replace_then_publish,
            ):
                with self.assertRaises(StorageIdentityError):
                    storage.complete(pending)
            self.assertEqual(original, held_original.read_bytes())
            self.assertFalse(pending.path.exists())
            self.assertEqual(replacement, pending.final_path.read_bytes())
            self.assertTrue(pending.journal_path.exists())

    def test_publish_target_is_reverified_immediately_before_journal_removal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            original = valid_wav(frame_count=1)
            pending.path.write_bytes(original)
            held_published = root / "held-published.wav"
            replacement = b"late target replacement"
            real_verify = storage._verify_exact_path
            published_checks = 0

            def verify_then_replace(
                path: Path, identity: object, description: str
            ) -> os.stat_result:
                nonlocal published_checks
                if description == "Published recording":
                    published_checks += 1
                metadata = real_verify(
                    path, identity, description  # type: ignore[arg-type]
                )
                if description == "Published recording" and published_checks == 1:
                    path.rename(held_published)
                    path.write_bytes(replacement)
                return metadata

            with patch.object(
                storage, "_verify_exact_path", side_effect=verify_then_replace
            ):
                with self.assertRaises(StorageIdentityError):
                    storage.complete(pending)
            self.assertEqual(2, published_checks)
            self.assertEqual(original, held_published.read_bytes())
            self.assertFalse(pending.path.exists())
            self.assertEqual(replacement, pending.final_path.read_bytes())
            self.assertTrue(pending.journal_path.exists())

    def test_publish_crash_before_journal_removal_converges_on_startup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            payload = valid_wav(frame_count=1)
            pending.path.write_bytes(payload)

            with patch.object(
                storage,
                "_remove_journal",
                side_effect=OSError("simulated crash before journal removal"),
            ):
                with self.assertRaises(OSError):
                    storage.complete(pending)
            self.assertFalse(pending.path.exists())
            self.assertEqual(payload, pending.final_path.read_bytes())
            self.assertTrue(pending.journal_path.exists())

            outcome = RecordingStorage(
                root / "recordings", root / "state"
            ).recover_startup().recordings[0]
            self.assertIs(RecordingRecoveryStatus.COMPLETED, outcome.status)
            self.assertEqual(payload, pending.final_path.read_bytes())
            self.assertFalse(pending.journal_path.exists())

    def test_legacy_duplicate_cleanup_checks_surviving_public_inode_before_unlink(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RecordingStorage(root / "recordings", root / "state")
            pending = storage.create_pending(RecordingFormat.WAV, name="Voice")
            original = valid_wav(frame_count=1)
            pending.path.write_bytes(original)
            os.link(pending.path, pending.final_path)
            held_public = root / "held-public.wav"
            replacement = b"late public replacement"

            def replace_public(_quarantine: Path) -> None:
                pending.final_path.rename(held_public)
                pending.final_path.write_bytes(replacement)

            with patch(
                "minirec.storage._quarantine_unlink_hook",
                side_effect=replace_public,
            ):
                with self.assertRaises(StorageIdentityError):
                    storage.complete(pending)
            journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
            quarantine = Path(journal["cleanup_path"])
            self.assertEqual(original, pending.path.read_bytes())
            self.assertFalse(quarantine.exists())
            self.assertEqual(original, held_public.read_bytes())
            self.assertEqual(replacement, pending.final_path.read_bytes())
            self.assertTrue(pending.journal_path.exists())

    def test_user_rename_source_race_never_moves_replacement_as_rollback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            source = recordings / "Original.wav"
            original = valid_wav(frame_count=1)
            source.write_bytes(original)
            held_original = root / "held-original.wav"
            replacement = b"replacement after rename journal"
            storage = RecordingStorage(recordings, root / "state")
            real_rename = storage_module._rename_noreplace

            def replace_then_rename(current: Path, target: Path) -> None:
                if current == source:
                    current.rename(held_original)
                    current.write_bytes(replacement)
                real_rename(current, target)

            with patch(
                "minirec.storage._rename_noreplace",
                side_effect=replace_then_rename,
            ):
                with self.assertRaises(StorageIdentityError):
                    storage.rename_recording(source, "Renamed")
            self.assertEqual(original, held_original.read_bytes())
            self.assertFalse(source.exists())
            self.assertEqual(
                replacement, (recordings / "Renamed.wav").read_bytes()
            )
            journals = list((root / "state").glob("rename-*.json"))
            self.assertEqual(1, len(journals))

            report = storage.recover_startup()
            uncertain = [
                outcome
                for outcome in report.recordings
                if outcome.status is RecordingRecoveryStatus.UNCERTAIN
            ]
            self.assertEqual(1, len(uncertain))
            self.assertEqual(journals[0], uncertain[0].journal_path)
            self.assertTrue(journals[0].exists())

    def test_user_rename_rechecks_target_after_directory_fsync(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            source = recordings / "Original.wav"
            original = valid_wav(frame_count=1)
            source.write_bytes(original)
            target = recordings / "Renamed.wav"
            held_target = root / "held-renamed.wav"
            replacement = b"replacement after first rename verification"
            storage = RecordingStorage(recordings, root / "state")
            real_verify = storage._verify_exact_path
            rename_checks = 0

            def verify_then_replace(
                path: Path, identity: object, description: str
            ) -> os.stat_result:
                nonlocal rename_checks
                if description.startswith("Renamed recording"):
                    rename_checks += 1
                metadata = real_verify(
                    path, identity, description  # type: ignore[arg-type]
                )
                if description == "Renamed recording" and rename_checks == 1:
                    path.rename(held_target)
                    path.write_bytes(replacement)
                return metadata

            with patch.object(
                storage, "_verify_exact_path", side_effect=verify_then_replace
            ):
                with self.assertRaises(StorageIdentityError):
                    storage.rename_recording(source, "Renamed")
            self.assertEqual(2, rename_checks)
            self.assertFalse(source.exists())
            self.assertEqual(original, held_target.read_bytes())
            self.assertEqual(replacement, target.read_bytes())
            self.assertEqual(1, len(list((root / "state").glob("rename-*.json"))))

    def test_startup_rename_rechecks_target_after_fsync_before_journal_removal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            source = recordings / "Original.wav"
            original = valid_wav(frame_count=1)
            source.write_bytes(original)
            storage = RecordingStorage(recordings, root / "state")
            with patch.object(
                storage,
                "_remove_journal",
                side_effect=OSError("simulated crash before rename journal removal"),
            ):
                with self.assertRaises(OSError):
                    storage.rename_recording(source, "Renamed")
            target = recordings / "Renamed.wav"
            journal = next((root / "state").glob("rename-*.json"))
            held_target = root / "held-startup-target.wav"
            replacement = b"replacement after startup first verification"
            restarted = RecordingStorage(recordings, root / "state")
            real_verify = restarted._verify_exact_path
            startup_checks = 0

            def verify_then_replace(
                path: Path, identity: object, description: str
            ) -> os.stat_result:
                nonlocal startup_checks
                if description.startswith("Renamed recording"):
                    startup_checks += 1
                metadata = real_verify(
                    path, identity, description  # type: ignore[arg-type]
                )
                if description == "Renamed recording during startup":
                    path.rename(held_target)
                    path.write_bytes(replacement)
                return metadata

            with patch.object(
                restarted, "_verify_exact_path", side_effect=verify_then_replace
            ):
                report = restarted.recover_startup()
            self.assertEqual(2, startup_checks)
            self.assertEqual(1, len(report.recordings))
            self.assertIs(
                RecordingRecoveryStatus.UNCERTAIN,
                report.recordings[0].status,
            )
            self.assertEqual(original, held_target.read_bytes())
            self.assertEqual(replacement, target.read_bytes())
            self.assertTrue(journal.exists())

    def test_user_rename_crash_before_journal_removal_converges_on_startup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            recordings.mkdir()
            source = recordings / "Original.wav"
            payload = valid_wav(frame_count=1)
            source.write_bytes(payload)
            storage = RecordingStorage(recordings, root / "state")

            with patch.object(
                storage,
                "_remove_journal",
                side_effect=OSError("simulated crash before journal removal"),
            ):
                with self.assertRaises(OSError):
                    storage.rename_recording(source, "Renamed")
            target = recordings / "Renamed.wav"
            self.assertFalse(source.exists())
            self.assertEqual(payload, target.read_bytes())
            journals = list((root / "state").glob("rename-*.json"))
            self.assertEqual(1, len(journals))

            report = RecordingStorage(recordings, root / "state").recover_startup()
            self.assertEqual((), report.recordings)
            self.assertEqual(payload, target.read_bytes())
            self.assertFalse(journals[0].exists())


if __name__ == "__main__":
    unittest.main()
