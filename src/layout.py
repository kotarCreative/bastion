from pathlib import Path


ZERO_OID = "0" * 40


def canonical_repo(root: Path) -> Path:
    return root / "canonical.git"


def view_repo(root: Path, name: str) -> Path:
    return root / "views" / f"{name}.git"
