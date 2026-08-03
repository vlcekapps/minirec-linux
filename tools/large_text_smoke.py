#!/usr/bin/env python3
"""Verify MiniRec reflow at 22 pt and about 320 CSS pixels.

The gate runs English and Czech in light, dark and high-contrast modes.  A
private in-memory GSettings backend toggles high contrast without changing the
user's desktop preferences.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


# This must be selected before Gio/libadwaita reads desktop preferences.  It is
# process-local and makes the contrast part of this test deterministic.
os.environ.setdefault("GSETTINGS_BACKEND", "memory")

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from minirec.i18n import Translator  # noqa: E402
from minirec.ui import MainWindow, PlayerView, RenameWindow  # noqa: E402
from tools.gui_smoke import (  # noqa: E402
    FakeCallbacks,
    RECORDINGS,
    widget_descendants,
)


MAX_REFLOW_WIDTH = 320
PLATFORM_WINDOW_MINIMUM = 360


def minimum_width(widget: Gtk.Widget) -> int:
    return widget.measure(Gtk.Orientation.HORIZONTAL, -1).minimum


def content_scroll(window: Gtk.Window) -> Gtk.ScrolledWindow:
    scrolls = [
        widget
        for widget in widget_descendants(window)
        if isinstance(widget, Gtk.ScrolledWindow)
    ]
    assert scrolls, type(window).__name__
    return scrolls[0]


def content_body(window: Gtk.Window) -> Gtk.Widget:
    body = content_scroll(window).get_child()
    assert body is not None
    return body


def matching_labels(widget: Gtk.Widget, text: str) -> list[Gtk.Label]:
    return [
        child
        for child in widget_descendants(widget)
        if isinstance(child, Gtk.Label) and child.get_text() == text
    ]


class LargeTextApplication(Adw.Application):
    """One language/theme combination of the reflow gate."""

    def __init__(self, language: str, *, dark: bool, high_contrast: bool) -> None:
        suffix = "Cs" if language == "cs" else "En"
        mode = ("Dark" if dark else "Light") + ("Hc" if high_contrast else "")
        super().__init__(
            application_id=f"cz.pvlcek.minirec.LargeText{suffix}{mode}",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.language = language
        self.dark = dark
        self.high_contrast = high_contrast
        self.callbacks = FakeCallbacks(language)
        self.main_window: MainWindow | None = None
        self.windows: list[Gtk.Window] = []
        self.provider: Gtk.CssProvider | None = None
        self.passed = False
        self.action_signature: frozenset[str] = frozenset()

    def do_activate(self) -> None:
        try:
            display = Gdk.Display.get_default()
            assert display is not None
            provider = Gtk.CssProvider()
            provider.load_from_string("* { font-size: 22pt; }")
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self.provider = provider

            gtk_settings = Gtk.Settings.get_for_display(display)
            assert gtk_settings is not None
            gtk_settings.set_property(
                "gtk-interface-contrast",
                Gtk.InterfaceContrast.MORE
                if self.high_contrast
                else Gtk.InterfaceContrast.NO_PREFERENCE,
            )
            manager = Adw.StyleManager.get_default()
            manager.set_color_scheme(
                Adw.ColorScheme.FORCE_DARK
                if self.dark
                else Adw.ColorScheme.FORCE_LIGHT
            )

            translator = Translator(
                self.language,
                system_locale="cs_CZ" if self.language == "cs" else "en_US",
            )
            main = MainWindow(
                self,
                self.callbacks,
                translator=translator,
                settings=self.callbacks.settings,
                version="large-text-smoke",
                recordings_path="/tmp/minirec-large-text/Recordings/MiniRec",
            )
            self.main_window = main
            self.callbacks.bind(main)
            main.set_recordings(RECORDINGS)
            main.set_default_size(MAX_REFLOW_WIDTH, 520)
            main.present()

            main.show_settings()
            main.show_recordings()
            main.show_player(RECORDINGS[0])
            main.set_player_view(
                PlayerView("ready", 15.0, 125.0, 1.0)
            )
            assert main._settings_window is not None
            assert main._recordings_window is not None
            assert main._player_window is not None
            rename = RenameWindow(
                main._recordings_window,
                translator,
                RECORDINGS[0],
                main._recordings_window.refresh_button,
                lambda _name: None,
            )
            self.windows = [
                main._settings_window,
                main._recordings_window,
                main._player_window,
                rename,
            ]
            for window in self.windows:
                window.set_default_size(MAX_REFLOW_WIDTH, 520)
                window.present()
            GLib.timeout_add(650, self.exercise)
        except BaseException:
            traceback.print_exc()
            self._close_and_quit()

    def exercise(self) -> bool:
        try:
            assert self.main_window is not None
            main = self.main_window
            manager = Adw.StyleManager.get_default()
            gtk_settings = Gtk.Settings.get_for_display(Gdk.Display.get_default())
            assert gtk_settings is not None
            expected_contrast = (
                Gtk.InterfaceContrast.MORE
                if self.high_contrast
                else Gtk.InterfaceContrast.NO_PREFERENCE
            )
            assert gtk_settings.get_property("gtk-interface-contrast") == expected_contrast, (
                self.language,
                self.dark,
                self.high_contrast,
                gtk_settings.get_property("gtk-interface-contrast"),
            )
            expected_scheme = (
                Adw.ColorScheme.FORCE_DARK
                if self.dark
                else Adw.ColorScheme.FORCE_LIGHT
            )
            assert manager.get_color_scheme() == expected_scheme

            roots = [main, *self.windows]
            assert main.get_width() <= PLATFORM_WINDOW_MINIMUM, (
                self.language,
                self.dark,
                self.high_contrast,
                main.get_width(),
            )
            for window in self.windows:
                assert window.get_width() <= PLATFORM_WINDOW_MINIMUM, (
                    self.language,
                    self.dark,
                    self.high_contrast,
                    type(window).__name__,
                    window.get_width(),
                )

            for window in roots:
                body = content_body(window)
                assert minimum_width(body) <= MAX_REFLOW_WIDTH, (
                    self.language,
                    self.dark,
                    self.high_contrast,
                    type(window).__name__,
                    minimum_width(body),
                )
                horizontal, _vertical = content_scroll(window).get_policy()
                assert horizontal == Gtk.PolicyType.NEVER

            browser = main._recordings_window
            assert browser is not None
            for position in range(browser.list._minirec_store.get_n_items()):
                row = browser.list._minirec_store.get_item(position)
                assert isinstance(row, Gtk.Widget)
                assert minimum_width(row) <= MAX_REFLOW_WIDTH

            actions: set[str] = set()
            for root in roots:
                for widget in (root, *widget_descendants(root)):
                    if isinstance(widget, Gtk.CheckButton):
                        text = widget.get_label()
                    elif isinstance(widget, Gtk.Button):
                        text = widget.get_label()
                    else:
                        continue
                    if not text:
                        continue
                    actions.add(text)
                    labels = matching_labels(widget, text)
                    assert labels, (
                        self.language,
                        type(widget).__name__,
                        text,
                    )
                    assert all(
                        label.get_wrap()
                        and label.get_wrap_mode().value_nick == "word-char"
                        and label.get_natural_wrap_mode().value_nick == "word"
                        for label in labels
                    ), (self.language, type(widget).__name__, text)

            # The same controls must remain exposed in every visual theme;
            # neither colour nor contrast may be an action's only affordance.
            assert main.primary_button.get_mapped()
            assert main.menu_button.get_mapped()
            assert main._settings_window.format.get_mapped()
            assert browser.refresh_button.get_mapped()
            assert main._player_window.play_button.get_mapped()
            self.action_signature = frozenset(actions)
            self.passed = True
        except BaseException:
            traceback.print_exc()
        finally:
            self._close_and_quit()
        return GLib.SOURCE_REMOVE

    def _close_and_quit(self) -> None:
        for window in reversed(self.windows):
            window.close()
        if self.main_window is not None:
            self.main_window.close()
        if self.provider is not None:
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.remove_provider_for_display(display, self.provider)
            self.provider = None
        self.quit()


def select_high_contrast(enabled: bool) -> None:
    settings = Gio.Settings.new("org.gnome.desktop.a11y.interface")
    if not settings.set_boolean("high-contrast", enabled):
        raise RuntimeError("The private high-contrast setting could not be changed")
    Gio.Settings.sync()


def run_variant(
    language: str,
    *,
    dark: bool,
    high_contrast: bool,
) -> frozenset[str] | None:
    select_high_contrast(high_contrast)
    app = LargeTextApplication(
        language,
        dark=dark,
        high_contrast=high_contrast,
    )
    status = app.run([sys.argv[0]])
    if status != 0 or not app.passed:
        return None
    return app.action_signature


if __name__ == "__main__":
    variants = (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    )
    for language in ("en", "cs"):
        signatures = [
            run_variant(language, dark=dark, high_contrast=contrast)
            for dark, contrast in variants
        ]
        if any(signature is None for signature in signatures):
            raise SystemExit(1)
        assert signatures and all(
            signature == signatures[0] for signature in signatures[1:]
        ), (language, signatures)
    print(
        "Large-text smoke test passed at 22 pt/320 px for English and Czech "
        "in light, dark and high-contrast modes"
    )
