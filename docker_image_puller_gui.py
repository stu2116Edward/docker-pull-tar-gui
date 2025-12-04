import os
import sys
import threading
import json
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
    QPlainTextEdit,
    QProgressBar,
    QMessageBox,
    QDialog,
    QWidget,
    QGridLayout,
    QGroupBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMenu
)
from PyQt6.QtGui import QIcon, QFont, QColor, QPalette
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QSize

# 导入核心功能
from docker_image_puller import pull_image_logic, stop_event, VERSION, cancel_current_pull
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

# 定义默认输出条数
DEFAULT_RESULT_LIMIT = 25

class SearchWorker(QObject):
    log_signal = pyqtSignal(str)
    search_result_signal = pyqtSignal(list)

    def __init__(self, search_term, limit=DEFAULT_RESULT_LIMIT):
        super().__init__()
        self.search_term = search_term
        self.limit = limit
        self.searcher = DockerImageSearcher()

    def run(self):
        try:
            self.log_signal.emit(f"正在搜索镜像: {self.search_term}...\n")
            QApplication.processEvents()
            results = self.searcher.search_images(self.search_term, limit=self.limit)
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
        self.result_limit = DEFAULT_RESULT_LIMIT

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

        # 添加“认证信息”选项卡，样式与“镜像拉取”一致
        self.create_auth_tab()

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

        # 来源信息标签
        self.search_source_label = QLabel("")
        self.search_source_label.setFont(QFont("Microsoft YaHei", 10))
        self.search_source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        search_layout.addWidget(self.search_source_label)

        # 搜索结果表格
        self.search_result_table = QTableWidget()
        self.search_result_table.setColumnCount(4)
        self.search_result_table.setHorizontalHeaderLabels(["NAME", "DESCRIPTION", "STARS", "OFFICIAL"])
        self.search_result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.search_result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.search_result_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.search_result_table.horizontalHeader().setStretchLastSection(True)
        self.search_result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.search_result_table.verticalHeader().setVisible(False)
        self.search_result_table.setFont(QFont("Consolas", 10))
        self.search_result_table.doubleClicked.connect(self.fill_pull_fields_from_search)
        search_layout.addWidget(self.search_result_table)

        self.tabs.addTab(search_tab, {
            "zh": "镜像搜索",
            "en": "Image Search"
        }[self.language])

        # 初始化表头颜色
        self.update_search_table_header_style()
        
        # 添加右键复制输出信息
        self.search_result_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_result_table.customContextMenuRequested.connect(self.show_table_context_menu)
        
    def update_search_table_header_style(self):
        """根据主题设置表头颜色"""
        header = self.search_result_table.horizontalHeader()
        if self.theme_mode == "dark":
            header.setStyleSheet("""
                QHeaderView::section {
                    background-color: #353535;
                    color: #FFD700;
                    border: 1px solid #444;
                    font-weight: bold;
                }
            """)
        else:
            header.setStyleSheet("""
                QHeaderView::section {
                    background-color: #f0f0f0;
                    color: #0078d7;
                    border: 1px solid #ccc;
                    font-weight: bold;
                }
            """)
            
    def show_table_context_menu(self, pos):
        index = self.search_result_table.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(self)
        copy_action = menu.addAction({
            "zh": "复制本行",
            "en": "Copy This Row"
        }[self.language])
        # 主题自适应
        if self.theme_mode == "dark":
            menu.setStyleSheet("""
                QMenu { background-color: #353535; color: white; }
                QMenu::item:selected { background-color: #636363; }
            """)
        else:
            menu.setStyleSheet("""
                QMenu { background-color: #fff; color: black; }
                QMenu::item:selected { background-color: #cceeff; }
            """)
        copy_action.triggered.connect(lambda: self.copy_table_row(index.row()))
        menu.exec(self.search_result_table.viewport().mapToGlobal(pos))

    def copy_table_row(self, row):
        """复制指定行的所有内容（包括被省略的）"""
        col_count = self.search_result_table.columnCount()
        row_data = []
        for col in range(col_count):
            item = self.search_result_table.item(row, col)
            row_data.append(item.text() if item else "")
        clipboard = QApplication.clipboard()
        clipboard.setText('\t'.join(row_data))

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
        # 支持手动输入仓库地址
        self.registry_combobox.setEditable(True)
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

        # 角落控件在 init_ui 中设置

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

    def create_auth_tab(self):
        """创建认证信息选项卡（样式与镜像拉取一致）"""
        auth_tab = QWidget()
        auth_layout = QVBoxLayout(auth_tab)

        # 分组框，与拉取页风格一致
        self.auth_group = QGroupBox({
            "zh": "认证信息",
            "en": "Auth Info"
        }[self.language])
        group_layout = QVBoxLayout()

        # JSON 编辑器，仅保留一种格式
        self.auth_json_edit = QPlainTextEdit()
        self.auth_json_edit.setFont(QFont("Consolas", 10))
        placeholder = {
            "zh": "{\n  \"registry\": \"your.registry.com\",\n  \"username\": \"your_user\",\n  \"password\": \"your_pass\"\n}",
            "en": "{\n  \"registry\": \"your.registry.com\",\n  \"username\": \"your_user\",\n  \"password\": \"your_pass\"\n}"
        }[self.language]
        self.auth_json_edit.setPlaceholderText(placeholder)
        # 默认写入占位示例，方便用户直接修改
        self.auth_json_edit.setPlainText(placeholder)

        # 操作按钮，保持一致的字号与样式
        self.apply_auth_button = QPushButton({
            "zh": "保存认证",
            "en": "Save Auth"
        }[self.language])
        self.apply_auth_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(self.apply_auth_button)
        self.apply_auth_button.clicked.connect(self.apply_auth_json_from_editor)

        group_layout.addWidget(self.auth_json_edit)
        group_layout.addWidget(self.apply_auth_button)
        self.auth_group.setLayout(group_layout)
        auth_layout.addWidget(self.auth_group)

        # 字体与拉取页一致
        font = QFont("Microsoft YaHei", 12)
        self.auth_group.setFont(font)
        self.apply_auth_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))

        # 加载已保存的认证JSON
        saved = self.read_saved_auth_json()
        if saved:
            self.auth_json_edit.setPlainText(saved)

        # 添加到标签页
        self.tabs.addTab(auth_tab, {
            "zh": "认证信息",
            "en": "Auth Info"
        }[self.language])

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

        self.search_result_table.setRowCount(0)
        
        self.search_source_label.setText({
            "zh": "正在搜索，请稍候...",
            "en": "Searching, please wait..."
        }[self.language])

        self.worker = SearchWorker(search_term, self.result_limit)
        self.worker.search_result_signal.connect(self.display_search_results)
        threading.Thread(target=self.worker.run).start()

    def display_search_results(self, results):
        """显示搜索结果"""
        self.is_searching = False
        self.search_button.setEnabled(True)

        self.search_result_table.setRowCount(0)
        # 获取来源
        source = getattr(self.worker.searcher, "current_registry", "未知来源")
        if "://" in source:
            source = source.split("://", 1)[1]
        if results:
            msg = {
                "zh": f"从 {source} 找到 {len(results)} 个结果:",
                "en": f"Found {len(results)} results from {source}:"
            }[self.language]
            self.search_source_label.setText(msg)
            self.search_result_table.setRowCount(len(results))
            for row, img in enumerate(results):
                self.search_result_table.setItem(row, 0, QTableWidgetItem(img['name']))
                self.search_result_table.setItem(row, 1, QTableWidgetItem(img['description']))
                self.search_result_table.setItem(row, 2, QTableWidgetItem(str(img['stars'])))
                self.search_result_table.setItem(row, 3, QTableWidgetItem(str(img['official'])))
        else:
            msg = {
                "zh": "没有找到匹配的镜像",
                "en": "No matching images found"
            }[self.language]
            self.search_source_label.setText(msg)
            self.search_result_table.setRowCount(1)
            self.search_result_table.setItem(0, 0, QTableWidgetItem(msg))
            self.search_result_table.setItem(0, 1, QTableWidgetItem(""))
            self.search_result_table.setItem(0, 2, QTableWidgetItem(""))
            self.search_result_table.setItem(0, 3, QTableWidgetItem(""))

        # 每次都刷新表头颜色
        self.update_search_table_header_style()

        # 设置表头颜色适配主题
        header = self.search_result_table.horizontalHeader()
        if self.theme_mode == "dark":
            # 暗色模式表头
            header.setStyleSheet("""
                QHeaderView::section {
                    background-color: #353535;
                    color: #FFD700;
                    border: 1px solid #444;
                    font-weight: bold;
                }
            """)
        else:
            # 亮色模式表头
            header.setStyleSheet("""
                QHeaderView::section {
                    background-color: #f0f0f0;
                    color: #0078d7;
                    border: 1px solid #ccc;
                    font-weight: bold;
                }
            """)

    def fill_pull_fields_from_search(self):
        """将搜索结果填充到拉取表单"""
        selected = self.search_result_table.currentRow()
        if selected >= 0:
            image_name = self.search_result_table.item(selected, 0).text()
            if image_name and image_name != "NAME":
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

        # 不再提前应用认证信息；仅在后端遇到 401 时按需读取 auth.json

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
        """重置表单和搜索状态"""
        if self.is_pulling:
            # 调用取消函数，立即终止网络请求并关闭会话
            cancel_current_pull()
            self.is_pulling = False
            self.pull_button.setEnabled(True)

        # 拉取区重置
        self.pull_log_text.clear()
        self.image_entry.clear()
        self.tag_entry.setText("latest")
        self.registry_combobox.setCurrentIndex(0)
        self.arch_combobox.setCurrentIndex(0)
        self.layer_progress_bar.setValue(0)
        self.overall_progress_bar.setValue(0)

        # 搜索区重置
        self.search_entry.clear()
        self.search_result_table.setRowCount(0)
        self.search_source_label.setText("")
        self.is_searching = False
        self.search_button.setEnabled(True)
        self.load_registries()  # 重新加载仓库地址

    def load_registries(self):
        """加载仓库列表"""
        self.registry_combobox.clear()
        # 默认包含协议，鼓励用户在 registries.txt 中显式写出协议
        self.registry_combobox.addItem("https://registry.hub.docker.com")
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
            registries_text.setText("https://registry.hub.docker.com\n")

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

    def parse_auth_json(self, text=None):
        """解析认证 JSON，支持以下结构：
        - 单对象：{"registry": "host:port", "username": "u", "password": "p"}
          兼容键名形如"registry1"等前缀。
        - 列表：[{...}, {...}]，将选择与当前仓库匹配的条目；若无匹配，仅保存不应用。
        - 映射：{"auths": {"host:port": {"username": "u", "password": "p"}}}
        返回匹配当前仓库的凭据 dict 或 None（表示不应用，仅保存）。
        """
        if text is None:
            text = ''
        text = text.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.show_message({
                "zh": "错误",
                "en": "Error"
            }[self.language], {
                "zh": "认证信息 JSON 解析失败，请检查格式。",
                "en": "Failed to parse auth JSON. Please check the format."
            }[self.language])
            return None

        # 规范化比较：忽略协议与尾部斜杠差异
        def _normalize_registry(reg):
            if not reg:
                return ''
            r = str(reg).strip()
            if r.startswith('http://'):
                r = r[len('http://'):]
            elif r.startswith('https://'):
                r = r[len('https://'):]
            return r.rstrip('/')

        def _extract_registry_value(obj: dict):
            # 首选标准键
            if 'registry' in obj:
                return obj.get('registry')
            # 兼容形如 registry1/registry2 的键名
            for k in obj.keys():
                if isinstance(k, str) and k.lower().startswith('registry'):
                    return obj.get(k)
            return None

        current_registry = self.registry_combobox.currentText() if hasattr(self, 'registry_combobox') else None
        current_norm = _normalize_registry(current_registry) if current_registry else None

        # 映射结构：{"auths": {"host:port": {"username": "u", "password": "p"}}}
        if isinstance(data, dict) and isinstance(data.get('auths'), dict):
            for reg, val in data.get('auths', {}).items():
                if current_norm and _normalize_registry(reg) == current_norm and isinstance(val, dict):
                    user = val.get('username')
                    pwd = val.get('password')
                    if user and pwd:
                        return {"username": user, "password": pwd}
            # 无匹配：静默返回 None（仅保存，不应用）
            return None

        # 单对象或普通字典
        if isinstance(data, dict):
            reg_val = _extract_registry_value(data)
            if reg_val and (not current_norm or _normalize_registry(reg_val) == current_norm):
                return {"username": data.get("username"), "password": data.get("password")}
            # 无匹配或缺少 registry：静默返回 None（仅保存，不应用）
            return None

        # 列表结构：选择与当前仓库匹配的条目
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    reg_val = _extract_registry_value(item)
                    if reg_val and current_norm and _normalize_registry(reg_val) == current_norm:
                        user = item.get('username')
                        pwd = item.get('password')
                        if user and pwd:
                            return {"username": user, "password": pwd}
            # 无匹配：静默返回 None（仅保存，不应用）
            return None

        # 其他结构不支持
        self.show_message({
            "zh": "错误",
            "en": "Error"
        }[self.language], {
            "zh": "认证信息 JSON 支持对象、数组或包含 auths 的对象。",
            "en": "Auth JSON supports an object, an array, or an object with auths."
        }[self.language])
        return None

    def apply_auth_env(self, creds):
        """将认证信息写入环境变量，供拉取逻辑使用"""
        vars_to_set = [
            ("DOCKER_REGISTRY_USERNAME", "username"),
            ("DOCKER_REGISTRY_PASSWORD", "password"),
            ("REGISTRY_USERNAME", "username"),
            ("REGISTRY_PASSWORD", "password")
        ]
        if creds:
            for env_key, key in vars_to_set:
                os.environ[env_key] = creds.get(key, "")
            # 反馈日志
            if hasattr(self, 'pull_log_text'):
                self.pull_log_text.append({
                    "zh": f"已应用认证信息。用户：{creds.get('username', '')}",
                    "en": f"Auth applied. User: {creds.get('username', '')}"
                }[self.language])
        else:
            for env_key, _ in vars_to_set:
                if env_key in os.environ:
                    os.environ.pop(env_key, None)

    def apply_auth_json(self, text=None):
        """解析并应用认证JSON，同时保存到本地文件。
        当不匹配当前仓库或为列表/映射无直接匹配时，仍会保存文件，但不应用到环境变量。
        后端会在拉取时按需读取并匹配使用。
        """
        # 先保存到文件
        saved_ok = False
        try:
            text_to_save = text if text is not None else ''
            with open("auth.json", "w", encoding="utf-8") as f:
                f.write(text_to_save)
            saved_ok = True
        except Exception:
            saved_ok = False

        # 再尝试解析并应用到环境变量（若匹配当前仓库）
        creds = self.parse_auth_json(text)
        if creds:
            self.apply_auth_env(creds)

        # 成功保存后提示
        if saved_ok:
            self.show_message({
                "zh": "保存成功",
                "en": "Success"
            }[self.language], {
                "zh": "认证信息已保存。",
                "en": "Auth JSON has been saved."
            }[self.language], icon=QMessageBox.Icon.Information)

    def apply_auth_json_from_editor(self):
        """从选项卡编辑器读取并应用"""
        text = self.auth_json_edit.toPlainText() if hasattr(self, 'auth_json_edit') else ''
        self.apply_auth_json(text)

    def read_saved_auth_json(self):
        """读取本地保存的认证JSON文本（如果存在）"""
        try:
            if os.path.exists("auth.json"):
                with open("auth.json", "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            return ""
        return ""
        # 旧的弹窗认证入口已被认证选项卡替代

    def show_message(self, title, message, icon=None):
        """显示消息对话框"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)
        # 默认使用错误图标；若传入自定义图标则替换
        if icon is None:
            icon = QMessageBox.Icon.Critical
        msg_box.setIcon(icon)

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

            dark_style = """
                QTextEdit, QLineEdit, QPlainTextEdit {
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
                QTableWidget {
                    background-color: white;
                    color: black;
                    border: 1px solid #ccc;
                    selection-background-color: #e0e0e0;
                    selection-color: black;
                }
            """
            self.setStyleSheet(dark_style)
            self.search_result_table.setStyleSheet("QTableWidget { background-color: #252525; color: white; border: 1px solid #444; }")
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
                QTextEdit, QLineEdit, QPlainTextEdit {
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
                QTableWidget {
                    background-color: white;
                    color: black;
                    border: 1px solid #ccc;
                }
                QTableWidget::item:selected {
                    background-color: #e0e0e0;
                    color: black;
                }
                QTableWidget::item:focus {
                    outline: none;
                }
                QTableWidget::item:selected:!active {
                    background-color: #e0e0e0;
                    color: black;
                }
            """
            self.setStyleSheet(light_style)
            self.search_result_table.setStyleSheet("QTableWidget { background-color: white; color: black; border: 1px solid #ccc; }")
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
            label_color = "color: black;"

        # 强制设置所有相关label颜色
        for label in [
            self.registry_label,
            self.image_label,
            self.tag_label,
            self.arch_label,
            self.layer_progress_label,
            self.overall_progress_label,
            getattr(self, "search_source_label", None)
        ]:
            if label:
                label.setStyleSheet(label_color)

        # 应用调色板到应用程序和窗口
        self.setPalette(palette)
        QApplication.instance().setPalette(palette)

        # 每次切换主题都刷新表头颜色
        if hasattr(self, "update_search_table_header_style"):
            self.update_search_table_header_style()

    def update_ui_text(self):
        """更新UI文本"""
        translations = {
            "zh": {
                "window_title": f"Docker 镜像工具 {VERSION}",
                "search_tab": "镜像搜索",
                "pull_tab": "镜像拉取",
                "auth_tab": "认证信息",
                "search_btn": "搜索",
                "pull_btn": "拉取镜像",
                "reset_btn": "重置",
                "manage_registries": "管理仓库",
                "registry_label": "仓库地址：",
                "image_label": "镜像名称：",
                "tag_label": "标签版本：",
                "arch_label": "系统架构：",
                "layer_progress": "当前层：",
                "overall_progress": "总体进度：",
                "auth_group": "",
                "apply_auth": "保存认证",
                "auth_placeholder": "{\n  \"registry\": \"your.registry.com\",\n  \"username\": \"your_user\",\n  \"password\": \"your_pass\"\n}"
            },
            "en": {
                "window_title": f"Docker Image Tool {VERSION}",
                "search_tab": "Image Search",
                "pull_tab": "Image Pull",
                "auth_tab": "Auth Info",
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
                "overall_progress": "Overall:",
                "auth_group": "Auth Info",
                "apply_auth": "Save Auth",
                "auth_placeholder": "{\n  \"registry\": \"your.registry.com\",\n  \"username\": \"your_user\",\n  \"password\": \"your_pass\"\n}"
            }
        }
        trans = translations[self.language]

        self.setWindowTitle(trans["window_title"])
        self.tabs.setTabText(0, trans["search_tab"])
        self.tabs.setTabText(1, trans["pull_tab"])
        if self.tabs.count() > 2:
            self.tabs.setTabText(2, trans["auth_tab"])
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
        self.search_entry.setPlaceholderText({
            "zh": "输入镜像名称 (如: nginx)",
            "en": "Enter image name (e.g. nginx)"
        }[self.language])
        # 更新认证选项卡控件文本
        if hasattr(self, "auth_group"):
            self.auth_group.setTitle(trans["auth_group"])
        if hasattr(self, "apply_auth_button"):
            self.apply_auth_button.setText(trans["apply_auth"])
        if hasattr(self, "auth_json_edit"):
            self.auth_json_edit.setPlaceholderText(trans["auth_placeholder"])

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

        # 输出条数设置
        limit_label = QLabel({
            "zh": "输出条数：",
            "en": "Result Limit:"
        }[self.language])
        from PyQt6.QtWidgets import QSpinBox
        limit_spin = QSpinBox()
        limit_spin.setRange(1, 100)
        limit_spin.setValue(getattr(self, "result_limit", DEFAULT_RESULT_LIMIT))
        limit_spin.setSingleStep(1)

        # 应用按钮
        apply_btn = QPushButton({
            "zh": "应用",
            "en": "Apply"
        }[self.language])
        apply_btn.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.apply_button_style(apply_btn)

        # 自适应布局
        layout = QVBoxLayout()
        layout.addWidget(lang_label)
        layout.addWidget(lang_combo)
        layout.addWidget(theme_label)
        layout.addWidget(theme_combo)
        layout.addWidget(limit_label)
        layout.addWidget(limit_spin)
        layout.addWidget(apply_btn)
        layout.addStretch()
        dialog.setLayout(layout)

        def apply_settings():
            self.language = "zh" if lang_combo.currentText() == "中文" else "en"
            self.theme_mode = "light" if theme_combo.currentText() in ["亮色", "Light"] else "dark"
            self.result_limit = limit_spin.value()
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
