# docker-pull-tar-gui

这是一个用于打包 Docker 镜像的工具，无需安装任何本地环境即可开箱即用，目前仅支持中文和英文。  


### 演示
**搜索镜像**：  
![dp_demo3](https://github.com/user-attachments/assets/9cd39f54-55a6-4cd2-8ba1-8929d760ef4e)  

**下载镜像包**：  
<img width="1193" height="941" alt="dp_demo4" src="https://github.com/user-attachments/assets/abddc7af-392f-4749-bd45-6f546eb211b1" />

这个项目的目的在于方便那些喜欢图形界面的用户使用  


### 如何在Linux中使用
获取脚本：
```bash
wget https://raw.githubusercontent.com/stu2116Edward/docker-pull-tar-gui/refs/heads/main/docker_image_puller.py
```
用法：
```bash
python3 docker_image_puller.py [-i 镜像名称] [-a 架构] [-r 仓库地址]
```
例如：
```bash
python3 docker_image_puller.py -i alpine -a amd64 -r abc.itelyou.cf
```
**基本用法**
```bash
python3 docker_image_puller.py [选项]
```
- `-h, --help`：显示帮助信息
- `-v, --version`：显示版本信息
- `-i, --image`：指定 Docker 镜像名称（例如：library/ubuntu:latest 或者 alpine）
- `-a, --arch`：指定架构（默认：amd64）
- `-r, --registry`：指定 Docker 仓库地址（默认：abc.itelyou.cf）
- `--debug`：启用调试模式，打印详细日志

**演示**：  
显示帮助信息
```bash
python3 docker_image_puller.py -h
```
查看版本信息
```bash
python3 docker_image_puller.py -v
```
通过调试获取镜像包
```
python3 docker_image_puller.py -i alpine -a amd64 -r abc.itelyou.cf --debug
```
与tar文件一样日志文件`docker_pull_log.txt`也会生成在当前的目录下


### 如何使用镜像包

1. 使用此工具拉取镜像并生成 .tar 文件，例如 `library_nginx_amd64.tar`  
2. 将 .tar 文件传输到具有 Docker 环境的主机上  
3. 运行以下命令导入镜像：
```bash
docker load -i library_nginx_amd64.tar
```
4. 验证镜像是否导入成功
```bash
docker images
```


### 项目打包
安装 Pyinstaller：
```
pip install pyinstaller
```
**GUI**:
```
pyinstaller -F -w -i favicon.ico docker_image_puller_gui.py --add-data "logo.ico;." --add-data "settings.png;."
```
**CLI**:
```
pyinstaller -F -i favicon.ico docker_image_puller.py
```

