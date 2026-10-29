"""Release signatures with a key the org holds, in OpenSSH's SSH signature format.

Ruling (M3-21): an Ed25519 key the org generates and keeps in its secret store signs every
release file; the signatures are SSHSIG (OpenSSH PROTOCOL.sshsig), the format of
`ssh-keygen -Y sign`, under the namespace `agentic-release`. Anyone can check a download
with stock OpenSSH, which Windows, macOS and Linux all ship:

    ssh-keygen -Y verify -f allowed_signers -I release@your-org -n agentic-release \
        -s agentic_cli-1.0.0-py3-none-any.whl.sig < agentic_cli-1.0.0-py3-none-any.whl

`allowed_signers` (one `principal [namespaces="..."] ssh-ed25519 AAAA...` line per key)
is the trust root: rotating a key adds a line, revoking one deletes it. This module reads
that file strictly: an option it does not implement (such as `valid-before`) is refused
rather than ignored; use ssh-keygen for those.

A release folder is signed as a whole: SHA256SUMS lists every file, and every listed file
and SHA256SUMS itself get a `.sig`. `verify_dir` refuses a changed, missing, unsigned or
unlisted file.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
import struct
import textwrap
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

NAMESPACE = "agentic-release"
SUMS = "SHA256SUMS"
SIG_SUFFIX = ".sig"
_MAGIC = b"SSHSIG"
_VERSION = 1
_KEY_TYPE = b"ssh-ed25519"
_HASHES = {"sha512": hashlib.sha512, "sha256": hashlib.sha256}
_BEGIN, _END = "-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----"
_KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384"}
_KEY_TYPES |= {"ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com"}


class SignatureError(ValueError):
    """A key, signature or release folder that does not check out."""


# --- the wire format ---


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _read_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise SignatureError("truncated signature")
    (size,) = struct.unpack(">I", blob[offset : offset + 4])
    end = offset + 4 + size
    if end > len(blob):
        raise SignatureError("truncated signature")
    return blob[offset + 4 : end], end


def _public_blob(key: Ed25519PublicKey) -> bytes:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _string(_KEY_TYPE) + _string(raw)


def _signed_data(namespace: str, hash_name: str, digest: bytes) -> bytes:
    return (
        _MAGIC
        + _string(namespace.encode())
        + _string(b"")
        + _string(hash_name.encode())
        + _string(digest)
    )


# --- keys ---


def load_private_key(path: Path, password: bytes | None = None) -> Ed25519PrivateKey:
    """The org's release key: an OpenSSH private key file holding an Ed25519 key."""
    try:
        key = serialization.load_ssh_private_key(Path(path).read_bytes(), password)
    except (ValueError, TypeError) as error:
        raise SignatureError(f"{path}: not a readable OpenSSH private key ({error})") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError(f"{path}: release keys are Ed25519 (ssh-keygen -t ed25519)")
    return key


def parse_allowed(text: str) -> dict[str, list[tuple[bytes, set[str] | None]]]:
    """An allowed_signers file: principal -> [(public key blob, namespaces or None)]."""
    signers: dict[str, list[tuple[bytes, set[str] | None]]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words = shlex.split(line, posix=True)
        if len(words) < 3:
            raise SignatureError(f"allowed_signers line {number}: too few fields")
        principals, rest = words[0], words[1:]
        namespaces = None
        if rest[0] not in _KEY_TYPES:
            for option in rest.pop(0).split(","):
                name, _, value = option.partition("=")
                if name.lower() != "namespaces":
                    raise SignatureError(
                        f"allowed_signers line {number}: option {name!r} is not supported "
                        "here; check such signatures with ssh-keygen -Y verify"
                    )
                namespaces = {n.strip() for n in value.strip('"').split(",") if n.strip()}
        if len(rest) < 2 or rest[0] not in _KEY_TYPES:
            raise SignatureError(f"allowed_signers line {number}: no key type and key")
        try:
            blob = base64.b64decode(rest[1], validate=True)
        except ValueError as error:
            raise SignatureError(f"allowed_signers line {number}: bad key") from error
        for principal in principals.split(","):
            signers.setdefault(principal, []).append((blob, namespaces))
    return signers


# --- one signature ---


def sign_bytes(key: Ed25519PrivateKey, data: bytes, namespace: str = NAMESPACE) -> str:
    """An armored SSH signature of `data` (what `ssh-keygen -Y sign` writes)."""
    digest = hashlib.sha512(data).digest()
    signature = key.sign(_signed_data(namespace, "sha512", digest))
    blob = (
        _MAGIC
        + struct.pack(">I", _VERSION)
        + _string(_public_blob(key.public_key()))
        + _string(namespace.encode())
        + _string(b"")
        + _string(b"sha512")
        + _string(_string(_KEY_TYPE) + _string(signature))
    )
    body = "\n".join(textwrap.wrap(base64.b64encode(blob).decode(), 70))
    return f"{_BEGIN}\n{body}\n{_END}\n"


def verify_bytes(
    data: bytes,
    armored: str,
    allowed: dict[str, list[tuple[bytes, set[str] | None]]],
    identity: str,
    namespace: str = NAMESPACE,
) -> None:
    """Raise SignatureError unless `armored` is a good signature of `data`, in
    `namespace`, by a key `allowed` lists for `identity`."""
    text = armored.replace("\r\n", "\n").strip()
    if not (text.startswith(_BEGIN) and text.endswith(_END)):
        raise SignatureError("not an SSH signature (no BEGIN/END SSH SIGNATURE lines)")
    try:
        blob = base64.b64decode("".join(text[len(_BEGIN) : -len(_END)].split()), validate=True)
    except ValueError as error:
        raise SignatureError("the signature is not valid base64") from error
    if not blob.startswith(_MAGIC) or len(blob) < 10:
        raise SignatureError("not an SSH signature (no SSHSIG preamble)")
    (version,) = struct.unpack(">I", blob[6:10])
    if version != _VERSION:
        raise SignatureError(f"SSH signature version {version} is not supported")
    public, at = _read_string(blob, 10)
    signed_ns, at = _read_string(blob, at)
    _reserved, at = _read_string(blob, at)
    hash_name, at = _read_string(blob, at)
    wrapped, at = _read_string(blob, at)
    if signed_ns.decode(errors="replace") != namespace:
        raise SignatureError(
            f"signed for namespace {signed_ns.decode(errors='replace')!r}, not {namespace!r}"
        )
    keys = [blob_ for blob_, spaces in allowed.get(identity, ()) if blob_ == public]
    if not keys:
        raise SignatureError(f"no allowed key for {identity} made this signature")
    if all(
        spaces is not None and namespace not in spaces
        for blob_, spaces in allowed.get(identity, ())
        if blob_ == public
    ):
        raise SignatureError(f"the key of {identity} is not allowed the namespace {namespace}")
    key_type, rest = _read_string(public, 0)
    raw, _ = _read_string(public, rest)
    sig_type, rest = _read_string(wrapped, 0)
    signature, _ = _read_string(wrapped, rest)
    if key_type != _KEY_TYPE or sig_type != _KEY_TYPE:
        raise SignatureError("only Ed25519 release signatures are supported")
    hasher = _HASHES.get(hash_name.decode(errors="replace"))
    if hasher is None:
        raise SignatureError(f"hash {hash_name!r} is not supported")
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(
            signature, _signed_data(namespace, hash_name.decode(), hasher(data).digest())
        )
    except (InvalidSignature, ValueError) as error:
        raise SignatureError("the signature does not verify: the file changed") from error


# --- a release folder ---


def _release_files(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and not p.name.endswith(SIG_SUFFIX)),
        key=lambda p: p.name,
    )


def write_sums(folder: Path) -> Path:
    """SHA256SUMS for every file in `folder` (`sha256sum -c` reads it)."""
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
        for p in _release_files(folder)
        if p.name != SUMS
    ]
    sums = folder / SUMS
    sums.write_text("".join(lines), encoding="utf-8", newline="\n")
    return sums


def sign_dir(folder: Path, key: Ed25519PrivateKey) -> list[Path]:
    """Write SHA256SUMS, then a `.sig` for it and every file it lists."""
    write_sums(folder)
    written = []
    for path in _release_files(folder):
        sig = path.with_name(path.name + SIG_SUFFIX)
        sig.write_text(sign_bytes(key, path.read_bytes()), encoding="utf-8", newline="\n")
        written.append(sig)
    return written


_SUM_LINE = re.compile(r"^([0-9a-f]{64})  ([^/\\]+)$")


def verify_dir(
    folder: Path, allowed: dict[str, list[tuple[bytes, set[str] | None]]], identity: str
) -> list[str]:
    """The names of the verified release files; SignatureError listing every problem
    when any file is changed, unsigned, unlisted or missing."""
    sums = folder / SUMS
    sums_sig = folder / (SUMS + SIG_SUFFIX)
    if not sums.is_file() or not sums_sig.is_file():
        raise SignatureError(f"{folder}: no {SUMS} and {SUMS}{SIG_SUFFIX}: not a signed release")
    verify_bytes(sums.read_bytes(), sums_sig.read_text(encoding="utf-8"), allowed, identity)
    listed: dict[str, str] = {}
    problems = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        found = _SUM_LINE.match(line)
        if found is None:
            problems.append(f"{SUMS}: unreadable line {line!r}")
            continue
        listed[found[2]] = found[1]
    present = {p.name for p in _release_files(folder)} - {SUMS}
    for name in sorted(present - set(listed)):
        problems.append(f"{name}: not listed in {SUMS} (added after signing?)")
    for name, digest in sorted(listed.items()):
        path = folder / name
        if not path.is_file():
            problems.append(f"{name}: listed in {SUMS} but missing")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name}: its sha256 differs from {SUMS}")
        sig = folder / (name + SIG_SUFFIX)
        if not sig.is_file():
            problems.append(f"{name}: no {SIG_SUFFIX} file")
            continue
        try:
            verify_bytes(path.read_bytes(), sig.read_text(encoding="utf-8"), allowed, identity)
        except SignatureError as error:
            problems.append(f"{name}: {error}")
    if problems:
        raise SignatureError("the release does not verify:\n  " + "\n  ".join(problems))
    return sorted(listed)
