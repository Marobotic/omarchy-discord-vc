"""Write or remove the daemon's systemd user unit.

    python3 -I service.py write-unit    install/replace the unit file
    python3 -I service.py remove-unit   delete it

Called by bin/omarchy-discord-vc, never directly. The unit makes the daemon
persistent and the daemon holds the Discord token, so before anything is
written this checks that what the unit will execute can only have been put
there by root or by us:

  * the interpreter is the absolute /usr/bin/python3 -- never PATH or
    /usr/bin/env -- and it and every directory above it are root-owned and
    not writable by anyone else;
  * the daemon directory, and each module the daemon imports, is owned by
    root or by us and not group/other-writable, reached without symlinks.

The unit runs that interpreter in isolated mode (-I: no PYTHON* variables, no
user site-packages, no script-relative imports it did not ask for), with a
fixed PATH and the dynamic-loader variables stripped, so nothing from the
user manager's environment chooses what code runs.

The unit file itself is written through safefs: the unit directory is walked
without following symlinks, and the file is created under a random name with
O_EXCL|O_NOFOLLOW and renamed into place relative to that directory.
"""

import os
import re
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from safefs import (  # noqa: E402
    MAX_SYMLINKS, UnsafePath, open_trusted_dir, write_file_atomic)

UNIT_NAME = "omarchy-discord-vc.service"
PYTHON = "/usr/bin/python3"
DAEMON_MODULES = ("daemon.py", "rpc.py", "auth.py", "safefs.py")

# Characters allowed in the daemon path embedded in ExecStart. Anything else
# (quotes, backslashes, whitespace, systemd's % specifiers, newlines) could
# change how systemd parses the line, so it is refused rather than escaped.
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]+$")


def unit_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "systemd", "user")


def _check_code_file(dir_fd, name, where, root_only=False):
    """A regular file (not a link) that only root -- or we -- could write."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                 | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    owners = (0,) if root_only else (0, os.getuid())
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"{where} is not a regular file")
    if st.st_uid not in owners:
        raise UnsafePath(f"{where} is owned by uid {st.st_uid}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafePath(f"{where} is writable by other users")


def check_python():
    """Vet /usr/bin/python3, following its symlink chain only through root's dirs."""
    path = PYTHON
    for _ in range(MAX_SYMLINKS):
        parent, name = os.path.split(path)
        dfd = open_trusted_dir(parent, create=False)
        try:
            if os.fstat(dfd).st_uid != 0:
                raise UnsafePath(f"{parent} is not owned by root")
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                if st.st_uid != 0:
                    raise UnsafePath(f"{path} is a symlink not owned by root")
                path = os.path.normpath(
                    os.path.join(parent, os.readlink(name, dir_fd=dfd)))
                continue
            _check_code_file(dfd, name, path, root_only=True)
            if not os.access(name, os.X_OK, dir_fd=dfd):
                raise UnsafePath(f"{path} is not executable")
            return path
        finally:
            os.close(dfd)
    raise UnsafePath(f"too many symlinks resolving {PYTHON}")


def check_daemon(daemon_dir):
    if not SAFE_PATH.match(daemon_dir):
        raise UnsafePath(
            f"{daemon_dir!r} contains characters that cannot be embedded "
            "safely in a unit file; install the plugin under a plainer path")
    dfd = open_trusted_dir(daemon_dir, create=False)
    try:
        for module in DAEMON_MODULES:
            _check_code_file(dfd, module, os.path.join(daemon_dir, module))
    finally:
        os.close(dfd)


def render_unit(daemon_dir):
    return f"""[Unit]
Description=Discord voice state publisher for the Omarchy bar
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
# Absolute interpreter in isolated mode; nothing from the environment picks
# which python or which modules run.
ExecStart={PYTHON} -I -B "{daemon_dir}/daemon.py"
Environment=PATH=/usr/bin
UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE
Restart=always
RestartSec=3
UMask=0077
# The daemon only talks to Discord's unix socket and writes two small files
# under $XDG_RUNTIME_DIR and $XDG_STATE_HOME: no network, no privileges.
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
SystemCallArchitectures=native

[Install]
WantedBy=graphical-session.target
"""


def write_unit():
    # Embedded in ExecStart, so it must name the files that were vetted: the
    # canonical path, walked again by check_daemon() without following links.
    daemon_dir = os.path.dirname(os.path.realpath(__file__))
    check_python()
    check_daemon(daemon_dir)
    dfd = open_trusted_dir(unit_dir(), create=True)
    try:
        write_file_atomic(dfd, UNIT_NAME, render_unit(daemon_dir).encode(),
                          mode=0o644)
    finally:
        os.close(dfd)
    print(os.path.join(unit_dir(), UNIT_NAME))


def remove_unit():
    try:
        dfd = open_trusted_dir(unit_dir(), create=False)
    except FileNotFoundError:
        return
    try:
        try:
            st = os.stat(UNIT_NAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        # Only ever remove our own file -- a link or directory placed at
        # that name is left alone for the user to look at.
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise UnsafePath(f"{UNIT_NAME} is not a regular file we own")
        os.unlink(UNIT_NAME, dir_fd=dfd)
    finally:
        os.close(dfd)


def main(argv):
    commands = {"write-unit": write_unit, "remove-unit": remove_unit}
    if len(argv) != 2 or argv[1] not in commands:
        print(f"usage: {argv[0]} write-unit|remove-unit", file=sys.stderr)
        return 2
    try:
        commands[argv[1]]()
    except OSError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
