"""A simulated AEISP board: the harness protocol in Python (line limit, request ids, checksums, harness commands,
watchdog resets) plus a small calculator app with bugs that can be planted on purpose.

    fw = SimFirmware(bugs={"div_zero_hang"})
    DeviceManager(link_factory=lambda: SimLink(fw))

Planted bugs
    div_zero_hang    APP DIV a 0 loops forever -> no reply, watchdog reset 3 s later
    long_arg_crash   an APP argument longer than 24 characters overwrites the stack -> the board restarts
    mul_wrong        APP MUL answers a + b instead of a * b
    slow_add         APP ADD takes 700 ms
    div_zero_dead    APP DIV a 0 kills the board for good (it never comes back on USB)
    no_line_timeout  an unfinished line is never answered with ERR 7
    mul_slow         APP MUL takes 400 us on the board (instead of 40)
    mul_fast         APP MUL takes 12 us on the board
    mul_noisy        APP MUL takes 40..100 us, different in every profiling window (not repeatable)
Harness 0.4.0 answers PROF with simulated, repeatable timings (COST below); 0.3.0 does not know PROF.
`builds` maps a build id to the bugs that build has, so deploying a fixed build changes the behaviour.
"""
import re
import time

COST = {"APP ADD": 35, "APP MUL": 40, "APP DIV": 60, "APP": 30}   # us per command, default 20

from app.hardware.link import LinkTimeout, NotConnected
from app.hardware.protocol import crc16

ERR = {1: "UNKNOWN_CMD", 2: "BAD_ARGS", 3: "BAD_RANGE", 4: "BAD_CRC", 5: "TOO_LONG", 6: "BAD_FORMAT", 7: "TIMEOUT",
       8: "BAD_STATE"}
INT32 = (-(2 ** 31), 2 ** 31 - 1)


class Hang(Exception):
    pass


class Crash(Exception):
    pass


class SimFirmware:
    def __init__(self, bugs=(), build_id=1, src="aaaa1111", builds=None, clock=time.monotonic, fw="0.4.0"):
        self.builds = builds or {}
        self.clock = clock
        self.build_id, self.src = build_id, src
        self.bugs = set(self.builds.get(build_id, bugs))
        self.boot = clock() - 5.0
        self.reset_reason = "POWER_ON_OR_RESET"
        self.led, self.blink, self.cmds, self.errors = "OFF", 0, 0, 0
        self.hung = False
        self.dead = False
        self.fw = fw
        self.prof_reset()
        self.log = []

    # ------------------------------------------------------------ lifecycle
    def reset(self, reason):
        self.boot = self.clock()
        self.reset_reason = reason
        self.led, self.blink, self.hung = "OFF", 0, False

    def flash(self, build_id, src="bbbb2222"):
        self.build_id, self.src = build_id, src
        self.bugs = set(self.builds.get(build_id, ()))
        self.reset("SOFTWARE_REBOOT")

    def uptime(self):
        return int((self.clock() - self.boot) * 1000)

    # ------------------------------------------------------------ the on-device profiler (harness 0.4.0)
    def prof_reset(self):
        self.windows = getattr(self, "windows", -1) + 1
        self.prof = {}
        self.prof_start = self.clock()
        self.rx_hwm = self.reply_hwm = 0

    def cost_us(self, line):
        words = line.split(" ")
        key = " ".join(words[:2]) if words[0] == "APP" else words[0]
        us = COST.get(key, COST.get(words[0], 20))
        if key == "APP MUL" and "mul_slow" in self.bugs:
            us = 400
        if key == "APP MUL" and "mul_fast" in self.bugs:
            us = 12
        if key == "APP MUL" and "mul_noisy" in self.bugs:
            us = 40 + (self.windows * 37) % 61
        n = self.prof.get(words[0], (0, 0, 0, 0))[0]
        return us + (n % 3)                                 # a little jitter, the same every run

    def record(self, verb, us):
        n, total, lo, hi = self.prof.get(verb, (0, 0, us, us))
        self.prof[verb] = (n + 1, total + us, min(lo, us), max(hi, us))

    def h_PROF(self, a):
        if self.fw < "0.4.0":
            return self._err(1, "unknown command PROF")
        words = a.split()
        if not words:
            win_ms = int((self.clock() - self.prof_start) * 1000)
            busy_us = sum(v[1] for v in self.prof.values())
            pct = 0 if not win_ms else min(10000, busy_us * 10 // max(1, win_ms))
            return (f"OK PROF window_ms={win_ms} loops={win_ms} busy_pct={pct // 100}.{pct % 100:02d} "
                    f"loop_max_us={max([v[3] for v in self.prof.values()] or [0]) + 15} loop_avg_us=12 "
                    f"tick_max_us=4 app_tick_avg_us=1 rx_hwm={self.rx_hwm} reply_hwm={self.reply_hwm}")
        if words == ["RESET"]:
            self.prof_reset()
            return "OK PROF reset"
        if len(words) == 2 and words[0] == "CMD":
            n, total, lo, hi = self.prof.get(words[1], (0, 0, 0, 0))
            return f"OK PROF cmd={words[1]} n={n} avg_us={total // n if n else 0} min_us={lo} max_us={hi}"
        if len(words) == 2 and words[0] == "APP" and words[1].isdigit() and int(words[1]) < 8:
            return f"OK PROF app={words[1]} name=- n=0 avg_us=0 min_us=0 max_us=0"
        return self._err(2, "usage: PROF [CMD <name>|APP <slot>|RESET]")

    # ------------------------------------------------------------ protocol
    def handle(self, line):
        reply = self._handle(line)
        self.rx_hwm = max(self.rx_hwm, len(line))
        self.reply_hwm = max(self.reply_hwm, len(reply))
        return reply

    def _handle(self, line):
        self.log.append(line)
        if len(line) > 128:
            return self._err(5, "line is longer than 128 characters")
        prefix = ""
        if line.startswith("#"):
            m = re.match(r"^#(\d{1,5}) (.*)$", line)
            if not m:
                return self._err(6, "bad request id")
            prefix, line = f"#{m.group(1)} ", m.group(2)
        if "*" in line:
            m = re.match(r"^(.*)\*([0-9A-F]{4})$", line)
            if not m:
                return prefix + self._err(6, "bad checksum field")
            if crc16(prefix + m.group(1)) != int(m.group(2), 16):
                return prefix + self._err(4, "checksum mismatch")
            line = m.group(1)
        if not line.isprintable() or not line.isascii():
            return prefix + self._err(6, "bad character")
        self.cmds += 1
        verb, _, rest = line.partition(" ")
        h = getattr(self, f"h_{verb}", None) if verb.isupper() and verb.isalpha() else None
        if h is None:
            return prefix + self._err(1)                     # like the harness: the verb is not echoed
        us = self.cost_us(line)
        out = h(rest)
        if verb != "PROF" or rest.split()[:1] != ["RESET"]:
            self.record(verb, us)
        return prefix + out

    def _err(self, code, text=""):
        self.errors += 1
        return f"ERR {code} {ERR[code]} {text}".rstrip()

    def h_PING(self, a):
        return "OK PONG" if not a else self._err(2, "PING takes no arguments")

    def h_HELLO(self, a):
        return "OK HELLO AEISP-DEV 0.3.0 DE61C48787331A35"

    def h_ECHO(self, a):
        return f"OK {a}" if a else self._err(2, "ECHO needs text")

    def h_UPTIME(self, a):
        return f"OK UPTIME {self.uptime()}" if not a else self._err(2, "UPTIME takes no arguments")

    def h_VERSION(self, a):
        return f"OK VERSION fw={self.fw} proto=1 sdk=1.5.1 build={self.build_id} src={self.src}"

    def h_INFO(self, a):
        return "OK INFO name=AEISP-DEV fw=0.3.0 proto=1 uid=DE61C48787331A35 board=shrike-lite mcu=rp2040 clk_mhz=125"

    def h_STATUS(self, a):
        return (f"OK STATUS uptime_ms={self.uptime()} led={self.led} blink_ms={self.blink} timer=IDLE "
                f"cmds={self.cmds} errors={self.errors}")

    def h_RESETINFO(self, a):
        return f"OK RESETINFO reason={self.reset_reason}"

    def h_TEMP(self, a):
        return "OK TEMP c=27.1"

    def h_LED(self, a):
        if a in ("ON", "OFF"):
            self.led = a
        elif a == "TOGGLE":
            self.led = "OFF" if self.led == "ON" else "ON"
        elif a != "GET":
            return self._err(2, "LED ON|OFF|TOGGLE|GET")
        return f"OK LED state={self.led}"

    def _num(self, a, lo, hi):
        if not re.fullmatch(r"-?\d{1,10}", a or ""):
            return None
        v = int(a)
        return v if lo <= v <= hi else None

    def h_BLINK(self, a):
        if not re.fullmatch(r"\d{1,10}", a or ""):
            return self._err(2, "BLINK <ms>")
        v = int(a)
        if v != 0 and not 50 <= v <= 10000:
            return self._err(3, "interval must be 0 or 50..10000 ms")
        self.blink = v
        return f"OK BLINK interval_ms={v}"

    def h_TIMER(self, a):
        if not re.fullmatch(r"\d{1,10}", a or ""):
            return self._err(2, "TIMER <ms>")
        return f"OK TIMER ms={int(a)}"

    def h_APP(self, a):
        if not a:
            return self._err(2, "usage: APP <command>")
        words = a.split(" ")
        cmd, args = words[0], words[1:]
        if "long_arg_crash" in self.bugs and any(len(w) > 24 for w in args):
            raise Crash()
        if cmd not in ("ADD", "MUL", "DIV"):
            return self._err(2, "unknown app command")
        if len(args) != 2:
            return self._err(2, f"usage: {cmd} <a> <b>")
        nums = [self._num(x, *INT32) for x in args]
        if None in nums:
            bad = [x for x, n in zip(args, nums) if n is None]
            return self._err(3 if all(re.fullmatch(r"-?\d+", x) for x in bad) else 2, "number out of range")
        x, y = nums
        if cmd == "ADD":
            if "slow_add" in self.bugs:
                time.sleep(0.7)
            return f"OK sum={x + y}"
        if cmd == "MUL":
            return f"OK product={x + y if 'mul_wrong' in self.bugs else x * y}"
        if y == 0:
            if "div_zero_dead" in self.bugs:
                self.dead = True
                raise Hang()
            if "div_zero_hang" in self.bugs:
                raise Hang()
            return self._err(3, "division by zero")
        return f"OK quotient={int(x / y)}"


class _Info:
    def __init__(self):
        self.port, self.firmware, self.version, self.uid = "COM8", "AEISP-DEV", "0.3.0", "DE61C48787331A35"

    def to_dict(self):
        return dict(self.__dict__)


class SimLink:
    """Looks like SerialLink to the DeviceManager."""

    def __init__(self, fw):
        self.fw = fw
        self.is_connected = False
        self.info = None
        self.lost = False                    # the USB port vanished (the board is restarting)

    def connect(self, handshake_tries=3):
        if self.fw.dead:
            raise NotConnected("no RP2040 serial port found (USB vendor ID 2E8A)")
        if self.fw.hung:                     # the watchdog fires while we try: the board comes back
            self.fw.reset("WATCHDOG_TIMEOUT")
        self.is_connected, self.info, self.lost = True, _Info(), False
        return self.info

    def close(self):
        self.is_connected = False

    def request(self, text, retries=0, reconnect_wait=5.0):
        if self.fw.dead and (self.lost or not self.is_connected):
            self.is_connected = False
            raise NotConnected("no RP2040 serial port found (USB vendor ID 2E8A)")
        if self.lost:
            self.lost = False
            self.is_connected = False
            raise NotConnected("link lost: device disconnected")
        if not self.is_connected:
            self.connect()
        if self.fw.hung:                     # still hung: the next attempt sees the port gone
            self.lost = True
            raise LinkTimeout(f"no reply to {text[:40]!r}")
        try:
            return self.fw.handle(text)
        except Hang:
            self.fw.hung = True
            raise LinkTimeout(f"no reply to {text[:40]!r}")
        except Crash:
            self.fw.reset("SOFTWARE_REBOOT")
            self.is_connected = False
            raise NotConnected("link lost: device disconnected")

    def send_partial(self, text, listen=3.0):
        self.fw.log.append(text + " <no line ending>")
        return [] if "no_line_timeout" in self.fw.bugs else ["ERR 7 TIMEOUT incomplete line"]
