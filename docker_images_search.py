import requests
import argparse
import os
import sys
from typing import List, Dict, Optional

class DockerImageSearcher:
    """
    Docker镜像搜索工具类（无需本地Docker环境）
    使用Docker Hub的V2 API搜索镜像，输出格式类似docker search命令
    """
    
    def __init__(self):
        self.registries = self._load_registries()
        self.current_registry = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        }
        self.timeout = 3  # 请求超时时间
    
    def _load_registries(self) -> List[str]:
        """加载注册表地址列表，优先使用官方地址"""
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
    
    # 接口请求示例：https://registry.hub.docker.com/v2/search/repositories/?query=nginx
    def search_images(self, term: str, limit: int = 25) -> Optional[List[Dict]]:
        """
        搜索Docker镜像
        """
        params = {
            "query": term,
            "page_size": limit,
        }
        
        for registry in self.registries:
            registry = registry.rstrip("/")
            if not registry.startswith(("http://", "https://")):
                registry = f"https://{registry}"
            
            api_url = f"{registry}/v2/search/repositories/"
            self.current_registry = registry
            
            try:
                print(f"尝试从 {registry} 搜索...", flush=True)
                response = requests.get(
                    api_url,
                    headers=self.headers,
                    params=params,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if not data.get("results"):
                        print(f"从 {registry} 获取到空结果，尝试下一个注册表...", flush=True)
                        continue
                    
                    results = []
                    for item in data.get("results", []):
                        results.append({
                            "name": item.get("repo_name", ""),
                            "description": (item.get("short_description", "") or "")[:60],
                            "stars": item.get("star_count", 0),
                            "official": "[OK]" if item.get("is_official", False) else "",
                            "automated": "[OK]" if item.get("is_automated", False) else "",
                        })
                    return results
                else:
                    print(f"从 {registry} 获取数据失败，状态码: {response.status_code}", flush=True)
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


def main():
    parser = argparse.ArgumentParser(
        description="Docker镜像搜索工具（无需本地Docker环境）",
        add_help=False  # 禁用自动help，我们自己处理
    )
    parser.add_argument("search_term", nargs="?", help="要搜索的镜像名称或关键字")
    parser.add_argument("--limit", type=int, default=25, help="返回结果数量限制")
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助信息")

    try:
        args, unknown = parser.parse_known_args()
        
        if args.help or not args.search_term:
            parser.print_help()
            print("\n示例:")
            print("  python docker_images_search.py nginx")
            print("  python docker_images_search.py mysql --limit 10")
            return
        
        searcher = DockerImageSearcher()
        search_results = searcher.search_images(args.search_term, args.limit)
        if search_results is not None:
            print_search_results(search_results, searcher.current_registry)
            
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {str(e)}")


if __name__ == "__main__":
    main()
