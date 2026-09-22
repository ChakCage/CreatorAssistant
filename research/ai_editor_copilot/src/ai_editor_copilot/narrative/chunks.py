"""Lossless sentence/segment chunking with bounded overlap and explicit coverage."""
import json
from ai_editor_copilot.planner.service import stable_id


def payload(segments):
    return [{"id": s.id, "start": s.range.start, "end": s.range.end, "text": s.text} for s in segments]


def make_chunks(source, max_chars=10000, max_seconds=360, overlap_segments=3):
    if max_chars < 500 or max_seconds <= 0 or overlap_segments < 0:
        raise ValueError("invalid chunk budget")
    chunks, start = [], 0
    while start < len(source.segments):
        end = start
        while end < len(source.segments):
            trial = source.segments[start:end + 1]
            chars = len(json.dumps(payload(trial), ensure_ascii=False))
            if chars > max_chars or trial[-1].range.end - trial[0].range.start > max_seconds:
                break
            end += 1
        if end == start:
            raise ValueError("single raw segment exceeds chunk budget; no silent truncation")
        selected = source.segments[start:end]
        chunks.append({"id": stable_id("chunk_", [source.transcript_sha256, [s.id for s in selected]]),
            "segments": payload(selected), "chars": len(json.dumps(payload(selected), ensure_ascii=False))})
        if end == len(source.segments):
            break
        start = max(start + 1, end - overlap_segments)
    return chunks


def union_length(ranges):
    end, total = 0.0, 0.0
    for start, stop in sorted(ranges):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def coverage(source, chunks, analyzed_ids, candidates=()):
    known = {c["id"] for c in chunks}
    if not set(analyzed_ids) <= known:
        raise ValueError("coverage contains unknown chunk")
    analyzed = [c for c in chunks if c["id"] in analyzed_ids]
    ids = {s["id"] for c in analyzed for s in c["segments"]}
    occupied = union_length([(s.range.start, s.range.end) for s in source.segments if s.id in ids])
    windows = union_length([(c["segments"][0]["start"], c["segments"][-1]["end"]) for c in analyzed])
    return {"source_duration": source.duration, "transcript_segments": len(source.segments),
        "total_chars": sum(len(s.text) for s in source.segments),
        "token_count": None, "token_count_note": "actual transport metrics recorded separately; no tokenizer estimate presented as actual",
        "chunks_produced": len(chunks), "chunks_analyzed": len(analyzed),
        "segments_analyzed": len(ids), "segment_coverage_percent": 100 * len(ids) / len(source.segments),
        "source_time_coverage_percent": 100 * windows / source.duration,
        "transcribed_speech_coverage_percent": 100 * occupied / source.duration,
        "full_transcript_analyzed": len(ids) == len(source.segments),
        "zero_duration_raw_segments": [s.id for s in source.segments if s.range.end == s.range.start],
        "empty_raw_segments": [s.id for s in source.segments if not s.text.strip()],
        "word_alignment_anomalies": [s.id for s in source.segments
            if any(w.start < s.range.start or w.end > s.range.end for w in s.words)],
        "unprocessed_segment_ids": [s.id for s in source.segments if s.id not in ids],
        "candidates_per_source_third": [sum(int(min(2, c.evidence.evidence_start / source.duration * 3)) == i for c in candidates) for i in range(3)],
        "coverage_note": "Time coverage is union of analyzed chunk windows, not a claim about untranscribed audio or visuals."}
