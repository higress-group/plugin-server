import os
from pathlib import Path
from datetime import datetime
import hashlib

# 设置根目录
plugins_dir = Path('plugins')

def calculate_md5(file_path, chunk_size=4096):
    """计算文件的 MD5 值"""
    md5_hash = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()

# 遍历所有 plugin.wasm 文件
for wasm_path in plugins_dir.rglob('plugin.wasm'):
    # 获取两个上级目录名
    parent_name = wasm_path.parent.name
    grandparent_name = wasm_path.parent.parent.name
    file_label = f"{grandparent_name}:{parent_name}"

    # 获取文件元信息
    stat_info = wasm_path.stat()
    size = stat_info.st_size
    mtime = datetime.fromtimestamp(stat_info.st_mtime).isoformat()
    ctime = datetime.fromtimestamp(stat_info.st_ctime).isoformat()

    # 计算文件的 MD5 值
    md5_value = calculate_md5(wasm_path)

    # 构建 metadata.txt 的路径
    metadata_path = wasm_path.parent / 'metadata.txt'

    # 写入元信息（File 字段更新）
    with open(metadata_path, 'w') as f:
        f.write(f"File: {file_label}\n")
        f.write(f"Size: {size} bytes\n")
        f.write(f"Last Modified: {mtime}\n")
        f.write(f"Created: {ctime}\n")
        f.write(f"MD5: {md5_value}\n")
