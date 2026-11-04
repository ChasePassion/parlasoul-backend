"""Centralized dependency providers for FastAPI layers."""

from __future__ import annotations

import logging
import time
import uuid
from functools import lru_cache

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClientError
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.jwks import verify_better_auth_jwt
from src.core.llm_registry import LLMRegistry, llm_registry
from src.core.llm_settings import llm_settings
from src.core.logging_context import set_request_user_id
from src.core.tts_registry import TTSRegistry, tts_registry
from src.core.uow import UnitOfWork
from src.db.models import User
from src.db.session import get_db
from src.memory_system import Memory, MemoryConfig
from src.realtime.services.realtime_dialog_service import RealtimeDialogService
from src.realtime.services.realtime_ice_config_service import RealtimeIceConfigService
from src.realtime.services.realtime_turn_service import RealtimeTurnService
from src.realtime.services.realtime_voice_runtime import RealtimeVoiceRuntime
from src.realtime.session.manager import RealtimeSessionManager
from src.realtime.vad.silero import SileroVADModel
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.repositories.growth_repository import GrowthRepository
from src.repositories.llm_gateway_repository import LLMGatewayRepository
from src.repositories.memory_repository import MemoryRepository
from src.repositories.payment_order_repository import PaymentOrderRepository
from src.repositories.payment_webhook_event_repository import PaymentWebhookEventRepository
from src.repositories.proactive_character_preference_repository import (
    ProactiveCharacterPreferenceRepository,
)
from src.repositories.proactive_dispatch_repository import ProactiveDispatchRepository
from src.repositories.saved_item_repository import SavedItemRepository
from src.repositories.subscription_webhook_event_repository import (
    SubscriptionWebhookEventRepository,
)
from src.repositories.turn_repository import TurnRepository
from src.repositories.user_access_pass_repository import UserAccessPassRepository
from src.repositories.user_repository import UserRepository
from src.repositories.user_settings_repository import UserSettingsRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.repositories.voice_profile_repository import VoiceProfileRepository
from src.services.character_llm_route_service import CharacterLLMRouteService
from src.services.character_llm_service import CharacterLLMService
from src.services.chat_title_service import ChatTitleService
from src.services.discover_config_service import DiscoverConfigService
from src.services.growth_service import GrowthService
from src.services.image_processing_service import (
    ImageProcessingService,
    get_image_processing_service,
)
from src.services.llm_gateway_service import LLMGatewayService
from src.services.media_cache_service import MediaCacheService, get_media_cache_service
from src.services.proactive_preference_service import ProactivePreferenceService
from src.services.r2_storage_service import R2StorageService, get_r2_storage_service
from src.services.realtime_telemetry_service import RealtimeTelemetryService
from src.services.tts_gateway_service import TTSGatewayService
from src.repositories.realtime_telemetry_repository import RealtimeTelemetryRepository

logger = logging.getLogger(__name__)


# We use a simple Bearer token scheme because login uses email+code flow,
# not OAuth2 password grant.
bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def get_llm_registry() -> LLMRegistry:
    return llm_registry


@lru_cache
def get_llm_gateway_service() -> LLMGatewayService:
    return LLMGatewayService()


@lru_cache
def get_tts_registry() -> TTSRegistry:
    return tts_registry


@lru_cache
def get_memory_system() -> Memory:
    """Get singleton Memory instance."""
    retries = max(1, int(settings.MEMORY_INIT_MAX_RETRIES))
    base_delay = max(0.1, float(settings.MEMORY_INIT_RETRY_BASE_SECONDS))
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            logger.info(
                "Initializing Memory system singleton (attempt %s/%s)...",
                attempt,
                retries,
            )
            registry = get_llm_registry()
            route = registry.resolve_explicit_route(
                provider="anthropic",
                model=llm_settings.anthropic_model_id,
                source="system_memory",
            )
            if route is None:
                raise RuntimeError("anthropic memory llm route is not configured")
            config = MemoryConfig(llm_route=route)
            memory = Memory(
                config,
                gateway_service=get_llm_gateway_service(),
                llm_registry=registry,
            )
            logger.info("Memory system initialized with collection: %s", config.collection_name)
            return memory
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            delay_s = min(base_delay * (2 ** (attempt - 1)), 8.0)
            logger.warning(
                "Memory initialization attempt %s/%s failed, retrying in %.1fs: %s",
                attempt,
                retries,
                delay_s,
                exc,
            )
            time.sleep(delay_s)

    assert last_exc is not None
    raise last_exc


async def _bind_db_user_context(db: AsyncSession, user_id: str) -> None:
    """Bind user context for current transaction and future transactions."""
    db.info["current_user_id"] = user_id
    await db.execute(
        text("SELECT set_config('app.current_user_id', :user_id, true)"),
        {"user_id": user_id},
    )


def get_user_repository(db: AsyncSession = Depends(get_db)) -> UserRepository:
    """Centralized provider for UserRepository."""
    return UserRepository(db)


async def _resolve_authenticated_user(
    token: str,
    *,
    db: AsyncSession,
    user_repo: UserRepository,
) -> User:
    credentials_exception = APIError(
        status_code=401,
        code="auth_token_invalid",
        message="authorization failed: token is missing or invalid",
    )

    token_preview = token[:10] + "..." if len(token) > 10 else "(empty)"

    try:
        payload = await verify_better_auth_jwt(
            token,
            jwks_url=settings.BETTER_AUTH_JWKS_URL,
            issuer=settings.BETTER_AUTH_JWT_ISSUER,
            audience=settings.BETTER_AUTH_JWT_AUDIENCE,
        )
        user_id = uuid.UUID(str(payload["sub"]))
        logger.info(
            "JWT verified successfully: user_id=%s token_preview=%s",
            user_id,
            token_preview,
        )
    except (InvalidTokenError, PyJWKClientError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "JWT verification failed: token_preview=%s error_type=%s error=%s",
            token_preview,
            type(exc).__name__,
            str(exc),
        )
        raise credentials_exception

    user = await user_repo.get_by_id(user_id)
    if user is None:
        logger.warning(
            "User not found after JWT verification: user_id=%s token_preview=%s",
            user_id,
            token_preview,
        )
        raise credentials_exception

    await _bind_db_user_context(db, str(user.id))
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    user_repo: UserRepository = Depends(get_user_repository),
) -> User:
    if credentials is None:
        logger.warning(
            "Authorization header missing or invalid: token not provided in request",
        )
        raise APIError(
            status_code=401,
            code="auth_token_invalid",
            message="authorization failed: token is missing or invalid",
        )

    user = await _resolve_authenticated_user(
        credentials.credentials,
        db=db,
        user_repo=user_repo,
    )
    set_request_user_id(str(user.id))
    return user


async def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    user_repo: UserRepository = Depends(get_user_repository),
) -> User | None:
    """Get current user if authenticated, otherwise None."""
    if credentials is None:
        return None

    user = await _resolve_authenticated_user(
        credentials.credentials,
        db=db,
        user_repo=user_repo,
    )
    set_request_user_id(str(user.id))
    return user


def get_r2_storage_service_provider() -> R2StorageService:
    return get_r2_storage_service()


def get_media_cache_service_provider() -> MediaCacheService:
    return get_media_cache_service()


def get_image_processing_service_provider() -> ImageProcessingService:
    return get_image_processing_service()


def get_user_settings_repository(db: AsyncSession = Depends(get_db)) -> UserSettingsRepository:
    """Centralized provider for UserSettingsRepository."""
    return UserSettingsRepository(db)


def get_subscription_webhook_event_repository(
    db: AsyncSession = Depends(get_db),
) -> SubscriptionWebhookEventRepository:
    return SubscriptionWebhookEventRepository(db)


def get_payment_order_repository(db: AsyncSession = Depends(get_db)) -> PaymentOrderRepository:
    return PaymentOrderRepository(db)


def get_payment_webhook_event_repository(
    db: AsyncSession = Depends(get_db),
) -> PaymentWebhookEventRepository:
    return PaymentWebhookEventRepository(db)


def get_proactive_character_preference_repository(
    db: AsyncSession = Depends(get_db),
) -> ProactiveCharacterPreferenceRepository:
    return ProactiveCharacterPreferenceRepository(db)


def get_proactive_dispatch_repository(
    db: AsyncSession = Depends(get_db),
) -> ProactiveDispatchRepository:
    return ProactiveDispatchRepository(db)


def get_user_access_pass_repository(
    db: AsyncSession = Depends(get_db),
) -> UserAccessPassRepository:
    return UserAccessPassRepository(db)


def get_chat_repository(db: AsyncSession = Depends(get_db)) -> ChatRepository:
    """Centralized provider for ChatRepository."""
    return ChatRepository(db)


def get_growth_repository(db: AsyncSession = Depends(get_db)) -> GrowthRepository:
    """Centralized provider for GrowthRepository."""
    return GrowthRepository(db)


def get_turn_repository(db: AsyncSession = Depends(get_db)) -> TurnRepository:
    """Centralized provider for TurnRepository."""
    return TurnRepository(db)


def get_candidate_repository(db: AsyncSession = Depends(get_db)) -> CandidateRepository:
    """Centralized provider for CandidateRepository."""
    return CandidateRepository(db)


def get_memory_repository() -> MemoryRepository:
    """Centralized provider for lazily initialized MemoryRepository."""
    return MemoryRepository(memory_factory=get_memory_system)


def get_character_repository(db: AsyncSession = Depends(get_db)) -> CharacterRepository:
    """Centralized provider for CharacterRepository."""
    return CharacterRepository(db)


def get_saved_item_repository(db: AsyncSession = Depends(get_db)) -> SavedItemRepository:
    """Centralized provider for SavedItemRepository."""
    return SavedItemRepository(db)


def get_voice_gateway_repository() -> VoiceGatewayRepository:
    """Centralized provider for voice upstream gateway repository."""
    return VoiceGatewayRepository()


@lru_cache
def get_shared_tts_gateway_service() -> TTSGatewayService:
    """Process-level TTS gateway used by normal chat/message TTS paths."""
    voice_gateway_repo = get_voice_gateway_repository()
    return TTSGatewayService(
        registry=get_tts_registry(),
        dashscope_gateway=voice_gateway_repo,
    )


def get_tts_gateway_service() -> TTSGatewayService:
    """Centralized provider for provider-aware TTS gateway service."""
    return get_shared_tts_gateway_service()


def get_voice_profile_repository(db: AsyncSession = Depends(get_db)) -> VoiceProfileRepository:
    """Centralized provider for VoiceProfileRepository."""
    return VoiceProfileRepository(db)


def get_uow(db: AsyncSession = Depends(get_db)) -> UnitOfWork:
    """Centralized provider for UnitOfWork."""
    return UnitOfWork(db)


def get_subscription_service(
    user_repo: UserRepository = Depends(get_user_repository),
    webhook_event_repo: SubscriptionWebhookEventRepository = Depends(
        get_subscription_webhook_event_repository
    ),
    user_access_pass_repo: UserAccessPassRepository = Depends(get_user_access_pass_repository),
    uow: UnitOfWork = Depends(get_uow),
):
    from src.services.subscription_service import SubscriptionService

    return SubscriptionService(
        user_repo=user_repo,
        webhook_event_repo=webhook_event_repo,
        user_access_pass_repo=user_access_pass_repo,
        uow=uow,
    )


def get_wechat_payment_service(
    order_repo: PaymentOrderRepository = Depends(get_payment_order_repository),
    webhook_event_repo: PaymentWebhookEventRepository = Depends(
        get_payment_webhook_event_repository
    ),
    user_access_pass_repo: UserAccessPassRepository = Depends(get_user_access_pass_repository),
    user_repo: UserRepository = Depends(get_user_repository),
    subscription_service=Depends(get_subscription_service),
    uow: UnitOfWork = Depends(get_uow),
):
    from src.services.wechat_payment_service import WechatPaymentService

    return WechatPaymentService(
        order_repo=order_repo,
        webhook_event_repo=webhook_event_repo,
        user_access_pass_repo=user_access_pass_repo,
        user_repo=user_repo,
        subscription_service=subscription_service,
        uow=uow,
    )


async def require_voice_clone_access(
    current_user: User = Depends(get_current_user),
    subscription_service=Depends(get_subscription_service),
) -> User:
    await subscription_service.assert_feature(current_user, "voice_clone")
    return current_user


def get_discover_config_service() -> DiscoverConfigService:
    """Centralized provider for discover config service."""
    return DiscoverConfigService(settings)


def get_character_llm_route_service(
    registry: LLMRegistry = Depends(get_llm_registry),
) -> CharacterLLMRouteService:
    """Centralized provider for character LLM route service."""
    return CharacterLLMRouteService(registry=registry)


def get_llm_gateway_repository(
    gateway_service: LLMGatewayService = Depends(get_llm_gateway_service),
) -> LLMGatewayRepository:
    """Centralized provider for runtime LLM gateway repository."""
    return LLMGatewayRepository(gateway_service=gateway_service)


def get_character_llm_service(
    route_service: CharacterLLMRouteService = Depends(get_character_llm_route_service),
    gateway_repo: LLMGatewayRepository = Depends(get_llm_gateway_repository),
) -> CharacterLLMService:
    """Centralized provider for character reply LLM service."""
    return CharacterLLMService(route_service=route_service, gateway_repo=gateway_repo)


def get_voice_service(
    voice_gateway_repo: VoiceGatewayRepository = Depends(get_voice_gateway_repository),
    tts_gateway_service: TTSGatewayService = Depends(get_tts_gateway_service),
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    character_repo: CharacterRepository = Depends(get_character_repository),
):
    """Centralized provider for VoiceService."""
    from src.services.voice_service import VoiceService

    return VoiceService(
        voice_gateway_repo=voice_gateway_repo,
        candidate_repo=candidate_repo,
        character_repo=character_repo,
        tts_gateway_service=tts_gateway_service,
    )


def get_chat_learning_service(
    gateway_service: LLMGatewayService = Depends(get_llm_gateway_service),
    registry: LLMRegistry = Depends(get_llm_registry),
    saved_item_repo: SavedItemRepository = Depends(get_saved_item_repository),
):
    """Centralized provider for ChatLearningService."""
    from src.services.chat_learning_service import ChatLearningService

    return ChatLearningService(gateway_service, registry=registry, saved_item_repo=saved_item_repo)


def get_chat_title_service(
    gateway_service: LLMGatewayService = Depends(get_llm_gateway_service),
    registry: LLMRegistry = Depends(get_llm_registry),
) -> ChatTitleService:
    """Centralized provider for ChatTitleService."""
    return ChatTitleService(gateway_service=gateway_service, registry=registry)


def get_growth_service(
    uow: UnitOfWork = Depends(get_uow),
    growth_repo: GrowthRepository = Depends(get_growth_repository),
    chat_repo: ChatRepository = Depends(get_chat_repository),
    turn_repo: TurnRepository = Depends(get_turn_repository),
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    character_repo: CharacterRepository = Depends(get_character_repository),
) -> GrowthService:
    """Centralized provider for GrowthService."""
    return GrowthService(
        uow=uow,
        growth_repo=growth_repo,
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=character_repo,
    )


def get_user_service(
    repo: UserRepository = Depends(get_user_repository),
    character_repo: CharacterRepository = Depends(get_character_repository),
):
    """Centralized provider for UserService."""
    from src.services.user_service import UserService

    return UserService(repo, character_repo)


def get_user_settings_service(
    repo: UserSettingsRepository = Depends(get_user_settings_repository),
    uow: UnitOfWork = Depends(get_uow),
    subscription_service=Depends(get_subscription_service),
):
    """Centralized provider for UserSettingsService."""
    from src.services.user_settings_service import UserSettingsService

    return UserSettingsService(repo, uow, subscription_service)


def get_proactive_preference_service(
    preference_repo: ProactiveCharacterPreferenceRepository = Depends(
        get_proactive_character_preference_repository
    ),
    uow: UnitOfWork = Depends(get_uow),
) -> ProactivePreferenceService:
    return ProactivePreferenceService(preference_repo, uow)


def get_upload_service(
    r2_storage: R2StorageService = Depends(get_r2_storage_service_provider),
    media_cache: MediaCacheService = Depends(get_media_cache_service_provider),
    image_processor: ImageProcessingService = Depends(get_image_processing_service_provider),
):
    """Centralized provider for UploadService."""
    from src.services.upload_service import UploadService

    return UploadService(
        r2_storage=r2_storage,
        media_cache=media_cache,
        image_processor=image_processor,
    )


async def get_chat_service(
    uow: UnitOfWork = Depends(get_uow),
    chat_repo: ChatRepository = Depends(get_chat_repository),
    turn_repo: TurnRepository = Depends(get_turn_repository),
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    character_repo: CharacterRepository = Depends(get_character_repository),
    memory_repo: MemoryRepository = Depends(get_memory_repository),
    user_settings_repo: UserSettingsRepository = Depends(get_user_settings_repository),
    chat_learning_service=Depends(get_chat_learning_service),
    chat_title_service: ChatTitleService = Depends(get_chat_title_service),
    growth_service: GrowthService = Depends(get_growth_service),
    voice_service=Depends(get_voice_service),
    character_llm_service: CharacterLLMService = Depends(get_character_llm_service),
    subscription_service=Depends(get_subscription_service),
):
    """Centralized provider for ChatService."""
    from src.services.chat_service import ChatService

    return ChatService(
        uow=uow,
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=character_repo,
        memory_repo=memory_repo,
        user_settings_repo=user_settings_repo,
        chat_learning_service=chat_learning_service,
        chat_title_service=chat_title_service,
        growth_service=growth_service,
        voice_service=voice_service,
        character_llm_service=character_llm_service,
        subscription_service=subscription_service,
    )


async def get_turn_service(
    uow: UnitOfWork = Depends(get_uow),
    chat_repo: ChatRepository = Depends(get_chat_repository),
    turn_repo: TurnRepository = Depends(get_turn_repository),
    candidate_repo: CandidateRepository = Depends(get_candidate_repository),
    character_repo: CharacterRepository = Depends(get_character_repository),
    memory_repo: MemoryRepository = Depends(get_memory_repository),
    user_settings_repo: UserSettingsRepository = Depends(get_user_settings_repository),
    chat_learning_service=Depends(get_chat_learning_service),
    voice_service=Depends(get_voice_service),
    character_llm_service: CharacterLLMService = Depends(get_character_llm_service),
    subscription_service=Depends(get_subscription_service),
):
    """Centralized provider for TurnService."""
    from src.services.turn_service import TurnService

    return TurnService(
        uow=uow,
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=character_repo,
        memory_repo=memory_repo,
        user_settings_repo=user_settings_repo,
        chat_learning_service=chat_learning_service,
        voice_service=voice_service,
        character_llm_service=character_llm_service,
        subscription_service=subscription_service,
    )


def get_character_service(
    repo: CharacterRepository = Depends(get_character_repository),
    voice_profile_repo: VoiceProfileRepository = Depends(get_voice_profile_repository),
    voice_gateway_repo: VoiceGatewayRepository = Depends(get_voice_gateway_repository),
    tts_registry: TTSRegistry = Depends(get_tts_registry),
    subscription_service=Depends(get_subscription_service),
):
    """Centralized provider for CharacterService."""
    from src.services.character_service import CharacterService

    return CharacterService(
        repo,
        voice_profile_repo,
        voice_gateway_repo,
        tts_registry=tts_registry,
        subscription_service=subscription_service,
    )


def get_voice_profile_service(
    repo: VoiceProfileRepository = Depends(get_voice_profile_repository),
    gateway_repo: VoiceGatewayRepository = Depends(get_voice_gateway_repository),
    tts_gateway_service: TTSGatewayService = Depends(get_tts_gateway_service),
    tts_registry: TTSRegistry = Depends(get_tts_registry),
    character_repo: CharacterRepository = Depends(get_character_repository),
    subscription_service=Depends(get_subscription_service),
    uow: UnitOfWork = Depends(get_uow),
):
    """Centralized provider for VoiceProfileService."""
    from src.services.voice_profile_service import VoiceProfileService

    return VoiceProfileService(
        repo,
        gateway_repo,
        character_repo,
        subscription_service,
        uow,
        tts_gateway_service=tts_gateway_service,
        tts_registry=tts_registry,
    )


@lru_cache
def get_realtime_voice_runtime() -> RealtimeVoiceRuntime:
    voice_gateway = get_voice_gateway_repository()
    return RealtimeVoiceRuntime(
        voice_gateway,
        tts_gateway_service=TTSGatewayService(
            registry=get_tts_registry(),
            dashscope_gateway=voice_gateway,
        ),
    )


@lru_cache
def get_realtime_ice_config_service() -> RealtimeIceConfigService:
    return RealtimeIceConfigService(settings)


@lru_cache
def get_realtime_dialog_service() -> RealtimeDialogService:
    return RealtimeDialogService(
        character_llm_service=get_character_llm_service(
            route_service=get_character_llm_route_service(registry=get_llm_registry()),
            gateway_repo=get_llm_gateway_repository(gateway_service=get_llm_gateway_service()),
        ),
        chat_learning_service=get_chat_learning_service(
            gateway_service=get_llm_gateway_service(),
            registry=get_llm_registry(),
            saved_item_repo=None,
        ),
    )


@lru_cache
def get_realtime_turn_service() -> RealtimeTurnService:
    return RealtimeTurnService()


@lru_cache
def get_realtime_vad_model() -> SileroVADModel:
    return SileroVADModel(settings.REALTIME_VAD_MODEL_PATH)


@lru_cache
def get_realtime_session_manager() -> RealtimeSessionManager:
    return RealtimeSessionManager(
        dialog_service=get_realtime_dialog_service(),
        turn_service=get_realtime_turn_service(),
        voice_runtime=get_realtime_voice_runtime(),
        ice_config_service=get_realtime_ice_config_service(),
        vad_model=get_realtime_vad_model(),
        settings=settings,
    )


def get_realtime_telemetry_repository(
    db: AsyncSession = Depends(get_db),
) -> RealtimeTelemetryRepository:
    """Centralized provider for the realtime telemetry repository."""
    return RealtimeTelemetryRepository(db)


def get_realtime_telemetry_service(
    repository: RealtimeTelemetryRepository = Depends(get_realtime_telemetry_repository),
    uow: UnitOfWork = Depends(get_uow),
) -> RealtimeTelemetryService:
    """Centralized provider for the realtime telemetry service."""
    return RealtimeTelemetryService(repository, uow)


def get_character_access_service(
    character_repo: CharacterRepository = Depends(get_character_repository),
):
    """Centralized provider for CharacterAccessService."""
    from src.services.character_access_service import CharacterAccessService

    return CharacterAccessService(character_repo)


def get_memory_service(
    memory_repo: MemoryRepository = Depends(get_memory_repository),
    character_access_service=Depends(get_character_access_service),
):
    """Centralized provider for MemoryService."""
    from src.services.memory_service import MemoryService

    return MemoryService(memory_repo, character_access_service)


def get_saved_item_service(
    repo: SavedItemRepository = Depends(get_saved_item_repository),
    uow: UnitOfWork = Depends(get_uow),
):
    """Centralized provider for SavedItemService."""
    from src.services.saved_item_service import SavedItemService

    return SavedItemService(repo, uow)


__all__ = [
    "bearer_scheme",
    "get_db",
    "get_memory_system",
    "get_current_user",
    "get_optional_current_user",
    "get_r2_storage_service_provider",
    "get_media_cache_service_provider",
    "get_image_processing_service_provider",
    "get_llm_registry",
    "get_llm_gateway_service",
    "get_user_repository",
    "get_user_settings_repository",
    "get_subscription_webhook_event_repository",
    "get_payment_order_repository",
    "get_payment_webhook_event_repository",
    "get_proactive_character_preference_repository",
    "get_proactive_dispatch_repository",
    "get_user_access_pass_repository",
    "get_chat_repository",
    "get_growth_repository",
    "get_turn_repository",
    "get_candidate_repository",
    "get_memory_repository",
    "get_user_service",
    "get_user_settings_service",
    "get_proactive_preference_service",
    "get_subscription_service",
    "get_wechat_payment_service",
    "get_upload_service",
    "get_chat_service",
    "get_turn_service",
    "get_character_repository",
    "get_voice_profile_repository",
    "get_saved_item_repository",
    "get_uow",
    "get_character_service",
    "get_character_access_service",
    "get_memory_service",
    "get_chat_learning_service",
    "get_saved_item_service",
    "get_voice_gateway_repository",
    "get_tts_registry",
    "get_tts_gateway_service",
    "get_voice_service",
    "get_voice_profile_service",
    "get_realtime_voice_runtime",
    "get_realtime_ice_config_service",
    "get_realtime_dialog_service",
    "get_realtime_turn_service",
    "get_realtime_vad_model",
    "get_realtime_session_manager",
    "get_realtime_telemetry_repository",
    "get_realtime_telemetry_service",
    "require_voice_clone_access",
    "get_chat_title_service",
    "get_growth_service",
    "get_discover_config_service",
    "get_character_llm_route_service",
    "get_llm_gateway_repository",
    "get_character_llm_service",
]
