from __future__ import annotations

import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from minirec.models import RecordingFormat
from minirec.recovery import (
    RecoveryIdentityError,
    plan_cbr_mp3_recovery,
    plan_ogg_opus_recovery,
    plan_wav_recovery,
    recover_recording,
)


def pcm_wav(payload: bytes, *, channels: int = 2, declared_data_size: int = 0) -> bytes:
    sample_rate = 48_000
    block_align = channels * 2
    byte_rate = sample_rate * block_align
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", 0),
            b"WAVE",
            b"fmt ",
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
            struct.pack("<I", declared_data_size),
            payload,
        )
    )


def mp3_frame(*, bitrate_index: int = 9, channel_mode: int = 0) -> bytes:
    # MPEG-1 Layer III, 48 kHz. Bitrate index 9 is 128 kb/s and 384 bytes.
    header = (
        0xFFE0_0000
        | (3 << 19)
        | (1 << 17)
        | (1 << 16)
        | (bitrate_index << 12)
        | (1 << 10)
        | (channel_mode << 6)
    )
    bitrates = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    frame_size = 144_000 * bitrates[bitrate_index - 1] // 48_000
    return header.to_bytes(4, "big") + bytes([0x55]) * (frame_size - 4)


def ogg_crc(payload: bytes) -> int:
    table: list[int] = []
    for seed in range(256):
        value = seed << 24
        for _ in range(8):
            value = (
                ((value << 1) ^ 0x04C1_1DB7)
                if value & 0x8000_0000
                else value << 1
            ) & 0xFFFF_FFFF
        table.append(value)
    checksum = 0
    for byte in payload:
        checksum = ((checksum << 8) & 0xFFFF_FFFF) ^ table[
            ((checksum >> 24) & 0xFF) ^ byte
        ]
    return checksum


def ogg_page(
    body: bytes,
    *,
    sequence: int,
    granule: int,
    header_type: int = 0,
    serial: int = 0x1234ABCD,
) -> bytes:
    if len(body) > 255:
        raise ValueError("test fixture uses one lacing value")
    header = bytearray(27)
    header[:4] = b"OggS"
    header[4] = 0
    header[5] = header_type
    header[6:14] = granule.to_bytes(8, "little")
    header[14:18] = serial.to_bytes(4, "little")
    header[18:22] = sequence.to_bytes(4, "little")
    header[26] = 1
    page = header + bytes([len(body)]) + body
    page[22:26] = ogg_crc(page).to_bytes(4, "little")
    return bytes(page)


def opus_stream() -> tuple[bytes, int]:
    pre_skip = 312
    head = (
        b"OpusHead"
        + bytes((1, 2))
        + pre_skip.to_bytes(2, "little")
        + (48_000).to_bytes(4, "little")
        + (0).to_bytes(2, "little", signed=True)
        + b"\x00"
    )
    tags = b"OpusTags" + (0).to_bytes(4, "little") + (0).to_bytes(4, "little")
    pages = (
        ogg_page(head, sequence=0, granule=0, header_type=0x02)
        + ogg_page(tags, sequence=1, granule=0)
        + ogg_page(b"\xf8\xff\xfe", sequence=2, granule=pre_skip + 48_000)
    )
    return pages, pre_skip


class WavRecoveryTest(unittest.TestCase):
    def test_repairs_sizes_and_trims_only_partial_pcm_frame(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.wav"
            path.write_bytes(pcm_wav(b"\x11" * 11, channels=2))

            plan = plan_wav_recovery(path)
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(52, plan.safe_size)
            self.assertEqual(2, plan.unit_count)

            metadata = path.stat()
            recovered = recover_recording(
                path,
                RecordingFormat.WAV,
                expected_device=metadata.st_dev,
                expected_inode=metadata.st_ino,
            )
            self.assertEqual(plan, recovered)
            payload = path.read_bytes()
            self.assertEqual(52, len(payload))
            self.assertEqual(44, int.from_bytes(payload[4:8], "little"))
            self.assertEqual(8, int.from_bytes(payload[40:44], "little"))

    def test_rejects_non_pcm_or_header_only_wav(self) -> None:
        with TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.wav"
            payload = bytearray(pcm_wav(b"\x00" * 8))
            payload[20:22] = (3).to_bytes(2, "little")
            invalid.write_bytes(payload)
            self.assertIsNone(plan_wav_recovery(invalid))

            empty_audio = Path(directory) / "header.wav"
            empty_audio.write_bytes(pcm_wav(b""))
            self.assertIsNone(plan_wav_recovery(empty_audio))

    def test_identity_mismatch_prevents_every_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            original = pcm_wav(b"\x00" * 9)
            path.write_bytes(original)
            metadata = path.stat()
            with self.assertRaises(RecoveryIdentityError):
                recover_recording(
                    path,
                    RecordingFormat.WAV,
                    expected_device=metadata.st_dev,
                    expected_inode=metadata.st_ino + 1,
                )
            self.assertEqual(original, path.read_bytes())


class Mp3RecoveryTest(unittest.TestCase):
    def test_contiguous_compatible_frames_and_incomplete_last_frame(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.mp3"
            frame = mp3_frame()
            path.write_bytes(frame + frame + frame[:100])
            plan = plan_cbr_mp3_recovery(path)
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(len(frame) * 2, plan.safe_size)
            self.assertEqual(2, plan.unit_count)
            self.assertAlmostEqual(2 * 1_152 / 48_000, plan.duration_seconds)

            metadata = path.stat()
            recover_recording(
                path,
                RecordingFormat.MP3,
                expected_device=metadata.st_dev,
                expected_inode=metadata.st_ino,
            )
            self.assertEqual(frame + frame, path.read_bytes())

    def test_never_skips_invalid_or_incompatible_tail(self) -> None:
        with TemporaryDirectory() as directory:
            frame = mp3_frame()
            garbage = Path(directory) / "garbage.mp3"
            garbage.write_bytes(frame + frame + b"BAD!")
            self.assertIsNone(plan_cbr_mp3_recovery(garbage))

            changed = Path(directory) / "changed.mp3"
            changed.write_bytes(frame + frame + mp3_frame(bitrate_index=10)[:100])
            self.assertIsNone(plan_cbr_mp3_recovery(changed))

    def test_requires_two_frames_from_byte_zero(self) -> None:
        with TemporaryDirectory() as directory:
            one = Path(directory) / "one.mp3"
            one.write_bytes(mp3_frame())
            self.assertIsNone(plan_cbr_mp3_recovery(one))
            tagged = Path(directory) / "tagged.mp3"
            tagged.write_bytes(b"ID3" + mp3_frame() * 2)
            self.assertIsNone(plan_cbr_mp3_recovery(tagged))


class OggOpusRecoveryTest(unittest.TestCase):
    def test_crc_checked_pages_trim_an_incomplete_next_page(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.oga"
            stream, _ = opus_stream()
            incomplete = ogg_page(
                b"another packet", sequence=3, granule=96_312, header_type=0x04
            )
            path.write_bytes(stream + incomplete[:20])
            plan = plan_ogg_opus_recovery(path)
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(len(stream), plan.safe_size)
            self.assertEqual(3, plan.unit_count)
            self.assertAlmostEqual(1.0, plan.duration_seconds)

            metadata = os.stat(path)
            recover_recording(
                path,
                RecordingFormat.OGG_OPUS,
                expected_device=metadata.st_dev,
                expected_inode=metadata.st_ino,
            )
            self.assertEqual(stream, path.read_bytes())

    def test_incomplete_page_must_match_established_stream_header(self) -> None:
        with TemporaryDirectory() as directory:
            stream, _ = opus_stream()
            path = Path(directory) / "voice.oga"
            incompatible_pages = (
                ogg_page(b"next", sequence=3, granule=96_312, serial=7),
                ogg_page(b"next", sequence=9, granule=96_312),
                ogg_page(
                    b"next", sequence=3, granule=96_312, header_type=0x01
                ),
            )
            for page in incompatible_pages:
                with self.subTest(header=page[:27]):
                    path.write_bytes(stream + page[:27])
                    self.assertIsNone(plan_ogg_opus_recovery(path))

    def test_incomplete_header_boundaries_are_safe_and_stream_matched(self) -> None:
        with TemporaryDirectory() as directory:
            stream, _ = opus_stream()
            path = Path(directory) / "voice.oga"
            compatible = ogg_page(
                b"next", sequence=3, granule=96_312, header_type=0x04
            )
            for length in (1, 4, 5, 6, 14, 18, 22, 26):
                with self.subTest(length=length):
                    path.write_bytes(stream + compatible[:length])
                    plan = plan_ogg_opus_recovery(path)
                    self.assertIsNotNone(plan)
                    assert plan is not None
                    self.assertEqual(len(stream), plan.safe_size)

            incompatible = (
                b"OggS\x01",
                b"OggS\x00\x80",
                compatible[:14] + (7).to_bytes(4, "little"),
                compatible[:18] + (9).to_bytes(4, "little"),
            )
            for tail in incompatible:
                with self.subTest(tail=tail):
                    path.write_bytes(stream + tail)
                    self.assertIsNone(plan_ogg_opus_recovery(path))

    def test_rejects_bad_crc_sequence_and_random_tail(self) -> None:
        with TemporaryDirectory() as directory:
            stream, _ = opus_stream()
            bad_crc = bytearray(stream)
            bad_crc[-1] ^= 1
            path = Path(directory) / "bad.oga"
            path.write_bytes(bad_crc)
            self.assertIsNone(plan_ogg_opus_recovery(path))

            random_tail = Path(directory) / "tail.oga"
            random_tail.write_bytes(stream + b"junk")
            self.assertIsNone(plan_ogg_opus_recovery(random_tail))

            after_eos = Path(directory) / "after-eos.oga"
            after_eos.write_bytes(
                stream
                + ogg_page(
                    b"final", sequence=3, granule=96_312, header_type=0x04
                )
                + b"Ogg"
            )
            self.assertIsNone(plan_ogg_opus_recovery(after_eos))

    def test_rejects_non_opus_and_header_only_prefix(self) -> None:
        with TemporaryDirectory() as directory:
            not_opus = Path(directory) / "other.oga"
            not_opus.write_bytes(
                ogg_page(b"vorbis", sequence=0, granule=0, header_type=0x02)
            )
            self.assertIsNone(plan_ogg_opus_recovery(not_opus))


if __name__ == "__main__":
    unittest.main()
