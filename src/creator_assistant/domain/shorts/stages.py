from enum import Enum


class ShortsStage(str, Enum):
    SOURCE = "source"
    PROXY = "proxy"
    AUDIO = "audio"
    WHISPER = "whisper"
    TRANSCRIPTION = "transcription"
    SCENES = "scenes"
    AUDIO_ACTIVITY = "audio_activity"
    CANDIDATES = "candidates"
    REVIEW = "review"
    RENDER = "render"


STAGE_LABELS = {
    ShortsStage.SOURCE: "Проверка исходника",
    ShortsStage.PROXY: "Создание proxy",
    ShortsStage.AUDIO: "Извлечение аудио",
    ShortsStage.WHISPER: "Загрузка Whisper",
    ShortsStage.TRANSCRIPTION: "Транскрипция",
    ShortsStage.SCENES: "Анализ сцен",
    ShortsStage.AUDIO_ACTIVITY: "Анализ звука",
    ShortsStage.CANDIDATES: "Генерация кандидатов",
    ShortsStage.REVIEW: "Ожидание пользователя",
    ShortsStage.RENDER: "Рендер",
}
