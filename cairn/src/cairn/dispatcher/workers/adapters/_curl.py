from __future__ import annotations

import shlex
from dataclasses import dataclass


_VERBOSE_CURL_SCRIPT = """
url="$1"
payload="$2"
shift 2

body_file="$(mktemp)"
cleanup() {
    rm -f "$body_file"
}
trap cleanup EXIT

curl_rc=0
http_status="$(
    curl -sS -o "$body_file" -w '%{http_code}' "$url" "$@" -d "$payload"
)" || curl_rc=$?

printf 'http_status=%s\\n' "$http_status"
if [ -s "$body_file" ]; then
    cat "$body_file"
fi

if [ "$curl_rc" -ne 0 ]; then
    exit "$curl_rc"
fi

case "$http_status" in
    2??) exit 0 ;;
    *) exit 1 ;;
esac
""".strip()


def build_curl_healthcheck(
    url: str,
    *,
    headers: list[str],
    payload: str,
    required_executables: tuple[str, ...] = (),
) -> list[str]:
    return [
        "/bin/sh",
        "-lc",
        _preflight_script(required_executables) + 'exec curl "$@"',
        "--",
        "-sS",
        "--fail",
        "-o",
        "/dev/null",
        url,
        *headers,
        "-d",
        payload,
    ]


def build_verbose_curl_healthcheck(
    url: str,
    *,
    headers: list[str],
    payload: str,
    required_executables: tuple[str, ...] = (),
) -> list[str]:
    return [
        "/bin/sh",
        "-lc",
        _preflight_script(required_executables) + _VERBOSE_CURL_SCRIPT,
        "--",
        url,
        payload,
        *headers,
    ]


@dataclass(frozen=True, slots=True)
class ShellArgument:
    text: str
    expand_env: bool = False


def expand_env(text: str) -> ShellArgument:
    return ShellArgument(text=text, expand_env=True)


def render_curl_command(
    url: str,
    *,
    headers: list[str | ShellArgument],
    payload: str,
    required_executables: tuple[str, ...] = (),
) -> str:
    parts = ["curl", "-sS", url, *headers, "-d", payload]
    command = " ".join(_render_shell_argument(part) for part in parts)
    preflight = " && ".join(
        f"command -v {shlex.quote(executable)} >/dev/null"
        for executable in required_executables
    )
    return f"{preflight} && {command}" if preflight else command


def _preflight_script(required_executables: tuple[str, ...]) -> str:
    lines = []
    for executable in required_executables:
        quoted = shlex.quote(executable)
        lines.append(
            f"command -v {quoted} >/dev/null || "
            f"{{ echo 'missing executable: {executable}' >&2; exit 127; }}"
        )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _render_shell_argument(part: str | ShellArgument) -> str:
    if isinstance(part, ShellArgument):
        if part.expand_env:
            return _double_quote_with_env_expansion(part.text)
        return shlex.quote(part.text)
    return shlex.quote(part)


def _double_quote_with_env_expansion(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'
