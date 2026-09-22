# Рабочая исследовательская рамка

## Возможные названия

1. «Планирование семантически связного видеомонтажа по естественно-языковым инструкциям».
2. «Методы валидации и исполнения объяснимых планов автоматизированного видеомонтажа».
3. «Интерактивный ассистент видеомонтажа с обучением на корректировках редактора».
4. «Семантическая реконструкция короткого видеосюжета из несмежных фрагментов длинного материала».
5. «Мультимодальное планирование редактируемых видеопоследовательностей с управлением на естественном языке».

## Problem / motivation

Поиск связной истории в длинном материале требует просмотра, отбора и проверки
смысловых связей. Оценка отдельных интересных фрагментов не гарантирует, что их
последовательность образует завершённый сюжет. Нужен проверяемый интерфейс между
семантическими решениями AI и обратимыми операциями монтажной системы.

## Research question и гипотезы

RQ1: улучшает ли глобальный отбор несмежных фрагментов с narrative roles связность
короткого сюжета по сравнению с contiguous-window baseline при том же бюджете времени?

H1: на отложенных длинных исходниках редакторы выше оценят coherence и payoff
глобально собранного rough cut, при контроле общей длительности.

RQ2: снижает ли typed DSL + semantic validation + bounded repair частоту технически
неисполнимых планов по сравнению с прямым free-form JSON?

H2: доля валидных планов растёт; измерить цену в latency и число repair, не скрывая
ошибки. Схемы сами по себе не доказывают смысловую корректность.

RQ3 (позже): уменьшает ли обучение на коррекциях время до принятого rough cut
для нового проекта того же пользователя без ухудшения результатов у новых пользователей?

## Метод и кандидаты вклада

Архитектура: ingest → semantic timeline → style/command → constrained planner →
validated DSL → capability-aware executor → feedback. Уже готовые LLM/STT/visual
models и embeddings используются как компоненты. Foundation video model с нуля
не обучается. Собственные части: temporal/narrative representation, compiler,
семантические constraints, evaluation protocol и будущие decision/ranking models.

Новизна — гипотеза до literature review: (a) отбор несмежной истории с явными
constraints; (b) измерение роли validator/repair; (c) feedback dataset для действий.
Сам факт подключения Ollama к DaVinci, голосовой ввод или UI — продуктовые функции,
не самостоятельный научный вклад. Не заявлять отсутствие аналогов без обзора работ.

## Метрики

| Метрика | Операциональное определение |
|---|---|
| Edit acceptance | accepted unchanged / все показанные предложения; rejected и undo учитывать |
| Corrections | число правок на принятую минуту и величина trim/position deltas |
| Time to rough cut | от запроса до принятой sequence; отдельно inference и активное время редактора |
| Manual time reduction | парное отношение времени baseline и assisted, с доверительным интервалом |
| Semantic continuity | слепая оценка 1–5: причинность, референсы, хронология, setup/payoff; agreement |
| Duration adherence | доля планов в [min,target]; абсолютная ошибка от желаемой длины отдельно |
| Style adherence | отклонение cut/visual-change/B-roll statistics от профиля после execution |
| Command success | доля заданий, где независимый оценщик подтвердил все обязательные условия |
| Validity | schema-valid, semantically-valid, executable — три отдельных показателя |
| Satisfaction | заранее выбранная одинаковая анкета + причины отказа |

## Эксперимент

Пилот: 10–20 добровольно предоставленных роликов разных жанров; масштаб выборки
после оценки variance и доступности экспертов. Для каждой записи — human narrative
annotations, source duration, допустимые outcomes; не считать один reference edit
единственным правильным ответом. Строгий test split по видео. Редакторы получают
результаты в случайном порядке без имени метода; crossover порядок балансировать.

Baselines: (1) ручной montage, (2) best contiguous window, (3) global LLM без ролей,
(4) полный pipeline. Ablations: без style, без semantic validation, без repair;
ошибочные планы только оценивать офлайн, не исполнять. Фиксировать model digest,
hardware, options, seeds, latency и число запусков; повторять nondeterministic
inference, показывать median/CI и failures, не выбирать только лучший ответ.

Синтетический часовой transcript на 9 сегментов — **engineering smoke**, не час
реальной речи и не эксперимент подтверждения гипотезы. Реальные длинные transcript
потребуют hierarchical context budgeting. Следующий шаг: небольшой corpus и
coherence rubric, затем слепой пилот сравнения с contiguous baseline.

## Milestone 1 protocol limits

The user-selected corpus contains three Minecraft gameplay/storytelling sources from
MylesMC, not a representative multi-genre sample. The longest is 117 minutes (outside
the preferred 20–90 range), retained in full. Ten pre-specified scenario probes include
three task-held-out cases; they are NOT verified narrative ground truth or source-disjoint
test data. Consequently candidate recall is null until independent labels exist.

See `evaluation/HUMAN_RUBRIC.md`. Blind text selection evaluation cannot measure visual
continuity or retention. Role completeness is structural only, especially for the
contiguous baseline whose roles are positional proxies. Repeated same-model requests
with temperature zero measure observed selection stability, not cross-model robustness.
Report refusals, invalid output and failed transport separately from valid-plan quality.
Do not equate provenance/reference validity with truth or publish superiority without
independent human ratings. Full raw evidence remains private, never part of public Git.

### Observed development failure, 2026-09-22

The first complete development protocol yielded 0/21 accepted plans despite full
segment coverage. This falsifies readiness of this particular configuration, not
the general possibility of noncontiguous narrative editing. Model refusals confuse
source-time distance with montage duration in at least one recorded example, and
sometimes invoke operational coverage codes unsupported by measured coverage.
Follow-up experiments should separate model-semantic refusal codes from measured
pipeline failures, state units explicitly, and compare episode-local retrieval with
global retrieval before increasing model size or adding an executor. Freeze a new
protocol version and reserve new held-out tasks before such tuning.
