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

def generate_all_metadata(base_path):
    """
    统一生成 metadata.txt:扫描 plugins/<name>/<version>/plugin.wasm,
    覆盖 properties 中下载/跳过的插件、本地预置插件,以及未在 properties
    中定义的自定义插件。临时目录(<name>_<version>_temp)与失败目录
    因不匹配该路径模板,不会被遍历。
    """
    plugins_base_path = os.path.join(base_path, 'plugins')
    if not os.path.isdir(plugins_base_path):
        return 0
    count = 0
    for plugin_name in sorted(os.listdir(plugins_base_path)):
        name_dir = os.path.join(plugins_base_path, plugin_name)
        if not os.path.isdir(name_dir):
            continue
        for version in sorted(os.listdir(name_dir)):
            plugin_dir = os.path.join(name_dir, version)
            if os.path.isfile(os.path.join(plugin_dir, 'plugin.wasm')):
                generate_metadata(plugin_dir, plugin_name)
                count += 1
    print(f"共为 {count} 个插件目录生成 metadata.txt")
    return count

def is_valid_wasm(path):
    """最小校验:存在、非空、且以 wasm magic (\\x00asm) 开头。"""
    if not os.path.isfile(path) or os.path.getsize(path) < 8:
        return False
    with open(path, 'rb') as f:
        return f.read(4) == b'\x00asm'

def process_plugin(base_path, plugin_name, plugin_url, version, use_local=False):
    """
    处理单个 OCI 插件:把 plugin.wasm 放到 plugins/<name>/<version>/。
    本地已预置(通过 Dockerfile 的 COPY plugins/)则跳过下载。
    metadata.txt 不在此处生成,由 generate_all_metadata() 统一处理。
    """
    plugins_base_path = os.path.join(base_path, 'plugins')
    os.makedirs(plugins_base_path, exist_ok=True)

    plugin_dir = os.path.join(plugins_base_path, plugin_name, version)
    os.makedirs(plugin_dir, exist_ok=True)

    # use_local=True 时校验本地副本:合法(非空且 wasm magic 正确)才跳过下载
    local_wasm = os.path.join(plugin_dir, 'plugin.wasm')
    if use_local:
        if is_valid_wasm(local_wasm):
            print(f"{plugin_name} ({version}) 检测到本地副本,跳过下载")
            return True
        if os.path.isfile(local_wasm):
            print(f"{plugin_name} ({version}) 本地副本存在但校验失败,将重新下载")

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
        shutil.rmtree(plugin_dir, ignore_errors=True)
        return False
    except Exception as e:
        print(f"{plugin_name} ({version}) 处理过程中发生错误: {e}")
        shutil.rmtree(plugin_dir, ignore_errors=True)
        return False
    finally:
        shutil.rmtree(temp_download_dir, ignore_errors=True)

    if not wasm_found:
        print(f"{plugin_name} ({version}) 未找到 .wasm 文件")
        shutil.rmtree(plugin_dir, ignore_errors=True)

    return wasm_found

def main():
    parser = argparse.ArgumentParser(description='处理插件配置文件')
    parser.add_argument('properties_path', nargs='?', default=None,
                        help='properties文件路径（默认：脚本所在目录下的plugins.properties）')
    parser.add_argument('--download-v2', action='store_true',
                        help='是否下载 2.0.0 版本插件')
    parser.add_argument('--use-local', action='store_true',
                        help='启用本地 WASM 文件：plugins/<name>/<version>/plugin.wasm 存在时跳过 OCI 下载')
    args = parser.parse_args()

    # 用户未提供路径时，使用默认逻辑
    if args.properties_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.properties_path = os.path.join(script_dir, 'plugins.properties')

    base_path = os.path.dirname(args.properties_path)
    properties = read_properties(args.properties_path)

    if not properties:
        # 空配置(如纯本地插件场景):仍为本地预置的插件生成 metadata,然后结束
        print("未找到有效的插件配置,仅处理本地预置插件(若存在)")
        generate_all_metadata(base_path)
        return

    failed_plugins = []

    for plugin_name, plugin_url in properties.items():
        print(f"\n正在处理插件: {plugin_name}")
        # 处理原始版本（1.0.0）
        success = process_plugin(base_path, plugin_name, plugin_url, "1.0.0", use_local=args.use_local)
        if not success:
            failed_plugins.append(f"{plugin_name}:1.0.0")

        # 如果指定了 --download-v2 参数，则额外处理 2.0.0 版本
        if args.download_v2:
            v2_url = plugin_url.replace(":1.0.0", ":2.0.0")
            print(f"\n正在处理插件 {plugin_name} 的 2.0.0 版本")
            success = process_plugin(base_path, plugin_name, v2_url, "2.0.0", use_local=args.use_local)
            if not success:
                failed_plugins.append(f"{plugin_name}:2.0.0")

    # 统一生成 metadata.txt:覆盖下载的、本地预置的、以及未在 properties 中定义的自定义插件
    generate_all_metadata(base_path)

    if failed_plugins:
        print("\n以下插件未成功处理:")
        for plugin in failed_plugins:
            print(f"- {plugin}")
        sys.exit(1)

if __name__ == '__main__':
    main()
