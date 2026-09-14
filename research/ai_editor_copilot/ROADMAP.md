# Roadmap

| Phase | Цель | Dependency | Измеримый критерий приёмки |
|---|---|---|---|
| 0 | Architecture, schemas, planner POC | timestamp fixtures | Schemas и tests; валидный noncontiguous ≤60s plan; invalid plan не исполняется |
| 1 | Semantic transcript timeline | 0, CA STT adapter | 5 реальных transcript без потери source/time references; provenance каждой annotation |
| 2 | Narrative Shorts | 1 | Global story retrieval по полному материалу; минимум 10 held-out задач; rubric и contiguous baseline |
| 3 | DSL + dry run hardening | 0 (ядро уже есть) | Все 31 parameter contracts; revision-aware targets; dependencies/undo journal tests |
| 4 | Video/scene understanding | 1 | Shot/visual annotation benchmark; precision/recall и cost; unknown evidence не выдумывается |
| 5 | DaVinci rough-cut executor | 2, 3 | В новом timeline 5 subclips+audio; roundtrip offsets ≤1 frame, подтверждение на 29.97/59.94 |
| 6 | Voice commands | 3, 5 | Те же команды text/voice дают эквивалентные планы в 20 сценариях; low-confidence confirmation |
| 7 | Style Analyzer | 4 | 3–5 reference на профиль; сравнение shot/B-roll metrics с human labels; error thresholds после пилота |
| 8 | B-roll/meme retrieval | 4, 7 | Локальный каталог и права/provenance; Recall@K/NDCG; ни одного silent download |
| 9 | Learning from corrections | 3, 5, opt-in corpus | source-disjoint splits, do-nothing negatives; held-out improvement over baseline с CI |
| 10 | Thesis evaluation | 2, 5; 9 опционально | Пререгистрированный протокол, blind comparison, ablations, latency/failure report |

Фазы могут перекрываться: DSL и dry-run из Phase 3 минимально реализованы в Phase 0.
Полный multimodal/voice executor не является условием исследования narrative selection.

## Следующий конкретный milestone

**Реальный transcript → evidence-backed SemanticTimeline + hierarchical narrative selection.**
Взять 3–5 разрешённых длинных transcript через существующий CA TranscriptionBackend,
сохранить source fingerprints, выделять story candidates по блокам без потери начала/
середины/конца, глобально собирать одну историю, сравнить с contiguous baseline.
Приёмка: исходные timestamps сохранены; context budget не обрезает материал молча;
на всех входах валидный план или явная причина отказа; слепая оценка связности по rubric.
После этого — первый минимальный Resolve executor на отдельном timeline.
