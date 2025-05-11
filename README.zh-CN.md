# docker-pull-tar-gui

这是一个用于打包 Docker 镜像的工具，无需安装任何本地环境即可开箱即用，目前仅支持中文和英文。  

### 演示
**搜索镜像**：  
![dp_demo3](https://github.com/user-attachments/assets/3919525a-a4e2-45bb-80c9-87324c949d6d)  

**下载镜像包**：  
![dp_demo4](https://github.com/user-attachments/assets/d92150a6-51d3-4935-bab2-6465c6ba8c85)  

这个项目的目的在于方便那些喜欢图形界面的用户使用  

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
