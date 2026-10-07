# **Android ROM Toolkit for Windows**

#### **介绍**

一个面向 Windows 的 Android ROM 工具，简称 **A.R.T**。本项目基于原作者 ColdWindScholar 的 [D.N.A3](https://github.com/ColdWindScholar/D.N.A3) 开源项目重构和扩展，保留原项目的许可证与致谢信息，现提供 Windows 图形界面、CLI 和 MCP 接口。

#### **运行平台**

- Windows 10/11 x64：优先使用 `art-res/bin-win-amd64` 的原生 `.exe` 工具，也可通过 `ART_BACKEND=wsl` 使用 WSL2。
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

GUI 使用 `ttkbootstrap` 的浅色主题（默认），可在“工具链 → 界面外观”中切换浅色/深色；未安装时会自动回退到 Python 标准 `ttk`。主题偏好会保存到 `art-res/ui-settings.json`。图形界面覆盖 ROM ZIP、payload、super、IMG、WIN、DAT/DAT.BR 的导入、解包和回包，并提供 super 合成、RAW/Sparse 转换和工具链诊断。Windows 发布包优先使用 `art-res/bin-win-amd64` 中的原生工具；缺失的可下载工具可以在“工具链”页自动补齐，仍没有 Windows 版本的校验工具通过 WSL 兼容层运行。MCP 工具包含同一套工程、导入、解包、回包和 super 合成功能。

Windows 打包：

```powershell
python build.py
```

产物为 `A.R.T-Windows-amd64.zip`，解压后双击 `art.exe` 即可启动工作台，不会额外打开终端窗口。后台任务日志仍显示在界面中，`--mcp` 继续通过标准输入输出管道通信。

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

#### **构建**

1.安装python3,pip  

2.pip install -r requirements.txt --break-system-packages (这里更推荐使用虚拟环境)  

3.执行./build.py构建  


#### **反馈**

请直接提issue
