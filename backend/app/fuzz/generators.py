"""Fuzz input generators. Every generator is driven by one seeded random generator, so a campaign with the same seed
and corpus produces exactly the same inputs (a finding can always be produced again).

Families
    boundary   numeric edge values for every argument of the known commands (0, -1, INT32 limits, +-1 past them ...)
    overflow   numbers far outside any range, odd number formats (0x10, 1e3, +5, 007)
    mutation   the corpus (real commands) with characters / tokens flipped, dropped, duplicated, swapped
    malformed  broken framing: bad / partial checksums, bad request ids, control characters, stray spaces, non-ASCII
    oversize   lines around and far beyond the 128-character limit
    invalid    unknown commands, wrong case, empty arguments
    state      short sequences that drive the harness state (LED / BLINK / TIMER) and repeat commands
    timeout    a line without its line ending (the firmware must answer ERR 7 TIMEOUT)

The safety filter is applied to EVERY generated line: nothing that could start the bootloader, hang or reboot the
board is ever sent, whatever the mutation produced.
"""
from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Iterator, List, Optional, Sequence

from ..hardware.protocol import crc16

LINE_MAX = 128
FORBIDDEN = ("BOOTSEL", "HANG", "REBOOT")
FAMILIES = ["boundary", "overflow", "mutation", "malformed", "oversize", "invalid", "state", "timeout"]

INT32_MAX, INT32_MIN = 2 ** 31 - 1, -(2 ** 31)
BOUNDARY_NUMBERS = ["0", "1", "-1", "2", str(INT32_MAX), str(INT32_MIN), str(INT32_MAX + 1), str(INT32_MIN - 1),
                    "65535", "65536", "255", "256", "4294967295", "4294967296", "-2147483647"]
ODD_NUMBERS = ["9223372036854775808", "-9223372036854775809", "99999999999999999999999", "0x10", "0b101", "1e3",
               "+5", "-0", "007", "1.5", "-", "+", "", "NaN", "inf", " 5", "5 "]

DEFAULT_CORPUS = ["PING", "HELLO", "ECHO hello", "UPTIME", "VERSION", "INFO", "STATUS", "LED ON", "LED OFF",
                  "LED GET", "LED TOGGLE", "BLINK 200", "BLINK 0", "TIMER 100", "TEMP", "RESETINFO", "APP", "PROF", "PROF CMD PING",
                  "PROF APP 0"]

STATE_SEQUENCES = [
    ["LED ON", "LED GET", "LED OFF", "LED GET"],
    ["BLINK 50", "STATUS", "BLINK 0", "STATUS", "LED GET"],
    ["TIMER 50", "STATUS", "TIMER 0", "STATUS"],
    ["BLINK 10000", "LED ON", "BLINK 0", "LED GET"],
    ["LED TOGGLE"] * 20 + ["LED GET"],
    ["ECHO a"] * 30,
    ["STATUS"] * 20,
]


@dataclass
class FuzzInput:
    text: str
    family: str
    note: str = ""
    partial: bool = False                     # send without a line ending (timeout family)
    sequence: List[str] = field(default_factory=list)   # state family: the whole sequence (text = last line)

    def to_dict(self) -> dict:
        return asdict(self)


def is_safe(line: str) -> bool:
    """False for anything that could reach the bootloader, hang or reboot the board, in any spelling."""
    if "\n" in line or "\r" in line:
        return False
    head = re.sub(r"^\s*#\S*\s*", "", line)          # a request id prefix does not hide the verb
    words = re.split(r"[\s*]+", head.strip())
    first = words[0].upper() if words and words[0] else ""
    if any(first.startswith(f) for f in FORBIDDEN):
        return False
    return not any(f in line.upper().replace(" ", "") for f in FORBIDDEN)


def command_shapes(spec: Optional[dict]) -> List[List[str]]:
    """Command templates with their argument count: from the project's spec, plus the harness commands."""
    shapes = [["BLINK", "<n>"], ["TIMER", "<n>"], ["ECHO", "<s>"], ["LED", "<s>"]]
    for c in (spec or {}).get("commands", []):
        words = c.get("usage", "").split()
        if len(words) >= 2 and words[0].upper() == "APP":
            shapes.append(["APP", words[1]] + ["<n>" if re.search(r"<\w+>", w) else w for w in words[2:]])
    return shapes


def corpus_from(spec: Optional[dict] = None, suites: Sequence[dict] = ()) -> List[str]:
    out = list(DEFAULT_CORPUS)
    for c in (spec or {}).get("commands", []):
        out += [ex["request"] for ex in c.get("examples", [])]
    for s in suites:
        out += [c["command"] for c in s.get("cases", [])]
    seen, uniq = set(), []
    for line in out:
        if line not in seen and is_safe(line):
            seen.add(line)
            uniq.append(line)
    return uniq


class Generator:
    def __init__(self, seed: int, corpus: Sequence[str], shapes: Sequence[Sequence[str]]) -> None:
        self.rng = random.Random(seed)
        self.corpus = list(corpus) or list(DEFAULT_CORPUS)
        self.shapes = [list(s) for s in shapes]

    # ------------------------------------------------------------ families
    def _fill(self, shape: Sequence[str], values: Sequence[str]) -> str:
        parts = []
        for w in shape:
            if w == "<n>":
                parts.append(self.rng.choice(values))
            elif w == "<s>":
                parts.append(self.rng.choice(["", "x", "ON", "OFF", "GET", "TOGGLE", "on", "A" * 40]))
            else:
                parts.append(w)
        return " ".join(parts).rstrip()

    def boundary(self) -> Iterator[FuzzInput]:
        for shape in self.shapes:
            n_args = shape.count("<n>")
            if not n_args:
                continue
            for v in BOUNDARY_NUMBERS:                      # every value in every numeric position once
                for pos in range(n_args):
                    vals, k = [], 0
                    for w in shape:
                        if w == "<n>":
                            vals.append(v if k == pos else "1")
                            k += 1
                        else:
                            vals.append(w)
                    yield FuzzInput(" ".join(vals), "boundary", f"{shape[0]} {shape[1] if len(shape) > 1 else ''} "
                                                               f"arg {pos + 1} = {v}".strip())
            yield FuzzInput(" ".join(w for w in shape if w not in ("<n>", "<s>")), "boundary", "no arguments")
            yield FuzzInput(self._fill(shape, ["1"]) + " 1", "boundary", "one argument too many")

    def overflow(self) -> Iterator[FuzzInput]:
        for shape in self.shapes:
            if "<n>" in shape:
                for v in ODD_NUMBERS:
                    yield FuzzInput(self._fill(shape, [v]), "overflow", f"value {v!r}")

    def mutate(self, line: str) -> str:
        r = self.rng
        op = r.randrange(10)
        chars = list(line)
        words = line.split(" ")
        if op == 0 and chars:                                        # flip a character
            i = r.randrange(len(chars))
            chars[i] = chr(r.randrange(32, 127))
            return "".join(chars)
        if op == 1 and chars:                                        # delete a character
            del chars[r.randrange(len(chars))]
            return "".join(chars)
        if op == 2:                                                  # insert a character
            chars.insert(r.randrange(len(chars) + 1), chr(r.randrange(32, 127)))
            return "".join(chars)
        if op == 3 and words:                                        # duplicate a word
            i = r.randrange(len(words))
            words.insert(i, words[i])
            return " ".join(words)
        if op == 4 and len(words) > 1:                               # drop a word
            del words[r.randrange(1, len(words))]
            return " ".join(words)
        if op == 5 and len(words) > 2:                               # swap two arguments
            i, j = r.sample(range(1, len(words)), 2)
            words[i], words[j] = words[j], words[i]
            return " ".join(words)
        if op == 6:                                                  # change the case
            return line.lower() if r.random() < 0.5 else line.swapcase()
        if op == 7:                                                  # repeat the arguments
            return line + " " + " ".join(words[1:] * r.randint(1, 4))
        if op == 8:                                                  # cut it short
            return line[: r.randrange(max(1, len(line)))]
        return line + r.choice([" ", "  ", "\t", "\x00", " " + "9" * r.randint(5, 30), "*", "#"])

    def mutation(self, count: int) -> Iterator[FuzzInput]:
        for _ in range(count):
            base = self.rng.choice(self.corpus)
            text = base
            for _ in range(self.rng.randint(1, 3)):
                text = self.mutate(text)
            yield FuzzInput(text, "mutation", f"from {base!r}")

    def malformed(self) -> Iterator[FuzzInput]:
        base = self.rng.choice(self.corpus)
        good = f"{base}*{crc16(base):04X}"
        cases = [
            (f"{base}*0000", "wrong checksum"), (good.lower(), "checksum in lower case"),
            (f"{base}*", "checksum marker without digits"), (f"{base}*12", "short checksum"),
            (f"{base}*ZZZZ", "checksum that is not hex"), (f"{good}*{crc16(good):04X}", "two checksums"),
            (f"#1 {base}", "request id"), (f"#99999 {base}", "largest request id"),
            (f"#100000 {base}", "request id too large"), (f"#abc {base}", "request id not a number"),
            (f"# {base}", "empty request id"), (f"#1{base}", "request id without a space"),
            (f"  {base}", "leading spaces"), (f"{base}   ", "trailing spaces"),
            (base.replace(" ", "  "), "double spaces"), (base.replace(" ", "\t"), "tab instead of space"),
            (f"{base}\x01", "control character"), (f"\x7f{base}", "DEL character"), (f"{base}\x00x", "NUL byte"),
            (f"{base} éè", "non-ASCII characters"), ("", "empty line"), ("   ", "only spaces"),
            ("*", "only a checksum marker"), ("#", "only an id marker"),
        ]
        for text, note in cases:
            yield FuzzInput(text, "malformed", note)

    def oversize(self) -> Iterator[FuzzInput]:
        for n in (LINE_MAX - 1, LINE_MAX, LINE_MAX + 1, 200, 512, 1024):
            yield FuzzInput("ECHO " + "A" * (n - 5), "oversize", f"{n} characters")
        for shape in self.shapes:
            if shape[0] == "APP":
                yield FuzzInput(" ".join(shape[:2]) + " " + "7" * 110, "oversize", "huge number argument")
                yield FuzzInput(" ".join(shape[:2]) + " " + " ".join(["1"] * 55), "oversize", "many arguments")

    def invalid(self) -> Iterator[FuzzInput]:
        for text, note in [("NOPE", "unknown command"), ("ping", "lower case"), ("Ping", "mixed case"),
                           ("PING PING", "argument where none is allowed"), ("APP", "APP without a command"),
                           ("APP nope", "unknown APP command"), ("APP " + "Z" * 60, "long unknown APP command"),
                           ("LED", "LED without an argument"), ("LED BLUE", "bad LED argument"),
                           ("BLINK", "BLINK without a value"), ("TIMER x", "TIMER with text")]:
            yield FuzzInput(text, "invalid", note)
        for shape in self.shapes:
            if shape[0] == "APP":
                yield FuzzInput(f"APP {shape[1].lower()}", "invalid", "APP command in lower case")

    def state(self) -> Iterator[FuzzInput]:
        for seq in STATE_SEQUENCES:
            yield FuzzInput(seq[-1], "state", f"sequence of {len(seq)} commands", sequence=list(seq))

    def timeout(self) -> Iterator[FuzzInput]:
        yield FuzzInput("PIN", "timeout", "line without its ending", partial=True)
        yield FuzzInput("ECHO waiting", "timeout", "line without its ending", partial=True)

    # ------------------------------------------------------------ a whole campaign
    def generate(self, families: Iterable[str], limit: int) -> List[FuzzInput]:
        fams = [f for f in FAMILIES if f in set(families)]
        fixed: List[FuzzInput] = []
        for f in fams:
            if f != "mutation":
                fixed += list(getattr(self, f)())
        if "mutation" in fams:
            fixed += list(self.mutation(max(0, limit - len(fixed)) or limit // 3))
        out, seen = [], set()
        for fi in fixed:
            key = (fi.text, fi.partial, tuple(fi.sequence))
            if key in seen or not is_safe(fi.text) or not all(is_safe(s) for s in fi.sequence):
                continue
            seen.add(key)
            out.append(fi)
        if len(out) > limit:                       # keep every family represented when the budget is small
            by: dict = {}
            for fi in out:
                by.setdefault(fi.family, []).append(fi)
            picked, i = [], 0
            while len(picked) < limit:
                for f in fams:
                    if i < len(by.get(f, [])) and len(picked) < limit:
                        picked.append(by[f][i])
                i += 1
            out = picked
        return out
