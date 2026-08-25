from pathlib import Path


ZERO_OID = "0" * 40


def canonical_repo(root: Path) -> Path:
    return root / "canonical.git"


def view_repo(root: Path, name: str) -> Path:
    return root / "views" / f"{name}.git"


def intake_repo(root: Path, name: str) -> Path:
    return root / "intake" / f"{name}.git"


def repository_config(root: Path) -> Path:
    return root / "repository.json"


def admin_staging_repo(root: Path) -> Path:
    return root / "admin-staging.git"


def materializations_dir(root: Path) -> Path:
    return root / "materializations"


def materialization_lock(root: Path, view: str | None = None) -> Path:
    if view is None:
        return root / "materialization-locks" / "canonical.lock"
    return root / "materialization-locks" / "views" / f"{view}.lock"
