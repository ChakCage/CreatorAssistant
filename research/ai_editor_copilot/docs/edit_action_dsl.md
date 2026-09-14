# Edit Action DSL 1.0

Каждое действие: schema_version, id, action, target(kind/id), source(asset или
asset_search либо null), timing(output seconds), parameters, reason, confidence,
dependencies, reversible, generated_by(backend/model/prompt_version).

| Группа | Типы | Typed parameters |
|---|---|---|
| Монтаж | CUT, TRIM, DELETE_RANGE, REMOVE_SILENCE, MOVE_CLIP | empty / source_range / threshold_db+minimum_silence / destination |
| Вставки | INSERT_BROLL, INSERT_MEME, INSERT_IMAGE, INSERT_VIDEO | fit: contain/cover |
| Текст | ADD_TEXT, REPLACE_TEXT, ADD_SUBTITLES | text/font/size либо language/style_id |
| Геометрия | PUNCH_IN, ZOOM, PAN, CROP, REFRAME | scale 1–4; normalized x/y; normalized margins; aspect_ratio/center_x |
| Время | SPEED_UP, SLOW_MOTION, FREEZE_FRAME | rate >1 / 0<rate<1 / source_time |
| Композитинг | CHROMA_KEY, MASK, TRACK_OBJECT, BLUR | RGB hex+tolerance / description / object_description / radius |
| Звук | ADD_MUSIC, DUCK_MUSIC, ADD_SFX, FADE_AUDIO, NORMALIZE_AUDIO | gain_db / reduction_db+attack+release / direction / target_lufs |
| Прочее | TRANSITION, STABILIZE | kind / strength |

31 тип; параметры не принимают посторонние поля. Схема может описать будущие
операции, но весь список пока выполняется только как печать dry-run proposals.
Compiler первого use case создаёт только INSERT_VIDEO.

Пример формы (полный валидный экземпляр — в example edit_plan.json):

```json
{
  "schema_version":"1.0", "id":"edit_example", "action":"PUNCH_IN",
  "target":{"kind":"clip","id":"clip_example"}, "source":null,
  "timing":{"timeline_start":12.0,"duration":0.7},
  "parameters":{"scale":1.35},
  "reason":"Punchline в речи; визуальное подтверждение ещё нужно", "confidence":0.7,
  "dependencies":[], "reversible":true,
  "generated_by":{"backend":"mock","model":"fixture","prompt_version":"narrative-v1"}
}
```

Timing CUT имеет duration=0; остальные операции требуют >0. Все зависимости
должны быть ранее в actions, что исключает циклы. Clip-target должен существовать,
действие находится внутри него, source ranges внутри assets. Ровно одна базовая
INSERT_VIDEO на clip. Внутренние параметры будущих сложных эффектов потребуют
расширения schema_version и executor-specific capability checks.

DELETE_RANGE означает удаление участка timeline, не файла. В dry-run не удаляется
даже участок. Для будущего исполнения нужны revision, consent policy и undo journal.
