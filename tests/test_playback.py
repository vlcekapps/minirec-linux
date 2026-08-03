from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import wave

from minirec.playback import (
    PlaybackError,
    PlaybackEventType,
    PlaybackPhase,
    Player,
    SUPPORTED_PLAYBACK_SPEEDS,
    media_uri,
)


SECOND = 1_000_000_000


class FakeMessage:
    def __init__(self, message_type: str, detail: str = "decoder failed") -> None:
        self.type = message_type
        self.detail = detail

    def parse_error(self):
        return RuntimeError(self.detail), "debug context"


class FakeBus:
    def __init__(self) -> None:
        self.callback = None
        self.watching = False
        self.disconnected: list[object] = []

    def add_signal_watch(self) -> None:
        self.watching = True

    def remove_signal_watch(self) -> None:
        self.watching = False

    def connect(self, _name: str, callback):
        self.callback = callback
        return 23

    def disconnect(self, handler: object) -> None:
        self.disconnected.append(handler)

    def emit(self, message_type: str, detail: str = "decoder failed") -> None:
        assert self.callback is not None
        self.callback(self, FakeMessage(message_type, detail))


class FakeFilter:
    pass


class FakePlaybin:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.properties: dict[str, object] = {}
        self.states: list[str] = []
        self.state_result = "async"
        self.position = 0
        self.duration = 120 * SECOND
        self.query_position_success = True
        self.query_duration_success = True
        self.seek_result = True
        self.uri_error: Exception | None = None
        self.seeks: list[tuple[object, ...]] = []

    def get_bus(self) -> FakeBus:
        return self.bus

    def set_property(self, name: str, value: object) -> None:
        if name == "uri" and self.uri_error is not None:
            raise self.uri_error
        self.properties[name] = value

    def set_state(self, state: str) -> str:
        self.states.append(state)
        return self.state_result

    def query_position(self, _format: object):
        return self.query_position_success, self.position

    def query_duration(self, _format: object):
        return self.query_duration_success, self.duration

    def seek(self, *arguments: object) -> bool:
        self.seeks.append(arguments)
        if self.seek_result:
            self.position = int(arguments[4])
        return self.seek_result


class FakeElementFactory:
    def __init__(
        self,
        playbin: FakePlaybin,
        *,
        playbin3: bool = True,
        scaletempo: bool = True,
    ) -> None:
        self.playbin = playbin
        self.playbin3 = playbin3
        self.scaletempo = scaletempo
        self.calls: list[str] = []

    def make(self, factory: str, _name: str):
        self.calls.append(factory)
        if factory == "playbin3":
            return self.playbin if self.playbin3 else None
        if factory == "playbin":
            return self.playbin
        if factory == "scaletempo":
            return FakeFilter() if self.scaletempo else None
        return None


class FakeGst:
    class State:
        NULL = "null"
        PAUSED = "paused"
        PLAYING = "playing"

    class StateChangeReturn:
        FAILURE = "failure"
        SUCCESS = "success"
        ASYNC = "async"
        NO_PREROLL = "no-preroll"

    class MessageType:
        ASYNC_DONE = "async-done"
        EOS = "eos"
        ERROR = "error"

    class Format:
        TIME = "time"

    class SeekFlags:
        FLUSH = 1
        ACCURATE = 2

    class SeekType:
        SET = "set"
        NONE = "none"

    def __init__(
        self,
        playbin: FakePlaybin,
        *,
        playbin3: bool = True,
        scaletempo: bool = True,
    ) -> None:
        self.ElementFactory = FakeElementFactory(
            playbin,
            playbin3=playbin3,
            scaletempo=scaletempo,
        )


class PlayerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.playbin = FakePlaybin()
        self.gst = FakeGst(self.playbin)
        self.events = []
        self.player = Player(self.events.append, gst=self.gst)

    def make_ready(self) -> None:
        self.assertTrue(self.player.open("/tmp/voice.oga"))
        self.assertEqual(PlaybackPhase.PREPARING, self.player.phase)
        self.playbin.bus.emit(FakeGst.MessageType.ASYNC_DONE)
        self.assertEqual(PlaybackPhase.READY, self.player.phase)

    def test_prefers_playbin3_and_installs_scaletempo_audio_filter(self) -> None:
        self.assertEqual("playbin3", self.player.snapshot.backend_name)
        self.assertTrue(self.player.pitch_preserved)
        self.assertIs(self.player._scaletempo, self.playbin.properties["audio-filter"])
        self.assertEqual(["playbin3", "scaletempo"], self.gst.ElementFactory.calls)

    def test_falls_back_to_playbin_and_rate_change_without_scaletempo(self) -> None:
        playbin = FakePlaybin()
        gst = FakeGst(playbin, playbin3=False, scaletempo=False)
        player = Player(gst=gst)
        self.assertEqual("playbin", player.snapshot.backend_name)
        self.assertFalse(player.pitch_preserved)
        self.assertEqual(["playbin3", "playbin", "scaletempo"], gst.ElementFactory.calls)

    def test_open_prepares_without_autoplay_and_reports_duration_async(self) -> None:
        self.assertTrue(self.player.open("/tmp/My voice.oga"))
        self.assertEqual("null", self.playbin.states[-2])
        self.assertEqual("paused", self.playbin.states[-1])
        self.assertNotIn("playing", self.playbin.states)
        self.assertEqual("file:///tmp/My%20voice.oga", self.playbin.properties["uri"])

        self.playbin.bus.emit(FakeGst.MessageType.ASYNC_DONE)
        self.assertEqual(PlaybackPhase.READY, self.player.phase)
        self.assertEqual(120.0, self.player.snapshot.duration_seconds)

    def test_failed_reopen_never_reports_the_previous_media_snapshot(self) -> None:
        self.make_ready()
        self.playbin.position = 37 * SECOND
        self.assertTrue(self.player.set_speed(1.25))
        self.playbin.uri_error = RuntimeError("URI rejected")

        self.assertFalse(self.player.open("/tmp/new voice.oga"))
        snapshot = self.player.snapshot
        self.assertEqual(PlaybackPhase.ERROR, snapshot.phase)
        self.assertEqual("file:///tmp/new%20voice.oga", snapshot.uri)
        self.assertEqual(0.0, snapshot.position_seconds)
        self.assertEqual(0.0, snapshot.duration_seconds)
        self.assertEqual(1.0, snapshot.speed)

    def test_play_pause_and_toggle_have_explicit_states(self) -> None:
        self.make_ready()
        self.assertTrue(self.player.play())
        self.assertEqual(PlaybackPhase.PLAYING, self.player.phase)
        self.playbin.position = 7 * SECOND
        self.assertTrue(self.player.toggle())
        self.assertEqual(PlaybackPhase.PAUSED, self.player.phase)
        self.assertEqual(7.0, self.player.snapshot.position_seconds)
        self.assertTrue(self.player.toggle())
        self.assertEqual(PlaybackPhase.PLAYING, self.player.phase)

    def test_absolute_and_relative_seek_clamp_to_zero_and_duration(self) -> None:
        self.make_ready()
        self.assertTrue(self.player.seek_to(999))
        self.assertEqual(120 * SECOND, self.playbin.seeks[-1][4])
        self.playbin.position = 5 * SECOND
        self.assertTrue(self.player.seek_by(-10))
        self.assertEqual(0, self.playbin.seeks[-1][4])
        self.playbin.position = 10 * SECOND
        self.assertTrue(self.player.seek_forward())
        self.assertEqual(20 * SECOND, self.playbin.seeks[-1][4])
        self.assertTrue(self.player.seek_back())
        self.assertEqual(10 * SECOND, self.playbin.seeks[-1][4])

    def test_every_fixed_speed_is_applied_as_seek_rate(self) -> None:
        self.make_ready()
        self.playbin.position = 12 * SECOND
        for speed in SUPPORTED_PLAYBACK_SPEEDS:
            self.assertTrue(self.player.set_speed(speed))
            self.assertEqual(speed, self.playbin.seeks[-1][0])
            self.assertEqual(speed, self.player.snapshot.speed)
        speed_events = [
            event for event in self.events if event.type is PlaybackEventType.SPEED_CHANGED
        ]
        self.assertEqual(len(SUPPORTED_PLAYBACK_SPEEDS), len(speed_events))

    def test_invalid_speed_and_nonfinite_seek_are_rejected(self) -> None:
        self.make_ready()
        with self.assertRaises(ValueError):
            self.player.set_speed(1.1)
        with self.assertRaises(ValueError):
            self.player.seek_to(float("nan"))
        with self.assertRaises(ValueError):
            self.player.seek_by(float("inf"))

    def test_rejected_speed_is_nonfatal_and_preserves_phase_and_speed(self) -> None:
        self.make_ready()
        self.player.play()
        self.playbin.seek_result = False
        self.assertFalse(self.player.set_speed(1.5))
        self.assertEqual(PlaybackPhase.PLAYING, self.player.phase)
        self.assertEqual(1.0, self.player.snapshot.speed)
        self.assertTrue(
            any(event.type is PlaybackEventType.SPEED_ERROR for event in self.events)
        )
        self.assertFalse(any(event.type is PlaybackEventType.ERROR for event in self.events))

    def test_end_of_stream_sets_final_position_and_can_restart(self) -> None:
        self.make_ready()
        self.player.play()
        self.playbin.bus.emit(FakeGst.MessageType.EOS)
        self.assertEqual(PlaybackPhase.ENDED, self.player.phase)
        self.assertEqual(120.0, self.player.snapshot.position_seconds)
        self.assertTrue(
            any(event.type is PlaybackEventType.END_OF_STREAM for event in self.events)
        )
        self.assertEqual(FakeGst.State.PAUSED, self.playbin.states[-1])
        self.assertTrue(self.player.play())
        self.assertEqual(0, self.playbin.seeks[-1][4])

    def test_seek_after_end_stays_silent_and_play_continues_from_target(self) -> None:
        self.make_ready()
        self.player.play()
        self.playbin.bus.emit(FakeGst.MessageType.EOS)
        self.assertEqual(PlaybackPhase.ENDED, self.player.phase)

        self.assertTrue(self.player.seek_to(17.0))
        self.assertEqual(PlaybackPhase.PAUSED, self.player.phase)
        self.assertEqual(FakeGst.State.PAUSED, self.playbin.states[-1])
        self.assertEqual(17 * SECOND, self.playbin.seeks[-1][4])
        seek_count = len(self.playbin.seeks)

        self.assertTrue(self.player.play())
        self.assertEqual(PlaybackPhase.PLAYING, self.player.phase)
        self.assertEqual(seek_count, len(self.playbin.seeks))
        self.assertEqual(17 * SECOND, self.playbin.seeks[-1][4])

    def test_bus_error_stops_backend_and_preserves_detail(self) -> None:
        self.make_ready()
        self.playbin.bus.emit(FakeGst.MessageType.ERROR, "bad stream")
        self.assertEqual(PlaybackPhase.ERROR, self.player.phase)
        self.assertIn("bad stream", self.player.snapshot.error or "")
        self.assertEqual(FakeGst.State.NULL, self.playbin.states[-1])

    def test_failed_or_unknown_queries_are_safe_zero(self) -> None:
        self.make_ready()
        self.playbin.query_position_success = False
        self.playbin.query_duration_success = False
        self.assertEqual(0.0, self.player.position_seconds)
        self.assertEqual(0.0, self.player.duration_seconds)

    def test_close_is_idempotent_and_future_open_is_rejected(self) -> None:
        self.player.close()
        self.player.close()
        self.assertEqual(PlaybackPhase.CLOSED, self.player.phase)
        self.assertFalse(self.playbin.bus.watching)
        self.assertEqual([23], self.playbin.bus.disconnected)
        with self.assertRaises(PlaybackError):
            self.player.open("/tmp/voice.oga")

    def test_callback_exception_never_breaks_player(self) -> None:
        player = Player(
            lambda _event: (_ for _ in ()).throw(RuntimeError("UI failed")),
            gst=FakeGst(FakePlaybin()),
        )
        self.assertTrue(player.open("/tmp/voice.oga"))
        self.assertEqual(PlaybackPhase.PREPARING, player.phase)


class GStreamerPlaybackEosTest(unittest.TestCase):
    """Prove rename and post-EOS policy with real playbin, without hardware."""

    def test_ready_file_rename_and_post_eos_seek_preserve_playback(self) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import GLib, Gst
        except (ImportError, ValueError) as error:
            self.skipTest(f"GStreamer introspection is unavailable: {error}")

        Gst.init(None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(48_000)
                output.writeframes(b"\0\0" * 48_000)

            events = []
            loop = GLib.MainLoop()
            timed_out = False
            renamed_path = path.with_name("renamed.wav")
            renamed_while_ready = False
            player = None

            def callback(event) -> None:
                nonlocal renamed_while_ready
                events.append(event)
                if event.type is PlaybackEventType.STATE_CHANGED:
                    if event.snapshot.phase is PlaybackPhase.READY:
                        assert player is not None
                        if not renamed_while_ready:
                            path.rename(renamed_path)
                            renamed_while_ready = True
                        player.play()
                if event.type in {
                    PlaybackEventType.END_OF_STREAM,
                    PlaybackEventType.ERROR,
                }:
                    loop.quit()

            player = Player(callback, gst=Gst)
            sink = Gst.ElementFactory.make("fakesink", "test-audio-output")
            self.assertIsNotNone(sink)
            sink.set_property("sync", False)
            player._playbin.set_property("audio-sink", sink)

            def timeout() -> bool:
                nonlocal timed_out
                timed_out = True
                loop.quit()
                return GLib.SOURCE_REMOVE

            source_id = GLib.timeout_add(3_000, timeout)
            try:
                self.assertTrue(player.open(path))
                loop.run()
                if not timed_out:
                    GLib.Source.remove(source_id)
                self.assertFalse(timed_out, "real playbin did not reach EOS")
                self.assertFalse(
                    any(event.type is PlaybackEventType.ERROR for event in events)
                )
                self.assertTrue(renamed_while_ready)
                self.assertFalse(path.exists())
                self.assertTrue(renamed_path.exists())
                self.assertEqual(PlaybackPhase.ENDED, player.phase)
                _result, state, _pending = player._playbin.get_state(Gst.SECOND)
                self.assertEqual(Gst.State.PAUSED, state)

                self.assertTrue(player.seek_to(0.25))
                self.assertEqual(PlaybackPhase.PAUSED, player.phase)
                _result, state, _pending = player._playbin.get_state(Gst.SECOND)
                self.assertEqual(Gst.State.PAUSED, state)
                self.assertTrue(player.play())
                self.assertEqual(PlaybackPhase.PLAYING, player.phase)
            finally:
                player.close()


class MediaUriTest(unittest.TestCase):
    def test_local_paths_are_absolute_and_percent_encoded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "český hlas.oga"
            uri = media_uri(path)
            self.assertTrue(uri.startswith("file://"))
            self.assertIn("%C4%8Desk%C3%BD%20hlas.oga", uri)

    def test_existing_absolute_uris_are_not_reinterpreted_as_paths(self) -> None:
        uri = "https://example.test/audio/voice.oga?token=one"
        self.assertEqual(uri, media_uri(uri))
        self.assertEqual("file:///tmp/voice.oga", media_uri("file:///tmp/voice.oga"))
        self.assertEqual(
            "data:audio/ogg;base64,T2dnUw==",
            media_uri("data:audio/ogg;base64,T2dnUw=="),
        )

    def test_empty_media_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            media_uri("  ")


if __name__ == "__main__":
    unittest.main()
