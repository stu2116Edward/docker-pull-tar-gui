import os
import sys
import threading
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QPushButton,
    QTextEdit,
    QProgressBar,
    QMessageBox,
    QDialog,
    QWidget,
    QGridLayout,
    QGroupBox,
    QTabWidget,
    QListWidget,
    QInputDialog
)
from PyQt6.QtGui import QIcon, QFont, QColor, QPalette
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QSize

# 导入核心功能
from docker_image_puller_core import pull_image_logic, stop_event, VERSION
from docker_images_search import DockerImageSearcher

class Worker(QObject):
    """用于拉取镜像的后台线程"""
    log_signal = pyqtSignal(str)
    layer_progress_signal = pyqtSignal(int)
    overall_progress_signal = pyqtSignal(int)

    def __init__(self, image, registry, arch, language):
        super().__init__()
        self.image = image
        self.registry = registry
        self.arch = arch
        self.language = language

    def run(self):
        try:
            log_msg = {
                "zh": f"开始拉取镜像：{self.image}\n",
                "en": f"Pulling image: {self.image}\n"
            }
            self.log_signal.emit(log_msg[self.language])

            pull_image_logic(
                self.image,
                self.registry,
                self.arch,
                log_callback=self.log_callback,
                layer_progress_callback=self.layer_progress_callback,
                overall_progress_callback=self.overall_progress_callback
            )

        except Exception as e:
            error_msg = {
                "zh": f"[ERROR] 发生错误：{e}\n",
                "en": f"[ERROR] Error occurred: {e}\n"
            }
            self.log_callback(error_msg[self.language])
        finally:
            self.layer_progress_callback(0)
            self.overall_progress_callback(0)

    def log_callback(self, message):
        self.log_signal.emit(message)

    def layer_progress_callback(self, value):
        self.layer_progress_signal.emit(value)

    def overall_progress_callback(self, value):
        self.overall_progress_signal.emit(value)


class SearchWorker(QObject):
    """用于搜索镜像的后台线程"""
    log_signal = pyqtSignal(str)
    search_result_signal = pyqtSignal(list)

    def __init__(self, search_term):
        super().__init__()
        self.search_term = search_term
        self.searcher = DockerImageSearcher()

    def run(self):
        try:
            self.log_signal.emit(f"正在搜索镜像: {self.search_term}...\n")
            QApplication.processEvents()

            results = self.searcher.search_images(self.search_term, limit=25)
            if results:
                self.log_signal.emit(f"从 {self.searcher.current_registry} 找到 {len(results)} 个结果:\n")
                self.search_result_signal.emit(results)
            else:
                self.log_signal.emit("没有找到匹配的镜像\n")
        except Exception as e:
            self.log_signal.emit(f"[ERROR] 搜索镜像时出错: {e}\n")


class DockerPullerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.language = "zh"
        self.theme_mode = "light"
        self.is_pulling = False
        self.is_searching = False
        self.searcher = DockerImageSearcher()

        # 定义图标路径
        base_path = os.path.dirname(os.path.abspath(__file__))
        logo_icon_path = os.path.join(base_path, "logo.ico")
        settings_icon_path = os.path.join(base_path, "settings.png")

        self.init_ui(logo_icon_path, settings_icon_path)
        self.apply_theme_mode()
        self.update_ui_text()

    def init_ui(self, logo_icon_path, settings_icon_path):
        self.setWindowTitle(f"Docker 镜像工具 {VERSION}")
        self.setGeometry(100, 100, 800, 600)
        self.setWindowIcon(QIcon(logo_icon_path))

        # 主布局
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # 标签页
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # 搜索标签页
        self.create_search_tab()

        # 拉取标签页
        self.create_pull_tab()

        # 设置按钮
        self.settings_button = QPushButton()
        self.settings_button.setIcon(QIcon(settings_icon_path))
        self.settings_button.setIconSize(QSize(24, 24))
        self.settings_button.clicked.connect(self.show_settings_dialog)
        settings_layout = QHBoxLayout()
        settings_layout.addWidget(self.settings_button)
        settings_layout.addStretch()
        main_layout.addLayout(settings_layout)

    def create_search_tab(self):
        """创建搜索标签页"""
        search_tab = QWidget()
        search_layout = QVBoxLayout(search_tab)

        # 搜索区域
        search_group = QGroupBox()
        search_box_layout = QHBoxLayout()

        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText({
            "zh": "输入镜像名称 (如: nginx)",
            "en": "Enter image name (e.g. nginx)"
        }[self.language])
        self.search_entry.returnPressed.connect(self.search_images)

        self.search_button = QPushButton({
            "zh": "搜索",
            "en": "Search"
        }[self.language])
        self.search_button.clicked.connect(self.search_images)
        self.search_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(self.search_button)

        search_box_layout.addWidget(self.search_entry)
        search_box_layout.addWidget(self.search_button)
        search_group.setLayout(search_box_layout)
        search_layout.addWidget(search_group)

        # 搜索结果
        self.search_result_text = QTextEdit()
        self.search_result_text.setReadOnly(True)
        self.search_result_text.setFont(QFont("Consolas", 10))
        search_layout.addWidget(self.search_result_text)

        self.tabs.addTab(search_tab, {
            "zh": "镜像搜索",
            "en": "Image Search"
        }[self.language])

    def create_pull_tab(self):
        """创建拉取标签页"""
        pull_tab = QWidget()
        pull_layout = QVBoxLayout(pull_tab)

        # 输入区域
        input_group = QGroupBox()
        input_grid = QGridLayout()

        # 仓库地址
        self.registry_label = QLabel({
            "zh": "仓库地址：",
            "en": "Registry:"
        }[self.language])
        self.registry_combobox = QComboBox()
        self.load_registries()
        input_grid.addWidget(self.registry_label, 0, 0)
        input_grid.addWidget(self.registry_combobox, 0, 1)

        # 添加“管理仓库”按钮
        self.manage_registries_button = QPushButton({
            "zh": "管理仓库",
            "en": "Manage Registries"
        }[self.language])
        self.manage_registries_button.clicked.connect(self.manage_registries)
        self.manage_registries_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(self.manage_registries_button)
        input_grid.addWidget(self.manage_registries_button, 0, 2)

        # 镜像名称
        self.image_label = QLabel({
            "zh": "镜像名称：",
            "en": "Image Name:"
        }[self.language])
        self.image_entry = QLineEdit()
        input_grid.addWidget(self.image_label, 1, 0)
        input_grid.addWidget(self.image_entry, 1, 1)

        # 标签
        self.tag_label = QLabel({
            "zh": "标签版本：",
            "en": "Tag:"
        }[self.language])
        self.tag_entry = QLineEdit()
        self.tag_entry.setText("latest")
        input_grid.addWidget(self.tag_label, 2, 0)
        input_grid.addWidget(self.tag_entry, 2, 1)

        # 架构
        self.arch_label = QLabel({
            "zh": "系统架构：",
            "en": "Architecture:"
        }[self.language])
        self.arch_combobox = QComboBox()
        self.arch_combobox.addItems([
            "amd64", "arm64", "arm32v7", "arm32v5", "i386", "ppc64le", "s390x", "mips64le"
        ])
        self.arch_combobox.setCurrentIndex(0)
        input_grid.addWidget(self.arch_label, 3, 0)
        input_grid.addWidget(self.arch_combobox, 3, 1)

        input_group.setLayout(input_grid)
        pull_layout.addWidget(input_group)

        # 按钮区域
        button_layout = QHBoxLayout()
        self.pull_button = QPushButton({
            "zh": "拉取镜像",
            "en": "Pull Image"
        }[self.language])
        self.pull_button.clicked.connect(self.pull_image)
        self.pull_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(self.pull_button)

        self.reset_button = QPushButton({
            "zh": "重置",
            "en": "Reset"
        }[self.language])
        self.reset_button.clicked.connect(self.reset_fields)
        self.reset_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(self.reset_button)

        button_layout.addWidget(self.pull_button)
        button_layout.addWidget(self.reset_button)
        button_layout.addWidget(self.manage_registries_button)
        button_layout.setStretch(0, 1)  # 拉取镜像按钮自适应拉伸
        button_layout.setStretch(1, 1)  # 重置按钮自适应拉伸
        button_layout.setStretch(2, 1)  # 管理仓库按钮自适应拉伸

        pull_layout.addLayout(button_layout)

        # 日志区域
        self.pull_log_text = QTextEdit()
        self.pull_log_text.setReadOnly(True)
        pull_layout.addWidget(self.pull_log_text)

        # 进度条
        progress_layout = QHBoxLayout()
        self.layer_progress_label = QLabel({
            "zh": "当前层：",
            "en": "Layer:"
        }[self.language])
        self.layer_progress_bar = QProgressBar()
        self.layer_progress_bar.setValue(0)

        self.overall_progress_label = QLabel({
            "zh": "总体进度：",
            "en": "Overall:"
        }[self.language])
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setValue(0)

        progress_layout.addWidget(self.layer_progress_label)
        progress_layout.addWidget(self.layer_progress_bar)
        progress_layout.addWidget(self.overall_progress_label)
        progress_layout.addWidget(self.overall_progress_bar)
        pull_layout.addLayout(progress_layout)

        self.tabs.addTab(pull_tab, {
            "zh": "镜像拉取",
            "en": "Image Pull"
        }[self.language])

        # 设置字体
        font = QFont("Microsoft YaHei", 12)
        for widget in [
            self.registry_label, self.registry_combobox,
            self.image_label, self.image_entry,
            self.tag_label, self.tag_entry,
            self.arch_label, self.arch_combobox,
            self.search_entry
        ]:
            widget.setFont(font)

    def search_images(self):
        """搜索Docker镜像"""
        search_term = self.search_entry.text().strip()
        if not search_term:
            self.show_message({
                "zh": "错误",
                "en": "Error"
            }[self.language], {
                "zh": "搜索词不能为空！",
                "en": "Search term cannot be empty!"
            }[self.language])
            return

        if self.is_searching:
            self.show_message({
                "zh": "提示",
                "en": "Info"
            }[self.language], {
                "zh": "搜索正在进行中，请稍后再试！",
                "en": "Search is in progress, please try again later!"
            }[self.language])
            return

        self.is_searching = True
        self.search_button.setEnabled(False)

        self.search_result_text.clear()
        self.worker = SearchWorker(search_term)
        self.worker.log_signal.connect(self.search_result_text.append)
        self.worker.search_result_signal.connect(self.display_search_results)
        threading.Thread(target=self.worker.run).start()

    def display_search_results(self, results):
        """显示搜索结果"""
        self.is_searching = False
        self.search_button.setEnabled(True)

        if results:
            # 格式化表头
            header = f"{'NAME'.ljust(30)}{'DESCRIPTION'.ljust(60)}{'STARS'.ljust(10)}{'OFFICIAL'}"
            self.search_result_text.append(header)
            self.search_result_text.append("-" * len(header))

            # 添加结果
            for img in results:
                self.search_result_text.append(
                    f"{img['name'].ljust(30)}"
                    f"{img['description'].ljust(60)}"
                    f"{str(img['stars']).ljust(10)}"
                    f"{img['official']}"
                )

            # 双击结果自动填充到拉取标签页
            self.search_result_text.mouseDoubleClickEvent = lambda event: self.fill_pull_fields_from_search()
        else:
            self.search_result_text.append({
                "zh": "没有找到匹配的镜像\n",
                "en": "No matching images found\n"
            }[self.language])

    def fill_pull_fields_from_search(self):
        """将搜索结果填充到拉取表单"""
        cursor = self.search_result_text.textCursor()
        line_text = cursor.block().text()
        if line_text and "NAME" not in line_text:  # 忽略表头
            image_name = line_text[:30].strip()
            self.image_entry.setText(image_name)
            self.tabs.setCurrentIndex(1)  # 切换到拉取标签页

    def pull_image(self):
        """拉取镜像"""
        image = self.image_entry.text().strip()
        tag = self.tag_entry.text().strip()

        if not image or not tag:
            self.show_message({
                "zh": "错误",
                "en": "Error"
            }[self.language], {
                "zh": "镜像名称和标签不能为空！",
                "en": "Image name and tag cannot be empty!"
            }[self.language])
            return

        self.is_pulling = True
        self.pull_button.setEnabled(False)

        self.worker = Worker(
            f"{image}:{tag}",
            self.registry_combobox.currentText(),
            self.arch_combobox.currentText(),
            self.language
        )

        self.worker.log_signal.connect(self.pull_log_text.append)
        self.worker.layer_progress_signal.connect(self.layer_progress_bar.setValue)
        self.worker.overall_progress_signal.connect(self.overall_progress_bar.setValue)

        # 拉取完成后恢复按钮
        def on_pull_finished(*args, **kwargs):
            self.is_pulling = False
            self.pull_button.setEnabled(True)
        # 连接线程结束信号
        thread = threading.Thread(target=self.worker.run)
        thread.start()
        # 轮询线程状态，拉取完成后恢复按钮
        def check_thread():
            if thread.is_alive():
                QTimer.singleShot(200, check_thread)
            else:
                on_pull_finished()
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(200, check_thread)

    def reset_fields(self):
        """重置表单"""
        if self.is_pulling:
            stop_event.set()
            self.is_pulling = False
            self.pull_button.setEnabled(True)

        self.pull_log_text.clear()
        self.image_entry.clear()
        self.tag_entry.setText("latest")
        self.registry_combobox.setCurrentIndex(0)
        self.arch_combobox.setCurrentIndex(0)
        self.layer_progress_bar.setValue(0)
        self.overall_progress_bar.setValue(0)

    def load_registries(self):
        """加载仓库列表"""
        self.registry_combobox.clear()
        self.registry_combobox.addItem("registry.hub.docker.com")
        if os.path.exists("registries.txt"):
            with open("registries.txt", "r", encoding="utf-8") as f:
                registries = [line.strip() for line in f if line.strip()]
                self.registry_combobox.addItems(registries)

    def manage_registries(self):
        """管理仓库地址"""
        dialog = QDialog(self)
        dialog.setWindowTitle({
            "zh": "管理仓库地址",
            "en": "Manage Registries"
        }[self.language])

        # 使用 QTextEdit 允许直接编辑仓库地址
        registries_text = QTextEdit()
        registries_text.setFont(QFont("Consolas", 10))
        if os.path.exists("registries.txt"):
            with open("registries.txt", "r", encoding="utf-8") as f:
                registries_text.setText(f.read().strip())
        else:
            registries_text.setText("registry.hub.docker.com\n")

        save_button = QPushButton({
            "zh": "保存",
            "en": "Save"
        }[self.language])
        save_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(save_button)

        layout = QVBoxLayout()
        layout.addWidget(registries_text)
        layout.addWidget(save_button)
        dialog.setLayout(layout)

        def save_registries():
            """保存仓库地址"""
            registries = registries_text.toPlainText().strip().split("\n")
            with open("registries.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(registries))
            self.load_registries()
            dialog.close()

        save_button.clicked.connect(save_registries)
        dialog.exec()

    def show_message(self, title, message):
        """显示消息对话框"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)
        msg_box.setIcon(QMessageBox.Icon.Critical)

        # 设置 OK 按钮的样式
        ok_button = msg_box.addButton(QMessageBox.StandardButton.Ok)
        ok_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(ok_button)

        # 修复暗色模式下弹窗为亮色的问题
        if self.theme_mode == "dark":
            msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #353535;
                    color: white;
                }
                QLabel {
                    color: white;
                }
                QPushButton {
                    background-color: #535353;
                    color: white;
                    border: 1px solid #333;
                }
                QPushButton:hover {
                    background-color: #636363;
                }
            """)
        else:
            msg_box.setStyleSheet("")

        msg_box.exec()

    def apply_button_style(self, button):
        """应用按钮样式"""
        if self.theme_mode == "light":
            button.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    border: none;
                    color: white;
                    padding: 8px 16px;
                    text-align: center;
                    text-decoration: none;
                    font-size: 14px;
                    margin: 4px 2px;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
            """)
        else:
            button.setStyleSheet("""
                QPushButton {
                    background-color: #535353;
                    border: 1px solid #333;
                    color: white;
                    padding: 8px 16px;
                    text-align: center;
                    text-decoration: none;
                    font-size: 14px;
                    margin: 4px 2px;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #636363;
                }
            """)

    def apply_theme_mode(self):
        """应用主题模式"""
        palette = QPalette()
        if self.theme_mode == "dark":
            # 暗色模式设置
            palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.PlaceholderText, Qt.GlobalColor.lightGray)

            # 强制控件样式
            dark_style = """
                QTextEdit, QLineEdit {
                    background-color: #252525;
                    color: white;
                    border: 1px solid #444;
                    selection-background-color: #444;
                    selection-color: white;
                }
                QComboBox {
                    background-color: #252525;
                    color: white;
                    border: 1px solid #444;
                }
                QComboBox QAbstractItemView {
                    background-color: #252525;
                    color: white;
                    selection-background-color: #444;
                    selection-color: white;
                }
                QGroupBox {
                    border: 1px solid #444;
                    margin-top: 6px;
                    color: white;
                }
                QGroupBox:title {
                    subcontrol-origin: margin;
                    subcontrol-position: top left;
                    padding: 0 3px;
                }
                QTabWidget::pane {
                    border: 1px solid #444;
                    background: #353535;
                }
                QTabBar::tab {
                    background: #353535;
                    color: white;
                    border: 1px solid #444;
                    padding: 6px 12px;
                    border-bottom: none;
                }
                QTabBar::tab:selected {
                    background: #252525;
                    color: #FFD700;
                    border-bottom: 2px solid #FFD700;
                }
                QProgressBar {
                    border: 1px solid #444;
                    border-radius: 4px;
                    background: #252525;
                    text-align: center;
                    color: white;
                }
                QProgressBar::chunk {
                    background-color: #45a049;
                }
            """
            self.setStyleSheet(dark_style)
            self.search_result_text.setStyleSheet("QTextEdit { background-color: #252525; color: white; border: 1px solid #444; }")
            self.pull_log_text.setStyleSheet("QTextEdit { background-color: #252525; color: white; border: 1px solid #444; }")
            self.settings_button.setStyleSheet("""
                QPushButton {
                    background-color: #535353;
                    border: none;
                    color: white;
                }
                QPushButton:hover {
                    background-color: #636363;
                }
            """)
            # 强制设置所有相关label为白色
            label_color = "color: white;"
        else:
            # 亮色模式设置
            palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.PlaceholderText, Qt.GlobalColor.gray)

            light_style = """
                QTextEdit, QLineEdit {
                    background-color: white;
                    color: black;
                    border: 1px solid #ccc;
                    selection-background-color: #cceeff;
                    selection-color: black;
                }
                QComboBox {
                    background-color: white;
                    color: black;
                    border: 1px solid #ccc;
                }
                QComboBox QAbstractItemView {
                    background-color: white;
                    color: black;
                    selection-background-color: #cceeff;
                    selection-color: black;
                }
                QGroupBox {
                    border: 1px solid #ccc;
                    margin-top: 6px;
                    color: black;
                }
                QGroupBox:title {
                    subcontrol-origin: margin;
                    subcontrol-position: top left;
                    padding: 0 3px;
                }
                QTabWidget::pane {
                    border: 1px solid #ccc;
                    background: #f0f0f0;
                }
                QTabBar::tab {
                    background: #f0f0f0;
                    color: black;
                    border: 1px solid #ccc;
                    padding: 6px 12px;
                    border-bottom: none;
                }
                QTabBar::tab:selected {
                    background: white;
                    color: #0078d7;
                    border-bottom: 2px solid #0078d7;
                }
                QProgressBar {
                    border: 1px solid #ccc;
                    border-radius: 4px;
                    background: white;
                    text-align: center;
                    color: black;
                }
                QProgressBar::chunk {
                    background-color: #4CAF50;
                }
            """
            self.setStyleSheet(light_style)
            self.search_result_text.setStyleSheet("QTextEdit { background-color: white; color: black; border: 1px solid #ccc; }")
            self.pull_log_text.setStyleSheet("QTextEdit { background-color: white; color: black; border: 1px solid #ccc; }")
            self.settings_button.setStyleSheet("""
                QPushButton {
                    background-color: #f0f0f0;
                    border: none;
                    color: black;
                }
                QPushButton:hover {
                    background-color: #e0e0e0;
                }
            """)
            # 强制设置所有相关label为黑色
            label_color = "color: black;"

        # 强制设置所有相关label颜色
        for label in [
            self.registry_label,
            self.image_label,
            self.tag_label,
            self.arch_label,
            self.layer_progress_label,
            self.overall_progress_label
        ]:
            label.setStyleSheet(label_color)

        # 应用调色板到应用程序和窗口
        self.setPalette(palette)
        QApplication.instance().setPalette(palette)

    def update_ui_text(self):
        """更新UI文本"""
        translations = {
            "zh": {
                "window_title": f"Docker 镜像工具 {VERSION}",
                "search_tab": "镜像搜索",
                "pull_tab": "镜像拉取",
                "search_btn": "搜索",
                "search_group": "镜像搜索",
                "pull_btn": "拉取镜像",
                "reset_btn": "重置",
                "manage_registries": "管理仓库",
                "registry_label": "仓库地址：",
                "image_label": "镜像名称：",
                "tag_label": "标签版本：",
                "arch_label": "系统架构：",
                "layer_progress": "当前层：",
                "overall_progress": "总体进度："
            },
            "en": {
                "window_title": f"Docker Image Tool {VERSION}",
                "search_tab": "Image Search",
                "pull_tab": "Image Pull",
                "search_btn": "Search",
                "search_group": "Image Search",
                "pull_btn": "Pull Image",
                "reset_btn": "Reset",
                "manage_registries": "Manage Registries",
                "registry_label": "Registry:",
                "image_label": "Image Name:",
                "tag_label": "Tag:",
                "arch_label": "Architecture:",
                "layer_progress": "Layer:",
                "overall_progress": "Overall:"
            }
        }
        trans = translations[self.language]

        self.setWindowTitle(trans["window_title"])
        self.tabs.setTabText(0, trans["search_tab"])
        self.tabs.setTabText(1, trans["pull_tab"])
        self.search_button.setText(trans["search_btn"])
        self.pull_button.setText(trans["pull_btn"])
        self.reset_button.setText(trans["reset_btn"])
        self.manage_registries_button.setText(trans["manage_registries"])
        self.registry_label.setText(trans["registry_label"])
        self.image_label.setText(trans["image_label"])
        self.tag_label.setText(trans["tag_label"])
        self.arch_label.setText(trans["arch_label"])
        self.layer_progress_label.setText(trans["layer_progress"])
        self.overall_progress_label.setText(trans["overall_progress"])
        self.findChild(QGroupBox).setTitle(trans["search_group"])
        self.search_entry.setPlaceholderText({
            "zh": "输入镜像名称 (如: nginx)",
            "en": "Enter image name (e.g. nginx)"
        }[self.language])

    def show_settings_dialog(self):
        """显示设置对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle({
            "zh": "设置",
            "en": "Settings"
        }[self.language])

        if self.theme_mode == "dark":
            dialog.setStyleSheet("background-color: #353535; color: white;")
        else:
            dialog.setStyleSheet("background-color: white; color: black;")

        # 语言设置
        lang_label = QLabel({
            "zh": "语言设置：",
            "en": "Language:"
        }[self.language])

        lang_combo = QComboBox()
        lang_combo.addItems(["中文", "English"])
        lang_combo.setCurrentText("中文" if self.language == "zh" else "English")

        # 主题设置
        theme_label = QLabel({
            "zh": "主题模式：",
            "en": "Theme:"
        }[self.language])

        theme_combo = QComboBox()
        theme_combo.addItems(["亮色", "暗色"] if self.language == "zh" else ["Light", "Dark"])
        theme_combo.setCurrentText({
            ("light", "zh"): "亮色",
            ("dark", "zh"): "暗色",
            ("light", "en"): "Light",
            ("dark", "en"): "Dark"
        }[(self.theme_mode, self.language)])

        # 应用按钮
        apply_btn = QPushButton({
            "zh": "应用",
            "en": "Apply"
        }[self.language])
        apply_btn.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(apply_btn)

        layout = QVBoxLayout()
        layout.addWidget(lang_label)
        layout.addWidget(lang_combo)
        layout.addWidget(theme_label)
        layout.addWidget(theme_combo)
        layout.addWidget(apply_btn)
        dialog.setLayout(layout)

        def apply_settings():
            self.language = "zh" if lang_combo.currentText() == "中文" else "en"
            self.theme_mode = "light" if theme_combo.currentText() in ["亮色", "Light"] else "dark"
            self.update_ui_text()
            self.apply_theme_mode()
            dialog.close()

        apply_btn.clicked.connect(apply_settings)
        dialog.exec()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DockerPullerGUI()
    window.show()
    sys.exit(app.exec())
