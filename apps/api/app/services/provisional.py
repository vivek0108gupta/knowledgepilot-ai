"""Turning the model's streamed output into text that can be shown before it is checked.

The model writes a JSON document (`{"found_answer": ..., "answer": "...", "citations": [...]}`)
a few characters at a time. `AnswerFieldReader` picks the `answer` string out of it as it
arrives, decoding JSON escapes, without waiting for the document to be complete.

`ProvisionalText` decides what of that text may be shown yet. It is provisional: the answer
that is stored, with its citations, is only produced once the whole output has been checked
(citations.finalize), and it replaces the provisional text. Until then:

- source markers ([S1], [S2, S3]) are hidden, because their numbers are only assigned when
  the citations are checked, and an invalid one must never be shown;
- the most recent characters are held back, so that text repeating the instructions is
  noticed before any of it is sent; once noticed, nothing more is shown.
"""

import re

from app.services.citations import MARKER
from app.services.prompts import leaks_instructions

# Held back from the end of the text: more than the longest phrase the leak check looks for,
# with room for extra whitespace inside it.
HOLD_BACK_CHARACTERS = 96

# The end of the text, if it could still turn out to be a marker once more text arrives.
PARTIAL_MARKER = re.compile(r"\s?\[\s*(?:S\d*(?:\s*[,;]\s*(?:S\d*)?)*)?\s*$")

ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class AnswerFieldReader:
    """Reads the top-level "answer" string of a JSON object fed to it in pieces.

    A small JSON scanner: it tracks nesting and strings, so an "answer" key inside a citation
    quote, or a quote character inside the answer, is handled correctly. It does not validate
    the document; `citations.parse` does that once it is complete.
    """

    def __init__(self) -> None:
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._unicode: str | None = None  # hex digits of a \uXXXX escape being read
        self._high_surrogate: int | None = None
        self._expecting_key = False
        self._reading_key = False
        self._key: list[str] = []
        self._last_key: str | None = None
        self._capturing = False
        self.done = False

    def feed(self, chunk: str) -> str:
        """Returns the answer text contained in `chunk`."""
        out: list[str] = []
        for character in chunk:
            if self._in_string:
                self._string_character(character, out)
            else:
                self._structure_character(character)
        return "".join(out)

    def _emit(self, text: str, out: list[str]) -> None:
        if self._reading_key:
            self._key.append(text)
        elif self._capturing:
            out.append(text)

    def _string_character(self, character: str, out: list[str]) -> None:
        if self._unicode is not None:
            self._unicode += character
            if len(self._unicode) == 4:
                self._code_point(int(self._unicode, 16) if _is_hex(self._unicode) else 0xFFFD, out)
                self._unicode = None
            return
        if self._escape:
            self._escape = False
            if character == "u":
                self._unicode = ""
                return
            self._flush_surrogate(out)
            self._emit(ESCAPES.get(character, character), out)
            return
        if character == "\\":
            self._escape = True
            return
        self._flush_surrogate(out)
        if character == '"':
            self._in_string = False
            if self._reading_key:
                self._reading_key = False
                self._last_key = "".join(self._key)
                self._key = []
            elif self._capturing:
                self._capturing = False
                self.done = True
            return
        self._emit(character, out)

    def _code_point(self, code: int, out: list[str]) -> None:
        if 0xD800 <= code <= 0xDBFF:
            self._flush_surrogate(out)
            self._high_surrogate = code
            return
        if 0xDC00 <= code <= 0xDFFF:
            if self._high_surrogate is None:
                self._emit("�", out)
                return
            combined = 0x10000 + ((self._high_surrogate - 0xD800) << 10) + (code - 0xDC00)
            self._high_surrogate = None
            self._emit(chr(combined), out)
            return
        self._flush_surrogate(out)
        self._emit(chr(code), out)

    def _flush_surrogate(self, out: list[str]) -> None:
        if self._high_surrogate is not None:
            self._high_surrogate = None
            self._emit("�", out)

    def _structure_character(self, character: str) -> None:
        if character == '"':
            self._in_string = True
            if self._depth == 1 and self._expecting_key:
                self._reading_key = True
            elif self._depth == 1 and self._last_key == "answer" and not self.done:
                self._capturing = True
        elif character in "{[":
            self._depth += 1
            self._expecting_key = character == "{" and self._depth == 1
        elif character in "}]":
            self._depth -= 1
        elif character == "," and self._depth == 1:
            self._expecting_key = True
            self._last_key = None
        elif character == ":" and self._depth == 1:
            self._expecting_key = False


def _is_hex(text: str) -> bool:
    return all(character in "0123456789abcdefABCDEF" for character in text)


class ProvisionalText:
    """What of the answer may be shown so far. Feed it the answer text as it arrives; each call
    returns only the new text that is safe to show."""

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary
        self._raw = ""
        self._sent = ""
        # Once the text repeats the instructions, nothing more is shown. The checked answer
        # that replaces it will be the standard "not found" reply.
        self.withheld = False

    @property
    def shown(self) -> str:
        return self._sent

    def feed(self, text: str) -> str:
        if not text or self.withheld:
            return ""
        self._raw += text
        if leaks_instructions(self._raw, self._boundary):
            self.withheld = True
            return ""
        visible = MARKER.sub("", self._raw)
        cut = len(visible) - HOLD_BACK_CHARACTERS
        partial = PARTIAL_MARKER.search(visible)
        if partial is not None:
            cut = min(cut, partial.start())
        ready = visible[: max(cut, 0)]
        # Whitespace at the end may still turn out to precede a marker, which removes it.
        ready = ready[: len(ready.rstrip())] if ready.rstrip() != ready else ready
        if not ready.startswith(self._sent) or len(ready) <= len(self._sent):
            return ""
        new = ready[len(self._sent) :]
        self._sent = ready
        return new
