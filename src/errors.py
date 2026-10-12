class ForgeError(Exception):
    """A stable, client-safe failure raised by the experimental forge.

    Messages are operator-facing diagnostics that may name repository paths,
    object ids, or subprocess details, so they are relayed verbatim only to
    local clients. ``operator_detail`` marks the failures whose text is built
    from raw process output (see :func:`git.run`) rather than written by hand
    as a client-safe explanation; those are additionally withheld from remote
    clients even when a transport looks local.
    """

    def __init__(
        self,
        message: str,
        category: str = "policy",
        operator_detail: bool = False,
    ):
        super().__init__(message)
        self.category = category
        self.operator_detail = operator_detail
