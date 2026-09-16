"""A small ANSI screen emulator, used to verify terminal rendering in tests.

The terminal view draws with cursor positioning and colour escapes rather than
printing lines in order, so its output cannot be checked by reading the raw byte
stream. Feeding that stream through this emulator reconstructs the grid a user
would actually see, which is what the tests assert against.

Deliberately minimal: it handles exactly the sequences `termlib.py` emits -
absolute cursor positioning, erase-display, erase-line, SGR colour, and the
private modes for the alternate screen and cursor visibility.
"""
from __future__ import annotations

import re


class VirtualScreen:
    """Applies escape sequences to a character grid."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0
        #: Rows that scrolled off the top, so tall frames stay inspectable.
        self.scrollback: list[list[str]] = []

    def feed(self, data: str) -> "VirtualScreen":
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", data[i:])
                if match:
                    self._csi(match.group(1), match.group(2))
                    i += match.end()
                    continue
                match = re.match(r"\x1b[()][A-Za-z0-9]", data[i:])
                if match:
                    i += match.end()
                    continue
                i += 1
                continue
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self._advance_row()
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch >= " ":
                if self.row < self.rows and self.col < self.cols:
                    self.grid[self.row][self.col] = ch
                # No auto-wrap. The application positions every run explicitly
                # with cursor moves and never relies on the terminal wrapping;
                # wrapping here would scroll the header away and make the
                # reconstruction disagree with the real screen.
                self.col = min(self.cols - 1, self.col + 1)
            i += 1
        return self

    def _advance_row(self) -> None:
        self.row += 1
        if self.row >= self.rows:
            self.scrollback.append(self.grid.pop(0))
            self.grid.append([" "] * self.cols)
            self.row = self.rows - 1

    def _csi(self, params: str, final: str) -> None:
        if params.startswith("?"):
            return                     # private modes: cursor visibility, alt screen
        numbers = [int(p) for p in params.split(";") if p.isdigit()]

        def amount(default: int = 1) -> int:
            return max(1, numbers[0] if numbers else default)

        if final == "H":
            self.row = (numbers[0] - 1) if numbers else 0
            self.col = (numbers[1] - 1) if len(numbers) > 1 else 0
            self.row = max(0, min(self.rows - 1, self.row))
            self.col = max(0, min(self.cols - 1, self.col))
        elif final == "J":
            self.grid = [[" "] * self.cols for _ in range(self.rows)]
            self.row = self.col = 0
        elif final == "K":
            for x in range(self.col, self.cols):
                self.grid[self.row][x] = " "
        elif final == "A":
            self.row = max(0, self.row - amount())
        elif final == "B":
            self.row = min(self.rows - 1, self.row + amount())
        elif final == "C":
            self.col = min(self.cols - 1, self.col + amount())
        elif final == "D":
            self.col = max(0, self.col - amount())

    def text(self) -> str:
        """The visible screen, right-trimmed."""
        return "\n".join("".join(r).rstrip() for r in self.grid)

    def full_text(self) -> str:
        """Scrollback plus the visible screen."""
        rows = self.scrollback + self.grid
        return "\n".join("".join(r).rstrip() for r in rows)
