"""Duplicate management tab; all device operations run on the shared worker."""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME
from .duplicates import MATCH_LABELS, DuplicateManager
from .models import AppState


class DuplicatesPane(QWidget):
    def __init__(self, controller, folder):
        super().__init__()
        self.ctrl = controller
        self.folder = folder
        self.scan = None
        self.scan_context = None
        self.session_context = None
        self.choices = []
        self.cancel = threading.Event()
        layout = QVBoxLayout(self)
        description = QLabel(
            "Точные дубли подтверждаются SHA-256. Повышение агрессивности добавляет возможные совпадения.\n"
            "Возможные дубли проверяйте вручную: это могут быть разные исполнения, форматы или мастеринги.\n"
            "Выберите папку музыки выше. Во время очистки закройте Poweramp."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        aggression_row = QHBoxLayout()
        aggression_row.addWidget(QLabel("Агрессивность:"))
        self.aggression = QSlider(Qt.Horizontal)
        self.aggression.setRange(0, 3)
        self.aggression.setSingleStep(1)
        self.aggression.setPageStep(1)
        self.aggression.setTickInterval(1)
        self.aggression.setTickPosition(QSlider.TicksBelow)
        self.aggression.setMinimumWidth(160)
        self.aggression.setToolTip(
            "Влево — только точные дубли; вправо — больше возможных совпадений"
        )
        aggression_row.addWidget(self.aggression, 1)
        self.aggression_label = QLabel(MATCH_LABELS[0])
        aggression_row.addWidget(self.aggression_label)
        layout.addLayout(aggression_row)
        self.aggression.valueChanged.connect(self._aggression_changed)
        row = QHBoxLayout()
        self.find_btn = QPushButton("Найти дубликаты")
        self.find_btn.clicked.connect(self._scan)
        row.addWidget(self.find_btn)
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.clicked.connect(lambda: self.cancel.set())
        row.addWidget(self.stop_btn)
        self.apply_btn = QPushButton("Переместить выбранные в карантин")
        self.apply_btn.clicked.connect(self._quarantine)
        row.addWidget(self.apply_btn)
        layout.addLayout(row)
        self.status = QLabel("Поиск ещё не выполнялся.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(
            [
                "Группа / путь файла",
                "Совпадение / длительность / размер",
                "Обрабатывать",
                "Оставить файл",
            ]
        )
        self.tree.setColumnWidth(0, 400)
        self.tree.setColumnWidth(1, 265)
        self.tree.setColumnWidth(2, 95)
        layout.addWidget(self.tree, 1)
        layout.addWidget(QLabel("Карантин на выбранном хранилище (доступен после перезапуска):"))
        row = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setMinimumContentsLength(25)
        row.addWidget(self.session_combo, 1)
        self.sessions_btn = QPushButton("Обновить карантин")
        self.sessions_btn.clicked.connect(self._sessions)
        row.addWidget(self.sessions_btn)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.restore_btn = QPushButton("Восстановить")
        self.restore_btn.clicked.connect(self._restore)
        row.addWidget(self.restore_btn)
        self.purge_btn = QPushButton("Удалить из карантина навсегда")
        self.purge_btn.clicked.connect(self._purge)
        row.addWidget(self.purge_btn)
        layout.addLayout(row)
        note = QLabel(
            "Карантин не освобождает место. Место освободится после окончательного удаления.\n"
            "Ссылки в файловых M3U/M3U8 внутри выбранной папки обновляются с резервной копией.\n"
            "После операции пересканируйте плейлисты Poweramp. Внутренние плейлисты его БД не изменяются."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.ctrl.maintenance_busy_changed.connect(self._busy_changed)
        self.ctrl.maintenance_failed.connect(self._failed)
        self.ctrl.maintenance_message.connect(self.status.setText)
        self.ctrl.state_changed.connect(lambda _: self._update_buttons())
        self.ctrl.catalog_busy_changed.connect(lambda _: self._update_buttons())
        self.session_combo.currentIndexChanged.connect(lambda _: self._update_buttons())
        self._update_buttons()

    def _context(self):
        d = self.ctrl.selected_device
        s = self.ctrl.selected_storage
        if self.ctrl.client is None or d is None or not d.is_ready or s is None:
            raise ValueError("Выберите подключённое устройство и хранилище.")
        return (d.stable_id or d.serial, d.selector, s.mount_path, self.folder().strip() or "Music")

    def _manager(self):
        context = self._context()
        return DuplicateManager(self.ctrl.client, context[2], context[1])

    def _run(self, work, done):
        try:
            self._context()
            self.cancel = threading.Event()
            self.ctrl.run_maintenance(work, done)
        except Exception as exc:
            self._failed(str(exc))

    def _busy_changed(self, busy):
        self._update_buttons()
        for checked, choice, _group in self.choices:
            checked.setEnabled(not busy)
            choice.setEnabled(not busy)

    def _update_buttons(self):
        idle = (
            not self.ctrl.maintenance_busy
            and not self.ctrl.catalog_busy
            and self.ctrl.state
            not in (AppState.TRANSFERRING, AppState.PAUSED, AppState.CANCELLING, AppState.SCANNING)
        )
        self.aggression.setEnabled(idle)
        self.find_btn.setEnabled(idle)
        self.apply_btn.setEnabled(
            idle
            and self.scan is not None
            and any(checked.isChecked() for checked, _, _ in self.choices)
        )
        self.sessions_btn.setEnabled(idle)
        self.session_combo.setEnabled(idle)
        state = self.session_combo.currentData()
        self.restore_btn.setEnabled(
            idle and bool(state) and state[1] in ("prepared", "quarantined", "restoring")
        )
        self.purge_btn.setEnabled(idle and bool(state) and state[1] in ("quarantined", "purging"))
        self.stop_btn.setEnabled(self.ctrl.maintenance_busy)

    def _failed(self, message):
        self.status.setText(
            message
            + "\nЕсли очистка была прервана, нажмите «Обновить карантин» и восстановите операцию."
        )
        QMessageBox.warning(self, APP_NAME, message)

    def _aggression_changed(self, value):
        self.aggression_label.setText(MATCH_LABELS[value])
        self.scan = None
        self.choices.clear()
        self.tree.clear()
        self.status.setText("Уровень изменён. Нажмите «Найти дубликаты» для нового сравнения.")
        self._update_buttons()

    def _scan(self):
        try:
            manager = self._manager()
            context = self._context()
        except Exception as exc:
            self._failed(str(exc))
            return
        level = self.aggression.value()
        self.scan = None
        self.tree.clear()
        self.choices.clear()
        self._update_buttons()

        def done(scan):
            self.scan = scan
            self.scan_context = context
            exact = sum(g.mode == "exact" for g in scan.groups)
            possible = len(scan.groups) - exact
            text = (
                f"Файлов: {scan.file_count}. Проверено хешей: {scan.hashed_count}. "
                f"Точных групп: {exact}. Возможных групп: {possible}.\n"
                f"Объём кандидатов при сохранении первого файла: {scan.redundant_bytes / 1024**2:.1f} MiB. "
                "Возможные совпадения не выбраны автоматически."
            )
            if scan.level:
                text += f"\nДлительность из Android доступна для {scan.duration_count}/{scan.file_count} файлов."
            if scan.level == 1:
                text += " Файлы без длительности участвуют только в проверке точных дублей."
            self.status.setText(text)
            modes = {
                "exact": "Точный SHA-256",
                "name_duration": "Название + длительность ±2 с",
                "name": "Одинаковое название",
                "similar_name": "Похожие названия ≥90%",
            }
            for index, group in enumerate(scan.groups, 1):
                kind = "Точные дубли" if group.mode == "exact" else "ВОЗМОЖНЫЕ совпадения"
                item = QTreeWidgetItem(
                    [f"{index}. {kind}: {len(group.paths)} файлов", modes[group.mode]]
                )
                if group.mode != "exact":
                    item.setToolTip(
                        0,
                        "Содержимое файлов различается. Проверяйте версии и качество перед очисткой.",
                    )
                self.tree.addTopLevelItem(item)
                checked = QCheckBox()
                checked.setChecked(group.mode == "exact")
                checked.toggled.connect(lambda _: self._update_buttons())
                self.tree.setItemWidget(item, 2, checked)
                choice = QComboBox()
                durations = dict(group.durations)
                for path in group.paths:
                    choice.addItem(path, path)
                    choice.setItemData(choice.count() - 1, path, Qt.ToolTipRole)
                    duration = durations.get(path)
                    duration_text = (
                        f"{duration // 60000}:{duration // 1000 % 60:02d}"
                        if duration is not None
                        else "длительность —"
                    )
                    size = group.size_for(path) / 1024**2
                    child = QTreeWidgetItem(item, [path, f"{duration_text} | {size:.1f} MiB"])
                    child.setToolTip(0, path)
                self.tree.setItemWidget(item, 3, choice)
                self.choices.append((checked, choice, group))
                item.setExpanded(True)
            self.tree.setColumnWidth(3, 340)
            self._update_buttons()

        self._run(
            lambda: manager.scan(
                context[3],
                level=level,
                progress=self.ctrl.maintenance_message.emit,
                cancel=self.cancel,
            ),
            done,
        )

    def _quarantine(self):
        try:
            if self._context() != self.scan_context:
                raise ValueError("Выбор устройства/папки изменился. Выполните поиск заново.")
            manager = self._manager()
            keep = {
                g.digest: choice.currentData()
                for checked, choice, g in self.choices
                if checked.isChecked()
            }
            if not keep:
                raise ValueError("Выберите группы для очистки.")
            count = sum(len(g.paths) - 1 for _, _, g in self.choices if g.digest in keep)
            possible = sum(g.mode != "exact" for _, _, g in self.choices if g.digest in keep)
        except Exception as exc:
            self._failed(str(exc))
            return
        if (
            QMessageBox.question(
                self,
                APP_NAME,
                f"Переместить {count} выбранных файлов в карантин и обновить файловые плейлисты?\n"
                + (
                    f"Вы выбрали {possible} групп ВОЗМОЖНЫХ совпадений: файлы могут содержать разные исполнения.\n"
                    if possible
                    else ""
                )
                + "Восстановление доступно во вкладке «Дубликаты». Закройте Poweramp перед операцией.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        scan = self.scan

        def done(session):
            self.status.setText(
                f"Копии в карантине: {session}\nПлейлисты обновлены. Пересканируйте их в Poweramp."
            )
            self._invalidate_plan()
            self.scan = None
            self.choices.clear()
            self.tree.clear()
            self.session_combo.clear()
            self.session_combo.addItem("Последняя очистка", (session, "quarantined"))
            self.session_context = self._context()[:3]
            self._update_buttons()

        self._invalidate_plan()
        self._run(
            lambda: manager.quarantine(
                scan, keep, progress=self.ctrl.maintenance_message.emit, cancel=self.cancel
            ),
            done,
        )

    def _invalidate_plan(self):
        self.ctrl.engine = None
        self.ctrl.plan = None
        self.ctrl.set_state(AppState.IDLE)

    def _sessions(self):
        try:
            manager = self._manager()
            context = self._context()[:3]
        except Exception as exc:
            self._failed(str(exc))
            return

        def done(sessions):
            self.session_combo.clear()
            self.session_context = context
            labels = {
                "prepared": "прерванная очистка",
                "quarantined": "в карантине",
                "restoring": "прерванное восстановление",
                "purging": "прерванное удаление",
            }
            for path, state, created, count in sessions:
                self.session_combo.addItem(
                    f"{created[:19]} — {count} копий — {labels.get(state, state)}", (path, state)
                )
            self.status.setText(f"Доступных операций в карантине: {len(sessions)}")
            self._update_buttons()

        self._run(manager.sessions, done)

    def _session_action(self, purge=False):
        try:
            if self._context()[:3] != self.session_context:
                raise ValueError("Хранилище/устройство изменилось. Обновите список карантина.")
            item = self.session_combo.currentData()
            if not item:
                return
            manager = self._manager()
        except Exception as exc:
            self._failed(str(exc))
            return
        message = (
            (
                "Удалить копии из выбранного карантина НАВСЕГДА? Восстановление будет невозможно.\n"
                "Сохранённые копии будут повторно проверены перед удалением."
            )
            if purge
            else (
                "Восстановить файлы и исходные плейлисты выбранной операции? Закройте Poweramp."
                "\nИзменённые после очистки файлы/плейлисты перезаписываться не будут."
            )
        )
        if (
            QMessageBox.question(
                self, APP_NAME, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            != QMessageBox.Yes
        ):
            return

        def done(_):
            self._invalidate_plan()
            self.scan = None
            self.choices.clear()
            self.tree.clear()
            self.session_combo.removeItem(self.session_combo.currentIndex())
            self.status.setText(
                "Карантин удалён. Место освобождено."
                if purge
                else "Файлы и плейлисты восстановлены. Пересканируйте Poweramp."
            )
            self._update_buttons()

        self._invalidate_plan()
        operation = manager.purge if purge else manager.restore
        self._run(
            lambda: operation(
                item[0], progress=self.ctrl.maintenance_message.emit, cancel=self.cancel
            ),
            done,
        )

    def _restore(self):
        self._session_action()

    def _purge(self):
        self._session_action(purge=True)
