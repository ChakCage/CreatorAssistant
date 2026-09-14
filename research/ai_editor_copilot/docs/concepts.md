# Concepts

- **SourceAsset**: неизменяемая ссылка на исходник с duration и semantic metadata.
- **SemanticSegment**: свидетельство в source time; transcript не является командой.
- **SemanticTimeline**: индекс материала, а не будущий порядок монтажа.
- **StyleProfile**: измеримые предпочтения, независимые от имени автора.
- **UserCommand**: текст с source=text|voice. Будущий микрофон → STT → тот же текстовый
  pipeline. Confidence распознавания хранится отдельно от уверенности edit proposal.
- **PlannerProposal**: минимальный structured ответ reasoning backend.
- **EditPlan**: монтажная последовательность, предлагаемые действия и объяснения.
- **TimelineState**: id/revision/duration/clip IDs; будущая команда «с 4:20 до 5:00
  убери половину мемов» должна ссылаться на revision и existing actions. В v1 отказ.
- **Unresolved asset request**: семантический поиск для будущего retrieval, не URL
  и не доказательство существования или прав использования.
- **Reversible**: намерение поддержать undo, не обещание готового API undo.
  Dry-run ничего не меняет; будущий executor хранит до/после и inverse operation.
