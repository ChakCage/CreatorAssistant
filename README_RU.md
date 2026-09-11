# Creator Assistant

Creator Assistant — настольное Windows-приложение для подготовки материалов к монтажу, озвучке и публикации.

## Редакции

**Creator Assistant Developer Preview** — бесплатная standalone-редакция для тестирования и разработки. Она работает без подписки, activation code, Telegram-бота, YooKassa и backend Creator Assistant. В неё входят «Подготовка проекта», Shorts, вертикальный редактор, локальный AI, Autopilot, очередь публикаций и диагностика.

**Creator Assistant Commercial** — отдельная будущая подписочная редакция. Её лицензирование, данные и update channel не используются Developer Preview.

Developer Preview при первом запуске показывает мастер компонентов. FFmpeg/FFprobe и yt-dlp можно установить в общий пользовательский runtime. Ollama и модель `qwen3.6:35b-a3b` загружаются только после явного подтверждения; без AI остальные функции продолжают работать.

## Возможности Project Preparation MVP

- получение метаданных, форматов, FPS, размеров и превью через yt-dlp;
- выбор папки автора и обнаружение существующих проектов;
- создание структуры проекта и manifest с поддержкой продолжения незавершённой работы;
- загрузка максимального SDR-видео без HDR, HLG и Dolby Vision;
- создание MP4-прокси для REAPER с настраиваемым качеством 480p, 720p или 1080p;
- загрузка оригинальной audio-only дорожки и лучшего превью;
- создание Instrumental FLAC через Audio Separator с моделью `UVR-MDX-NET Inst HQ 3`;
- использование GPU для Audio Separator и NVIDIA NVENC для прокси при доступности;
- генерация и открытие проекта REAPER, открытие корневой папки проекта;
- подробный прогресс, журнал, отмена операций и продолжение прерванного задания;
- контроль свободного места и настраиваемая временная папка;
- начало нового проекта без перезапуска приложения;
- one-folder Windows-сборка без консольных окон.

## Системные требования

- Windows 10 или Windows 11 x64;
- для запуска из исходников: Python 3.9 или новее, рекомендуется Python 3.10+;
- свободное место для исходного видео, прокси, аудио и временных файлов;
- NVIDIA GPU рекомендуется для NVENC и GPU-разделения, но доступность конкретных режимов зависит от установленных драйверов и внешних программ.

## Возможности Shorts MVP

- локальный файл или финальный рендер из папки проекта;
- FFprobe, analysis proxy и возобновляемый cache;
- реальная локальная транскрипция Whisper без облачного API;
- анализ сцен, пауз и речи, эвристический ranking и dedupe кандидатов;
- QtMultimedia preview, ручные границы, approve/reject;
- отдельный редактор UTF-8 SRT/ASS для каждого Short;
- Center Crop и Blur Background;
- последовательный рендер 1080×1920 H.264/AAC из оригинала с NVENC fallback;
- отмена, resume и FFprobe-валидация результата.

Подробности: [docs/SHORTS_MVP_RU.md](docs/SHORTS_MVP_RU.md).

## Внешние зависимости

В installer Developer Preview не входят крупные или проприетарные зависимости:

- [yt-dlp](https://github.com/yt-dlp/yt-dlp);
- FFmpeg и FFprobe;
- Ultimate Vocal Remover / совместимый Audio Separator runtime;
- REAPER;
- модели ONNX и драйверы NVIDIA.

Пути можно задать в настройках. Мастер умеет безопасно установить yt-dlp и FFmpeg/FFprobe, открыть официальный installer Ollama и загрузить точную AI-модель после подтверждения. REAPER, VEGAS и DaVinci Resolve только обнаруживаются или выбираются вручную и не устанавливаются приложением.

## Запуск из исходников

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\run_dev.ps1
```

Запуск тестов:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m pytest
```

Автоматические тесты используют временные каталоги и не должны обращаться к пользовательским проектам.

## Сборка Windows EXE

```powershell
.\scripts\build.ps1
```

Developer Preview собирается командой `scripts\build-installers.ps1 -Edition developer`. Результат: `dist\installers\CreatorAssistant-Developer-Preview-Setup-<version>.exe`.

## Пользовательские данные

- настройки Developer Preview: `%APPDATA%\CreatorAssistant\DeveloperPreview`;
- журналы и состояния: `%LOCALAPPDATA%\CreatorAssistant\DeveloperPreview`;
- общие локальные модели/runtimes: `%LOCALAPPDATA%\CreatorAssistant` и системный cache Ollama.

Пользовательские `settings.json`, cookies, browser profiles, журналы, проекты, медиа, модели и runtimes не входят в репозиторий. Не добавляйте их в коммиты или release-архивы.

## Структура проекта

- `src/creator_assistant/domain` — модели, этапы и ошибки предметной области;
- `src/creator_assistant/services` — подготовка медиа, REAPER, хранилище и Audio Separator;
- `src/creator_assistant/infrastructure` — процессы, настройки, manifest, задания и обнаружение зависимостей;
- `src/creator_assistant/ui` — интерфейс PySide6 и безопасные UI-worker bridges;
- `tests/unit` и `tests/integration` — автоматические проверки;
- `scripts` — запуск, сборка и проверочные сценарии;
- `CreatorAssistant.spec` — переносимая конфигурация PyInstaller.

## Ограничения Developer Preview

- AI-модель занимает десятки гигабайт и загружается отдельно после подтверждения;
- приложение пока не имеет подписи Authenticode, поэтому SmartScreen может предупредить;
- автоматические обновления Commercial отключены; новые preview-версии публикуются через GitHub Releases;
- доступность автоматического Audio Separator зависит от совместимого runtime;
- качество и доступность потоков определяются YouTube и yt-dlp;
- приложение не предназначено для обхода DRM или доступа к приватным материалам.

## Права на контент

Пользователь обязан соблюдать авторские права, условия YouTube и применимое законодательство. Загружайте и обрабатывайте только тот контент, на который у вас есть необходимые права или разрешение.

## Ветки

Тег `v0.1.0-project-prep`, `main` и `stable/project-preparation-mvp` сохраняют стабильный Project Preparation MVP. Shorts разрабатывается только в `feature/shorts-mvp` и не объединяется с `main` без отдельного разрешения.
