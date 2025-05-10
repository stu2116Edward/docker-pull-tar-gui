import argparse
import sys
import logging
from docker_image_puller_core import (
    pull_image_logic, 
    VERSION,
    logger
)

def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=50, fill='█'):
    """
    打印进度条
    :param iteration: 当前迭代次数
    :param total: 总迭代次数
    :param prefix: 前缀字符串
    :param suffix: 后缀字符串
    :param decimals: 进度百分比小数位数
    :param length: 进度条长度
    :param fill: 进度条填充字符
    """
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
        parser.add_argument("-r", "--registry", help="Docker 仓库地址（默认：docker.xuanyuan.me）")
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}", help="显示版本信息")
        parser.add_argument("--debug", action="store_true", help="启用调试模式，打印请求 URL 和连接状态")

        logger.info(f'欢迎使用 Docker 镜像拉取工具 {VERSION}')

        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        # 获取镜像名称
        if not args.image:
            args.image = input("请输入 Docker 镜像名称（例如：library/ubuntu:latest 或者 alpine）：").strip()
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
            args.registry = input("请输入 Docker 仓库地址（默认：docker.xuanyuan.me）：").strip() or 'docker.xuanyuan.me'

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
