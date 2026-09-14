# DaVinci integration: архитектурный аудит, 2026-09-15

## Источники и границы доказательств

Основной источник — **официальный README установленного SDK**, а не сторонняя
копия: `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\README.txt`.
В заголовке Last Updated: 7 Oct 2025; SHA-256:
`0de217e65f9bdfcc5b0c84eecdda78eac5aa212d59e9d3b9e0655ca6dc7387fd`.
Документирование метода подтверждено чтением файла; runtime вызовы НЕ выполнены.
Наличие SDK не доказывает установленную работоспособную Studio/Free версию.

Дополнительные первичные источники:

- [Blackmagic Fusion scripting guide](https://documents.blackmagicdesign.com/UserManuals/Fusion8_Scripting_Guide.pdf):
  модель объектов FusionScript, Python/Lua automation; исторический guide Fusion 8.
- [Официальная страница Resolve Fusion](https://www.blackmagicdesign.com/ca/products/davinciresolve/fusion):
  scripting/automation. Не использовать как доказательство конкретных методов.

Confidence ниже относится к **предполагаемому пути интеграции**, не к реализованной
capability. Для всех действий нужна runtime проверка на точно указанной версии/edition.

| Action | Possible DaVinci mechanism | Confidence | Needs verification | Fallback |
|---|---|---|---|---|
| CUT | Пересобрать два subclip через MediaPool.AppendToTimeline clipInfo | medium | Да: отдельный split method не подтверждён | EDL/XML rough cut |
| TRIM | AppendToTimeline startFrame/endFrame в новом timeline | high для создания | Да: trim существующего item не доказан | Пересборка копии timeline |
| DELETE_RANGE | Timeline.DeleteClips(items, ripple) после разбиения | medium | Да: диапазон ≠ целый item | Сборка нового timeline |
| REMOVE_SILENCE | Внешнее VAD → subclips → AppendToTimeline | medium | Да: VAD, audio handles, ripple | EDL proposal/markers |
| MOVE_CLIP | Пересборка с recordFrame/trackIndex | medium | Да: move existing item не подтверждён | Новый timeline |
| INSERT_BROLL | Media pool import + AppendToTimeline trackIndex/recordFrame | high для пути | Да: overlay, длительность, аудио | XML или markers |
| INSERT_MEME | Тот же media pool путь | medium | Да: alpha/GIF, timing | Проверенный локальный asset |
| INSERT_IMAGE | Media pool import и длительность still | medium | Да: still duration semantics | Fusion still/manual |
| INSERT_VIDEO | AppendToTimeline clipInfo | high | Да: endFrame inclusivity, fractional FPS | EDL/XML |
| ADD_TEXT | InsertFusionTitleIntoTimeline; Fusion comp | medium | Да: доступ к text tool/style | Marker с текстом |
| REPLACE_TEXT | Fusion comp tool inputs | medium | Да: template-specific paths | Manual title edit |
| ADD_SUBTITLES | CreateSubtitlesFromAudio документирован | medium | Да: импорт произвольных готовых cues не доказан | SRT + manual import |
| PUNCH_IN | TimelineItem.SetProperty ZoomX/ZoomY на выделенном subclip | high для static | Да: длительность эффекта | Fusion transform |
| ZOOM | Static SetProperty или Fusion animation | medium | Да: keyframe API | Fusion comp |
| PAN | SetProperty Pan/Tilt | high для static | Да: units/animation | Fusion transform |
| CROP | SetProperty CropLeft/Right/Top/Bottom | high | Да: normalized→pixel conversion | Fusion crop |
| REFRAME | Crop/Zoom/Pan и формат timeline | medium | Да: subject tracking отдельно | Static crop/manual |
| SPEED_UP | RetimeProcess — только интерполяция, не speed | low | Да: setter скорости не подтверждён | XML или manual |
| SLOW_MOTION | То же, Fusion time tools как гипотеза | low | Да: motion artifacts/audio | Manual retime |
| FREEZE_FRAME | Fusion time tool / still subclip | low | Да: нет подтверждённого freeze setter | Export still + manual |
| CHROMA_KEY | Fusion composition с keyer | medium | Да: node/params/version | Предложение marker |
| MASK | Fusion mask nodes | medium | Да: coordinates/template | Manual mask |
| TRACK_OBJECT | Fusion tracker | medium | Да: tracker configuration/evaluation | Manual track |
| BLUR | Fusion comp blur | medium | Да: radius mapping | Manual effect |
| ADD_MUSIC | Media pool + audio track via mediaType=2 | high для placement | Да: уровни/looping | Audio clip + manual gain |
| DUCK_MUSIC | Automation/keyframes/Fairlight — не подтверждено | low | Да | Envelope export/manual |
| ADD_SFX | Audio-only AppendToTimeline | high для placement | Да: gain/offset | Audio clip |
| FADE_AUDIO | Audio automation — конкретный API не подтверждён | low | Да | Manual fade |
| NORMALIZE_AUDIO | Fairlight operation — API не подтверждён | low | Да: LUFS/true peak | Измерение + recommendation |
| TRANSITION | Timeline/Fusion template — arbitrary setter не найден | low | Да | Hard cut/manual |
| STABILIZE | TimelineItem.Stabilize() документирован | high для вызова | Да: параметры/edition/result | Manual stabilization |

## Подтверждённые в SDK точки входа

MediaPool.AppendToTimeline: README строка 221 описывает mediaPoolItem, startFrame,
endFrame, mediaType, trackIndex, recordFrame. ImportTimelineFromFile (225) перечисляет
AAF/EDL/XML/FCPXML/DRT/ADL/OTIO. Timeline.DeleteClips (348) имеет optional ripple.
Markers (351, 432) поддерживают customData — будущая связь plan/action IDs.
Fusion comp Add/Import (448–449), SetProperty (428; свойства с 846), Stabilize (481).
Render queue: AddRenderJob, SetRenderSettings, StartRendering; это экспорт, не
планирование/редактируемый timeline. Export перечисляет EDL/FCPXML/OTIO, но roundtrip
effects и audio требует отдельного теста.

## Первый будущий executor

1. Проверить Resolve version/edition, scripting settings и connect локально.
2. Создать **новый** timeline, импортировать один разрешённый fixture asset.
3. Перевести half-open seconds в frame ranges, проверить границы с 29.97/59.94 FPS.
4. AppendToTimeline пяти subclips с сохранением linked audio.
5. Прочитать обратно start/duration/source, сравнить с EditPlan до допуска операции.
6. Добавить markers с action_id/reason; сохранить DRT и undo journal.

Никаких обещаний полного undo/transaction: использовать отдельный timeline и
rollback удалением только вновь созданных объектов после подтверждения стратегии.
Не открывать network scripting. Полноценный plugin/управление существующим проектом
не входит в текущий milestone.
