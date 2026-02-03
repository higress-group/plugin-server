import os
import sys
import subprocess
import json
import argparse
import tarfile
import shutil
import hashlib
from datetime import datetime

def calculate_md5(file_path, chunk_size=4096):
    """计算文件的 MD5 值"""
    md5_hash = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()

def read_properties(file_path):
    """
    读取 properties 文件并解析所有插件信息
    """
    properties = {}
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    value = value.replace('oci://', '', 1)
                    properties[key.strip()] = value.strip()
    except Exception as e:
        print(f"读取文件时发生错误: {e}")
        return None
    return properties

def handle_tar_layer(tar_path, target_dir):
    """
    处理 tar.gzip 层
    返回是否找到 wasm 文件
    """
    try:
        with tarfile.open(tar_path, 'r:gz') as tar:
            wasm_files = [f for f in tar.getmembers() if f.name.endswith('.wasm')]
            if wasm_files:
                wasm_file = wasm_files[0]
                tar.extract(wasm_file, path=target_dir)
                old_path = os.path.join(target_dir, wasm_file.name)
                new_path = os.path.join(target_dir, 'plugin.wasm')
                os.rename(old_path, new_path)
                print(f"成功提取 .wasm 文件: {new_path}")
                return True
            else:
                print("未找到 .wasm 文件")
                return False
    except Exception as e:
        print(f"解压 tar 文件错误: {e}")
        return False

def handle_wasm_layer(wasm_path, target_dir):
    """
    处理 .wasm 层
    返回是否成功复制 wasm 文件
    """
    try:
        new_path = os.path.join(target_dir, 'plugin.wasm')
        shutil.copy2(wasm_path, new_path)
        print(f"成功复制 .wasm 文件: {new_path}")
        return True
    except Exception as e:
        print(f"复制 .wasm 文件错误: {e}")
        return False

def generate_metadata(plugin_dir, plugin_name):
    """
    为 plugin.wasm 生成 metadata.txt
    """
    wasm_path = os.path.join(plugin_dir, 'plugin.wasm')
    try:
        stat_info = os.stat(wasm_path)
        size = stat_info.st_size
        mtime = datetime.fromtimestamp(stat_info.st_mtime).isoformat()
        ctime = datetime.fromtimestamp(stat_info.st_ctime).isoformat()
        md5_value = calculate_md5(wasm_path)
        metadata_path = os.path.join(plugin_dir, 'metadata.txt')
        with open(metadata_path, 'w') as f:
            f.write(f"Plugin Name: {plugin_name}\n")
            f.write(f"Size: {size} bytes\n")
            f.write(f"Last Modified: {mtime}\n")
            f.write(f"Created: {ctime}\n")
            f.write(f"MD5: {md5_value}\n")
        print(f"成功生成 metadata.txt: {metadata_path}")
    except Exception as e:
        print(f"生成元数据失败: {e}")

def main():
    parser = argparse.ArgumentParser(description='处理插件配置文件')
    parser.add_argument('properties_path', nargs='?', default=None,
                        help='properties文件路径（默认：脚本所在目录下的plugins.properties）')
    parser.add_argument('--download-v2', action='store_true',
                        help='是否下载 2.0.0 版本插件')
    args = parser.parse_args()

    # 用户未提供路径时，使用默认逻辑
    if args.properties_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(script_dir, 'plugins.properties')

        args.properties_path = default_path

    base_path = os.path.dirname(args.properties_path)
    properties = read_properties(args.properties_path)

    if not properties:
        print("未找到有效的插件配置")
        return

    failed_plugins = []

    for plugin_name, plugin_url in properties.items():
        print(f"\n正在处理插件: {plugin_name}")
        # 处理原始版本（1.0.0）
        success = process_plugin(base_path, plugin_name, plugin_url, "1.0.0")
        if not success:
            failed_plugins.append(f"{plugin_name}:1.0.0")
        
        # 如果指定了 --download-v2 参数，则额外处理 2.0.0 版本
        if args.download_v2:
            v2_url = plugin_url.replace(":1.0.0", ":2.0.0")
            print(f"\n正在处理插件 {plugin_name} 的 2.0.0 版本")
            success = process_plugin(base_path, plugin_name, v2_url, "2.0.0")
            if not success:
                failed_plugins.append(f"{plugin_name}:2.0.0")

    if failed_plugins:
        print("\n以下插件未成功处理:")
        for plugin in failed_plugins:
            print(f"- {plugin}")

def process_plugin(base_path, plugin_name, plugin_url, version):
    """
    处理单个插件下载和信息获取
    """
    plugins_base_path = os.path.join(base_path, 'plugins')
    os.makedirs(plugins_base_path, exist_ok=True)

    plugin_dir = os.path.join(plugins_base_path, plugin_name, version)
    os.makedirs(plugin_dir, exist_ok=True)

    temp_download_dir = os.path.join(plugins_base_path, f"{plugin_name}_{version}_temp")
    os.makedirs(temp_download_dir, exist_ok=True)

    wasm_found = False

    try:
        subprocess.run(['oras', 'cp', plugin_url, '--to-oci-layout', temp_download_dir], check=True)

        with open(os.path.join(temp_download_dir, 'index.json'), 'r') as f:
            index = json.load(f)

        manifest_digest = index['manifests'][0]['digest']
        manifest_path = os.path.join(temp_download_dir, 'blobs', 'sha256', manifest_digest.split(':')[1])

        with open(manifest_path, 'r') as f:
            manifest = json.load(f)

        for layer in manifest.get('layers', []):
            media_type = layer.get('mediaType', '')
            digest = layer.get('digest', '').split(':')[1]

            if media_type in [
                'application/vnd.docker.image.rootfs.diff.tar.gzip',
                'application/vnd.oci.image.layer.v1.tar+gzip'
            ]:
                tar_path = os.path.join(temp_download_dir, 'blobs', 'sha256', digest)
                wasm_found = handle_tar_layer(tar_path, plugin_dir)

            elif media_type == 'application/vnd.module.wasm.content.layer.v1+wasm':
                wasm_path = os.path.join(temp_download_dir, 'blobs', 'sha256', digest)
                wasm_found = handle_wasm_layer(wasm_path, plugin_dir)

    except subprocess.CalledProcessError as e:
        print(f"{plugin_name} ({version}) 命令执行失败: {e}")
        return False
    except Exception as e:
        print(f"{plugin_name} ({version}) 处理过程中发生错误: {e}")
        return False
    finally:
        shutil.rmtree(temp_download_dir, ignore_errors=True)

    if wasm_found:
        generate_metadata(plugin_dir, plugin_name)

    return wasm_found

if __name__ == '__main__':
    main()