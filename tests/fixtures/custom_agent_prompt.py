"""Run against a locally built Herdr: python3 tests/fixtures/custom_agent_prompt.py target/debug/herdr."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time


def verify(herdr, directory):
    base = Path(directory).resolve()
    sink = base / "received"
    # Use a native foreground executable, not a process that merely mentions the
    # custom agent label in its arguments.
    source = base / "fixture.c"
    source.write_text(
        '#include <stdio.h>\n'
        'int main(int argc, char **argv) {\n'
        '  FILE *out = fopen(argv[1], "a");\n'
        '  if (!out) return 1;\n'
        '  char line[4096];\n'
        '  puts("CUSTOM_FIXTURE_READY"); fflush(stdout);\n'
        '  while (fgets(line, sizeof(line), stdin)) { fputs(line, out); fflush(out); }\n'
        '  return fclose(out);\n'
        '}\n'
    )
    fixture = base / "fixture"
    subprocess.run(["cc", "-o", fixture, source], check=True)
    env = {k: str(base / v) for k, v in {
        "HOME": "h", "TMPDIR": "t", "XDG_CONFIG_HOME": "c",
        "XDG_STATE_HOME": "s", "XDG_DATA_HOME": "d", "XDG_RUNTIME_DIR": "r",
    }.items()}
    for path in env.values():
        Path(path).mkdir(mode=0o700)
    env.update(PATH=os.defpath, SHELL="/bin/sh", TERM="xterm-256color")
    cli = [str(herdr), "--session", "fixture"]

    def call(*args, error=None):
        p = subprocess.run(cli + list(args), check=False, capture_output=True,
                           cwd=base, env=env, text=True, timeout=6)
        if error:
            assert p.returncode == 1, (args, p.stdout, p.stderr)
            expected = (error,) if isinstance(error, str) else error
            assert json.loads(p.stderr)["error"]["code"] in expected, (args, p.stderr)
        elif p.returncode:
            raise RuntimeError((args, p.stdout, p.stderr))
        return p.stdout

    with (base / "server.log").open("w") as log:
        server = subprocess.Popen(cli + ["server"], env=env, cwd=base,
                                  stdout=log, stderr=log)
        try:
            for _ in range(45):
                try:
                    call("workspace", "list")
                    break
                except RuntimeError:
                    if server.poll() is not None:
                        raise RuntimeError((base / "server.log").read_text())
                    time.sleep(.1)

            info = json.loads(call("workspace", "create", "--cwd", str(base)))
            pane = info["result"]["root_pane"]["pane_id"]
            own = ("--source", "custom:fixture", "--agent", "fixture")

            def identity(expected):
                for kind in ("agent", "pane"):
                    result = json.loads(call(kind, "get", pane))["result"][kind]
                    assert (result.get("agent_session") or {}).get("value") == expected, result

            def state(value, seq, *options):
                call("pane", "report-agent", pane, *own, "--state", value,
                     "--seq", str(seq), *options)

            def session(value, seq):
                # Keep the same immutable pane target on all calls.
                call("pane", "report-agent-session", pane, *own, "--seq",
                     str(seq), "--agent-session-id", value)

            time.sleep(.4)
            call("pane", "run", pane, shlex.join([str(fixture), str(sink)]))
            call("pane", "wait-output", pane, "--match", "CUSTOM_FIXTURE_READY", "--timeout", "4000")
            session("unowned", 1)
            state("idle", 2, "--agent-session-id", "session-a")
            identity("session-a")
            assert sink.read_text() == ""
            call("agent", "prompt", pane, "unguarded", error="agent_not_ready")
            call("agent", "prompt", pane, "wrong-session", "--expect-session",
                 "wrong", error="agent_session_mismatch")
            state("blocked", 3)
            call("agent", "prompt", pane, "blocked", "--expect-session",
                 "session-a", error="agent_blocked")
            state("working", 4)
            call("agent", "prompt", pane, "working", "--expect-session",
                 "session-a", error="agent_not_ready")
            state("idle", 5)
            call("pane", "report-agent", pane, "--source", "custom:other",
                 "--agent", "fixture", "--state", "idle", "--seq", "6",
                 "--agent-session-id", "foreign", error="agent_owner_mismatch")
            session("session-b", 7)
            session("stale", 6)
            identity("session-b")
            call("agent", "prompt", pane, "old", "--expect-session",
                 "session-a", error="agent_session_mismatch")
            assert sink.read_text() == ""
            call("agent", "prompt", pane, "FIXTURE_PROMPT", "--expect-session", "session-b")
            for _ in range(30):
                if sink.read_text():
                    break
                time.sleep(.05)
            assert sink.read_text() == "FIXTURE_PROMPT\n"
            call("pane", "send-keys", pane, "ctrl+c")
            time.sleep(.4)
            call("agent", "prompt", pane, "after-exit", "--expect-session", "session-b",
                 error=("agent_not_ready", "agent_not_found"))
            assert sink.read_text() == "FIXTURE_PROMPT\n"
        finally:
            try:
                call("server", "stop")
                server.wait(timeout=3)
            except (RuntimeError, subprocess.TimeoutExpired):
                server.kill()
                server.wait()
    print("Custom binding and guarded PTY submission verified.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="hca", dir="/tmp") as temp:
        verify(Path(sys.argv[1]).resolve(strict=True), temp)
