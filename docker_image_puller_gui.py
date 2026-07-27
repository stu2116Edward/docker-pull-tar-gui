import os
import sys
import threading
import json
import ctypes
import time
import re
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
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QSize, QTimer

# 导入核心功能
from docker_image_puller import pull_image_logic, stop_event, VERSION, cancel_current_pull
from docker_images_search import DockerImageSearcher

class Worker(QObject):
    """用于拉取镜像的后台线程"""
    log_signal = pyqtSignal(int, str)       # 普通日志信号
    progress_signal = pyqtSignal(int, str)  # 进度条专用信号
    finished_signal = pyqtSignal(int)       # 完成信号

    def __init__(self, image, registry, arch, language, generation=0, username=None, password=None):
        super().__init__()
        self.image = image
        self.registry = registry
        self.arch = arch
        self.language = language
        self.generation = generation
        self.username = username
        self.password = password

    def run(self):
        try:
            log_msg = {
                "zh": f"开始拉取镜像：{self.image}\n",
                "en": f"Pulling image: {self.image}\n"
            }[self.language]
            self.log_signal.emit(self.generation, log_msg)

            # 如果有临时认证信息，显示在日志中
            if self.username and self.password:
                auth_msg = {
                    "zh": f"[INFO] 使用认证用户: {self.username}\n",
                    "en": f"[INFO] Using auth user: {self.username}\n"
                }[self.language]
                self.log_signal.emit(self.generation, auth_msg)

            # 调用拉取逻辑，传入认证信息
            pull_image_logic(
                self.image,
                registry=self.registry,
                arch=self.arch,
                username=self.username,
                password=self.password,
                log_callback=self._log_callback
            )

        except Exception as e:
            error_msg = {
                "zh": f"[ERROR] 发生错误：{e}\n",
                "en": f"[ERROR] Error occurred: {e}\n"
            }[self.language]
            self.log_signal.emit(self.generation, error_msg)
        finally:
            self.finished_signal.emit(self.generation)

    def _log_callback(self, message):
        """处理日志消息，区分普通日志和进度更新"""
        if not message:
            return
        # 去除末尾的换行符，避免空行
        message = message.rstrip('\n')
        if not message:
            return
        # 检查是否是进度消息（包含 ⬇️ ✅ 📊 或 |███| 进度条格式）
        if ('⬇️' in message or '✅' in message or '📊' in message or 
            '█' in message or '░' in message):
            # 进度消息发送到进度信号
            self.progress_signal.emit(self.generation, message)
        else:
            # 普通日志消息
            self.log_signal.emit(self.generation, message)


def force_kill_thread(thread):
    """强制终止后台工作线程（绝不应用于 UI 主线程）。
    
    安全机制：
    1. 绝不杀死当前线程（UI 主线程）
    2. 只用于 daemon=True 的后台工作线程
    3. Windows 下优先使用 kernel32.TerminateThread 直接终止原生线程
    4. 非 Windows 回退到 PyThreadState_SetAsyncExc 注入 SystemExit
    
    注意：在 64 位 Windows 上必须正确设置 OpenThread 的返回类型为 c_void_p，
    否则 ctypes 会默认按 32 位 c_int 截断 HANDLE 值，导致 TerminateThread 失败。
    """
    import logging
    if not thread or not thread.is_alive():
        return True
    
    # 安全检查：绝不杀死当前线程
    current_tid = threading.current_thread().ident
    if thread.ident == current_tid:
        return False
    
    # 安全检查：只杀死守护线程
    if not thread.daemon:
        return False
    
    tid = thread.ident
    if tid is None:
        return False
    
    # 方案1: Windows API 直接终止原生线程（最可靠，即使线程在 C 代码中也能立即终止）
    if sys.platform == 'win32':
        try:
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            # 必须精确设置返回类型和参数类型以匹配 Windows API，否则 64 位系统上可能失效
            kernel32.OpenThread.restype = wintypes.HANDLE
            kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.TerminateThread.restype = wintypes.BOOL
            kernel32.TerminateThread.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.GetLastError.restype = wintypes.DWORD
            
            # THREAD_TERMINATE = 0x0001
            handle = kernel32.OpenThread(0x0001, wintypes.BOOL(0), wintypes.DWORD(tid))
            # 兼容某些环境/ctypes版本下 OpenThread 直接返回 int 的情况
            handle_val = getattr(handle, 'value', handle)
            if handle_val and handle_val != 0:  # NULL handle check
                result = kernel32.TerminateThread(handle, wintypes.DWORD(0))
                kernel32.CloseHandle(handle)
                if result:
                    logging.info(f"✅ Windows API 成功终止线程 {tid}")
                    return True
                else:
                    err = kernel32.GetLastError()
                    logging.warning(f"⚠️ TerminateThread 返回失败，线程 {tid} 可能仍在运行，错误码: {err}")
            else:
                err = kernel32.GetLastError()
                logging.warning(f"⚠️ OpenThread 失败，无法获取线程 {tid} 句柄，错误码: {err}")
        except Exception as e:
            logging.warning(f"⚠️ Windows API 终止线程 {tid} 异常: {e}")
    
    # 方案2: 注入 SystemExit 异常（线程回到 Python 层面时才会生效）
    try:
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(tid),
            ctypes.py_object(SystemExit)
        )
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), 0)
            logging.warning(f"⚠️ PyThreadState_SetAsyncExc 注入异常，线程 {tid}")
            return False
        logging.info(f"✅ 已向线程 {tid} 注入 SystemExit")
        return True
    except Exception as e:
        logging.warning(f"⚠️ PyThreadState_SetAsyncExc 异常，线程 {tid}: {e}")
        return False


class SearchWorker(QObject):
    log_signal = pyqtSignal(str)
    search_result_signal = pyqtSignal(int, dict)

    def __init__(self, search_term, page=1, page_size=100, generation=0):
        super().__init__()
        self.search_term = search_term
        self.page = page
        self.page_size = page_size
        self.generation = generation
        # 使用默认限制初始化搜索器（不再限制数量）
        self.searcher = DockerImageSearcher()

    def run(self):
        try:
            self.log_signal.emit(f"正在搜索镜像: {self.search_term} (第{self.page}页)...\n")
            QApplication.processEvents()
            result = self.searcher.search_images(self.search_term, page=self.page, page_size=self.page_size)
            if result and result.get("results"):
                self.log_signal.emit(f"从 {self.searcher.current_registry} 找到 {result.get('total', 0)} 个结果:\n")
                # 添加页码信息到结果中
                result["page"] = self.page
                self.search_result_signal.emit(self.generation, result)
            else:
                self.log_signal.emit("没有找到匹配的镜像\n")
                self.search_result_signal.emit(self.generation, {"total": 0, "results": [], "page": self.page})
        except Exception as e:
            self.log_signal.emit(f"[ERROR] 搜索镜像时出错: {e}\n")
            self.search_result_signal.emit(self.generation, {"total": 0, "results": [], "page": self.page})


class TagsWorker(QObject):
    """用于获取标签的后台线程（支持分页）"""
    log_signal = pyqtSignal(str)
    tags_result_signal = pyqtSignal(int, dict, str)  # generation, result_dict, image_name

    def __init__(self, image_name, page=1, page_size=100, tags_limit=None, generation=0, registry=None):
        super().__init__()
        self.image_name = image_name
        self.page = page
        self.page_size = page_size
        self.tags_limit = tags_limit
        self.generation = generation
        # 使用传入的限制初始化搜索器，不传 registry，让 get_tags 自动遍历所有 registry
        self.searcher = DockerImageSearcher(tags_limit=tags_limit)

    def run(self):
        try:
            self.log_signal.emit(f"正在获取 {self.image_name} 第 {self.page} 页标签...\n")
            QApplication.processEvents()
            result = self.searcher.get_tags(self.image_name, page=self.page, page_size=self.page_size)
            if result:
                tags = result.get("results", [])
                total = result.get("total", -1)
                has_more = result.get("has_more", False)
                total_str = str(total) if total >= 0 else "未知"
                self.log_signal.emit(f"找到 {total_str} 个标签，当前页 {len(tags)} 个\n")
                self.tags_result_signal.emit(self.generation, result, self.image_name)
            else:
                self.log_signal.emit("没有找到标签\n")
                self.tags_result_signal.emit(self.generation, {"total": 0, "results": [], "has_more": False}, self.image_name)
        except Exception as e:
            self.log_signal.emit(f"[ERROR] 获取标签时出错: {e}\n")
            self.tags_result_signal.emit(self.generation, {"total": 0, "results": [], "has_more": False}, self.image_name)


class DockerPullerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.language = "zh"
        self.theme_mode = "light"
        self.is_pulling = False
        self.is_searching = False
        # 分别管理镜像和标签搜索结果限制（None表示无限制）
        self.images_limit = None
        self.tags_limit = None
        self.search_generation = 0
        self.worker_generation = 0  # Worker代次，防止旧worker输出干扰

        # 跟踪活跃的后台线程
        self.pull_thread = None     # 拉取线程
        self.search_thread = None   # 搜索线程

        # 分离搜索和拉取的 worker 变量
        self.pull_worker = None     # 拉取专用
        self.search_worker = None   # 搜索专用

        # 定义图标路径
        base_path = os.path.dirname(os.path.abspath(__file__))
        logo_icon_path = os.path.join(base_path, "logo.ico")
        settings_icon_path = os.path.join(base_path, "settings.png")

        # 加载样式片段
        self.style_snippets = self.load_style_snippets()

        self.init_ui(logo_icon_path, settings_icon_path)
        self.apply_theme_mode()
        self.update_ui_text()

    def load_style_snippets(self):
        """从 style.qss 文件中读取所有样式片段，返回字典 {'name': 'style_string'}"""
        snippets = {}
        style_file = os.path.join(os.path.dirname(__file__), "style.qss")
        try:
            with open(style_file, "r", encoding="utf-8") as f:
                content = f.read()
            # 匹配 /* name */ ... 直到下一个 /* 或结尾
            pattern = r'/\*\s*([^*]+?)\s*\*/(.*?)(?=/\*|$)'
            matches = re.findall(pattern, content, re.DOTALL)
            for name, style in matches:
                name = name.strip()
                style = style.strip()
                snippets[name] = style
            # 补充一些必须存在的键，防止缺失时报错
            if 'msg_box_light' not in snippets:
                snippets['msg_box_light'] = ''  # 亮色模式默认空
            return snippets
        except Exception as e:
            print(f"警告：加载 style.qss 失败 ({e})，使用空样式")
            # 返回空字典，后续会使用硬编码的后备（但为了提取，我们在此不提供后备，因为样式已提取）
            return {}

    def init_ui(self, logo_icon_path, settings_icon_path):
        self.setWindowTitle(f"Docker 镜像打包工具 {VERSION}")
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

        # 添加"认证信息"选项卡
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
        # 保存当前搜索结果，用于返回功能
        self.last_search_results = []
        self.current_search_term = ""
        self.current_image_name_for_tags = ""
        self.is_showing_tags = False  # 是否正在显示标签结果
        self.current_search_registry = None  # 当前搜索使用的注册表

        # 分页相关变量
        self.PAGE_SIZE = 100  # 每页显示条数
        self.current_page = 0  # 当前页码（从0开始）
        self.total_pages = 0   # 总页数
        self.total_results = 0  # 总结果数
        self.page_cache = {}  # 页码 -> 数据列表（1-based）
        self.loaded_pages = set()  # 已加载的页码集合

        # 保存镜像搜索的分页状态，用于从标签页返回时恢复
        self._saved_image_page_state = None

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
        self.search_result_table.doubleClicked.connect(self.on_search_table_double_click)
        search_layout.addWidget(self.search_result_table)

        # 分页控件
        self.pagination_widget = QWidget()
        pagination_layout = QHBoxLayout(self.pagination_widget)
        pagination_layout.setContentsMargins(0, 0, 0, 0)

        self.prev_page_button = QPushButton({
            "zh": "上一页",
            "en": "Previous"
        }[self.language])
        self.prev_page_button.clicked.connect(self.go_to_prev_page)
        self.prev_page_button.setEnabled(False)

        self.page_input = QLineEdit("1")
        self.page_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_input.setFont(QFont("Microsoft YaHei", 10))
        self.page_input.setFixedWidth(80)
        self.page_input.setPlaceholderText("页码")
        self.page_input.returnPressed.connect(self.go_to_page_from_input)

        self.page_total_label = QLabel("/ 1")
        self.page_total_label.setFont(QFont("Microsoft YaHei", 10))

        self.next_page_button = QPushButton({
            "zh": "下一页",
            "en": "Next"
        }[self.language])
        self.next_page_button.clicked.connect(self.go_to_next_page)
        self.next_page_button.setEnabled(False)

        pagination_layout.addStretch()
        pagination_layout.addWidget(self.prev_page_button)
        pagination_layout.addWidget(self.page_input)
        pagination_layout.addWidget(self.page_total_label)
        pagination_layout.addWidget(self.next_page_button)
        pagination_layout.addStretch()
        search_layout.addWidget(self.pagination_widget)

        self.tabs.addTab(search_tab, {
            "zh": "镜像搜索",
            "en": "Image Search"
        }[self.language])

        # 初始化表头颜色
        self.update_search_table_header_style()

        # 添加右键菜单
        self.search_result_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_result_table.customContextMenuRequested.connect(self.show_table_context_menu)

    def update_search_table_header_style(self):
        """根据主题设置表头颜色"""
        header = self.search_result_table.horizontalHeader()
        if self.theme_mode == "dark":
            header.setStyleSheet(self.style_snippets.get('header_dark', ''))
        else:
            header.setStyleSheet(self.style_snippets.get('header_light', ''))

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
            menu.setStyleSheet(self.style_snippets.get('menu_dark', ''))
        else:
            menu.setStyleSheet(self.style_snippets.get('menu_light', ''))
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

        # 添加"管理仓库"按钮
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
            "amd64", "arm32v5", "arm32v6", "arm32v7", "arm64v8", "i386", "ppc64le", "riscv64", "s390x"
        ])
        self.arch_combobox.setCurrentIndex(0)
        # 设置为可编辑，保留右侧下拉箭头
        self.arch_combobox.setEditable(True)
        input_grid.addWidget(self.arch_label, 3, 0)
        input_grid.addWidget(self.arch_combobox, 3, 1)

        # 认证区域 - 直接显示，不再使用下拉
        self.auth_label = QLabel({
            "zh": "认证信息：",
            "en": "Auth Info:"
        }[self.language])
        # 设置字体与上方标签一致
        auth_label_font = QFont("Microsoft YaHei", 12)
        self.auth_label.setFont(auth_label_font)
        input_grid.addWidget(self.auth_label, 4, 0)

        # 认证输入区域 - 横向布局
        self.auth_input_widget = QWidget()
        auth_input_layout = QHBoxLayout(self.auth_input_widget)
        auth_input_layout.setContentsMargins(0, 0, 0, 0)
        auth_input_layout.setSpacing(10)

        # 用户名
        self.username_entry = QLineEdit()
        self.username_entry.setPlaceholderText({
            "zh": "用户名（可选）",
            "en": "Username (optional)"
        }[self.language])
        # 设置字体与上方输入框一致
        auth_input_font = QFont("Microsoft YaHei", 12)
        self.username_entry.setFont(auth_input_font)
        auth_input_layout.addWidget(self.username_entry)

        # 密码
        self.password_entry = QLineEdit()
        self.password_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_entry.setPlaceholderText({
            "zh": "密码（可选）",
            "en": "Password (optional)"
        }[self.language])
        # 设置字体与上方输入框一致
        self.password_entry.setFont(auth_input_font)
        auth_input_layout.addWidget(self.password_entry)

        input_grid.addWidget(self.auth_input_widget, 4, 1)

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
        button_layout.setStretch(0, 1)
        button_layout.setStretch(1, 1)
        button_layout.setStretch(2, 1)

        pull_layout.addLayout(button_layout)

        # 日志区域（包含动态刷新的进度信息）- 扩展以填充更多空间
        self.pull_log_text = QTextEdit()
        self.pull_log_text.setReadOnly(True)
        pull_layout.addWidget(self.pull_log_text, stretch=1)

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

        # 分组框，与拉取页风格一致 - 始终不显示标题，避免与Tab重复
        self.auth_group = QGroupBox()  # 不设置标题
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

        # 递增搜索代次，用于忽略旧线程的返回结果
        self.search_generation += 1
        self.current_search_term = search_term
        self.search_worker = SearchWorker(search_term, page=1, page_size=self.PAGE_SIZE, generation=self.search_generation)
        self.search_worker.search_result_signal.connect(self.display_search_results)
        
        # 创建并跟踪搜索线程（设为守护线程，主程序退出时自动终止）
        self.search_thread = threading.Thread(target=self.search_worker.run, daemon=True)
        self.search_thread.start()

    def display_search_results(self, generation, result):
        """显示搜索结果（带分页，支持按需加载）"""
        # 忽略已被重置的旧搜索线程回传的结果
        if generation != self.search_generation:
            return
        self.is_searching = False
        self.search_button.setEnabled(True)

        # 解析返回结果
        total = result.get("total", 0) if result else 0
        results = result.get("results", []) if result else []
        page = result.get("page", 1) if result else 1

        # 获取来源
        source = getattr(self.search_worker.searcher, "current_registry", "未知来源") if self.search_worker else "未知来源"
        if "://" in source:
            source = source.split("://", 1)[1]

        if results:
            # 如果是第一页，重置所有数据
            if page == 1:
                self.page_cache = {}
                self.loaded_pages = set()
                self.total_results = total
                self.total_pages = min(100, max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE))
                self.current_page = 0
                self.is_showing_tags = False
                # 保存当前搜索使用的注册表
                self.current_search_registry = getattr(self.search_worker.searcher, "current_registry", None) if self.search_worker else None
                # 恢复原始表头
                self.search_result_table.setColumnCount(4)
                self.search_result_table.setHorizontalHeaderLabels(["NAME", "DESCRIPTION", "STARS", "OFFICIAL"])
                # 显示分页控件
                self.pagination_widget.setVisible(True)

            # 缓存当前页数据（1-based）
            self.page_cache[page] = results
            self.loaded_pages.add(page)
            # 更新 last_search_results 为所有已缓存的数据
            all_results = []
            for p in sorted(self.page_cache.keys()):
                all_results.extend(self.page_cache[p])
            self.last_search_results = all_results

            msg = {
                "zh": f"从 {source} 找到 {total} 个结果 （双击搜索tag）:",
                "en": f"Found {total} results from {source} (Double-click to search tag):"
            }[self.language]
            self.search_source_label.setText(msg)
            self._update_pagination_display()
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
            self.total_results = 0
            self.page_cache = {}
            self.loaded_pages = set()
            self.total_pages = 0
            self.current_page = 0
            self._update_pagination_display()

        # 每次都刷新表头颜色
        self.update_search_table_header_style()

        # 设置表头颜色适配主题（双重保险）
        header = self.search_result_table.horizontalHeader()
        if self.theme_mode == "dark":
            header.setStyleSheet(self.style_snippets.get('header_dark', ''))
        else:
            header.setStyleSheet(self.style_snippets.get('header_light', ''))

    def _update_pagination_display(self):
        """更新分页显示"""
        if not self.page_cache:
            self.search_result_table.setRowCount(0)
            self.page_input.setText("1")
            self.page_total_label.setText("/ 1")
            self.prev_page_button.setEnabled(False)
            self.next_page_button.setEnabled(False)
            return

        # 从缓存获取当前页数据（1-based）
        current_page_1based = self.current_page + 1
        page_data = self.page_cache.get(current_page_1based, [])

        self.search_result_table.setRowCount(len(page_data))
        for row, img in enumerate(page_data):
            self.search_result_table.setItem(row, 0, QTableWidgetItem(img['name']))
            self.search_result_table.setItem(row, 1, QTableWidgetItem(img['description']))
            self.search_result_table.setItem(row, 2, QTableWidgetItem(str(img['stars'])))
            self.search_result_table.setItem(row, 3, QTableWidgetItem(str(img['official'])))

        self.page_input.setText(str(current_page_1based))
        self.page_total_label.setText(f"/ {self.total_pages}")
        self.prev_page_button.setEnabled(self.current_page > 0)
        self.next_page_button.setEnabled(self.current_page < self.total_pages - 1)

    def go_to_prev_page(self):
        """上一页"""
        if self.current_page > 0:
            self.current_page -= 1
            if self.is_showing_tags:
                target_page_1based = self.current_page + 1
                if target_page_1based in self.tags_loaded_pages:
                    self._update_tags_pagination_display()
                else:
                    self._load_tags_page_data(target_page_1based)
            else:
                self._update_pagination_display()

    def go_to_next_page(self):
        """下一页"""
        if self.current_page < self.total_pages - 1:
            target_page = self.current_page + 1
            target_page_1based = target_page + 1
            # 先切换页码显示
            self.current_page = target_page
            self.page_input.setText(str(target_page_1based))
            self.prev_page_button.setEnabled(self.current_page > 0)
            self.next_page_button.setEnabled(self.current_page < self.total_pages - 1)
            
            if self.is_showing_tags:
                # 标签结果按需加载
                if target_page_1based in self.tags_loaded_pages:
                    self._update_tags_pagination_display()
                else:
                    # 先清空表格显示加载中，再后台加载
                    self.search_result_table.setRowCount(0)
                    self.search_source_label.setText({
                        "zh": f"正在加载第 {target_page_1based} 页标签...",
                        "en": f"Loading page {target_page_1based} tags..."
                    }[self.language])
                    self._load_tags_page_data(target_page_1based)
            else:
                # 镜像搜索结果需要按需加载
                if target_page_1based in self.loaded_pages:
                    self._update_pagination_display()
                else:
                    # 先清空表格显示加载中，再后台加载
                    self.search_result_table.setRowCount(0)
                    self.search_source_label.setText({
                        "zh": f"正在加载第 {target_page_1based} 页数据...",
                        "en": f"Loading page {target_page_1based}..."
                    }[self.language])
                    self._load_page_data(target_page_1based)

    def go_to_page_from_input(self):
        """从输入框跳转页码"""
        try:
            page = int(self.page_input.text().strip())
            if page < 1:
                page = 1
            if page > self.total_pages:
                page = self.total_pages
            target_page = page - 1
            # 先切换页码显示
            self.current_page = target_page
            self.page_input.setText(str(page))
            self.prev_page_button.setEnabled(self.current_page > 0)
            self.next_page_button.setEnabled(self.current_page < self.total_pages - 1)
            
            if self.is_showing_tags:
                # 标签结果按需加载
                if page in self.tags_loaded_pages:
                    self._update_tags_pagination_display()
                else:
                    # 先清空表格显示加载中，再后台加载
                    self.search_result_table.setRowCount(0)
                    self.search_source_label.setText({
                        "zh": f"正在加载第 {page} 页标签...",
                        "en": f"Loading page {page} tags..."
                    }[self.language])
                    self._load_tags_page_data(page)
            else:
                # 镜像搜索结果需要按需加载
                if page in self.loaded_pages:
                    self._update_pagination_display()
                else:
                    # 先清空表格显示加载中，再后台加载
                    self.search_result_table.setRowCount(0)
                    self.search_source_label.setText({
                        "zh": f"正在加载第 {page} 页数据...",
                        "en": f"Loading page {page}..."
                    }[self.language])
                    self._load_page_data(page)
        except ValueError:
            # 输入无效，恢复当前页码显示
            self.page_input.setText(str(self.current_page + 1))

    def _load_page_data(self, page):
        """加载指定页的数据"""
        if self.is_searching or not self.current_search_term:
            return
        
        self.is_searching = True
        self.search_button.setEnabled(False)
        
        self.search_source_label.setText({
            "zh": f"正在加载第 {page} 页数据...",
            "en": f"Loading page {page}..."
        }[self.language])
        
        # 递增搜索代次
        self.search_generation += 1
        self.search_worker = SearchWorker(
            self.current_search_term, 
            page=page, 
            page_size=self.PAGE_SIZE, 
            generation=self.search_generation
        )
        self.search_worker.search_result_signal.connect(self.display_search_results)
        
        # 创建并跟踪搜索线程（设为守护线程，主程序退出时自动终止）
        self.search_thread = threading.Thread(target=self.search_worker.run, daemon=True)
        self.search_thread.start()

    def _load_tags_page_data(self, page):
        """加载指定页的标签数据"""
        if self.is_searching or not self.current_image_name_for_tags:
            return
        
        self.is_searching = True
        self.search_button.setEnabled(False)
        
        self.search_source_label.setText({
            "zh": f"正在加载第 {page} 页标签...",
            "en": f"Loading page {page} tags..."
        }[self.language])
        
        # 递增搜索代次
        self.search_generation += 1
        self.tags_worker = TagsWorker(
            self.current_image_name_for_tags,
            page=page,
            page_size=self.PAGE_SIZE,
            tags_limit=self.tags_limit,
            generation=self.search_generation
        )
        self.tags_worker.tags_result_signal.connect(self.display_tags_results)
        self.tags_worker.log_signal.connect(lambda msg: print(msg.strip()))
        
        # 创建并跟踪搜索线程（设为守护线程，主程序退出时自动终止）
        self.search_thread = threading.Thread(target=self.tags_worker.run, daemon=True)
        self.search_thread.start()

    def on_search_table_double_click(self, index):
        """处理搜索结果表格的双击事件"""
        if not index.isValid():
            return

        row = index.row()
        if row < 0:
            return

        if self.is_showing_tags:
            # 当前显示的是标签列表，双击复制到拉取页的镜像:标签格式
            tag_item = self.search_result_table.item(row, 0)
            if tag_item and tag_item.text():
                tag_name = tag_item.text()
                # 设置镜像名称和标签到拉取页
                self.image_entry.setText(self.current_image_name_for_tags)
                self.tag_entry.setText(tag_name)
                # 切换到拉取标签页
                self.tabs.setCurrentIndex(1)
        else:
            # 当前显示的是镜像列表，双击获取标签
            image_item = self.search_result_table.item(row, 0)
            if image_item and image_item.text():
                image_name = image_item.text()
                self.get_tags_for_image(image_name)

    def get_tags_for_image(self, image_name):
        """获取并显示指定镜像的标签（支持按需分页加载）"""
        if self.is_searching:
            return

        self.is_searching = True
        self.search_button.setEnabled(False)
        self.current_image_name_for_tags = image_name

        # 保存当前镜像搜索的分页状态，以便返回时恢复
        if not self.is_showing_tags:
            self._saved_image_page_state = {
                "page_cache": dict(self.page_cache),
                "loaded_pages": set(self.loaded_pages),
                "total_results": self.total_results,
                "total_pages": self.total_pages,
                "current_page": self.current_page,
                "current_search_registry": self.current_search_registry,
            }

        # 重置标签分页缓存
        self.tags_page_cache = {}       # 页码 -> 数据列表（1-based）
        self.tags_loaded_pages = set()  # 已加载的标签页码集合
        self.tags_total_results = 0
        self.tags_has_more = False
        self.tags_current_search_registry = None

        self.search_source_label.setText({
            "zh": f"正在获取 {image_name} 的标签...",
            "en": f"Getting tags for {image_name}..."
        }[self.language])

        # 隐藏分页控件（等数据返回后再显示）
        self.pagination_widget.setVisible(False)

        # 递增搜索代次
        self.search_generation += 1
        # 让 TagsWorker 获取第 1 页标签
        self.tags_worker = TagsWorker(
            image_name, 
            page=1,
            page_size=self.PAGE_SIZE,
            tags_limit=self.tags_limit, 
            generation=self.search_generation
        )
        self.tags_worker.tags_result_signal.connect(self.display_tags_results)
        self.tags_worker.log_signal.connect(lambda msg: print(msg.strip()))

        # 创建并跟踪搜索线程（设为守护线程，主程序退出时自动终止）
        self.search_thread = threading.Thread(target=self.tags_worker.run, daemon=True)
        self.search_thread.start()

    def display_tags_results(self, generation, result, image_name):
        """显示标签搜索结果（支持按需分页加载）"""
        # 忽略已被重置的旧搜索线程回传的结果
        if generation != self.search_generation:
            return
        self.is_searching = False
        self.search_button.setEnabled(True)

        # 解析返回结果
        total = result.get("total", 0) if result else 0
        tags = result.get("results", []) if result else []
        has_more = result.get("has_more", False) if result else False
        page = getattr(self.tags_worker, 'page', 1) if self.tags_worker else 1

        # 获取来源
        source = getattr(self.tags_worker.searcher, "current_registry", "未知来源") if self.tags_worker else "未知来源"
        if "://" in source:
            source = source.split("://", 1)[1]
        self.tags_current_search_registry = source

        # 设置标志为正在显示标签
        self.is_showing_tags = True

        # 更改表头为标签格式
        self.search_result_table.setColumnCount(4)
        self.search_result_table.setHorizontalHeaderLabels(["TAG", "SIZE", "ARCHITECTURES", "LAST_UPDATED"])

        if tags:
            # 缓存当前页数据（1-based）
            self.tags_page_cache[page] = tags
            self.tags_loaded_pages.add(page)
            self.tags_total_results = total if total >= 0 else len(tags)
            self.tags_has_more = has_more
            
            # 计算总页数
            if total >= 0:
                self.total_pages = min(100, max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE))
            else:
                # 总数未知时，根据 has_more 推断
                self.total_pages = page + 1 if has_more else page
            
            self.current_page = page - 1  # 内部使用 0-based
            
            total_str = str(total) if total >= 0 else "未知"
            msg = {
                "zh": f"{image_name} - 找到 {total_str} 个标签，共 {self.total_pages} 页 (双击填充到拉取页，右键返回)",
                "en": f"{image_name} - Found {total_str} tags, {self.total_pages} pages (double-click to copy, right-click to go back)"
            }[self.language]
            self.search_source_label.setText(msg)
            
            # 显示分页控件
            self.pagination_widget.setVisible(True)
            self._update_tags_pagination_display()
        else:
            msg = {
                "zh": f"{image_name} - 没有找到标签",
                "en": f"{image_name} - No tags found"
            }[self.language]
            self.search_source_label.setText(msg)
            self.search_result_table.setRowCount(0)
            self.pagination_widget.setVisible(False)
            self.total_pages = 0
            self.current_page = 0

        # 更新表头颜色
        self.update_search_table_header_style()

    def _update_tags_pagination_display(self):
        """更新标签分页显示（从缓存中获取当前页数据）"""
        current_page_1based = self.current_page + 1
        page_data = self.tags_page_cache.get(current_page_1based, [])

        if not page_data:
            self.search_result_table.setRowCount(0)
        else:
            self.search_result_table.setRowCount(len(page_data))
            for row, tag in enumerate(page_data):
                self.search_result_table.setItem(row, 0, QTableWidgetItem(tag.get('name', '')))
                self.search_result_table.setItem(row, 1, QTableWidgetItem(tag.get('size', 'N/A')))
                self.search_result_table.setItem(row, 2, QTableWidgetItem(tag.get('architectures', '')))
                last_updated = tag.get('last_updated', '')
                if last_updated:
                    last_updated = last_updated.replace("T", " ").replace("Z", "")[:19]
                self.search_result_table.setItem(row, 3, QTableWidgetItem(last_updated))

        self.page_input.setText(str(current_page_1based))
        self.page_total_label.setText(f"/ {self.total_pages}")
        self.prev_page_button.setEnabled(self.current_page > 0)
        # 当总数未知时，如果有更多页则启用下一页按钮
        if self.tags_total_results >= 0:
            self.next_page_button.setEnabled(self.current_page < self.total_pages - 1)
        else:
            self.next_page_button.setEnabled(self.tags_has_more or (current_page_1based in self.tags_page_cache and len(self.tags_page_cache[current_page_1based]) == self.PAGE_SIZE))

    def show_table_context_menu(self, pos):
        """显示表格右键菜单"""
        index = self.search_result_table.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(self)

        if self.is_showing_tags:
            # 显示标签列表时，右键可以返回镜像列表
            back_action = menu.addAction({
                "zh": "返回镜像列表",
                "en": "Back to Image List"
            }[self.language])
            back_action.triggered.connect(self.restore_image_search_results)
        else:
            # 显示镜像列表时，可以复制本行
            copy_action = menu.addAction({
                "zh": "复制本行",
                "en": "Copy This Row"
            }[self.language])
            copy_action.triggered.connect(lambda: self.copy_table_row(index.row()))

        # 主题自适应
        if self.theme_mode == "dark":
            menu.setStyleSheet(self.style_snippets.get('menu_dark', ''))
        else:
            menu.setStyleSheet(self.style_snippets.get('menu_light', ''))

        menu.exec(self.search_result_table.viewport().mapToGlobal(pos))

    def restore_image_search_results(self):
        """恢复显示之前的镜像搜索结果（带分页）"""
        if not self.last_search_results:
            return

        self.is_showing_tags = False
        # 恢复表头
        self.search_result_table.setColumnCount(4)
        self.search_result_table.setHorizontalHeaderLabels(["NAME", "DESCRIPTION", "STARS", "OFFICIAL"])

        # 如果有保存的分页状态，恢复它
        if self._saved_image_page_state is not None:
            self.page_cache = self._saved_image_page_state["page_cache"]
            self.loaded_pages = self._saved_image_page_state["loaded_pages"]
            self.total_results = self._saved_image_page_state["total_results"]
            self.total_pages = self._saved_image_page_state["total_pages"]
            self.current_page = self._saved_image_page_state["current_page"]
            self.current_search_registry = self._saved_image_page_state.get("current_search_registry")
            self._saved_image_page_state = None
        else:
            # 从 last_search_results 重建 page_cache（假设第一页）
            self.page_cache = {1: self.last_search_results}
            self.loaded_pages = {1}
            self.total_results = len(self.last_search_results)
            self.total_pages = min(100, max(1, (len(self.last_search_results) + self.PAGE_SIZE - 1) // self.PAGE_SIZE))
            self.current_page = 0

        # 显示分页控件
        self.pagination_widget.setVisible(True)

        if self.last_search_results:
            msg = {
                "zh": f"找到 {self.total_results} 个结果",
                "en": f"Found {self.total_results} results"
            }[self.language]
            self.search_source_label.setText(msg)
            self._update_pagination_display()

        # 更新表头颜色
        self.update_search_table_header_style()

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

        # 如果旧拉取线程仍在运行，拒绝新拉取（避免 stop_event 竞争）
        if self.pull_thread and self.pull_thread.is_alive():
            self.show_message({
                "zh": "提示",
                "en": "Info"
            }[self.language], {
                "zh": "当前拉取操作正在取消中，请稍后再试。",
                "en": "Current pull operation is being cancelled. Please try again later."
            }[self.language])
            return

        self.is_pulling = True
        self.pull_button.setEnabled(False)
        
        # 清空日志区域，显示等待进度条
        self.pull_log_text.clear()
        self.pull_log_text.append("准备下载...")

        # 获取认证信息（直接读取输入框）
        username = self.username_entry.text().strip()
        password = self.password_entry.text().strip()

        # 如果存在旧worker，先终止它并等待完成
        if self.pull_worker is not None:
            # 标记为不再使用，让旧线程自然结束
            self.pull_worker = None

        # 强制处理所有待处理事件，确保旧连接清理完成
        QApplication.processEvents()

        # 断开之前可能存在的所有信号连接
        try:
            # 清理可能残留的信号连接
            if hasattr(self, '_log_slot'):
                self._log_slot.disconnect()
        except:
            pass

        # 递增worker代次，用于过滤旧worker的信号
        self.worker_generation += 1
        current_generation = self.worker_generation

        # 清除 stop_event，允许新拉取（旧线程已确认死亡）
        from docker_image_puller import stop_event
        try:
            stop_event.clear()
        except Exception:
            pass

        self.pull_worker = Worker(
            f"{image}:{tag}",
            self.registry_combobox.currentText(),
            self.arch_combobox.currentText(),
            self.language,
            generation=current_generation,
            username=username,
            password=password
        )

        # 连接信号，使用包装函数来过滤旧worker的信号
        self.pull_worker.log_signal.connect(self._handle_log_signal)
        self.pull_worker.progress_signal.connect(self._handle_progress_signal)
        self.pull_worker.finished_signal.connect(self._handle_finished_signal)

        # 启动线程并跟踪（设为守护线程，主程序退出时自动终止）
        self.pull_thread = threading.Thread(target=self.pull_worker.run, daemon=True)
        self.pull_thread.start()

    def _handle_log_signal(self, generation, message):
        """处理日志信号，只接受当前代次的worker消息"""
        if generation == self.worker_generation:
            # 去除末尾换行符避免空行，QTextEdit.append会自动添加换行
            message = message.rstrip('\n')
            if message:
                self.pull_log_text.append(message)

    def _handle_progress_signal(self, generation, progress_text):
        """处理进度信号，只接受当前代次的worker消息"""
        if generation == self.worker_generation:
            self._update_progress_area(progress_text)

    def _handle_finished_signal(self, generation):
        """处理完成信号，只接受当前代次的worker消息"""
        if generation == self.worker_generation:
            self.on_pull_finished()

    def _update_progress_area(self, progress_text):
        """更新进度显示 - 在同一个日志框中动态替换进度信息"""
        # 获取当前文本
        current_text = self.pull_log_text.toPlainText()
        lines = current_text.split('\n')
        
        # 检查是否已有进度行（以 ⬇️ ✅ 📊 █ ░ 开头）
        progress_start = -1
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith(('  ⬇️', '  ✅', '📊')) or '█' in lines[i] or '░' in lines[i]:
                progress_start = i
            elif progress_start != -1:
                break
        
        if progress_start != -1:
            # 移除旧进度行
            lines = lines[:progress_start]
        
        # 添加新进度文本
        new_text = '\n'.join(lines)
        if new_text and not new_text.endswith('\n'):
            new_text += '\n'
        new_text += progress_text
        
        # 更新文本
        self.pull_log_text.setPlainText(new_text)
        
        # 滚动到底部
        scrollbar = self.pull_log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_pull_finished(self):
        """拉取完成后的处理"""
        self.is_pulling = False
        self.pull_button.setEnabled(True)
        # 重置stop_event，允许下一次下载
        from docker_image_puller import stop_event
        try:
            stop_event.clear()
        except Exception:
            pass
        # 清除进度显示状态
        self.progress_lines_count = 0
        # 在GUI中显示恢复状态消息
        self.pull_log_text.append("🔄 已恢复初始状态")
        # 恢复初始状态
        self.reset_ui_state()
        import logging
        logging.info("🔄 已恢复初始状态")

    def reset_ui_state(self):
        """仅重置UI状态，不触发取消操作"""
        # 递增worker代次，使旧worker的信号被过滤掉
        self.worker_generation += 1

        # 断开拉取worker信号连接
        if self.pull_worker is not None:
            try:
                self.pull_worker.log_signal.disconnect()
                self.pull_worker.progress_signal.disconnect()
                self.pull_worker.finished_signal.disconnect()
            except (TypeError, RuntimeError):
                pass
            self.pull_worker = None

        # 清空输入框
        self.image_entry.clear()
        self.tag_entry.setText("latest")
        self.registry_combobox.setCurrentIndex(0)
        self.arch_combobox.setCurrentIndex(0)
        self.username_entry.clear()
        self.password_entry.clear()

    def reset_fields(self):
        """重置表单和搜索状态 - 优先优雅终止线程，避免文件锁定"""
        import logging
        import time

        # 1. 发送停止信号并关闭网络连接（让线程有机会正常退出）
        try:
            cancel_current_pull()  # 设置 stop_event + 关闭 SessionManager
        except Exception:
            pass

        # 关闭搜索器的 session 以中断 HTTP 请求
        for worker_attr in ['search_worker', 'tags_worker']:
            worker = getattr(self, worker_attr, None)
            if worker and hasattr(worker, 'searcher'):
                try:
                    worker.searcher.stop()
                except Exception:
                    pass

        # 2. 等待后台线程自然退出（优先）
        threads_to_wait = []
        if self.pull_thread and self.pull_thread.is_alive():
            threads_to_wait.append(self.pull_thread)
        if self.search_thread and self.search_thread.is_alive():
            threads_to_wait.append(self.search_thread)

        # 最多等待 3 秒，让线程有机会清理文件句柄
        wait_time = 0
        while threads_to_wait and wait_time < 3.0:
            for thread in threads_to_wait[:]:
                if not thread.is_alive():
                    threads_to_wait.remove(thread)
            if threads_to_wait:
                time.sleep(0.1)
                wait_time += 0.1
                QApplication.processEvents()

        # 3. 对于仍未退出的线程，强制终止（最后手段）
        if self.pull_thread and self.pull_thread.is_alive():
            force_kill_thread(self.pull_thread)
        if self.search_thread and self.search_thread.is_alive():
            force_kill_thread(self.search_thread)

        # 4. 重置所有状态变量
        self.is_pulling = False
        self.is_searching = False
        
        # 5. 递增代次以过滤旧信号
        self.worker_generation += 1
        self.search_generation += 1
        
        # 6. 断开所有信号连接
        if self.pull_worker is not None:
            try:
                self.pull_worker.log_signal.disconnect()
                self.pull_worker.progress_signal.disconnect()
                self.pull_worker.finished_signal.disconnect()
            except (TypeError, RuntimeError):
                pass
            self.pull_worker = None
        
        if self.search_worker is not None:
            try:
                self.search_worker.log_signal.disconnect()
                self.search_worker.search_result_signal.disconnect()
            except (TypeError, RuntimeError):
                pass
            self.search_worker = None
        
        if getattr(self, 'tags_worker', None) is not None:
            try:
                self.tags_worker.log_signal.disconnect()
                self.tags_worker.tags_result_signal.disconnect()
            except (TypeError, RuntimeError):
                pass
            self.tags_worker = None
        
        # 7. 重置 UI 控件状态
        self.pull_button.setEnabled(True)
        self.search_button.setEnabled(True)
        
        # 8. 重置拉取区域
        self.pull_log_text.clear()
        self.pull_log_text.append("🔄 已重置 - 后台线程已终止")
        self.image_entry.clear()
        self.tag_entry.setText("latest")
        self.registry_combobox.setCurrentIndex(0)
        self.arch_combobox.setCurrentIndex(0)
        
        # 9. 重置认证区域
        self.username_entry.clear()
        self.password_entry.clear()
        
        # 10. 重置搜索区域
        self.search_entry.clear()
        self.search_result_table.setRowCount(0)
        self.search_source_label.setText("")
        self.load_registries()
        
        # 11. 重置分页相关状态
        self.page_cache = {}
        self.loaded_pages = set()
        self.total_results = 0
        self.total_pages = 0
        self.current_page = 0
        self.is_showing_tags = False
        self._saved_image_page_state = None
        self.last_search_results = []
        self.current_search_term = ""
        self.current_image_name_for_tags = ""
        self.current_search_registry = None

        # 12. 重置标签分页缓存
        self.tags_page_cache = {}
        self.tags_loaded_pages = set()
        self.tags_total_results = 0
        self.tags_has_more = False
        self.tags_current_search_registry = None
        self.page_input.setText("1")
        self.page_total_label.setText("/ 1")
        self.prev_page_button.setEnabled(False)
        self.next_page_button.setEnabled(False)
        self.pagination_widget.setVisible(True)
        
        # 13. 重置进度显示计数
        self.progress_lines_count = 0
        
        # 14. 清理线程引用
        self.pull_thread = None
        self.search_thread = None
        
        # 15. 清除 stop_event，允许新的操作
        try:
            stop_event.clear()
        except Exception:
            pass
        
        logging.info("✅ 已优雅终止后台线程，UI 已恢复初始状态")

    def load_registries(self):
        """加载仓库列表，优先使用 registries.txt 中的首个地址作为默认"""
        self.registry_combobox.clear()
        if os.path.exists("registries.txt"):
            with open("registries.txt", "r", encoding="utf-8") as f:
                registries = [line.strip() for line in f if line.strip()]
                if registries:
                    # 使用 registries.txt 中的首个地址作为默认
                    self.registry_combobox.addItems(registries)
                    return
        # 兜底：使用官方 Docker Hub
        self.registry_combobox.addItem("https://registry.hub.docker.com")

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
                }[self.language] + "\n")
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
            msg_box.setStyleSheet(self.style_snippets.get('msg_box_dark', ''))
        else:
            msg_box.setStyleSheet(self.style_snippets.get('msg_box_light', ''))

        msg_box.exec()

    def apply_button_style(self, button):
        """应用按钮样式"""
        if self.theme_mode == "light":
            button.setStyleSheet(self.style_snippets.get('button_light', ''))
        else:
            button.setStyleSheet(self.style_snippets.get('button_dark', ''))

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

            self.setStyleSheet(self.style_snippets.get('global_dark', ''))
            self.search_result_table.setStyleSheet(self.style_snippets.get('table_dark', ''))
            self.pull_log_text.setStyleSheet(self.style_snippets.get('log_dark', ''))
            self.settings_button.setStyleSheet(self.style_snippets.get('settings_button_dark', ''))
            label_color = self.style_snippets.get('label_dark', 'color: white;')
        else:
            # 亮色模式设置
            palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.PlaceholderText, Qt.GlobalColor.gray)

            self.setStyleSheet(self.style_snippets.get('global_light', ''))
            self.search_result_table.setStyleSheet(self.style_snippets.get('table_light', ''))
            self.pull_log_text.setStyleSheet(self.style_snippets.get('log_light', ''))
            self.settings_button.setStyleSheet(self.style_snippets.get('settings_button_light', ''))
            label_color = self.style_snippets.get('label_light', 'color: black;')

        # 强制设置所有相关label颜色
        for label in [
            self.registry_label,
            self.image_label,
            self.tag_label,
            self.arch_label,
            self.auth_label,
            getattr(self, "search_source_label", None)
        ]:
            if label:
                label.setStyleSheet(label_color)

        # 设置分页控件颜色
        if hasattr(self, 'page_input'):
            if self.theme_mode == "dark":
                self.page_input.setStyleSheet(self.style_snippets.get('page_input_dark', ''))
                self.page_total_label.setStyleSheet(self.style_snippets.get('page_total_label_dark', ''))
                self.prev_page_button.setStyleSheet(self.style_snippets.get('prev_next_button_dark', ''))
                self.next_page_button.setStyleSheet(self.style_snippets.get('prev_next_button_dark', ''))
            else:
                self.page_input.setStyleSheet(self.style_snippets.get('page_input_light', ''))
                self.page_total_label.setStyleSheet(self.style_snippets.get('page_total_label_light', ''))
                self.prev_page_button.setStyleSheet(self.style_snippets.get('prev_next_button_light', ''))
                self.next_page_button.setStyleSheet(self.style_snippets.get('prev_next_button_light', ''))

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
                "window_title": f"Docker 镜像打包工具 {VERSION}",
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
                "auth_group": "",
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
        # 更新认证信息标签文本
        if hasattr(self, "auth_label"):
            self.auth_label.setText({
                "zh": "认证信息：",
                "en": "Auth Info:"
            }[self.language])
        # 更新认证输入框的placeholder
        if hasattr(self, "username_entry"):
            self.username_entry.setPlaceholderText({
                "zh": "用户名（可选）",
                "en": "Username (optional)"
            }[self.language])
        if hasattr(self, "password_entry"):
            self.password_entry.setPlaceholderText({
                "zh": "密码（可选）",
                "en": "Password (optional)"
            }[self.language])
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
        layout.addWidget(apply_btn)
        layout.addStretch()
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
    
    # 确保程序退出时强制终止所有后台线程
    def cleanup_on_exit():
        """程序退出时的清理函数 - 强制终止所有后台线程，确保进程完全退出"""
        import logging
        logging.info("🧹 程序正在退出，强制终止所有后台线程...")
        
        # 设置停止事件，通知所有后台线程停止
        stop_event.set()
        
        # 关闭拉取 session 连接
        try:
            from docker_image_puller import SessionManager
            SessionManager.close_session()
        except Exception:
            pass
        
        # 关闭搜索器 session 以中断 HTTP 请求
        for attr in ['search_worker', 'tags_worker']:
            worker = getattr(window, attr, None)
            if worker and hasattr(worker, 'searcher'):
                try:
                    worker.searcher.stop()
                except Exception:
                    pass
        
        # 强制终止所有后台线程（守护线程 + force_kill_thread 双保险）
        threads_to_kill = []
        if window.pull_thread and window.pull_thread.is_alive():
            threads_to_kill.append(window.pull_thread)
        if window.search_thread and window.search_thread.is_alive():
            threads_to_kill.append(window.search_thread)
        
        killed_count = 0
        for thread in threads_to_kill:
            if force_kill_thread(thread):
                killed_count += 1
        
        logging.info(f"✅ 已强制终止后台线程，程序正在退出")
    
    # 注册退出清理函数
    app.aboutToQuit.connect(cleanup_on_exit)
    
    sys.exit(app.exec())
