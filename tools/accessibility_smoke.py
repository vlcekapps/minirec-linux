#!/usr/bin/env python3
"""Assert MiniRec's essential keyboard and AT-SPI contract.

Run this gate inside the user's graphical session.  It launches the inert
``gui_smoke.py --atspi-harness`` process with private XDG directories, inspects
real GTK accessibility objects in English and Czech, and then terminates only
that child process.  No microphone, recording file or network service is used.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from minirec.i18n import (  # noqa: E402
    Translator,
    format_duration,
    format_file_size,
)


RECORDING_MODIFIED_NS = 1_785_751_872_000_000_000


def require_accessibility_bus() -> None:
    """Start/check the session AT-SPI bus before libatspi can abort on access."""

    command = (
        "gdbus",
        "call",
        "--session",
        "--dest",
        "org.a11y.Bus",
        "--object-path",
        "/org/a11y/bus",
        "--method",
        "org.a11y.Bus.GetAddress",
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SystemExit(
            "AT-SPI accessibility bus is unavailable; run this gate inside "
            f"the graphical user session. {detail}"
        )


require_accessibility_bus()

import gi  # noqa: E402

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402


def children(node: Atspi.Accessible) -> list[Atspi.Accessible]:
    result: list[Atspi.Accessible] = []
    try:
        count = node.get_child_count()
    except GLib.Error:
        return result
    for index in range(count):
        try:
            child = node.get_child_at_index(index)
        except GLib.Error:
            continue
        if child is not None:
            result.append(child)
    return result


def walk(root: Atspi.Accessible) -> list[Atspi.Accessible]:
    found: list[Atspi.Accessible] = []
    seen: set[Atspi.Accessible] = set()
    pending = [root]
    while pending and len(found) < 2_000:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        pending.extend(reversed(children(current)))
    return found


def safe_name(node: Atspi.Accessible) -> str:
    try:
        return node.get_name() or ""
    except GLib.Error:
        return ""


def safe_role(node: Atspi.Accessible) -> Atspi.Role:
    try:
        return node.get_role()
    except GLib.Error:
        return Atspi.Role.INVALID


def safe_description(node: Atspi.Accessible) -> str:
    try:
        return node.get_description() or ""
    except GLib.Error:
        return ""


def safe_action_count(node: Atspi.Accessible) -> int:
    try:
        return node.get_n_actions()
    except GLib.Error:
        return 0


def has_state(node: Atspi.Accessible, state: Atspi.StateType) -> bool:
    try:
        return node.get_state_set().contains(state)
    except GLib.Error:
        return False


def find_application(
    token: str,
    expected_frames: frozenset[str],
) -> Atspi.Accessible | None:
    desktop = Atspi.get_desktop(0)
    fallback: list[Atspi.Accessible] = []
    for application in children(desktop):
        if safe_name(application) == token:
            return application
        frame_names = {
            safe_name(node)
            for node in walk(application)
            if safe_role(node) is Atspi.Role.FRAME
        }
        if expected_frames.issubset(frame_names):
            fallback.append(application)
    return fallback[0] if len(fallback) == 1 else None


def wait_for_application(
    process: subprocess.Popen[str],
    token: str,
    expected_frames: frozenset[str],
    *,
    timeout: float = 12.0,
) -> Atspi.Accessible:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                "MiniRec accessibility harness exited early\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        application = find_application(token, expected_frames)
        if application is not None:
            frames = {
                safe_name(node)
                for node in walk(application)
                if safe_role(node) is Atspi.Role.FRAME
            }
            if expected_frames.issubset(frames):
                return application
        time.sleep(0.1)
    raise AssertionError("MiniRec did not publish its complete AT-SPI tree")


def matching(
    nodes: list[Atspi.Accessible],
    name: str,
    roles: frozenset[Atspi.Role],
) -> list[Atspi.Accessible]:
    return [
        node
        for node in nodes
        if safe_name(node).strip() == name and safe_role(node) in roles
    ]


def require_named(
    nodes: list[Atspi.Accessible],
    name: str,
    roles: frozenset[Atspi.Role],
) -> Atspi.Accessible:
    found = matching(nodes, name, roles)
    if not found:
        available = sorted(
            {
                (safe_name(node), str(safe_role(node)))
                for node in nodes
                if safe_name(node).strip()
            }
        )
        raise AssertionError(
            f"Missing accessible object {name!r} with role in {roles}; "
            f"available={available}"
        )
    return found[0]


def require_action(node: Atspi.Accessible) -> None:
    """Require a native action, accepting GTK's MenuButton wrapper only."""

    if safe_action_count(node) > 0:
        return
    name = safe_name(node)
    if any(
        safe_name(child) == name and safe_action_count(child) > 0
        for child in walk(node)[1:]
    ):
        return
    raise AssertionError(
        f"{safe_name(node)!r} ({safe_role(node)}) has no AT-SPI action"
    )


def keyboard_target(
    node: Atspi.Accessible,
    *,
    actionable: bool = False,
) -> Atspi.Accessible:
    """Resolve a named GTK composite wrapper to its keyboard target."""

    name = safe_name(node)
    candidates = [
        candidate
        for candidate in (node, *walk(node)[1:])
        if safe_name(candidate) == name
        and has_state(candidate, Atspi.StateType.FOCUSABLE)
    ]
    if actionable:
        candidates = [
            candidate for candidate in candidates if safe_action_count(candidate) > 0
        ]
    if candidates:
        return candidates[0]
    return node


def require_focusable(node: Atspi.Accessible) -> None:
    if not has_state(node, Atspi.StateType.FOCUSABLE):
        raise AssertionError(
            f"{safe_name(node)!r} ({safe_role(node)}) is not keyboard focusable"
        )


def require_value(node: Atspi.Accessible) -> None:
    try:
        available = node.is_value()
    except GLib.Error:
        available = False
    if not available:
        raise AssertionError(f"{safe_name(node)!r} has no AT-SPI value interface")


def require_text(node: Atspi.Accessible) -> None:
    try:
        available = node.is_text()
    except GLib.Error:
        available = False
    if not available:
        raise AssertionError(f"{safe_name(node)!r} has no AT-SPI text interface")


def focus(node: Atspi.Accessible) -> None:
    ancestor: Atspi.Accessible | None = node
    while ancestor is not None and safe_role(ancestor) is not Atspi.Role.FRAME:
        try:
            ancestor = ancestor.get_parent()
        except GLib.Error:
            ancestor = None
    if ancestor is not None:
        try:
            ancestor.grab_focus()
            time.sleep(0.1)
        except GLib.Error:
            pass
    try:
        node.grab_focus()
    except GLib.Error as error:
        raise AssertionError(f"Cannot focus {safe_name(node)!r}") from error
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if has_state(node, Atspi.StateType.FOCUSED):
            return
        time.sleep(0.05)
    raise AssertionError(f"AT-SPI focus did not reach {safe_name(node)!r}")


def inspect(application: Atspi.Accessible, language: str) -> None:
    nodes = walk(application)
    text = {
        "en": {
            "frames": frozenset(
                {
                    "MiniRec",
                    "MiniRec settings",
                    "MiniRec recordings",
                    "MiniRec player",
                    "Rename recording",
                }
            ),
            "headings": {
                "Audio recorder",
                "Settings",
                "Recordings",
                "Recording player",
                "Rename recording",
            },
            "record": "Record",
            "menu": "Main menu",
            "language": "Language",
            "format": "Audio format",
            "gain": "Microphone gain",
            "mono": "Mono",
            "sleep": "Prevent automatic sleep while recording",
            "refresh": "Refresh",
            "list": "Recordings; activate a row to open its player",
            "row": "Open player: Morning notes.oga",
            "select": "Select",
            "play": "Play",
            "back": "Back 10 seconds",
            "seek": "Playback position",
            "speed": "Playback speed",
            "entry": "New name",
            "playing": "Playing",
        },
        "cs": {
            "frames": frozenset(
                {
                    "MiniRec",
                    "Nastavení MiniRec",
                    "Nahrávky MiniRec",
                    "Přehrávač MiniRec",
                    "Přejmenovat nahrávku",
                }
            ),
            "headings": {
                "Záznam zvuku",
                "Nastavení",
                "Nahrávky",
                "Přehrávač nahrávky",
                "Přejmenovat nahrávku",
            },
            "record": "Nahrát",
            "menu": "Hlavní nabídka",
            "language": "Jazyk",
            "format": "Formát zvuku",
            "gain": "Zesílení mikrofonu",
            "mono": "Mono",
            "sleep": "Během nahrávání zabránit automatickému uspání",
            "refresh": "Obnovit",
            "list": "Nahrávky; aktivací řádku otevřete přehrávač",
            "row": "Otevřít přehrávač: Morning notes.oga",
            "select": "Vybrat",
            "play": "Přehrát",
            "back": "Zpět o 10 sekund",
            "seek": "Pozice přehrávání",
            "speed": "Rychlost přehrávání",
            "entry": "Nový název",
            "playing": "Přehrávání probíhá",
        },
    }[language]

    frame_names = {
        safe_name(node)
        for node in nodes
        if safe_role(node) is Atspi.Role.FRAME
    }
    assert text["frames"].issubset(frame_names), frame_names

    heading_names = {
        safe_name(node)
        for node in nodes
        if safe_role(node) is Atspi.Role.HEADING
    }
    assert text["headings"].issubset(heading_names), heading_names

    button_roles = frozenset(
        {
            Atspi.Role.PUSH_BUTTON,
            Atspi.Role.PUSH_BUTTON_MENU,
            Atspi.Role.TOGGLE_BUTTON,
        }
    )
    record = keyboard_target(
        require_named(nodes, text["record"], button_roles), actionable=True
    )
    menu = keyboard_target(
        require_named(nodes, text["menu"], button_roles), actionable=True
    )
    refresh = keyboard_target(
        require_named(nodes, text["refresh"], button_roles), actionable=True
    )
    play = keyboard_target(
        require_named(nodes, text["play"], button_roles), actionable=True
    )
    back = keyboard_target(
        require_named(nodes, text["back"], button_roles), actionable=True
    )
    select = keyboard_target(
        require_named(
            nodes,
            text["select"],
            frozenset({Atspi.Role.CHECK_BOX, Atspi.Role.TOGGLE_BUTTON}),
        ),
        actionable=True,
    )
    mono = keyboard_target(
        require_named(
            nodes,
            text["mono"],
            frozenset({Atspi.Role.RADIO_BUTTON}),
        ),
        actionable=True,
    )
    sleep = keyboard_target(
        require_named(
            nodes,
            text["sleep"],
            frozenset({Atspi.Role.CHECK_BOX}),
        ),
        actionable=True,
    )
    row = keyboard_target(
        require_named(
            nodes,
            text["row"],
            frozenset({Atspi.Role.LIST_ITEM}),
        ),
        actionable=True,
    )
    for action in (record, menu, refresh, play, back, select, mono, sleep, row):
        require_focusable(action)
        require_action(action)

    recordings_list = require_named(
        nodes,
        text["list"],
        frozenset({Atspi.Role.LIST}),
    )
    translator = Translator(
        language,
        system_locale="cs_CZ" if language == "cs" else "en_US",
    )
    expected_details = translator(
        "recording_details",
        date=translator.format_recording_date(RECORDING_MODIFIED_NS),
        duration=format_duration(65.0),
        size=format_file_size(123_456),
        format=translator("format_ogg_opus"),
    )
    expected_description = translator(
        "recording_row",
        name="Morning notes.oga",
        details=expected_details,
    )
    if safe_description(row) != expected_description:
        raise AssertionError(
            "The recording row did not expose its complete date, duration, "
            "size and format through AT-SPI"
        )

    language_combo = keyboard_target(
        require_named(
            nodes,
            text["language"],
            frozenset({Atspi.Role.COMBO_BOX}),
        )
    )
    format_combo = keyboard_target(
        require_named(
            nodes,
            text["format"],
            frozenset({Atspi.Role.COMBO_BOX}),
        )
    )
    speed_combo = keyboard_target(
        require_named(
            nodes,
            text["speed"],
            frozenset({Atspi.Role.COMBO_BOX}),
        )
    )
    for combo in (language_combo, format_combo, speed_combo):
        require_focusable(combo)

    gain = require_named(
        nodes,
        text["gain"],
        frozenset({Atspi.Role.SLIDER}),
    )
    seek = require_named(
        nodes,
        text["seek"],
        frozenset({Atspi.Role.SLIDER}),
    )
    for slider in (gain, seek):
        require_focusable(slider)
        require_value(slider)
    if not safe_description(gain).strip():
        raise AssertionError("The microphone gain has no accessible range description")

    entry = require_named(
        nodes,
        text["entry"],
        frozenset({Atspi.Role.ENTRY, Atspi.Role.TEXT}),
    )
    require_focusable(entry)
    require_text(entry)

    # GTK publishes every keyboard destination as FOCUSABLE above.  The modal
    # form's documented map-time focus move must additionally be visible as
    # the real AT-SPI FOCUSED state.  Wayland intentionally rejects arbitrary
    # external Component.grabFocus requests, so this checks application-driven
    # focus without weakening the compositor's security model.
    focus_deadline = time.monotonic() + 2.0
    while not has_state(entry, Atspi.StateType.FOCUSED) and time.monotonic() < focus_deadline:
        time.sleep(0.05)
        nodes = walk(application)
        current_entries = matching(
            nodes,
            text["entry"],
            frozenset({Atspi.Role.ENTRY, Atspi.Role.TEXT}),
        )
        if current_entries:
            entry = current_entries[0]
    if not has_state(entry, Atspi.StateType.FOCUSED):
        focused = [
            (safe_name(node), str(safe_role(node)))
            for node in nodes
            if has_state(node, Atspi.StateType.FOCUSED)
        ]
        raise AssertionError(
            f"The rename entry did not receive its documented initial focus: {focused}"
        )

    # Invoke one harmless media action through AT-SPI and verify that its live
    # status changes.  The harness callback is in-memory and opens no audio.
    if not play.do_action(0):
        raise AssertionError("The Play AT-SPI action failed")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        nodes = walk(application)
        if any(safe_name(node) == text["playing"] for node in nodes):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("The Play action did not expose its updated status")

    # No silent keyboard stops: every focusable object involved in the tested
    # contract has a non-empty name.
    tested = (
        record,
        menu,
        refresh,
        play,
        back,
        select,
        mono,
        sleep,
        row,
        recordings_list,
        language_combo,
        format_combo,
        speed_combo,
        gain,
        seek,
        entry,
    )
    unnamed = [str(safe_role(node)) for node in tested if not safe_name(node).strip()]
    if unnamed:
        raise AssertionError(f"Unnamed essential keyboard stops: {unnamed}")


def run_language(language: str) -> None:
    labels = {
        "en": frozenset(
            {
                "MiniRec",
                "MiniRec settings",
                "MiniRec recordings",
                "MiniRec player",
                "Rename recording",
            }
        ),
        "cs": frozenset(
            {
                "MiniRec",
                "Nastavení MiniRec",
                "Nahrávky MiniRec",
                "Přehrávač MiniRec",
                "Přejmenovat nahrávku",
            }
        ),
    }[language]
    token = f"MiniRec AT-SPI smoke {language} {uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="minirec-atspi-") as temporary:
        root = Path(temporary)
        environment = os.environ.copy()
        environment.update(
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "GTK_A11Y": "atspi",
            }
        )
        environment.pop("NO_AT_BRIDGE", None)
        process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT / "tools" / "gui_smoke.py"),
                "--atspi-harness",
                "--language",
                language,
                "--token",
                token,
            ],
            cwd=PROJECT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            application = wait_for_application(process, token, labels)
            inspect(application, language)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            stdout, stderr = process.communicate(timeout=1)
            if process.returncode not in {0, -15}:
                raise AssertionError(
                    f"Harness cleanup failed ({process.returncode})\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )


if __name__ == "__main__":
    for selected_language in ("en", "cs"):
        run_language(selected_language)
    print("AT-SPI accessibility smoke test passed for English and Czech")
