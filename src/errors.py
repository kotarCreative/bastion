class ForgeError(Exception):
    """A stable, client-safe failure raised by the experimental forge."""

    def __init__(self, message: str, category: str = "policy"):
        super().__init__(message)
        self.category = category
