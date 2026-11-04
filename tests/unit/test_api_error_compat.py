from src.api.response import APIError as ApiResponseAPIError
from src.core.exceptions import APIError as CoreAPIError


def test_api_error_alias_is_compatible() -> None:
    assert ApiResponseAPIError is CoreAPIError

