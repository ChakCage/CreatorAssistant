from ai_editor_copilot.domain.models import StyleProfile


def profile(name="narrative_short"):
    presets = {
        "narrative_short": dict(pacing="dynamic", average_shot_duration=3, visual_changes_per_minute=20),
        "dynamic_talking_head": dict(pacing="dynamic", average_shot_duration=2, visual_changes_per_minute=30,
            broll_density=0.5, meme_density=0.1, zoom_density=0.3, sfx_density=0.2, music_usage="background"),
        "travel_vlog": dict(average_shot_duration=5, visual_changes_per_minute=12),
        "gaming_fast": dict(pacing="dynamic", average_shot_duration=2, visual_changes_per_minute=30),
        "documentary": dict(pacing="slow", average_shot_duration=8, visual_changes_per_minute=7.5),
    }
    if name not in presets:
        raise ValueError("unknown style profile: " + name)
    return StyleProfile(id=name, name=name.replace("_", " "), **presets[name])
