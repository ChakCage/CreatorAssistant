from __future__ import annotations

import multiprocessing
import importlib
import sys
import json
import time
import os
import subprocess
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from pathlib import Path

from creator_assistant.infrastructure.crash_logging import initialize_early

initialize_early()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QGroupBox, QMessageBox, QPushButton

from creator_assistant.app import ServiceContainer
from creator_assistant.ui.main_window import MainWindow
from creator_assistant.product import (
    AppEdition,
    Feature,
    FeatureRegistry,
    current_edition,
    qsettings_application_name,
)
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.domain.stages import JobStage, ORDERED_STAGES
from creator_assistant.infrastructure.crash_logging import exception as log_exception, install_qt_message_handler
from creator_assistant.infrastructure.build_info import current_build_info


STYLE = """
QWidget { background: #11151b; color: #e8edf3; font-family: "Segoe UI"; font-size: 10pt; }
QMainWindow, QDialog { background: #0d1117; }
QToolBar { background: #161c24; border: 0; border-bottom: 1px solid #2c3440; padding: 7px; spacing: 8px; }
QToolBar QLabel { color: #50d890; font-weight: 800; letter-spacing: 1px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #151b23; color: #aeb7c4; padding: 11px 20px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #63e6a5; border-bottom-color: #42d88b; }
QTabBar::tab:disabled { color: #56606d; }
QFrame#panel, QGroupBox { background: #151b23; border: 1px solid #29313d; border-radius: 9px; }
QGroupBox { margin-top: 12px; padding: 14px 9px 9px 9px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #cbd5e1; }
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QListWidget, QTableWidget { background: #0e1319; border: 1px solid #313b49; border-radius: 6px; padding: 7px; selection-background-color: #2b7652; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #42d88b; }
QPushButton { background: #252e3a; border: 1px solid #384454; border-radius: 6px; padding: 7px 13px; }
QPushButton:hover { background: #303c4b; border-color: #536276; }
QPushButton:pressed { background: #1d252f; }
QPushButton:disabled { color: #637080; background: #1a2028; }
QPushButton#primaryButton { background: #1b8c59; color: white; border-color: #38c982; font-weight: 700; padding: 10px; }
QPushButton#primaryButton:hover { background: #21a76a; }
QLabel#pageTitle { font-size: 21pt; font-weight: 750; color: #f4f7fb; }
QLabel#sectionTitle { font-size: 12pt; font-weight: 700; color: #dce4ee; }
QLabel#thumbnail { background: #090d12; border: 1px dashed #354151; border-radius: 7px; color: #738093; }
QFrame#infoCard { background: #10161d; border: 1px solid #27313d; border-radius: 7px; }
QLabel#infoValue { color: #63e6a5; font-size: 13pt; font-weight: 700; }
QLabel[class="muted"] { color: #8995a5; }
QLabel[class="errorText"] { color: #ff9197; font-size: 12pt; }
QProgressBar { border: 1px solid #303a47; border-radius: 6px; background: #0d1218; height: 17px; text-align: center; }
QProgressBar::chunk { background: #35c981; border-radius: 5px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QHeaderView::section { background: #202833; color: #bfc9d6; border: 0; padding: 7px; }
QScrollBar:vertical { background: #11161d; width: 11px; }
QScrollBar::handle:vertical { background: #344152; border-radius: 5px; min-height: 25px; }
"""


def _prepare_headless_console() -> None:
    """Attach the windowed PyInstaller build to its caller for CLI output."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        attached = bool(ctypes.windll.kernel32.AttachConsole(-1))
        if not attached:
            ctypes.windll.kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except (OSError, AttributeError):
        pass


def _proxy_ui_verification(window: MainWindow, container: ServiceContainer, report_path: Path) -> None:
    """Exercise live Save/Cancel behavior inside the packaged application."""
    tab = window.prep_tab
    original = deepcopy(container.settings)
    events = []
    signal_snapshots = []
    container.settings_service.settings_changed.connect(signal_snapshots.append)

    def snapshot(action: str, expected_height: int, signal_count_before: int) -> None:
        stage = tab.stage_list.item(ORDERED_STAGES.index(JobStage.CREATE_PROXY)).text()
        events.append({
            "action": action,
            "expected_height": expected_height,
            "setting": int(container.settings.get("reaper_proxy_height", 720)),
            "checkbox": tab.proxy_check.text(),
            "card": tab.info_captions["proxy_size"].text(),
            "stage": stage,
            "project_threads": len(tab._threads),
            "signals_emitted": len(signal_snapshots) - signal_count_before,
        })

    def save(height: int) -> None:
        dialog = SettingsDialog(container, window)
        dialog.proxy_height.setCurrentIndex(dialog.proxy_height.findData(height))
        signal_count_before = len(signal_snapshots)
        started = time.monotonic()
        dialog._save()
        tab.reload_authors()
        snapshot(f"save_{height}", height, signal_count_before)
        events[-1]["ui_latency_ms"] = container.settings_service.last_timing.get("ui_latency_ms")
        events[-1]["save_return_ms"] = (time.monotonic() - started) * 1000.0

    for height in (480, 720, 1080):
        save(height)

    cancelled = SettingsDialog(container, window)
    cancelled.proxy_height.setCurrentIndex(cancelled.proxy_height.findData(480))
    signal_count_before_cancel = len(signal_snapshots)
    cancelled.reject()
    snapshot("cancel_480", 1080, signal_count_before_cancel)

    for height in (480, 720, 1080, 480, 720, 1080, 480):
        save(height)

    saved_events = [event for event in events if event["action"].startswith("save_")]
    cancel_events = [event for event in events if event["action"].startswith("cancel_")]
    all_saved_under_200_ms = all(
        event.get("ui_latency_ms") is not None
        and float(event["ui_latency_ms"]) < 200
        and event.get("save_return_ms") is not None
        and float(event["save_return_ms"]) < 200
        for event in saved_events
    )
    report = {
        "pid": __import__("os").getpid(),
        "single_process": True,
        "events": events,
        "save_count": len(saved_events),
        "signal_count": len(signal_snapshots),
        "max_ui_latency_ms": max(float(event["ui_latency_ms"]) for event in saved_events),
        "max_save_return_ms": max(float(event["save_return_ms"]) for event in saved_events),
        "success": len(saved_events) >= 10
        and all(event["setting"] == event["expected_height"] for event in events)
        and all(event["signals_emitted"] == 1 for event in saved_events)
        and all(event["signals_emitted"] == 0 for event in cancel_events)
        and all(event["project_threads"] == 0 for event in events)
        and all(f"{event['setting']}p" in event["stage"] for event in events)
        and all_saved_under_200_ms,
        "all_saved_under_200_ms": all_saved_under_200_ms,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    container.save_settings(original)


def _template_ui_verification(window: MainWindow, report_path: Path) -> None:
    """Render and inspect the actual packaged Vertical Editor controls."""
    window.tabs.setCurrentWidget(window.shorts_tab)
    editor = window.shorts_tab.subtitle_editor
    window.shorts_tab.workspace.setCurrentWidget(editor)
    editor.settings_scroll.ensureWidgetVisible(editor.template_group, 20, 20)
    QApplication.processEvents()
    buttons = {
        "save": editor.template_save,
        "apply_all": editor.template_apply_all,
        "reset_short": editor.template_reset_short,
        "view": editor.template_view,
        "global_library": editor.global_templates,
    }
    image_path = report_path.with_suffix(".png")
    image_path.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(image_path), "PNG")
    build = current_build_info()
    settings = SettingsDialog(window.container, window)
    ai_verification = {
        "model": settings._selected_shorts_ai_model(),
        "timeout": settings.shorts_ai_timeout.value(),
        "warmup_timeout": settings.shorts_ai_warmup_timeout.value(),
        "strict_model": settings.shorts_ai_strict.isChecked(),
        "context": settings._selected_shorts_ai_context(),
        "global_template_selectors": len(settings.global_template_selectors),
    }
    settings.close()
    report = {
        "success": (
            all(button.isVisibleTo(window) and button.width() > 120 and button.height() > 20 for button in buttons.values())
            and ai_verification["model"] == "qwen3.6:35b-a3b"
            and ai_verification["timeout"] >= 600
            and ai_verification["strict_model"]
            and ai_verification["global_template_selectors"] == 3
        ),
        "window_title": window.windowTitle(),
        "executable": build.executable,
        "commit": build.commit,
        "build_date": build.build_date,
        "main_tab": window.tabs.tabText(window.tabs.currentIndex()),
        "shorts_tab": window.shorts_tab.workspace.tabText(window.shorts_tab.workspace.currentIndex()),
        "group_visible": editor.template_group.isVisibleTo(window),
        "ai": ai_verification,
        "buttons": {
            name: {
                "text": button.text(),
                "visible": button.isVisibleTo(window),
                "width": button.width(),
                "height": button.height(),
                "tooltip": button.toolTip(),
            }
            for name, button in buttons.items()
        },
        "screenshot": str(image_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _publishing_ui_verification(window: MainWindow, container: ServiceContainer, report_path: Path) -> None:
    """Capture actual packaged Autopilot, account settings and publishing queue."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    window.tabs.setCurrentWidget(window.autopilot_tab); QApplication.processEvents()
    autopilot_image = report_path.with_name(report_path.stem + "-autopilot.png")
    window.grab().save(str(autopilot_image), "PNG")
    groups = {item.title() for item in window.autopilot_tab.findChildren(QGroupBox)}
    buttons = {item.text() for item in window.autopilot_tab.findChildren(QPushButton)}
    settings = SettingsDialog(container, window); settings.show()
    if hasattr(settings, "publishing_accounts_panel"):
        settings.scroll_area.ensureWidgetVisible(settings.publishing_accounts_panel, 20, 20)
    QApplication.processEvents()
    settings_image = report_path.with_name(report_path.stem + "-accounts.png")
    settings.grab().save(str(settings_image), "PNG")
    accounts_visible = bool(hasattr(settings, "publishing_accounts_panel") and settings.publishing_accounts_panel.isVisibleTo(settings))
    settings.close()
    window.tabs.setCurrentWidget(window.publishing_queue_tab); QApplication.processEvents()
    queue_image = report_path.with_name(report_path.stem + "-queue.png")
    window.grab().save(str(queue_image), "PNG")
    required_groups = {"1. Источники", "2. Режим и профиль", "3. Отбор Shorts", "4. Оформление", "5. Расписание", "6. Подключённые платформы", "7. Задания — управление", "Результаты"}
    schedule_values = window.autopilot_tab.slots.values()
    schedule_preview = window.autopilot_tab.schedule_preview.text()
    upload_strategy = str(window.autopilot_tab.upload_strategy.currentData())
    report = {
        "success": (
            required_groups <= groups and accounts_visible
            and "Выбрать всех найденных кандидатов" in buttons
            and schedule_values == ["13:00", "19:00"]
            and "Short 001" in schedule_preview and "Short 010" in schedule_preview
            and upload_strategy == "REMOTE_SCHEDULE"
            and window.publishing_queue_tab.table.columnCount() == 13
        ),
        "autopilot_groups": sorted(groups), "account_settings_visible": accounts_visible,
        "schedule_slots": schedule_values, "schedule_preview": schedule_preview,
        "default_upload_strategy": upload_strategy,
        "publishing_queue_columns": window.publishing_queue_tab.table.columnCount(),
        "screenshots": {"autopilot": str(autopilot_image), "accounts": str(settings_image), "queue": str(queue_image)},
        "build": current_build_info().__dict__,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Settings diagnostics may still own a worker. The verification process has
    # already persisted its report and must not keep build.ps1 waiting.
    QTimer.singleShot(5000, lambda: os._exit(0))


def _edition_verification(window: MainWindow, container: ServiceContainer, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    QApplication.processEvents()
    window.tabs.setCurrentWidget(window.shorts_tab)
    editor = window.shorts_tab.subtitle_editor
    window.shorts_tab.workspace.setCurrentWidget(editor)
    editor.settings_scroll.ensureWidgetVisible(editor.branding_group, 20, 20)
    QApplication.processEvents()
    image_path = report_path.with_suffix(".png")
    window.grab().save(str(image_path), "PNG")
    tabs = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    edition = container.edition
    forbidden_prefixes = (
        "creator_assistant.ui.autopilot_tab",
        "creator_assistant.ui.publishing_queue",
        "creator_assistant.ui.publishing_accounts",
        "creator_assistant.services.automation",
        "creator_assistant.services.publishing",
        "creator_assistant.infrastructure.publishing_store",
        "creator_assistant.infrastructure.automation_job_store",
    )
    forbidden_loaded = sorted(
        name for name in sys.modules if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
    ) if edition is AppEdition.COMMERCIAL else []
    expected = (
        ["Подготовка проекта", "Shorts", "Автопилот", "Очередь публикаций"]
        if edition is AppEdition.DEVELOPER else ["Подготовка проекта", "Shorts"]
    )
    profiles = container.channel_assets.profiles()
    brand_buttons = {
        "add_image": editor.banner_import,
        "add_video": editor.banner_import_video,
        "select": editor.banner_select,
        "replace": editor.banner_replace,
        "delete": editor.banner_delete,
        "verify": editor.banner_verify,
        "refresh": editor.banner_refresh,
        "import_developer": editor.banner_import_developer,
    }
    brand_library_visible = (
        editor.branding_group.isVisibleTo(window)
        and all(button.isVisibleTo(window) and button.isEnabled() for button in brand_buttons.values())
    )
    report = {
        "success": tabs == expected and not forbidden_loaded and brand_library_visible,
        "edition": edition.value,
        "title": window.windowTitle(),
        "tabs": tabs,
        "features": sorted(item.value for item in FeatureRegistry.available_features(edition)),
        "forbidden_modules_loaded": forbidden_loaded,
        "settings_path": str(container.settings_store.path),
        "has_automation_engine": hasattr(container, "automation_engine"),
        "has_publishing_store": hasattr(container, "publishing_store"),
        "has_licensing": hasattr(container, "licensing"),
        "brand_library": {
            "root": str(container.channel_assets.library.root),
            "visible": brand_library_visible,
            "profile_count": len(profiles),
            "profiles": [profile.id for profile in profiles],
            "buttons": {name: button.text() for name, button in brand_buttons.items()},
        },
        "build": current_build_info().__dict__,
        "screenshot": str(image_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Some multimedia backends keep native worker threads alive after Qt quits.
    # The edition smoke report is complete, so do not leave a verification EXE running.
    QTimer.singleShot(1500, lambda: os._exit(0))


def _commercial_setup_verification(window: MainWindow, container: ServiceContainer, report_path: Path) -> None:
    """Inspect the actual packaged first-run wizard without modifying projects or downloading models."""
    module = importlib.import_module("creator_assistant.ui." + "commercial_setup_wizard")
    wizard = module.CommercialSetupWizard(container, window)
    wizard.show(); QApplication.processEvents()
    image_path = report_path.with_suffix(".png")
    image_path.parent.mkdir(parents=True, exist_ok=True)
    wizard.grab().save(str(image_path), "PNG")
    computer = container.commercial_setup.inspect()
    page_titles = [wizard.page(page_id).title() for page_id in wizard.pageIds()]
    forbidden = sorted(name for name in sys.modules if name.startswith((
        "creator_assistant.ui.autopilot_tab", "creator_assistant.ui.publishing_queue",
        "creator_assistant.services.automation", "creator_assistant.services.publishing",
    )))
    report = {
        "success": (
            container.edition is AppEdition.COMMERCIAL
            and not bool(container.settings.get("commercial_setup", {}).get("completed", False))
            and len(page_titles) == 11
            and set(wizard.profile_radios) == {"compact", "maximum_quality"}
            and not forbidden
        ),
        "isolated_first_run": not bool(container.settings.get("commercial_setup", {}).get("completed", False)),
        "pages": page_titles,
        "profiles": {key: value.model_id for key, value in module.AI_PROFILES.items()},
        "recommendation": computer.recommendation,
        "ollama_api": computer.ollama_api,
        "ollama_version": computer.ollama_version,
        "installed_supported_models": [
            str(item.get("name") or item.get("model")) for item in computer.installed_models
            if str(item.get("name") or item.get("model")) in {"qwen3:14b", "qwen3.6:35b-a3b"}
        ],
        "settings_path": str(container.settings_store.path),
        "forbidden_modules_loaded": forbidden,
        "screenshot": str(image_path),
        "build": current_build_info().__dict__,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    wizard.close()
    QTimer.singleShot(250, lambda: os._exit(0))


def _commercial_license_verification(container: ServiceContainer, report_path: Path) -> None:
    """Exercise the real packaged Commercial client against its sealed backend profile."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    code = os.environ.get("CREATOR_ASSISTANT_E2E_ACTIVATION_CODE", "")
    report = {"success": False, "build": current_build_info().__dict__}
    try:
        if container.edition is not AppEdition.COMMERCIAL or not code:
            raise RuntimeError("Commercial edition and CREATOR_ASSISTANT_E2E_ACTIVATION_CODE are required")
        before = container.feature_gate.status()
        activated = container.licensing.activate(code, device_name="Packaged E2E")
        container.require_entitlement("shorts_analysis")
        # A new service instance reads the same Credential Manager entries,
        # proving that activation survives application restart.
        restarted_service = type(container.licensing)()
        restarted = restarted_service.status()
        refreshed = restarted_service.refresh()
        payload = restarted_service.storage.load().get("payload") or {}
        grace_boundary = datetime.fromisoformat(str(payload["offline_grace_until"]).replace("Z", "+00:00"))
        offline_clock = grace_boundary - timedelta(seconds=1)
        offline_service = type(container.licensing)(wall_clock=lambda: offline_clock)
        offline = offline_service.status(server_available=False)
        expired_clock = grace_boundary + timedelta(seconds=1)
        expired_service = type(container.licensing)(wall_clock=lambda: expired_clock)
        after_boundary = expired_service.status(server_available=False)
        feature_blocked = False
        try:
            type(container.feature_gate)(container.edition, expired_service).require("shorts_analysis")
        except Exception:
            feature_blocked = True
        recovered = restarted_service.refresh()
        preserve = os.environ.get("CREATOR_ASSISTANT_E2E_PRESERVE_LICENSE") == "1"
        if preserve:
            after_deactivate = refreshed
        else:
            restarted_service.deactivate_device(refreshed.device_id)
            after_deactivate = restarted_service.status()
        if os.environ.get("CREATOR_ASSISTANT_E2E_CREDENTIAL_NAMESPACE") and not preserve:
            restarted_service.storage.credentials.delete("installation", kind="id")
        report.update({
            "success": (
                not before.active and activated.active and restarted.active and refreshed.active
                and offline.active and offline.state.value == "OFFLINE_GRACE"
                and not after_boundary.active and feature_blocked and recovered.active
                and (after_deactivate.active if preserve else not after_deactivate.active)
            ),
            "before": before.state.value,
            "activated": {"state": activated.state.value, "plan": activated.plan, "expires_at": activated.expires_at},
            "restart": restarted.state.value,
            "refresh": refreshed.state.value,
            "offline_before_boundary": offline.state.value,
            "offline_after_boundary": after_boundary.state.value,
            "feature_gate_blocked_after_boundary": feature_blocked,
            "online_recovery": recovered.state.value,
            "offline_grace_boundary": grace_boundary.isoformat(),
            "after_deactivate": after_deactivate.state.value,
            "license_preserved_for_update": preserve,
            "backend": restarted_service.endpoint,
            "installation_stable": container.licensing.installation_id == restarted_service.installation_id,
        })
    except Exception as exc:
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        report["request_id"] = getattr(exc, "request_id", "")
        report["message"] = str(exc)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_e2e_verification(container: ServiceContainer, report_path: Path) -> None:
    """Use the same sealed update policy as the UI, then launch the verified installer."""
    from urllib.parse import urlparse
    from creator_assistant.infrastructure.release_updates import (
        ReleaseUpdateService, UpdatePolicy,
    )

    build = current_build_info()
    report = {"success": False, "from": build.__dict__}
    try:
        host = urlparse(build.update_backend_url).hostname or ""
        service = ReleaseUpdateService(
            build.update_backend_url,
            UpdatePolicy(
                edition=build.edition, channel=build.channel, architecture=build.architecture,
                current_version=build.version, current_build=build.build_number,
                public_keys=build.update_public_keys, allowed_hosts=(host,),
                allow_local_http=build.build_variant == "staging",
            ),
            busy_check=container.update_busy_check,
        )
        manifest = service.latest()
        if not manifest:
            raise RuntimeError("No matching update")
        installer = service.download(manifest, report_path.parent / "downloads")
        report.update({
            "success": True, "manifest": manifest.__dict__, "installer": str(installer),
            "installer_sha256_verified": True,
        })
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.environ.get("CREATOR_ASSISTANT_E2E_INSTALL_UPDATE") == "1":
            subprocess.Popen([
                str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
            ])
    except Exception as exc:
        report.update({
            "error": str(exc), "error_code": getattr(exc, "code", type(exc).__name__),
            "request_id": getattr(exc, "request_id", ""),
        })
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    multiprocessing.freeze_support()
    install_qt_message_handler()
    if "--publishing-agent" in sys.argv[1:]:
        if not FeatureRegistry.is_available(Feature.YOUTUBE_PUBLISHING):
            return 2
        _prepare_headless_console()
        try:
            os.environ["CREATOR_ASSISTANT_AGENT_MODE"] = "1"
            container = ServiceContainer()
            container.publishing_agent.run()
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            log_exception("Background publishing agent failed", exc)
            return 1
    automation_args = {"--run-job", "--resume-job", "--list-jobs", "--show-job"}
    if any(value in automation_args for value in sys.argv[1:]):
        _prepare_headless_console()
        try:
            if not FeatureRegistry.is_available(Feature.AUTOPILOT):
                return 2
            cli_module = importlib.import_module("creator_assistant.services." + "automation.cli")
            output = (lambda value: print(value)) if sys.stdout is not None else (lambda _value: None)
            container = ServiceContainer()
            return cli_module.run_automation_cli(sys.argv[1:], container.automation_engine, output)
        except Exception as exc:
            log_exception("Automation CLI failed", exc)
            if sys.stderr is not None:
                print(f"Automation CLI error: {exc}", file=sys.stderr)
            return 1
    app = QApplication(sys.argv)
    edition = current_edition()
    app.setApplicationName(qsettings_application_name())
    app.setApplicationDisplayName(edition.application_name)
    app.setOrganizationName("CreatorAssistant")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        container = ServiceContainer()
        commercial_setup_verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-commercial-setup=")),
            "",
        )
        if commercial_setup_verification_arg:
            container.first_run = False
        commercial_license_verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-commercial-license=")),
            "",
        )
        if commercial_license_verification_arg:
            container.first_run = False
        update_e2e_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-update=")),
            "",
        )
        if update_e2e_arg:
            container.first_run = False
        project_fixture_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-project-fixture=")),
            "",
        )
        if project_fixture_arg:
            container.first_run = False
        if "--smoke-test" in sys.argv:
            container.first_run = False
        window = MainWindow(container)
        window.show()
        verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-proxy-ui-log=")),
            "",
        )
        if verification_arg:
            container.first_run = False
            QTimer.singleShot(
                250,
                lambda: (
                    _proxy_ui_verification(window, container, Path(verification_arg)),
                    app.quit(),
                ),
            )
        template_verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-template-ui=")),
            "",
        )
        if template_verification_arg:
            container.first_run = False
            QTimer.singleShot(
                500,
                lambda: (
                    _template_ui_verification(window, Path(template_verification_arg)),
                    app.quit(),
                ),
            )
        publishing_verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-publishing-ui=")),
            "",
        )
        if publishing_verification_arg:
            container.first_run = False
            QTimer.singleShot(
                700,
                lambda: (
                    _publishing_ui_verification(window, container, Path(publishing_verification_arg)),
                    app.quit(),
                ),
            )
        edition_verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-edition=")),
            "",
        )
        if edition_verification_arg:
            container.first_run = False
            QTimer.singleShot(
                700,
                lambda: (
                    _edition_verification(window, container, Path(edition_verification_arg)),
                    app.quit(),
                ),
            )
        if commercial_setup_verification_arg:
            QTimer.singleShot(
                700,
                lambda: _commercial_setup_verification(window, container, Path(commercial_setup_verification_arg)),
            )
        if commercial_license_verification_arg:
            QTimer.singleShot(
                500,
                lambda: (
                    _commercial_license_verification(container, Path(commercial_license_verification_arg)),
                    app.quit(),
                ),
            )
        if update_e2e_arg:
            QTimer.singleShot(
                500,
                lambda: (
                    _update_e2e_verification(container, Path(update_e2e_arg)),
                    app.quit(),
                ),
            )
        if project_fixture_arg:
            def verify_project_fixture() -> None:
                from creator_assistant.infrastructure.packaged_project_fixture import run_packaged_project_fixture
                run_packaged_project_fixture(container.feature_gate, Path(project_fixture_arg))
                app.quit()
            QTimer.singleShot(500, verify_project_fixture)
        if "--smoke-test" in sys.argv:
            QTimer.singleShot(250, app.quit)
        return app.exec()
    except Exception as exc:
        log_exception("Application startup failed", exc)
        QMessageBox.critical(None, "Creator Assistant", f"Приложение не удалось запустить:\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
