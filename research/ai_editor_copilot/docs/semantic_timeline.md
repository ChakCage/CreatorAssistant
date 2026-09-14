# Semantic Timeline

`semantic_timeline.schema.json` хранит sources и segments. У source duration
обязательна семантически для timed segments. Время в секундах, `[start,end)`;
`end > start`, start>=0, end<=source duration, IDs уникальны.

Segment: range, source_asset_id, transcript, speaker, topic, story_id,
semantic_summary, narrative_role, emotion, energy, punchline_likelihood, importance,
scene, visual_description, detected_text, objects, faces, silence, shot_change.
Оценки в [0,1], неизвестное — null. faces в будущем — анонимные track IDs, не
распознавание личности. `annotation_origin` отличает human/synthetic/model от
transcript_only. Детальная per-field evidence/provenance — следующий schema revision.

Raw ingest формат: source_id, source_uri, duration, segments[{id,start,end,text,speaker?}].
Можно импортировать готовый SemanticTimeline или CA Transcript через адаптер.
SRT, video/audio probing и запуск STT остаются upstream функциями: этот этап требует
готовых timestamps, не выдумывает длительность файла. Доверенность media metadata
должна подтверждаться FFprobe на этапе реального executor.

Порядок segments не ограничивает sequence. Например, исходник на 57-й минуте
может стать первым hook. Source и output timestamps никогда не смешиваются.
