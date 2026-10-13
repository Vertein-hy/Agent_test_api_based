"""Closed diagnostic categories; exception text never contains provider/user content."""


class StreamError(ValueError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class HTTPError(RuntimeError):
    def __init__(self, status):
        self.code = "http_" + str(status)
        self.retryable = status in {408, 500, 502, 503, 504}
        super().__init__(self.code)
