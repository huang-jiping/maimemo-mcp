"""Safe error categories; never retain upstream response or request objects."""


class AuthenticationError(Exception):
    """The API credential was rejected."""


class RateLimitError(Exception):
    """The upstream retry budget was exhausted by rate limiting."""


class UpstreamUnavailableError(Exception):
    """Network or upstream failure exhausted the retry budget."""


class UpstreamSchemaError(Exception):
    """A successful response did not meet the required schema."""


class InvalidRequestError(Exception):
    """The request was invalid locally or rejected upstream."""
