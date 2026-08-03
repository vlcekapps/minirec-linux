"""Reusable GTK accessibility helpers and UI-only interaction policies.

The policy functions at the top are intentionally GTK-independent so the
keyboard/focus/selection contract has fast offline tests.  Widget helpers use
only standard GTK 4/libadwaita controls and preserve their native AT-SPI roles.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from enum import Enum
import math
from typing import TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


MIN_CONTROL_HEIGHT = 48
PRIMARY_CONTROL_HEIGHT = 64
COMPACT_CONTENT_WIDTH = 320
CONTENT_WIDTH = 720
MAX_RECORDING_SELECTION = 500

_T = TypeVar("_T", bound=Hashable)


def phase_name(value: object) -> str:
    """Return a stable lower-case phase name for strings and enums."""

    if isinstance(value, Enum):
        value = value.name
    return str(value or "").strip().casefold().rsplit(".", 1)[-1]


def record_action_for_state(state: object) -> str:
    """Return the primary action name for a recorder state.

    Unknown/transitional states deliberately have no primary action.  This
    prevents an accidental second recording while finalization is in progress.
    """

    normalized = phase_name(state)
    return {
        "idle": "record",
        "ready": "record",
        "stopped": "record",
        "error": "record",
        "recording": "pause",
        "paused": "resume",
    }.get(normalized, "none")


def normalize_selection(
    identifiers: Iterable[_T],
    *,
    available: Iterable[_T] | None = None,
    limit: int = MAX_RECORDING_SELECTION,
) -> tuple[_T, ...]:
    """Deduplicate a stable selection, filter stale IDs and enforce its limit."""

    if limit < 0:
        raise ValueError("selection limit cannot be negative")
    if limit == 0:
        return ()
    allowed = set(available) if available is not None else None
    result: list[_T] = []
    seen: set[_T] = set()
    for identifier in identifiers:
        if identifier in seen or (allowed is not None and identifier not in allowed):
            continue
        seen.add(identifier)
        result.append(identifier)
        if len(result) == limit:
            break
    return tuple(result)


def toggle_selection(
    selected: Sequence[_T],
    identifier: _T,
    *,
    limit: int = MAX_RECORDING_SELECTION,
) -> tuple[tuple[_T, ...], bool]:
    """Toggle one ID and report whether a requested addition was accepted."""

    normalized = list(normalize_selection(selected, limit=limit))
    if identifier in normalized:
        normalized.remove(identifier)
        return tuple(normalized), True
    if len(normalized) >= limit:
        return tuple(normalized), False
    normalized.append(identifier)
    return tuple(normalized), True


def focus_index_after_removal(removed_index: int, remaining_count: int) -> int | None:
    """Choose the nearest useful row after an item disappears."""

    if remaining_count <= 0:
        return None
    return min(max(0, removed_index), remaining_count - 1)


def clamp(value: int | float, minimum: int | float, maximum: int | float) -> float:
    """Clamp a finite number to an inclusive range."""

    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("value must be finite")
    if minimum > maximum:
        raise ValueError("minimum cannot exceed maximum")
    return max(float(minimum), min(float(maximum), numeric))


def clamp_seek(position: int | float, duration: int | float | None) -> float:
    """Clamp a seek position, accepting an unknown duration."""

    maximum = max(0.0, float(duration)) if duration is not None else max(0.0, float(position))
    return clamp(position, 0.0, maximum)


def seek_step_target(
    position: int | float,
    duration: int | float | None,
    step: int | float,
) -> float:
    """Return the bounded target of a relative playback seek."""

    return clamp_seek(float(position) + float(step), duration)


def index_for_value(
    choices: Sequence[_T],
    value: _T,
    *,
    default: int = 0,
) -> int:
    """Find a drop-down value without ever returning an invalid index."""

    if not choices:
        raise ValueError("choices cannot be empty")
    if not 0 <= default < len(choices):
        raise ValueError("default index is outside choices")
    try:
        return choices.index(value)
    except ValueError:
        return default


def _enable_label_reflow(label: Gtk.Label) -> None:
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)


def _enable_control_label_reflow(control: Gtk.Widget) -> None:
    def visit(widget: Gtk.Widget) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                _enable_label_reflow(child)
            visit(child)
            child = child.get_next_sibling()

    visit(control)


class HeadingLabel(Gtk.Label):
    """A native label with heading semantics and large-text reflow."""

    def __init__(self, text: str, *, level: int = 1) -> None:
        super().__init__(label=text, xalign=0, wrap=True)
        _enable_label_reflow(self)
        self.set_focusable(False)
        self.set_accessible_role(Gtk.AccessibleRole.HEADING)
        self.update_property([Gtk.AccessibleProperty.LEVEL], [max(1, level)])
        self.add_css_class("title-1" if level == 1 else "title-2")


def heading(text: str, *, level: int = 1) -> HeadingLabel:
    return HeadingLabel(text, level=level)


def description(text: str, *, readable: bool = False) -> Gtk.Label:
    """Create wrapping descriptive text, optionally as one keyboard stop."""

    label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=readable)
    _enable_label_reflow(label)
    label.set_focusable(readable)
    label.add_css_class("dim-label")
    return label


def wrapping_button(label: str, **properties: object) -> Gtk.Button:
    """Create a text button whose visible label survives 200% text."""

    button = Gtk.Button(label=label, **properties)
    _enable_control_label_reflow(button)
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    button.set_size_request(-1, MIN_CONTROL_HEIGHT)
    return button


def wrapping_check_button(label: str, **properties: object) -> Gtk.CheckButton:
    button = Gtk.CheckButton(label=label, **properties)
    _enable_control_label_reflow(button)
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    button.set_size_request(-1, MIN_CONTROL_HEIGHT)
    return button


def labelled(label_text: str, widget: Gtk.Widget) -> Gtk.Box:
    """Place a visible label above a form control and associate the two."""

    label = Gtk.Label(label=label_text, xalign=0, wrap=True)
    _enable_label_reflow(label)
    label.set_focusable(False)
    label.set_mnemonic_widget(widget)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label_text])
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_focusable(False)
    box.append(label)
    box.append(widget)
    return box


def set_accessible_label(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


def set_accessible_description(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text])


def set_value_text(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [text])


def string_dropdown(
    label_text: str,
    choices: Sequence[str],
    *,
    selected: int = 0,
) -> Gtk.DropDown:
    """Create a named native GTK 4.20 drop-down with text choices."""

    if not choices:
        raise ValueError("drop-down choices cannot be empty")
    selected = min(max(0, selected), len(choices) - 1)
    dropdown = Gtk.DropDown(
        model=Gtk.StringList.new(list(choices)),
        selected=selected,
        hexpand=True,
    )
    dropdown.set_size_request(-1, MIN_CONTROL_HEIGHT)
    set_accessible_label(dropdown, label_text)
    return dropdown


class LiveStatus(Gtk.Label):
    """Visible status text announced only when its meaningful content changes."""

    def __init__(self, text: str = "", *, readable: bool = True) -> None:
        super().__init__(label=text, xalign=0, wrap=True, selectable=readable)
        _enable_label_reflow(self)
        self.set_focusable(readable)
        self.set_accessible_role(Gtk.AccessibleRole.STATUS)
        self.set_visible(bool(text))

    def set_status(self, text: str, *, announce: bool = True) -> None:
        changed = self.get_text() != text
        self.set_text(text)
        self.set_visible(bool(text))
        if changed and text and announce:
            self.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)


def clear_container(container: Gtk.Box | Gtk.ListBox) -> None:
    """Remove every direct child without depending on widget internals."""

    child = container.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        container.remove(child)
        child = following


def _list_item_setup(
    _factory: Gtk.SignalListItemFactory,
    item: Gtk.ListItem,
) -> None:
    item.set_focusable(True)
    item.set_selectable(True)


def _list_item_bind(
    _factory: Gtk.SignalListItemFactory,
    item: Gtk.ListItem,
    view: Gtk.ListView,
) -> None:
    child = item.get_item()
    if not isinstance(child, Gtk.Widget):
        return
    label, detail = view._minirec_metadata[child]
    item.set_accessible_label(label)
    item.set_accessible_description(detail)
    item.set_activatable(child in view._minirec_callbacks)
    item.set_child(child)
    view._minirec_bindings[child] = item


def _list_item_unbind(
    _factory: Gtk.SignalListItemFactory,
    item: Gtk.ListItem,
    view: Gtk.ListView,
) -> None:
    child = item.get_child()
    if child is not None:
        view._minirec_bindings.pop(child, None)
    item.set_child(None)


def _list_item_activated(view: Gtk.ListView, position: int) -> None:
    child = view._minirec_store.get_item(position)
    callback = view._minirec_callbacks.get(child)
    if callback is not None:
        callback()


def navigable_list(label: str) -> Gtk.ListView:
    """Create a named native list whose rows activate with Enter or Space."""

    store = Gio.ListStore.new(Gtk.Widget)
    selection = Gtk.SingleSelection(model=store)
    selection.set_autoselect(False)
    factory = Gtk.SignalListItemFactory()
    view = Gtk.ListView(model=selection, factory=factory)
    view._minirec_store = store
    view._minirec_callbacks: dict[Gtk.Widget, Callable[[], None]] = {}
    view._minirec_metadata: dict[Gtk.Widget, tuple[str, str]] = {}
    view._minirec_bindings: dict[Gtk.Widget, Gtk.ListItem] = {}
    factory.connect("setup", _list_item_setup)
    factory.connect("bind", _list_item_bind, view)
    factory.connect("unbind", _list_item_unbind, view)
    view.connect("activate", _list_item_activated)
    view.set_single_click_activate(False)
    view.set_show_separators(True)
    view.set_tab_behavior(Gtk.ListTabBehavior.ALL)
    view.add_css_class("boxed-list")
    set_accessible_label(view, label)
    return view


def append_list_item(
    view: Gtk.ListView,
    child: Gtk.Widget,
    *,
    label: str,
    description_text: str,
    callback: Callable[[], None],
) -> None:
    """Append one activatable, named row to :func:`navigable_list`."""

    view._minirec_metadata[child] = (label, description_text)
    view._minirec_callbacks[child] = callback
    view._minirec_store.append(child)


def clear_list(view: Gtk.ListView) -> None:
    view._minirec_callbacks.clear()
    view._minirec_metadata.clear()
    view._minirec_bindings.clear()
    view._minirec_store.remove_all()


def focus_list_item_later(view: Gtk.ListView, position: int) -> None:
    """Select, scroll to and focus a row after GTK has bound it."""

    def apply() -> bool:
        if not 0 <= position < view._minirec_store.get_n_items():
            return GLib.SOURCE_REMOVE
        model = view.get_model()
        if isinstance(model, Gtk.SingleSelection):
            model.set_selected(position)
        view.scroll_to(
            position,
            Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT,
            None,
        )
        return GLib.SOURCE_REMOVE

    GLib.idle_add(apply)


def scrolled_content(
    child: Gtk.Widget,
    *,
    propagate_natural_height: bool = False,
) -> Gtk.ScrolledWindow:
    scroll = Gtk.ScrolledWindow()
    scroll.set_focusable(False)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_propagate_natural_height(propagate_natural_height)
    scroll.set_child(child)
    return scroll


def content_box(*, spacing: int = 18) -> Gtk.Box:
    """Create the common reflow-safe vertical page container."""

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    box.set_focusable(False)
    box.set_margin_top(18)
    box.set_margin_bottom(18)
    box.set_margin_start(18)
    box.set_margin_end(18)
    return box


def action_group(*widgets: Gtk.Widget) -> Gtk.Box:
    """Stack text actions vertically so 320 px/22 pt never clips them."""

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_focusable(False)
    for widget in widgets:
        widget.set_hexpand(True)
        box.append(widget)
    return box


def focus_later(widget: Gtk.Widget) -> None:
    """Focus an exact control once it has been mapped."""

    def apply() -> bool:
        if widget.get_visible() and widget.get_sensitive():
            widget.grab_focus()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(apply)


def install_escape_handler(
    window: Gtk.Window,
    callback: Callable[[], None] | None = None,
) -> Gtk.EventControllerKey:
    """Make Escape perform the same explicit close action as the text button."""

    controller = Gtk.EventControllerKey()

    def pressed(
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        if callback is None:
            window.close()
        else:
            callback()
        return True

    controller.connect("key-pressed", pressed)
    window.add_controller(controller)
    return controller


def return_focus(widget: Gtk.Widget | None) -> None:
    """Restore focus after a child window/dialog closes when possible."""

    if widget is not None:
        focus_later(widget)


def mark_selected(widget: Gtk.Widget, selected: bool) -> None:
    """Expose an application-managed multi-selection through AT-SPI."""

    widget.update_state([Gtk.AccessibleState.SELECTED], [selected])


def alert(parent: Gtk.Window, title: str, detail: str = "") -> None:
    """Show a native modal alert with standard Escape handling."""

    Gtk.AlertDialog(message=title, detail=detail, modal=True).show(parent)


def confirm(
    parent: Gtk.Window,
    title: str,
    detail: str,
    cancel_label: str,
    confirm_label: str,
    callback: Callable[[], None],
    *,
    return_to: Gtk.Widget | None = None,
    destructive: bool = False,
) -> Gtk.AlertDialog:
    """Ask a native two-button question and restore the initiating focus."""

    dialog = Gtk.AlertDialog(message=title, detail=detail, modal=True)
    dialog.set_buttons([cancel_label, confirm_label])
    dialog.set_cancel_button(0)
    dialog.set_default_button(0)
    # ``Gtk.AlertDialog`` intentionally owns its platform presentation.  The
    # semantic risk is therefore carried by the explicit button text; GTK 4.20
    # does not expose per-button style classes here.
    _ = destructive

    def finished(source: Gtk.AlertDialog, result: Gio.AsyncResult) -> None:
        try:
            response = source.choose_finish(result)
        except GLib.Error:
            return_focus(return_to)
            return
        if response == 1:
            callback()
        return_focus(return_to)

    dialog.choose(parent, None, finished)
    return dialog


def set_invalid(widget: Gtk.Widget, invalid: bool) -> None:
    """Synchronize a visible validation style and native invalid state."""

    if invalid:
        widget.add_css_class("error")
    else:
        widget.remove_css_class("error")
    widget.update_state(
        [Gtk.AccessibleState.INVALID],
        [
            int(Gtk.AccessibleInvalidState.TRUE)
            if invalid
            else int(Gtk.AccessibleInvalidState.FALSE)
        ],
    )


class ChildWindow(Adw.ApplicationWindow):
    """Adaptive application child with an explicit text Close button."""

    def __init__(
        self,
        parent: Adw.ApplicationWindow,
        title: str,
        close_label: str,
        *,
        width: int = 640,
        height: int = 680,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            application=parent.get_application(),
            transient_for=parent,
            modal=False,
        )
        self.set_title(title)
        self.set_default_size(width, height)
        self._on_explicit_close = on_close
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title_widget = Gtk.Label(label=title, ellipsize=Pango.EllipsizeMode.END)
        title_widget.set_tooltip_text(title)
        header.set_title_widget(title_widget)
        close_button = wrapping_button(close_label)
        close_button.connect("clicked", lambda _button: self.explicit_close())
        header.pack_start(close_button)
        toolbar.add_top_bar(header)
        self.page = content_box()
        scroll = scrolled_content(self.page)
        scroll.set_vexpand(True)
        toolbar.set_content(scroll)
        self.set_content(toolbar)
        install_escape_handler(self, self.explicit_close)
        self.title_widget = title_widget
        self.close_button = close_button

    def explicit_close(self) -> None:
        if self._on_explicit_close is not None:
            self._on_explicit_close()
        self.close()
