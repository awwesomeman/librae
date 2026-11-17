"""A live halt kept on local disk until an operator resets it.

The state store records a halt in its checkpoint, but a halt made while the
store is down reaches it only once the store answers, and a restart before
then would load the older checkpoint, unhalted. So a live halt is written here
before it cancels anything, and a requested one as the request is made unless
a halt is there already; a start that finds it comes up halted whatever the
checkpoint says, and only ``reset_halt`` removes it.

The write is atomic and durable: the record goes to a temporary file in the
same directory, which is fsynced and renamed over the halt file, then the
directory is fsynced. rename(2): "If newpath already exists, it will be
atomically replaced, so that there is no point at which another process
attempting to access newpath will find it missing"
(https://man7.org/linux/man-pages/man2/rename.2.html). fsync(2): "Calling
fsync() does not necessarily ensure that the entry in the directory containing
the file has also reached disk. For that an explicit fsync() on a file
descriptor for the directory is also needed"
(https://man7.org/linux/man-pages/man2/fsync.2.html).

Each write has a temporary file of its own, since a halt request may write
from another thread, or from a signal handler that interrupts a write in
progress. Its name comes from a counter, not ``tempfile``, whose first call
takes a lock that the interrupted code may hold.
"""

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Mapping
from pathlib import Path

_write_ids = itertools.count()


def write_halt_file(path: Path, record: Mapping[str, str]) -> None:
    """Replace ``path`` with ``record``, atomically and durably."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{next(_write_ids)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(record), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_halt_file(path: Path) -> str | None:
    """The halt ``path`` holds, described for an alert, or None when there is none.

    A record that does not decode or parse still reads as a halt: the file
    exists only because a halt was written. A read error other than a missing
    file raises.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
        return (
            f"{record['title']}: {record['message']} "
            f"(run_id={record['run_id']}, at {record['halted_at']})"
        )
    except (ValueError, KeyError, TypeError) as exc:
        return f"unreadable halt record ({type(exc).__name__}: {exc})"
