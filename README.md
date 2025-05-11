# docker-pull-tar-gui

This tool is used for packaging Docker images and can be used out of the box without needing to install any local environment.  
Currently only supports Chinese and English.

### Demo  
**Search mirror**：  
![dp_demo1](https://github.com/user-attachments/assets/d237dd36-1d1f-49c2-a573-d56b16d5e67f)  

**Download the image package**：  
![dp_demo2](https://github.com/user-attachments/assets/5ca5d959-f0dd-4005-a3a2-306e1b9e4f70)  

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
