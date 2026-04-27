import requests
import argparse
import os
import sys
from typing import List, Dict, Optional

# 全局默认搜索结果限制（与GUI共享）
DEFAULT_IMAGES_LIMIT = 30  # 镜像名称搜索结果数限制
DEFAULT_TAGS_LIMIT = 50    # 标签搜索结果数限制

class DockerImageSearcher:
    """
    Docker镜像搜索工具类（无需本地Docker环境）
    使用Docker Hub的V2 API搜索镜像，输出格式类似docker search命令
    """
    
    def __init__(self, images_limit: int = None, tags_limit: int = None, registry: str = None):
        self.registries = self._load_registries(registry)
        self.current_registry = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        }
        self.timeout = 5
        # 分别管理搜索结果输出限制（与GUI兼容）
        self.max_images_count = images_limit if images_limit is not None else DEFAULT_IMAGES_LIMIT  # 镜像名称搜索结果数限制
        self.max_tags_count = tags_limit if tags_limit is not None else DEFAULT_TAGS_LIMIT      # 标签搜索结果数限制
        self.custom_registry = registry  # 保存自定义注册表地址

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

    def _load_registries(self, custom_registry: str = None) -> List[str]:
        """加载注册表地址列表，优先使用官方地址"""
        # 如果用户指定了自定义注册表地址，优先使用
        if custom_registry:
            return [custom_registry]
        
        registries = [
            "https://registry.hub.docker.com",
        ]
        try:
            base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            reg_path = os.path.join(base_dir, "registries.txt")
            if os.path.exists(reg_path):
                with open(reg_path, "r") as f:
                    custom_registries = [line.strip() for line in f if line.strip()]
                    registries[1:1] = custom_registries
        except Exception as e:
            print(f"警告: 加载registries.txt失败 - {str(e)}")
        return registries

    def get_tags(self, image_name: str, limit: int = None) -> Optional[List[Dict]]:
        # 使用类变量作为默认限制
        if limit is None:
            limit = self.max_tags_count
        # 确保不超过最大限制
        limit = min(limit, self.max_tags_count)
        """
        获取 Docker 镜像的标签列表
        
        支持两种 API:
        1. Docker Hub API: https://hub.docker.com/v2/repositories/<namespace>/<image>/tags/
        2. OCI 标准接口: https://<registry>/v2/<name>/tags/list
        
        判断规则：
        - 如果镜像名称包含 '/'，说明已经包含 namespace，直接使用
        - 如果镜像名称不包含 '/'，使用 'library' 前缀（官方镜像）
        
        Args:
            image_name: 镜像名称，如 "java"、"nginx"、"openresty/openresty"
            limit: 返回标签数量限制
            
        Returns:
            标签列表，每个标签包含 name、size、last_updated 等信息
        """
        # 根据镜像名是否包含 '/' 来判断 namespace
        if "/" in image_name:
            # 包含 / 说明是 namespace/image 格式
            namespace, image = image_name.rsplit("/", 1)
        else:
            # 单名称镜像（如 java），默认使用 library namespace
            namespace, image = "library", image_name
        
        tags_list = []
        
        # 添加 User-Agent 头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        
        # 1. 首先尝试 Docker Hub API（仅当用户未指定自定义注册表时）
        if not self.custom_registry:
            try:
                url = f"https://hub.docker.com/v2/repositories/{namespace}/{image}/tags/"
                try:
                    print(f"尝试从 Docker Hub API 获取 tags: {url}", flush=True)
                    response = requests.get(
                        url,
                        headers=headers,
                        params={"page_size": limit},
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        results = data.get("results", [])
                        
                        for tag in results:
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
                        
                        if tags_list:
                            print(f"✅ 成功获取 {len(tags_list)} 个标签")
                            return tags_list
                            
                except requests.exceptions.RequestException:
                    pass
                    
            except Exception as e:
                print(f"Docker Hub API 查询失败: {e}", flush=True)
        
        # 2. 尝试 OCI 标准接口 /v2/<name>/tags/list
        try:
            for registry in self.registries:
                registry = registry.rstrip("/")
                if not registry.startswith(("http://", "https://")):
                    registry = f"https://{registry}"
                
                # 构建 repository 路径
                repo_path = f"{namespace}/{image}"
                
                oci_url = f"{registry}/v2/{repo_path}/tags/list"
                print(f"尝试 OCI 标准接口: {oci_url}", flush=True)
                
                try:
                    response = requests.get(
                        oci_url,
                        headers=headers,
                        timeout=self.timeout
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        tags = data.get("tags", [])
                        for tag_name in tags[:limit]:
                            tags_list.append({
                                "name": tag_name,
                                "size": "N/A",
                                "last_updated": "",
                                "digest": "",
                                "architectures": "",
                            })
                        
                        if tags_list:
                            print(f"✅ 成功从 OCI 接口获取 {len(tags_list)} 个标签")
                            return tags_list
                            
                except requests.exceptions.RequestException:
                    continue
                    
        except Exception as e:
            print(f"OCI 接口查询失败: {e}", flush=True)
        
        return tags_list if tags_list else None

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小显示"""
        if size_bytes == 0:
            return "N/A"
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(size_bytes) < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"
    
    # 接口请求示例：https://registry.hub.docker.com/v2/search/repositories/?query=nginx
    def search_images(self, term: str, limit: int = None) -> Optional[List[Dict]]:
        # 使用类变量作为默认限制
        if limit is None:
            limit = self.max_images_count
        # 确保不超过最大限制
        limit = min(limit, self.max_images_count)
        """
        搜索Docker镜像
        支持多种接口：
        1. Docker Hub搜索API: /v2/search/repositories/?query=xxx
        2. OCI标准接口: /v2/<name>/tags/list
        3. 私有仓库目录接口: /v2/_catalog
        """
        params = {
            "query": term,
            "page_size": limit,
        }
        
        for registry in self.registries:
            registry = registry.rstrip("/")
            if not registry.startswith(("http://", "https://")):
                registry = f"https://{registry}"
            
            self.current_registry = registry
            
            try:
                print(f"尝试从 {registry} 搜索...", flush=True)
                
                # 1. 首先尝试 Docker Hub 标准搜索 API
                api_url = f"{registry}/v2/search/repositories/"
                response = requests.get(
                    api_url,
                    headers=self.headers,
                    params=params,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if data.get("results"):
                        results = []
                        for item in data.get("results", []):
                            results.append({
                                "name": item.get("repo_name", ""),
                                "description": (item.get("short_description", "") or "")[:60],
                                "stars": item.get("star_count", 0),
                                "official": "[OK]" if item.get("is_official", False) else "",
                                "automated": "[OK]" if item.get("is_automated", False) else "",
                            })
                        if results:
                            print(f"✅ 成功获取 {len(results)} 个镜像")
                        return results
                    else:
                        print(f"从 {registry} 获取到空结果，尝试其他接口...", flush=True)
                
                # 2. 尝试 OCI 标准接口 /v2/<name>/tags/list
                print(f"尝试 OCI 标准接口...", flush=True)
                oci_url = f"{registry}/v2/{term}/tags/list"
                oci_response = requests.get(
                    oci_url,
                    headers=self.headers,
                    timeout=self.timeout
                )
                
                if oci_response.status_code == 200:
                    oci_data = oci_response.json()
                    tags = oci_data.get("tags", [])
                    if tags:
                        # 返回镜像信息，包含标签列表
                        return [{
                            "name": term,
                            "description": f"Tags: {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}",
                            "stars": 0,
                            "official": "",
                            "automated": "",
                        }]
                elif oci_response.status_code == 404:
                    print(f"OCI接口返回404，镜像可能不存在", flush=True)
                
                # 3. 尝试私有仓库目录接口 /v2/_catalog
                print(f"尝试私有仓库目录接口...", flush=True)
                catalog_url = f"{registry}/v2/_catalog"
                catalog_response = requests.get(
                    catalog_url,
                    headers=self.headers,
                    timeout=self.timeout
                )
                
                if catalog_response.status_code == 200:
                    catalog_data = catalog_response.json()
                    repositories = catalog_data.get("repositories", [])
                    
                    # 过滤匹配的仓库
                    matching_repos = [repo for repo in repositories if term.lower() in repo.lower()]
                    
                    if matching_repos:
                        results = []
                        for repo in matching_repos[:limit]:
                            results.append({
                                "name": repo,
                                "description": "",
                                "stars": 0,
                                "official": "",
                                "automated": "",
                            })
                        return results
                    else:
                        print(f"目录接口未找到匹配镜像", flush=True)
                
                print(f"从 {registry} 所有接口均无法获取数据", flush=True)
                continue
                
            except requests.exceptions.Timeout:
                print(f"连接 {registry} 超时，尝试下一个注册表...", flush=True)
                continue
            except requests.exceptions.RequestException as e:
                print(f"连接 {registry} 出错: {str(e)}", flush=True)
                continue
            except KeyboardInterrupt:
                print("\n用户中断操作，停止搜索")
                return None
            except Exception as e:
                print(f"处理 {registry} 数据时出错: {str(e)}", flush=True)
                continue
        
        print("所有注册表尝试失败，请检查网络连接或稍后再试", flush=True)
        return None


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
    parser.add_argument("--limit", type=int, default=None, help=f"搜索结果数量限制（镜像默认: {DEFAULT_IMAGES_LIMIT}, 标签默认: {DEFAULT_TAGS_LIMIT}）")
    parser.add_argument("--tags", action="store_true", help="查询镜像的标签列表")
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
        
        searcher = DockerImageSearcher(images_limit=images_limit, tags_limit=tags_limit, registry=args.registry)
        
        # 如果指定了 --tags 参数，查询标签列表
        if args.tags:
            print(f"正在查询镜像 {args.search_term} 的标签...")
            tags_results = searcher.get_tags(args.search_term)
            if tags_results is not None:
                print_tags_results(tags_results, args.search_term)
        else:
            # 否则执行搜索
            search_results = searcher.search_images(args.search_term)
            if search_results is not None:
                print_search_results(search_results, searcher.current_registry)
            
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {str(e)}")


if __name__ == "__main__":
    main()
