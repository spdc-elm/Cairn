from __future__ import annotations

from io import StringIO

from cairn.dispatcher.runtime.environments.ssh import SshManagedProcess


class _Logger:
    def __init__(self) -> None:
        self.chunks: list[tuple[str, str]] = []

    def write_stream(self, stream: str, text: str) -> None:
        self.chunks.append((stream, text))


def test_ssh_managed_process_stream_reader_flushes_lines_to_logger():
    process = SshManagedProcess.__new__(SshManagedProcess)
    logger = _Logger()
    process.run_logger = logger
    target: list[str] = []

    process._read_stream(StringIO("one\n" "two\n"), target, "stdout")

    assert target == ["one\n", "two\n"]
    assert logger.chunks == [("stdout", "one\n"), ("stdout", "two\n")]
