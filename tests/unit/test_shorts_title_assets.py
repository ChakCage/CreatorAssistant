import json
from types import SimpleNamespace

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Candidate, ShortsManifest, SourceInfo, Transcript, TranscriptSegment
from creator_assistant.services.shorts.semantic_backend import OllamaSemanticScorer
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.title_assets import ShortTitleAssetService


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")


def source(tmp_path):
    project = tmp_path / "I Mined 48,235 Obsidian - Hardcore"
    project.mkdir()
    video = project / "Видос.mp4"
    video.write_bytes(b"video")
    return SourceInfo(str(video), video.name, 5, 1, 120, 1920, 1080, 59.94, "h264", "aac", 2, 48000)


def paths(tmp_path):
    root = tmp_path / "I Mined 48,235 Obsidian - Hardcore" / "Shorts"
    root.mkdir(exist_ok=True)
    return SimpleNamespace(root=root, manifest=root / "shorts_manifest.json")


def transcript():
    return Transcript("ru", 60, "all", [
        TranscriptSegment(0, 10, 15, "Сначала ферма скелетов внезапно перестала работать"),
        TranscriptSegment(1, 20, 25, "Потом я нашёл проблему со спавнером и проверил сундуки"),
        TranscriptSegment(2, 30, 35, "В конце я починил ферму и получил сто черепов"),
    ])


def candidate(candidate_id="short_001"):
    return Candidate(candidate_id, 10, 35, 91, "Ферма сломалась, потом я нашёл проблему и починил её", heuristic_score=88, semantic_score=92)


def opener_factory(calls):
    def opener(request, **_kwargs):
        calls.append(json.loads(request.data) if request.data else {"method": request.get_method(), "url": request.full_url})
        if request.get_method() == "GET":
            return Response({"models": [{"name": "qwen3.6:35b-a3b", "digest": "digest-a", "details": {"quantization_level": "Q4_K_M"}}]})
        schema = calls[-1]["format"].get("properties", {})
        if "translation" in schema:
            return Response({"message": {"content": json.dumps({"translation": "Я добыл 48 235 обсидиана — хардкор"}, ensure_ascii=False)}})
        payload = {
            "results": [{
                "candidate_id": "short_001",
                "recommended_id": "hook_1",
                "suggestions": [
                    {"id": "hook_1", "text": "ПОЧЕМУ ФЕРМА СЛОМАЛАСЬ?", "score": 94, "reason": "Есть интрига"},
                    {"id": "hook_2", "text": "КУДА ПРОПАЛИ СКЕЛЕТЫ?", "score": 91, "reason": "Передаёт проблему"},
                    {"id": "hook_3", "text": "Я ПОЧИНИЛ ЭТУ ФЕРМУ!", "score": 86, "reason": "Показывает финал"},
                ],
            }]
        }
        return Response({"message": {"content": json.dumps(payload, ensure_ascii=False)}})
    return opener


def test_title_assets_are_precomputed_and_saved_for_final_candidates(tmp_path):
    calls = []
    src = source(tmp_path)
    manifest = ShortsManifest(1, "shorts", src.path, "fp", 5, 1, 120)
    candidates = [candidate()]
    backend = OllamaSemanticScorer(model="qwen3.6:35b-a3b", opener=opener_factory(calls))
    result = ShortTitleAssetService().prepare(
        source=src,
        paths=paths(tmp_path),
        manifest=manifest,
        candidates=candidates,
        transcript=transcript(),
        backend=backend,
        ai_settings={"enabled": True, "cache": True, "model": "qwen3.6:35b-a3b"},
        cache=SemanticCache(tmp_path / "title_cache.json"),
        cancellation=CancellationToken(),
        content_type="gaming",
    )
    assert result.translated_ready is True
    assert result.hook_ready == 1
    assert manifest.title_assets["original_title"] == "I Mined 48,235 Obsidian - Hardcore"
    assert manifest.title_assets["translated_title"].startswith("Я добыл")
    suggestions = candidates[0].branding_settings["title_suggestions"]
    assert suggestions["status"] == "ready"
    assert len(suggestions["suggestions"]) == 3
    assert suggestions["recommended_id"] == "hook_1"
    assert candidates[0].branding_settings["translated_video_title"].startswith("Я добыл")


def test_title_assets_reuse_cache_without_new_chat_requests(tmp_path):
    calls = []
    src = source(tmp_path)
    cache = SemanticCache(tmp_path / "title_cache.json")
    backend = OllamaSemanticScorer(model="qwen3.6:35b-a3b", opener=opener_factory(calls))
    kwargs = dict(
        source=src,
        paths=paths(tmp_path),
        transcript=transcript(),
        backend=backend,
        ai_settings={"enabled": True, "cache": True, "model": "qwen3.6:35b-a3b"},
        cache=cache,
        cancellation=CancellationToken(),
        content_type="gaming",
    )
    ShortTitleAssetService().prepare(manifest=ShortsManifest(1, "shorts", src.path, "fp", 5, 1, 120), candidates=[candidate()], **kwargs)
    first_chat_count = sum(1 for item in calls if item.get("messages"))
    ShortTitleAssetService().prepare(manifest=ShortsManifest(1, "shorts", src.path, "fp", 5, 1, 120), candidates=[candidate()], **kwargs)
    assert sum(1 for item in calls if item.get("messages")) == first_chat_count
