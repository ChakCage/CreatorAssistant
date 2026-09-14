# Milestone 0 — semantic edit planning foundation

Дата: 2026-09-15. Статус: исследовательский prototype, без media execution.

## База и изоляция

- Исходный checkout: `feature/admin-support-center`, HEAD
  `0c5d5e67cfa0caee8b63ffc88d54111f72141091`, с незавершёнными media-download изменениями.
- Выбранная чистая база: опубликованный Developer Preview
  `507fa555f2ebcf1a5a9c7c75f8e5ff5d9943d773` (`release/developer-preview`).
- Новая ветка: `feature/ai-editor-copilot-lab`, отдельный worktree.
- Все изменения этого этапа внутри `research/ai_editor_copilot/`.

## Реализовано

Typed domain, 8 JSON Schemas (5 обязательных + asset_index/planner_input/proposal),
31 action type с отдельными typed parameters. Source time отделён от output time.
Compiler создаёт stable IDs и gap-free sequence. Backend abstraction, mock и
реальный local adapter CA; bounded repair (1), fail clearly, dry-run с повторной
валидацией, JSON ingest и CA Transcript adapter. Feedback schema и локальный writer.

Исследовательская модель не привязана к Ollama; только конкретный адаптер использует
существующий OllamaSemanticScorer и проверенную модель qwen3.6:35b-a3b.
Нет изменений общего local-AI кода. Compileall успешен.

## Demo evidence

Synthetic source duration 3600 секунд, 9 разрозненных timestamp segments.
Это не час настоящей речи. Фактического video fixture нет.

### Детерминированный baseline

`examples/narrative_short/mock_output/edit_plan.json`: пять клипов,
42–49, 850–862, 1864–1876, 2892–2908, 3428–3439 = **58 секунд**.
Роли hook/setup/development/climax/payoff. Использованы synthetic annotations.

### Реальная локальная модель

`examples/narrative_short/local_output/edit_plan.json`: четыре клипа,
850–862 (hook), 1864–1876 (setup), 2892–2908 (climax), 3428–3439 (payoff),
итого **51 секунда**, constraint [50,60]. Backend creator-assistant-local,
model qwen3.6:35b-a3b. Исходный `raw_transcript.json` не содержит story/role/importance
labels, IDs нейтральные. Полный validated input и dry-run сохранены рядом.

Первый live запуск до уточнения duration-инструкции был отклонён: 66 секунд
после repair. Его raw responses не были сохранены; доступное evidence — CLI error.
После добавления арифметики durations модель сначала вернула 51 секунду без
явного первого hook; валидатор отклонил. Одна repair-попытка вернула валидные роли.
Обе responses этого запуска сохранены в `local_output/validation.json`.
Это проверка repair/fail-closed, не доказательство стабильности модели по одному запуску.

Смысловая оценка: отель → отказы → открытие двери/кошки → объяснение приюта даёт
понятный causal arc. Модель пропустила готовую тизерную фразу на 42-й секунде и
назвала завязку hook; качество hook требует оценки редактором. В объяснении есть
слова «visual hook», но это inference из текста, не visual-model evidence.
Гарантии размера/границ/ролей проверены; реальная монтажная связность, дыхание,
субтитры, кадры и качество cuts пока не проверялись.

## Тесты

- 75 новых tests passed: schemas, strict types, timestamps, 31 parameter types,
  source/output boundaries, duration, duplicate/overlap, roles, reordered source,
  malformed JSON, repair, deterministic IDs, dependencies, mutated-plan refusal,
  dry-run side effects, feedback, unresolved assets, local transport contract,
  import isolation, CLI failure/evidence preservation, обе committed demos.
- 20 relevant CA tests passed: semantic backend, transcription и две pipeline
  проверки (включая повтор тестов, нестабильно упавших в полном suite).
- Полный существующий suite: два итоговых запуска по 505 passed / 1 failed.
  В одном WinError 5 os.replace JobStore на MAX pipeline, в другом на resume
  pipeline. Оба прошли отдельно без изменений кода. Это transient Windows file
  access failure; точный источник блокировки файлов не установлен.
- Предварительные прогоны имели ошибки тестового окружения: отсутствовал parent
  basetemp, слишком длинный путь менял Windows filename budget, UI пытался писать
  в обычный AppData. Финальные прогоны используют короткий workspace basetemp и
  отдельные APPDATA/LOCALAPPDATA. Не выдаём полный suite за безусловные 506/506.

## Что остаётся

Style preferences пока не материализуются в эффекты; only base INSERT_VIDEO.
Все другие action types — DSL proposals. Нет execution в Resolve/FFmpeg, voice,
media retrieval, visual analysis, training или изменения существующего timeline.
Detailed findings: `docs/davinci_integration.md`; hypotheses/metrics: THESIS_NOTES;
phases и следующий milestone: ROADMAP.

Следующая реализация: реальные длинные transcript, evidence/provenance annotations
и hierarchical narrative retrieval с contiguous baseline и human coherence rubric.
