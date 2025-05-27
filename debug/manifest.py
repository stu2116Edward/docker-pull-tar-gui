import requests
import json
import logging
import re

# 禁用 SSL 警告
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def get_auth_head(session, auth_url, reg_service, repository):
    """获取认证头（兼容access_token和token字段）"""
    url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'
    logger.debug(f"获取认证头 CURL 命令: curl '{url}'")
    resp = session.get(url, verify=False, timeout=10)
    resp.raise_for_status()
    token_data = resp.json()
    token = token_data.get("access_token") or token_data.get("token")
    if not token:
        logger.error("Token响应格式错误，未找到'token'或'access_token'字段")
        raise ValueError("无效的Token响应")
    auth_header = {'Authorization': f'Bearer {token}'}
    logger.debug(f"生成的认证头: {auth_header}")
    return auth_header

def fetch_manifest(session, registry, repository, tag, auth_head):
    """获取镜像清单"""
    url = f'https://{registry}/v2/{repository}/manifests/{tag}'
    headers = {
        'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
        'Authorization': auth_head.get('Authorization', '')
    }
    logger.debug(f"清单请求头: {headers}")
    resp = session.get(url, headers=headers, verify=False, timeout=30)
    resp.raise_for_status()
    return resp.json()

def main(registry, repo_name, tag):
    """主函数，用于获取镜像的认证头和清单"""
    repository = f"library/{repo_name}"
    session = requests.Session()
    try:
        # 第一步：请求 /v2/ 获取认证参数
        url = f'https://{registry}/v2/'
        resp = session.get(url, verify=False, timeout=10)
        if resp.status_code == 401:
            www_auth = resp.headers.get('WWW-Authenticate', '')
            # 解析认证服务和 service
            m = re.search(r'realm="([^"]+)",service="([^"]+)"', www_auth)
            if not m:
                raise Exception("无法解析认证服务参数")
            auth_url = m.group(1)
            reg_service = m.group(2)
            logger.info(f"自动发现认证服务: {auth_url}, service: {reg_service}")
            auth_head = get_auth_head(session, auth_url, reg_service, repository)
        else:
            # 不需要认证
            auth_head = {}

        # 第二步：获取 Manifest
        manifest = fetch_manifest(session, registry, repository, tag, auth_head)
        print("Manifest 响应：")
        print(json.dumps(manifest, indent=2, ensure_ascii=False))

    except Exception as e:
        logger.error(f"主流程失败: {e}")
        exit(1)

if __name__ == "__main__":
    # 配置参数
    registry = "abc.itelyou.cf"
    repo_name = "nginx"
    tag = "latest"
    main(registry, repo_name, tag)