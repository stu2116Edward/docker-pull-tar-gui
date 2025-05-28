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
VERSION = "v1.1.0"

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


def create_session():
    """创建带有重试和代理配置的请求会话"""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # 设置代理
    session.proxies = {
        'http': os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'),
        'https': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    }
    if session.proxies.get('http') or session.proxies.get('https'):
        logger.info('使用代理设置从环境变量')
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

def get_auth_head(session, auth_url, reg_service, repository):
    """获取认证头"""
    try:
        url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'
        logger.debug(f"获取认证头 CURL 命令: curl '{url}'")
        resp = session.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        access_token = resp.json()['token']
        auth_head = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
        return auth_head
    except requests.exceptions.RequestException as e:
        logger.error(f'请求认证失败: {e}')
        raise

def fetch_manifest(session, registry, repository, tag, auth_head):
    """获取镜像清单"""
    try:
        url = f'https://{registry}/v2/{repository}/manifests/{tag}'
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        logger.debug(f'获取镜像清单 CURL 命令: curl -H "Accept: {headers["Accept"]}" -H "Authorization: {headers["Authorization"]}" {url}')
        resp = session.get(url, headers=headers, verify=False, timeout=30)
        resp.raise_for_status()
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

def download_layers(session, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag, log_callback=None, layer_progress_callback=None, overall_progress_callback=None):
    """下载镜像层"""
    try:
        config = resp_json['config']['digest']
        url = f'https://{registry}/v2/{repository}/blobs/{config}'
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        if log_callback:
            log_callback(f"[DEBUG] 下载配置文件 CURL 命令: {url}\n")
        with session.get(url, headers=headers, verify=False, timeout=30, stream=True) as confresp:
            confresp.raise_for_status()
            with open(f'{imgdir}/{config[7:]}.json', 'wb') as file:
                shutil.copyfileobj(confresp.raw, file)
        if log_callback:
            log_callback(f"配置文件下载完成：{config}\n")

        content = [{
            'Config': f'{config[7:]}.json',
            'RepoTags': [f'{"/".join(imgparts[:-1])}/{img}:{tag}' if imgparts[:-1] else f'{img}:{tag}'],
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

        for index, layer in enumerate(layers, start=1):
            if stop_event.is_set():
                if log_callback:
                    log_callback("下载已停止。\n")
                    log_callback("[INFO] 镜像下载中断！\n")
                return

            ublob = layer['digest']
            fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
            layerdir = f'{imgdir}/{fake_layerid}'
            os.makedirs(layerdir, exist_ok=True)

            with open(f'{layerdir}/VERSION', 'w') as file:
                file.write('1.0')

            try:
                url = f'https://{registry}/v2/{repository}/blobs/{ublob}'
                headers = {
                    'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
                    'Authorization': auth_head.get('Authorization', '')
                }
                if log_callback:
                    log_callback(f"[DEBUG] 下载镜像层 CURL 命令: {url}\n")
                with session.get(url, headers=headers, verify=False, timeout=30, stream=True) as bresp:
                    bresp.raise_for_status()
                    total_size = int(bresp.headers.get('content-length', 0))
                    downloaded_size = 0

                    with open(f'{layerdir}/layer_gzip.tar', 'wb') as file:
                        for chunk in bresp.iter_content(chunk_size=1024):
                            if stop_event.is_set():
                                if log_callback:
                                    log_callback("下载已停止。\n")
                                    log_callback("[INFO] 镜像下载中断！\n")
                                return
                            if chunk:
                                file.write(chunk)
                                downloaded_size += len(chunk)
                                if layer_progress_callback:
                                    layer_progress_callback(int(downloaded_size / total_size * 100))

                    if log_callback:
                        log_callback(f"镜像层下载完成：{ublob[:12]}\n")

                with gzip.open(f'{layerdir}/layer_gzip.tar', 'rb') as gz, open(f'{layerdir}/layer.tar', 'wb') as file:
                    shutil.copyfileobj(gz, file)
                os.remove(f'{layerdir}/layer_gzip.tar')

                content[0]['Layers'].append(f'{fake_layerid}/layer.tar')

                if layers[-1]['digest'] == layer['digest']:
                    with open(f'{imgdir}/{config[7:]}.json', 'rb') as file:
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

            except Exception as e:
                if log_callback:
                    log_callback(f"[ERROR] 请求层失败: {e}\n")
                raise
            finally:
                overall_progress += 1
                if overall_progress_callback:
                    overall_progress_callback(int(overall_progress / total_layers * 100))
    except Exception as e:
        if log_callback:
            log_callback(f"[ERROR] 下载镜像层失败: {e}\n")
        raise

    with open(f'{imgdir}/manifest.json', 'w') as file:
        json.dump(content, file)

    repo_tag = f'{"/".join(imgparts[:-1])}/{img}' if imgparts[:-1] else img
    with open(f'{imgdir}/repositories', 'w') as file:
        json.dump({repo_tag: {tag: fake_layerid}}, file)

def create_image_tar(imgdir, repo, img, arch):
    """将镜像打包为 tar 文件"""
    docker_tar = f'{repo.replace("/", "_")}_{img}_{arch}.tar'
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

def pull_image_logic(image, registry, arch, debug=False, log_callback=None, layer_progress_callback=None, overall_progress_callback=None):
    """核心逻辑函数，接受直接传递的参数"""
    global stop_event
    stop_event.clear()  # 重置停止事件

    try:
        if debug:
            logger.setLevel(logging.DEBUG)

        repo, img, tag = parse_image_input(image)
        repository = f'{repo}/{img}'

        session = create_session()

        url = f'https://{registry}/v2/'
        if log_callback:
            log_callback(f"[DEBUG] 获取认证信息 CURL 命令: {url}\n")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 401:
            auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
            reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
            auth_head = get_auth_head(session, auth_url, reg_service, repository)
        else:
            auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}

        resp = fetch_manifest(session, registry, repository, tag, auth_head)
        resp_json = resp.json()
        manifests = resp_json.get('manifests')
        if manifests:
            archs = [m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or m.get('platform', {}).get('architecture') for m in manifests if m.get('platform', {}).get('os') == 'linux']
            if log_callback:
                log_callback(f'当前可用架构：{", ".join(archs)}\n')

            digest = select_manifest(manifests, arch)
            if digest:
                url = f'https://{registry}/v2/{repository}/manifests/{digest}'
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
        download_layers(session, registry, repository, resp_json['layers'], auth_head, imgdir, resp_json, [repo], img, tag, log_callback=log_callback, layer_progress_callback=layer_progress_callback, overall_progress_callback=overall_progress_callback)

        create_image_tar(imgdir, repo, img, arch)
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
