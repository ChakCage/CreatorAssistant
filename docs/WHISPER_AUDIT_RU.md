# Аудит локального Whisper для Shorts

Дата проверки: 13 июля 2026 года.

- Продукт: официальный Python-пакет `openai-whisper` 20250625 (headless CLI/API, не GUI).
- Python: `C:\Users\wachi\AppData\Local\Programs\Python\Python39\python.exe`, версия 3.9.9.
- CLI: `C:\Users\wachi\AppData\Local\Programs\Python\Python39\Scripts\whisper.exe`.
- Модель: `C:\Users\wachi\.cache\whisper\large-v3-turbo.pt`, 1 617 941 637 байт.
- PyTorch: 2.8.0+cu128; CUDA доступна; NVIDIA GeForce RTX 5080, capability 12.0.
- Выбран backend: `ExistingWhisperBackend`; устройство CUDA; FP16 и word timestamps включены.

Реальный тест выполнен на русском WAV длительностью 19,666 секунды. Расшифровка заняла 16,082 секунды и вернула язык `ru`, 4 сегмента и 24 слова с временными метками. Кириллица и UTF-8 корректны. Память GPU во время запуска выросла примерно с 2 770 до 8 344 MiB, зарегистрирована загрузка GPU до 10%, поэтому использование GPU подтверждено фактическим запуском.

Whisper предупредил об отсутствии локального CUDA Toolkit/Triton kernels и использовал более медленную реализацию median/DTW. Расшифровка при этом завершилась успешно. Тестовые WAV и результаты после фиксации этих метрик удаляются; модель и пользовательские данные не удаляются.
