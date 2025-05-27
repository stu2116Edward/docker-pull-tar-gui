import requests
import json
import logging
from requests.packages.urllib3.exceptions import InsecureRequestWarning # type: ignore

# 禁用 SSL 警告
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def get_auth_token(session, auth_url, reg_service, repository):
    """获取认证 Token 的完整响应"""
    try:
        url = f'https://{auth_url}/token?service={reg_service}&scope=repository:{repository}:pull'
        logger.debug(f"获取认证 Token CURL 命令: curl '{url}'")
        resp = session.get(url, verify=False, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f'请求认证失败: {e}')
        return None
    

# 官方Token 请求示例： curl "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/nginx:pull"
# 第三方Token 请求示例：curl -X GET "https://abc.itelyou.cf/token?service=abc.itelyou.cf&scope=repository:library/nginx:pull" -H "Accept: application/json"
if __name__ == "__main__":
    # 使用官方Token请求
    # auth_url = "auth.docker.io"
    # reg_service = "registry.docker.io"
    # repository = "nginx"
    
    # 使用第三方Token请求
    auth_url = "abc.itelyou.cf"       # 这里需替换为镜像域名
    reg_service = "abc.itelyou.cf"    # 这里可以与 auth_url 域名一致也可以使用registry.docker.io
    repository = "nginx"

    session = requests.Session()
    result = get_auth_token(session, auth_url, reg_service, repository)
    if result:
        print("完整的 Token 响应：")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("获取认证 Token 失败")
