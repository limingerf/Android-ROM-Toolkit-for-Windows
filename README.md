# **Android ROM Toolkit for Windows**

当前版本：**v1.2.6**

#### **介绍**

一个面向 Windows 的 Android ROM 工具，简称 **A.R.T**。本项目基于原作者 ELF-RC 的 [A.R.T](https://github.com/ELF-RC/A.R.T/tree/master?tab=readme-ov-file) 开源项目重构和扩展，保留原项目的许可证与致谢信息，现提供 Windows 图形界面、CLI 和 MCP 接口。

#### **运行平台**

- Windows 10/11 x64：默认使用 Windows 原生工具及 EXE 内置的 AVB 工具。常规操作不会启动 WSL；只有 OTA 扩展参数需要内置兼容 avbroot 时，才会按需通过 `wsl.exe` 调用它。完整 WSL 后端仍需在工具链页明确选择并保存，或设置 `ART_BACKEND=wsl`。
- Linux x86_64/arm64：继续使用原有 ELF 工具和 CLI 流程。
- Windows 原生工具目录可通过 `ART_WINDOWS_TOOLS` 指定，例如 `E:\\MIO-KITCHEN-4.1.3-win\\bin\\Windows\\AMD64`。

#### **注意事项**

- **非必要请不要授予工具SU ! ! !**

- **请勿删除【工程目录/configs文件夹】，打包时所需的文件信息都在此处**

- 工程路径建议使用英文和数字；GUI 支持 Unicode 输入。若第三方工具或 WSL 不接受 Unicode 路径，请将工程放在英文目录。

#### **启动方式**

```powershell
python Scripts/main.py --gui  # 现代桌面界面
python Scripts/main.py --mcp  # MCP stdio 服务
python Scripts/main.py        # 原有 CLI
```

GUI 使用 PySide6/Qt 的原生桌面工作台，浅色主题为默认，可在“工具链 → 界面外观”中切换浅色/深色；主题偏好会保存到 `art-res/ui-settings.json`。图形界面覆盖 ROM ZIP、payload、super、IMG、WIN、DAT/DAT.BR 的导入、解包和回包，并提供 super 合成、RAW/Sparse 转换和工具链诊断。Windows 发布包使用 `art-res/bin-win-amd64` 中的原生工具；缺失的可下载工具可以在“工具链”页自动补齐。EXT4 校验工具 `e2fsck.exe`、`resize2fs.exe` 及依赖 DLL 放在独立的 `e2fsprogs` 子目录，通过 Cygwin Windows 运行库运行，无需 WSL。使用的 Windows 移植版为 1.44.5，若镜像采用该版本不支持的新特性，校验会明确失败，不会跳过或隐式切换到 WSL。MCP 工具包含同一套工程、导入、解包、回包和 super 合成功能。

Windows 打包：

```powershell
python build.py
```

产物为 `A.R.T-Windows-amd64.zip`，解压后双击 `art.exe` 即可启动工作台，不会额外打开终端窗口。后台任务日志仍显示在界面中，`--mcp` 继续通过标准输入输出管道通信。

#### **Windows AVB 与 OTA 签名**

- `avbtool` 基于 AOSP 1.3.0，连同 RSA 签名库内嵌到 `art.exe`。签名、验签、镜像信息和去除 footer 无需另装 Python、OpenSSL 或 WSL。源码运行需安装 `requirements.txt`。
- “高级工具 → AVB 镜像签名”可选择无签名、内置 RSA2048 测试密钥、内置 RSA4096 测试密钥（默认）或自定义 PEM；RSA 算法自动匹配密钥位数。内置测试私钥随源码及 EXE 公开提供，正式发布签名请使用自己的密钥。
- 原生哈希树签名默认生成 AVB 内嵌 FEC。AOSP `avbtool` 的 FEC 数据由随包的 `avbroot fec generate` 生成，格式、交错布局和 roots 参数与 Android `fec` 工具一致；`--do_not_generate_fec` 仍可显式关闭。自动大小基于镜像逻辑大小计算并为哈希树、FEC 与 AVB footer 预留空间。
- 发布包附带官方 Windows x64 `avbroot 3.34.1`。缺失时在首次运行相应操作或“自动补齐 Windows 工具”中查询官方最新稳定版，验证 SHA256 与实际版本后安装。
- 发布包同时保留原项目的扩展 avbroot 构建。OTA 使用 `--add-partition`、`--disable-avb` 或原版 `--super-mode` 时，Windows 原生后端会自动调用这个内置兼容工具（`--super-mode NAME` 会转换为动态分区标记）；普通 OTA 替换、AVB 和 FEC 仍走 Windows 原生工具。扩展构建通过随包的 `art-res/bin-amd64/avbroot` 运行，Windows 需要启用 `wsl.exe`，无需在工具链页切换后端。
- 深浅色主题统一下拉列表的边框、文本、选中背景及禁用状态；签名失败会保留工具错误信息。

可用 `art.exe --avbtool version` 检查内置工具。发布前的真实工具检查：

```powershell
python -m unittest discover -s tests -v
python tests/native_ext4_smoke.py path/to/art.exe
python tests/native_avb_smoke.py path/to/art.exe
```

#### **工具预览**


#### **工程目录结构**

```text
DNA_工程名/
├── INPUT/                 # 原始输入区：放置 img、payload.bin、new.dat、br、win 等文件
├── OUT/                   # 最终输出区：合成的 img、new.dat、br、super.img、修补后的 boot 镜像
└── WORKSPACE/             # 可编辑工作区
    ├── config/            # 分区 fsconfig、SELinux contexts、镜像信息等合成 metadata
    ├── system/
    ├── vendor/
    └── 其他分区目录/
```

- `INPUT` 是只读输入区：工具不会主动删除、改名或覆盖其中的文件；所有中间文件写入 `WORKSPACE`
- 分区文件树与 `config` 均位于 `WORKSPACE/`，请勿手动删除 `WORKSPACE/config/` 中需要回包的 metadata
- 最终合成产物输出到 `OUT/`

#### **回包 metadata 自动补齐**

新增或修改过的文件在回包时会自动补上缺失的合成 metadata，无需手写整条规则：

- `fsconfig`：缺失的路径按目录 `0755`、文件 `0644`（可执行目录下的二进制 `0755`、`*.sh` `0750`）补齐
- `SELinux contexts`：为没有专属规则的路径补一行标签，继承顺序为
  1. `.prop`、`.rc`、`.xml`、`.conf`、`.cfg`、`.ini`、`.json` 等配置文件，继承同目录同类配置文件的标签
  2. 其他文件继承同目录常规文件的标签
  3. 目录继承同目录目录、再逐级向上继承父目录的标签
- 补齐的行追加在 `WORKSPACE/config/<分区>_contexts.txt` 末尾（`libselinux` 取最后一条匹配，因此它会覆盖分区级兜底规则），并回写到工程文件
- 标签不理想时直接编辑该文件对应行再回包即可，无需从路径开始重写上下文规则

#### **构建**

1.安装python3,pip  

2.pip install -r requirements.txt --break-system-packages (这里更推荐使用虚拟环境)  

3.执行./build.py构建  


#### **反馈**

请直接提issue
