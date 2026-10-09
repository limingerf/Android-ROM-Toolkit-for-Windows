"""MCP stdio server exposing the stable A.R.T application facade."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from Scripts.application import ArtController
from Scripts.version import APP_VERSION

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None

PROTOCOL_VERSION = "2025-06-18"
TOOLS = [
    {"name": "art_toolchain_status", "description": "检测 Windows 原生工具、WSL2 或 Linux 工具链状态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "art_list_projects", "description": "列出 A.R.T 工程状态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "art_create_project", "description": "创建兼容原 A.R.T 布局的工程。", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "art_inspect_input", "description": "识别 ROM 输入类型。", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "art_import_inputs", "description": "复制输入文件到工程 INPUT。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}}, "required": ["project", "sources"]}},
    {"name": "art_import_rom_zip", "description": "安全导入 ROM ZIP 中的 payload、镜像和 DAT 文件。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "path": {"type": "string"}}, "required": ["project", "path"]}},
    {"name": "art_extract_images", "description": "执行现有 A.R.T 批处理解包。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}}, "required": ["project"]}},
    {"name": "art_repack_partition", "description": "将工作区分区回包为 IMG、DAT 或 DAT.BR。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "partition": {"type": "string"}, "target": {"type": "string", "enum": ["img", "dat", "dat.br"]}, "sparse": {"type": "boolean"}}, "required": ["project", "partition"]}},
    {"name": "art_repack_super", "description": "使用 INPUT/OUT 中的分区镜像合成 super.img。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}, "super_type": {"type": "integer", "enum": [0, 1, 2]}, "sparse": {"type": "boolean"}}, "required": ["project", "sources"]}},
    {"name": "art_list_outputs", "description": "列出工程 OUT 目录中的产物。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]}},
    {"name": "art_delete_project", "description": "删除工程目录（仅允许工程根目录下的 DNA_* 工程）。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]}},
    {"name": "art_get_settings", "description": "读取原版 CLI 的合成、EROFS、SUPER 与 DAT 设置。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "art_update_settings", "description": "更新原版 CLI 的合成、EROFS、SUPER 与 DAT 设置。", "inputSchema": {"type": "object", "properties": {"updates": {"type": "object"}}, "required": ["updates"]}},
    {"name": "art_payload_partitions", "description": "列出 INPUT/payload.bin 中可选择提取的分区。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "source": {"type": "string"}}, "required": ["project", "source"]}},
    {"name": "art_super_partitions", "description": "读取 INPUT/super.img 中的逻辑分区列表。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "source": {"type": "string"}}, "required": ["project", "source"]}},
    {"name": "art_avb_info", "description": "解析镜像 AVB/VBMeta 信息。", "inputSchema": {"type": "object", "properties": {"image": {"type": "string"}}, "required": ["image"]}},
    {"name": "art_avb_verify", "description": "验证镜像 AVB 签名。", "inputSchema": {"type": "object", "properties": {"image": {"type": "string"}}, "required": ["image"]}},
    {"name": "art_avb_erase_footer", "description": "复制并去除镜像 AVB footer。", "inputSchema": {"type": "object", "properties": {"image": {"type": "string"}, "output": {"type": "string"}}, "required": ["image"]}},
    {"name": "art_avb_add_footer", "description": "复制镜像并添加 AVB hash 或 hashtree footer。", "inputSchema": {"type": "object", "properties": {"image": {"type": "string"}, "kind": {"type": "string", "enum": ["hash", "hashtree"]}, "key": {"type": "string"}, "partition_name": {"type": "string"}, "partition_size": {"type": "integer"}, "output": {"type": "string"}}, "required": ["image"]}},
    {"name": "art_ota_status", "description": "查看工程 OTA_WORK 状态。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]}},
    {"name": "art_ota_select", "description": "选择 OTA_WORK/stock-zip 中的 OTA 包。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "name": {"type": "string"}}, "required": ["project", "name"]}},
    {"name": "art_ota_verify", "description": "使用 avbroot 验证 OTA 包签名。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "archive": {"type": "string"}}, "required": ["project"]}},
    {"name": "art_ota_generate_keys", "description": "为工程生成 AVB/OTA 密钥材料。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "passphrase": {"type": "string"}}, "required": ["project"]}},
    {"name": "art_ota_patch", "description": "使用 OTA_WORK/input-img 中的镜像修补选定 OTA 包。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "disable_avb": {"type": "boolean"}}, "required": ["project"]}},
    {"name": "art_list_plugins", "description": "列出已安装的 CLI 插件/子模块。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "art_install_plugin", "description": "安全安装包含 run.sh 的 CLI 插件 ZIP。", "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "replace": {"type": "boolean"}}, "required": ["source"]}},
    {"name": "art_remove_plugin", "description": "删除已安装的 CLI 插件。", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]


class McpServer:
    def __init__(self, root: str | Path):
        self.controller = ArtController(root)

    @staticmethod
    def result(value):
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}], "structuredContent": value}

    def call(self, name, args):
        operations = {
            "art_toolchain_status": lambda: self.controller.toolchain_status(),
            "art_list_projects": lambda: self.controller.list_projects(),
            "art_create_project": lambda: self.controller.create_project(args.get("name", "")),
            "art_inspect_input": lambda: self.controller.inspect_input(args.get("path", "")),
            "art_import_inputs": lambda: self.controller.import_inputs(args.get("project", ""), args.get("sources", [])),
            "art_import_rom_zip": lambda: self.controller.import_rom_archive(args.get("project", ""), args.get("path", "")),
            "art_extract_images": lambda: self.controller.extract_images(args.get("project", ""), args.get("sources")),
            "art_repack_partition": lambda: self.controller.run_job("repack", project=args.get("project", ""), partition=args.get("partition", ""), target=args.get("target", "img"), sparse=args.get("sparse", False)),
            "art_repack_super": lambda: self.controller.repack_super(args.get("project", ""), args.get("sources", []), args.get("super_type", 0), args.get("sparse", False)),
            "art_list_outputs": lambda: self.controller.list_outputs(args.get("project", "")),
            "art_delete_project": lambda: self.controller.delete_project(args.get("project", "")),
            "art_get_settings": lambda: self.controller.get_settings(),
            "art_update_settings": lambda: self.controller.update_settings(args.get("updates") or {}),
            "art_payload_partitions": lambda: self.controller.payload_partitions(args.get("project", ""), args.get("source", "")),
            "art_super_partitions": lambda: self.controller.super_partitions(args.get("project", ""), args.get("source", "")),
            "art_avb_info": lambda: self.controller.avb_info(args.get("image", "")),
            "art_avb_verify": lambda: self.controller.avb_verify(args.get("image", "")),
            "art_avb_erase_footer": lambda: self.controller.avb_erase_footer(args.get("image", ""), args.get("output")),
            "art_avb_add_footer": lambda: self.controller.avb_add_footer(args.get("image", ""), kind=args.get("kind", "hash"), key=args.get("key"), partition_name=args.get("partition_name"), partition_size=args.get("partition_size"), output=args.get("output")),
            "art_ota_status": lambda: self.controller.ota_status(args.get("project", "")),
            "art_ota_select": lambda: self.controller.ota_select_zip(args.get("project", ""), args.get("name", "")),
            "art_ota_verify": lambda: self.controller.ota_verify(args.get("project", ""), args.get("archive")),
            "art_ota_generate_keys": lambda: self.controller.ota_generate_keys(args.get("project", ""), args.get("passphrase", "")),
            "art_ota_patch": lambda: self.controller.ota_patch(args.get("project", ""), disable_avb=args.get("disable_avb", False)),
            "art_list_plugins": lambda: self.controller.list_plugins(),
            "art_install_plugin": lambda: self.controller.install_plugin(args.get("source", ""), replace=args.get("replace", True)),
            "art_remove_plugin": lambda: self.controller.remove_plugin(args.get("name", "")),
        }
        if name not in operations:
            raise ValueError(f"未知工具: {name}")
        return self.result(operations[name]())

    def handle(self, request):
        method, request_id = request.get("method"), request.get("id")
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "art-rom-toolkit", "version": APP_VERSION}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                result = self.call(params.get("name", ""), params.get("arguments") or {})
            except Exception as error:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(error)}}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"未知方法: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self):
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                response = self.handle(json.loads(line))
            except Exception as error:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(error)}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


def main(root=None):
    root_path = root or Path(__file__).resolve().parent.parent
    if FastMCP is not None:
        server = FastMCP("art-rom-toolkit")
        controller = ArtController(root_path)

        @server.tool()
        def art_toolchain_status() -> dict:
            return controller.toolchain_status()

        @server.tool()
        def art_list_projects() -> list[dict]:
            return controller.list_projects()

        @server.tool()
        def art_create_project(name: str) -> dict:
            return controller.create_project(name)

        @server.tool()
        def art_inspect_input(path: str) -> dict:
            return controller.inspect_input(path)

        @server.tool()
        def art_import_inputs(project: str, sources: list[str]) -> list[str]:
            return controller.import_inputs(project, sources)

        @server.tool()
        def art_import_rom_zip(project: str, path: str) -> list[str]:
            return controller.import_rom_archive(project, path)

        @server.tool()
        def art_extract_images(project: str, sources: list[str] | None = None) -> dict:
            return controller.extract_images(project, sources)

        @server.tool()
        def art_repack_partition(project: str, partition: str, target: str = "img", sparse: bool = False) -> dict:
            return controller.run_job("repack", project=project, partition=partition, target=target, sparse=sparse)

        @server.tool()
        def art_repack_super(project: str, sources: list[str], super_type: int = 0, sparse: bool = False) -> dict:
            return controller.repack_super(project, sources, super_type, sparse)

        @server.tool()
        def art_list_outputs(project: str) -> list[dict]:
            return controller.list_outputs(project)

        @server.tool()
        def art_delete_project(project: str) -> dict:
            return controller.delete_project(project)

        @server.tool()
        def art_get_settings() -> dict:
            return controller.get_settings()

        @server.tool()
        def art_update_settings(updates: dict) -> dict:
            return controller.update_settings(updates)

        @server.tool()
        def art_payload_partitions(project: str, source: str) -> list[dict]:
            return controller.payload_partitions(project, source)

        @server.tool()
        def art_super_partitions(project: str, source: str) -> list[str]:
            return controller.super_partitions(project, source)

        @server.tool()
        def art_avb_info(image: str) -> dict:
            return controller.avb_info(image)

        @server.tool()
        def art_avb_verify(image: str) -> dict:
            return controller.avb_verify(image)

        @server.tool()
        def art_avb_erase_footer(image: str, output: str | None = None) -> dict:
            return controller.avb_erase_footer(image, output)

        @server.tool()
        def art_avb_add_footer(image: str, kind: str = "hash", key: str | None = None,
                               partition_name: str | None = None, partition_size: int | None = None,
                               output: str | None = None) -> dict:
            return controller.avb_add_footer(image, kind=kind, key=key, partition_name=partition_name,
                                             partition_size=partition_size, output=output)

        @server.tool()
        def art_ota_status(project: str) -> dict:
            return controller.ota_status(project)

        @server.tool()
        def art_ota_select(project: str, name: str) -> dict:
            return controller.ota_select_zip(project, name)

        @server.tool()
        def art_ota_verify(project: str, archive: str | None = None) -> dict:
            return controller.ota_verify(project, archive)

        @server.tool()
        def art_ota_generate_keys(project: str, passphrase: str = "") -> dict:
            return controller.ota_generate_keys(project, passphrase)

        @server.tool()
        def art_ota_patch(project: str, disable_avb: bool = False) -> dict:
            return controller.ota_patch(project, disable_avb=disable_avb)

        @server.tool()
        def art_list_plugins() -> list[dict]:
            return controller.list_plugins()

        @server.tool()
        def art_install_plugin(source: str, replace: bool = True) -> dict:
            return controller.install_plugin(source, replace=replace)

        @server.tool()
        def art_remove_plugin(name: str) -> dict:
            return controller.remove_plugin(name)

        server.run(transport="stdio")
        return
    McpServer(root_path).serve()


if __name__ == "__main__":
    main()
