from __future__ import annotations

from types import SimpleNamespace

from src.core.dependencies import get_user_service
from src.services.user_service import UserService


def test_get_user_service_matches_user_service_constructor() -> None:
    user_repo = SimpleNamespace()
    character_repo = SimpleNamespace()

    service = get_user_service(user_repo, character_repo)

    assert isinstance(service, UserService)
    assert service._user_repo is user_repo
    assert service._character_repo is character_repo
