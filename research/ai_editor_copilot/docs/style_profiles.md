# Style Profiles и будущий Style Analyzer

Пресеты: narrative_short, dynamic_talking_head, travel_vlog, gaming_fast,
documentary. Все описаны количественно в `style/profiles.py`. Density — доля
подходящих возможностей/времени, а не обещание вставлять эффект через N секунд;
операционная метрика каждой density должна фиксироваться в эксперименте.

Храним pacing, average_shot_duration, visual_changes_per_minute, B-roll/meme/zoom/SFX
density, subtitle/transition style, music usage, cold open, dead-air aggressiveness.
В v1 передаются planner и сохраняются в plan; гарантия пока только по длительности
и структуре последовательности. Visual смены 1–3 сек требуют второго pass поверх
rough cut: привязка assets/effects к клипам и проверка реального visual-change rate.

Style Analyzer (будущий): несколько добровольно предоставленных reference videos
→ shot boundaries, audio events, overlay/zoom statistics → robust medians/quantiles
и pacing curve с confidence → StyleProfile(origin=reference_analysis).
Измерять cut frequency, shot length distribution, B-roll ratio, zoom/SFX density,
subtitle behavior, music density, intro structure и динамику по времени.
Отделять визуальные изменения от простых cuts и игровое движение от zoom.
Сначала вручную размеченный небольшой benchmark, затем quality check извлечения.
Не передавать planner только имя автора; проверять перенос стиля между жанрами.
