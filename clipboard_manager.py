import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
import html
from typing import List, Optional

import keyboard
from PySide6 import QtCore, QtGui, QtWidgets
import winreg


MAX_ITEMS = 20
PREVIEW_MAX_CHARS = 140
PASTE_DELAY_MS = 80
SMOKE_TEST_AUTOQUIT_MS = 800
MAX_CLIPBOARD_TEXT_BYTES = 500 * 1024  # 500KB
CLIPBOARD_DEBOUNCE_MS = 125


class GlobalHotkeyManager:
    def __init__(self) -> None:
        self._registered = False

    def register_toggle(self, callback) -> Optional[Exception]:
        try:
            keyboard.add_hotkey("ctrl+shift+v", callback)
            self._registered = True
            return None
        except Exception as e:
            return e

    def shutdown(self) -> None:
        try:
            if self._registered:
                keyboard.unhook_all_hotkeys()
        except Exception:
            pass


@dataclass(frozen=True)
class HistoryItem:
    id: str
    text: str
    pinned: bool = False
    ts: float = 0.0  # unix seconds


class ClipboardHistory:
    def __init__(self, max_items: int = MAX_ITEMS, storage_path: Optional[str] = None):
        self.max_items = max_items
        self.storage_path = storage_path or self.default_history_path()
        self._items: List[HistoryItem] = []
        self._ignore_next: Optional[str] = None

    # -------- persistence helpers --------
    @staticmethod
    def default_app_dir() -> str:
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "MiniClipboardManager")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def default_history_path(cls) -> str:
        return os.path.join(cls.default_app_dir(), "history.json")

    def load(self) -> None:
        path = self.storage_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items: List[HistoryItem] = []
            for row in (data.get("items") or []):
                text = (row.get("text") or "").strip("\r\n")
                if not text:
                    continue
                item_id = str(row.get("id") or "").strip() or uuid.uuid4().hex
                pinned = bool(row.get("pinned", False))
                ts = float(row.get("ts", 0.0) or 0.0)
                if ts <= 0:
                    ts = time.time()
                items.append(HistoryItem(id=item_id, text=text, pinned=pinned, ts=ts))
            self.set_items(items)
        except Exception:
            # Keep going silently; best-effort load
            pass

    def save(self) -> None:
        path = self.storage_path
        if not path:
            return
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "items": [{"id": it.id, "text": it.text, "pinned": it.pinned, "ts": it.ts} for it in self.items()],
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            # Best-effort save; ignore errors
            pass

    # -------- in-memory operations --------
    def items(self) -> List[HistoryItem]:
        return list(self._items)

    def set_items(self, items: List[HistoryItem]) -> None:
        # Ensure ordering: pinned first, then newest; pinned are never trimmed
        pinned = [it for it in items if it.pinned]
        unpinned = [it for it in items if not it.pinned]
        pinned.sort(key=lambda x: x.ts, reverse=True)
        unpinned.sort(key=lambda x: x.ts, reverse=True)

        remaining_slots = max(0, self.max_items - len(pinned))
        keep_unpinned = [] if remaining_slots == 0 else unpinned[:remaining_slots]
        # pinned can exceed max_items; they are preserved
        self._items = pinned + keep_unpinned

    def set_ignore_next(self, value: str) -> None:
        self._ignore_next = value

    def add(self, value: str) -> bool:
        value = (value or "").strip("\r\n")
        if not value:
            return False

        if self._ignore_next is not None and value == self._ignore_next:
            self._ignore_next = None
            return False

        now = time.time()

        # Move-to-front dedupe; preserve pinned if it already exists
        existing = next((it for it in self._items if it.text == value), None)
        existing_id = existing.id if existing else uuid.uuid4().hex
        pinned = bool(existing.pinned) if existing else False
        self._items = [it for it in self._items if it.text != value]
        self._items.insert(0, HistoryItem(id=existing_id, text=value, pinned=pinned, ts=now))

        # Keep pinned items at the top
        self.set_items(self._items)
        return True

    def toggle_pin(self, item_id: str) -> None:
        updated: List[HistoryItem] = []
        for it in self._items:
            if it.id == item_id:
                updated.append(HistoryItem(id=it.id, text=it.text, pinned=not it.pinned, ts=it.ts or time.time()))
            else:
                updated.append(it)
        self.set_items(updated)

    def remove(self, item_id: str) -> None:
        self._items = [it for it in self._items if it.id != item_id]

    def clear(self) -> None:
        self._items = []

    def export_to_text(self) -> str:
        lines: List[str] = []
        for it in self.items():
            pin = "⭐ " if it.pinned else ""
            when = format_time_ago(it.ts)
            header = f"{pin}{when}".strip()
            if header:
                lines.append(header)
            lines.append(it.text)
            lines.append("-" * 40)
        return "\n".join(lines).rstrip() + "\n"


class AutostartManager:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE_NAME = "MiniClipboardManager"

    @staticmethod
    def _command() -> str:
        script = os.path.abspath(sys.argv[0])
        exe = sys.executable
        return f"\"{exe}\" \"{script}\""

    @classmethod
    def is_enabled(cls) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY, 0, winreg.KEY_READ) as k:
                v, _t = winreg.QueryValueEx(k, cls.VALUE_NAME)
            return isinstance(v, str) and v.strip() != ""
        except FileNotFoundError:
            return False
        except OSError:
            return False

    @classmethod
    def set_enabled(cls, enabled: bool) -> Optional[Exception]:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY) as k:
                if enabled:
                    winreg.SetValueEx(k, cls.VALUE_NAME, 0, winreg.REG_SZ, cls._command())
                else:
                    try:
                        winreg.DeleteValue(k, cls.VALUE_NAME)
                    except FileNotFoundError:
                        pass
            return None
        except Exception as e:
            return e


def format_time_ago(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24 and dt.date() == now.date():
        return f"Today {dt:%H:%M}"
    if hours < 48:
        return f"Yesterday {dt:%H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


class RichListDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter: QtGui.QPainter, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> None:
        painter.save()
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawPrimitive(QtWidgets.QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)

        doc = QtGui.QTextDocument()
        doc.setHtml(index.data(QtCore.Qt.ItemDataRole.UserRole + 10) or "")
        doc.setTextWidth(opt.rect.width() - 12)

        ctx = QtGui.QAbstractTextDocumentLayout.PaintContext()
        if opt.state & QtWidgets.QStyle.StateFlag.State_Selected:
            ctx.palette.setColor(QtGui.QPalette.ColorRole.Text, opt.palette.color(QtGui.QPalette.ColorRole.HighlightedText))

        painter.translate(opt.rect.left() + 6, opt.rect.top() + 4)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def sizeHint(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> QtCore.QSize:
        doc = QtGui.QTextDocument()
        doc.setHtml(index.data(QtCore.Qt.ItemDataRole.UserRole + 10) or "")
        doc.setTextWidth(max(10, option.rect.width() - 12))
        return QtCore.QSize(int(doc.idealWidth()) + 12, int(doc.size().height()) + 8)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, app: QtWidgets.QApplication):
        super().__init__()
        self._app = app
        self._clipboard = app.clipboard()
        self._settings = QtCore.QSettings("MiniClipboardManager", "MiniClipboardManager")
        self._smoke = os.getenv("MCM_SMOKE_TEST") == "1"
        self._hotkeys = GlobalHotkeyManager()

        self.setWindowTitle("Mini Clipboard Manager (last 10)")
        self.resize(560, 420)
        self.setMinimumSize(520, 360)

        self.history = ClipboardHistory(MAX_ITEMS)
        self._last_clipboard: Optional[str] = None
        self._clipboard_debounce = QtCore.QTimer(self)
        self._clipboard_debounce.setSingleShot(True)
        self._clipboard_debounce.setInterval(CLIPBOARD_DEBOUNCE_MS)
        self._clipboard_debounce.timeout.connect(self._process_clipboard_change)

        self._build_ui()
        self.history.load()
        self._refresh_list()
        if not self._smoke:
            self._setup_hotkeys()
        self._clipboard.dataChanged.connect(self._on_clipboard_signal)

        self._apply_topmost()
        self._restore_window_position()

        # extra safety: ensure we persist on quit (tray exit, etc.)
        self._app.aboutToQuit.connect(self._on_about_to_quit)
        if not self._smoke:
            self._setup_tray()

    def _setup_tray(self) -> None:
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView)
        self._tray = QtWidgets.QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Mini Clipboard Manager")

        menu = QtWidgets.QMenu()

        act_toggle = menu.addAction("Show / Hide")
        act_toggle.triggered.connect(self._toggle_visibility)

        act_autostart = QtGui.QAction("Start with Windows", menu)
        act_autostart.setCheckable(True)
        act_autostart.setChecked(AutostartManager.is_enabled())
        act_autostart.triggered.connect(self._set_autostart)
        menu.addAction(act_autostart)

        act_export = menu.addAction("Export history → .txt")
        act_export.triggered.connect(self.export_history)

        act_exit = menu.addAction("Exit")
        act_exit.triggered.connect(self._app.quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _set_autostart(self, enabled: bool) -> None:
        err = AutostartManager.set_enabled(enabled)
        if err is not None:
            QtWidgets.QMessageBox.warning(self, "Autostart failed", str(err))

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_visibility()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        root.addLayout(header)

        title = QtWidgets.QLabel("Clipboard history (last 10). Double-click to paste.")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        header.addWidget(title, 1)

        self.pin_checkbox = QtWidgets.QCheckBox("Keep on top")
        self.pin_checkbox.setChecked(True)
        self.pin_checkbox.stateChanged.connect(self._on_keep_on_top_changed)
        header.addWidget(self.pin_checkbox, 0, QtCore.Qt.AlignmentFlag.AlignRight)

        self.dark_checkbox = QtWidgets.QCheckBox("Dark mode")
        self.dark_checkbox.setChecked(bool(self._settings.value("ui/dark_mode", False, type=bool)))
        self.dark_checkbox.stateChanged.connect(self._on_dark_mode_changed)
        header.addWidget(self.dark_checkbox, 0, QtCore.Qt.AlignmentFlag.AlignRight)

        search_row = QtWidgets.QHBoxLayout()
        root.addLayout(search_row)
        search_row.addWidget(QtWidgets.QLabel("Filter:"), 0)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.textChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.search_edit, 1)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setItemDelegate(RichListDelegate(self.list_widget))
        self.list_widget.itemDoubleClicked.connect(self._on_item_activated)
        self.list_widget.itemActivated.connect(self._on_item_activated)
        self.list_widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_item_menu)
        root.addWidget(self.list_widget, 1)

        buttons_row = QtWidgets.QHBoxLayout()
        root.addLayout(buttons_row)

        copy_btn = QtWidgets.QPushButton("Copy")
        copy_btn.clicked.connect(self.copy_selected)
        buttons_row.addWidget(copy_btn)

        paste_btn = QtWidgets.QPushButton("Paste")
        paste_btn.clicked.connect(self.copy_and_paste_selected)
        buttons_row.addWidget(paste_btn)

        clear_btn = QtWidgets.QPushButton("Clear history")
        clear_btn.clicked.connect(self.clear_history)
        buttons_row.addWidget(clear_btn)

        buttons_row.addStretch(1)

        hide_btn = QtWidgets.QPushButton("Hide (Ctrl+Shift+V to show)")
        hide_btn.clicked.connect(self.hide)
        buttons_row.addWidget(hide_btn)

        hint = QtWidgets.QLabel("Hotkey: Ctrl+Shift+V toggles the window. Paste uses a simulated Ctrl+V.")
        hint.setWordWrap(True)
        pal = hint.palette()
        pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#555"))
        hint.setPalette(pal)
        root.addWidget(hint, 0)

        # Keyboard shortcuts
        QtGui.QShortcut(QtGui.QKeySequence("Return"), self, activated=self.copy_and_paste_selected)
        QtGui.QShortcut(QtGui.QKeySequence("Enter"), self, activated=self.copy_and_paste_selected)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+C"), self, activated=self.copy_selected)
        QtGui.QShortcut(QtGui.QKeySequence("Delete"), self, activated=self.remove_selected)

        self._apply_theme()

    def _on_keep_on_top_changed(self, _state: int) -> None:
        self._apply_topmost()

    def _on_dark_mode_changed(self, _state: int) -> None:
        self._apply_theme()

    def _on_filter_changed(self, _text: str) -> None:
        self._refresh_list()

    def _on_item_activated(self, _item: QtWidgets.QListWidgetItem) -> None:
        self.copy_and_paste_selected()

    def _apply_topmost(self) -> None:
        flags = self.windowFlags()
        if self.pin_checkbox.isChecked():
            flags |= QtCore.Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~QtCore.Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _apply_theme(self) -> None:
        dark = self.dark_checkbox.isChecked()
        self._settings.setValue("ui/dark_mode", dark)

        if not dark:
            QtWidgets.QApplication.setStyle("Fusion")
            QtWidgets.QApplication.setPalette(QtWidgets.QApplication.style().standardPalette())
            return

        QtWidgets.QApplication.setStyle("Fusion")
        pal = QtGui.QPalette()
        pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(30, 30, 30))
        pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(20, 20, 20))
        pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(30, 30, 30))
        pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(45, 45, 45))
        pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(230, 230, 230))
        pal.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor(255, 0, 0))
        pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(42, 130, 218))
        pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(0, 0, 0))
        QtWidgets.QApplication.setPalette(pal)

    def _setup_hotkeys(self) -> None:
        err = self._hotkeys.register_toggle(self._toggle_visibility_threadsafe)
        if err is not None:
            QtCore.QTimer.singleShot(
                0,
                lambda: QtWidgets.QMessageBox.warning(
                    self,
                    "Hotkey unavailable",
                    f"Failed to register global hotkey Ctrl+Shift+V.\n\n{err}",
                ),
            )

    def _toggle_visibility_threadsafe(self) -> None:
        # keyboard callbacks are not on Qt thread
        QtCore.QTimer.singleShot(0, self._toggle_visibility)

    def _toggle_visibility(self) -> None:
        if self.isHidden():
            self.show()
            self.raise_()
            self.activateWindow()
        else:
            self.hide()

    def _on_clipboard_signal(self) -> None:
        # Many apps cause multiple clipboard signals in quick succession.
        # Debounce and process once.
        self._clipboard_debounce.stop()
        self._clipboard_debounce.start()

    def _process_clipboard_change(self) -> None:
        md = self._clipboard.mimeData()
        if md is None or not md.hasText():
            return
        current = md.text()
        try:
            if len(current.encode("utf-8", errors="ignore")) > MAX_CLIPBOARD_TEXT_BYTES:
                return
        except Exception:
            return
        if current != self._last_clipboard:
            self._last_clipboard = current
            if self.history.add(current):
                self._refresh_list()
                self.history.save()

    def _refresh_list(self) -> None:
        items = self.history.items()
        q = (self.search_edit.text() or "").strip().lower()
        if q:
            items = [x for x in items if q in x.text.lower()]

        prev_row = self.list_widget.currentRow()
        self.list_widget.clear()

        for i, it in enumerate(items):
            raw = it.text
            lines = raw.splitlines() or [raw]
            first = lines[0]
            extra_lines = max(0, len(lines) - 1)
            preview = first
            if extra_lines:
                preview += f"  (＋{extra_lines} lines)"
            preview = preview.strip()
            if len(preview) > PREVIEW_MAX_CHARS:
                preview = preview[:PREVIEW_MAX_CHARS] + "…"

            pin = "⭐ " if it.pinned else ""
            when = format_time_ago(it.ts)

            # Highlight search matches inside preview
            escaped = html.escape(preview)
            if q:
                # simple case-insensitive highlight
                low = preview.lower()
                start = low.find(q)
                if start >= 0:
                    end = start + len(q)
                    escaped = (
                        html.escape(preview[:start])
                        + '<span style="background-color:#ffe58f;">'
                        + html.escape(preview[start:end])
                        + "</span>"
                        + html.escape(preview[end:])
                    )

            html_text = (
                f"<div>"
                f"<span>{i+1}. {html.escape(pin)}{escaped}</span>"
                f"<br><span style='color:#666;font-size:10pt'>{html.escape(when)}</span>"
                f"</div>"
            )

            item = QtWidgets.QListWidgetItem("")  # rendered by delegate
            item.setData(QtCore.Qt.ItemDataRole.UserRole, it.id)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 3, it.text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, it.pinned)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 2, it.ts)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 10, html_text)
            self.list_widget.addItem(item)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(max(0, min(prev_row, self.list_widget.count() - 1)))

    def _selected_history_item(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return item.data(QtCore.Qt.ItemDataRole.UserRole)

    def _selected_history_text(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return item.data(QtCore.Qt.ItemDataRole.UserRole + 3)

    def copy_selected(self) -> None:
        text = self._selected_history_text()
        if text is None:
            return
        self.history.set_ignore_next(text)
        self._clipboard.setText(text, mode=self._clipboard.Mode.Clipboard)

    def copy_selected_plain_text(self) -> None:
        text = self._selected_history_text()
        if text is None:
            return
        self.history.set_ignore_next(text)
        md = QtCore.QMimeData()
        md.setText(text)
        self._clipboard.setMimeData(md, mode=self._clipboard.Mode.Clipboard)

    def copy_and_paste_selected(self) -> None:
        text = self._selected_history_text()
        if text is None:
            return

        self.history.set_ignore_next(text)
        self._clipboard.setText(text, mode=self._clipboard.Mode.Clipboard)

        # Hide window quickly so paste goes to previous app
        self.hide()

        def do_paste() -> None:
            try:
                keyboard.send("ctrl+v")
            except Exception as e:
                msg = f"Paste simulation failed. The item is in your clipboard—press Ctrl+V manually.\n\n{e}"
                tray = getattr(self, "_tray", None)
                try:
                    if tray is not None:
                        tray.showMessage("Paste failed", msg, QtWidgets.QSystemTrayIcon.MessageIcon.Warning, 4000)
                        return
                except Exception:
                    pass
                try:
                    QtWidgets.QMessageBox.information(self, "Paste failed", msg)
                except Exception:
                    pass

        QtCore.QTimer.singleShot(PASTE_DELAY_MS, do_paste)

    def remove_selected(self) -> None:
        item_id = self._selected_history_item()
        if item_id is None:
            return
        self.history.remove(item_id)
        self._refresh_list()

    def toggle_pin_selected(self) -> None:
        item_id = self._selected_history_item()
        if item_id is None:
            return
        self.history.toggle_pin(item_id)
        self.history.save()
        self._refresh_list()

    def _show_item_menu(self, pos: QtCore.QPoint) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        self.list_widget.setCurrentItem(item)

        pinned = bool(item.data(QtCore.Qt.ItemDataRole.UserRole + 1))
        menu = QtWidgets.QMenu(self)

        act_pin = menu.addAction("Unpin ⭐" if pinned else "Pin ⭐")
        act_pin.triggered.connect(self.toggle_pin_selected)

        menu.addSeparator()

        act_copy = menu.addAction("Copy")
        act_copy.triggered.connect(self.copy_selected)

        act_copy_plain = menu.addAction("Copy as plain text")
        act_copy_plain.triggered.connect(self.copy_selected_plain_text)

        act_paste = menu.addAction("Paste")
        act_paste.triggered.connect(self.copy_and_paste_selected)

        act_remove = menu.addAction("Remove (Delete)")
        act_remove.triggered.connect(self.remove_selected)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def export_history(self) -> None:
        default_name = os.path.join(os.path.expanduser("~"), "clipboard_history.txt")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export history", default_name, "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.history.export_to_text())
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

    def clear_history(self) -> None:
        self.history.clear()
        self.history.save()
        self._refresh_list()

    def _restore_window_position(self) -> None:
        geom = self._settings.value("window/geometry")
        if isinstance(geom, (QtCore.QByteArray, bytes)):
            self.restoreGeometry(geom)

    def _save_window_position(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())

    def _shutdown_hotkeys(self) -> None:
        self._hotkeys.shutdown()

    def _on_about_to_quit(self) -> None:
        # Called for tray exit, Alt+F4, etc.
        self._save_window_position()
        self.history.save()
        self._shutdown_hotkeys()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._save_window_position()
        self.history.save()
        event.accept()


def main() -> None:
    app = QtWidgets.QApplication([])
    win = MainWindow(app)
    win.show()
    # If smoke testing, auto-exit quickly after startup
    if os.getenv("MCM_SMOKE_TEST") == "1":
        QtCore.QTimer.singleShot(SMOKE_TEST_AUTOQUIT_MS, app.quit)
    app.exec()


if __name__ == "__main__":
    main()

