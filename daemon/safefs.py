"""Descriptor-relative file handling for the few files this plugin owns.

Everything here works on directory file descriptors, never on pathnames that
are re-resolved later, so a path swapped underneath us (a symlink, a renamed
directory) cannot redirect a read, a write or a chmod somewhere else.

    open_trusted_dir(path)       walk path from / with O_NOFOLLOW, creating
                                 missing components 0700; every component must
                                 be owned by root or by us and not writable by
                                 anyone else, and a symlink is only resolved
                                 when it sits in such a directory
    open_private_dir(path)       open_trusted_dir, plus the final directory must
                                 be ours and is forced to 0700 through its fd
    read_private_file(dfd, ...)  bounded read of a regular file we own that
                                 nobody else can read or write
    write_file_atomic(dfd, ...)  create a fresh randomly named temp file with
                                 O_EXCL|O_NOFOLLOW at the final mode, fsync it,
                                 and rename it over the target within the same
                                 directory descriptor
"""

import os
import secrets
import stat

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(OSError):
    pass


def _check_component(st, where):
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePath(f"{where} is not a directory")
    if st.st_uid not in (0, os.getuid()):
        raise UnsafePath(f"{where} is owned by uid {st.st_uid}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafePath(f"{where} is writable by other users")


# Symlink hops allowed while resolving one path, as the kernel's own limit.
MAX_SYMLINKS = 40


def _open_component(parent_fd, part, create):
    """Open one path component under a vetted parent: (fd, None) or (None, link).

    A symlink is not followed here; its target is returned for the caller to
    resolve, so every directory it leads through is vetted in turn.
    """
    try:
        return os.open(part, _DIR_FLAGS, dir_fd=parent_fd), None
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(part, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return _open_component(parent_fd, part, False)
    except (NotADirectoryError, OSError) as exc:
        st = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            return None, os.readlink(part, dir_fd=parent_fd)
        if isinstance(exc, NotADirectoryError) or not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{part} is not a directory") from exc
        raise


def open_trusted_dir(path, create=True):
    """Return an O_DIRECTORY fd for `path`, opened without following links.

    The path is walked from / one component at a time, each opened with
    O_NOFOLLOW relative to its already-vetted parent's descriptor and vetted
    through its own: owned by root or by us, not group/other-writable.
    Missing components are created 0700 when `create` is set.

    A symlink is never followed by the kernel. Its target is read out of the
    vetted directory holding it and the walk restarts on the resulting path,
    so a link only counts if it lives in a directory that only root or we
    can write -- as with a distro that points /home at /var/home -- and
    everything it leads through is vetted again. A link planted anywhere
    someone else can write is never reached, because that directory fails
    the check first.
    """
    if not os.path.isabs(path):
        raise UnsafePath(f"{path} is not an absolute path")
    pending = [p for p in path.split("/") if p and p != "."]
    hops = 0
    while True:
        fd = os.open("/", _DIR_FLAGS)
        try:
            _check_component(os.fstat(fd), "/")
            done = []
            restart = None
            while pending:
                part = pending.pop(0)
                if part == "..":
                    # Textual parent: `done` holds only real directories,
                    # since every link so far has been resolved away.
                    restart = done[:-1] + pending
                    break
                child, link = _open_component(fd, part, create)
                if link is not None:
                    hops += 1
                    if hops > MAX_SYMLINKS:
                        raise UnsafePath(f"too many symlinks resolving {path}")
                    base = [] if link.startswith("/") else done
                    restart = base + [p for p in link.split("/")
                                      if p and p != "."] + pending
                    break
                os.close(fd)
                fd = child
                done.append(part)
                _check_component(os.fstat(fd), "/" + "/".join(done))
            if restart is None:
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        pending = restart


def open_private_dir(path):
    """open_trusted_dir, then require the directory itself to be ours, 0700.

    The mode is tightened with fchmod on the descriptor that was vetted, so
    the chmod can only ever land on this directory.
    """
    fd = open_trusted_dir(path, create=True)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise UnsafePath(f"{path} is not owned by us")
        if stat.S_IMODE(st.st_mode) != stat.S_IRWXU:
            os.fchmod(fd, stat.S_IRWXU)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private_file(dir_fd, name, max_bytes):
    """Read `name` in `dir_fd`: a regular file of ours, 0600 or tighter.

    Returns None if it does not exist. Refuses a symlink, a FIFO or device
    (O_NONBLOCK keeps opening one from hanging before the type check), a file
    someone else owns, one others can read or write, and anything larger
    than `max_bytes`.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                     | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(f"{name} is not a regular file")
        if st.st_uid != os.getuid():
            raise UnsafePath(f"{name} is owned by uid {st.st_uid}")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise UnsafePath(f"{name} is accessible to other users")
        if st.st_size > max_bytes:
            raise UnsafePath(f"{name} is larger than {max_bytes} bytes")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, max_bytes + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise UnsafePath(f"{name} is larger than {max_bytes} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_file_atomic(dir_fd, name, data, mode=0o600):
    """Atomically replace `name` in `dir_fd` with `data`, created at `mode`.

    The temp name is random and created O_EXCL|O_NOFOLLOW, so nothing that
    was already sitting at that name -- file or symlink -- is written through
    or reused. The rename is relative to the same descriptor, so the file
    lands in the directory that was vetted, not wherever its path now points.
    """
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    try:
        try:
            # The umask can only remove bits; set the exact mode explicitly.
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    try:
        os.fsync(dir_fd)
    except OSError:
        pass


def runtime_dir_path():
    """$XDG_RUNTIME_DIR, or the systemd default for our uid."""
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    return base if os.path.isabs(base) else f"/run/user/{os.getuid()}"


def open_runtime_dir():
    """Open the runtime directory, which must be ours and closed to others.

    Never created or chmod-ed: it belongs to the session manager, and one
    that is missing or shared means the session is not what we expect.
    """
    path = runtime_dir_path()
    fd = open_trusted_dir(path, create=False)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise UnsafePath(f"{path} is not owned by us")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise UnsafePath(f"{path} is accessible to other users")
        return fd
    except BaseException:
        os.close(fd)
        raise
