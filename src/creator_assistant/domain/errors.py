class CreatorAssistantError(Exception):
    """Понятная пользователю ошибка приложения."""


class InvalidVideoUrlError(CreatorAssistantError):
    pass


class PlaylistNotSupportedError(CreatorAssistantError):
    pass


class DependencyMissingError(CreatorAssistantError):
    pass


class ProcessExecutionError(CreatorAssistantError):
    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.details = details


class JobCancelledError(CreatorAssistantError):
    pass


class ValidationError(CreatorAssistantError):
    pass


class InsufficientSpaceError(CreatorAssistantError):
    pass


class DiskSpaceError(CreatorAssistantError):
    def __init__(self, operation, path, required_bytes=0, free_bytes=0, reserve_bytes=0, details="") -> None:
        super().__init__("Недостаточно свободного места на диске.")
        self.operation = str(operation)
        self.path = str(path)
        self.required_bytes = int(required_bytes or 0)
        self.free_bytes = int(free_bytes or 0)
        self.reserve_bytes = int(reserve_bytes or 0)
        self.details = str(details)


class SystemMemoryError(CreatorAssistantError):
    pass


class GpuOutOfMemoryError(CreatorAssistantError):
    pass


class AudioSeparatorProcessError(CreatorAssistantError):
    def __init__(self, message="Ошибка Audio Separator.", details="") -> None:
        super().__init__(message)
        self.details = details


class AudioSeparatorOutputMissingError(CreatorAssistantError):
    def __init__(self, temp_path="", output_path="", details="") -> None:
        super().__init__("Audio Separator завершился, но выходной Instrumental не был найден.")
        self.temp_path = str(temp_path)
        self.output_path = str(output_path)
        self.details = str(details)


class MediaValidationError(ValidationError):
    pass


class VegasProjectError(CreatorAssistantError):
    def __init__(self, message="Ошибка создания проекта VEGAS.", details="") -> None:
        super().__init__(message)
        self.details = details


class VegasProjectValidationError(ValidationError):
    pass


class VegasNotFoundError(VegasProjectError):
    pass


class VegasScriptApiUnavailableError(VegasProjectError):
    pass


class VegasUnsupportedVersionError(VegasProjectError):
    pass


class VegasMediaUnsupportedError(VegasProjectError):
    pass


class VegasProjectCreationError(VegasProjectError):
    pass


class VegasProjectSaveError(VegasProjectError):
    pass


class VegasProjectAlreadyExistsError(VegasProjectError):
    pass


class VegasScriptTimeoutError(VegasProjectError):
    pass


class StemSeparationUnavailableError(CreatorAssistantError):
    pass


class ManualActionRequiredError(CreatorAssistantError):
    def __init__(self, source, expected_output, launcher) -> None:
        super().__init__("Автоматическая интеграция UVR недоступна. Требуется ручная обработка.")
        self.source = source
        self.expected_output = expected_output
        self.launcher = launcher


class AudioSeparatorRuntimeMissingError(CreatorAssistantError):
    def __init__(self, runtime_path) -> None:
        super().__init__("Для автоматического разделения требуется установить локальный Audio Separator Runtime.")
        self.runtime_path = runtime_path


class YouTubeAuthenticationRequiredError(CreatorAssistantError):
    def __init__(self, url, video_id, reason, stderr, cookies_used=False, browser="") -> None:
        super().__init__(reason)
        self.url = url
        self.video_id = video_id
        self.reason = reason
        self.stderr = stderr
        self.cookies_used = cookies_used
        self.browser = browser


class YouTubeCookiesUnavailableError(CreatorAssistantError):
    def __init__(self, browser, reason, stderr="") -> None:
        super().__init__(reason)
        self.browser = browser
        self.reason = reason
        self.stderr = stderr


class TransientMetadataError(CreatorAssistantError):
    def __init__(self, message, details="") -> None:
        super().__init__(message)
        self.details = details


class MetadataRequestFailedError(CreatorAssistantError):
    def __init__(self, category, message, details="") -> None:
        super().__init__(message)
        self.category = category
        self.details = details


class YouTubeMediaForbiddenError(CreatorAssistantError):
    def __init__(
        self,
        video_id,
        url,
        role,
        format_id,
        downloaded_bytes=0,
        total_bytes=0,
        percent=None,
        part_path=None,
        auth_context="",
        player_client="unknown",
        po_token_provider="unknown",
        stderr="",
        attempts=1,
    ) -> None:
        super().__init__("YouTube прервал загрузку медиапотока с ошибкой 403. Уже загруженная часть сохранена.")
        self.video_id = video_id
        self.url = url
        self.role = role
        self.format_id = format_id
        self.video_format_id = format_id.split("+", 1)[0]
        self.audio_format_id = format_id.split("+", 1)[1] if "+" in format_id else ""
        self.downloaded_bytes = downloaded_bytes or 0
        self.total_bytes = total_bytes or 0
        self.percent = percent
        self.part_path = part_path
        self.auth_context = auth_context
        self.player_client = player_client
        self.po_token_provider = po_token_provider
        self.stderr = stderr
        self.attempts = attempts
