# Feedback и learning strategy

Сейчас обучение не выполняется. `FeedbackEvent` связывает AI proposal,
user modification, final edit decision с action ID, plan ID и timeline revisions.
Решения: accepted / modified / rejected / undone. Timestamp с timezone, participant
псевдонимный. Original proposal хранится целиком; accepted сохраняет его без изменений.
Контекст связывается digest; хранение corpus должно быть версионированным и приватным.

Для пригодного будущего датасета фиксировать: источник и его content hash, выбранные
и отклонённые candidates, language/genre, StyleProfile version, prompt/model/digest,
seed/options/context window, evidence segments, latency, ошибки validation/repair,
AI confidence, ручные offsets/trim/effects, время редактирования, финальную revision.
Не использовать confidence LLM как calibrated probability без проверки calibration.
Нужна запись «ничего не делать», иначе selection bias заставит модель вставлять эффекты.

Текущий JSON writer append-only по ID с exclusive create, локальный одиночный event.
Для production durable concurrent logging нужны transactional store/WAL и recovery;
не объявляем простую файловую запись полноценным dataset service.

## Кандидаты моделей

1. Edit Decision Model: semantic/visual context + genre/style + previous actions →
   do nothing/cut/B-roll/meme/zoom/SFX/text; supervised + cost-sensitive baseline.
2. Ranking Model: pairwise preferences по hook, next shot, B-roll/meme alternatives.
3. Personalization: user correction delta относительно общего профиля, с явным opt-in.

Разбивать train/test по source video и автору/проекту, а не по соседним клипам.
Один исходник и его edited variants не должны попадать в разные splits.
Отдельный cold-user test, inter-rater agreement, baseline без personalization.
Прежде обучения проверить coverage, права на материалы, качество labels и размер
выборки. Синтетические примеры используются для инженерных проверок, не как
доказательство эффективности на реальных редакторах.
