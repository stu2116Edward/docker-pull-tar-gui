import tarfile
import os
import sys

def pack_layers_to_tar(layers_dir="tmp", tar_name="image_layers.tar"):
    """将 layers 目录下所有内容打包为 tar 文件，不删除原文件"""
    if not os.path.isdir(layers_dir):
        print(f"目录不存在: {layers_dir}")
        return
    with tarfile.open(tar_name, "w") as tar:
        for root, dirs, files in os.walk(layers_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, layers_dir)
                tar.add(file_path, arcname=arcname)
    print(f"已打包为: {tar_name}")

if __name__ == "__main__":
    tar_name = sys.argv[1] if len(sys.argv) > 1 else "image_layers.tar"
    pack_layers_to_tar(tar_name=tar_name)