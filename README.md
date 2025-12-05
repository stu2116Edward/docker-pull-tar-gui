# docker-pull-tar-gui

**Language**: [中文](https://github.com/stu2116Edward/docker-pull-tar-gui/blob/main/README.zh-CN.md#%E5%A6%82%E4%BD%95%E4%BD%BF%E7%94%A8%E9%95%9C%E5%83%8F%E5%8C%85)  

This tool is used for packaging Docker images and can be used out of the box without needing to install any local environment.  
Currently only supports Chinese and English.


### Demo  
**Search mirror**：  
<img width="1202" height="949" alt="dp_demo1" src="https://github.com/user-attachments/assets/c35b4e7c-bd65-4087-a87c-3506c9f16aed" />

**Download the image package**：  
<img width="1192" height="937" alt="dp_demo2" src="https://github.com/user-attachments/assets/49a6d081-8839-4656-9580-247a512f7b2d" />

The purpose of this project is to facilitate the use for users who prefer graphical interfaces.

**Private repository**：  
<img width="1190" height="928" alt="屏幕截图 2025-12-05 125650" src="https://github.com/user-attachments/assets/676e8258-fe0b-43d3-a92f-71b65f939595" />  

Use JSON format to add the private repository address, and the request should use the v2 format.

### How to use it in Linux
Get the script：
```bash
wget https://raw.githubusercontent.com/stu2116Edward/docker-pull-tar-gui/refs/heads/main/docker_image_puller.py
```
Usage：
```bash
python3 docker_image_puller.py [-i IMAGE] [-a ARCH] [-r REGISTRY]
```
example:
```bash
python3 docker_image_puller.py -i alpine -a amd64 -r abc.itelyou.cf
```
#### Basic usage
```
python3 docker_image_puller.py [Options]
```
- `-h, --help`：Displays help information
- `-v, --version`：Displays version information
- `-i, --image`：Specify the name of the Docker image（example：library/ubuntu:latest or alpine）
- `-a, --arch`：Specify the Architecture（default：amd64）
- `-r, --registry`：Specify the Docker repository address（default：abc.itelyou.cf）
- `--debug`：Enable debug mode and print detailed logs

**example**:  
Displays help information
```bash
python3 docker_image_puller.py -h
```
Displays version information
```bash
python3 docker_image_puller.py -v
```
Enable debug mode and print detailed logs
```
python3 docker_image_puller.py -i alpine -a amd64 -r abc.itelyou.cf --debug
```
As with tar files, log files `docker_pull_log.txt` generated in the current directory

### How to Use the image Package

1. Use this tool to pull the image and generate a .tar file, for example `library_nginx_amd64.tar`.  
2. Transfer the .tar file to a host with a Docker environment.
3. Run the following command to import the image:
```bash
docker load -i library_nginx_amd64.tar
```
4. Verify whether the image has been imported successfully.
```bash
docker images
```

### Project packaging
Install Pyinstaller：
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
