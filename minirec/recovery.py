"""Format-aware, in-place recovery of interrupted MiniRec recordings.

The recovery scanners deliberately accept less than a general-purpose media
player.  MiniRec knows exactly which streams it writes, so a startup repair is
allowed only when a contiguous prefix can be proved safe without searching for
another sync word after damaged data.  Callers must additionally verify the
file's durable device/inode identity before using :func:`recover_recording`.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import struct
from typing import Final

from .models import RecordingFormat


MAX_WAV_HEADER_BYTES: Final = 64 * 1024
MAX_CLASSIC_WAV_FILE_BYTES: Final = 0xFFFF_FFFF
MAX_MP3_FRAME_BYTES: Final = 2_048
MIN_MP3_FRAMES: Final = 2
MP3_SCAN_BUFFER_BYTES: Final = 64 * 1024
OPUS_SAMPLE_RATE: Final = 48_000


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """A checked publishable prefix and any bounded header repairs it needs."""

    format: RecordingFormat
    original_size: int
    safe_size: int
    duration_seconds: float
    unit_count: int
    patches: tuple[tuple[int, bytes], ...] = ()

    @property
    def needs_repair(self) -> bool:
        return self.safe_size != self.original_size or bool(self.patches)


class RecoveryError(OSError):
    """The checked recovery mutation could not be completed durably."""


class RecoveryIdentityError(RecoveryError):
    """The opened path is no longer the journaled regular file."""


def inspect_recording(
    path: str | Path,
    recording_format: RecordingFormat | None = None,
) -> RecoveryPlan | None:
    """Return a verified stream plan without changing *path*.

    ``None`` means that no safe interpretation was established.  It does not
    mean that the file is definitely corrupt; normal library listing can still
    show such a file with an unknown duration.
    """

    candidate = Path(path)
    selected_format = recording_format or RecordingFormat.from_filename(candidate)
    if selected_format is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            return None
        return _plan_for_descriptor(descriptor, metadata.st_size, selected_format)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def plan_wav_recovery(path: str | Path) -> RecoveryPlan | None:
    """Inspect an interrupted PCM16 RIFF/WAVE file without mutating it."""

    return inspect_recording(path, RecordingFormat.WAV)


def plan_cbr_mp3_recovery(path: str | Path) -> RecoveryPlan | None:
    """Inspect a headerless contiguous CBR MPEG Layer III stream."""

    return inspect_recording(path, RecordingFormat.MP3)


def plan_ogg_opus_recovery(path: str | Path) -> RecoveryPlan | None:
    """Inspect a single serial, CRC-checked Ogg/Opus stream."""

    return inspect_recording(path, RecordingFormat.OGG_OPUS)


def recover_recording(
    path: str | Path,
    recording_format: RecordingFormat,
    *,
    expected_device: int,
    expected_inode: int,
) -> RecoveryPlan | None:
    """Repair exactly the journaled file and synchronize the safe prefix.

    No mutation occurs unless the open descriptor still identifies the exact
    regular file recorded in the journal and a scanner returns a complete
    plan.  The operation is idempotent: a crash before the caller publishes
    the file can safely run the same scanner again.
    """

    candidate = Path(path)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != expected_device
            or metadata.st_ino != expected_inode
        ):
            raise RecoveryIdentityError("Recovery target identity changed")
        plan = _plan_for_descriptor(descriptor, metadata.st_size, recording_format)
        if plan is None:
            return None
        metadata_after_scan = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata_after_scan.st_mode)
            or metadata_after_scan.st_dev != expected_device
            or metadata_after_scan.st_ino != expected_inode
            or metadata_after_scan.st_size != metadata.st_size
        ):
            raise RecoveryIdentityError("Recovery target changed while being scanned")
        if plan.safe_size < plan.original_size:
            os.ftruncate(descriptor, plan.safe_size)
        for offset, payload in plan.patches:
            _pwrite_all(descriptor, payload, offset)
        os.fsync(descriptor)
        return plan
    except RecoveryIdentityError:
        raise
    except OSError as error:
        raise RecoveryError(f"Could not recover {candidate}") from error
    finally:
        os.close(descriptor)


def _plan_for_descriptor(
    descriptor: int,
    file_size: int,
    recording_format: RecordingFormat,
) -> RecoveryPlan | None:
    if recording_format is RecordingFormat.WAV:
        return _plan_wav(descriptor, file_size)
    if recording_format is RecordingFormat.MP3:
        return _plan_mp3(descriptor, file_size)
    if recording_format is RecordingFormat.OGG_OPUS:
        return _plan_ogg_opus(descriptor, file_size)
    return None


def _plan_wav(descriptor: int, file_size: int) -> RecoveryPlan | None:
    # RIFF stores file-size-minus-eight in an unsigned 32-bit field.
    if file_size < 12 or file_size > MAX_CLASSIC_WAV_FILE_BYTES + 8:
        return None
    prefix = os.pread(descriptor, min(file_size, MAX_WAV_HEADER_BYTES), 0)
    if len(prefix) < 12 or prefix[:4] != b"RIFF" or prefix[8:12] != b"WAVE":
        return None

    offset = 12
    sample_rate = 0
    byte_rate = 0
    block_align = 0
    format_found = False
    while offset <= len(prefix) - 8:
        chunk_id = prefix[offset : offset + 4]
        chunk_size = int.from_bytes(prefix[offset + 4 : offset + 8], "little")
        payload_offset = offset + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16 or payload_offset + 16 > len(prefix):
                return None
            (
                audio_format,
                channels,
                sample_rate,
                byte_rate,
                block_align,
                bits_per_sample,
            ) = struct.unpack_from("<HHIIHH", prefix, payload_offset)
            if (
                audio_format != 1
                or channels not in (1, 2)
                or bits_per_sample != 16
                or sample_rate <= 0
                or block_align != channels * 2
                or byte_rate != sample_rate * block_align
            ):
                return None
            format_found = True
        elif chunk_id == b"data":
            if not format_found or block_align <= 0 or byte_rate <= 0:
                return None
            data_offset = payload_offset
            if data_offset > file_size:
                return None
            available = file_size - data_offset
            safe_data_size = available - (available % block_align)
            if safe_data_size <= 0 or safe_data_size > 0xFFFF_FFFF:
                return None
            safe_file_size = data_offset + safe_data_size
            riff_size = safe_file_size - 8
            if not 0 < riff_size <= 0xFFFF_FFFF:
                return None
            return RecoveryPlan(
                format=RecordingFormat.WAV,
                original_size=file_size,
                safe_size=safe_file_size,
                duration_seconds=safe_data_size / byte_rate,
                unit_count=safe_data_size // block_align,
                patches=(
                    (4, riff_size.to_bytes(4, "little")),
                    (offset + 4, safe_data_size.to_bytes(4, "little")),
                ),
            )

        padded_size = chunk_size + (chunk_size & 1)
        next_offset = payload_offset + padded_size
        if next_offset <= offset or next_offset > len(prefix):
            return None
        offset = next_offset
    return None


@dataclass(frozen=True, slots=True)
class _Mp3Header:
    frame_size: int
    samples_per_frame: int
    signature: tuple[int, int, int, int]


class _PreadWindow:
    """Small sequential cache which avoids one syscall for every MP3 frame."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.offset = -1
        self.payload = b""

    def read(self, offset: int, size: int) -> bytes:
        relative = offset - self.offset
        if relative >= 0 and relative + size <= len(self.payload):
            return self.payload[relative : relative + size]
        self.offset = offset
        self.payload = os.pread(
            self.descriptor, max(MP3_SCAN_BUFFER_BYTES, size), offset
        )
        return self.payload[:size]


def _plan_mp3(descriptor: int, file_size: int) -> RecoveryPlan | None:
    if file_size <= 0:
        return None
    offset = 0
    frame_count = 0
    samples = 0
    signature: tuple[int, int, int, int] | None = None
    sample_rate = 0
    reader = _PreadWindow(descriptor)

    while offset < file_size:
        remaining = file_size - offset
        if remaining < 4:
            # Fewer than four bytes can only be a torn next header at an exact
            # frame boundary; arbitrary longer garbage is never trimmed.
            return _established_mp3_plan(
                file_size, offset, frame_count, samples, sample_rate
            )
        header_bytes = reader.read(offset, 4)
        if len(header_bytes) != 4:
            return None
        header = _parse_mp3_header(header_bytes)
        if header is None:
            return None
        if signature is None:
            signature = header.signature
            sample_rate = header.signature[2]
        elif header.signature != signature:
            return None
        if header.frame_size > remaining:
            # A complete compatible header proves that the last frame, and
            # only that frame, was interrupted.
            return _established_mp3_plan(
                file_size, offset, frame_count, samples, sample_rate
            )
        offset += header.frame_size
        frame_count += 1
        samples += header.samples_per_frame

    return _established_mp3_plan(
        file_size, offset, frame_count, samples, sample_rate
    )


def _established_mp3_plan(
    original_size: int,
    safe_size: int,
    frame_count: int,
    samples: int,
    sample_rate: int,
) -> RecoveryPlan | None:
    if frame_count < MIN_MP3_FRAMES or safe_size <= 0 or sample_rate <= 0:
        return None
    return RecoveryPlan(
        format=RecordingFormat.MP3,
        original_size=original_size,
        safe_size=safe_size,
        duration_seconds=samples / sample_rate,
        unit_count=frame_count,
    )


def _parse_mp3_header(value: bytes) -> _Mp3Header | None:
    if len(value) != 4:
        return None
    header = int.from_bytes(value, "big")
    if header & 0xFFE0_0000 != 0xFFE0_0000:
        return None
    version_bits = (header >> 19) & 0x3
    layer_bits = (header >> 17) & 0x3
    bitrate_index = (header >> 12) & 0xF
    sample_rate_index = (header >> 10) & 0x3
    padding = (header >> 9) & 0x1
    channel_mode = (header >> 6) & 0x3
    if (
        version_bits == 1
        or layer_bits != 1
        or not 1 <= bitrate_index <= 14
        or sample_rate_index == 3
    ):
        return None

    mpeg1_bitrates = (
        32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320,
    )
    mpeg2_bitrates = (
        8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160,
    )
    bitrate = (mpeg1_bitrates if version_bits == 3 else mpeg2_bitrates)[
        bitrate_index - 1
    ]
    base_rate = (44_100, 48_000, 32_000)[sample_rate_index]
    sample_rate = {3: base_rate, 2: base_rate // 2, 0: base_rate // 4}.get(
        version_bits
    )
    if not sample_rate:
        return None
    coefficient = 144_000 if version_bits == 3 else 72_000
    frame_size = coefficient * bitrate // sample_rate + padding
    if not 4 <= frame_size <= MAX_MP3_FRAME_BYTES:
        return None
    return _Mp3Header(
        frame_size=frame_size,
        samples_per_frame=1_152 if version_bits == 3 else 576,
        signature=(version_bits, bitrate, sample_rate, channel_mode),
    )


def _plan_ogg_opus(descriptor: int, file_size: int) -> RecoveryPlan | None:
    if file_size <= 0:
        return None
    offset = 0
    page_count = 0
    serial: int | None = None
    next_sequence = 0
    expect_continued_packet = False
    opus_pre_skip: int | None = None
    last_granule = -1
    saw_eos = False

    while offset < file_size:
        if saw_eos:
            # An EOS page is terminal. Bytes after it are not an interrupted
            # continuation of MiniRec's single logical Opus stream.
            return None
        remaining = file_size - offset
        if remaining < 27:
            tail = os.pread(descriptor, remaining, offset)
            if _looks_like_incomplete_ogg_header(
                tail,
                serial=serial,
                sequence=next_sequence,
                expect_continued=expect_continued_packet,
                first_page=page_count == 0,
            ):
                return _established_ogg_plan(
                    file_size, offset, page_count, last_granule, opus_pre_skip
                )
            return None
        fixed = os.pread(descriptor, 27, offset)
        if len(fixed) != 27 or fixed[:4] != b"OggS" or fixed[4] != 0:
            return None
        header_type = fixed[5]
        granule_raw = int.from_bytes(fixed[6:14], "little")
        granule = -1 if granule_raw == 0xFFFF_FFFF_FFFF_FFFF else granule_raw
        current_serial = int.from_bytes(fixed[14:18], "little")
        sequence = int.from_bytes(fixed[18:22], "little")
        segment_count = fixed[26]
        continued = bool(header_type & 0x01)
        beginning = bool(header_type & 0x02)
        ending = bool(header_type & 0x04)
        if header_type & ~0x07:
            return None
        if page_count == 0:
            if not beginning or continued or sequence != 0 or ending:
                return None
        elif (
            beginning
            or continued != expect_continued_packet
            or current_serial != serial
            or sequence != next_sequence
        ):
            return None
        if granule >= 0 and last_granule >= 0 and granule < last_granule:
            return None
        segment_table = os.pread(descriptor, segment_count, offset + 27)
        if len(segment_table) != segment_count:
            return _established_ogg_plan(
                file_size, offset, page_count, last_granule, opus_pre_skip
            )
        body_size = sum(segment_table)
        page_size = 27 + segment_count + body_size
        if page_size > remaining:
            return _established_ogg_plan(
                file_size, offset, page_count, last_granule, opus_pre_skip
            )
        page = os.pread(descriptor, page_size, offset)
        if len(page) != page_size or not _valid_ogg_crc(page):
            return None

        if page_count == 0:
            serial = current_serial
            body_offset = 27 + segment_count
            if body_size < 19 or page[body_offset : body_offset + 8] != b"OpusHead":
                return None
            if page[body_offset + 8] != 1 or page[body_offset + 9] not in (1, 2):
                return None
            opus_pre_skip = int.from_bytes(
                page[body_offset + 10 : body_offset + 12], "little"
            )
        page_count += 1
        next_sequence = (sequence + 1) & 0xFFFF_FFFF
        expect_continued_packet = bool(segment_table and segment_table[-1] == 255)
        if granule >= 0:
            if last_granule >= 0 and granule < last_granule:
                return None
            last_granule = granule
        saw_eos = ending
        offset += page_size

    return _established_ogg_plan(
        file_size, offset, page_count, last_granule, opus_pre_skip
    )


def _established_ogg_plan(
    original_size: int,
    safe_size: int,
    page_count: int,
    last_granule: int,
    opus_pre_skip: int | None,
) -> RecoveryPlan | None:
    # OpusHead/comment-only prefixes contain no user audio and are kept for a
    # future/manual decision instead of being published as an empty recording.
    if (
        page_count < 2
        or safe_size <= 0
        or opus_pre_skip is None
        or last_granule <= opus_pre_skip
    ):
        return None
    samples = max(0, last_granule - opus_pre_skip)
    return RecoveryPlan(
        format=RecordingFormat.OGG_OPUS,
        original_size=original_size,
        safe_size=safe_size,
        duration_seconds=samples / OPUS_SAMPLE_RATE,
        unit_count=page_count,
    )


def _looks_like_incomplete_ogg_header(
    tail: bytes,
    *,
    serial: int | None,
    sequence: int,
    expect_continued: bool,
    first_page: bool,
) -> bool:
    """Return whether every available header byte matches this Ogg stream."""

    if not tail:
        return False
    if len(tail) <= 4:
        return b"OggS".startswith(tail)
    if not tail.startswith(b"OggS") or tail[4] != 0:
        return False
    if len(tail) >= 6:
        header_type = tail[5]
        if header_type & ~0x07:
            return False
        continued = bool(header_type & 0x01)
        beginning = bool(header_type & 0x02)
        ending = bool(header_type & 0x04)
        if first_page:
            if not beginning or continued or ending:
                return False
        elif beginning or continued != expect_continued:
            return False
    if not first_page and serial is not None and len(tail) > 14:
        available = min(len(tail), 18) - 14
        expected = serial.to_bytes(4, "little")
        if tail[14 : 14 + available] != expected[:available]:
            return False
    if len(tail) > 18:
        available = min(len(tail), 22) - 18
        expected = sequence.to_bytes(4, "little")
        if tail[18 : 18 + available] != expected[:available]:
            return False
    return True


def _valid_ogg_crc(page: bytes) -> bool:
    if len(page) < 27:
        return False
    expected = int.from_bytes(page[22:26], "little")
    checksum = 0
    for index, byte in enumerate(page):
        if 22 <= index < 26:
            byte = 0
        checksum = ((checksum << 8) & 0xFFFF_FFFF) ^ _OGG_CRC_TABLE[
            ((checksum >> 24) & 0xFF) ^ byte
        ]
    return checksum == expected


def _build_ogg_crc_table() -> tuple[int, ...]:
    values: list[int] = []
    for seed in range(256):
        value = seed << 24
        for _ in range(8):
            value = (
                ((value << 1) ^ 0x04C1_1DB7)
                if value & 0x8000_0000
                else value << 1
            ) & 0xFFFF_FFFF
        values.append(value)
    return tuple(values)


_OGG_CRC_TABLE: Final = _build_ogg_crc_table()


def _pwrite_all(descriptor: int, payload: bytes, offset: int) -> None:
    written = 0
    while written < len(payload):
        count = os.pwrite(descriptor, payload[written:], offset + written)
        if count <= 0:
            raise OSError("short pwrite while repairing recording")
        written += count


__all__ = [
    "MAX_CLASSIC_WAV_FILE_BYTES",
    "RecoveryError",
    "RecoveryIdentityError",
    "RecoveryPlan",
    "inspect_recording",
    "plan_cbr_mp3_recovery",
    "plan_ogg_opus_recovery",
    "plan_wav_recovery",
    "recover_recording",
]
