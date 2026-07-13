# Журнал изменений

## Unreleased — Shorts MVP (`feature/shorts-mvp`)

- локальный headless Whisper с CUDA/FP16, word timestamps, словарём и cache;
- source/FFprobe, analysis proxy, сцены, активность звука, candidates/scoring/dedupe;
- QtMultimedia preview, approve/reject и ручные границы;
- UTF-8 SRT/ASS, редактор, Center Crop и Blur Background;
- последовательный NVENC/libx264 рендер 1080×1920 с отменой, resume и FFprobe-валидацией;
- стабильная вкладка «Подготовка проекта» остаётся в `main`, stable-ветке и теге без merge Shorts.

## 0.1.0 — Project Preparation MVP

Стабильная первая версия вкладки «Подготовка проекта»:

- подготовка проекта по ссылке на публичное YouTube-видео;
- выбор и загрузка максимального SDR-видео;
- прокси для REAPER с качеством 480p, 720p или 1080p;
- загрузка оригинальной audio-only дорожки и превью;
- создание Instrumental FLAC через Audio Separator;
- создание и открытие проекта REAPER;
- manifest, обнаружение существующих проектов и продолжение незавершённой работы;
- детальный прогресс, журнал и безопасная отмена операций;
- настраиваемая временная папка и контроль свободного места;
- открытие RPP и корневой папки готового проекта;
- one-folder Windows-сборка без консольных окон.

### Известные ограничения

- yt-dlp, FFmpeg, FFprobe, REAPER, Audio Separator runtime и модели устанавливаются отдельно;
- автоматическое разделение зависит от совместимого Audio Separator runtime;
- вкладка Shorts ещё не реализована.
