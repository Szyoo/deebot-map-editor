"""科沃斯扫地机地图查看/编辑器。"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("deebot-map-editor")
except PackageNotFoundError:  # 未安装、直接从源码运行
    __version__ = "0.0.0"
