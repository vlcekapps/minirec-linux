from __future__ import annotations

from array import array
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import tempfile
import unittest

from minirec.models import ChannelMode, RecordingFormat, RecordingSettings
from minirec.recording import (
    BuiltRecordingPipeline,
    CapsSpec,
    ElementSpec,
    MAX_CLASSIC_RIFF_PCM_BYTES,
    PipelineBuildError,
    Recorder,
    RecordingEventType,
    RecordingPhase,
    RecordingSignal,
    SIGNAL_SEQUENCE_PLANS,
    build_recording_plan,
    build_gstreamer_pipeline,
    synthesize_signal_pcm,
)
from minirec.recovery import inspect_recording


class FakeGst:
    class State:
        NULL = "null"
        PLAYING = "playing"
        PAUSED = "paused"

    class StateChangeReturn:
        FAILURE = "failure"
        SUCCESS = "success"
        ASYNC = "async"
        NO_PREROLL = "no-preroll"

    class MessageType:
        EOS = "eos"
        ERROR = "error"
        ASYNC_DONE = "async-done"
        STATE_CHANGED = "state-changed"

    class PadProbeType:
        BUFFER = "buffer"

    class PadProbeReturn:
        OK = "ok"
        DROP = "drop"

    class Event:
        @staticmethod
        def new_eos() -> str:
            return "eos-event"


class FakeMessage:
    def __init__(
        self,
        message_type: str,
        detail: str = "failure",
        *,
        src=None,
    ) -> None:
        self.type = message_type
        self.detail = detail
        self.src = src

    def parse_error(self) -> tuple[Exception, str | None]:
        return RuntimeError(self.detail), None

    def parse_state_changed(self):
        return FakeGst.State.PAUSED, FakeGst.State.PLAYING, FakeGst.State.NULL


class FakeBus:
    def __init__(self, owner=None) -> None:
        self.callback = None
        self.owner = owner
        self.watch_count = 0
        self.disconnected: list[object] = []

    def add_signal_watch(self) -> None:
        self.watch_count += 1

    def remove_signal_watch(self) -> None:
        self.watch_count -= 1

    def connect(self, _name: str, callback):
        self.callback = callback
        return 17

    def disconnect(self, handler: object) -> None:
        self.disconnected.append(handler)

    def emit(self, message_type: str, detail: str = "failure") -> None:
        assert self.callback is not None
        self.callback(self, FakeMessage(message_type, detail, src=self.owner))


class FakeBuffer:
    def __init__(self, size: int) -> None:
        self.size = size

    def get_size(self) -> int:
        return self.size


class FakeProbeInfo:
    def __init__(self, size: int) -> None:
        self.buffer = FakeBuffer(size)

    def get_buffer(self) -> FakeBuffer:
        return self.buffer


class FakePad:
    def __init__(self) -> None:
        self.callback = None
        self.removed: list[object] = []

    def add_probe(self, probe_type: object, callback) -> int:
        assert probe_type == FakeGst.PadProbeType.BUFFER
        self.callback = callback
        return 23

    def remove_probe(self, probe_id: object) -> None:
        self.removed.append(probe_id)
        self.callback = None

    def push(self, size: int) -> object:
        assert self.callback is not None
        return self.callback(self, FakeProbeInfo(size))


class FakeElement:
    def __init__(self, *, src_pad: FakePad | None = None) -> None:
        self.properties: dict[str, object] = {}
        self.src_pad = src_pad

    def set_property(self, name: str, value: object) -> None:
        self.properties[name] = value

    def get_static_pad(self, name: str) -> FakePad | None:
        return self.src_pad if name == "src" else None


class FakePipeline:
    def __init__(self, state_result: str = FakeGst.StateChangeReturn.ASYNC) -> None:
        self.bus = FakeBus(self)
        self.state_result = state_result
        self.states: list[str] = []
        self.events: list[object] = []
        self.accept_eos = True

    def get_bus(self) -> FakeBus:
        return self.bus

    def set_state(self, state: str) -> str:
        self.states.append(state)
        return self.state_result

    def send_event(self, event: object) -> bool:
        self.events.append(event)
        return self.accept_eos


class FakeBuilder:
    def __init__(self, fail: set[tuple[str, int]] | None = None) -> None:
        self.fail = fail or set()
        self.plans = []
        self.pipelines: list[FakePipeline] = []

    def __call__(self, plan, _gst) -> BuiltRecordingPipeline:
        self.plans.append(plan)
        if (plan.source_factory, plan.channels) in self.fail:
            raise PipelineBuildError("candidate unavailable")
        pipeline = FakePipeline()
        self.pipelines.append(pipeline)
        elements = {"capture-gate": FakeElement()}
        if plan.format is RecordingFormat.WAV:
            elements["pcm16-caps"] = FakeElement(src_pad=FakePad())
        return BuiltRecordingPipeline(
            pipeline,
            elements,
        )


class FakeSignalPlayer:
    def __init__(self) -> None:
        self.played: list[RecordingSignal] = []
        self.callback = None
        self.closed = False

    def play(self, signal: RecordingSignal, callback) -> None:
        self.played.append(signal)
        self.callback = callback

    def finish(self, success: bool = True, detail: str | None = None) -> None:
        callback = self.callback
        self.callback = None
        assert callback is not None
        callback(success, detail)

    def close(self) -> None:
        self.closed = True
        self.callback = None


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ManualDispatcher:
    def __init__(self) -> None:
        self.pending = []

    def __call__(self, callback) -> None:
        self.pending.append(callback)

    def flush(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def properties(element) -> dict[str, object]:
    return dict(element.properties)


class RecordingPipelinePlanTest(unittest.TestCase):
    def test_classic_riff_pcm_budget_matches_unsigned_header_limit(self) -> None:
        self.assertEqual(0xFFFF_FFFF - 36, MAX_CLASSIC_RIFF_PCM_BYTES)

    def test_stereo_caps_are_immediately_after_source_and_before_conversion(self) -> None:
        plan = build_recording_plan("/tmp/recording.oga", RecordingSettings())
        self.assertEqual("pulsesrc", plan.elements[0].factory)
        self.assertEqual("input-caps", plan.elements[1].name)
        caps = properties(plan.element("input-caps"))["caps"]
        self.assertIsInstance(caps, CapsSpec)
        self.assertIn("channels=2", caps.to_string())
        self.assertIn("format=S16LE", caps.to_string())
        self.assertEqual(("audio-source", "input-caps"), plan.links[0])
        self.assertLess(
            [item.name for item in plan.elements].index("input-caps"),
            [item.name for item in plan.elements].index("audio-convert"),
        )
        gate = properties(plan.element("capture-gate"))
        self.assertIs(True, gate["drop"])
        self.assertEqual(1, gate["drop-mode"])

    def test_opus_ogg_is_default_and_uses_requested_bitrate(self) -> None:
        plan = build_recording_plan(
            "/tmp/recording.oga",
            RecordingSettings(bitrate_kbps=96),
        )
        self.assertEqual("opusenc", plan.element("audio-encoder").factory)
        self.assertEqual(96_000, properties(plan.element("audio-encoder"))["bitrate"])
        self.assertEqual("oggmux", plan.element("container-muxer").factory)

    def test_mp3_plan_is_explicit_cbr_for_every_supported_channel_mode(self) -> None:
        for mode in (ChannelMode.MONO, ChannelMode.STEREO):
            plan = build_recording_plan(
                "/tmp/recording.mp3",
                RecordingSettings(
                    format=RecordingFormat.MP3,
                    bitrate_kbps=320,
                    channel_mode=mode,
                ),
            )
            encoder = properties(plan.element("audio-encoder"))
            self.assertEqual(1, encoder["target"])
            self.assertEqual(320, encoder["bitrate"])
            self.assertIs(True, encoder["cbr"])
            self.assertEqual(mode is ChannelMode.MONO, encoder["mono"])
            self.assertEqual("mpegaudioparse", plan.element("audio-parser").factory)

    def test_wav_plan_forces_interleaved_pcm16(self) -> None:
        plan = build_recording_plan(
            "/tmp/recording.wav",
            RecordingSettings(format=RecordingFormat.WAV),
            channels=1,
        )
        caps = properties(plan.element("pcm16-caps"))["caps"].to_string()
        self.assertIn("format=S16LE", caps)
        self.assertIn("layout=interleaved", caps)
        self.assertIn("channels=1", caps)
        self.assertEqual("wavenc", plan.element("container-muxer").factory)
        self.assertEqual(
            "audio/x-wav",
            properties(plan.element("classic-wav-caps"))["caps"].to_string(),
        )

    def test_gain_is_assigned_as_linear_property_not_launch_text(self) -> None:
        settings = RecordingSettings(gain_db=12)
        plan = build_recording_plan("/tmp/name with ! syntax.oga", settings)
        self.assertAlmostEqual(settings.linear_gain, properties(plan.element("input-gain"))["volume"])
        self.assertEqual(
            "/tmp/name with ! syntax.oga",
            properties(plan.element("output-file"))["location"],
        )

    def test_verified_descriptor_uses_fdsink_instead_of_reopening_path(self) -> None:
        plan = build_recording_plan(
            "/tmp/pending.oga",
            RecordingSettings(),
            output_fd=42,
        )
        sink = plan.element("output-file")
        self.assertEqual("fdsink", sink.factory)
        self.assertEqual(42, properties(sink)["fd"])
        self.assertNotIn("location", properties(sink))

    def test_only_known_desktop_sources_and_exact_channels_are_accepted(self) -> None:
        with self.assertRaises(ValueError):
            build_recording_plan("/tmp/a.oga", RecordingSettings(), source_factory="alsasrc")
        with self.assertRaises(ValueError):
            build_recording_plan("/tmp/a.oga", RecordingSettings(), channels=6)


class SignalPlanTest(unittest.TestCase):
    def test_android_compatible_tone_order_and_silence_are_exact(self) -> None:
        def compact(signal: RecordingSignal):
            return [
                (segment.frequency_hz, segment.duration_ms)
                for segment in SIGNAL_SEQUENCE_PLANS[signal]
            ]

        start = [(440.0, 100), (580.0, 100), (None, 100)]
        self.assertEqual(start, compact(RecordingSignal.START))
        self.assertEqual(start, compact(RecordingSignal.RESUME))
        self.assertEqual(
            [(580.0, 100), (None, 60), (580.0, 100)],
            compact(RecordingSignal.PAUSE),
        )
        self.assertEqual(
            [(580.0, 100), (440.0, 100), (None, 60), (440.0, 100)],
            compact(RecordingSignal.STOP),
        )

    def test_pcm_length_silence_tail_and_amplitude_are_deterministic(self) -> None:
        pcm = synthesize_signal_pcm(RecordingSignal.START, sample_rate=10_000)
        samples = array("h")
        samples.frombytes(pcm)
        self.assertEqual(3_000, len(samples))
        self.assertTrue(any(samples[:2_000]))
        self.assertEqual({0}, set(samples[2_000:]))
        self.assertLessEqual(max(abs(value) for value in samples), round(32_767 * 0.25))
        self.assertGreater(max(abs(value) for value in samples), 8_000)

    def test_all_pcm_durations_match_their_plans(self) -> None:
        for signal, plan in SIGNAL_SEQUENCE_PLANS.items():
            expected_samples = sum(segment.duration_ms for segment in plan) * 48
            self.assertEqual(expected_samples * 2, len(synthesize_signal_pcm(signal)))


class RecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pending.oga"
        self.clock = FakeClock()
        self.signals = FakeSignalPlayer()
        self.builder = FakeBuilder()
        self.events = []
        self.recorder = Recorder(
            on_event=self.events.append,
            gst=FakeGst,
            clock=self.clock,
            signal_player=self.signals,
            pipeline_builder=self.builder,
        )

    def start_and_finish_signal(self) -> FakePipeline:
        self.assertTrue(self.recorder.start(self.path))
        pipeline = self.builder.pipelines[-1]
        self.assertEqual(RecordingPhase.STARTING, self.recorder.phase)
        self.assertEqual([RecordingSignal.START], self.signals.played)
        self.assertIs(True, self.builder.plans[-1].output_fd is not None)
        self.assertIs(True, self.builder.pipelines[-1].states[-1] == FakeGst.State.PLAYING)
        self.assertIs(True, self.recorder._built.elements["capture-gate"].properties["drop"])
        self.signals.finish()
        self.assertEqual(RecordingPhase.STARTING, self.recorder.phase)
        pipeline.bus.emit(FakeGst.MessageType.ASYNC_DONE)
        self.assertEqual(RecordingPhase.RECORDING, self.recorder.phase)
        self.assertIs(False, self.recorder._built.elements["capture-gate"].properties["drop"])
        return pipeline

    def test_start_atomically_creates_private_output_and_never_overwrites(self) -> None:
        self.assertTrue(self.recorder.start(self.path))
        self.assertTrue(self.path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

        other = Recorder(
            gst=FakeGst,
            signal_player=FakeSignalPlayer(),
            pipeline_builder=FakeBuilder(),
        )
        with self.assertRaises(FileExistsError):
            other.start(self.path)

    def test_prepared_empty_regular_file_is_adopted_without_truncating_data(self) -> None:
        self.path.touch(mode=0o600)
        self.assertTrue(self.recorder.start(self.path, prepared=True))
        self.assertEqual("fdsink", self.builder.plans[0].element("output-file").factory)

        nonempty = Path(self.temp.name) / "nonempty.oga"
        nonempty.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            Recorder(
                gst=FakeGst,
                signal_player=FakeSignalPlayer(),
                pipeline_builder=FakeBuilder(),
            ).start(nonempty, prepared=True)
        self.assertEqual(b"do not overwrite", nonempty.read_bytes())

    def test_prepared_symlink_is_rejected(self) -> None:
        target = Path(self.temp.name) / "target.oga"
        target.touch()
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.recorder.start(self.path, prepared=True)

    def test_elapsed_excludes_start_pause_resume_and_stop_cues(self) -> None:
        pipeline = self.start_and_finish_signal()
        self.clock.advance(5)
        self.assertEqual(5, self.recorder.elapsed_seconds)

        self.assertTrue(self.recorder.pause())
        self.assertEqual(RecordingPhase.PAUSING, self.recorder.phase)
        self.clock.advance(20)
        self.signals.finish()
        self.assertEqual(RecordingPhase.PAUSED, self.recorder.phase)
        self.assertEqual(5, self.recorder.elapsed_seconds)
        self.assertEqual(FakeGst.State.PAUSED, pipeline.states[-1])

        self.assertTrue(self.recorder.resume())
        self.clock.advance(20)
        self.signals.finish()
        self.clock.advance(3)
        self.assertEqual(8, self.recorder.elapsed_seconds)

        self.assertTrue(self.recorder.stop())
        self.clock.advance(20)
        self.assertEqual(8, self.recorder.elapsed_seconds)

    def test_stop_signal_starts_only_after_eos_pipeline_null_and_fd_close(self) -> None:
        pipeline = self.start_and_finish_signal()
        self.assertTrue(self.recorder.stop())
        self.assertEqual([RecordingSignal.START], self.signals.played)
        self.assertEqual(["eos-event"], pipeline.events)
        self.assertEqual(RecordingPhase.STOPPING, self.recorder.phase)

        descriptor = self.builder.plans[-1].output_fd
        assert descriptor is not None
        os.fstat(descriptor)  # still open before EOS
        pipeline.bus.emit(FakeGst.MessageType.EOS)
        self.assertEqual(FakeGst.State.NULL, pipeline.states[-1])
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertEqual(
            [RecordingSignal.START, RecordingSignal.STOP],
            self.signals.played,
        )
        self.assertEqual(RecordingPhase.STOPPING, self.recorder.phase)
        self.assertFalse(any(event.type is RecordingEventType.FINALIZED for event in self.events))

        self.signals.finish()
        self.assertEqual(RecordingPhase.STOPPED, self.recorder.phase)
        self.assertTrue(any(event.type is RecordingEventType.FINALIZED for event in self.events))

    def test_paused_stop_wakes_closed_gate_only_to_deliver_eos(self) -> None:
        pipeline = self.start_and_finish_signal()
        self.recorder.pause()
        self.signals.finish()
        self.assertTrue(self.recorder.stop())
        self.assertEqual(FakeGst.State.PLAYING, pipeline.states[-1])
        self.assertIs(True, self.recorder._built.elements["capture-gate"].properties["drop"])

    def test_wav_limit_drops_first_overflow_buffer_then_defers_safe_eos(self) -> None:
        path = Path(self.temp.name) / "near-limit.wav"
        builder = FakeBuilder()
        signals = FakeSignalPlayer()
        dispatcher = ManualDispatcher()
        events = []
        recorder = Recorder(
            RecordingSettings(
                format=RecordingFormat.WAV,
                channel_mode=ChannelMode.STEREO,
            ),
            events.append,
            gst=FakeGst,
            clock=self.clock,
            signal_player=signals,
            pipeline_builder=builder,
            dispatcher=dispatcher,
            # Stereo PCM frames are four bytes, so this deliberately aligns
            # down to eight bytes without allocating a multi-gigabyte buffer.
            wav_data_limit_bytes=10,
        )

        self.assertTrue(recorder.start(path))
        pipeline = builder.pipelines[-1]
        signals.finish()
        pipeline.bus.emit(FakeGst.MessageType.ASYNC_DONE)
        self.assertEqual(RecordingPhase.RECORDING, recorder.phase)
        pad = recorder._built.elements["pcm16-caps"].src_pad
        assert pad is not None

        self.assertEqual(FakeGst.PadProbeReturn.OK, pad.push(8))
        self.assertEqual(FakeGst.PadProbeReturn.DROP, pad.push(4))
        self.assertIs(
            True,
            recorder._built.elements["capture-gate"].properties["drop"],
        )
        self.assertEqual(RecordingPhase.RECORDING, recorder.phase)
        self.assertEqual([], pipeline.events)
        self.assertEqual(1, len(dispatcher.pending))

        # Every later buffer remains blocked, while only one main-context
        # transition is queued.
        self.assertEqual(FakeGst.PadProbeReturn.DROP, pad.push(4))
        self.assertEqual(1, len(dispatcher.pending))
        dispatcher.flush()
        self.assertEqual(RecordingPhase.STOPPING, recorder.phase)
        self.assertEqual(["eos-event"], pipeline.events)

        pipeline.bus.emit(FakeGst.MessageType.EOS)
        self.assertEqual(RecordingSignal.STOP, signals.played[-1])
        signals.finish()
        self.assertEqual(RecordingPhase.STOPPED, recorder.phase)
        self.assertTrue(
            any(event.type is RecordingEventType.FINALIZED for event in events)
        )

    def test_stalled_stop_timeout_preserves_pending_for_recovery(self) -> None:
        pipeline = self.start_and_finish_signal()
        self.assertTrue(self.recorder.stop())
        descriptor = self.builder.plans[-1].output_fd
        assert descriptor is not None

        self.assertTrue(self.recorder.timeout_stalled_stop())
        self.assertEqual(RecordingPhase.ERROR, self.recorder.phase)
        self.assertTrue(self.path.exists())
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertEqual(FakeGst.State.NULL, pipeline.states[-1])
        self.assertTrue(
            any(event.type is RecordingEventType.ERROR for event in self.events)
        )
        self.assertFalse(self.recorder.timeout_stalled_stop())

    def test_forced_process_exit_stops_io_and_retains_pending_file(self) -> None:
        pipeline = self.start_and_finish_signal()
        descriptor = self.builder.plans[-1].output_fd
        assert descriptor is not None

        self.recorder.emergency_close_for_process_exit()

        self.assertEqual(RecordingPhase.CLOSED, self.recorder.phase)
        self.assertEqual(FakeGst.State.NULL, pipeline.states[-1])
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertTrue(self.path.exists())

    def test_source_then_clean_mono_fallback_order_is_visible(self) -> None:
        self.builder.fail = {
            ("pulsesrc", 2),
            ("autoaudiosrc", 2),
        }
        self.assertTrue(self.recorder.start(self.path))
        self.assertEqual(
            [("pulsesrc", 2), ("autoaudiosrc", 2), ("pulsesrc", 1)],
            [(plan.source_factory, plan.channels) for plan in self.builder.plans],
        )
        self.assertEqual(1, self.recorder.snapshot.active_channels)
        self.assertTrue(
            any(event.type is RecordingEventType.CHANNEL_FALLBACK for event in self.events)
        )

    def test_autoaudiosrc_fallback_is_visible(self) -> None:
        self.builder.fail = {("pulsesrc", 2)}
        self.assertTrue(self.recorder.start(self.path))
        self.assertEqual("autoaudiosrc", self.recorder.snapshot.source_factory)
        self.assertTrue(
            any(event.type is RecordingEventType.SOURCE_FALLBACK for event in self.events)
        )

    def test_asynchronous_start_error_retries_next_candidate(self) -> None:
        self.assertTrue(self.recorder.start(self.path))
        first = self.builder.pipelines[-1]
        first.bus.emit(FakeGst.MessageType.ERROR, "pulse refused device")
        self.assertEqual(2, len(self.builder.plans))
        self.assertEqual("autoaudiosrc", self.builder.plans[-1].source_factory)
        self.assertEqual(RecordingPhase.STARTING, self.recorder.phase)

    def test_error_after_cue_before_async_done_still_retries_candidate(self) -> None:
        self.assertTrue(self.recorder.start(self.path))
        first = self.builder.pipelines[-1]
        self.signals.finish()
        self.assertEqual(RecordingPhase.STARTING, self.recorder.phase)
        self.assertIs(
            False,
            self.recorder._built.elements["capture-gate"].properties["drop"],
        )
        first.bus.emit(FakeGst.MessageType.ERROR, "late negotiation failure")
        self.assertEqual(2, len(self.builder.plans))
        self.assertEqual("autoaudiosrc", self.builder.plans[-1].source_factory)
        self.assertEqual([RecordingSignal.START], self.signals.played)
        self.assertEqual(RecordingPhase.STARTING, self.recorder.phase)
        second = self.builder.pipelines[-1]
        second.bus.emit(FakeGst.MessageType.ASYNC_DONE)
        self.assertEqual(RecordingPhase.RECORDING, self.recorder.phase)

    def test_error_after_start_never_changes_channels_mid_file(self) -> None:
        pipeline = self.start_and_finish_signal()
        pipeline.bus.emit(FakeGst.MessageType.ERROR, "device removed")
        self.assertEqual(RecordingPhase.ERROR, self.recorder.phase)
        self.assertEqual(1, len(self.builder.plans))
        self.assertIn("device removed", self.recorder.snapshot.error or "")

    def test_signal_failure_does_not_block_capture_and_is_reported(self) -> None:
        self.assertTrue(self.recorder.start(self.path))
        self.signals.finish(False, "no output device")
        self.builder.pipelines[-1].bus.emit(FakeGst.MessageType.ASYNC_DONE)
        self.assertEqual(RecordingPhase.RECORDING, self.recorder.phase)
        self.assertTrue(
            any(event.type is RecordingEventType.SIGNAL_ERROR for event in self.events)
        )

    def test_close_during_capture_waits_for_eos_and_stop_signal(self) -> None:
        pipeline = self.start_and_finish_signal()
        self.recorder.close()
        self.assertEqual(RecordingPhase.STOPPING, self.recorder.phase)
        self.assertFalse(self.signals.closed)
        pipeline.bus.emit(FakeGst.MessageType.EOS)
        self.assertFalse(self.signals.closed)
        self.signals.finish()
        self.assertEqual(RecordingPhase.CLOSED, self.recorder.phase)
        self.assertTrue(self.signals.closed)

    def test_callback_exception_is_isolated_from_audio_state(self) -> None:
        recorder = Recorder(
            on_event=lambda _event: (_ for _ in ()).throw(RuntimeError("UI failed")),
            gst=FakeGst,
            signal_player=FakeSignalPlayer(),
            pipeline_builder=FakeBuilder(),
        )
        self.assertTrue(recorder.start(self.path))
        self.assertEqual(RecordingPhase.STARTING, recorder.phase)


class GStreamerValveEosTest(unittest.TestCase):
    """Exercise the real valve semantics which fakes cannot model."""

    def test_closed_capture_gate_forwards_eos(self) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError) as error:
            self.skipTest(f"GStreamer introspection is unavailable: {error}")

        Gst.init(None)
        pipeline = Gst.parse_launch(
            "audiotestsrc num-buffers=1 ! "
            "valve drop=true drop-mode=forward-sticky-events ! fakesink"
        )
        try:
            self.assertNotEqual(
                Gst.StateChangeReturn.FAILURE,
                pipeline.set_state(Gst.State.PLAYING),
            )
            message = pipeline.get_bus().timed_pop_filtered(
                2 * Gst.SECOND,
                Gst.MessageType.EOS | Gst.MessageType.ERROR,
            )
            self.assertIsNotNone(message, "closed valve swallowed EOS")
            assert message is not None
            if message.type == Gst.MessageType.ERROR:
                failure, debug = message.parse_error()
                self.fail(f"GStreamer pipeline failed: {failure}; {debug or ''}")
            self.assertEqual(Gst.MessageType.EOS, message.type)
        finally:
            pipeline.set_state(Gst.State.NULL)


class GStreamerWavLimitTest(unittest.TestCase):
    """Exercise the real pad probe, GLib dispatch and classic WAV muxer."""

    def test_real_wav_guard_finalizes_a_bounded_riff_file(self) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import GLib, Gst
        except (ImportError, ValueError) as error:
            self.skipTest(f"GStreamer introspection is unavailable: {error}")

        class ImmediateSignalPlayer:
            def play(self, _signal, callback) -> None:
                callback(True, None)

            def close(self) -> None:
                return

        Gst.init(None)
        loop = GLib.MainLoop()
        terminal_events = []
        timed_out = False

        def on_event(event) -> None:
            if event.type in {
                RecordingEventType.FINALIZED,
                RecordingEventType.ERROR,
            }:
                terminal_events.append(event)
                loop.quit()

        def synthetic_builder(plan, gst) -> BuiltRecordingPipeline:
            source = ElementSpec(
                "audiotestsrc",
                "audio-source",
                (("is-live", True), ("wave", 4)),
            )
            return build_gstreamer_pipeline(
                replace(plan, elements=(source, *plan.elements[1:])),
                gst,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bounded.wav"
            recorder = Recorder(
                RecordingSettings(
                    format=RecordingFormat.WAV,
                    channel_mode=ChannelMode.STEREO,
                ),
                on_event,
                gst=Gst,
                signal_player=ImmediateSignalPlayer(),
                pipeline_builder=synthetic_builder,
                source_factories=("autoaudiosrc",),
                wav_data_limit_bytes=8_192,
            )

            def timeout() -> bool:
                nonlocal timed_out
                timed_out = True
                loop.quit()
                return GLib.SOURCE_REMOVE

            timeout_id = GLib.timeout_add_seconds(5, timeout)
            try:
                self.assertTrue(recorder.start(path))
                loop.run()
            finally:
                if not timed_out:
                    GLib.source_remove(timeout_id)
                if recorder.phase not in {
                    RecordingPhase.STOPPED,
                    RecordingPhase.CLOSED,
                }:
                    recorder.emergency_close_for_process_exit()

            self.assertFalse(timed_out, "WAV limit did not complete EOS")
            self.assertTrue(terminal_events)
            self.assertEqual(
                RecordingEventType.FINALIZED,
                terminal_events[-1].type,
                terminal_events[-1].detail,
            )
            payload = path.read_bytes()
            self.assertEqual(b"RIFF", payload[:4])
            self.assertGreater(len(payload), 44)
            self.assertLessEqual(len(payload), 44 + 8_192)
            verified = inspect_recording(path, RecordingFormat.WAV)
            self.assertIsNotNone(verified)


class GStreamerCodecOutputTest(unittest.TestCase):
    """Run every shipped encoder/muxer headlessly through MiniRec's plan."""

    def test_mono_and_stereo_outputs_are_finalized_and_format_verifiable(self) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError) as error:
            self.skipTest(f"GStreamer introspection is unavailable: {error}")

        Gst.init(None)
        with tempfile.TemporaryDirectory() as directory:
            for recording_format in RecordingFormat:
                for channel_mode in ChannelMode:
                    with self.subTest(
                        recording_format=recording_format.name,
                        channel_mode=channel_mode.name,
                    ):
                        path = Path(directory) / (
                            f"codec-{channel_mode.name.casefold()}"
                            f"{recording_format.extension}"
                        )
                        self._verify_output(
                            Gst,
                            path,
                            recording_format,
                            channel_mode,
                        )

    def _verify_output(
        self,
        Gst,
        path: Path,
        recording_format: RecordingFormat,
        channel_mode: ChannelMode,
    ) -> None:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR,
            0o600,
        )
        pipeline = None
        try:
            settings = RecordingSettings(
                format=recording_format,
                bitrate_kbps=128,
                channel_mode=channel_mode,
            )
            plan = build_recording_plan(
                path,
                settings,
                source_factory="autoaudiosrc",
                output_fd=descriptor,
            )
            source = ElementSpec(
                "audiotestsrc",
                "audio-source",
                (("num-buffers", 64), ("wave", 4)),
            )
            plan = replace(
                plan,
                elements=(source, *plan.elements[1:]),
            )
            built = build_gstreamer_pipeline(plan, Gst)
            pipeline = built.pipeline
            built.elements["capture-gate"].set_property("drop", False)
            self.assertNotEqual(
                Gst.StateChangeReturn.FAILURE,
                pipeline.set_state(Gst.State.PLAYING),
            )
            message = pipeline.get_bus().timed_pop_filtered(
                5 * Gst.SECOND,
                Gst.MessageType.EOS | Gst.MessageType.ERROR,
            )
            self.assertIsNotNone(message, "codec pipeline timed out")
            assert message is not None
            if message.type == Gst.MessageType.ERROR:
                failure, debug = message.parse_error()
                self.fail(f"codec failed: {failure}; {debug or ''}")
            self.assertEqual(Gst.MessageType.EOS, message.type)
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            os.fsync(descriptor)
            os.close(descriptor)

        verified = inspect_recording(path, recording_format)
        self.assertIsNotNone(verified)
        assert verified is not None
        self.assertGreater(verified.safe_size, 0)
        self.assertGreater(verified.duration_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
