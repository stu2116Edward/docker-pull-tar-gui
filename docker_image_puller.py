import os
import sys
import gzip
import json
import hashlib
import shutil
import threading
import time
import warnings
import re

# Set default encoding to UTF-8
try:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    if sys.stderr and hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
except Exception:
    pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import tarfile
import urllib3
import argparse
import logging
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any, Callable
from pathlib import Path
import io
import signal


urllib3.disable_warnings()

VERSION = "v1.3.0"

MIRROR_SITES = {
    "1": {"name": "Docker Hub (官方)", "registry": "registry-1.docker.io"},
    "2": {"name": "1ms.run", "registry": "docker.1ms.run"},
    "3": {"name": "xuanyuan", "registry": "docker.xuanyuan.me"},
    "4": {"name": "xuanyuan(付费)", "registry": "docker.xuanyuan.cloud"},
    "5": {"name": "DaoCloud - Docker Hub", "registry": "docker.m.daocloud.io"},
    "6": {"name": "DaoCloud - K8s", "registry": "k8s.m.daocloud.io"},
    "7": {"name": "DaoCloud - NVCR", "registry": "nvcr.m.daocloud.io"},
    "8": {"name": "DaoCloud - GCR", "registry": "gcr.m.daocloud.io"},
    "9": {"name": "DaoCloud - GHCR", "registry": "ghcr.m.daocloud.io"},
    "10": {"name": "DaoCloud - Quay", "registry": "quay.m.daocloud.io"},
}

# 自定义 logging handler，用于将日志发送到 GUI
# 通过回调函数的方式，将日志消息实时传递到GUI界面显示
class GUILogHandler(logging.Handler):
    """
    GUI日志处理器
    
    功能：将Python标准日志系统的输出通过回调函数传递到GUI界面
    用途：在PyQt6界面中显示程序运行日志，包括下载进度、错误信息等
    
    属性:
        log_callback: 回调函数，接收格式化后的日志字符串
    """
    
    def __init__(self, log_callback=None):
        """
        初始化GUI日志处理器
        
        参数:
            log_callback: 回调函数，用于将日志发送到GUI显示
        """
        super().__init__()
        self.log_callback = log_callback

    def emit(self, record):
        """
        发送日志记录
        
        功能：格式化日志记录并通过回调函数发送到GUI
        参数:
            record: logging.LogRecord对象，包含日志级别、消息等信息
        """
        if self.log_callback:
            msg = self.format(record)
            self.log_callback(msg + '\n')


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

stop_event = threading.Event()
progress_lock = threading.Lock()
original_sigint_handler = None


def signal_handler(signum, frame):
    """信号处理函数：处理Ctrl+C中断信号，支持二次强制退出"""
    global stop_event
    if stop_event.is_set():
        print('\n⚠️ 强制退出...')
        if original_sigint_handler:
            signal.signal(signal.SIGINT, original_sigint_handler)
            raise KeyboardInterrupt
        sys.exit(1)
    
    stop_event.set()
    print('\n⚠️ 收到中断信号，正在保存进度并退出...')
    print('💡 再次按 Ctrl+C 强制退出')


original_sigint_handler = signal.signal(signal.SIGINT, signal_handler)


@dataclass
class ImageInfo:
    """镜像信息数据类：存储仓库地址、镜像名称、标签和协议等信息"""
    registry: str
    repository: str
    image_name: str
    tag: str
    protocol: str = 'https'


@dataclass
class DownloadStats:
    """下载统计信息：总大小、已下载大小、下载速度等"""
    total_size: int = 0
    downloaded_size: int = 0
    start_time: float = 0.0
    speeds: List[float] = field(default_factory=list)

    def get_avg_speed(self) -> float:
        """获取平均下载速度（取最近10次速度的平均值）"""
        if not self.speeds:
            return 0.0
        return sum(self.speeds[-10:]) / len(self.speeds[-10:])

    def format_size(self, size: int) -> str:
        """格式化文件大小显示（B/KB/MB/GB/TB）"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"

    def format_time(self, seconds: float) -> str:
        """格式化时间显示（秒/分秒/小时分）"""
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds // 60)}分{int(seconds % 60)}秒"
        else:
            return f"{int(seconds // 3600)}小时{int((seconds % 3600) // 60)}分"


class LayerProgress:
    """镜像层下载进度管理：跟踪每个层的下载状态、大小和分片信息"""
    def __init__(self, name: str, total_size: int, index: int, total_layers: int):
        self.name = name
        self.total_size = total_size
        self.downloaded_size = 0
        self.index = index
        self.total_layers = total_layers
        self.status = 'waiting'
        self.chunk_count = 0
        self.total_chunks = 0
        self.current_chunk = 0
        self.retry_count = 0
        self.is_resume = False

    def update(self, downloaded: int, chunk_info: str = ''):
        """更新已下载大小和分片信息"""
        self.downloaded_size = downloaded
        self.chunk_info = chunk_info

    def set_chunk_info(self, current: int, total: int):
        """设置当前分片序号和总分片数"""
        self.current_chunk = current
        self.total_chunks = total

    def set_total_size(self, total_size: int):
        """设置层总大小"""
        self.total_size = total_size

    @staticmethod
    def format_size(size: int) -> str:
        """格式化大小显示（B/KB/MB/GB/TB）"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


class ProgressDisplay:
    """进度显示管理：在终端或GUI中显示所有层的下载进度条"""
    def __init__(self, bar_width: int = 30, log_callback: Optional[Callable] = None, cli_output: bool = True):
        self.bar_width = bar_width
        self.layers: Dict[str, LayerProgress] = {}
        self.stats: Optional[DownloadStats] = None
        self.last_update = 0
        self.update_interval = 0.2
        self.initialized = False
        self.last_line_count = 0
        self.log_callback = log_callback  # GUI回调函数
        # 当 sys.stdout 为 None 时（PyInstaller -w 模式），禁用 CLI 输出
        self.cli_output = bool(cli_output and sys.stdout and hasattr(sys.stdout, 'write'))

    def add_layer(self, name: str, total_size: int, index: int, total_layers: int):
        """添加一个新的镜像层到进度显示列表中"""
        with progress_lock:
            self.layers[name] = LayerProgress(name, total_size, index, total_layers)
    
    def update_layer(self, name: str, downloaded: int):
        """更新指定层的已下载大小"""
        with progress_lock:
            if name in self.layers:
                self.layers[name].downloaded_size = downloaded
                self.layers[name].status = 'downloading'
        self._refresh_display()

    def update_layer_size(self, name: str, total_size: int):
        """更新指定层的总大小"""
        with progress_lock:
            if name in self.layers:
                self.layers[name].set_total_size(total_size)

    def complete_layer(self, name: str):
        """标记指定层为已完成状态"""
        with progress_lock:
            if name in self.layers:
                layer = self.layers[name]
                if layer.total_size == 0:
                    layer.total_size = layer.downloaded_size
                else:
                    layer.downloaded_size = layer.total_size
                layer.status = 'completed'
        self._refresh_display()
    
    def set_chunk_info(self, name: str, current: int, total: int):
        """设置指定层的分片下载信息"""
        with progress_lock:
            if name in self.layers:
                self.layers[name].current_chunk = current
                self.layers[name].total_chunks = total

    def _refresh_display(self):
        """刷新进度显示：计算当前进度并输出到终端或GUI"""
        current_time = time.time()
        if current_time - self.last_update < self.update_interval:
            return
        self.last_update = current_time

        with progress_lock:
            lines = []
            for name, layer in sorted(self.layers.items(), key=lambda x: x[1].index):
                line = self._format_layer_line(layer)
                lines.append(line)
            
            if self.stats:
                speed = self.stats.get_avg_speed()
                speed_str = self.stats.format_size(int(speed)) if speed > 0 else "0B"
                lines.append(f"📊 速度: {speed_str}/s")

            progress_text = "\n".join(lines)
            
            # GUI模式：发送进度到GUI
            if self.log_callback:
                self.log_callback(progress_text + "\n")
            
            # CLI模式：同时输出到终端（如果启用）
            if self.cli_output:
                # 终端模式：使用ANSI转义码刷新显示
                if self.initialized and self.last_line_count > 0:
                    for _ in range(self.last_line_count):
                        sys.stdout.write('\033[F')
                    sys.stdout.write('\033[J')
                
                for line in lines:
                    print(line)
                
                self.last_line_count = len(lines)
                self.initialized = True
                sys.stdout.flush()

    def _format_layer_line(self, layer: LayerProgress) -> str:
        """格式化单个层的进度显示行"""
        if layer.total_size > 0:
            progress = layer.downloaded_size / layer.total_size
        else:
            progress = 0

        filled = int(self.bar_width * progress)
        empty = self.bar_width - filled
        
        bar = '█' * filled + '░' * empty
        
        size_str = f"{layer.format_size(layer.downloaded_size)}/{layer.format_size(layer.total_size)}"
        
        chunk_info = ""
        if layer.total_chunks > 0:
            chunk_info = f" [{layer.current_chunk}/{layer.total_chunks}]"
        
        status_icon = "✅" if layer.status == 'completed' else "⬇️"
        
        retry_info = ""
        if layer.retry_count > 0:
            retry_info = f" 🔄{layer.retry_count}"
        
        resume_info = ""
        if layer.is_resume:
            resume_info = " 📎"
        
        total_layers_str = str(layer.total_layers)
        index_str = str(layer.index).rjust(len(total_layers_str))
        layer_info = f"({index_str}/{total_layers_str})"
        
        return f"  {status_icon} {layer_info} {layer.name:<12} |{bar}| {progress*100:5.1f}% {size_str:>15}{chunk_info}{retry_info}{resume_info}"

    def print_initial(self):
        """打印初始进度显示（所有层尚未开始下载）"""
        with progress_lock:
            for name, layer in sorted(self.layers.items(), key=lambda x: x[1].index):
                line = self._format_layer_line(layer)
                print(line)
            if self.stats:
                print(f"📊 速度: 计算中...")
            self.last_line_count = len(self.layers) + 1
            self.initialized = True


progress_display = ProgressDisplay()


def cancel_current_pull():
    """取消当前正在进行的拉取操作，发送停止信号并关闭会话连接"""
    global stop_event
    stop_event.set()
    logger.info('⚠️ 已发送取消信号')
    # 强制关闭所有进行中的session连接
    try:
        SessionManager.close_session()
    except Exception as e:
        logger.debug(f'关闭session时出错: {e}')


class SessionManager:
    """HTTP会话管理器：管理全局requests会话，支持连接池和代理"""
    _instance: Optional[requests.Session] = None
    _lock = threading.Lock()

    @classmethod
    def get_session(cls) -> requests.Session:
        """获取全局HTTP会话实例（单例模式）"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls._create_session()
            return cls._instance

    @classmethod
    def close_session(cls):
        """关闭当前session，强制终止所有进行中的请求"""
        with cls._lock:
            if cls._instance is not None:
                try:
                    # 关闭所有适配器连接池
                    cls._instance.close()
                except Exception as e:
                    logger.debug(f'关闭session连接时出错: {e}')
                finally:
                    cls._instance = None

    @classmethod
    def _create_session(cls) -> requests.Session:
        """创建配置好的HTTP会话：设置重试策略、连接池和代理"""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,    # http/https连接超时重试次数
            backoff_factor=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=50,
            pool_block=False
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.timeout = (30, 600)    # http/https连接超时30秒, 读取超时600秒

        session.proxies = {
            'http': os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'),
            'https': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        }
        if session.proxies.get('http') or session.proxies.get('https'):
            logger.info('🌐 使用代理设置从环境变量')

        return session


def _normalize_registry(reg: str) -> str:
    """规范化仓库字符串：移除协议与尾部斜杠"""
    if not reg:
        return ''
    r = str(reg).strip()
    if r.startswith('http://'):
        r = r[len('http://'):]
    elif r.startswith('https://'):
        r = r[len('https://'):]
    return r.rstrip('/')


def _get_namespace_from_docker_hub(image_name: str) -> str:
    """
    根据镜像名称判断 namespace。
    
    规则：
    - 如果镜像名称包含 '/'，说明已经包含 namespace，直接使用
    - 如果镜像名称不包含 '/'，使用 'library' 前缀
    
    返回正确的 namespace，默认为 'library'。
    """
    # 根据镜像名是否包含 '/' 来判断 namespace
    if '/' in image_name:
        # 包含 / 说明是 namespace/image 格式
        return image_name.split('/')[0]
    else:
        # 不包含 / 的默认是 library 官方镜像
        return 'library'


def parse_www_authenticate(header_value: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """解析 WWW-Authenticate 头，支持 Bearer 和 Basic。
    返回 (scheme, realm_url, service_name)。可能返回 None。
    """
    if not header_value:
        return None, None, None
    # 方案名称（例如 Bearer 或 Basic）
    scheme = header_value.split()[0] if header_value.split() else None
    realm = None
    service = None
    # 优先解析带引号的参数
    m = re.search(r'realm="([^"]+)"', header_value)
    if m:
        realm = m.group(1)
    m = re.search(r'service="([^"]+)"', header_value)
    if m:
        service = m.group(1)
    # 兼容不带引号的写法
    if realm is None:
        m = re.search(r'realm=([^,\s]+)', header_value)
        if m:
            realm = m.group(1)
    if service is None:
        m = re.search(r'service=([^,\s]+)', header_value)
        if m:
            service = m.group(1)
    return scheme, realm, service


def load_auth_credentials(current_registry_host: str) -> Tuple[Optional[str], Optional[str]]:
    """读取 auth.json 中的认证信息，支持多仓库配置。
    匹配当前仓库后返回 (username, password)，否则返回 (None, None)。
    支持以下结构：
    - 单对象：{"registry": "host:port", "username": "u", "password": "p"}
      兼容键名形如 "registry1"、"registry2" 等前缀。
    - 列表：[{...}, {...}]（同上键名兼容）
    - 映射：{"auths": {"host:port": {"username": "u", "password": "p"}}}
    """
    hostnorm = _normalize_registry(current_registry_host)
    try:
        if os.path.exists('auth.json'):
            with open('auth.json', 'r', encoding='utf-8') as f:
                data = json.load(f)

            def _extract_registry_value(obj: dict):
                if 'registry' in obj:
                    return obj.get('registry')
                for k in obj.keys():
                    if isinstance(k, str) and k.lower().startswith('registry'):
                        return obj.get(k)
                return None

            # 列表形式
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        reg_val = _extract_registry_value(item)
                        if reg_val and _normalize_registry(reg_val) == hostnorm:
                            user = item.get('username')
                            pwd = item.get('password')
                            if user and pwd:
                                return user, pwd

            # 字典形式
            elif isinstance(data, dict):
                # 单对象
                reg_val = _extract_registry_value(data) if isinstance(data, dict) else None
                if reg_val and all(k in data for k in ('username', 'password')):
                    if _normalize_registry(reg_val) == hostnorm:
                        return data.get('username'), data.get('password')
                # 映射：auths
                elif isinstance(data.get('auths'), dict):
                    for reg, val in data.get('auths', {}).items():
                        if _normalize_registry(reg) == hostnorm and isinstance(val, dict):
                            user = val.get('username')
                            pwd = val.get('password')
                            if user and pwd:
                                return user, pwd
                # 列表嵌套：entries
                elif isinstance(data.get('entries'), list):
                    for item in data.get('entries'):
                        if isinstance(item, dict):
                            reg_val = _extract_registry_value(item)
                            if reg_val and _normalize_registry(reg_val) == hostnorm:
                                user = item.get('username')
                                pwd = item.get('password')
                                if user and pwd:
                                    return user, pwd
    except Exception:
        # 忽略解析错误，返回空凭据
        pass
    return None, None


def get_output_dir(repository: str, tag: str, arch: str, output_path: Optional[str] = None) -> Path:
    """获取输出目录路径，创建以镜像名_tag_arch命名的目录"""
    safe_repo = repository.replace("/", "_").replace(":", "_")
    dir_name = f"{safe_repo}_{tag}_{arch}"

    if output_path:
        output_dir = Path(output_path) / dir_name
    else:
        output_dir = Path.cwd() / dir_name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def parse_image_input(image_input: str, custom_registry: Optional[str] = None) -> ImageInfo:
    """解析用户输入的镜像字符串，提取仓库地址、镜像名称和标签等信息"""
    registry_host = custom_registry
    protocol = 'https'  # 默认使用https
    
    if registry_host:
        # 提取并移除协议前缀
        if registry_host.startswith('https://'):
            protocol = 'https'
            registry_host = registry_host[8:]
        elif registry_host.startswith('http://'):
            protocol = 'http'
            registry_host = registry_host[7:]
        # 移除尾部斜杠
        registry_host = registry_host.rstrip('/')

    # 检查image_input是否包含协议前缀
    if image_input.startswith('https://'):
        protocol = 'https'
        image_input = image_input[8:]
    elif image_input.startswith('http://'):
        protocol = 'http'
        image_input = image_input[7:]

    if '/' in image_input and ('.' in image_input.split('/')[0] or ':' in image_input.split('/')[0]):
        registry, remainder = image_input.split('/', 1)
        parts = remainder.split('/')

        if len(parts) == 1:
            repo = ''
            img_tag = parts[0]
        else:
            repo = '/'.join(parts[:-1])
            img_tag = parts[-1]

        img, *tag_parts = img_tag.split(':')
        tag = tag_parts[0] if tag_parts else 'latest'
        repository = remainder.split(':')[0]

        return ImageInfo(registry, repository, img, tag, protocol)
    else:
        parts = image_input.split('/')
        if len(parts) == 1:
            # 单名称镜像（如 java, nginx），需要判断 namespace
            img_tag = parts[0]
            img, *tag_parts = img_tag.split(':')
            tag = tag_parts[0] if tag_parts else 'latest'
            
            # 尝试从 Docker Hub API 获取 namespace，默认为 library
            namespace = _get_namespace_from_docker_hub(img)
            repository = f'{namespace}/{img}'
        else:
            repo = '/'.join(parts[:-1])
            img_tag = parts[-1]
            img, *tag_parts = img_tag.split(':')
            tag = tag_parts[0] if tag_parts else 'latest'
            repository = f'{repo}/{img}'

        if not registry_host:
            registry = 'registry-1.docker.io'
        else:
            registry = registry_host

        return ImageInfo(registry, repository, img, tag, protocol)


def get_auth_head(
    session: requests.Session,
    auth_url: str,
    reg_service: str,
    repository: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    max_retries: int = 3    # 认证请求重试次数
) -> Dict[str, str]:
    """向认证服务器请求Bearer token，返回带认证头的请求头字典"""
    for attempt in range(max_retries):
        try:
            url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'

            headers = {}
            if username and password:
                auth_string = f"{username}:{password}"
                encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                headers['Authorization'] = f'Basic {encoded_auth}'

            logger.debug(f"获取认证头: {url}")

            resp = session.get(url, headers=headers, verify=False, timeout=60)
            resp.raise_for_status()
            access_token = resp.json()['token']
            auth_head = {
                'Authorization': f'Bearer {access_token}',
                'Accept': ', '.join([
                    'application/vnd.docker.distribution.manifest.v2+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.index.v1+json',
                    'application/vnd.oci.image.manifest.v1+json',
                ])
            }

            return auth_head
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f'认证请求失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}')
                time.sleep(wait_time)
            else:
                logger.error(f'请求认证失败: {e}')
                raise


def _get_available_tags_from_docker_hub(repository: str) -> List[str]:
    """
    从 Docker Hub API 获取镜像的可用标签列表（用于错误提示）
    """
    try:
        # 解析 namespace 和 image name
        if '/' in repository:
            namespace, image = repository.rsplit('/', 1)
        else:
            namespace, image = 'library', repository
        
        url = f'https://hub.docker.com/v2/repositories/{namespace}/{image}/tags/'
        resp = requests.get(url, params={'page_size': 10}, timeout=5, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get('results', [])
            return [tag.get('name') for tag in results if tag.get('name')]
    except Exception:
        pass
    return []


def fetch_manifest(
    session: requests.Session,
    registry: str,
    repository: str,
    tag: str,
    auth_head: Dict[str, str],
    protocol: str = 'https',
    max_retries: int = 3    # 清单获取重试次数
) -> Tuple[requests.Response, int]:
    """获取镜像清单（manifest），返回响应对象和HTTP状态码"""
    for attempt in range(max_retries):
        try:
            url = f'{protocol}://{registry}/v2/{repository}/manifests/{tag}'
            logger.debug(f'获取镜像清单: {url}')

            resp = session.get(url, headers=auth_head, verify=False, timeout=60)
            if resp.status_code == 401:
                logger.info('需要认证。')
                return resp, 401
            if resp.status_code == 404:
                # Tag 不存在，尝试获取可用标签列表
                logger.error(f'镜像标签 "{tag}" 不存在')
                available_tags = _get_available_tags_from_docker_hub(repository)
                if available_tags:
                    logger.info(f'💡 可用标签: {", ".join(available_tags[:10])}{"..." if len(available_tags) > 10 else ""}')
                    logger.info(f'💡 请使用 -i {repository.split("/")[-1]}:<tag> 指定正确的标签')
                return resp, 404
            resp.raise_for_status()
            return resp, 200
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f'清单请求失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}')
                time.sleep(wait_time)
            else:
                logger.error(f'请求清单失败: {e}')
                raise


def select_manifest(manifests: List[Dict], arch: str) -> Optional[str]:
    """从多架构清单中选择指定架构的镜像digest"""
    for m in manifests:
        if (m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') == arch or
            m.get('platform', {}).get('architecture') == arch) and \
                m.get('platform', {}).get('os') == 'linux':
            return m.get('digest')
    return None


class DownloadProgressManager:
    """下载进度管理器：保存和恢复下载进度，支持断点续传"""
    def __init__(self, output_dir: Path, repository: str, tag: str, arch: str):
        self.output_dir = output_dir
        self.repository = repository
        self.tag = tag
        self.arch = arch
        self.progress_file = output_dir / 'progress.json'
        self.progress_data = self.load_progress()

    def load_progress(self) -> Dict[str, Any]:
        """从进度文件加载已保存的下载进度"""
        if self.progress_file.exists():
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                    metadata = data.get('metadata', {})
                    if (metadata.get('repository') == self.repository and
                            metadata.get('tag') == self.tag and
                            metadata.get('arch') == self.arch):

                        logger.info(f'📋 加载已有下载进度，共 {len(data.get("layers", {}))} 个文件')
                        return data
                    else:
                        logger.warning(f'进度文件镜像信息不匹配，将创建新的进度')
                        return self._create_new_progress()

            except Exception as e:
                logger.warning(f'加载进度文件失败: {e}')

        return self._create_new_progress()

    def _create_new_progress(self) -> Dict[str, Any]:
        """创建新的进度数据结构"""
        return {
            'metadata': {
                'repository': self.repository,
                'tag': self.tag,
                'arch': self.arch,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
            },
            'layers': {},
            'config': None
        }

    def save_progress(self):
        """保存当前下载进度到文件"""
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f'保存进度文件失败: {e}')

    def update_layer_status(self, digest: str, status: str, **kwargs):
        """更新指定层的下载状态"""
        if digest not in self.progress_data['layers']:
            self.progress_data['layers'][digest] = {}

        self.progress_data['layers'][digest]['status'] = status
        self.progress_data['layers'][digest].update(kwargs)
        self.save_progress()

    def get_layer_status(self, digest: str) -> Dict[str, Any]:
        """获取指定层的下载状态"""
        return self.progress_data['layers'].get(digest, {})

    def is_layer_completed(self, digest: str) -> bool:
        """检查指定层是否已完成下载"""
        layer_info = self.get_layer_status(digest)
        return layer_info.get('status') == 'completed'

    def update_config_status(self, status: str, **kwargs):
        """更新Config文件的下状态"""
        if self.progress_data['config'] is None:
            self.progress_data['config'] = {}
        self.progress_data['config']['status'] = status
        self.progress_data['config'].update(kwargs)
        self.save_progress()

    def is_config_completed(self) -> bool:
        """检查Config文件是否已完成下载"""
        config_data = self.progress_data.get('config')
        if config_data is None:
            return False
        return config_data.get('status') == 'completed'

    def clear_progress(self):
        """清除进度文件（下载完成后调用）"""
        if self.progress_file.exists():
            try:
                self.progress_file.unlink()
                logger.debug('进度文件已清除')
            except Exception as e:
                logger.error(f'清除进度文件失败: {e}')


def get_file_size(session: requests.Session, url: str, headers: Dict[str, str]) -> int:
    """通过HEAD请求获取文件大小（字节数）"""
    try:
        resp = session.head(url, headers=headers, verify=False, timeout=30)
        if resp.status_code == 200:
            return int(resp.headers.get('content-length', 0))
    except:
        pass
    return 0


def download_file_with_progress(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    save_path: str,
    desc: str,
    expected_digest: Optional[str] = None,
    max_retries: int = 10,    # 文件下载重试次数
    stats: Optional[DownloadStats] = None,
    chunk_size: int = 10 * 1024 * 1024
) -> bool:
    """带进度显示的文件下载函数，支持断点续传和SHA256校验"""
    CHUNK_THRESHOLD = 50 * 1024 * 1024
    
    for attempt in range(max_retries):
        if stop_event.is_set():
            return False

        resume_pos = 0
        if os.path.exists(save_path):
            resume_pos = os.path.getsize(save_path)
            if resume_pos > 0 and attempt == 0:
                logger.info(f'📎 {desc} 检测到已下载 {LayerProgress.format_size(resume_pos)}，尝试断点续传...')

        download_headers = headers.copy()
        if resume_pos > 0:
            download_headers['Range'] = f'bytes={resume_pos}-'

        try:
            with session.get(url, headers=download_headers, verify=False, timeout=120, stream=True) as resp:
                if resp.status_code == 416:
                    progress_display.complete_layer(desc)
                    return True

                resp.raise_for_status()

                content_range = resp.headers.get('content-range')
                if content_range:
                    total_size = int(content_range.split('/')[1])
                else:
                    total_size = int(resp.headers.get('content-length', 0)) + resume_pos

                progress_display.update_layer_size(desc, total_size)

                if total_size - resume_pos > CHUNK_THRESHOLD and resume_pos == 0:
                    return download_file_in_chunks(
                        session, url, headers, save_path, desc, 
                        total_size, expected_digest, max_retries, stats, chunk_size
                    )

                mode = 'ab' if resume_pos > 0 else 'wb'
                sha256_hash = hashlib.sha256() if expected_digest else None

                if resume_pos > 0 and sha256_hash:
                    with open(save_path, 'rb') as existing_file:
                        while True:
                            chunk = existing_file.read(65536)
                            if not chunk:
                                break
                            sha256_hash.update(chunk)

                if stats:
                    stats.total_size += total_size - resume_pos
                    if stats.start_time == 0:
                        stats.start_time = time.time()

                downloaded_size = resume_pos
                last_update_time = time.time()
                last_downloaded = resume_pos

                with open(save_path, mode) as file:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if stop_event.is_set():
                            return False

                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)

                            if sha256_hash:
                                sha256_hash.update(chunk)

                            progress_display.update_layer(desc, downloaded_size)

                            if stats:
                                current_time = time.time()
                                if current_time - last_update_time >= 0.5:
                                    speed = (downloaded_size - last_downloaded) / (current_time - last_update_time)
                                    stats.speeds.append(speed)
                                    last_downloaded = downloaded_size
                                    last_update_time = current_time

                if expected_digest and sha256_hash:
                    actual_digest = f'sha256:{sha256_hash.hexdigest()}'
                    if actual_digest != expected_digest:
                        logger.error(f'❌ {desc} 校验失败！')
                        if os.path.exists(save_path):
                            os.remove(save_path)
                        if attempt < max_retries - 1:
                            wait_time = min(2 ** attempt, 60)
                            time.sleep(wait_time)
                        continue

                progress_display.complete_layer(desc)
                return True

        except KeyboardInterrupt:
            return False
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 60)
                logger.info(f'🔄 {desc} 连接超时/失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries})')
                time.sleep(wait_time)
                continue
            else:
                logger.error(f'❌ {desc} 下载失败')
                return False
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            # 检查是否已取消
            if stop_event.is_set():
                return False
            # 400错误可能是认证令牌过期或权限问题，尝试刷新认证
            if status_code == 400 and attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)
                logger.warning(f'🔄 {desc} HTTP 400 (可能是认证问题)，{wait_time}秒后重试 ({attempt + 1}/{max_retries})')
                # 使用stop_event.wait代替time.sleep，可以立即响应取消信号
                if stop_event.wait(wait_time):
                    return False
                continue
            elif status_code in [401, 403]:
                logger.error(f'❌ {desc} 下载失败: 认证失败或无权限访问 (HTTP {status_code})')
                logger.info(f'💡 提示：该镜像可能需要认证，请检查用户名和密码是否正确')
                return False
            elif status_code in [429, 500, 502, 503, 504] and attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 60)
                logger.info(f'🔄 {desc} HTTP {status_code}，{wait_time}秒后重试 ({attempt + 1}/{max_retries})')
                # 使用stop_event.wait代替time.sleep，可以立即响应取消信号
                if stop_event.wait(wait_time):
                    return False
                continue
            else:
                logger.error(f'❌ {desc} 下载失败: HTTP {status_code} - {e}')
                return False
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 60)
                logger.info(f'🔄 {desc} 下载异常，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}')
                # 使用stop_event.wait代替time.sleep，可以立即响应取消信号
                if stop_event.wait(wait_time):
                    return False
                continue
            logger.error(f'❌ {desc} 下载失败: {e}')
            return False

    return False


def download_file_in_chunks(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    save_path: str,
    desc: str,
    total_size: int,
    expected_digest: Optional[str] = None,
    max_retries: int = 10,    # 分片下载重试次数
    stats: Optional[DownloadStats] = None,
    chunk_size: int = 10 * 1024 * 1024
) -> bool:
    """分片下载大文件，将文件分成多个小块并发下载，最后合并"""
    num_chunks = (total_size + chunk_size - 1) // chunk_size
    temp_dir = save_path + '.chunks'
    
    progress_display.set_chunk_info(desc, 0, num_chunks)
    
    try:
        os.makedirs(temp_dir, exist_ok=True)
        
        chunk_files = []
        for i in range(num_chunks):
            start = i * chunk_size
            end = min((i + 1) * chunk_size, total_size)
            chunk_file = os.path.join(temp_dir, f'chunk_{i:04d}')
            chunk_files.append((start, end, chunk_file))
        
        completed_size = 0
        for existing_start, existing_end, existing_chunk_file in chunk_files:
            if os.path.exists(existing_chunk_file):
                completed_size += os.path.getsize(existing_chunk_file)
        
        if stats:
            stats.total_size += total_size - completed_size
            if stats.start_time == 0:
                stats.start_time = time.time()
        
        sha256_hash = hashlib.sha256() if expected_digest else None
        completed_chunks = [False] * num_chunks
        chunk_sizes = [end - start for start, end, _ in chunk_files]
        
        def download_single_chunk(i: int, start: int, end: int, chunk_file: str) -> bool:
            """下载单个分片的内部函数"""
            if stop_event.is_set():
                return False
            
            if os.path.exists(chunk_file):
                existing_size = os.path.getsize(chunk_file)
                if existing_size == end - start:
                    return True
                else:
                    os.remove(chunk_file)
            
            chunk_headers = headers.copy()
            chunk_headers['Range'] = f'bytes={start}-{end-1}'
            
            for attempt in range(max_retries):
                if stop_event.is_set():
                    return False
                
                try:
                    with session.get(url, headers=chunk_headers, verify=False, timeout=120, stream=True) as resp:
                        resp.raise_for_status()
                        
                        with open(chunk_file, 'wb') as f:
                            for data in resp.iter_content(chunk_size=65536):
                                if stop_event.is_set():
                                    return False
                                if data:
                                    f.write(data)
                        
                        if os.path.getsize(chunk_file) == end - start:
                            return True
                        else:
                            if os.path.exists(chunk_file):
                                os.remove(chunk_file)
                            if attempt < max_retries - 1:
                                wait_time = min(2 ** attempt, 60)
                                time.sleep(wait_time)
                                continue
                            return False
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = min(2 ** attempt, 60)
                        logger.info(f'🔄 {desc} 分片 {i+1} 下载失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}')
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f'❌ {desc} 分片 {i+1} 下载失败: {e}')
                        return False
            
            return False
        
        max_workers = min(num_chunks, 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, (start, end, chunk_file) in enumerate(chunk_files):
                if os.path.exists(chunk_file) and os.path.getsize(chunk_file) == end - start:
                    completed_chunks[i] = True
                    continue
                futures[executor.submit(download_single_chunk, i, start, end, chunk_file)] = i
            
            while futures:
                for future in list(futures.keys()):
                    if future.done():
                        i = futures.pop(future)
                        try:
                            result = future.result()
                            if result:
                                completed_chunks[i] = True
                                progress_display.set_chunk_info(desc, sum(completed_chunks), num_chunks)
                            else:
                                logger.error(f'❌ {desc} 分片 {i+1} 下载失败')
                                return False
                        except Exception as e:
                            logger.error(f'❌ {desc} 分片 {i+1} 下载异常: {e}')
                            return False
                
                current_completed = sum(1 for c in completed_chunks if c)
                current_size = sum(chunk_sizes[i] for i in range(num_chunks) if completed_chunks[i])
                progress_display.update_layer(desc, current_size)
                progress_display.set_chunk_info(desc, current_completed, num_chunks)
                
                time.sleep(0.1)
        
        logger.info(f'{desc}: 合并 {num_chunks} 个分片...')
        
        with open(save_path, 'wb') as outfile:
            for i, (_, _, chunk_file) in enumerate(chunk_files):
                if stop_event.is_set():
                    return False
                
                with open(chunk_file, 'rb') as infile:
                    while True:
                        data = infile.read(65536)
                        if not data:
                            break
                        outfile.write(data)
                        if sha256_hash:
                            sha256_hash.update(data)
        
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        if expected_digest and sha256_hash:
            actual_digest = f'sha256:{sha256_hash.hexdigest()}'
            if actual_digest != expected_digest:
                logger.error(f'❌ {desc} 校验失败！')
                if os.path.exists(save_path):
                    os.remove(save_path)
                return False
        
        progress_display.complete_layer(desc)
        return True
        
    except Exception as e:
        logger.error(f'❌ {desc} 分片下载失败: {e}')
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        return False


def download_layers(
    session: requests.Session,
    registry: str,
    repository: str,
    layers: List[Dict],
    auth_head: Dict[str, str],
    imgdir: str,
    resp_json: Dict,
    imgparts: List[str],
    img: str,
    tag: str,
    arch: str,
    output_dir: Path,
    log_callback: Optional[Callable] = None,
    protocol: str = 'https'
):
    """下载所有镜像层，包括Config文件和各个layer，支持断点续传"""
    global progress_display
    progress_display = ProgressDisplay(log_callback=log_callback)

    os.makedirs(imgdir, exist_ok=True)

    progress_manager = DownloadProgressManager(output_dir, repository, tag, arch)
    stats = DownloadStats()
    progress_display.stats = stats

    try:
        config_digest = resp_json['config']['digest']
        config_filename = f'{config_digest[7:]}.json'
        config_path = os.path.join(imgdir, config_filename)
        config_url = f'{protocol}://{registry}/v2/{repository}/blobs/{config_digest}'

        if progress_manager.is_config_completed() and os.path.exists(config_path):
            logger.info(f'✅ Config 已存在，跳过下载')
        else:
            progress_manager.update_config_status('downloading', digest=config_digest)
            
            # 尝试获取config大小，如果失败则使用默认值
            try:
                config_size = get_file_size(session, config_url, auth_head)
            except Exception as e:
                logger.debug(f'获取Config大小失败: {e}，使用默认值')
                config_size = 0
            
            progress_display.add_layer('Config', config_size, 0, len(layers) + 1)
            
            # 下载config，添加特殊错误处理
            try:
                success = download_file_with_progress(
                    session, config_url, auth_head, config_path, "Config",
                    expected_digest=config_digest, stats=stats
                )
                # 检查是否已取消
                if stop_event.is_set():
                    raise KeyboardInterrupt("用户已取消操作")
                
                if not success:
                    # 如果下载失败，检查是否是认证问题
                    logger.error(f'❌ Config 下载失败')
                    # 尝试不带认证头重新下载（某些仓库可能不需要认证）
                    if 'Authorization' in auth_head:
                        logger.info('🔄 尝试使用匿名方式重新下载Config...')
                        anon_headers = {k: v for k, v in auth_head.items() if k != 'Authorization'}
                        success = download_file_with_progress(
                            session, config_url, anon_headers, config_path, "Config",
                            expected_digest=config_digest, stats=stats
                        )
                        # 检查是否已取消
                        if stop_event.is_set():
                            raise KeyboardInterrupt("用户已取消操作")
                    
                    if not success:
                        progress_manager.update_config_status('failed')
                        # Config下载失败不终止整个流程，继续尝试下载layers
                        logger.warning('⚠️ Config下载失败，尝试继续下载镜像层...')
                    else:
                        progress_manager.update_config_status('completed', digest=config_digest)
                else:
                    progress_manager.update_config_status('completed', digest=config_digest)
            except KeyboardInterrupt:
                raise
            except Exception as download_e:
                logger.error(f'❌ Config 下载异常: {download_e}')
                progress_manager.update_config_status('failed')
                # 不终止流程，继续尝试下载layers
                logger.warning('⚠️ Config下载异常，尝试继续下载镜像层...')

    except Exception as e:
        logging.error(f'请求配置失败: {e}')
        # 不终止流程，继续尝试下载layers
        logger.warning('⚠️ 配置处理失败，尝试继续下载镜像层...')

    repo_tag = f'{"/".join(imgparts)}/{img}:{tag}' if imgparts else f'{img}:{tag}'
    content = [{'Config': config_filename, 'RepoTags': [repo_tag], 'Layers': []}]
    parentid = ''
    layer_json_map: Dict[str, Dict] = {}

    layers_to_download = []
    skipped_count = 0

    for layer in layers:
        ublob = layer['digest']
        fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
        layerdir = f'{imgdir}/{fake_layerid}'
        os.makedirs(layerdir, exist_ok=True)
        layer_json_map[fake_layerid] = {"id": fake_layerid, "parent": parentid if parentid else None}
        parentid = fake_layerid

        save_path = f'{layerdir}/layer_gzip.tar'

        if progress_manager.is_layer_completed(ublob) and os.path.exists(save_path):
            skipped_count += 1
        else:
            layers_to_download.append((ublob, fake_layerid, layerdir, save_path))

    if skipped_count > 0:
        logger.info(f'📦 跳过 {skipped_count} 个已下载的层，还需下载 {len(layers_to_download)} 个层')

    for idx, (ublob, fake_layerid, layerdir, save_path) in enumerate(layers_to_download):
        url = f'{protocol}://{registry}/v2/{repository}/blobs/{ublob}'
        layer_size = get_file_size(session, url, auth_head)
        progress_display.add_layer(ublob[:12], layer_size, idx + 1, len(layers_to_download))

    progress_display.print_initial()

    num_workers = min(len(layers_to_download), 4) if layers_to_download else 1

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {}
        try:
            for idx, (ublob, fake_layerid, layerdir, save_path) in enumerate(layers_to_download):
                if stop_event.is_set():
                    raise KeyboardInterrupt

                url = f'{protocol}://{registry}/v2/{repository}/blobs/{ublob}'
                progress_manager.update_layer_status(ublob, 'downloading')

                futures[executor.submit(
                    download_file_with_progress,
                    session,
                    url,
                    auth_head,
                    save_path,
                    ublob[:12],
                    expected_digest=ublob,
                    stats=stats
                )] = (ublob, save_path)

            for future in as_completed(futures):
                if stop_event.is_set():
                    raise KeyboardInterrupt

                ublob, save_path = futures[future]
                result = future.result()

                if not result:
                    progress_manager.update_layer_status(ublob, 'failed')
                    raise Exception(f'层 {ublob[:12]} 下载失败')
                else:
                    progress_manager.update_layer_status(ublob, 'completed')

        except KeyboardInterrupt:
            logging.error("用户终止下载，保存当前进度...")
            stop_event.set()
            executor.shutdown(wait=False)
            raise

    # CLI模式下才打印空行，GUI模式下跳过
    if sys.stdout and hasattr(sys.stdout, 'write'):
        print()

    for fake_layerid in layer_json_map.keys():
        if stop_event.is_set():
            raise KeyboardInterrupt("用户已取消操作")

        layerdir = f'{imgdir}/{fake_layerid}'
        gz_path = f'{layerdir}/layer_gzip.tar'
        tar_path = f'{layerdir}/layer.tar'

        if os.path.exists(gz_path):
            with gzip.open(gz_path, 'rb') as gz, open(tar_path, 'wb') as file:
                shutil.copyfileobj(gz, file)
            os.remove(gz_path)

        json_path = f'{layerdir}/json'
        with open(json_path, 'w') as file:
            json.dump(layer_json_map[fake_layerid], file)

        content[0]['Layers'].append(f'{fake_layerid}/layer.tar')

    manifest_path = os.path.join(imgdir, 'manifest.json')
    with open(manifest_path, 'w') as file:
        json.dump(content, file)

    repositories_path = os.path.join(imgdir, 'repositories')
    with open(repositories_path, 'w') as file:
        json.dump({repository if '/' in repository else img: {tag: parentid}}, file)

    if stats.start_time > 0:
        elapsed = time.time() - stats.start_time
        avg_speed = stats.get_avg_speed()
        logger.info(f'📊 平均下载速度: {stats.format_size(int(avg_speed))}/s')
        logger.info(f'⏱️  总耗时: {stats.format_time(elapsed)}')

    logging.info(f'✅ 镜像 {img}:{tag} 下载完成！')
    progress_manager.clear_progress()


def create_image_tar(imgdir: str, repository: str, tag: str, arch: str, output_dir: Path) -> str:
    """将下载的镜像层打包成Docker兼容的tar文件，并清理临时目录"""
    safe_repo = repository.replace("/", "_")
    docker_tar = str(output_dir / f'{safe_repo}_{tag}_{arch}.tar')
    try:
        with tarfile.open(docker_tar, "w") as tar:
            tar.add(imgdir, arcname='/')
        logger.debug(f'Docker 镜像已拉取：{docker_tar}')
        
        try:
            if os.path.exists(imgdir):
                shutil.rmtree(imgdir)
                logger.debug(f'已清理 layers 目录: {imgdir}')
        except Exception as e:
            logger.warning(f'清理 layers 目录失败: {e}')
        
        return docker_tar
    except Exception as e:
        logger.error(f'打包镜像失败: {e}')
        raise


def cleanup_tmp_dir():
    """清理临时目录（tmp目录），释放磁盘空间"""
    tmp_dir = 'tmp'
    try:
        if os.path.exists(tmp_dir):
            logger.debug(f'清理临时目录: {tmp_dir}')
            shutil.rmtree(tmp_dir)
            logger.debug('临时目录已清理。')
    except Exception as e:
        logger.error(f'清理临时目录失败: {e}')


def _get_default_auth_head() -> Dict[str, str]:
    """返回默认的匿名认证头（无Authorization字段）"""
    return {
        'Accept': ', '.join([
            'application/vnd.docker.distribution.manifest.v2+json',
            'application/vnd.docker.distribution.manifest.list.v2+json',
            'application/vnd.oci.image.index.v1+json',
            'application/vnd.oci.image.manifest.v1+json',
        ])
    }


def _create_basic_auth_head(username: str, password: str) -> Dict[str, str]:
    """创建Basic认证头，对用户名密码进行Base64编码"""
    auth_string = f"{username}:{password}"
    encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
    return {
        'Authorization': f'Basic {encoded_auth}',
        'Accept': ', '.join([
            'application/vnd.docker.distribution.manifest.v2+json',
            'application/vnd.docker.distribution.manifest.list.v2+json',
            'application/vnd.oci.image.index.v1+json',
            'application/vnd.oci.image.manifest.v1+json',
        ])
    }


def _handle_authentication(
    session: requests.Session,
    registry: str,
    repository: str,
    username: Optional[str],
    password: Optional[str],
    protocol: str = 'https'
) -> Tuple[Dict[str, str], bool, Optional[str]]:
    """处理认证流程，支持 Bearer 和 Basic 认证，支持从 auth.json 加载凭据。
    
    返回:
        auth_head: 认证头字典
        auth_success: 认证是否成功
        error_msg: 错误信息（如果失败）
    """
    # 1. 探测仓库认证方式
    url = f'{protocol}://{registry}/v2/'
    logger.info(f'🔍 使用 {protocol.upper()} 连接仓库')
    logger.info(f'🔍 探测仓库入口: {url}')
    
    try:
        resp = session.get(url, verify=False, timeout=60)
    except requests.exceptions.RequestException as e:
        return _get_default_auth_head(), False, f'连接仓库失败: {e}'
    
    # 2. 如果不需要认证（200），返回默认头
    if resp.status_code == 200:
        logger.info('🔓 仓库无需认证，使用匿名访问')
        return _get_default_auth_head(), True, None
    
    # 3. 解析认证头
    if resp.status_code != 401:
        # 其他非200/401状态码
        return _get_default_auth_head(), False, f'仓库返回错误状态码: {resp.status_code}'
    
    www_auth = resp.headers.get('WWW-Authenticate', '')
    if not www_auth:
        return _get_default_auth_head(), False, '服务器返回401但缺少WWW-Authenticate头'
    
    scheme, auth_url, reg_service = parse_www_authenticate(www_auth)
    
    # 4. 获取凭证优先级：传入参数 > auth.json > 环境变量
    effective_username = username
    effective_password = password
    credential_source = None
    
    if not effective_username or not effective_password:
        # 尝试从 auth.json 加载
        user_from_file, pwd_from_file = load_auth_credentials(registry)
        if user_from_file and pwd_from_file:
            effective_username = user_from_file
            effective_password = pwd_from_file
            credential_source = 'auth.json'
            logger.info('📄 从 auth.json 加载认证凭据')
        else:
            # 尝试环境变量
            env_user = (os.environ.get('DOCKER_REGISTRY_USERNAME') or 
                       os.environ.get('REGISTRY_USERNAME'))
            env_pwd = (os.environ.get('DOCKER_REGISTRY_PASSWORD') or 
                      os.environ.get('REGISTRY_PASSWORD'))
            if env_user and env_pwd:
                effective_username = env_user
                effective_password = env_pwd
                credential_source = '环境变量'
                logger.info('🌐 从环境变量加载认证凭据')
    else:
        credential_source = '传入参数'
    
    # 5. Bearer 认证流程
    if scheme and scheme.lower().startswith('bearer') and auth_url and reg_service:
        logger.info(f'🔐 仓库使用 Bearer 认证')
        if credential_source:
            logger.info(f'🔒 使用认证凭据（来源: {credential_source}）')
        else:
            logger.info('🔓 尝试匿名访问')
        
        try:
            auth_head = get_auth_head(
                session, auth_url, reg_service, repository,
                effective_username, effective_password
            )
            logger.info('✅ Bearer 认证成功')
            return auth_head, True, None
        except Exception as e:
            if 'Invalid username/password' in str(e) or '401' in str(e):
                return _get_default_auth_head(), False, f'认证失败，请检查用户名和密码: {e}'
            # 如果验证失败但没有提供凭据，返回匿名头允许重试
            if not effective_username:
                return _get_default_auth_head(), False, f'匿名认证失败，该镜像需要登录: {e}'
            return _get_default_auth_head(), False, f'认证失败: {e}'
    
    # 6. Basic 认证流程
    elif scheme and scheme.lower().startswith('basic'):
        logger.info(f'🔐 仓库使用 Basic 认证')
        
        if not effective_username or not effective_password:
            return _get_default_auth_head(), False, '该仓库需要 Basic 认证，但未提供用户名和密码'
        
        auth_head = _create_basic_auth_head(effective_username, effective_password)
        logger.info(f'✅ 使用 Basic 认证头（凭据来源: {credential_source}）')
        return auth_head, True, None
    
    # 7. 未识别的认证方案
    else:
        logger.warning(f'⚠️ 未识别的认证方案: {scheme}，尝试匿名访问')
        return _get_default_auth_head(), True, None


# GUI兼容的拉取镜像函数
def pull_image_logic(
    image: str,
    registry: Optional[str] = None,
    arch: str = "amd64",
    username: Optional[str] = None,
    password: Optional[str] = None,
    debug: bool = False,
    log_callback: Optional[Callable] = None
):
    """核心逻辑函数，供GUI调用"""
    global stop_event
    stop_event.clear()

    # 添加GUI日志处理器
    gui_handler = None
    if log_callback:
        gui_handler = GUILogHandler(log_callback)
        gui_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
        gui_handler.setFormatter(formatter)
        logger.addHandler(gui_handler)
        
        # 配置 urllib3 日志，使其在连接超时时能在GUI中显示警告信息
        # 这样可以捕获类似 "Retrying...connection broken by 'ConnectTimeoutError'..." 的警告
        urllib3_logger = logging.getLogger("urllib3.connectionpool")
        urllib3_logger.setLevel(logging.WARNING)
        urllib3_logger.addHandler(gui_handler)

    try:
        if debug:
            logger.setLevel(logging.DEBUG)

        logger.info("准备下载...")
        
        image_info = parse_image_input(image, registry)
        
        logger.info(f"开始拉取镜像：{image_info.image_name}:{image_info.tag}")
        logger.info(f"仓库地址：{image_info.registry}")
        logger.info(f"架构：{arch}")

        session = SessionManager.get_session()
        
        # 处理认证
        auth_head, auth_success, error_msg = _handle_authentication(
            session, image_info.registry, image_info.repository, username, password, image_info.protocol
        )
        
        if not auth_success:
            logger.error(f'❌ {error_msg}')
            if 'auth.json' in str(error_msg) or '用户名' in str(error_msg):
                logger.info('💡 提示：可以在 auth.json 文件中配置认证信息，或使用环境变量 DOCKER_REGISTRY_USERNAME/PASSWORD')
            return
        
        # 获取manifest
        resp, http_code = fetch_manifest(
            session, image_info.registry, image_info.repository,
            image_info.tag, auth_head, image_info.protocol
        )
        
        # 如果返回401，尝试重新认证（某些仓库在获取manifest时才需要认证）
        if http_code == 401:
            logger.warning('⚠️ 获取清单时需要重新认证')
            www_auth = resp.headers.get('WWW-Authenticate', '')
            scheme, auth_url, reg_service = parse_www_authenticate(www_auth)
            
            # 重新加载凭据
            user_from_file, pwd_from_file = load_auth_credentials(image_info.registry)
            effective_username = username or user_from_file or os.environ.get('DOCKER_REGISTRY_USERNAME') or os.environ.get('REGISTRY_USERNAME')
            effective_password = password or pwd_from_file or os.environ.get('DOCKER_REGISTRY_PASSWORD') or os.environ.get('REGISTRY_PASSWORD')
            
            if scheme and scheme.lower().startswith('bearer') and auth_url and reg_service and effective_username and effective_password:
                try:
                    auth_head = get_auth_head(
                        session, auth_url, reg_service, image_info.repository,
                        effective_username, effective_password
                    )
                    # 重试获取manifest
                    resp, http_code = fetch_manifest(
                        session, image_info.registry, image_info.repository,
                        image_info.tag, auth_head, image_info.protocol
                    )
                except Exception as e:
                    logger.error(f'❌ 重新认证失败: {e}')
                    return
            elif scheme and scheme.lower().startswith('basic') and effective_username and effective_password:
                auth_head = _create_basic_auth_head(effective_username, effective_password)
                resp, http_code = fetch_manifest(
                    session, image_info.registry, image_info.repository,
                    image_info.tag, auth_head, image_info.protocol
                )
            
            if http_code == 401:
                logger.error('❌ 认证失败，无法访问该镜像')
                logger.info('💡 提示：请检查用户名和密码是否正确')
                return

        if http_code != 200:
            logger.error(f'❌ 获取清单失败，HTTP状态码: {http_code}')
            return

        try:
            resp_json = resp.json()
        except Exception as e:
            logger.error(f'❌ 解析清单失败: {e}')
            return


        manifests = resp_json.get('manifests')
        if manifests is not None:
            archs = [
                m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or
                m.get('platform', {}).get('architecture')
                for m in manifests if m.get('platform', {}).get('os') == 'linux'
            ]

            if archs:
                logger.info(f'📋 当前可用架构：{", ".join(archs)}')

            if len(archs) == 1:
                arch = archs[0]
                logger.info(f'✅ 自动选择唯一可用架构: {arch}')

            if arch not in archs:
                logger.error(f'在清单中找不到指定的架构 {arch}')
                logger.info(f'可用架构: {", ".join(archs)}')
                return

            digest = select_manifest(manifests, arch)
            if not digest:
                logger.error(f'在清单中找不到指定的架构 {arch}')
                return

            url = f'{image_info.protocol}://{image_info.registry}/v2/{image_info.repository}/manifests/{digest}'
            logger.debug(f'获取架构清单: {url}')

            manifest_resp = session.get(url, headers=auth_head, verify=False, timeout=60)
            try:
                manifest_resp.raise_for_status()
                resp_json = manifest_resp.json()
            except Exception as e:
                logger.error(f'获取架构清单失败: {e}')
                return

            if 'layers' not in resp_json:
                logger.error('错误：清单中没有层')
                return

            if 'config' not in resp_json:
                logger.error('错误：清单中没有配置信息')
                return
        else:
            config_digest = resp_json.get('config', {}).get('digest')
            if config_digest:
                config_url = f'{image_info.protocol}://{image_info.registry}/v2/{image_info.repository}/blobs/{config_digest}'
                logger.debug(f'获取镜像配置: {config_url}')
                try:
                    config_resp = session.get(config_url, headers=auth_head, verify=False, timeout=60)
                    config_resp.raise_for_status()
                    config_json = config_resp.json()
                    actual_arch = config_json.get('architecture', 'unknown')
                    actual_os = config_json.get('os', 'unknown')
                    logger.info(f'📋 镜像实际架构: {actual_os}/{actual_arch}')
                except Exception as e:
                    logger.warning(f'获取镜像配置失败: {e}')

        if 'layers' not in resp_json or 'config' not in resp_json:
            logger.error('错误：清单格式不完整，缺少必要字段')
            return

        # 计算镜像总大小
        total_size = 0
        if 'layers' in resp_json:
            for layer in resp_json['layers']:
                total_size += layer.get('size', 0)
        if 'config' in resp_json and resp_json['config'].get('size', 0):
            total_size += resp_json['config'].get('size', 0)
        
        # 格式化大小显示
        def format_size(size: int) -> str:
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024:
                    return f"{size:.1f}{unit}"
                size /= 1024
            return f"{size:.1f}TB"
        
        size_str = format_size(total_size)

        logger.info(f'📦 仓库地址：{image_info.registry}')
        logger.info(f'📦 镜像：{image_info.repository}')
        logger.info(f'📦 标签：{image_info.tag}')
        logger.info(f'📦 架构：{arch}')
        logger.info(f'📦 镜像大小（压缩后的）：{size_str}')

        output_dir = get_output_dir(image_info.repository, image_info.tag, arch)
        imgdir = str(output_dir / 'layers')
        os.makedirs(imgdir, exist_ok=True)
        logger.info(f'📁 输出目录：{output_dir}')
        logger.info('📥 开始下载...')

        if image_info.registry in ('registry-1.docker.io', 'registry.hub.docker.com', 'docker.io') and image_info.repository.startswith('library/'):
            imgparts = []
        else:
            imgparts = image_info.repository.split('/')[:-1]

        download_layers(
            session, image_info.registry, image_info.repository,
            resp_json['layers'], auth_head, imgdir, resp_json,
            imgparts, image_info.image_name, image_info.tag, arch,
            output_dir,
            log_callback=log_callback,
            protocol=image_info.protocol
        )

        output_file = create_image_tar(imgdir, image_info.repository, image_info.tag, arch, output_dir)
        logger.info(f'✅ 镜像已保存为: {output_file}')
        logger.info(f'💡 导入命令: docker load -i {output_file}')

    except KeyboardInterrupt:
        logger.info('⚠️ 用户取消操作。')
    except Exception as e:
        logger.error(f'❌ 程序运行过程中发生异常: {e}')
        raise
    finally:
        cleanup_tmp_dir()


# 命令行入口（主函数）
def main():
    try:
        parser = argparse.ArgumentParser(
            description="Docker 镜像拉取工具 - 无需Docker环境直接下载镜像",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例:
  %(prog)s -i nginx:latest
  %(prog)s -i harbor.example.com/library/nginx:1.26.0 -u admin -p password
  %(prog)s -i alpine:latest -a arm64v8 -o ./downloads
            """
        )
        parser.add_argument("-i", "--image", required=False,
                            help="Docker 镜像名称（例如：nginx:latest 或 harbor.abc.com/abc/nginx:1.26.0）")
        parser.add_argument("-q", "--quiet", action="store_true", help="静默模式，减少交互")
        parser.add_argument("-r", "--custom-registry", help="自定义仓库地址（例如：harbor.abc.com）")
        parser.add_argument("-a", "--arch", default="amd64", help="架构,默认：amd64,常见：amd64, arm64v8等")
        parser.add_argument("-u", "--username", help="Docker 仓库用户名")
        parser.add_argument("-p", "--password", help="Docker 仓库密码")
        parser.add_argument("-o", "--output", help="输出目录，默认为当前目录下的镜像名_tag_arch目录")
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}", help="显示版本信息")
        parser.add_argument("--debug", action="store_true", help="启用调试模式，打印请求 URL 和连接状态")
        parser.add_argument("--workers", type=int, default=4, help="并发下载线程数，默认4")

        logger.info(f'🚀 Docker 镜像拉取工具 {VERSION}')

        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        if not args.image:
            args.image = input("请输入 Docker 镜像名称（例如：nginx:latest 或 harbor.abc.com/abc/nginx:1.26.0）：").strip()
            if not args.image:
                logger.error("错误：镜像名称是必填项。")
                return

        if not args.custom_registry and not args.quiet:
            print("\n📋 可用的镜像站：")
            for key, site in MIRROR_SITES.items():
                print(f"  {key}. {site['name']} ({site['registry']})")
            print("  0. 输入自定义仓库地址")
            
            choice = input("\n请选择镜像站（为空不额外添加镜像站前缀）：").strip() or "-1"
            
            if choice == "0":
                args.custom_registry = input("请输入自定义仓库地址：").strip() or None
            elif choice in MIRROR_SITES:
                args.custom_registry = MIRROR_SITES[choice]["registry"]
                logger.info(f"✅ 已选择镜像站：{MIRROR_SITES[choice]['name']}")
            else:
                args.custom_registry = None

        image_info = parse_image_input(args.image, args.custom_registry)

        if not args.username and not args.quiet:
            args.username = input("请输入镜像仓库用户名：").strip() or None
        if not args.password and not args.quiet:
            args.password = input("请输入镜像仓库密码：").strip() or None

        session = SessionManager.get_session()
        
        # 处理认证
        auth_head, auth_success, error_msg = _handle_authentication(
            session, image_info.registry, image_info.repository, 
            args.username, args.password, image_info.protocol
        )
        
        if not auth_success:
            logger.error(f'❌ {error_msg}')
            logger.info('💡 提示：请检查网络连接或在 auth.json 文件中配置认证信息，或使用环境变量 DOCKER_REGISTRY_USERNAME/PASSWORD')
            return
        
        # 获取manifest
        resp, http_code = fetch_manifest(
            session, image_info.registry, image_info.repository,
            image_info.tag, auth_head, image_info.protocol
        )
        
        # 如果返回401，尝试重新认证
        if http_code == 401:
            logger.warning('⚠️ 获取清单时需要认证')
            www_auth = resp.headers.get('WWW-Authenticate', '')
            scheme, auth_url, reg_service = parse_www_authenticate(www_auth)
            
            # 从用户获取凭据
            use_auth = input(f"当前仓库 {image_info.registry}，需要登录？(y/n, 默认: y): ").strip().lower() or 'y'
            if use_auth == 'y':
                args.username = input("请输入用户名: ").strip()
                args.password = input("请输入密码: ").strip()
            
            if scheme and scheme.lower().startswith('bearer') and auth_url and reg_service and args.username and args.password:
                try:
                    auth_head = get_auth_head(
                        session, auth_url, reg_service, image_info.repository,
                        args.username, args.password
                    )
                    logger.info('✅ Bearer 认证成功')
                except Exception as e:
                    logger.error(f'❌ 认证失败: {e}')
                    return
            elif scheme and scheme.lower().startswith('basic') and args.username and args.password:
                auth_head = _create_basic_auth_head(args.username, args.password)
                logger.info('✅ 使用 Basic 认证')
            else:
                logger.error('❌ 无法完成认证')
                return
            
            # 重试获取manifest
            resp, http_code = fetch_manifest(
                session, image_info.registry, image_info.repository,
                image_info.tag, auth_head, image_info.protocol
            )

        # 检查响应状态码和有效性
        if http_code >= 400 or not resp.text:
            logger.error(f'获取清单失败，HTTP状态码: {http_code}')
            logger.error(f'响应内容: {resp.text[:500] if resp.text else "空响应"}')
            logger.error('可能原因：无效的身份验证、镜像不存在或服务器错误')
            return

        # 尝试解析JSON响应
        try:
            content_type = resp.headers.get('Content-Type', '')
            if 'json' in content_type:
                resp_json = resp.json()
            else:
                # 如果内容类型不是JSON，尝试解析HTML错误信息
                logger.error(f'服务器返回非JSON格式: {content_type}')
                logger.error(f'响应内容: {resp.text[:500]}')
                logger.error('该仓库可能不支持直接拉取，请检查仓库配置')
                return
        except requests.exceptions.JSONDecodeError as e:
            logger.error(f'解析响应失败: {e}')
            logger.error(f'响应内容: {resp.text[:500] if resp.text else "空响应"}')
            logger.error('可能原因：无效的镜像名、仓库地址错误或需要认证')
            return

        manifests = resp_json.get('manifests')
        if manifests is not None:
            archs = [
                m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or
                m.get('platform', {}).get('architecture')
                for m in manifests if m.get('platform', {}).get('os') == 'linux'
            ]

            if archs:
                logger.info(f'📋 当前可用架构：{", ".join(archs)}')

            if len(archs) == 1:
                args.arch = archs[0]
                logger.info(f'✅ 自动选择唯一可用架构: {args.arch}')
            elif not args.quiet:
                default_arch = args.arch if args.arch in archs else 'amd64'
                user_arch = input(f"请输入架构（可选: {', '.join(archs)}，默认: {default_arch}）：").strip()
                args.arch = user_arch if user_arch else default_arch

            if args.arch not in archs:
                logger.error(f'在清单中找不到指定的架构 {args.arch}')
                logger.info(f'可用架构: {", ".join(archs)}')
                return

            digest = select_manifest(manifests, args.arch)
            if not digest:
                logger.error(f'在清单中找不到指定的架构 {args.arch}')
                return

            url = f'{image_info.protocol}://{image_info.registry}/v2/{image_info.repository}/manifests/{digest}'
            logger.debug(f'获取架构清单: {url}')

            manifest_resp = session.get(url, headers=auth_head, verify=False, timeout=60)
            try:
                manifest_resp.raise_for_status()
                resp_json = manifest_resp.json()
            except Exception as e:
                logger.error(f'获取架构清单失败: {e}')
                return

            if 'layers' not in resp_json:
                logger.error('错误：清单中没有层')
                return

            if 'config' not in resp_json:
                logger.error('错误：清单中没有配置信息')
                return
        else:
            config_digest = resp_json.get('config', {}).get('digest')
            if config_digest:
                config_url = f'{image_info.protocol}://{image_info.registry}/v2/{image_info.repository}/blobs/{config_digest}'
                logger.debug(f'获取镜像配置: {config_url}')
                try:
                    config_resp = session.get(config_url, headers=auth_head, verify=False, timeout=60)
                    config_resp.raise_for_status()
                    config_json = config_resp.json()
                    actual_arch = config_json.get('architecture', 'unknown')
                    actual_os = config_json.get('os', 'unknown')
                    logger.info(f'📋 镜像实际架构: {actual_os}/{actual_arch}')
                    
                    if actual_arch != args.arch:
                        logger.warning(f'⚠️  镜像架构为 {actual_arch}，与请求的 {args.arch} 不匹配')
                        if not args.quiet:
                            use_actual = input(f'是否使用镜像实际架构 {actual_arch}？(y/n, 默认: y): ').strip().lower() or 'y'
                            if use_actual == 'y':
                                args.arch = actual_arch
                    else:
                        if not args.quiet:
                            confirm = input(f'确认下载 {actual_os}/{actual_arch} 架构的镜像？(y/n, 默认: y): ').strip().lower() or 'y'
                            if confirm != 'y':
                                logger.info('用户取消下载')
                                return
                except Exception as e:
                    logger.warning(f'获取镜像配置失败: {e}')

        if 'layers' not in resp_json or 'config' not in resp_json:
            logger.error('错误：清单格式不完整，缺少必要字段')
            logger.debug(f'清单内容: {resp_json.keys()}')
            return

        logger.info(f'📦 仓库地址：{image_info.registry}')
        logger.info(f'📦 镜像：{image_info.repository}')
        logger.info(f'📦 标签：{image_info.tag}')
        logger.info(f'📦 架构：{args.arch}')

        output_dir = get_output_dir(image_info.repository, image_info.tag, args.arch, args.output)
        imgdir = str(output_dir / 'layers')
        os.makedirs(imgdir, exist_ok=True)
        logger.info(f'📁 输出目录：{output_dir}')
        logger.info('📥 开始下载...')

        if image_info.registry in ('registry-1.docker.io', 'registry.hub.docker.com', 'docker.io') and image_info.repository.startswith('library/'):
            imgparts = []
        else:
            imgparts = image_info.repository.split('/')[:-1]

        download_layers(
            session, image_info.registry, image_info.repository,
            resp_json['layers'], auth_head, imgdir, resp_json,
            imgparts, image_info.image_name, image_info.tag, args.arch,
            output_dir,
            protocol=image_info.protocol
        )

        output_file = create_image_tar(imgdir, image_info.repository, image_info.tag, args.arch, output_dir)
        logger.info(f'✅ 镜像已保存为: {output_file}')
        logger.info(f'💡 导入命令: docker load -i {output_file}')
        if image_info.registry not in ("registry-1.docker.io", "registry.hub.docker.com", "docker.io"):
            logger.info(f'💡 标签命令: docker tag {image_info.repository}:{image_info.tag} {image_info.registry}/{image_info.repository}:{image_info.tag}')

    except KeyboardInterrupt:
        logger.info('⚠️ 用户取消操作。')
    except requests.exceptions.RequestException as e:
        logger.error(f'❌ 网络连接失败: {e}')
    except json.JSONDecodeError as e:
        logger.error(f'❌ JSON解析失败: {e}')
    except FileNotFoundError as e:
        logger.error(f'❌ 文件操作失败: {e}')
    except argparse.ArgumentError as e:
        logger.error(f'❌ 命令行参数错误: {e}')
    except Exception as e:
        logger.error(f'❌ 程序运行过程中发生异常: {e}')
        import traceback
        logger.debug(traceback.format_exc())

    finally:
        cleanup_tmp_dir()
        try:
            input("\n按回车键退出程序...")
        except (KeyboardInterrupt, EOFError):
            pass
        sys.exit(0)


if __name__ == '__main__':
    main()
