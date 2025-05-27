import os
import logging
import requests
import tqdm
import re
import gzip
import json
import hashlib
import shutil
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# 禁用 SSL 警告
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def create_session():
    """创建带有重试机制的请求会话"""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def get_auth_head(session, auth_url, reg_service, repository):
    """获取认证头"""
    url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'
    resp = session.get(url, verify=False, timeout=10)
    resp.raise_for_status()
    token_data = resp.json()
    token = token_data.get("access_token") or token_data.get("token")
    if not token:
        raise ValueError("无效的Token响应")
    return {'Authorization': f'Bearer {token}'}

def fetch_manifest(session, registry, repository, tag, auth_head):
    """获取镜像清单"""
    url = f'https://{registry}/v2/{repository}/manifests/{tag}'
    headers = {
        'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
        **auth_head
    }
    resp = session.get(url, headers=headers, verify=False, timeout=30)
    resp.raise_for_status()
    return resp.json()

def download_file_with_progress(session, url, headers, save_path, desc):
    """与docker_image_puller.py一致的下载方式，带进度条"""
    with session.get(url, headers=headers, verify=False, timeout=120, stream=True) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get('content-length', 0))
        with open(save_path, 'wb') as file, tqdm.tqdm(
            total=total_size, unit='B', unit_scale=True, desc=desc, position=0, leave=True
        ) as pbar:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)
                    pbar.update(len(chunk))

def download_image_to_tmp(registry, repo_name, tag, arch="amd64"):
    """
    下载指定镜像到 tmp 目录，结构与docker_image_puller.py一致
    """
    if "/" not in repo_name:
        repository = f"library/{repo_name}"
    else:
        repository = repo_name

    session = create_session()

    # 获取认证参数
    url = f'https://{registry}/v2/'
    resp = session.get(url, verify=False, timeout=10)
    if resp.status_code == 401:
        www_auth = resp.headers.get('WWW-Authenticate', '')
        m = re.search(r'realm="([^"]+)",service="([^"]+)"', www_auth)
        if not m:
            raise Exception("无法解析认证服务参数")
        auth_url = m.group(1)
        reg_service = m.group(2)
        auth_head = get_auth_head(session, auth_url, reg_service, repository)
    else:
        auth_head = {}

    # 获取 Manifest
    manifest = fetch_manifest(session, registry, repository, tag, auth_head)
    logger.info("Manifest 获取成功")

    # 多架构处理
    if "manifests" in manifest:
        manifests = manifest["manifests"]
        selected = None
        for m in manifests:
            plat = m.get("platform", {})
            if plat.get("architecture") == arch and plat.get("os") == "linux":
                selected = m
                break
        if not selected:
            logger.error(f"未找到架构为 {arch} 的 manifest")
            return
        digest = selected["digest"]
        manifest = fetch_manifest(session, registry, repository, digest, auth_head)
        logger.info(f"已选择架构 {arch}，manifest 获取成功")

    # 下载 config 文件
    config_digest = manifest.get("config", {}).get("digest")
    config_filename = f'{config_digest[7:]}.json'
    config_path = os.path.join("tmp", config_filename)
    os.makedirs("tmp", exist_ok=True)
    if config_digest:
        url = f"https://{registry}/v2/{repository}/blobs/{config_digest}"
        logger.info(f"下载 config: {config_digest}")
        download_file_with_progress(session, url, auth_head, config_path, f"config")
    else:
        logger.warning("manifest 中未找到 config 字段，跳过 config 下载")

    # 下载所有层
    layers = manifest.get("layers", [])
    if not layers:
        logger.error("未找到 layers 字段，可能不是 schemaVersion 2 的 manifest")
        return

    parentid = ''
    layer_json_map = {}
    layer_tar_list = []

    for idx, layer in enumerate(layers, start=1):
        digest = layer['digest']
        # 生成伪 layerid
        fake_layerid = hashlib.sha256((parentid + '\n' + digest + '\n').encode('utf-8')).hexdigest()
        layerdir = os.path.join("tmp", fake_layerid)
        os.makedirs(layerdir, exist_ok=True)
        # 下载 gzip 文件
        url = f"https://{registry}/v2/{repository}/blobs/{digest}"
        gz_path = os.path.join(layerdir, "layer_gzip.tar")
        logger.info(f"下载 Layer {idx}: {digest}")
        download_file_with_progress(session, url, auth_head, gz_path, f"layer {idx}")
        # 解压为 layer.tar
        tar_path = os.path.join(layerdir, "layer.tar")
        with gzip.open(gz_path, 'rb') as gz, open(tar_path, 'wb') as file:
            shutil.copyfileobj(gz, file)
        os.remove(gz_path)
        # 写入 json
        json_path = os.path.join(layerdir, "json")
        layer_json_map[fake_layerid] = {"id": fake_layerid, "parent": parentid if parentid else None}
        with open(json_path, 'w') as f:
            json.dump(layer_json_map[fake_layerid], f)
        layer_tar_list.append(f"{fake_layerid}/layer.tar")
        parentid = fake_layerid

    # 写入 manifest.json
    repo_tag = f"{repository}:{tag}"
    manifest_content = [{
        "Config": config_filename,
        "RepoTags": [repo_tag],
        "Layers": layer_tar_list
    }]
    with open(os.path.join("tmp", "manifest.json"), "w") as f:
        json.dump(manifest_content, f)

    # 写入 repositories
    with open(os.path.join("tmp", "repositories"), "w") as f:
        json.dump({repository: {tag: parentid}}, f)

    logger.info("镜像 config 和所有层已全部下载到 tmp 目录")

if __name__ == "__main__":
    registry = "abc.itelyou.cf"
    repo_name = "nginx"
    tag = "latest"
    arch = "amd64"
    download_image_to_tmp(registry, repo_name, tag, arch)