#!/usr/bin/env python3

import argparse
import json
import socket
import struct
import sys
import time


MAX_PAYLOAD = 1024 * 1024
PROPERTIES = {
    "bootCompleted": "sys.boot_completed",
    "avdName": "ro.boot.qemu.avd_name",
}


class AdbProtocolError(Exception):
    pass


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("ADB observation budget expired")
    return value


def read_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        sock.settimeout(remaining(deadline))
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise AdbProtocolError(
                f"ADB server closed after {len(chunks)} of {size} expected bytes"
            )
        chunks.extend(chunk)
    return bytes(chunks)


def read_length(sock: socket.socket, deadline: float) -> int:
    raw = read_exact(sock, 4, deadline)
    try:
        size = int(raw, 16)
    except ValueError as error:
        raise AdbProtocolError(f"invalid ADB length {raw!r}") from error
    if size > MAX_PAYLOAD:
        raise AdbProtocolError(f"ADB payload exceeds {MAX_PAYLOAD} bytes")
    return size


def send_service(sock: socket.socket, service: str, deadline: float) -> None:
    encoded = service.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise AdbProtocolError("ADB service name is too long")
    request = f"{len(encoded):04x}".encode("ascii") + encoded
    sock.settimeout(remaining(deadline))
    sock.sendall(request)
    status = read_exact(sock, 4, deadline)
    if status == b"OKAY":
        return
    if status == b"FAIL":
        size = read_length(sock, deadline)
        detail = read_exact(sock, size, deadline).decode("utf-8", "replace")
        raise AdbProtocolError(f"ADB server rejected {service!r}: {detail}")
    raise AdbProtocolError(f"invalid ADB status {status!r}")


def connect(host: str, port: int, deadline: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=remaining(deadline))
    sock.settimeout(remaining(deadline))
    return sock


def get_state(host: str, port: int, serial: str, budget: float) -> str:
    deadline = time.monotonic() + budget
    with connect(host, port, deadline) as sock:
        send_service(sock, f"host-serial:{serial}:get-state", deadline)
        size = read_length(sock, deadline)
        return read_exact(sock, size, deadline).decode("utf-8", "strict").strip()


def get_property(
    host: str, port: int, serial: str, property_name: str, budget: float
) -> str:
    deadline = time.monotonic() + budget
    stdout = bytearray()
    stderr = bytearray()
    exit_code = None
    with connect(host, port, deadline) as sock:
        send_service(sock, f"host:transport:{serial}", deadline)
        send_service(sock, f"shell,v2,raw:getprop {property_name}", deadline)
        while exit_code is None:
            header = read_exact(sock, 5, deadline)
            stream_id = header[0]
            size = struct.unpack("<I", header[1:])[0]
            if size > MAX_PAYLOAD:
                raise AdbProtocolError(
                    f"ADB shell payload exceeds {MAX_PAYLOAD} bytes"
                )
            payload = read_exact(sock, size, deadline)
            if stream_id == 1:
                stdout.extend(payload)
            elif stream_id == 2:
                stderr.extend(payload)
            elif stream_id == 3:
                if len(payload) != 1:
                    raise AdbProtocolError("invalid ADB shell exit frame")
                exit_code = payload[0]
            else:
                raise AdbProtocolError(f"unexpected ADB shell stream {stream_id}")
    if exit_code != 0:
        detail = stderr.decode("utf-8", "replace").strip()
        raise AdbProtocolError(
            f"getprop {property_name} exited {exit_code}: {detail or 'no stderr'}"
        )
    if stderr:
        raise AdbProtocolError(
            f"getprop {property_name} wrote stderr: "
            f"{stderr.decode('utf-8', 'replace').strip()}"
        )
    return stdout.decode("utf-8", "strict").strip()


def observe(label: str, query) -> dict[str, str]:
    try:
        return {"status": "pass", "value": query()}
    except (AdbProtocolError, OSError, TimeoutError, UnicodeError) as error:
        reason = str(error) or error.__class__.__name__
        print(f"{label}: {reason}", file=sys.stderr)
        return {"status": "unmet", "reason": reason}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read fixed readiness values from an existing ADB server"
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5037)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not args.serial:
        parser.error("--serial must not be empty")
    return args


def main() -> int:
    args = parse_args()
    queries = {
        "state": observe(
            "state",
            lambda: get_state(args.host, args.port, args.serial, args.timeout),
        )
    }
    for label, property_name in PROPERTIES.items():
        queries[label] = observe(
            label,
            lambda property_name=property_name: get_property(
                args.host,
                args.port,
                args.serial,
                property_name,
                args.timeout,
            ),
        )
    print(json.dumps({"schemaVersion": 1, "queries": queries}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
