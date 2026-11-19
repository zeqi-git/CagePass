"""Security guardrails for mount validation, VCS protection, and file staging.

Inspired by Claude-Cage's internal/mounts/safety.go:
- Resolves physical paths (realpath) and case-folds for macOS APFS safety.
- Blocks mounting /, $HOME (or any ancestor), ~/.ssh, ~/.claude, system dirs,
  and cagepass's own root/.state directory.
- Detects VCS metadata dirs (.git, .hg, .sl, .jj) inside :rw mounts so they
  can be overlaid as :ro (preventing host git-hook RCE).
- Audits files staged via `cagepass put` for escaping symlinks and secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

PROTECTED_VCS_NAMES = (".git", ".hg", ".sl", ".jj")

SYSTEM_FORBIDDEN_DIRS = (
    "/etc",
    "/private/etc",
    "/var/run",
    "/var/db",
    "/var/root",
    "/var/log",
    "/var/lib",
    "/private/var/run",
    "/private/var/db",
    "/private/var/root",
    "/private/var/log",
    "/System",
    "/Library",
    "/usr",
    "/bin",
    "/sbin",
    "/dev",
    "/proc",
)

SYSTEM_EXACT_FORBIDDEN = ("/var", "/private/var", "/private")

SENSITIVE_FILE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "authorized_keys",
    "credentials",
}

SENSITIVE_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
)


class SafetyError(ValueError):
    """Raised when a mount path or staged file violates isolation rules."""


def resolve_real_fold(path: str) -> str:
    """Resolve symlinks (realpath) and case-fold for case-insensitive filesystems."""
    expanded = os.path.expanduser(path)
    real = os.path.realpath(expanded)
    folded = real.casefold()
    if folded != "/":
        folded = folded.rstrip("/")
    return folded


def _is_same_or_inside(child_folded: str, parent_folded: str) -> bool:
    if parent_folded == "/":
        return True
    return child_folded == parent_folded or child_folded.startswith(parent_folded + "/")


def _contains_or_same(outer_folded: str, inner_folded: str) -> bool:
    return _is_same_or_inside(inner_folded, outer_folded)


def validate_mount_path(
    path: str,
    project_root: str,
    home_dir: Optional[str] = None,
) -> str:
    """Validate that `path` is safe to bind-mount into local-cage.

    Returns the resolved realpath if safe; otherwise raises SafetyError.
    """
    if not path or not str(path).strip():
        raise SafetyError("Mount path cannot be empty.")

    expanded = os.path.expanduser(path)
    real = os.path.realpath(expanded)
    folded = resolve_real_fold(expanded)

    if folded == "/":
        raise SafetyError("Refusing to mount filesystem root '/'.")

    home_folded = resolve_real_fold(home_dir or os.path.expanduser("~"))
    repo_folded = resolve_real_fold(project_root)
    state_folded = resolve_real_fold(os.path.join(project_root, ".state"))

    # 1. Must not be $HOME itself or any ancestor containing $HOME
    if _contains_or_same(folded, home_folded):
        raise SafetyError(
            f"Refusing to mount '{path}' (resolves to '{real}'): "
            "it is or contains your home directory."
        )

    # 2. Must not be the cagepass repo root itself (contains scripts/.state)
    #    Subdirectories like vault/ are allowed, but .state/ is forbidden.
    if folded == repo_folded or _contains_or_same(folded, state_folded) or _is_same_or_inside(folded, state_folded):
        raise SafetyError(
            f"Refusing to mount '{path}' (resolves to '{real}'): "
            "it exposes cagepass's own root or .state/ directory."
        )

    # 3. Forbidden sensitive dotfiles under $HOME
    for rel_bad in (".ssh", ".claude", ".gnupg", ".aws", ".config/gcloud", ".kube"):
        bad_folded = resolve_real_fold(os.path.join(home_dir or os.path.expanduser("~"), rel_bad))
        if _is_same_or_inside(folded, bad_folded) or _contains_or_same(folded, bad_folded):
            raise SafetyError(
                f"Refusing to mount '{path}' (resolves to '{real}'): "
                f"overlaps sensitive directory '~/{rel_bad}'."
            )

    # 4. Forbidden OS system directories
    for exact_bad in SYSTEM_EXACT_FORBIDDEN:
        if folded == resolve_real_fold(exact_bad):
            raise SafetyError(
                f"Refusing to mount '{path}' (resolves to '{real}'): "
                f"forbidden system directory '{exact_bad}'."
            )
    for sys_dir in SYSTEM_FORBIDDEN_DIRS:
        sys_folded = resolve_real_fold(sys_dir)
        if _is_same_or_inside(folded, sys_folded):
            raise SafetyError(
                f"Refusing to mount '{path}' (resolves to '{real}'): "
                f"inside forbidden system directory '{sys_dir}'."
            )

    return real


def find_protected_vcs_dirs(
    root: str,
    names: Sequence[str] = PROTECTED_VCS_NAMES,
) -> List[str]:
    """Find VCS metadata directories (.git, .hg, .sl, .jj) under `root`.

    Matching is case-insensitive to protect macOS case-insensitive filesystems.
    Does not recurse inside matched VCS directories.
    """
    root_path = Path(os.path.realpath(root))
    if not root_path.is_dir():
        return []

    lower_names = {n.casefold() for n in names}
    matches: List[str] = []

    for dirpath, dirnames, _ in os.walk(str(root_path), followlinks=False):
        keep: List[str] = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d.casefold() in lower_names:
                matches.append(full)
            else:
                keep.append(d)
        dirnames[:] = keep

    matches.sort()
    return matches


@dataclass
class PutAuditResult:
    escaping_symlinks: List[str] = field(default_factory=list)
    sensitive_files: List[str] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return bool(self.escaping_symlinks)

    @property
    def has_warnings(self) -> bool:
        return bool(self.sensitive_files)


def _is_sensitive_filename(name: str) -> bool:
    low = name.casefold()
    if low in SENSITIVE_FILE_NAMES or low.startswith(".env."):
        return True
    return any(low.endswith(suf) for suf in SENSITIVE_FILE_SUFFIXES)


def inspect_put_source(src: str) -> PutAuditResult:
    """Inspect a file or directory before staging it into vault/."""
    audit = PutAuditResult()
    src_path = Path(os.path.expanduser(src))
    if not src_path.exists() and not src_path.is_symlink():
        raise SafetyError(f"Source path does not exist: {src}")

    if src_path.is_symlink():
        audit.escaping_symlinks.append(str(src_path))
        return audit

    root_folded = resolve_real_fold(str(src_path))

    if src_path.is_file():
        if _is_sensitive_filename(src_path.name):
            audit.sensitive_files.append(str(src_path))
        return audit

    for dirpath, dirnames, filenames in os.walk(str(src_path), followlinks=False):
        for entry in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, entry)
            if os.path.islink(full):
                target_folded = resolve_real_fold(full)
                if not _is_same_or_inside(target_folded, root_folded):
                    audit.escaping_symlinks.append(full)
            elif entry in filenames and _is_sensitive_filename(entry):
                audit.sensitive_files.append(full)

    return audit
