"""Safe, stable failures shared by the upstream client and HTTP API."""


class EtlabError(Exception):
    def __init__(self, code: str, message: str, status: int = 502) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
