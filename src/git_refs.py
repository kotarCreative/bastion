"""Pure Git ref validation shared by durable policy and WAL code."""
from errors import ForgeError


def validate_branch_ref(value: object) -> str:
    ref = value
    if not isinstance(ref, str) or not ref:
        raise ForgeError("branch ref must be a nonempty string")
    try:
        ref.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ForgeError("branch ref is not valid UTF-8") from error
    if (not ref.startswith("refs/heads/") or ref.endswith(("/", "."))
        or ".." in ref or "@{" in ref or "\\" in ref or "//" in ref
        or ref == "@" or any(ord(c) < 32 or ord(c) == 127 or c in " ~^:?*[" for c in ref)):
        raise ForgeError("branch ref is not a valid Git branch ref")
    if any(not part or part.startswith(".") or part.endswith(".lock") for part in ref.split("/")):
        raise ForgeError("branch ref is not a valid Git branch ref")
    return ref
