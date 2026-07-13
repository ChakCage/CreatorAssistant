from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set

from creator_assistant.domain.errors import (
    JobCancelledError,
    VegasProjectError,
    VegasProjectValidationError,
    VegasScriptTimeoutError,
)
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.settings_store import local_data_root
from creator_assistant.infrastructure.windows_paths import safe_file_name


SCRIPT_NAME = "CreateCreatorAssistantProject.cs"
RESULT_TIMEOUT_SECONDS = 240


@dataclass(frozen=True)
class VegasProjectResult:
    path: Path
    video_track_count: int
    audio_track_count: int
    video_events: int
    audio_events: int
    max_audio_events: int
    video_media_path: str
    audio_media_path: str
    video_start_nanos: int
    audio_start_nanos: int
    width: int
    height: int
    fps: float
    details: Dict[str, Any]

    @property
    def tracks(self) -> int:
        return self.video_track_count + self.audio_track_count


class VegasService:
    def __init__(self, vegas_path: str = "", script_dir: Optional[Path] = None) -> None:
        self.vegas_path = vegas_path
        self.script_dir = script_dir or (local_data_root() / "tools" / "vegas")

    @property
    def available(self) -> bool:
        path = Path(self.vegas_path) if self.vegas_path else Path()
        return path.is_file() and path.with_name("ScriptPortal.Vegas.dll").is_file()

    @property
    def script_path(self) -> Path:
        return self.script_dir / SCRIPT_NAME

    def ensure_script(self) -> Path:
        self.script_dir.mkdir(parents=True, exist_ok=True)
        path = self.script_path
        if not path.is_file() or path.read_text(encoding="utf-8") != VEGAS_SCRIPT:
            path.write_text(VEGAS_SCRIPT, encoding="utf-8")
        return path

    @staticmethod
    def project_path(project_root: Path, title: str) -> Path:
        return project_root / safe_file_name(title, "{title}", "veg")

    @staticmethod
    def validate_project(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    def create_project(
        self,
        *,
        output: Path,
        max_video: Path,
        instrumental: Path,
        duration: float,
        temp_dir: Path,
        cancellation: CancellationToken,
        auto_open: bool = False,
        job_id: str = "",
    ) -> VegasProjectResult:
        del duration, auto_open  # VEGAS reads exact stream lengths; opening happens only after COMPLETED.
        if not self.available:
            raise VegasProjectError("VEGAS Pro или ScriptPortal.Vegas.dll не найден.")
        if output.exists() and output.stat().st_size > 0:
            return VegasProjectResult(
                output, 0, 0, 0, 0, 0, "", "", 0, 0, 0, 0, 0.0,
                {"status": "EXISTS", "created_by_creator_assistant": False},
            )
        if output.exists():
            output.replace(self._unique_backup_path(output))
        for media in (max_video, instrumental):
            if not media.is_file() or media.stat().st_size == 0:
                raise VegasProjectValidationError("Не найден медиафайл для проекта VEGAS: " + str(media))

        cancellation.raise_if_cancelled()
        temp_dir = temp_dir.resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        script_path = self.ensure_script().resolve()
        job_path = temp_dir / "vegas_project_job.json"
        result_path = temp_dir / "vegas_result.json"
        preexisting_processes = self._running_vegas_processes()
        if preexisting_processes:
            raise VegasProjectError(
                "VEGAS Pro уже запущен. В VEGAS 22 build 248 фоновые дорожки недоступны — закройте VEGAS и повторите этап.",
                "Существующие процессы не остановлены: " + ", ".join(str(pid) for pid in sorted(preexisting_processes)),
            )
        close_dedicated = True
        job = {
            "schema_version": 1,
            "job_id": job_id or uuid.uuid4().hex,
            "video_path": str(max_video.resolve()),
            "instrumental_path": str(instrumental.resolve()),
            "output_project_path": str(output.resolve()),
            "result_path": str(result_path.resolve()),
            "close_dedicated_vegas_after_completion": close_dedicated,
            "use_active_dedicated_project": True,
            "dedicated_pid": 0,
        }
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        result_path.unlink(missing_ok=True)
        command = [
            str(self.vegas_path),
            "/NOLOGO",
            "/SCRIPTARGS",
            f"job={job_path}",
            f"-SCRIPT:{script_path}",
        ]
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["CREATOR_ASSISTANT_VEGAS_JOB"] = str(job_path)
        process = subprocess.Popen(
            command,
            cwd=str(Path(self.vegas_path).parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
            env=env,
        )
        job["dedicated_pid"] = process.pid
        updated_job = job_path.with_suffix(".pid.tmp")
        updated_job.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(updated_job), str(job_path))
        deadline = time.monotonic() + RESULT_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                if cancellation.is_cancelled:
                    raise JobCancelledError("Операция отменена пользователем.")
                if result_path.is_file() and result_path.stat().st_size > 0:
                    return self._read_result(result_path, output.resolve(), max_video.resolve(), instrumental.resolve())
                code = process.poll()
                if code is not None:
                    raise VegasProjectError("VEGAS завершился до создания проекта.", f"Код выхода: {code}")
                time.sleep(0.25)
            raise VegasScriptTimeoutError(
                "VEGAS Pro был запущен, но проект не был создан за отведённое время.",
                str(result_path),
            )
        finally:
            # This is the exact process handle launched for this job. Never enumerate or
            # stop a pre-existing VEGAS process owned by the user.
            if process.poll() is None and process.pid not in preexisting_processes:
                try:
                    process.terminate()
                except OSError:
                    pass

    @staticmethod
    def _unique_backup_path(output: Path) -> Path:
        candidate = output.with_suffix(output.suffix + ".bak")
        number = 2
        while candidate.exists():
            candidate = output.with_suffix(output.suffix + f".{number}.bak")
            number += 1
        return candidate

    def _running_vegas_processes(self) -> Set[int]:
        if os.name != "nt" or not self.vegas_path:
            return set()
        image = Path(self.vegas_path).name
        try:
            completed = subprocess.run(
                ["tasklist.exe", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        found: Set[int] = set()
        for line in completed.stdout.splitlines():
            columns = [part.strip().strip('"') for part in line.split('","')]
            if len(columns) >= 2 and columns[0].casefold() == image.casefold():
                try:
                    found.add(int(columns[1].replace(",", "")))
                except ValueError:
                    continue
        return found

    def _read_result(
        self,
        result_path: Path,
        expected_output: Path,
        expected_video: Optional[Path] = None,
        expected_audio: Optional[Path] = None,
    ) -> VegasProjectResult:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise VegasProjectError("VEGAS вернул нечитаемый result JSON.", str(exc)) from exc
        if not data.get("success"):
            raise VegasProjectError(str(data.get("error") or "Скрипт VEGAS завершился с ошибкой."), str(data.get("details") or ""))
        output = Path(str(data.get("project_path") or data.get("output") or expected_output)).resolve()
        video_tracks = int(data.get("video_track_count") or 0)
        audio_tracks = int(data.get("audio_track_count") or 0)
        video_events = int(data.get("video_event_count") or data.get("video_events") or 0)
        audio_events = int(data.get("audio_event_count") or data.get("audio_events") or 0)
        max_audio_events = int(data.get("max_audio_events") or 0)
        video_media = str(data.get("video_media_path") or "")
        audio_media = str(data.get("audio_media_path") or "")
        original_audio = bool(data.get("original_video_audio_added", max_audio_events > 0))
        video_start = int(data.get("video_start_nanos") or 0)
        audio_start = int(data.get("audio_start_nanos") or 0)
        if not self._same_path(output, expected_output) or not self.validate_project(output):
            raise VegasProjectValidationError("Проект VEGAS не создан по ожидаемому пути: " + str(expected_output))
        if (video_tracks, audio_tracks, video_events, audio_events) != (1, 1, 1, 1):
            raise VegasProjectValidationError("Проверка VEGAS не пройдена: ожидаются ровно две дорожки и два события.")
        if max_audio_events != 0 or original_audio or video_start != 0 or audio_start != 0:
            raise VegasProjectValidationError("Проверка VEGAS не пройдена: обнаружен звук MAX или ненулевое начало события.")
        if expected_video is not None and not self._same_path(Path(video_media), expected_video):
            raise VegasProjectValidationError("Видеособытие VEGAS ссылается не на MAX_VIDEO.")
        if expected_audio is not None and not self._same_path(Path(audio_media), expected_audio):
            raise VegasProjectValidationError("Аудиособытие VEGAS ссылается не на Instrumental.")
        return VegasProjectResult(
            output, video_tracks, audio_tracks, video_events, audio_events, max_audio_events,
            video_media, audio_media, video_start, audio_start,
            int(data.get("width") or 0), int(data.get("height") or 0), float(data.get("fps") or 0.0), data,
        )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


VEGAS_SCRIPT = r'''
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using ScriptPortal.Vegas;

public class EntryPoint
{
    public void FromVegas(Vegas vegas)
    {
        string resultPath = "";
        bool closeDedicated = false;
        try
        {
            string job = ReadReadyJob(FindJobPath());
            resultPath = Text(job, "result_path");
            string videoPath = FullPath(Text(job, "video_path"));
            string instrumentalPath = FullPath(Text(job, "instrumental_path"));
            string outputPath = FullPath(Text(job, "output_project_path"));
            closeDedicated = BooleanValue(job, "close_dedicated_vegas_after_completion");
            bool useActiveDedicated = BooleanValue(job, "use_active_dedicated_project");
            int dedicatedPid = IntegerValue(job, "dedicated_pid");
            RequireFile(videoPath, "MAX video");
            RequireFile(instrumentalPath, "Instrumental");

            if (useActiveDedicated && Process.GetCurrentProcess().Id != dedicatedPid)
                throw new ApplicationException("Refusing to replace a project in a VEGAS process not launched by Creator Assistant.");
            Project project;
            if (useActiveDedicated)
            {
                if (!vegas.NewProject(false, false)) throw new ApplicationException("VEGAS could not initialize the dedicated empty project.");
                project = vegas.Project;
            }
            else project = vegas.CreateEmptyProject();
                Media maxMedia = Media.CreateInstance(project, videoPath);
                Media instrumentalMedia = Media.CreateInstance(project, instrumentalPath);
                VideoStream videoStream = maxMedia.GetVideoStreamByIndex(0);
                AudioStream audioStream = instrumentalMedia.GetAudioStreamByIndex(0);
                if (videoStream == null) throw new ApplicationException("MAX media has no video stream.");
                if (audioStream == null) throw new ApplicationException("Instrumental media has no audio stream.");

                project.Video.MatchProjectSettingsWithMedia(maxMedia);
                project.Audio.MatchProjectSettingsWithMedia(instrumentalMedia);
                VideoTrack videoTrack = project.AddVideoTrack();
                videoTrack.Name = "\u0412\u0438\u0434\u0435\u043e";
                AudioTrack audioTrack = project.AddAudioTrack();
                audioTrack.Name = "\u0418\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442\u0430\u043b";
                Timecode zero = Timecode.FromNanos(0);
                VideoEvent videoEvent = videoTrack.AddVideoEvent(zero, maxMedia.Length);
                Take videoTake = videoEvent.AddTake(videoStream, true);
                AudioEvent audioEvent = audioTrack.AddAudioEvent(zero, instrumentalMedia.Length);
                Take audioTake = audioEvent.AddTake(audioStream, true);

                ValidateTimeline(project, videoEvent, audioEvent, videoTake, audioTake, videoPath, instrumentalPath);
                project.SaveProject(outputPath);
                if (!File.Exists(outputPath) || new FileInfo(outputPath).Length == 0)
                    throw new ApplicationException("Project.SaveProject did not create a non-empty VEG file.");
                if (!String.IsNullOrEmpty(project.FilePath) && !PathEquals(project.FilePath, outputPath))
                    throw new ApplicationException("Saved project path does not match requested output path.");
                WriteSuccess(resultPath, vegas, project, videoEvent, audioEvent, videoTake, audioTake, outputPath, videoPath, instrumentalPath);
        }
        catch (Exception ex)
        {
            if (!String.IsNullOrEmpty(resultPath)) WriteFailure(resultPath, ex);
        }
        finally
        {
            if (closeDedicated) vegas.Exit();
        }
    }

    private static void ValidateTimeline(Project project, VideoEvent videoEvent, AudioEvent audioEvent, Take videoTake, Take audioTake, string videoPath, string instrumentalPath)
    {
        int videoTracks = 0, audioTracks = 0, videoEvents = 0, audioEvents = 0, maxAudioEvents = 0;
        foreach (Track track in project.Tracks)
        {
            if (track.IsVideo()) videoTracks++;
            if (track.IsAudio()) audioTracks++;
            foreach (TrackEvent ev in track.Events)
            {
                if (ev.IsVideo()) videoEvents++;
                if (ev.IsAudio())
                {
                    audioEvents++;
                    foreach (Take take in ev.Takes)
                        if (PathEquals(take.MediaPath, videoPath)) maxAudioEvents++;
                }
            }
        }
        if (videoTracks != 1 || audioTracks != 1 || videoEvents != 1 || audioEvents != 1)
            throw new ApplicationException("Timeline must contain one video track/event and one audio track/event.");
        if (videoEvent.Start.Nanos != 0 || audioEvent.Start.Nanos != 0)
            throw new ApplicationException("Timeline events must start at zero.");
        if (!PathEquals(videoTake.MediaPath, videoPath))
            throw new ApplicationException("Video event does not reference expected MAX media.");
        if (!PathEquals(audioTake.MediaPath, instrumentalPath))
            throw new ApplicationException("Audio event does not reference expected Instrumental media.");
        if (maxAudioEvents != 0)
            throw new ApplicationException("An audio event references the MAX media file.");
    }

    private static void WriteSuccess(string resultPath, Vegas vegas, Project project, VideoEvent videoEvent, AudioEvent audioEvent, Take videoTake, Take audioTake, string outputPath, string videoPath, string instrumentalPath)
    {
        Dictionary<string, object> result = new Dictionary<string, object>();
        result["success"] = true;
        result["project_path"] = outputPath;
        result["video_track_count"] = 1;
        result["audio_track_count"] = 1;
        result["video_event_count"] = 1;
        result["audio_event_count"] = 1;
        result["video_media_path"] = videoTake.MediaPath;
        result["audio_media_path"] = audioTake.MediaPath;
        result["max_audio_events"] = 0;
        result["original_video_audio_added"] = false;
        result["video_start_nanos"] = videoEvent.Start.Nanos;
        result["audio_start_nanos"] = audioEvent.Start.Nanos;
        result["width"] = project.Video.Width;
        result["height"] = project.Video.Height;
        result["fps"] = project.Video.FrameRate;
        result["vegas_version"] = vegas.Version;
        result["vegas_build"] = vegas.BuildNumber;
        result["video_path"] = videoPath;
        result["instrumental_path"] = instrumentalPath;
        WriteJson(resultPath, result);
    }

    private static string FindJobPath()
    {
        string env = Environment.GetEnvironmentVariable("CREATOR_ASSISTANT_VEGAS_JOB");
        if (!String.IsNullOrEmpty(env)) return env;
        try
        {
            if (Script.Args != null && Script.Args.Exists("job", true)) return Script.Args.ValueOf("job", true);
        }
        catch {}
        foreach (string arg in Environment.GetCommandLineArgs())
        {
            if (arg.StartsWith("job=", StringComparison.OrdinalIgnoreCase)) return arg.Substring(4).Trim('"');
            if (arg.EndsWith(".json", StringComparison.OrdinalIgnoreCase) && File.Exists(arg)) return arg;
        }
        throw new ApplicationException("Creator Assistant job JSON was not provided.");
    }

    private static string ReadJson(string path)
    {
        return File.ReadAllText(path, Encoding.UTF8);
    }

    private static string ReadReadyJob(string path)
    {
        Exception lastError = null;
        for (int attempt = 0; attempt < 200; attempt++)
        {
            try
            {
                string json = ReadJson(path);
                if (IntegerValue(json, "dedicated_pid") > 0) return json;
            }
            catch (Exception ex)
            {
                lastError = ex;
            }
            Thread.Sleep(25);
        }
        throw new ApplicationException("Creator Assistant job JSON was not ready before the VEGAS script started.", lastError);
    }

    private static int ValueStart(string json, string key)
    {
        string marker = "\"" + key + "\"";
        int keyAt = json.IndexOf(marker, StringComparison.Ordinal);
        if (keyAt < 0) throw new ApplicationException("Missing JSON key: " + key);
        int colon = json.IndexOf(':', keyAt + marker.Length);
        if (colon < 0) throw new ApplicationException("Invalid JSON value for: " + key);
        int valueAt = colon + 1;
        while (valueAt < json.Length && Char.IsWhiteSpace(json[valueAt])) valueAt++;
        return valueAt;
    }

    private static string Text(string json, string key)
    {
        int valueAt = ValueStart(json, key);
        if (valueAt >= json.Length || json[valueAt] != '"') throw new ApplicationException("JSON string expected for: " + key);
        StringBuilder value = new StringBuilder();
        bool escaped = false;
        for (int index = valueAt + 1; index < json.Length; index++)
        {
            char current = json[index];
            if (!escaped && current == '"') return value.ToString();
            if (!escaped && current == '\\')
            {
                escaped = true;
                continue;
            }
            if (!escaped)
            {
                value.Append(current);
                continue;
            }
            escaped = false;
            switch (current)
            {
                case '"': value.Append('"'); break;
                case '\\': value.Append('\\'); break;
                case '/': value.Append('/'); break;
                case 'b': value.Append('\b'); break;
                case 'f': value.Append('\f'); break;
                case 'n': value.Append('\n'); break;
                case 'r': value.Append('\r'); break;
                case 't': value.Append('\t'); break;
                case 'u':
                    if (index + 4 >= json.Length) throw new ApplicationException("Invalid Unicode escape for: " + key);
                    value.Append((char)Int32.Parse(json.Substring(index + 1, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture));
                    index += 4;
                    break;
                default: throw new ApplicationException("Invalid JSON escape for: " + key);
            }
        }
        throw new ApplicationException("Unterminated JSON string for: " + key);
    }

    private static bool BooleanValue(string json, string key)
    {
        int valueAt = ValueStart(json, key);
        if (json.Substring(valueAt).StartsWith("true", StringComparison.Ordinal)) return true;
        if (json.Substring(valueAt).StartsWith("false", StringComparison.Ordinal)) return false;
        throw new ApplicationException("JSON boolean expected for: " + key);
    }

    private static int IntegerValue(string json, string key)
    {
        int valueAt = ValueStart(json, key);
        int end = valueAt;
        while (end < json.Length && (Char.IsDigit(json[end]) || json[end] == '-')) end++;
        int value;
        if (end == valueAt || !Int32.TryParse(json.Substring(valueAt, end - valueAt), NumberStyles.Integer, CultureInfo.InvariantCulture, out value))
            throw new ApplicationException("JSON integer expected for: " + key);
        return value;
    }

    private static string FullPath(string path) { return Path.GetFullPath(path); }

    private static void RequireFile(string path, string role)
    {
        if (!File.Exists(path) || new FileInfo(path).Length == 0) throw new FileNotFoundException(role + " is missing or empty.", path);
    }

    private static bool PathEquals(string left, string right)
    {
        return String.Equals(FullPath(left), FullPath(right), StringComparison.OrdinalIgnoreCase);
    }

    private static void WriteFailure(string path, Exception ex)
    {
        Dictionary<string, object> result = new Dictionary<string, object>();
        result["success"] = false;
        result["error"] = ex.Message;
        result["details"] = ex.ToString();
        WriteJson(path, result);
    }

    private static void WriteJson(string path, Dictionary<string, object> result)
    {
        StringBuilder json = new StringBuilder();
        json.Append('{');
        bool first = true;
        foreach (KeyValuePair<string, object> item in result)
        {
            if (!first) json.Append(',');
            first = false;
            json.Append('"').Append(JsonEscape(item.Key)).Append("\":");
            if (item.Value == null) json.Append("null");
            else if (item.Value is Boolean) json.Append((Boolean)item.Value ? "true" : "false");
            else if (item.Value is Byte || item.Value is Int16 || item.Value is Int32 || item.Value is Int64 || item.Value is Single || item.Value is Double || item.Value is Decimal)
                json.Append(Convert.ToString(item.Value, CultureInfo.InvariantCulture));
            else json.Append('"').Append(JsonEscape(Convert.ToString(item.Value))).Append('"');
        }
        json.Append('}');
        File.WriteAllText(path, json.ToString(), Encoding.UTF8);
    }

    private static string JsonEscape(string value)
    {
        StringBuilder escaped = new StringBuilder();
        foreach (char current in value ?? "")
        {
            switch (current)
            {
                case '"': escaped.Append("\\\""); break;
                case '\\': escaped.Append("\\\\"); break;
                case '\b': escaped.Append("\\b"); break;
                case '\f': escaped.Append("\\f"); break;
                case '\n': escaped.Append("\\n"); break;
                case '\r': escaped.Append("\\r"); break;
                case '\t': escaped.Append("\\t"); break;
                default:
                    if (current < 32) escaped.Append("\\u").Append(((int)current).ToString("x4"));
                    else escaped.Append(current);
                    break;
            }
        }
        return escaped.ToString();
    }
}
'''
