# AI Editor Copilot Lab

Исследовательский подпроект Creator Assistant, версия контракта **1.0**.
Первый milestone: transcript → semantic timeline → request/style → planner →
validated EditPlan → dry run. Никаких операций с реальным монтажным проектом.

## Запуск из этого каталога (PowerShell)

Используется Python >=3.9 и Pydantic 2.8–2.x. Для тестов нужен pytest.
Зависимости уже есть в окружении Creator Assistant. Отдельная установка возможна
через `python -m pip install -e .`; основное приложение устанавливать не требуется
для mock. Существующее окружение можно выбрать полным путём к python.exe.

```powershell
$env:PYTHONPATH = 'src;../../src'
python scripts/export_schemas.py
python -m pytest -q --basetemp=../../.test-runtime/ai-lab-pytest
python -m ai_editor_copilot.cli `
  --transcript examples/narrative_short/transcript.json `
  --prompt 'Сделай Reel про необычный отель: hook, развитие и концовка.' `
  --duration 60 --minimum-duration 50 --backend mock `
  --output runs/mock-demo
```

Для настоящей локальной модели:

```powershell
python -m ai_editor_copilot.cli `
  --transcript examples/narrative_short/raw_transcript.json `
  --prompt 'Сделай 60-секундный Reel про попытку попасть в необычный отель. Нужны hook, завязка, развитие и payoff.' `
  --duration 60 --minimum-duration 50 --backend local `
  --output runs/local-demo
```

Для local нужны исходники CA в `../../src`, работающая локальная Ollama и уже
установленная `qwen3.6:35b-a3b`. CLI ничего не устанавливает и не загружает.
Прогрев и генерация используют timeout 900 с, keep_alive 10m, контекст 32768.
Метод CA трактует generation timeout как отсутствие входящих данных; это не
жёсткий предел общей длительности. Отмена через Ctrl+C. Длиннее 60000 символов
prompt отклоняется явно; иерархический анализ полного часового текста — будущий этап.

## Выход

- `edit_plan.json`: строгий полный план с sequence, narrative_structure, actions.
- `dry_run.json`: предлагаемые операции, `media_modified=false`.
- `validation.json`: попытки и результат schema/semantic validation.
- `input.json`: воспроизводимый вход; не публиковать реальные личные transcript без согласия.
- `summary.txt`: диапазоны, роли, длительность и нерешённые требования.

Каждый запуск требует пустой output directory: предыдущие результаты не
перезаписываются. `runs/` игнорируется Git; примеры в examples/ — зафиксированные evidence.

Пример: 00:00:42–00:00:49 → 00:14:10–00:14:22 → 00:31:04–00:31:16 →
00:48:12–00:48:28 → 00:57:08–00:57:19 = **58 секунд**.
Mock использует искусственные story/role labels и является только baseline.
`raw_transcript.json` не содержит этих подсказок; local demo проверяется отдельно.

## Граница готовности

Работают ingest JSON, модели, 31 тип действия DSL, compiler последовательности,
валидация, одна repair-попытка, локальный backend adapter, mock и dry-run.
Планировщик первого этапа материализует только базовые `INSERT_VIDEO`.
Стиль передаётся модели и сохраняется; его визуальные метрики ещё не обеспечиваются
эффектами. Нерешённые требования остаются в плане. Связность сюжета проверяет человек:
структурно корректный hook/setup/payoff ещё не доказательство смыслового качества.

В Milestone 0 не было retrieval и STT-запуска; исследовательские команды Milestone 1
ниже добавляют их без production-интеграции. Визуальных моделей, voice capture,
рендера, DaVinci-интеграции или обучения нет. Timeline state принят в доменную схему, но его
редактирование отклоняется явно до реализации revision-aware compiler.

См. [ARCHITECTURE](ARCHITECTURE.md), [DSL](docs/edit_action_dsl.md),
[аудит Resolve](docs/davinci_integration.md), [исследование](THESIS_NOTES.md),
[ROADMAP](ROADMAP.md), [результаты этапа](MILESTONE_REPORT.md).
# Milestone 1 research entry points

Real-data research is documented in [MILESTONE_1_REPORT.md](MILESTONE_1_REPORT.md).
Private outputs go under ignored `runs/`, never into examples or schemas.

With `PYTHONPATH=src;../../src` from this directory (Windows):

```powershell
python scripts/ingest_media.py --source '<authorized original.mp4>' --output runs/dataset/source-id
python scripts/evaluate_narratives.py --dataset runs/dataset --tasks evaluation/tasks.json --output runs/evaluation/dev-001 --runs 3
python scripts/evaluate_narratives.py --dataset runs/dataset --tasks evaluation/tasks.json --output runs/evaluation/held-001 --split held_out --release-held-out --runs 3
```

The task manifest source IDs must correspond to sealed dataset directories. Evaluation
outputs are append-only; chunk cache is reused across output directories. A failed
analysis shows incomplete coverage rather than claiming the full source was analyzed.
Use `--task-id dev-06` with a new output directory to repeat only an interrupted
task without changing its definition. The summary exporter excludes unfinished task
groups (no `variance.json`); do not count pilot and final repetitions together.
Human review uses only `evaluation_package.json` and the rubric; do not give evaluators
the coordinator's mapping keys. No media execution is performed by these commands.
