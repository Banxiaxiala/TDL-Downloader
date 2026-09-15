# TDL 下载器

**Telegram（电报）资源下载器** —— `tdl` 的 Windows 图形界面封装（tkinter）。把命令行工具 `tdl` + `aria2` 包装成开箱即用的多任务下载器，用来批量下载 **Telegram 频道 / 群组 / 会话里的视频、图片、文件**，支持实时进度、历史记录、流量统计与链接整理。

> 本项目是 [iyear/tdl](https://github.com/iyear/tdl) 的 GUI 前端，遵循上游 **AGPL-3.0** 许可。下载能力全部由 `tdl` 与 `aria2` 提供。

---

## 支持的 Telegram 资源

所有下载都通过 Telegram 官方 API 进行，登录你自己的账号后即可访问其可见内容：

| 资源类型 | 说明 |
|---|---|
| **视频** | `mp4` / `mkv` / `mov` / `avi` 等，可只下视频忽略其他文件 |
| **图片** | 频道与群组中的图片消息 |
| **文件 / 文档** | 任意类型的附件 |
| **音乐 / 音频** | 音频消息与音频文件 |
| **频道 / 群组** | 按链接批量下载整个频道或群组的媒体 |
| **私聊 / 收藏夹** | 自己会话中的消息与「已保存消息」 |
| **话题 / 评论区** | 支持带话题的群组与评论内容 |
| **受限内容** | 开启 takeout 会话后可导出受保护频道的内容 |

链接格式支持 `https://t.me/...`（含 `c/` 私有频道、`?single` 单条消息等）。

---

## 功能特性

- **多任务下载**：粘贴一批 `https://t.me/...` 链接，自动提取、去重，按链接原顺序排列
- **实时进度**：每个文件的进度条、速度、剩余时间、总体进度一屏可见
- **aria2 加速**：多线程分片下载，可调并发数与单文件连接数
- **自动跳过已下载**：输出目录中已存在的文件不再重复下载
- **断点续传**：中断后重跑，aria2 从已完成分片继续
- **收尾自动改名**：下完自动去掉 `.part` 后缀，转正为正式文件名
- **内置登录**：桌面客户端 / 二维码 / 手机号验证码三种方式
- **历史下载**：按次记录 + 文件级清单，可扫描输出目录重建
- **流量统计**：按自然月累计下载量，可设月度限额
- **链接整理**：批量去重、按结尾数字排序、筛选
- **类型过滤**：仅视频模式、自定义扩展名白名单

---

## 界面预览

**下载页** —— 链接自动提取去重，实时显示每个文件的进度、速度、剩余时间与总体进度：

![下载页](docs/screenshot-download.png)

**下载设置页** —— 线程/并发/代理、视频过滤、流量限额：

![下载设置页](docs/screenshot-settings.png)

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11 |
| Python | 3.8 或更高（需含 tkinter，官方安装包默认自带） |
| 第三方库 | **无需任何 pip 安装**，仅用标准库 |
| 外部程序 | `tdl.exe`、`aria2c.exe`（需自行下载，见下） |

### 关于依赖

本项目**不依赖任何第三方 Python 包**，只用标准库：

```
os  re  sys  json  socket  time  threading  subprocess
queue  datetime  urllib.request  tkinter (tk/ttk/filedialog/messagebox)
```

因此不需要 `requirements.txt`，也不需要联网 `pip install`。

---

## 安装步骤

### 1. 获取代码

```bash
git clone https://github.com/Banxiaxiala/TDL-Downloader.git
cd TDL-Downloader
```

### 2. 放置二进制文件

仓库不包含可执行文件（体积大且上游更新频繁），需手动下载放到指定位置：

**tdl.exe** —— 下载后放在项目根目录：

- 来源：<https://github.com/iyear/tdl/releases>
- 选择 `tdl_Windows_64bit.zip`，解压得到 `tdl.exe`
- 放置路径：`<项目根目录>/tdl.exe`
- 本 GUI 适配的版本：**0.20.3**（其他版本多数兼容）

**aria2c.exe** —— 下载后放在 `aria2/` 子目录：

- 来源：<https://github.com/aria2/aria2/releases>
- 选择 `aria2-*-win-64bit-build1.zip`，解压得到 `aria2c.exe`
- 放置路径：`<项目根目录>/aria2/aria2c.exe`

放好后目录结构应为：

```
TDL-Downloader/
├── tdl_gui.pyw            # 主程序
├── 启动TDL下载器.bat       # 双击启动
├── tdl.exe                # ← 手动放置
├── aria2/
│   └── aria2c.exe         # ← 手动放置
└── README.md
```

### 3. 启动

双击 `启动TDL下载器.bat`，或命令行运行：

```bash
pythonw tdl_gui.pyw
```

首次启动会自动生成 `config.json`（默认配置见下文）。

---

## 使用说明

### 登录 Telegram

点击顶部 **登录** 按钮，选择一种方式：

| 方式 | 说明 |
|---|---|
| 桌面客户端（推荐） | 需已安装并登录 Telegram Desktop，自动复用其会话 |
| 二维码 | 用手机 Telegram 扫码 |
| 手机号 + 验证码 | 在弹出窗口中输入手机号与收到的验证码 |

登录信息保存在 `~/.tdl/data`（命名空间默认 `default`）。可点 **打开会话目录** 查看。

> 登录会打开独立控制台窗口交互，完成后窗口自动关闭。

### 下载

1. 在 **下载** 页把链接粘进文本框（支持混入其他文字，会自动提取 `https://t.me/...` 并去重）
2. 设置输出目录、并发数、代理
3. 点 **开始下载**
4. 进度列表实时刷新；已存在的文件自动标记「已完成」并跳过

### 其他页面

- **链接整理**：批量链接去重、按结尾数字升/降序排序
- **历史下载**：查看历次下载记录；「扫描输出目录」可按实际文件重建清单
- **下载设置**：输出目录、并发数、代理、仅视频、扩展名过滤、流量限额

---

## 配置项说明

配置保存在项目根目录 `config.json`，首次启动自动生成：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `out_dir` | `<项目目录>/DOW` | 下载输出目录 |
| `threads` | `8` | 单文件分片连接数 |
| `limit` | `4` | 同时下载的文件数 |
| `proxy` | `socks5://127.0.0.1:7890` | 代理地址，留空则直连 |
| `video_only` | `false` | 仅下载视频文件 |
| `video_ext` | `mp4,mkv,mov,avi` | 视频扩展名过滤 |
| `takeout` | `false` | 使用 takeout 会话（导出受限内容时启用） |
| `group` | `false` | 按群组分类存放 |
| `desktop_path` | 项目上级目录 | Telegram Desktop 数据目录 |
| `namespace` | `default` | tdl 登录命名空间 |
| `sort_desc` | `false` | 链接整理是否降序 |
| `aria2_path` | 空 | 自定义 `aria2c.exe` 路径，空则用 `aria2/aria2c.exe` |
| `quota_enable` | `false` | 是否启用月度流量限额 |
| `quota_gb` | `2` | 月度流量上限（GB） |

> `config.json` 含本机绝对路径等个人信息，已在 `.gitignore` 中排除，不会被提交。

---

## 常见问题

**Q：提示找不到 `tdl.exe` / `aria2c.exe`？**
按「安装步骤 2」下载并放到正确路径。`aria2c.exe` 必须放在 `aria2/` 子目录下。

**Q：下载完文件名还带 `.part`？**
`.part` 表示未下完。若数据已完整但没转正，通常是残留的 `tdl.exe` / `aria2c.exe` 进程占着文件句柄。程序收尾时会自动清理残留进程并重试改名；仍失败会在日志中提示手动去掉 `.part`。

**Q：上次中断的 `.part` 能续传吗？**
能——只要同目录下还有对应的 `.part.aria2` 控制文件，重跑即从断点继续。若控制文件已丢（如进程被强杀），该文件只能从头下载。

**Q：下载速度慢 / 连不上？**
检查代理设置（默认 `socks5://127.0.0.1:7890`）。速度上限取决于你的代理与 Telegram 账号类型。

**Q：`tdl serve 断连`？**
网络或代理不稳定导致。重跑即可，已下完的文件会自动跳过，未下完的续传。

**Q：日志在哪？**
项目根目录 `logs/`，按启动时间命名。

---

## 项目结构

```
TDL-Downloader/
├── tdl_gui.pyw            # 主程序（GUI 全部逻辑）
├── 启动TDL下载器.bat       # 启动脚本（自动查找 pythonw.exe）
├── docs/                  # README 截图
├── .gitignore             # 排除二进制、运行时数据、下载内容
├── README.md              # 本文档
└── LICENSE                # AGPL-3.0（继承自 iyear/tdl）
```

---

## 许可

本项目基于 [iyear/tdl](https://github.com/iyear/tdl) 构建，遵循 **AGPL-3.0** 许可协议。

`tdl` 与 `aria2` 的版权归各自作者所有。本仓库仅提供图形界面封装层。
