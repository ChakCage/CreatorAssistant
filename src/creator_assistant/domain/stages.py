from enum import Enum


class JobStage(str, Enum):
    VALIDATE_URL = "Проверка ссылки"
    FETCH_METADATA = "Получение информации"
    CHECK_DEPENDENCIES = "Проверка зависимостей"
    CHECK_DISK_SPACE = "Проверка свободного места"
    CREATE_STRUCTURE = "Создание структуры"
    DOWNLOAD_THUMBNAIL = "Скачивание превью"
    DOWNLOAD_MAXIMUM = "Скачивание максимального видео"
    CREATE_PROXY = "CREATE_REAPER_PROXY"
    DOWNLOAD_AUDIO = "Скачивание аудио"
    SEPARATE_STEMS = "Разделение аудио через UVR"
    CREATE_REAPER = "Создание проекта REAPER"
    FINAL_VALIDATION = "Финальная проверка"
    DONE = "Готово"


ORDERED_STAGES = list(JobStage)


def stage_display_name(stage: JobStage, proxy_height: int = 720) -> str:
    """Return a user-facing stage name without baking settings into stage IDs."""
    if stage == JobStage.CREATE_PROXY:
        height = proxy_height if proxy_height in {480, 720, 1080} else 720
        return f"Создание видео {height}p"
    return stage.value
