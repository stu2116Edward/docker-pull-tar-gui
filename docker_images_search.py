import requests
import argparse
import os
import sys
import threading
import json
from typing import List, Dict, Optional, Callable, Tuple
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

        # 从环境变量读取代理设置
        http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
        https_proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        
        if http_proxy or https_proxy:
            session.proxies = {
                'http': http_proxy,
                'https': https_proxy
            }
            logger.info(f'🌐 使用代理设置: HTTP_PROXY={http_proxy}, HTTPS_PROXY={https_proxy}')

        return session


class DockerImageSearcher:
    """
    Docker镜像搜索工具类（无需本地Docker环境）
    使用Docker Hub的V2 API搜索镜像，输出格式类似docker search命令
    """
    
    def __init__(self, images_limit: int = None, tags_limit: int = None, registry: str = None, 
                 log_callback: Optional[Callable] = None, timeout: Tuple[int, int] = None):
        self.registries = self._load_registries(registry)
        self.current_registry = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        }
        # 设置超时时间
        self.timeout = timeout or (30, 600)  # (连接超时, 读取超时)
        # 分别管理搜索结果输出限制（与GUI兼容）
        self.max_images_count = images_limit  # 镜像名称搜索结果数限制（None表示无限制）
        self.max_tags_count = tags_limit      # 标签搜索结果数限制（None表示无限制）
        self.custom_registry = registry  # 保存自定义注册表地址
        # 线程隔离：独立的停止事件，用于安全取消搜索操作
        self._stop_event = threading.Event()
        self._log_callback = log_callback  # GUI日志回调
        self._session = None  # 延迟初始化session

    def _log(self, msg: str):
        """统一日志输出：GUI模式下通过回调，CLI模式下通过print"""
        if self._log_callback:
            self._log_callback(msg + '\n')
        else:
            print(msg, flush=True)

    def set_images_limit(self, limit: int):
        """设置镜像名称搜索结果数限制"""
        if limit is not None and limit > 0:
            self.max_images_count = limit

    def set_tags_limit(self, limit: int):
        """设置标签搜索结果数限制"""
        if limit is not None and limit > 0:
            self.max_tags_count = limit

    def get_images_limit(self) -> int:
        """获取镜像名称搜索结果数限制"""
        return self.max_images_count

    def get_tags_limit(self) -> int:
        """获取标签搜索结果数限制"""
        return self.max_tags_count

    # ---- 线程安全取消控制 ----
    def set_stop_event(self, event: threading.Event):
        """设置外部停止事件"""
        self._stop_event = event

    def is_stopped(self) -> bool:
        """检查是否已收到停止信号"""
        return self._stop_event.is_set()

    def stop(self):
        """发送停止信号并关闭session以中断当前请求"""
        self._stop_event.set()
        self._close_session()

    def reset(self):
        """重置停止状态和session"""
        self._stop_event.clear()
        self._close_session()
        self._session = None

    def _close_session(self):
        """关闭当前session"""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _get_session(self) -> requests.Session:
        """获取当前session，如果不存在则创建新的"""
        if self._session is None:
            self._session = SessionManager.get_session()
        return self._session
    # --------------------------

    def _normalize_registry(self, registry_url: str) -> str:
        """规范化仓库地址，提取主机:端口"""
        if not registry_url:
            return ""
        # 移除协议前缀
        if registry_url.startswith(("http://", "https://")):
            parsed = urlparse(registry_url)
            return parsed.netloc or parsed.path
        return registry_url

    def _load_auth_credentials(self, registry_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        读取 auth.json 中的认证信息，支持多仓库配置。
        匹配当前仓库后返回 (username, password)，否则返回 (None, None)。
        支持以下结构：
        - 单对象：{"registry": "host:port", "username": "u", "password": "p"}
        - 列表：[{...}, {...}]
        - 映射：{"auths": {"host:port": {"username": "u", "password": "p"}}}
        """
        if not registry_url:
            return None, None
            
        hostnorm = self._normalize_registry(registry_url)
        auth_file = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "auth.json")
        
        try:
            if os.path.exists(auth_file):
                with open(auth_file, 'r', encoding='utf-8') as f:
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
                            if reg_val and self._normalize_registry(reg_val) == hostnorm:
                                user = item.get('username')
                                pwd = item.get('password')
                                if user and pwd:
                                    self._log(f"✅ 为 {hostnorm} 加载了认证凭据")
                                    return user, pwd

                # 字典形式
                elif isinstance(data, dict):
                    # 单对象
                    reg_val = _extract_registry_value(data) if isinstance(data, dict) else None
                    if reg_val and all(k in data for k in ('username', 'password')):
                        if self._normalize_registry(reg_val) == hostnorm:
                            user = data.get('username')
                            pwd = data.get('password')
                            if user and pwd:
                                self._log(f"✅ 为 {hostnorm} 加载了认证凭据")
                                return user, pwd
                    # 映射：auths
                    elif isinstance(data.get('auths'), dict):
                        for reg, val in data.get('auths', {}).items():
                            if self._normalize_registry(reg) == hostnorm and isinstance(val, dict):
                                user = val.get('username')
                                pwd = val.get('password')
                                if user and pwd:
                                    self._log(f"✅ 为 {hostnorm} 加载了认证凭据")
                                    return user, pwd
                    # 列表嵌套：entries
                    elif isinstance(data.get('entries'), list):
                        for item in data.get('entries'):
                            if isinstance(item, dict):
                                reg_val = _extract_registry_value(item)
                                if reg_val and self._normalize_registry(reg_val) == hostnorm:
                                    user = item.get('username')
                                    pwd = item.get('password')
                                    if user and pwd:
                                        self._log(f"✅ 为 {hostnorm} 加载了认证凭据")
                                        return user, pwd
            else:
                self._log(f"⚠️ 未找到 auth.json 文件，尝试无认证访问")
        except Exception as e:
            self._log(f"⚠️ 加载认证文件失败: {str(e)}")
        return None, None

    def _get_auth_for_registry(self, registry_url: str) -> Optional[Tuple]:
        """获取仓库的认证信息"""
        if not registry_url:
            return None
        username, password = self._load_auth_credentials(registry_url)
        if username and password:
            return (username, password)
        return None

    def _load_registries(self, custom_registry: str = None) -> List[str]:
        """加载注册表地址列表，优先使用 registries.txt 中的地址，registry.hub.docker.com 作为兜底"""
        registries = []
        
        # 如果用户指定了自定义注册表地址，将其插入到最前面优先尝试
        if custom_registry:
            registries.append(custom_registry)
        
        try:
            base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            reg_path = os.path.join(base_dir, "registries.txt")
            if os.path.exists(reg_path):
                with open(reg_path, "r") as f:
                    custom_registries = [line.strip() for line in f if line.strip()]
                    registries.extend(custom_registries)
        except Exception as e:
            print(f"警告: 加载registries.txt失败 - {str(e)}")
        
        # 将官方 Docker Hub 地址作为兜底
        registries.append("https://registry.hub.docker.com")
        return registries


    # 镜像名称查询接口
    def search_images(self, term: str, page: int = 1, page_size: int = 100) -> Optional[Dict]:
        """
        搜索Docker镜像（支持分页）
        支持多种接口：
        1. Docker Hub搜索API: /v2/search/repositories/?query=xxx&page=xxx&page_size=xxx
        2. 私有仓库目录接口: /v2/_catalog
        
        Args:
            term: 搜索关键词
            page: 页码（从1开始）
            page_size: 每页数量（最大100）
            
        Returns:
            包含 total 和 results 的字典，或 None
        """
        # 限制每页数量不超过100
        page_size = min(page_size, 100)
        # 如果设置了镜像数量限制，限制每页数量
        if self.max_images_count is not None:
            page_size = min(page_size, self.max_images_count)
        params = {
            "query": term,
            "page": page,
            "page_size": page_size,
        }
        
        for registry in self.registries:
            # 检查是否已收到停止信号
            if self.is_stopped():
                self._log("搜索操作已被取消")
                return None
            
            registry = registry.rstrip("/")
            if not registry.startswith(("http://", "https://")):
                registry = f"https://{registry}"
            
            self.current_registry = registry
            
            # 获取认证信息
            auth = self._get_auth_for_registry(registry)
            
            try:
                self._log(f"尝试从 {registry} 搜索...")
                
                # 1. 首先尝试 Docker Hub 标准搜索 API
                try:
                    api_url = f"{registry}/v2/search/repositories/"
                    params = {
                        "query": term,
                        "page": page,
                        "page_size": page_size,
                    }
                    self._log(f"尝试 Docker Hub API: {api_url}?query={term}")
                    response = self._get_session().get(
                        api_url,
                        headers=self.headers,
                        params=params,
                        timeout=self.timeout,
                        auth=auth
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        
                        if data.get("results"):
                            results = []
                            for item in data.get("results", []):
                                # 检查是否已收到停止信号
                                if self.is_stopped():
                                    self._log("搜索操作已被取消")
                                    return None
                                results.append({
                                    "name": item.get("repo_name", ""),
                                    "description": (item.get("short_description", "") or "")[:60],
                                    "stars": item.get("star_count", 0),
                                    "official": "[OK]" if item.get("is_official", False) else "",
                                    "automated": "[OK]" if item.get("is_automated", False) else "",
                                })
                            total = data.get("count", len(results))
                            if results:
                                self._log(f"✅ 成功获取第 {page} 页 {len(results)} 个镜像，共 {total} 个")
                            return {"total": total, "results": results}
                        else:
                            self._log(f"Docker Hub API 返回空结果，尝试 OCI 接口...")
                    else:
                        self._log(f"Docker Hub API 返回状态码: {response.status_code}")
                        if response.status_code in (401, 403):
                            self._log(f"⚠️ 认证失败，请检查 auth.json 中的凭据是否正确")
                except requests.exceptions.Timeout as e:
                    self._log(f"Docker Hub API 超时: {e}")
                except requests.exceptions.RequestException as e:
                    self._log(f"Docker Hub API 请求异常: {e}")
                
                # 2. 尝试私有仓库目录接口 /v2/_catalog
                try:
                    catalog_url = f"{registry}/v2/_catalog"
                    self._log(f"尝试私有仓库目录接口: {catalog_url}")
                    catalog_response = self._get_session().get(
                        catalog_url,
                        headers=self.headers,
                        timeout=self.timeout,
                        auth=auth
                    )
                    
                    if catalog_response.status_code == 200:
                        catalog_data = catalog_response.json()
                        repositories = catalog_data.get("repositories", [])
                        
                        # 过滤匹配的仓库
                        matching_repos = [repo for repo in repositories if term.lower() in repo.lower()]
                        
                        if matching_repos:
                            results = []
                            for repo in matching_repos[:page_size]:
                                # 检查是否已收到停止信号
                                if self.is_stopped():
                                    self._log("搜索操作已被取消")
                                    return None
                                results.append({
                                    "name": repo,
                                    "description": "",
                                    "stars": 0,
                                    "official": "",
                                    "automated": "",
                                })
                            return {"total": len(results), "results": results}
                        else:
                            self._log(f"目录接口未找到匹配镜像")
                    else:
                        self._log(f"目录接口返回状态码: {catalog_response.status_code}")
                        if catalog_response.status_code in (401, 403):
                            self._log(f"⚠️ 认证失败，请检查 auth.json 中的凭据是否正确")
                except requests.exceptions.Timeout as e:
                    self._log(f"目录接口超时: {e}")
                except requests.exceptions.RequestException as e:
                    self._log(f"目录接口请求异常: {e}")
                
                self._log(f"从 {registry} 所有接口均无法获取数据")
                continue
                
            except KeyboardInterrupt:
                self._log("\n用户中断操作，停止搜索")
                return None
            except Exception as e:
                self._log(f"处理 {registry} 数据时出错: {str(e)}")
                continue
        
        self._log("所有注册表尝试失败，请检查网络连接或稍后再试")
        return None


    # 镜像标签查询接口
    def get_tags(self, image_name: str, page: int = 1, page_size: int = 100, limit: int = None) -> Optional[Dict]:
        """
        获取 Docker 镜像的标签列表（支持分页）
        
        支持两种 API:
        1. Docker Hub API: https://<registry>/v2/repositories/<namespace>/<image>/tags/
        2. OCI 标准接口: https://<registry>/v2/<name>/tags/list
        
        判断规则：
        - 如果镜像名称包含 '/'，说明已经包含 namespace，直接使用
        - 如果镜像名称不包含 '/'，使用 'library' 前缀（官方镜像）
        
        Args:
            image_name: 镜像名称，如 "java"、"nginx"、"openresty/openresty"
            page: 页码（从1开始）
            page_size: 每页数量（最大100）
            limit: 返回标签数量限制（None表示无限制）
            
        Returns:
            包含 total、results、has_more 的字典，或 None
            total: 总标签数（如无法获取则为-1）
            results: 当前页标签列表
            has_more: 是否还有更多页
        """
        # 限制每页数量不超过100
        page_size = min(page_size, 100)
        
        # 如果设置了标签数量限制，限制每页数量
        if limit is None:
            limit = self.max_tags_count
        if limit is not None:
            page_size = min(page_size, limit)
        
        # 根据镜像名是否包含 '/' 来判断 namespace
        if "/" in image_name:
            # 包含 / 说明是 namespace/image 格式
            namespace, image = image_name.rsplit("/", 1)
        else:
            # 单名称镜像（如 java），默认使用 library namespace
            namespace, image = "library", image_name
        
        # 添加 User-Agent 头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        
        # 按 registries 顺序遍历，在每个 registry 上先尝试 Docker Hub API，再尝试 OCI 标准接口
        for registry in self.registries:
            # 检查是否已收到停止信号
            if self.is_stopped():
                self._log("搜索操作已被取消")
                return None
            
            registry = registry.rstrip("/")
            if not registry.startswith(("http://", "https://")):
                registry = f"https://{registry}"
            
            # 记录当前尝试的注册表，便于 GUI 显示来源
            self.current_registry = registry
            
            # 获取认证信息
            auth = self._get_auth_for_registry(registry)
            
            try:
                self._log(f"尝试从 {registry} 获取 tags...")
                
                # 1. 首先尝试 Docker Hub API
                url = f"{registry}/v2/repositories/{namespace}/{image}/tags/"
                try:
                    self._log(f"尝试 Docker Hub API: {url}")
                    
                    # 直接获取指定页
                    response = self._get_session().get(
                        url,
                        headers=headers,
                        params={"page_size": page_size, "page": page},
                        timeout=self.timeout,
                        auth=auth
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        results = data.get("results", [])
                        
                        # 处理当前页结果
                        tags_list = []
                        for tag in results:
                            # 检查是否已收到停止信号
                            if self.is_stopped():
                                self._log("搜索操作已被取消")
                                return None
                            tag_info = {
                                "name": tag.get("name"),
                                "size": self._format_size(tag.get("full_size", 0)),
                                "last_updated": tag.get("last_updated", ""),
                                "digest": tag.get("digest", ""),
                            }
                            # 添加架构信息
                            images = tag.get("images", [])
                            if images:
                                archs = [img.get("architecture") for img in images if img.get("architecture")]
                                tag_info["architectures"] = ", ".join(set(archs))
                            tags_list.append(tag_info)
                        
                        # 检查是否有更多页
                        has_more = bool(data.get("next"))
                        
                        # 尝试获取总数
                        total = data.get("count", -1)
                        if total == -1:
                            # 如果 API 没有返回 count，估算总数
                            if has_more:
                                total = -1  # 未知
                            else:
                                total = len(tags_list)
                        
                        if tags_list or not has_more:
                            self._log(f"✅ 成功从 Docker Hub API 获取第 {page} 页 {len(tags_list)} 个标签")
                            return {
                                "total": total,
                                "results": tags_list,
                                "has_more": has_more
                            }
                        else:
                            self._log(f"从 {registry} 获取到空结果，尝试其他接口...")
                    else:
                        self._log(f"Docker Hub API 返回状态码: {response.status_code}")
                        if response.status_code in (401, 403):
                            self._log(f"⚠️ 认证失败，请检查 auth.json 中的凭据是否正确")
                            
                except requests.exceptions.Timeout as e:
                    self._log(f"Docker Hub API 超时: {e}")
                except requests.exceptions.RequestException as e:
                    self._log(f"Docker Hub API 请求异常: {e}")
                
                # 2. 尝试 OCI 标准接口 /v2/<name>/tags/list
                repo_path = f"{namespace}/{image}"
                oci_url = f"{registry}/v2/{repo_path}/tags/list"
                self._log(f"尝试 OCI 标准接口: {oci_url}")
                
                try:
                    response = self._get_session().get(
                        oci_url,
                        headers=headers,
                        timeout=self.timeout,
                        auth=auth
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        tags = data.get("tags", [])
                        
                        # OCI 接口通常不支持分页参数，手动分页
                        total = len(tags)
                        start_idx = (page - 1) * page_size
                        end_idx = start_idx + page_size
                        page_tags = tags[start_idx:end_idx]
                        
                        tags_list = []
                        for tag_name in page_tags:
                            tags_list.append({
                                "name": tag_name,
                                "size": "N/A",
                                "last_updated": "",
                                "digest": "",
                                "architectures": "",
                            })
                        
                        has_more = end_idx < total
                        
                        if tags_list or not has_more:
                            self._log(f"✅ 成功从 OCI 接口获取第 {page} 页 {len(tags_list)} 个标签，共 {total} 个")
                            return {
                                "total": total,
                                "results": tags_list,
                                "has_more": has_more
                            }
                    else:
                        self._log(f"OCI 接口返回状态码: {response.status_code}")
                        if response.status_code in (401, 403):
                            self._log(f"⚠️ 认证失败，请检查 auth.json 中的凭据是否正确")
                            
                except requests.exceptions.Timeout as e:
                    self._log(f"OCI 接口超时: {e}")
                except requests.exceptions.RequestException as e:
                    self._log(f"OCI 接口请求异常: {e}")
                
                self._log(f"从 {registry} 所有接口均无法获取数据")
                continue
                
            except requests.exceptions.Timeout as e:
                self._log(f"连接 {registry} 超时: {e}")
                continue
            except requests.exceptions.RequestException as e:
                self._log(f"连接 {registry} 出错: {str(e)}")
                continue
            except KeyboardInterrupt:
                self._log("\n用户中断操作，停止搜索")
                return None
            except Exception as e:
                self._log(f"处理 {registry} 数据时出错: {str(e)}")
                continue
        
        return None

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小显示"""
        if size_bytes == 0:
            return "N/A"
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(size_bytes) < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"


def print_search_results(results: List[Dict], registry: str):
    """打印搜索结果，格式类似docker search命令"""
    print(f"\n使用的注册表地址: {registry}\n")
    
    if not results:
        print("没有找到匹配的镜像")
        return
    
    max_desc_len = 60
    name_width = max(len(img["name"]) for img in results) + 2
    desc_width = min(max(len(img["description"]) for img in results) + 2, max_desc_len + 2)
    stars_width = 7
    official_width = 8
    
    header = f"{'NAME'.ljust(name_width)}{'DESCRIPTION'.ljust(desc_width)}{'STARS'.ljust(stars_width)}{'OFFICIAL'.ljust(official_width)}"
    print(header)
    print("-" * len(header))
    
    for img in results:
        name = img["name"].ljust(name_width)
        desc = img["description"].ljust(desc_width)
        stars = str(img["stars"]).ljust(stars_width)
        official = img["official"].ljust(official_width)
        print(f"{name}{desc}{stars}{official}")


def print_tags_results(tags: List[Dict], image_name: str):
    """打印标签查询结果"""
    print(f"\n镜像: {image_name}\n")
    
    if not tags:
        print("没有找到标签")
        return
    
    # 计算列宽
    name_width = max(len(tag.get("name", "")) for tag in tags) + 2
    size_width = 12
    arch_width = max(len(tag.get("architectures", "")) for tag in tags) + 2
    
    header = f"{'TAG'.ljust(name_width)}{'SIZE'.ljust(size_width)}{'ARCHITECTURES'.ljust(arch_width)}{'LAST_UPDATED'}"
    print(header)
    print("-" * len(header))
    
    for tag in tags:
        name = tag.get("name", "").ljust(name_width)
        size = tag.get("size", "N/A").ljust(size_width)
        arch = tag.get("architectures", "").ljust(arch_width)
        updated = tag.get("last_updated", "")
        # 格式化时间显示
        if updated:
            updated = updated.replace("T", " ").replace("Z", "")[:19]
        print(f"{name}{size}{arch}{updated}")


def main():
    parser = argparse.ArgumentParser(
        description="Docker镜像搜索工具（无需本地Docker环境）",
        add_help=False  # 禁用自动help，我们自己处理
    )
    parser.add_argument("search_term", nargs="?", help="要搜索的镜像名称或关键字")
    parser.add_argument("--registry", dest="registry", default=None, help="指定Docker仓库地址（支持http/https完整地址，如：https://registry.example.com）")
    parser.add_argument("--limit", type=int, default=None, help="搜索结果数量限制（镜像/标签默认: 无限制）")
    parser.add_argument("--tags", action="store_true", help="查询镜像的标签列表")
    parser.add_argument("--timeout", type=int, default=30, help="连接超时时间（秒），默认30秒")
    parser.add_argument("--read-timeout", type=int, default=600, help="读取超时时间（秒），默认600秒")
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助信息")

    try:
        args, unknown = parser.parse_known_args()
        
        if args.help:
            parser.print_help()
            print("\n示例:")
            print("  python docker_images_search.py nginx")
            print("  python docker_images_search.py java --tags")
            print("  python docker_images_search.py mysql --limit 10")
            print("  python docker_images_search.py openresty/openresty --tags --limit 30")
            print("  python docker_images_search.py nginx --registry https://registry.example.com")
            print("  python docker_images_search.py alpine --tags --registry http://localhost:5000")
            print("  python docker_images_search.py nginx --timeout 60 --read-timeout 300")
            print("\n环境变量代理支持:")
            print("  export HTTP_PROXY=http://proxy.example.com:8080")
            print("  export HTTPS_PROXY=https://proxy.example.com:8080")
            return
        
        if not args.search_term:
            print("错误: 需要提供镜像名称")
            parser.print_help()
            return
        
        # 根据搜索模式设置相应的限制
        if args.tags:
            images_limit = None
            tags_limit = args.limit
        else:
            images_limit = args.limit
            tags_limit = None
        
        # 设置超时
        timeout = (args.timeout, args.read_timeout)
        
        searcher = DockerImageSearcher(
            images_limit=images_limit, 
            tags_limit=tags_limit, 
            registry=args.registry,
            timeout=timeout
        )
        
        # 如果指定了 --tags 参数，查询标签列表
        if args.tags:
            print(f"正在查询镜像 {args.search_term} 的标签...")
            tags_results = searcher.get_tags(args.search_term)
            if tags_results is not None:
                print_tags_results(tags_results.get("results", []), args.search_term)
        else:
            # 否则执行搜索
            search_results = searcher.search_images(args.search_term)
            if search_results is not None:
                print_search_results(search_results.get("results", []), searcher.current_registry)
            
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {str(e)}")
    finally:
        # 清理session
        SessionManager.close_session()


if __name__ == "__main__":
    main()
