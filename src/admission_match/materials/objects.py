"""Content-addressed immutable file objects.

Objects live under ``<store>/objects/xx/<64-hex-sha256>`` and are named by the
SHA256 of their bytes, so identical content is stored exactly once.  Every
file is streamed in fixed-size chunks: hashing, size accounting and copying
never buffer a whole object, which keeps peak RSS far below the 64 MiB limit
even for the largest legal 64 MiB object.

Path safety
-----------
A manifest path is a relative POSIX path beneath the batch files directory.
Absolute paths, backslashes, empty segments, ``.``/``..`` and a symlink at
*any* directory level are rejected before the file is opened; only regular
files are received.

Crash boundaries
----------------
New bytes are written to a temporary file in the same directory, fsynced and
then atomically ``os.replace``d onto their final name.  An existing object is
never overwritten: it is re-verified (size and full SHA256) before reuse, and
a corrupt existing object aborts the batch instead of being replaced.  Objects
are committed to disk *before* the SQLite directory transaction starts, so a
crash between the two phases leaves only unreferenced complete objects -- the
directory either stays at the complete old revision or moves to the complete
new one.
"""
import hashlib
import os
import tempfile

from .roster import MaterialsError
CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming window


def objects_dir(store_root):
    return os.path.join(store_root, 'objects')


def object_path(store_root, digest):
    return os.path.join(objects_dir(store_root), digest[:2], digest)


def _safe_source(files_dir, relative):
    """Resolve a manifest path, rejecting traversal and every symlink layer."""
    base = os.path.abspath(files_dir)
    parts = relative.split('/')
    current = base
    checked = []
    for part in parts:
        current = os.path.join(current, part)
        checked.append(part)
        # lstat sees symlinks themselves; every component up to and including
        # the final name must be a real directory entry, never a link.
        if os.path.islink(current):
            raise MaterialsError(
                'symlink rejected: ' + '/'.join(checked))
        if not os.path.exists(current):
            raise MaterialsError(
                'material file does not exist: ' + relative)
    # The chain must stay inside the files directory (defence in depth; the
    # manifest parser already forbids absolute paths, .. and empty segments,
    # and every component above was proven not to be a symlink).
    real = os.path.realpath(current)
    if not _is_within(base, real):
        raise MaterialsError('path escapes the files directory: ' + relative)
    if not os.path.isfile(current):
        raise MaterialsError('only regular files are accepted: ' + relative)
    return current


def _is_within(base, target):
    base = os.path.abspath(base)
    target = os.path.abspath(target)
    try:
        return os.path.commonpath([base, target]) == base
    except ValueError:
        return False


def hash_file(path):
    """Return ``(sha256_hex, byte_size)`` streaming one chunk at a time."""
    hasher = hashlib.sha256()
    size = 0
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def verify_existing_object(store_root, digest, expected_size):
    """Re-verify a stored object before reusing it; never overwrite it.

    A size or hash mismatch means the on-disk object is corrupt.  Callers must
    fail the whole batch rather than replacing the object, because committed
    slots may already reference it.
    """
    dest = object_path(store_root, digest)
    if not os.path.isfile(dest) or os.path.islink(dest):
        raise MaterialsError(
            'stored object missing or not a regular file: ' + digest)
    actual_digest, actual_size = hash_file(dest)
    if actual_size != expected_size:
        raise MaterialsError(
            'stored object %s has wrong byte size' % digest)
    if actual_digest != digest:
        raise MaterialsError(
            'stored object %s fails hash verification' % digest)


def receive_object(store_root, files_dir, relative, digest, declared_size):
    """Stream one source file into the content-addressed object store.

    Validates the actual byte count and SHA256 against the manifest, creates
    the object atomically when new, and re-verifies (never overwrites) when
    the content already exists.  Returns the digest on success.
    """
    source = _safe_source(files_dir, relative)
    dest = object_path(store_root, digest)
    if os.path.lexists(dest):
        # Reuse path: the object must be complete and match exactly.  It is
        # never overwritten, so a corrupt object fails the batch cleanly.
        verify_existing_object(store_root, digest, declared_size)
        return digest

    bucket = os.path.dirname(dest)
    os.makedirs(bucket, exist_ok=True)
    hasher = hashlib.sha256()
    actual_size = 0
    tmp_name = None
    try:
        with open(source, 'rb') as src, \
                tempfile_in_dir(bucket) as tmp_path:
            tmp_name = tmp_path
            with open(tmp_path, 'wb') as out:
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    actual_size += len(chunk)
                    if actual_size > declared_size:
                        raise MaterialsError(
                            'actual byte count exceeds declared byte_size: '
                            + relative)
                    hasher.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        if actual_size != declared_size:
            raise MaterialsError(
                'declared byte_size does not match actual bytes: ' + relative)
        actual_digest = hasher.hexdigest()
        if actual_digest != digest:
            raise MaterialsError(
                'declared sha256 does not match actual content: ' + relative)
        # Atomic publication; between this point and the directory commit the
        # object is fully on disk but unreferenced by any slot.
        os.replace(tmp_name, dest)
        tmp_name = None
        _fsync_dir(bucket)
    finally:
        if tmp_name is not None and os.path.lexists(tmp_name):
            os.unlink(tmp_name)
    return digest


class tempfile_in_dir:
    """Context manager giving a unique temporary file name inside ``dir_``."""

    def __init__(self, dir_):
        self._dir = dir_
        self._path = None

    def __enter__(self):
        fd, self._path = tempfile.mkstemp(prefix='.incoming-', dir=self._dir)
        os.close(fd)
        return self._path

    def __exit__(self, exc_type, exc, tb):
        return False


def _fsync_dir(path):
    """Best-effort directory fsync so a rename survives a power loss."""
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_slot_object(store_root, digest, expected_size):
    """Read-only verification used by reports before trusting a current put."""
    dest = object_path(store_root, digest)
    if not os.path.isfile(dest) or os.path.islink(dest):
        raise MaterialsError('referenced object missing: ' + digest)
    actual_digest, actual_size = hash_file(dest)
    if actual_size != expected_size or actual_digest != digest:
        raise MaterialsError('referenced object corrupt: ' + digest)
