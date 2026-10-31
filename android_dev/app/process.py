"""Subprocess capture with timeouts and bounded tail output."""
import os
import selectors
import signal
import subprocess
import time

MAX_OUTPUT_BYTES = 1024 * 1024


def run(argv, *, timeout=30, input=None, text=True, max_bytes=MAX_OUTPUT_BYTES, cwd=None, env=None):
    payload = input.encode() if isinstance(input, str) else input
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    if payload is not None:
        try: proc.stdin.write(payload); proc.stdin.close()
        except BrokenPipeError: pass
    output = bytearray()
    truncated = False
    eof = False
    start = time.monotonic()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if time.monotonic() - start >= timeout:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    proc.wait()
                raise subprocess.TimeoutExpired(argv, timeout, output=bytes(output))
            ready = selector.select(min(0.1, max(0.001, timeout - (time.monotonic() - start))))
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if chunk:
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        del output[:len(output) - max_bytes]
                        truncated = True
                else:
                    eof = True
                    selector.unregister(proc.stdout)
            # A daemon the command started (adb's server) can hold the pipe open
            # after the command exits, so exit plus an idle pipe is also the end.
            if proc.poll() is not None and (eof or not ready):
                break
    proc.stdout.close()
    result = bytes(output)
    if truncated: result = b"[earlier output truncated; showing final bytes]\n" + result
    decoded = result.decode(errors="replace") if text else result
    return subprocess.CompletedProcess(argv, proc.returncode, decoded, "")
