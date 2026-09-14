# Архитектура и решения

```text
Timestamped JSON / CA Transcript
  → ingest → SemanticTimeline (unknown observations = null)
  + AssetIndex + StyleProfile + UserCommand + constraints
  → EditPlanner → LLMPlannerBackend → PlannerProposal JSON
  → strict parse → Pydantic schema → compile → semantic validation
       ↳ одна repair-попытка с ошибкой и предыдущим ответом
  → EditPlan → повторная validation → DryRunExecutor
  → FeedbackEvent (proposal / correction / final decision)
```

## Независимые слои

| Каталог | Ответственность |
|---|---|
| domain | Данные, timestamps, источники, request, plan, constraints |
| ingest | JSON и duck-typed адаптер существующего CA Transcript |
| semantic | Контракт enrich; transcript-only реализация сохраняет неизвестные поля |
| assets | Каталог и локальный лексический поиск, без доступа к URI |
| style | Пять пресетов независимых от автора |
| planner | Backend contract, compiler, schema/semantic validation и repair |
| tools | Дискриминируемый Action DSL, параметры и registry |
| adapters | Mock и реальный CA/Ollama transport |
| executors | Dry-run; fail-closed stubs FFmpeg и Resolve |
| feedback | Формат решений и локальное хранение событий |

## Почему proposal отдельно от plan

Модель определяет **что/почему**, compiler — точные source IDs, монтажные offsets,
стабильные clip/action IDs и зависимости. Это уменьшает размер structured output
и устраняет арифметические ошибки модели. Схемы обоих контрактов опубликованы.
Plan ID — digest контекста/предложения/provenance; action ID зависит от типа и
источника/диапазона, а не положения в списке. Ручная коррекция хранит исходный ID
и новый revision; изменение диапазона compiler создаёт как новое предложение.

Секунды half-open `[start,end)`. Source time и output time — разные поля.
Output sequence допускает изменение исходного порядка. Исходные диапазоны не
пересекаются; вывод непрерывен. Финальный frame quantization не реализован:
будущий executor должен переводить интервалы по рациональному FPS, документировать
inclusive/exclusive endFrame и проверять accumulated drift.

## Источник истины и схемы

Pydantic-модели — канонические; `scripts/export_schemas.py` механически экспортирует
Draft 2020-12 JSON Schema. Тест сверяет каждый опубликованный schema-файл.
`additionalProperties=false` на всех моделях и отдельная модель parameters
на каждый action. JSON Schema не умеет сравнивать два произвольных поля и внешние
ссылки: поэтому semantic validation обязательна даже после успешного schema check.

Все операции описуемы, но v1 executor допускает только известные clip-targets
построенного плана. Timeline/track targets заложены для будущих revision-aware
операций. Наличие типа в registry не означает работоспособную DaVinci capability.

## Переиспользование CA

Адаптер импортирует `services.shorts.semantic_backend.OllamaSemanticScorer` лениво.
Используются preflight/warm_up и `_stream_chat`/`_record_metrics`. Последние private:
это осознанная локальная точка зависимости, покрытая transport-contract тестом.
Общее приложение не рефакторится. В будущем публичный StructuredLLMClient может
заменить эту обёртку. Доменный интерфейс не содержит Ollama/Qwen-specific полей.

Существующий `TranscriptionBackend` принимает audio/output_dir/cancellation и
возвращает CA Transcript. Сейчас результат можно передать `from_creator_transcript`;
распознавание не стартует неявно. Библиотеки/UI/модели Whisper не дублируются.

## Безопасность исполнения и воспроизводимость

Model output — данные: нет shell, eval, скачивания asset_search или вызовов Resolve.
Dry-run повторно валидирует mutable plan/context. Невалидный план не экспортируется
как успешный. Неуспех CLI: stderr и код 2, без нового plan; CLI требует пустой каталог
вывода и сохраняет validation report неудачных попыток для аудита.
Синтетические fixtures можно хранить в Git; реальные transcript/feedback хранить
в приватном каталоге по отдельному согласию. Никакого автоматического обучения.
