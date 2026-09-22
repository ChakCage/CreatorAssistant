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

## Milestone 1: evidence-backed narrative research

`scripts/ingest_media.py` explicitly invokes CA ExistingWhisperBackend/TranscriptionService,
with the existing large-v3-turbo model and English source language. No production settings
are changed. SHA-256 of the entire source and normalized raw transcript, FFprobe duration,
backend version, language and UTC ingest time are sealed in a private manifest under `runs/`.
Raw ASR text is never repaired in-place. Zero-duration/empty segments and word-alignment
anomalies remain visible evidence; zero-duration spans cannot become standalone clips.

`narrative/` layers:

1. `evidence`: exact references; OBSERVED is a verbatim quote, INFERRED carries provenance,
   UNKNOWN cannot contain a factual value. Inference provenance does not prove truth.
2. `chunks`: whole transcript segments, max 10k characters/360 seconds, three-segment overlap;
   oversized single segments fail explicitly. Segment and source-window union coverage are separate.
3. `pipeline`: local candidate extraction in every chunk, exact-span dedup only, cached raw responses.
   Global index pages are all ranked when over budget; every omitted candidate is audited.
   Final selection is then checked against reconstructed ORIGINAL text, not summaries alone.
4. `contracts`: typed refusal, story nodes/edges, per-cut rationale and prerequisite ordering.
   Structural checks cannot prove causal truth; unresolved contradictions are rejected.
5. `baseline`: deterministic density/lexical-relevance continuous window; positional role proxies
   pass the same temporal/structural EditPlan validator, without pretending semantic understanding.
6. `evaluation`: mechanical metrics, independent-request variance and blinded text packages.
   Human ratings/private mapping never enter model input. No executor or production UI added.

Transport budget is conservatively capped at 24k UTF-8 bytes including schema, leaving
space in the 32768-token context for 6000 generated tokens and framing. No prompt slicing.
Calls log original response, error, bounded repair, latency and reported token usage.
Models are fixed to qwen3.6:35b-a3b. Analysis cache identity includes transcript hash,
chunk contents, model and prompt version; global selection is regenerated for each repeat.

The graph currently lives in the adjacent `story_graph.json`, linked by selected candidate
IDs matching EditPlan segment IDs. It is not a claim that all 31 DSL operations have
evidence-aware execution semantics. Only INSERT_VIDEO plans are compiled; no media is rendered.
