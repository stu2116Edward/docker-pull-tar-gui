import sys
import os
import io
import gzip
import json
import hashlib
import logging
import shutil
import tarfile
import requests
import argparse
import time
import base64
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
from threading import Event


# Set default encoding to UTF-8
try:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    if sys.stderr and hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
except Exception:
    pass


# 禁用 SSL 警告
urllib3.disable_warnings()

# 版本号
VERSION = "v1.2.0"

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler("docker_pull_log.txt", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 停止事件
stop_event = Event()
# 当前活动的会话引用，用于取消时快速关闭连接
active_session = None

def cancel_current_pull():
    """设置停止事件并关闭当前活动会话，尽可能立即中断网络请求"""
    try:
        stop_event.set()
        global active_session
        s = active_session
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
            active_session = None
        logger.info('已取消当前拉取并关闭网络会话')
    except Exception as e:
        logger.warning(f'取消拉取时发生异常: {e}')


def create_session():
    """创建带有重试和代理配置的请求会话"""
    session = requests.Session()

    retry_strategy = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"],
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,
        pool_maxsize=20,
        pool_block=False,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.timeout = (30, 300)

    session.proxies = {
        'http': os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'),
        'https': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    }
    if session.proxies.get('http') or session.proxies.get('https'):
        logger.info('使用代理设置从环境变量')

    # 记录活动会话引用，供取消时关闭
    global active_session
    active_session = session
    return session

# 判断官方镜像与用户自定义镜像
def parse_image_input(image_input):
    """解析用户输入的镜像名称"""
    parts = image_input.split('/')
    if len(parts) == 1:
        repo = 'library'
        img_tag = parts[0]
    else:
        repo = '/'.join(parts[:-1])
        img_tag = parts[-1]

    img, *tag_parts = img_tag.split(':')
    tag = tag_parts[0] if tag_parts else 'latest'
    return repo, img, tag

def parse_www_authenticate(header_value):
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
    m = re.search(r'realm=\"([^\"]+)\"', header_value)
    if m:
        realm = m.group(1)
    m = re.search(r'service=\"([^\"]+)\"', header_value)
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

def _normalize_registry(reg):
    """规范化仓库字符串：移除协议与尾部斜杠"""
    if not reg:
        return ''
    r = str(reg).strip()
    if r.startswith('http://'):
        r = r[len('http://'):]
    elif r.startswith('https://'):
        r = r[len('https://'):]
    return r.rstrip('/')

def load_auth_credentials(current_registry_host):
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

def get_auth_head(session, auth_url, reg_service, repository, username=None, password=None):
    """获取认证头，支持用户名密码认证"""
    try:
        if stop_event.is_set():
            raise requests.exceptions.RequestException('Cancelled')
        url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'

        headers = {}
        if username and password:
            auth_string = f"{username}:{password}"
            encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
            headers['Authorization'] = f'Basic {encoded_auth}'

        logger.debug(f"获取认证头 CURL 命令: curl '{url}'")
        resp = session.get(url, headers=headers, verify=False, timeout=30)
        resp.raise_for_status()
        access_token = resp.json().get('token') or resp.json().get('access_token')
        # 同时接受 manifest list 与 manifest v2，兼容多架构
        auth_head = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
        }
        return auth_head
    except requests.exceptions.RequestException as e:
        logger.error(f'请求认证失败: {e}')
        raise

def fetch_manifest(session, registry, repository, tag, auth_head):
    """获取镜像清单"""
    try:
        # 协议在调用处检测，避免 HTTPS 失败
        raise NotImplementedError("fetch_manifest signature updated; use fetch_manifest_with_scheme")
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        # 不再使用
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f'请求清单失败: {e}')
        raise

def fetch_manifest_with_scheme(session, scheme, registry, repository, tag, auth_head):
    """获取镜像清单（支持 http/https）"""
    try:
        if stop_event.is_set():
            raise requests.exceptions.RequestException('Cancelled')
        url = f'{scheme}://{registry}/v2/{repository}/manifests/{tag}'
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        logger.debug(f'获取镜像清单 CURL 命令: curl -H "Accept: {headers["Accept"]}" -H "Authorization: {headers["Authorization"]}" {url}')
        resp = session.get(url, headers=headers, verify=False, timeout=30)
        # 返回响应以便调用处自行处理 401/错误码
        return resp
    except requests.exceptions.RequestException as e:
        logger.error(f'请求清单失败: {e}')
        raise

def select_manifest(manifests, arch):
    """选择适合指定架构的清单"""
    for m in manifests:
        if (m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') == arch or
            m.get('platform', {}).get('architecture') == arch) and \
            m.get('platform', {}).get('os') == 'linux':
            return m.get('digest')
    return None


class DownloadProgressManager:
    """下载进度管理器，支持进度持久化"""

    def __init__(self, repository, tag, arch=None):
        self.repository = repository
        self.tag = tag
        self.arch = arch or 'unknown'
        safe_repo = repository.replace("/", "_").replace(":", "_")
        self.progress_file = f'.download_progress_{safe_repo}_{tag}_{self.arch}.json'
        self.progress_data = self.load_progress()

    def load_progress(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    metadata = data.get('metadata', {})
                    if (metadata.get('repository') == self.repository and
                        metadata.get('tag') == self.tag and
                        metadata.get('arch') == self.arch):
                        logger.info(f'加载已有下载进度，共 {len(data.get("layers", {}))} 个文件')
                        return data
                    else:
                        return self._create_new_progress()
            except Exception:
                return self._create_new_progress()
        return self._create_new_progress()

    def _create_new_progress(self):
        return {
            'metadata': {
                'repository': self.repository,
                'tag': self.tag,
                'arch': self.arch
            },
            'layers': {},
            'config': None
        }

    def save_progress(self):
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def update_layer_status(self, digest, status, **kwargs):
        if digest not in self.progress_data['layers']:
            self.progress_data['layers'][digest] = {}
        self.progress_data['layers'][digest]['status'] = status
        self.progress_data['layers'][digest].update(kwargs)
        self.save_progress()

    def get_layer_status(self, digest):
        return self.progress_data['layers'].get(digest, {})

    def is_layer_completed(self, digest):
        layer_info = self.get_layer_status(digest)
        return layer_info.get('status') == 'completed'

    def update_config_status(self, status, **kwargs):
        if self.progress_data['config'] is None:
            self.progress_data['config'] = {}
        self.progress_data['config']['status'] = status
        self.progress_data['config'].update(kwargs)
        self.save_progress()

    def is_config_completed(self):
        config_data = self.progress_data.get('config')
        if config_data is None:
            return False
        return config_data.get('status') == 'completed'

    def clear_progress(self):
        if os.path.exists(self.progress_file):
            try:
                os.remove(self.progress_file)
            except Exception:
                pass


def download_file_with_progress(session, url, headers, save_path, desc, expected_digest=None, progress_callback=None, max_retries=5):
    for attempt in range(max_retries):
        if stop_event.is_set():
            logger.info('下载被取消')
            return False

        resume_pos = 0
        if os.path.exists(save_path):
            resume_pos = os.path.getsize(save_path)

        download_headers = headers.copy()
        if resume_pos > 0:
            download_headers['Range'] = f'bytes={resume_pos}-'

        try:
            with session.get(url, headers=download_headers, verify=False, timeout=60, stream=True) as resp:
                resp.raise_for_status()

                content_range = resp.headers.get('content-range')
                if content_range:
                    total_size = int(content_range.split('/')[1])
                else:
                    total_size = int(resp.headers.get('content-length', 0)) + resume_pos

                mode = 'ab' if resume_pos > 0 else 'wb'

                sha256_hash = hashlib.sha256() if expected_digest else None

                if resume_pos > 0 and sha256_hash:
                    with open(save_path, 'rb') as existing_file:
                        while True:
                            chunk = existing_file.read(8192)
                            if not chunk:
                                break
                            sha256_hash.update(chunk)

                downloaded_size = resume_pos
                with open(save_path, mode) as file:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if stop_event.is_set():
                            logger.info('下载被取消')
                            try:
                                resp.close()
                            except Exception:
                                pass
                            return False
                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)
                            if progress_callback and total_size:
                                progress_callback(int(downloaded_size / total_size * 100))
                            if sha256_hash:
                                sha256_hash.update(chunk)

                if expected_digest and sha256_hash:
                    actual_digest = f'sha256:{sha256_hash.hexdigest()}'
                    if actual_digest != expected_digest:
                        logger.error(f'{desc} 校验失败')
                        if os.path.exists(save_path):
                            os.remove(save_path)
                        if attempt < max_retries - 1:
                            wait_time = (2 ** attempt)
                            time.sleep(wait_time)
                            continue
                        return False
                return True

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f'{desc} 第 {attempt + 1}/{max_retries} 次下载超时: {e}')
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt)
                time.sleep(wait_time)
                continue
            else:
                if os.path.exists(save_path):
                    os.remove(save_path)
                return False
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in [429, 500, 502, 503, 504] and attempt < max_retries - 1:
                wait_time = (2 ** attempt)
                time.sleep(wait_time)
                continue
            else:
                if os.path.exists(save_path):
                    os.remove(save_path)
                return False
        except Exception:
            if os.path.exists(save_path):
                os.remove(save_path)
            return False

    return False

def download_layers(session, scheme, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag, log_callback=None, layer_progress_callback=None, overall_progress_callback=None, arch=None):
    try:
        progress_manager = DownloadProgressManager(repository, tag, arch)

        config_digest = resp_json['config']['digest']
        config_filename = f'{config_digest[7:]}.json'
        config_path = os.path.join(imgdir, config_filename)
        config_url = f'{scheme}://{registry}/v2/{repository}/blobs/{config_digest}'
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        if not (progress_manager.is_config_completed() and os.path.exists(config_path)):
            progress_manager.update_config_status('downloading', digest=config_digest)
            if log_callback:
                log_callback(f"[DEBUG] 下载配置文件 CURL 命令: {config_url}\n")
            ok = download_file_with_progress(
                session,
                config_url,
                headers,
                config_path,
                "Config",
                expected_digest=config_digest,
                progress_callback=layer_progress_callback
            )
            if not ok:
                progress_manager.update_config_status('failed')
                raise Exception('配置文件下载失败')
            progress_manager.update_config_status('completed', digest=config_digest)
        if log_callback:
            log_callback(f"配置文件下载完成：{config_digest}\n")

        content = [{
            'Config': config_filename,
            'RepoTags': [f'{repository}:{tag}'],
            'Layers': []
        }]

        empty_json = {
            "created": "1970-01-01T00:00:00Z",
            "container_config": {
                "Hostname": "",
                "Domainname": "",
                "User": "",
                "AttachStdin": False,
                "AttachStdout": False,
                "AttachStderr": False,
                "Tty": False,
                "OpenStdin": False,
                "StdinOnce": False,
                "Env": None,
                "Cmd": None,
                "Image": "",
                "Volumes": None,
                "WorkingDir": "",
                "Entrypoint": None,
                "OnBuild": None,
                "Labels": None
            }
        }

        parentid = ''
        total_layers = len(layers)
        overall_progress = 0

        for layer in layers:
            if stop_event.is_set():
                if log_callback:
                    log_callback("下载已停止。\n")
                    log_callback("[INFO] 镜像下载中断！\n")
                return False

            ublob = layer['digest']
            fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
            layerdir = f'{imgdir}/{fake_layerid}'
            os.makedirs(layerdir, exist_ok=True)

            with open(f'{layerdir}/VERSION', 'w') as file:
                file.write('1.0')

            save_path = f'{layerdir}/layer_gzip.tar'

            if progress_manager.is_layer_completed(ublob) and os.path.exists(save_path):
                pass
            else:
                progress_manager.update_layer_status(ublob, 'downloading')
                blob_url = f'{scheme}://{registry}/v2/{repository}/blobs/{ublob}'
                if log_callback:
                    log_callback(f"[DEBUG] 下载镜像层 CURL 命令: {blob_url}\n")
                ok = download_file_with_progress(
                    session,
                    blob_url,
                    headers,
                    save_path,
                    ublob[:12],
                    expected_digest=ublob,
                    progress_callback=layer_progress_callback
                )
                if not ok:
                    progress_manager.update_layer_status(ublob, 'failed')
                    raise Exception(f'层 {ublob[:12]} 下载失败')
                progress_manager.update_layer_status(ublob, 'completed')

            # 取消检查：在解压前中断
            if stop_event.is_set():
                if log_callback:
                    log_callback("下载已停止（解压阶段）。\n")
                return False

            with gzip.open(save_path, 'rb') as gz, open(f'{layerdir}/layer.tar', 'wb') as file:
                shutil.copyfileobj(gz, file)
            os.remove(save_path)

            content[0]['Layers'].append(f'{fake_layerid}/layer.tar')

            if layers[-1]['digest'] == layer['digest']:
                with open(config_path, 'rb') as file:
                    json_data = file.read()
                    json_obj = json.loads(json_data.decode('utf-8'))
                json_obj.pop('history', None)
                json_obj.pop('rootfs', None)
            else:
                json_obj = empty_json.copy()

            json_obj['id'] = fake_layerid
            if parentid:
                json_obj['parent'] = parentid
            parentid = json_obj['id']

            with open(f'{layerdir}/json', 'w') as file:
                json.dump(json_obj, file)

            overall_progress += 1
            if overall_progress_callback:
                overall_progress_callback(int(overall_progress / total_layers * 100))

        return True

    except Exception as e:
        if log_callback:
            log_callback(f"[ERROR] 下载镜像层失败: {e}\n")
        raise

    with open(f'{imgdir}/manifest.json', 'w') as file:
        json.dump(content, file)

    repo_tag = repository
    with open(f'{imgdir}/repositories', 'w') as file:
        json.dump({repo_tag: {tag: fake_layerid}}, file)

def create_image_tar(imgdir, repository, arch):
    """将镜像打包为 tar 文件，文件名基于解析后的 repository"""
    safe_repo = repository.replace("/", "_").replace(":", "_")
    docker_tar = f'{safe_repo}_{arch}.tar'
    try:
        with tarfile.open(docker_tar, "w") as tar:
            tar.add(imgdir, arcname='/')
        logger.info(f'Docker 镜像已拉取：{docker_tar}')
    except Exception as e:
        logger.error(f'打包镜像失败: {e}')
        raise

def cleanup_tmp_dir():
    """删除 tmp 目录"""
    tmp_dir = 'tmp'
    try:
        if os.path.exists(tmp_dir):
            logger.info(f'清理临时目录: {tmp_dir}')
            shutil.rmtree(tmp_dir)
            logger.info('临时目录已清理。')
    except Exception as e:
        logger.error(f'清理临时目录失败: {e}')

def cleanup_progress_file(repository, tag, arch):
    """删除与当前镜像相关的 .download_progress_*.json 文件"""
    try:
        safe_repo = repository.replace("/", "_").replace(":", "_")
        progress_file = f'.download_progress_{safe_repo}_{tag}_{arch}.json'
        if os.path.exists(progress_file):
            os.remove(progress_file)
            logger.info(f'已清理下载进度文件: {progress_file}')
    except Exception as e:
        logger.debug(f'清理下载进度文件失败: {e}')

def pull_image_logic(image, registry, arch, debug=False, log_callback=None, layer_progress_callback=None, overall_progress_callback=None):
    """核心逻辑函数，接受直接传递的参数"""
    global stop_event
    stop_event.clear()  # 重置停止事件

    try:
        if debug:
            logger.setLevel(logging.DEBUG)

        repo, img, tag = parse_image_input(image)
        # 针对非 Docker 官方仓库，单段镜像名不使用 `library/` 前缀
        # 官方仓库（Docker Hub）保留 `library/` 逻辑；其他仓库直接使用镜像名
        hub_hosts = {
            'registry.hub.docker.com',
            'registry-1.docker.io',
            'index.docker.io',
            'docker.io'
        }
        if repo == 'library' and _normalize_registry(registry) not in hub_hosts:
            repository = img
        else:
            repository = f'{repo}/{img}' if repo else img

        session = create_session()

        # 从传入的 registry 中解析协议与主机（优先使用 registries.txt 配置的协议）
        def parse_registry_input(reg):
            r = (reg or '').strip()
            if r.startswith('http://'):
                return 'http', r[len('http://'):].rstrip('/')
            elif r.startswith('https://'):
                return 'https', r[len('https://'):].rstrip('/')
            else:
                # 未指定协议时默认 https（不再自动回退到 http）
                return 'https', r.rstrip('/')

        # 解析得到 scheme 与 registry_host
        scheme, registry_host = parse_registry_input(registry)
        # 若镜像名包含了仓库主机前缀（例如 host:port/namespace/name），需剥离主机部分
        normalized_host = _normalize_registry(registry_host)
        if repository.startswith('http://'):
            repository = repository[len('http://'):]
        elif repository.startswith('https://'):
            repository = repository[len('https://'):]
        if normalized_host and repository.startswith(normalized_host + '/'):
            repository = repository[len(normalized_host) + 1:]
        # 上面已解析 scheme 与 registry_host，可直接使用
        # 基本校验，避免空主机导致请求路径仅为 /v2/
        if not registry_host:
            if log_callback:
                log_callback('[ERROR] 仓库地址为空，请在 registries.txt 或 GUI 中选择有效仓库（示例：https://registry-1.docker.io）。\n')
            raise ValueError('Invalid registry: empty host')

        # 首次访问 /v2/ 获取认证方式与可用状态
        base_url = f'{scheme}://{registry_host}/v2/'
        if log_callback:
            log_callback(f"[INFO] 使用 {scheme.upper()} 连接仓库\n")
            if session.proxies.get('http') or session.proxies.get('https'):
                log_callback(f"[INFO] 使用代理: http={session.proxies.get('http')}, https={session.proxies.get('https')}\n")
        try:
            resp = session.get(base_url, verify=False, timeout=30)
        except FileNotFoundError as fe:
            if log_callback:
                log_callback(f"[ERROR] 访问仓库入口失败: {fe}. 请检查仓库地址是否有效（{scheme}://{registry_host}）。\n")
            raise
        except requests.exceptions.RequestException as e:
            if log_callback:
                log_callback(f"[ERROR] 访问仓库入口网络错误: {e}\n")
            raise
        if log_callback:
            log_callback(f"[DEBUG] 探测仓库入口: {base_url} -> {resp.status_code}\n")
        if resp.status_code == 401:
            www_auth = resp.headers.get('WWW-Authenticate', '')
            scheme_name, auth_url, reg_service = parse_www_authenticate(www_auth)
            # 优先从 auth.json 加载凭据；若无则回退到环境变量（兼容 CLI/旧行为）
            username, password = load_auth_credentials(registry_host)
            if (not username or not password):
                username = username or os.environ.get('DOCKER_REGISTRY_USERNAME') or os.environ.get('REGISTRY_USERNAME')
                password = password or os.environ.get('DOCKER_REGISTRY_PASSWORD') or os.environ.get('REGISTRY_PASSWORD')
            if log_callback:
                if username and password:
                    log_callback('[INFO] 使用认证凭据（优先来自 auth.json）\n')
                else:
                    log_callback('[INFO] 未提供认证凭据，尝试匿名访问或公开令牌\n')

            if scheme_name and scheme_name.lower().startswith('bearer') and auth_url and reg_service:
                # Bearer 流程：去 token 服务获取 access token
                auth_head = get_auth_head(session, auth_url, reg_service, repository, username=username, password=password)
            elif scheme_name and scheme_name.lower().startswith('basic'):
                # Basic 流程：直接使用用户名密码构造认证头
                if username and password:
                    auth_string = f"{username}:{password}"
                    encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                    auth_head = {
                        'Authorization': f'Basic {encoded_auth}',
                        'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
                    }
                    if log_callback:
                        log_callback('[INFO] 使用 Basic 认证头\n')
                else:
                    # 无凭证，仅设置 Accept，可能继续 401
                    auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}
                    if log_callback:
                        log_callback('[WARN] Basic 认证需要用户名和密码\n')
            else:
                # 未识别或缺少参数，回退仅设置 Accept
                auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}
                if log_callback:
                    log_callback('[WARN] 未识别的认证头，回退仅 Accept\n')
        else:
            # 默认同时接受 list 与 v2，以兼容多架构
            auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}

        resp = fetch_manifest_with_scheme(session, scheme, registry_host, repository, tag, auth_head)
        # 如 manifest 请求返回 401，这里按需进行认证后重试（有的仓库不会在 /v2/ 返回 401）
        if resp is not None and getattr(resp, 'status_code', None) == 401:
            www_auth = resp.headers.get('WWW-Authenticate', '')
            scheme_name, auth_url, reg_service = parse_www_authenticate(www_auth)
            username, password = load_auth_credentials(registry_host)
            if (not username or not password):
                username = username or os.environ.get('DOCKER_REGISTRY_USERNAME') or os.environ.get('REGISTRY_USERNAME')
                password = password or os.environ.get('DOCKER_REGISTRY_PASSWORD') or os.environ.get('REGISTRY_PASSWORD')
            if scheme_name and scheme_name.lower().startswith('bearer') and auth_url and reg_service:
                auth_head = get_auth_head(session, auth_url, reg_service, repository, username=username, password=password)
            elif scheme_name and scheme_name.lower().startswith('basic'):
                if username and password:
                    auth_string = f"{username}:{password}"
                    encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                    auth_head = {
                        'Authorization': f'Basic {encoded_auth}',
                        'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
                    }
                else:
                    auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}
            else:
                auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}
            # 重试 manifest 获取
            resp = fetch_manifest_with_scheme(session, scheme, registry_host, repository, tag, auth_head)
        # 非 200 的情况直接报错
        if resp.status_code != 200:
            resp.raise_for_status()
        resp_json = resp.json()
        manifests = resp_json.get('manifests')
        if manifests:
            archs = [m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or m.get('platform', {}).get('architecture') for m in manifests if m.get('platform', {}).get('os') == 'linux']
            if log_callback:
                log_callback(f'当前可用架构：{", ".join(archs)}\n')

            digest = select_manifest(manifests, arch)
            if digest:
                url = f'{scheme}://{registry_host}/v2/{repository}/manifests/{digest}'
                headers = {
                    'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
                    'Authorization': auth_head.get('Authorization', '')
                }
                if log_callback:
                    log_callback(f'获取架构清单 CURL 命令: {url}\n')
                manifest_resp = session.get(url, headers=headers, verify=False, timeout=30)
                manifest_resp.raise_for_status()
                resp_json = manifest_resp.json()

        if 'layers' not in resp_json:
            if log_callback:
                log_callback('[ERROR] 错误：清单中没有层\n')
            return

        imgdir = 'tmp'
        os.makedirs(imgdir, exist_ok=True)
        if log_callback:
            log_callback('开始下载层...\n')
        success = download_layers(session, scheme, registry_host, repository, resp_json['layers'], auth_head, imgdir, resp_json, [repo], img, tag, log_callback=log_callback, layer_progress_callback=layer_progress_callback, overall_progress_callback=overall_progress_callback, arch=arch)

        # 已取消或失败时，跳过打包
        if stop_event.is_set() or not success:
            if log_callback:
                log_callback("[INFO] 拉取已取消，跳过打包。\n")
            return

        # 基于解析后的 repository 命名 tar 文件（不包含仓库主机）
        create_image_tar(imgdir, repository, arch)
        # 下载与打包成功后清理下载进度文件
        cleanup_progress_file(repository, tag, arch)
        if not stop_event.is_set() and log_callback:
            log_callback("镜像拉取完成！\n")
    except Exception as e:
        if log_callback:
            log_callback(f'[ERROR] 程序运行过程中发生异常: {e}\n')
        raise
    finally:
        cleanup_tmp_dir()

def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=50, fill='█'):
    """打印进度条"""
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    sys.stdout.write(f'\r{prefix} |{bar}| {percent}% {suffix}')
    sys.stdout.flush()
    if iteration == total:
        print()

def layer_progress_callback(progress):
    """层下载进度回调函数"""
    print_progress_bar(progress, 100, prefix='当前层进度:', length=30)

def overall_progress_callback(progress):
    """整体进度回调函数"""
    print_progress_bar(progress, 100, prefix='整体进度:', length=30)

def log_callback(message):
    """日志回调函数"""
    if message.startswith("[DEBUG]"):
        logger.debug(message[7:].strip())
    elif message.startswith("[ERROR]"):
        logger.error(message[7:].strip())
    else:
        logger.info(message.strip())

def main():
    """主函数"""
    try:
        parser = argparse.ArgumentParser(description="Docker 镜像拉取工具")
        parser.add_argument("-i", "--image", required=False,
                          help="Docker 镜像名称（例如：library/ubuntu:latest 或者 alpine）")
        parser.add_argument("-a", "--arch", help="架构（默认：amd64）")
        parser.add_argument("-r", "--registry", help="Docker 仓库地址（默认：abc.itelyou.cf）")
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}", help="显示版本信息")
        parser.add_argument("--debug", action="store_true", help="启用调试模式，打印请求 URL 和连接状态")

        logger.info(f'欢迎使用 Docker 镜像拉取工具 {VERSION}')

        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        # 获取镜像名称
        if not args.image:
            args.image = input("请输入 Docker 镜像名称（例如：library/nginx:latest 或者 alpine）：").strip()
            if not args.image:
                logger.error("错误：镜像名称是必填项。")
                while True:
                    user_input = input("输入 1 继续，输入 0 退出：").strip()
                    if user_input == '1':
                        main()  # 递归调用 main 函数继续执行
                        break
                    elif user_input == '0':
                        logger.info("退出程序。")
                        sys.exit(0)
                    else:
                        logger.info("输入无效，请输入 1 或 0。")

        # 获取仓库地址
        if not args.registry:
            args.registry = input("请输入 Docker 仓库地址（默认：abc.itelyou.cf）：").strip() or 'abc.itelyou.cf'

        # 获取架构
        if not args.arch:
            args.arch = input("请输入架构（默认：amd64）：").strip() or 'amd64'

        # 调用核心逻辑，传入进度回调函数
        pull_image_logic(
            args.image, 
            args.registry, 
            args.arch, 
            debug=args.debug,
            log_callback=log_callback,
            layer_progress_callback=layer_progress_callback,
            overall_progress_callback=overall_progress_callback
        )

    except KeyboardInterrupt:
        logger.info('用户取消操作。')
    except Exception as e:
        logger.error(f'程序运行过程中发生异常: {e}')

    # 等待用户输入，1继续，0退出
    while True:
        user_input = input("输入 1 继续，输入 0 退出：").strip()
        if user_input == '1':
            main()  # 递归调用 main 函数继续执行
            break
        elif user_input == '0':
            logger.info("退出程序。")
            sys.exit(0)
        else:
            logger.info("输入无效，请输入 1 或 0。")

if __name__ == '__main__':
    main()
