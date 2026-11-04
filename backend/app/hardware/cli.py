"""Command-line tools.   Run from the backend folder:  python -m app.hardware.cli <command>

Commands: ports | hello | ping | raw "<text>" | soak | watch | bootsel | term
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from datetime import datetime

from .discovery import list_all_ports
from .link import LinkError, LinkTimeout, NotConnected, SerialLink
from .protocol import add_crc, parse_reply


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def cmd_ports(args) -> int:
    ports = list_all_ports()
    if not ports:
        print("No serial ports found.")
        return 1
    for p in ports:
        d = p.to_dict()
        mark = "  <-- RP2040" if p.is_rp2040 else ""
        print(f"{d['device']:<8} VID={d['vid']} PID={d['pid']} serial={d['serial_number']} {d['description']}{mark}")
    return 0 if any(p.is_rp2040 for p in ports) else 1


def cmd_hello(args) -> int:
    link = SerialLink(port=args.port)
    info = link.connect()
    print(f"CONNECTED port={info.port} firmware={info.firmware} version={info.version} uid={info.uid}")
    link.close()
    return 0


def cmd_raw(args) -> int:
    link = SerialLink(port=args.port)
    link.connect()
    reply = link.request(args.text, retries=0)
    print(reply)
    link.close()
    return 0


def cmd_ping(args) -> int:
    link = SerialLink(port=args.port)
    link.connect()
    t0 = time.perf_counter()
    reply = link.request("PING", retries=0)
    ms = (time.perf_counter() - t0) * 1000
    print(f"{reply}  ({ms:.2f} ms)")
    link.close()
    return 0 if reply == "OK PONG" else 1


def cmd_soak(args) -> int:
    link = SerialLink(port=args.port)
    link.connect()
    rng = random.Random(args.seed)
    ok = mismatch = timeouts = errors = 0
    lat: list[float] = []
    for i in range(args.count):
        payload = "".join(rng.choice("0123456789abcdef") for _ in range(rng.randint(1, 60)))
        t0 = time.perf_counter()
        try:
            reply = link.request(f"ECHO {payload}", retries=0)
        except LinkError as e:
            if "no reply" in str(e):
                timeouts += 1
            else:
                errors += 1
            print(f"[{i}] {type(e).__name__}: {e}")
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        if reply == f"OK {payload}":
            ok += 1
        else:
            mismatch += 1
            print(f"[{i}] MISMATCH sent={payload!r} got={reply!r}")
    link.close()
    print()
    print(f"sent={args.count} ok={ok} mismatch={mismatch} timeouts={timeouts} errors={errors}")
    if lat:
        lat_sorted = sorted(lat)
        p95 = lat_sorted[int(0.95 * (len(lat_sorted) - 1))]
        print(f"latency ms: min={min(lat):.2f} avg={statistics.mean(lat):.2f} p95={p95:.2f} max={max(lat):.2f}")
    return 0 if ok == args.count else 1


def cmd_watch(args) -> int:
    link = SerialLink(port=args.port, timeout=0.5)
    end = time.time() + args.seconds if args.seconds else None
    last = None
    print(f"[{_stamp()}] watching (Ctrl+C to stop)")
    try:
        while end is None or time.time() < end:
            try:
                if not link.is_connected:
                    link.connect()
                else:
                    link.request("PING", retries=0)
                state = f"CONNECTED port={link.info.port} uid={link.info.uid}"
            except LinkError as e:
                state = f"DISCONNECTED ({e})"
            if state != last:
                print(f"[{_stamp()}] {state}")
                last = state
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    link.close()
    return 0


def cmd_bootsel(args) -> int:
    link = SerialLink(port=args.port)
    link.connect()
    print(link.request("BOOTSEL", retries=0))
    link.close()
    return 0


# ---------------------------------------------------------------- interactive terminal

TERM_HELP = """\
Type a command and press Enter. Whatever you type is sent to the board exactly as typed.

Special commands (start with a colon, they are handled here, not sent as typed):
  :crc <text>      send <text> with a checksum added automatically   e.g.  :crc LED ON
  :frame <text>    only SHOW what :crc would send (nothing is sent)
  :partial <text>  send <text> WITHOUT pressing Enter, then wait 3 s and show what the board says
  :blank           send an empty line
  :help            show this text
  :quit            leave (Ctrl+C also works)
"""


def _annotate(reply: str) -> str:
    r = parse_reply(reply)
    if r.crc_present:
        return f"{reply}    [checksum {'OK' if r.crc_ok else 'WRONG'}]"
    return reply


def _term_send(link: SerialLink, text: str) -> None:
    t0 = time.perf_counter()
    try:
        reply = link.request(text, retries=0)
    except LinkTimeout:
        print("  (no reply within 1 second)")
        return
    except NotConnected as e:
        print(f"  (connection lost: {e})")
        print("  reconnecting, please wait ...")
        try:
            info = link.reconnect(20.0)
            print(f"  RECONNECTED to {info.port} ({info.firmware} {info.version}). Your last command was NOT re-sent; type it again.")
        except LinkError as e2:
            print(f"  could not reconnect: {e2}")
        return
    except LinkError as e:
        print(f"  (error: {e})")
        return
    ms = (time.perf_counter() - t0) * 1000
    print(f"  {_annotate(reply)}    ({ms:.1f} ms)")


def cmd_term(args) -> int:
    link = SerialLink(port=args.port)
    info = link.connect()
    print(f"Connected to {info.port}: {info.firmware} {info.version}  (board id {info.uid})")
    print("Type a command and press Enter.   :help = help,   :quit = leave\n")
    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            text = line.rstrip("\r\n")
            if text.strip() == "":
                continue
            if text.startswith(":"):
                word, _, arg = text.partition(" ")
                if word in (":quit", ":q", ":exit"):
                    break
                elif word == ":help":
                    print(TERM_HELP)
                elif word == ":blank":
                    _term_send(link, "")
                elif word == ":frame":
                    print(f"  would send: {add_crc(arg)}")
                elif word == ":crc":
                    framed = add_crc(arg)
                    print(f"  sending: {framed}")
                    _term_send(link, framed)
                elif word == ":partial":
                    print(f"  sent '{arg}' without a line ending; waiting 3 seconds ...")
                    try:
                        replies = link.send_partial(arg, 3.0)
                    except LinkError as e:
                        print(f"  (error: {e})")
                        continue
                    if replies:
                        for r in replies:
                            print(f"  {_annotate(r)}")
                    else:
                        print("  (the board said nothing)")
                else:
                    print("  unknown ':' command, type :help")
                continue
            _term_send(link, text)
    except KeyboardInterrupt:
        print()
    link.close()
    print("Disconnected.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.hardware.cli")
    ap.add_argument("--port", help="COM port, e.g. COM8 (default: auto-detect RP2040)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports").set_defaults(fn=cmd_ports)
    sub.add_parser("hello").set_defaults(fn=cmd_hello)
    sub.add_parser("ping").set_defaults(fn=cmd_ping)
    sub.add_parser("bootsel").set_defaults(fn=cmd_bootsel)
    sub.add_parser("term").set_defaults(fn=cmd_term)
    p = sub.add_parser("raw")
    p.add_argument("text")
    p.set_defaults(fn=cmd_raw)
    p = sub.add_parser("soak")
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(fn=cmd_soak)
    p = sub.add_parser("watch")
    p.add_argument("--seconds", type=int, default=0, help="0 = until Ctrl+C")
    p.set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except LinkError as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())