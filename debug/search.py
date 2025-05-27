import requests
import json

class DockerImageFetcher:
    def __init__(self, registry):
        self.registry = registry
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        }
        self.timeout = 5

    def fetch_docker_images(self, query, page_size):
        url = f"https://{self.registry}/v2/search/repositories?query={query}&page_size={page_size}"
        try:
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return data
        except requests.exceptions.RequestException as e:
            print(f"请求失败: {e}")
            return None

if __name__ == "__main__":
    registry = "abc.itelyou.cf"     # 镜像仓库地址
    query = "nginx"                 # 请求镜像名称
    page_size = 5                   # 显示输出个数
    fetcher = DockerImageFetcher(registry)
    result = fetcher.fetch_docker_images(query, page_size)
    if result:
        print(json.dumps(result, indent=4))