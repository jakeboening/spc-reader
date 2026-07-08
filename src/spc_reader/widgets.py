"""Matplotlib widget extensions.

``EditableTextBox`` upgrades :class:`matplotlib.widgets.TextBox` from a
bare cursor to a usable text field: mouse-drag selection, select-all,
word-wise movement and deletion, and shift+arrow selection.
"""

from __future__ import annotations

from matplotlib.patches import Rectangle
from matplotlib.transforms import IdentityTransform
from matplotlib.widgets import TextBox

SELECTION_COLOR = "#b4d5fe"


class EditableTextBox(TextBox):
    """TextBox with text selection and standard editing keys.

    Adds over the stock TextBox:

    - mouse-drag selection (click and drag over the text)
    - ctrl/cmd+a — select all
    - shift+left/right/home/end — extend the selection
    - ctrl/alt+left/right — move by word (cmd+left/right = home/end)
    - ctrl/alt+backspace/delete — delete the previous/next word
    - typing/backspace/delete replace or remove the selection
    - escape — drop focus
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sel_anchor: int | None = None   # fixed end of the selection
        self._drag_origin: int | None = None  # char index of the mouse press
        self._sel_rect = Rectangle(
            (0, 0), 0, 0, transform=IdentityTransform(),
            facecolor=SELECTION_COLOR, edgecolor="none",
            zorder=self.text_disp.get_zorder() - 0.5, visible=False,
        )
        self.ax.add_patch(self._sel_rect)

    # --- selection state -------------------------------------------------

    def _selection_span(self) -> tuple[int, int] | None:
        if self._sel_anchor is None:
            return None
        a, b = sorted((self._sel_anchor, self.cursor_index))
        return (a, b) if a != b else None

    def _delete_selection(self, text: str) -> str:
        span = self._selection_span()
        if span:
            a, b = span
            text = text[:a] + text[b:]
            self.cursor_index = a
        self._sel_anchor = None
        return text

    @staticmethod
    def _word_left(text: str, i: int) -> int:
        while i > 0 and not text[i - 1].isalnum():
            i -= 1
        while i > 0 and text[i - 1].isalnum():
            i -= 1
        return i

    @staticmethod
    def _word_right(text: str, i: int) -> int:
        n = len(text)
        while i < n and not text[i].isalnum():
            i += 1
        while i < n and text[i].isalnum():
            i += 1
        return i

    # --- rendering --------------------------------------------------------

    def _x_of(self, index: int) -> float:
        """Pixel x of the given character index (same trick as _rendercursor)."""
        text = self.text_disp.get_text()
        bb = self.text_disp.get_window_extent()
        if index <= 0:
            return bb.x0
        self.text_disp.set_text(text[:index])
        width = self.text_disp.get_window_extent().width
        self.text_disp.set_text(text)
        return bb.x0 + width

    def _rendercursor(self):
        fig = self.ax.get_figure(root=True)
        if fig._get_renderer() is None:
            fig.canvas.draw()
        span = self._selection_span()
        if span:
            x0, x1 = self._x_of(span[0]), self._x_of(span[1])
            bb = self.ax.bbox
            pad = 0.16 * bb.height
            self._sel_rect.set_bounds(x0, bb.y0 + pad, x1 - x0, bb.height - 2 * pad)
            self._sel_rect.set_visible(True)
        else:
            self._sel_rect.set_visible(False)
        super()._rendercursor()

    # --- events -----------------------------------------------------------

    def _resize(self, event):
        # Overrides the stock handler, which (matplotlib 3.11.0) is wrapped by
        # a decorator that assumes event.inaxes exists — ResizeEvent has none.
        self.stop_typing()

    def stop_typing(self):
        self._sel_anchor = None
        self._drag_origin = None
        self._sel_rect.set_visible(False)
        super().stop_typing()

    def _click(self, event):
        if self.ignore(event):
            return
        if not self.ax.contains(event)[0]:
            self.stop_typing()
            return
        if not self.eventson:
            return
        if event.canvas.mouse_grabber != self.ax:
            event.canvas.grab_mouse(self.ax)
        if not self.capturekeystrokes:
            self.begin_typing()
        self.cursor_index = self.text_disp._char_index_at(event.x)
        self._sel_anchor = None
        self._drag_origin = self.cursor_index
        self._rendercursor()

    def _motion(self, event):
        if (self._drag_origin is not None
                and event.canvas.mouse_grabber == self.ax):
            self.cursor_index = self.text_disp._char_index_at(event.x)
            self._sel_anchor = self._drag_origin
            self._rendercursor()
            return
        super()._motion(event)

    def _release(self, event):
        self._drag_origin = None
        super()._release(event)

    def _keypress(self, event):
        if self.ignore(event) or not self.capturekeystrokes:
            return
        key = event.key or ""
        parts = key.split("+")
        base = parts[-1]
        mods = set(parts[:-1])
        word = bool(mods & {"ctrl", "cmd", "super", "alt"})
        shift = "shift" in mods
        text = self.text
        span = self._selection_span()
        changed = False

        if key in ("ctrl+a", "cmd+a", "super+a"):
            self._sel_anchor = 0
            self.cursor_index = len(text)
        elif key == "escape":
            self.stop_typing()
            return
        elif base in ("left", "right", "home", "end"):
            if base == "home" or (base == "left" and mods & {"cmd", "super"}):
                new = 0
            elif base == "end" or (base == "right" and mods & {"cmd", "super"}):
                new = len(text)
            elif word:
                new = (self._word_left(text, self.cursor_index) if base == "left"
                       else self._word_right(text, self.cursor_index))
            elif span and not shift:
                # A plain arrow collapses the selection to its edge.
                new = span[0] if base == "left" else span[1]
            else:
                new = self.cursor_index + (-1 if base == "left" else 1)
                new = max(0, min(len(text), new))
            if shift:
                if self._sel_anchor is None:
                    self._sel_anchor = self.cursor_index
            else:
                self._sel_anchor = None
            self.cursor_index = new
        elif base == "backspace":
            if span:
                text = self._delete_selection(text)
            elif word:
                j = self._word_left(text, self.cursor_index)
                text = text[:j] + text[self.cursor_index:]
                self.cursor_index = j
            elif self.cursor_index > 0:
                text = text[:self.cursor_index - 1] + text[self.cursor_index:]
                self.cursor_index -= 1
            changed = True
        elif base == "delete":
            if span:
                text = self._delete_selection(text)
            elif word:
                j = self._word_right(text, self.cursor_index)
                text = text[:self.cursor_index] + text[j:]
            elif self.cursor_index < len(text):
                text = text[:self.cursor_index] + text[self.cursor_index + 1:]
            changed = True
        elif len(key) == 1:
            if span:
                text = self._delete_selection(text)
            text = text[:self.cursor_index] + key + text[self.cursor_index:]
            self.cursor_index += 1
            changed = True
        elif key in ("enter", "return"):
            pass  # fall through to submit below
        else:
            return  # unhandled chord: leave text and selection untouched

        self.text_disp.set_text(text)
        self._rendercursor()
        if self.eventson:
            if changed:
                self._observers.process("change", self.text)
            if key in ("enter", "return"):
                self._observers.process("submit", self.text)
