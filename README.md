# docker-pull-tar-gui

[中文](https://github.com/stu2116Edward/docker-pull-tar-gui/blob/main/README.zh-CN.md#%E5%A6%82%E4%BD%95%E4%BD%BF%E7%94%A8%E9%95%9C%E5%83%8F%E5%8C%85)  

This tool is used for packaging Docker images and can be used out of the box without needing to install any local environment.  
Currently only supports Chinese and English.

### Demo  
**Search mirror**：  
![dp_demo1](https://github.com/user-attachments/assets/6d907bb9-bbee-4197-a3b5-dfd9358abf9d)  

**Download the image package**：  
![dp_demo2](https://github.com/user-attachments/assets/fc998a5d-7671-449c-a2d1-5ad6249eca62)  

The purpose of this project is to facilitate the use for users who prefer graphical interfaces.

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
