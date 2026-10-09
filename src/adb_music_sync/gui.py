"""Main application window (PySide6)."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .controller import Controller
from .logging_setup import get_logger, setup_logging
from .models import AppState

log = get_logger()


def _fmt_bytes(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller):
        super().__init__()
        self.ctrl = controller
        self.ctrl.devices_changed.connect(self._on_devices)
        self.ctrl.storages_changed.connect(self._on_storages)
        self.ctrl.storages_status.connect(self._on_storages_status)
        self.ctrl.scan_changed.connect(self._on_scan)
        self.ctrl.plan_changed.connect(self._on_plan)
        self.ctrl.state_changed.connect(self._on_state)
        self.ctrl.log_message.connect(self._log)
        self.ctrl.progress_changed.connect(self._on_progress)
        self.ctrl.catalog_busy_changed.connect(lambda _: self._apply_state())
        self.ctrl.catalog_exported.connect(self._on_catalog_exported)
        self.ctrl.catalog_failed.connect(
            lambda message: QMessageBox.warning(self, APP_NAME, message)
        )

        self._device_radios: dict[str, QRadioButton] = {}
        self._storage_radios: dict[str, QRadioButton] = {}
        self._device_group = QButtonGroup(self)
        self._storage_group = QButtonGroup(self)

        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self._build_ui()
        self._apply_state()

    # -- UI construction ---------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- ADB / device panel ---
        adb_box = QGroupBox("Устройство")
        adb_layout = QVBoxLayout(adb_box)
        self.adb_status = QLabel("ADB: —")
        adb_layout.addWidget(self.adb_status)
        adb_dev_row = QHBoxLayout()
        adb_dev_row.addWidget(QLabel("Подключённые устройства:"))
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.ctrl.refresh_devices)
        adb_dev_row.addWidget(self.refresh_btn)
        adb_layout.addLayout(adb_dev_row)
        self.device_container = QVBoxLayout()
        adb_layout.addLayout(self.device_container)
        self.device_hint = QLabel("")
        self.device_hint.setStyleSheet("color: #c09853")
        adb_layout.addWidget(self.device_hint)
        root.addWidget(adb_box)

        # --- storage panel ---
        stor_box = QGroupBox("Хранилище назначения")
        stor_layout = QVBoxLayout(stor_box)
        self.storage_container = QVBoxLayout()
        stor_layout.addLayout(self.storage_container)
        self.storage_status = QLabel("Сначала выберите устройство.")
        stor_layout.addWidget(self.storage_status)
        # destination
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Папка назначения:"))
        self.dest_edit = QLineEdit(self.ctrl.config.get("destination_path", "Music"))
        dest_row.addWidget(self.dest_edit)
        stor_layout.addLayout(dest_row)
        self.export_btn = QPushButton("Экспорт каталога для плейлистов…")
        self.export_btn.setToolTip(
            "Сохранить список музыки из выбранной папки устройства в JSON для ChatGPT / Poweramp"
        )
        self.export_btn.clicked.connect(self._export_catalog)
        stor_layout.addWidget(self.export_btn)
        root.addWidget(stor_box)

        # --- source panel ---
        src_box = QGroupBox("Музыка на ПК")
        src_layout = QGridLayout(src_box)
        self.src_edit = QLineEdit(self.ctrl.config.get("last_local_folder", ""))
        self.src_btn = QPushButton("Выбрать папку…")
        self.src_btn.clicked.connect(self._choose_source)
        self.scan_btn = QPushButton("Проверить")
        self.scan_btn.clicked.connect(self._scan)
        src_layout.addWidget(self.src_edit, 0, 0, 1, 3)
        src_layout.addWidget(self.src_btn, 0, 3)
        src_layout.addWidget(self.scan_btn, 0, 4)
        self.src_summary = QLabel("")
        src_layout.addWidget(self.src_summary, 1, 0, 1, 5)
        # pre-check summary
        self.check_summary = QLabel("")
        src_layout.addWidget(self.check_summary, 2, 0, 1, 5)
        root.addWidget(src_box)

        # --- transfer panel ---
        tr_box = QGroupBox("Передача")
        tr_layout = QVBoxLayout(tr_box)
        self.current_file = QLabel("—")
        tr_layout.addWidget(self.current_file)
        self.counter = QLabel("0 / 0")
        tr_layout.addWidget(self.counter)
        self.prog_files = QProgressBar()
        self.prog_files.setFormat("Файлы: %v / %m")
        tr_layout.addWidget(self.prog_files)
        self.speed = QLabel("")
        tr_layout.addWidget(self.speed)
        self.stats = QLabel("Успешно: 0 | Пропущено: 0 | Ошибок: 0")
        tr_layout.addWidget(self.stats)

        btns = QHBoxLayout()
        self.start_btn = QPushButton("Начать")
        self.pause_btn = QPushButton("Пауза")
        self.resume_btn = QPushButton("Продолжить")
        self.cancel_btn = QPushButton("Отменить")
        self.retry_btn = QPushButton("Повторить ошибки")
        self.start_btn.clicked.connect(self._start)
        self.pause_btn.clicked.connect(self.ctrl.pause_transfer)
        self.resume_btn.clicked.connect(self.ctrl.resume_transfer)
        self.cancel_btn.clicked.connect(self.ctrl.cancel_transfer)
        self.retry_btn.clicked.connect(self.ctrl.retry_errors)
        for b in (self.start_btn, self.pause_btn, self.resume_btn, self.cancel_btn, self.retry_btn):
            btns.addWidget(b)
        tr_layout.addLayout(btns)
        root.addWidget(tr_box)

        # --- log panel ---
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        root.addWidget(QLabel("Лог:"))
        root.addWidget(self.log_view, 1)

    # -- slots -------------------------------------------------------------
    def _on_devices(self, devices) -> None:
        # clear old radios
        while self._device_group.buttons():
            self._device_group.removeButton(self._device_group.buttons()[0])
        while self.device_container.count():
            item = self.device_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._device_radios.clear()

        if not devices:
            self.adb_status.setText("ADB: устройств не найдено")
            self.device_hint.setText("")
            return
        self.adb_status.setText("ADB: готов")
        self.device_hint.setText("")
        has_unauthorized = False
        selected_serial = self.ctrl.selected_device.serial if self.ctrl.selected_device else None
        for d in devices:
            # Human-readable: model -> "FiiO JM21", never "? — device".
            state_label = "подключено" if d.state.value == "device" else d.state.value
            label = f"{d.display_name} — {state_label}"
            rb = QRadioButton(label)
            self._device_radios[d.serial] = rb
            self._device_group.addButton(rb)
            self.device_container.addWidget(rb)
            # Small diagnostic subtitle: stable serial + live transport.
            detail_bits = []
            if d.stable_id and d.stable_id != d.display_name:
                detail_bits.append(f"Серийный номер: {d.stable_id}")
            if d.transport_id is not None:
                detail_bits.append(f"ADB transport: {d.transport_id}")
            if detail_bits:
                detail = QLabel("    " + "  ·  ".join(detail_bits))
                detail.setStyleSheet("color: #888; font-size: 9pt;")
                self.device_container.addWidget(detail)
            if d.serial == selected_serial:
                rb.setChecked(True)
            rb.toggled.connect(lambda checked, s=d.serial: checked and self.ctrl.select_device(s))
            if d.state.value == "unauthorized":
                has_unauthorized = True
        if has_unauthorized:
            self.device_hint.setText(
                "Разблокируйте Android-устройство и подтвердите разрешение USB-отладки."
            )
        # auto-select first ready device if none selected
        if self.ctrl.selected_device is None and devices:
            ready = [d for d in devices if d.state.value == "device"] or devices
            self.ctrl.select_device(ready[0].serial)

    def _on_storages_status(self, status: str) -> None:
        if status == "discovering":
            self.storage_status.setText("Определение хранилищ...")
        elif status == "error":
            self.storage_status.setText("Не удалось получить список хранилищ")

    def _on_storages(self, storages) -> None:
        while self._storage_group.buttons():
            self._storage_group.removeButton(self._storage_group.buttons()[0])
        while self.storage_container.count():
            item = self.storage_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._storage_radios.clear()
        if not storages:
            self.storage_status.setText("Хранилища не обнаружены.")
            return
        self.storage_status.setText("")
        for s in storages:
            rb = QRadioButton(
                f"{s.label}\n  {s.mount_path}\n  Свободно: {_fmt_bytes(s.free_bytes)}"
            )
            self._storage_radios[s.mount_path] = rb
            self._storage_group.addButton(rb)
            self.storage_container.addWidget(rb)
            rb.toggled.connect(
                lambda checked, m=s.mount_path: checked and self.ctrl.select_storage(m)
            )
        # select persisted storage (keyed by stable device id when available)
        dev = self.ctrl.selected_device
        dev_key = (dev.stable_id or dev.serial) if dev else ""
        saved = self.ctrl.config.get("storage_by_serial", {}).get(dev_key)
        if not saved and dev:
            saved = self.ctrl.config.get("storage_by_serial", {}).get(dev.serial)
        if saved and saved in self._storage_radios:
            self._storage_radios[saved].setChecked(True)
        elif storages:
            self._storage_radios[storages[0].mount_path].setChecked(True)

    def _on_scan(self, result) -> None:
        n = len(result.files)
        self.src_summary.setText(f"Найдено файлов: {n} | Объём: {_fmt_bytes(result.total_bytes)}")

    def _on_plan(self, plan) -> None:
        q = plan.to_transfer
        already = plan.total_count - len(q)
        self.check_summary.setText(
            f"Найдено файлов: {plan.total_count}\n"
            f"Уже на устройстве: {already}\n"
            f"Нужно передать: {len(q)}\n"
            f"Объём передачи: {_fmt_bytes(plan.to_transfer_bytes)}\n"
            f"Свободно: {_fmt_bytes(self.ctrl.selected_storage.free_bytes) if self.ctrl.selected_storage else '—'}"
        )

    def _on_state(self, state: str) -> None:
        self._apply_state(state)

    def _apply_state(self, state: str | None = None) -> None:
        s = AppState(state) if state else self.ctrl.state
        busy = self.ctrl.catalog_busy
        transferring = s in (AppState.TRANSFERRING, AppState.SCANNING)
        running = s is AppState.TRANSFERRING
        # "Начать" is only meaningful once a transfer plan has been built
        # successfully and a valid engine is armed (state == READY). During
        # DISCONNECTED/FAILED there is no engine — keep it disabled so the user
        # re-runs "Проверить" instead of hitting a silent `adb exited 1`.
        self.start_btn.setEnabled(not busy and s is AppState.READY and self.ctrl.engine is not None)
        self.pause_btn.setEnabled(running)
        self.resume_btn.setEnabled(s is AppState.PAUSED)
        self.cancel_btn.setEnabled(running or s is AppState.PAUSED or s is AppState.CANCELLING)
        self.retry_btn.setEnabled(not busy and s is AppState.FAILED)
        self.scan_btn.setEnabled(not busy and not transferring)
        self.refresh_btn.setEnabled(not busy and not running)
        self.export_btn.setEnabled(
            not busy
            and s
            not in (AppState.SCANNING, AppState.TRANSFERRING, AppState.PAUSED, AppState.CANCELLING)
        )
        self.export_btn.setText("Чтение каталога…" if busy else "Экспорт каталога для плейлистов…")
        self.dest_edit.setEnabled(not busy)
        for radio in (*self._device_radios.values(), *self._storage_radios.values()):
            radio.setEnabled(not busy)

    def _on_progress(self, progress) -> None:
        total = progress.total_files
        self.counter.setText(f"{progress.current_index} / {total}")
        self.current_file.setText(progress.current_file or "—")
        self.stats.setText(
            f"Успешно: {progress.transferred_files}"
            f" | Пропущено: {progress.skipped_files}"
            f" | Ошибок: {progress.error_files}"
        )

    def _log(self, msg: str) -> None:
        self.log_view.appendPlainText(f"[{self._now()}] {msg}")
        log.info("%s", msg)

    @staticmethod
    def _now() -> str:
        from datetime import datetime

        return datetime.now().strftime("%H:%M:%S")

    # -- user actions ------------------------------------------------------
    def _export_catalog(self) -> None:
        if (
            self.ctrl.selected_device is None
            or not self.ctrl.selected_device.is_ready
            or self.ctrl.selected_storage is None
        ):
            QMessageBox.warning(self, APP_NAME, "Выберите подключённое устройство и хранилище.")
            return
        output, _ = QFileDialog.getSaveFileName(
            self, "Сохранить каталог музыки", "jm21_catalog.json", "JSON (*.json)"
        )
        if output:
            self.ctrl.export_catalog_async(self.dest_edit.text(), output)

    def _on_catalog_exported(self, output: str, count: int) -> None:
        QMessageBox.information(
            self,
            APP_NAME,
            f"Каталог сохранён: {output}\nТреков: {count}\n\n"
            "Пришлите этот JSON в ChatGPT для составления плейлистов Poweramp.\n"
            "Готовые M3U8 размещайте в подпапке ChatGPT_Playlists выбранной папки музыки.",
        )

    def _choose_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку с музыкой")
        if folder:
            self.src_edit.setText(folder)
            self.ctrl.config["last_local_folder"] = folder
            self.ctrl._save()

    def _scan(self) -> None:
        folder = self.src_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, APP_NAME, "Укажите папку с музыкой на ПК.")
            return
        # Single orchestrated flow: scan -> (controller auto) -> build plan -> READY.
        # The controller chains the plan-build after the scan callback, so we
        # must NOT call build_plan_async here (it would collide with the scan).
        self.ctrl.scan_library_async(folder, destination=self.dest_edit.text())

    def _start(self) -> None:
        self.ctrl.start_transfer(self.dest_edit.text())

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.ctrl.state in (AppState.TRANSFERRING, AppState.PAUSED):
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "Идёт передача. Завершить и остановить?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.ctrl.save_window_geometry(self.width(), self.height(), self.x(), self.y())
        self.ctrl.shutdown()
        event.accept()


def run() -> int:
    setup_logging()
    log.info("Starting %s v%s", APP_NAME, __version__)
    app = QApplication(sys.argv)
    controller = Controller()
    window = MainWindow(controller)

    # restore geometry
    w = controller.config.get("window", {})
    if w.get("width") and w.get("height"):
        window.resize(w["width"], w["height"])
        if w.get("x") is not None:
            window.move(w["x"], w["y"])

    window.show()
    # initial device scan
    controller.refresh_devices()
    return app.exec()
