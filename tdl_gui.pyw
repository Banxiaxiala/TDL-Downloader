# -*- coding: utf-8 -*-
"""
TDL 视频下载器 GUI
- 多 Tab 结构：下载 / 链接整理
- 配置自动持久化（config.json）
- 内置登录功能（桌面客户端/二维码/手机号）
- 链接提取去重 + 按结尾数字排序
"""
import os
import re
import sys
import glob
import json
import socket
import time
import threading
import subprocess
import queue
import datetime
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

def _app_dir():
    """程序所在目录（数据文件都放这里）

    打包成 exe 后 __file__ 指向 PyInstaller 的临时解压目录，
    用它当基目录会导致找不到 tdl.exe，且配置/日志/下载目录
    每次运行都变、重启即丢。所以冻结时以 exe 所在目录为准。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_dir()
TDL_EXE = os.path.join(BASE_DIR, "tdl.exe")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

TELEGRAM_DESKTOP_DIR = os.path.normpath(os.path.join(BASE_DIR, ".."))
TDL_DATA_DIR = os.path.join(os.path.expanduser("~"), ".tdl", "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

LINKS_FILE = os.path.join(BASE_DIR, "links.txt")            # 下载Tab 链接持久化
LINKS_SORT_FILE = os.path.join(BASE_DIR, "链接整理输入.txt")  # 链接整理Tab 输入持久化
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")        # 历史下载记录（按次）
FILES_FILE = os.path.join(BASE_DIR, "history_files.json")    # 历史文件清单（平铺）
QUOTA_FILE = os.path.join(BASE_DIR, "traffic_quota.json")    # 流量统计（按自然月）
META_FILE = os.path.join(BASE_DIR, "file_meta_cache.json")   # 链接 -> 文件名/大小 缓存

# 文件名标识：群ID_消息ID（如 1710039486_5793_... 中的 1710039486_5793）
FILE_ID_RE = re.compile(r"^(\d{6,}_\d{1,})_")

MAX_PATH_LEN = 255  # Windows 路径长度上限（留一点余量）


def kill_tree(proc):
    """强制终止进程及其全部子进程

    Windows 上 subprocess.terminate() 只杀直接子进程，tdl 的子进程会残留，
    残留的 tdl.exe 会一直占着数据库锁，导致后续下载全部失败。
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kill_leftover_processes():
    """清理残留的 tdl.exe / aria2c.exe

    上一次异常退出（关窗口/崩溃/强制停止）可能留下持锁进程，
    开始新下载前清理，避免「Current database is used by another process」。
    只杀本目录下的进程，不误伤其它程序。
    """
    killed = []
    base = os.path.normcase(BASE_DIR)
    # 一次性拿到所有相关进程的 PID 和可执行文件路径（wmic 在新版 Windows 已移除）
    # 必须强制 PowerShell 用 UTF-8 输出，否则中文路径会乱码导致下面的路径比对失败
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process -Filter "
          "\"Name='tdl.exe' or Name='aria2c.exe'\" | "
          "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.ExecutablePath }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20).stdout or ""
    except Exception:
        return killed

    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid, _, path = line.partition("|")
        pid, path = pid.strip(), path.strip()
        if not pid.isdigit() or not path:
            continue
        if not os.path.normcase(os.path.abspath(path)).startswith(base):
            continue
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
            killed.append("%s(%s)" % (os.path.basename(path), pid))
        except Exception:
            pass
    return killed

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")          # 终端配色转义序列
# aria2 进度片段，如 [#1b2c3d 1.2GiB/1.6GiB(75%) CN:8 DL:12MiB]
ARIA2_PROGRESS_RE = re.compile(
    r"\[#\w{6}\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\)"     # 已下/总量/百分比
    r"(?:\s+CN:(\d+))?"                                  # 连接数
    r"(?:\s+DL:([\d.]+\w+))?"                            # 速度
    r"(?:\s+ETA:([\dhms]+))?"                            # 剩余时间
    r"\]"
)


def strip_ansi(s):
    """去掉终端配色转义序列，避免日志里出现 [1;32m 这类乱码"""
    return ANSI_RE.sub("", s).replace("\x1b", "").strip()


def parse_aria2_progress(line):
    """把 aria2 进度行转成「已下 / 总量 (百分比) 速度 剩余」的紧凑中文形式

    非进度行返回 None。
    """
    m = ARIA2_PROGRESS_RE.search(line)
    if not m:
        return None
    done, total, pct, conn, speed, eta = m.groups()
    parts = ["%s / %s (%s%%)" % (done, total, pct)]
    if speed:
        parts.append("速度 %s/s" % speed)
    if eta:
        parts.append("剩余 %s" % eta)
    return " | ".join(parts)


def strip_dup_prog(line):
    """进度行以 [#gid ...] 开头、以 ] 结尾的整行，交给 RPC 显示，界面直接丢掉"""
    return ARIA2_PROGRESS_RE.fullmatch(line.strip()) is not None


def parse_size(text):
    """把 "1.2MiB" / "900KiB" / "512B" 解析成字节数；无法解析返回 0"""
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?i?B)\s*$", str(text), re.I)
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except Exception:
        return 0
    unit = m.group(2).upper()
    mult = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
            "TIB": 1024 ** 4, "KB": 1000, "MB": 1000 ** 2,
            "GB": 1000 ** 3, "TB": 1000 ** 4}.get(unit, 1)
    return int(val * mult)


def _parse_speed(text):
    """把速度列的文字（"1.2MiB/s" / "3.5MB/s" / "-"）解析成 字节/秒；无法解析返回 0"""
    s = str(text).strip()
    if not s or s == "-":
        return 0
    s = s.split("/")[0].strip()          # 去掉 "/s"
    return parse_size(s)


def human_size(n):
    """字节数转 GiB/MiB 可读形式"""
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % int(n)
        n /= 1024
    return "%.1fPiB" % n


def fmt_eta(sec):
    """秒数转「1分23秒」形式"""
    try:
        sec = int(sec)
    except Exception:
        return ""
    if sec < 0:
        return ""
    if sec >= 3600:
        return "%d时%02d分" % (sec // 3600, (sec % 3600) // 60)
    if sec >= 60:
        return "%d分%02d秒" % (sec // 60, sec % 60)
    return "%d秒" % sec


def make_bar(pct, width=12):
    """用字符画出进度条

    注意 █ / ░ 是全角字符，每个约占 2 个 ASCII 字符宽，
    所以 Treeview 的列宽必须 >= width*2 个字符宽，否则会被截断。
    """
    try:
        pct = max(0.0, min(100.0, float(pct)))
    except Exception:
        pct = 0.0
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


DEFAULT_CONFIG = {
    "out_dir": os.path.join(BASE_DIR, "DOW"),
    "threads": 8,
    "limit": 4,
    "proxy": "socks5://127.0.0.1:7890",
    "video_only": False,
    "video_ext": "mp4,mkv,mov,avi",
    "takeout": False,
    "group": False,
    "desktop_path": TELEGRAM_DESKTOP_DIR,
    "namespace": "default",
    "sort_desc": False,
    "aria2_path": "",
    # 流量限额（按自然月累计）
    "quota_enable": False,
    "quota_gb": 2,              # 月度限额，单位 GB
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if k in cfg})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def current_month():
    """当前自然月，形如 2026-09"""
    return time.strftime("%Y-%m")


def load_quota():
    """读取流量统计；跨月自动清零

    返回 {"month": "2026-09", "used": 字节数, "history": {月份: 字节数}}
    """
    data = {"month": current_month(), "used": 0, "history": {}}
    try:
        if os.path.isfile(QUOTA_FILE):
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                data["month"] = saved.get("month") or data["month"]
                data["used"] = int(saved.get("used", 0) or 0)
                hist = saved.get("history")
                if isinstance(hist, dict):
                    data["history"] = hist
    except Exception:
        pass
    # 跨月：把上月用量归档，本月从 0 开始
    cur = current_month()
    if data["month"] != cur:
        if data["used"] > 0:
            data["history"][data["month"]] = data["used"]
        data["month"] = cur
        data["used"] = 0
        save_quota(data)
    return data


def save_quota(data):
    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def is_logged_in():
    if not os.path.isdir(TDL_DATA_DIR):
        return False
    try:
        for _ in os.listdir(TDL_DATA_DIR):
            return True
    except Exception:
        return False
    return False


def extract_and_dedup(content):
    """从文本提取 https://t.me/... 链接并去重，返回列表（保持原顺序）"""
    parts = re.split(r"(?=https://)", content)
    seen = set()
    unique = []
    for p in parts:
        u = p.strip()
        if not u.startswith("https://t.me/"):
            continue
        u = re.split(r"\s", u, maxsplit=1)[0]
        u = u.rstrip(",;，；}）)")
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def url_tail_number(url):
    """提取 URL 结尾的连续数字作为排序键，无数字返回无穷大排到最后"""
    m = re.search(r"(\d+)/?$", url)
    return int(m.group(1)) if m else float("inf")


def find_free_port():
    """获取一个本机空闲端口"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sanitize_filename(name, max_len=110):
    """去掉 Windows 文件名非法字符/控制字符，并限制长度

    注意：Windows 路径总长上限 260（MAX_PATH）。aria2 还会在同目录下写
    「文件名 + .part + .aria2」控制文件，所以文件名本身必须留足余量，
    否则报 "Failed to write into the segment file"（退出码 1）。
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)      # 路径分隔符与非法字符
    name = re.sub(r"[\x00-\x1f\x7f-\x9f]", "_", name)     # C0/C1 控制字符
    name = name.strip(" .")
    name = name[:max_len] or "file"
    return name


def fmt_size(n):
    """字节数转可读大小"""
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return ""


def load_history():
    """读取历史下载记录（JSON 列表）"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def append_history(entry, limit=100):
    """追加一条历史记录，超出 limit 截断"""
    items = load_history()
    items.append(entry)
    if len(items) > limit:
        items = items[-limit:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def extract_file_id(filename):
    """从文件名提取唯一 ID，如 1710039486_5793_xxx.mp4 → 1710039486_5793

    不符合命名规范时返回空串。
    """
    m = FILE_ID_RE.match(os.path.basename(filename))
    return m.group(1) if m else ""


def load_file_records():
    """读取历史文件清单，返回 {id: record} 字典（id 为 群ID_消息ID）"""
    try:
        with open(FILES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):        # 兼容列表形式
            return {r.get("id", ""): r for r in data if r.get("id")}
    except Exception:
        pass
    return {}


def save_file_records(records):
    """保存历史文件清单"""
    try:
        with open(FILES_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def tme_key(url):
    """从 t.me 链接提取 群ID_消息ID 作为缓存键，无法识别时返回空串"""
    m = re.search(r"t\.me/(?:c/)?(\d+)/(\d+)", url or "")
    return "%s_%s" % (m.group(1), m.group(2)) if m else ""


def load_meta_cache():
    """读取 链接 -> {name, size} 缓存（避免每次续传都重新 HEAD 取文件名与大小）"""
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def upsert_meta_cache(pairs):
    """批量写入 链接 -> {name, size} 缓存

    pairs: [(键, 原始文件名, 字节数)]
    """
    if not pairs:
        return
    cache = load_meta_cache()
    for key, name, size in pairs:
        if not key:
            continue
        old = cache.get(key) or {}
        rec = {"name": name or old.get("name", ""),
               "size": int(size or old.get("size", 0) or 0)}
        if rec["name"] or rec["size"] > 0:
            cache[key] = rec
    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def upsert_file_records(items):
    """批量写入/更新文件记录

    items: [(id, 文件名, 大小, 状态, 时间)]，id 为空时用文件名兜底做键
    返回 (新增数, 更新数)
    """
    records = load_file_records()
    added = updated = 0
    for fid, name, size, status, ts in items:
        key = fid or ("__" + name)
        old = records.get(key)
        if old:
            # 未下完的 .part 不能冲掉已有的「已下载」记录（正式文件可能还在）
            if status == "未完成" and old.get("status") == "已下载":
                continue
            old.update({"name": name, "size": size, "status": status, "time": ts})
            if fid:
                old["id"] = fid
            updated += 1
        else:
            records[key] = {"id": fid, "name": name, "size": size,
                            "status": status, "time": ts}
            added += 1
    save_file_records(records)
    return added, updated


def prune_file_records(valid_paths):
    """按文件是否实际存在，标记记录状态（存在=已下载，缺失=文件丢失）"""
    records = load_file_records()
    for key, rec in records.items():
        name = rec.get("name", "")
        rec["status"] = "已下载" if name in valid_paths else "文件缺失"
    save_file_records(records)
    return records
class TDLApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TDL 视频下载器")
        self.root.geometry("840x720")
        self.root.minsize(740, 620)

        self.cfg = load_config()
        self.process = None
        self.login_process = None
        self.out_q = queue.Queue()
        self.running = False
        self.stop_requested = False
        # aria2 RPC 进度查询
        self._rpc_stop = True
        self._prog_gids = []
        self._prog_names = {}
        self._prog_marks = {}
        # 前台下载进度列表：行 iid / 每个文件的元数据
        self._dl_iids = {}
        self._dl_meta = {}
        self._ordered_names = []    # 本次任务的链接原顺序（决定进度列表行序）
        self._serve_output = ""     # tdl serve 的最后一行输出（启动失败原因）
        # 初始化本次运行的日志文件（实时保存）
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_file = os.path.join(LOG_DIR, "tdl_log_%s.txt" % ts)
        except Exception:
            self.log_file = None

        self._build_style()
        self._build_ui()
        self._load_persisted_links()
        self._refresh_login_status()
        self._poll_output()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.nb.bind("<<NotebookTabChanged>>", self._on_nb_changed)

    # ---------- UI ----------
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Small.TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)

        # ===== Tab 1: 下载 =====
        self.tab_download = ttk.Frame(self.nb)
        self.nb.add(self.tab_download, text="  下载  ")
        self._build_download_tab(self.tab_download)

        # ===== Tab 2: 链接整理 =====
        self.tab_sort = ttk.Frame(self.nb)
        self.nb.add(self.tab_sort, text="  链接整理  ")
        self._build_sort_tab(self.tab_sort)

        # ===== Tab 3: 历史下载 =====
        self.tab_history = ttk.Frame(self.nb)
        self.nb.add(self.tab_history, text="  历史下载  ")
        self._build_history_tab(self.tab_history)

        # ===== Tab 4: 下载设置 =====
        self.tab_quota = ttk.Frame(self.nb)
        self.nb.add(self.tab_quota, text="  下载设置  ")
        self._build_quota_tab(self.tab_quota)

    def _build_download_tab(self, parent):
        pad = {"padx": 8, "pady": 4}

        # ---- 顶部工具栏：登录 ----
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(4, 4))
        self.login_status_var = tk.StringVar(value="登录状态: 检查中...")
        ttk.Label(top, textvariable=self.login_status_var, style="Status.TLabel").pack(side="left")
        ttk.Button(top, text="登录", command=self.open_login_dialog).pack(side="right", padx=4)
        ttk.Button(top, text="刷新状态", command=self._refresh_login_status).pack(side="right", padx=4)
        ttk.Button(top, text="打开会话目录", command=self.open_tdl_data).pack(side="right", padx=4)

        # ---- 链接区 ----
        link_frame = ttk.LabelFrame(parent, text="消息链接（随意粘贴，自动提取 https://t.me/... 并去重）")
        link_frame.pack(fill="both", expand=False, **pad)

        self.link_text = tk.Text(link_frame, height=7, wrap="none",
                                 font=("Consolas", 10), undo=True)
        self.link_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        link_sb = ttk.Scrollbar(link_frame, command=self.link_text.yview)
        link_sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.link_text.configure(yscrollcommand=link_sb.set)

        link_bar = ttk.Frame(parent)
        link_bar.pack(fill="x", padx=8)
        ttk.Button(link_bar, text="提取并去重", command=self.extract_links).pack(side="left")
        ttk.Button(link_bar, text="清空", command=lambda: self.link_text.delete("1.0", "end")).pack(side="left", padx=4)
        ttk.Button(link_bar, text="从文件加载", command=self.load_from_file).pack(side="left")
        self.link_count_var = tk.StringVar(value="有效链接: 0 条")
        ttk.Label(link_bar, textvariable=self.link_count_var, style="Small.TLabel").pack(side="right")

        # ---- 参数区（大部分下载参数已移到「下载设置」Tab）----
        opt_frame = ttk.LabelFrame(parent, text="下载参数（自动保存）")
        opt_frame.pack(fill="x", **pad)

        ttk.Label(opt_frame, text="保存目录:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.dir_var = tk.StringVar(value=self.cfg["out_dir"])
        self.dir_var.trace_add("write", self._on_cfg_change)
        ttk.Entry(opt_frame, textvariable=self.dir_var, width=46).grid(row=0, column=1, sticky="we", padx=4, pady=6)
        ttk.Button(opt_frame, text="浏览...", command=self.browse_dir).grid(row=0, column=2, padx=4, pady=6)
        opt_frame.columnconfigure(1, weight=1)

        ttk.Label(opt_frame,
                  text="线程 / 并发 / 代理 / 扩展名 / aria2 等参数已移至「下载设置」Tab",
                  style="Small.TLabel").grid(row=1, column=0, columnspan=3,
                                             sticky="w", padx=6, pady=(0, 6))

        # ---- 顶部工具栏按钮（原有的登录按钮区）----
        # 注：不再在此处创建参数控件，全部在「下载设置」Tab 中创建

        # ---- 操作按钮 ----
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill="x", padx=8, pady=8)
        self.start_btn = ttk.Button(btn_frame, text="开始下载", style="Accent.TButton", command=self.start_download)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="停止", state="disabled", command=self.stop_download)
        self.stop_btn.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(btn_frame, textvariable=self.status_var, style="Small.TLabel").pack(side="left", padx=12)
        ttk.Button(btn_frame, text="打开下载目录", command=self.open_dir).pack(side="right")

        # ---- 下载进度区（前台显示）----
        prog_frame = ttk.LabelFrame(parent, text="下载进度")
        prog_frame.pack(fill="both", expand=True, **pad)

        # 总体进度：进度条 + 文字
        top = ttk.Frame(prog_frame)
        top.pack(fill="x", padx=8, pady=(6, 2))
        self.overall_var = tk.DoubleVar(value=0.0)
        self.overall_bar = ttk.Progressbar(top, variable=self.overall_var,
                                           maximum=100.0, length=200)
        self.overall_bar.pack(side="left", fill="x", expand=True)
        self.overall_text_var = tk.StringVar(value="等待开始")
        ttk.Label(top, textvariable=self.overall_text_var,
                  style="Small.TLabel", width=42).pack(side="left", padx=8)

        # 文件列表：每个文件一行进度
        tree_wrap = ttk.Frame(prog_frame)
        tree_wrap.pack(fill="both", expand=True, padx=8, pady=(2, 4))
        cols = ("bar", "pct", "size", "speed", "eta", "status")
        self.dl_tree = ttk.Treeview(tree_wrap, columns=cols, show="tree headings",
                                    height=8)
        self.dl_tree.heading("#0", text="文件名")
        self.dl_tree.column("#0", width=190, anchor="w")
        for c, title, w, anchor in (
                ("bar", "进度", 120, "w"),
                ("pct", "百分比", 55, "center"),
                ("size", "已下载 / 总量", 130, "center"),
                ("speed", "速度", 75, "center"),
                ("eta", "剩余", 60, "center"),
                ("status", "状态", 60, "center")):
            self.dl_tree.heading(c, text=title)
            self.dl_tree.column(c, width=w, anchor=anchor)
        dsb = ttk.Scrollbar(tree_wrap, command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dsb.set)
        self.dl_tree.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        self.dl_tree.tag_configure("done", foreground="#6a9955")
        self.dl_tree.tag_configure("error", foreground="#f44747")
        self.dl_tree.tag_configure("wait", foreground="#808080")
        self.dl_tree.tag_configure("active", foreground="#6a9955")

        # 底部：状态提示 + 日志入口（日志只后台记录）
        log_bar = ttk.Frame(parent)
        log_bar.pack(fill="x", padx=8, pady=(0, 4))
        self.hint_var = tk.StringVar(value="")
        ttk.Label(log_bar, textvariable=self.hint_var, style="Small.TLabel").pack(side="left")
        ttk.Button(log_bar, text="打开日志目录", command=self.open_log_dir).pack(side="right")
        self.log_path_var = tk.StringVar(value="本次日志文件: 未生成")
        ttk.Label(log_bar, textvariable=self.log_path_var, style="Small.TLabel").pack(side="right", padx=12)

        # 隐藏的日志控件：日志改后台记录，界面上不再显示
        self.log_text = tk.Text(parent)

    def _build_history_tab(self, parent):
        """历史下载 Tab：平铺列出所有下载过的文件，支持导入文件夹识别"""
        pad = {"padx": 8, "pady": 4}

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(6, 4))
        ttk.Button(bar, text="导入文件夹", command=self.history_import_dir).pack(side="left")
        ttk.Button(bar, text="从下载目录刷新", command=self.history_scan_outdir).pack(side="left", padx=4)
        ttk.Button(bar, text="重新下载所选", command=self.history_redownload).pack(side="left", padx=4)
        ttk.Button(bar, text="定位文件", command=self.history_locate).pack(side="left", padx=4)
        ttk.Button(bar, text="删除所选记录", command=self.history_delete).pack(side="left", padx=4)
        ttk.Button(bar, text="全部清空", command=self.history_clear).pack(side="left", padx=4)
        ttk.Button(bar, text="打开下载目录", command=self.open_dir).pack(side="right")
        self.history_count_var = tk.StringVar(value="共 0 个文件")
        ttk.Label(bar, textvariable=self.history_count_var, style="Small.TLabel").pack(side="right", padx=8)

        filt = ttk.Frame(parent)
        filt.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(filt, text="筛选:", style="Small.TLabel").pack(side="left")
        self.history_filter_var = tk.StringVar()
        ent = ttk.Entry(filt, textvariable=self.history_filter_var)
        ent.pack(side="left", fill="x", expand=True, padx=4)
        self.history_filter_var.trace_add("write", lambda *a: self.refresh_history())
        ttk.Label(filt, text="（输入 ID / 文件名关键字过滤）", style="Small.TLabel").pack(side="left")

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True, **pad)
        cols = ("id", "size", "time", "status")
        self.history_tree = ttk.Treeview(tree_frame, columns=cols, show="tree headings",
                                         selectmode="extended")
        self.history_tree.heading("#0", text="文件名")
        self.history_tree.column("#0", width=430, anchor="w")
        for c, title, w, anchor in (
                ("id", "ID（群_消息）", 150, "center"),
                ("size", "大小", 90, "center"),
                ("time", "下载时间", 140, "center"),
                ("status", "状态", 80, "center")):
            self.history_tree.heading(c, text=title)
            self.history_tree.column(c, width=w, anchor=anchor)
        hs = ttk.Scrollbar(tree_frame, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=hs.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        hs.pack(side="right", fill="y")
        self.history_tree.tag_configure("missing", foreground="#808080")

        self.history_tip_var = tk.StringVar(
            value="双击可定位文件 · 「导入文件夹」会递归扫描并用 群ID_消息ID 识别文件")
        ttk.Label(parent, textvariable=self.history_tip_var, style="Small.TLabel").pack(
            anchor="w", padx=10, pady=(0, 6))

        self.history_tree.bind("<Double-1>", lambda e: self.history_locate())

    def refresh_history(self):
        """刷新历史文件清单（平铺，按时间倒序；支持关键字筛选）"""
        try:
            for iid in self.history_tree.get_children():
                self.history_tree.delete(iid)
            records = load_file_records()
            kw = self.history_filter_var.get().strip().lower()
            rows = []
            for key, rec in records.items():
                name = rec.get("name", "")
                fid = rec.get("id", "")
                if kw and kw not in name.lower() and kw not in fid.lower():
                    continue
                rows.append((rec.get("time", ""), fid, key, rec))
            rows.sort(key=lambda x: x[0], reverse=True)

            n_missing = 0
            for ts, fid, key, rec in rows:
                status = rec.get("status", "")
                if status != "已下载":
                    n_missing += 1
                self.history_tree.insert(
                    "", "end", iid=key, text=rec.get("name", ""),
                    values=(fid or "-", fmt_size(rec.get("size", 0)),
                            ts, status),
                    tags=("missing",) if status != "已下载" else ())
            total = len(records)
            self.history_count_var.set(
                "共 %d 个文件（显示 %d，缺失 %d）" % (total, len(rows), n_missing))
        except Exception as e:
            self._log("刷新历史失败: %s" % e, "err")

    def _selected_history_keys(self):
        """返回选中的记录键列表"""
        return list(self.history_tree.selection())

    def _record_path(self, rec):
        """记录对应文件的实际路径（可能在下载目录或导入目录）"""
        name = rec.get("name", "")
        cands = []
        if rec.get("path"):
            cands.append(rec["path"])
        if hasattr(self, "dir_var") and self.dir_var.get():
            cands.append(os.path.join(self.dir_var.get(), name))
        for p in cands:
            if p and os.path.isfile(p):
                return p
        return cands[0] if cands else ""

    def history_locate(self):
        keys = self._selected_history_keys()
        if not keys:
            messagebox.showinfo("提示", "请先选择一个文件记录。")
            return
        records = load_file_records()
        rec = records.get(keys[0])
        if not rec:
            return
        path = self._record_path(rec)
        if path and os.path.isfile(path):
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            except Exception as e:
                self._log("定位失败: %s" % e, "err")
        else:
            messagebox.showinfo("提示", "文件不存在（可能已被移动或删除）。")

    def history_redownload(self):
        """把所选文件的链接重新发到下载 Tab"""
        keys = self._selected_history_keys()
        if not keys:
            messagebox.showinfo("提示", "请先选择要重新下载的文件。")
            return
        records = load_file_records()
        links = []
        for k in keys:
            rec = records.get(k, {})
            url = rec.get("url", "")
            if url:
                links.append(url)
        if not links:
            messagebox.showinfo("提示", "所选记录没有保存链接（导入的文件无法重新下载）。")
            return
        self.link_text.delete("1.0", "end")
        self.link_text.insert("1.0", "\n".join(links))
        self.nb.select(self.tab_download)
        self.extract_links()
        self._log("已把 %d 条链接发送到下载 Tab" % len(links), "ok")

    def history_delete(self):
        keys = self._selected_history_keys()
        if not keys:
            messagebox.showinfo("提示", "请先选择要删除的记录。")
            return
        if not messagebox.askyesno("删除记录", "确定从清单中删除这 %d 条记录吗？\n（只删清单，不删文件）" % len(keys)):
            return
        records = load_file_records()
        for k in keys:
            records.pop(k, None)
        save_file_records(records)
        self.refresh_history()
        self._log("已删除 %d 条记录" % len(keys), "ok")

    def history_import_dir(self):
        """导入文件夹：递归扫描并用 群ID_消息ID 识别文件"""
        d = filedialog.askdirectory(title="选择要导入的文件夹")
        if not d:
            return
        self._log("正在扫描目录: %s" % d, "info")
        threading.Thread(target=self._import_dir_worker, args=(d,), daemon=True).start()

    def _import_dir_worker(self, folder):
        """后台扫描目录并写入清单（递归）"""
        try:
            items, seen, noid = [], set(), 0
            V_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
                      ".webm", ".ts", ".m4v", ".mpg", ".mpeg", ".rmvb", ".3gp"}
            for root_dir, _, files in os.walk(folder):
                for fn in files:
                    # 跳过临时/控制文件
                    if fn.endswith(".aria2") or fn.endswith(".aria2__temp"):
                        continue
                    low = fn.lower()
                    is_part = low.endswith(".part")
                    if is_part:
                        continue
                    if os.path.splitext(low)[1] not in V_EXTS:
                        continue
                    full = os.path.join(root_dir, fn)
                    try:
                        size = os.path.getsize(full)
                    except Exception:
                        size = 0
                    fid = extract_file_id(fn)
                    if not fid:
                        noid += 1
                    if fid and fid in seen:
                        continue
                    if fid:
                        seen.add(fid)
                    ts = datetime.datetime.fromtimestamp(
                        os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S")
                    items.append((fid, fn, size, "已下载", ts, full))

            if not items:
                self._log("该目录未找到可识别的视频文件", "warn")
                return

            records = load_file_records()
            added = updated = 0
            for fid, name, size, status, ts, full in items:
                key = fid or ("__" + name)
                old = records.get(key)
                if old:
                    old.update({"name": name, "size": size, "status": status,
                                "time": old.get("time") or ts, "path": full})
                    if fid:
                        old["id"] = fid
                    updated += 1
                else:
                    records[key] = {"id": fid, "name": name, "size": size,
                                    "status": status, "time": ts, "path": full}
                    added += 1
            save_file_records(records)
            self.out_q.put(("导入完成：新增 %d，更新 %d，共扫描到 %d 个文件%s"
                            % (added, updated, len(items),
                               ("（其中 %d 个无 ID）" % noid) if noid else ""), "ok"))
        except Exception as e:
            self.out_q.put(("导入失败: %s" % e, "err"))

    def history_scan_outdir(self):
        """从当前下载目录扫描并刷新清单状态"""
        d = self.dir_var.get() if hasattr(self, "dir_var") else ""
        if not d or not os.path.isdir(d):
            messagebox.showinfo("提示", "下载目录不存在，请先设置。")
            return
        self._log("正在扫描下载目录: %s" % d, "info")
        threading.Thread(target=self._import_dir_worker, args=(d,), daemon=True).start()

    def history_clear(self):
        if not messagebox.askyesno("清空历史", "确定清空全部历史文件清单吗？\n（只清清单，不删文件）"):
            return
        try:
            with open(FILES_FILE, "w", encoding="utf-8") as f:
                f.write("{}")
        except Exception:
            pass
        self.refresh_history()
        self._log("已清空历史文件清单", "ok")

    def _on_nb_changed(self, event):
        try:
            if self.nb.index("current") == 2:  # 历史下载 Tab
                self.refresh_history()
        except Exception:
            pass

    # ---------- 链接持久化 ----------
    def _load_persisted_links(self):
        for widget, path in ((self.link_text, LINKS_FILE), (self.sort_in_text, LINKS_SORT_FILE)):
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if content.strip():
                        widget.insert("1.0", content)
            except Exception:
                pass

    def _save_persisted_links(self):
        try:
            with open(LINKS_FILE, "w", encoding="utf-8") as f:
                f.write(self.link_text.get("1.0", "end"))
        except Exception:
            pass
        try:
            with open(LINKS_SORT_FILE, "w", encoding="utf-8") as f:
                f.write(self.sort_in_text.get("1.0", "end"))
        except Exception:
            pass

    def _build_quota_tab(self, parent):
        """下载设置 Tab：下载参数 + 月度流量限额"""
        pad = {"padx": 8, "pady": 4}

        # ---------- 下载参数（从下载 Tab 移过来）----------
        opt_frame = ttk.LabelFrame(parent, text="下载参数（自动保存）")
        opt_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(opt_frame)
        row1.grid(row=0, column=0, columnspan=3, sticky="we", padx=6, pady=6)
        ttk.Label(row1, text="单任务线程 (-t):").pack(side="left")
        self.threads_var = tk.IntVar(value=self.cfg["threads"])
        self.threads_var.trace_add("write", self._on_cfg_change)
        ttk.Spinbox(row1, from_=1, to=32, width=5, textvariable=self.threads_var).pack(side="left", padx=(4, 16))
        ttk.Label(row1, text="并发任务 (-l):").pack(side="left")
        self.limit_var = tk.IntVar(value=self.cfg["limit"])
        self.limit_var.trace_add("write", self._on_cfg_change)
        ttk.Spinbox(row1, from_=1, to=16, width=5, textvariable=self.limit_var).pack(side="left", padx=(4, 16))
        ttk.Label(row1, text="代理:").pack(side="left")
        self.proxy_var = tk.StringVar(value=self.cfg["proxy"])
        self.proxy_var.trace_add("write", self._on_cfg_change)
        ttk.Entry(row1, textvariable=self.proxy_var, width=28).pack(side="left", padx=4)

        row_ns = ttk.Frame(opt_frame)
        row_ns.grid(row=1, column=0, columnspan=3, sticky="we", padx=6, pady=4)
        ttk.Label(row_ns, text="桌面客户端路径:").pack(side="left")
        self.desktop_var = tk.StringVar(value=self.cfg["desktop_path"])
        self.desktop_var.trace_add("write", self._on_cfg_change)
        ttk.Entry(row_ns, textvariable=self.desktop_var, width=40).pack(side="left", padx=4)
        ttk.Button(row_ns, text="浏览...", command=self.browse_desktop).pack(side="left", padx=4)
        ttk.Label(row_ns, text="命名空间(-n):").pack(side="left", padx=(12, 0))
        self.ns_var = tk.StringVar(value=self.cfg["namespace"])
        self.ns_var.trace_add("write", self._on_cfg_change)
        ttk.Entry(row_ns, textvariable=self.ns_var, width=10).pack(side="left", padx=4)

        row2 = ttk.Frame(opt_frame)
        row2.grid(row=2, column=0, columnspan=3, sticky="we", padx=6, pady=4)
        self.video_only_var = tk.BooleanVar(value=self.cfg["video_only"])
        self.video_only_var.trace_add("write", self._on_cfg_change)
        ttk.Checkbutton(row2, text="仅视频扩展名", variable=self.video_only_var, command=self.toggle_video_filter).pack(side="left")
        self.video_ext_var = tk.StringVar(value=self.cfg["video_ext"])
        self.video_ext_var.trace_add("write", self._on_cfg_change)
        self.video_ext_entry = ttk.Entry(row2, textvariable=self.video_ext_var, width=18, state="disabled")
        self.video_ext_entry.pack(side="left", padx=4)
        self.takeout_var = tk.BooleanVar(value=self.cfg["takeout"])
        self.takeout_var.trace_add("write", self._on_cfg_change)
        ttk.Checkbutton(row2, text="Takeout 会话", variable=self.takeout_var).pack(side="left", padx=12)
        self.group_var = tk.BooleanVar(value=self.cfg["group"])
        self.group_var.trace_add("write", self._on_cfg_change)
        ttk.Checkbutton(row2, text="相册合并 (--group)", variable=self.group_var).pack(side="left", padx=12)

        row3 = ttk.Frame(opt_frame)
        row3.grid(row=3, column=0, columnspan=3, sticky="we", padx=6, pady=4)
        ttk.Label(row3, text="aria2c 路径:").pack(side="left")
        self.aria2_path_var = tk.StringVar(value=self.cfg["aria2_path"])
        self.aria2_path_var.trace_add("write", self._on_cfg_change)
        ttk.Entry(row3, textvariable=self.aria2_path_var, width=30).pack(side="left", padx=4)
        ttk.Button(row3, text="浏览...", command=self.browse_aria2).pack(side="left")

        # ---------- 流量限额 ----------
        q_frame = ttk.LabelFrame(parent, text="月度流量限额（按自然月累计，达到限额自动停止）")
        q_frame.pack(fill="x", **pad)

        qrow = ttk.Frame(q_frame)
        qrow.pack(fill="x", padx=6, pady=6)
        self.quota_enable_var = tk.BooleanVar(value=self.cfg["quota_enable"])
        self.quota_enable_var.trace_add("write", self._on_cfg_change)
        ttk.Checkbutton(qrow, text="启用流量限额", variable=self.quota_enable_var,
                        command=self._on_quota_toggle).pack(side="left")
        ttk.Label(qrow, text="每月上限:").pack(side="left", padx=(16, 0))
        self.quota_gb_var = tk.IntVar(value=self.cfg["quota_gb"])
        self.quota_gb_var.trace_add("write", self._on_cfg_change)
        ttk.Spinbox(qrow, from_=1, to=1024, increment=1, width=10,
                    textvariable=self.quota_gb_var).pack(side="left", padx=4)
        ttk.Label(qrow, text="GB").pack(side="left")
        ttk.Button(qrow, text="重置本月用量", command=self.quota_reset).pack(side="right")

        # 用量展示：进度条 + 文字
        qbar_wrap = ttk.Frame(q_frame)
        qbar_wrap.pack(fill="x", padx=6, pady=(0, 4))
        self.quota_bar_var = tk.DoubleVar(value=0.0)
        self.quota_bar = ttk.Progressbar(qbar_wrap, variable=self.quota_bar_var,
                                         maximum=100.0, length=200)
        self.quota_bar.pack(side="left", fill="x", expand=True)
        self.quota_text_var = tk.StringVar(value="")
        ttk.Label(qbar_wrap, textvariable=self.quota_text_var,
                  style="Small.TLabel", width=52).pack(side="left", padx=8)

        self.quota_hint_var = tk.StringVar(value="")
        ttk.Label(q_frame, textvariable=self.quota_hint_var,
                  style="Small.TLabel").pack(anchor="w", padx=6, pady=(0, 6))

        # 历史月份用量
        hist_frame = ttk.LabelFrame(parent, text="历史月份用量")
        hist_frame.pack(fill="both", expand=True, **pad)
        cols = ("month", "used")
        self.quota_tree = ttk.Treeview(hist_frame, columns=cols, show="headings", height=6)
        self.quota_tree.heading("month", text="月份")
        self.quota_tree.heading("used", text="已用流量")
        self.quota_tree.column("month", width=120, anchor="center")
        self.quota_tree.column("used", width=160, anchor="center")
        qsb = ttk.Scrollbar(hist_frame, command=self.quota_tree.yview)
        self.quota_tree.configure(yscrollcommand=qsb.set)
        self.quota_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        qsb.pack(side="right", fill="y", pady=6, padx=(0, 6))

        self._quota = load_quota()
        self._quota_session_base = self._quota["used"]   # 本次会话开始时的累计值
        self.refresh_quota()

    def refresh_quota(self):
        """刷新流量用量显示"""
        used = self._quota["used"]
        limit = max(1, int(self.quota_gb_var.get() or 1)) * 1024 ** 3
        pct = min(100.0, used * 100.0 / limit)
        self.quota_bar_var.set(pct)
        self.quota_text_var.set("%s / %s  (%.1f%%)" % (
            human_size(used), human_size(limit), pct))
        if not self.quota_enable_var.get():
            self.quota_hint_var.set("未启用限额，下载不会因流量被中断。")
        elif used >= limit:
            self.quota_hint_var.set("⚠ 本月流量已用尽，重新开始下载会立即停止。")
        else:
            self.quota_hint_var.set("本月剩余可用流量: %s" % human_size(limit - used))
        # 历史月份
        for iid in self.quota_tree.get_children():
            self.quota_tree.delete(iid)
        for month in sorted(self._quota["history"], reverse=True):
            self.quota_tree.insert("", "end",
                                   values=(month, human_size(self._quota["history"][month])))

    def _on_quota_toggle(self):
        self._on_cfg_change()
        self.refresh_quota()

    def quota_reset(self):
        """把本月已用流量清零（不影响历史归档）"""
        if not messagebox.askyesno("确认", "将本月已用流量重置为 0？"):
            return
        self._quota["used"] = 0
        self._quota["month"] = current_month()
        save_quota(self._quota)
        self._quota_session_base = 0
        self.refresh_quota()
        self._log("本月流量用量已重置为 0", "warn")

    def quota_add(self, nbytes):
        """累加流量并落盘；返回 True 表示已达限额需要停止"""
        if nbytes <= 0:
            return False
        self._quota["used"] += int(nbytes)
        save_quota(self._quota)
        self.root.after(0, self.refresh_quota)
        if not self.quota_enable_var.get():
            return False
        limit = max(1, int(self.quota_gb_var.get() or 1)) * 1024 ** 3
        return self._quota["used"] >= limit

    def _quota_tripped(self):
        """当前是否已达限额"""
        if not self.quota_enable_var.get():
            return False
        limit = max(1, int(self.quota_gb_var.get() or 1)) * 1024 ** 3
        return self._quota["used"] >= limit

    def _build_sort_tab(self, parent):
        """链接整理 Tab：提取去重 + 按结尾数字排序"""
        pad = {"padx": 8, "pady": 4}

        intro = ttk.LabelFrame(parent, text="说明")
        intro.pack(fill="x", **pad)
        ttk.Label(intro, text="粘贴或加载链接 → 提取去重 → 按链接结尾数字由小到大排序 → 可保存/复制/发送到下载 Tab",
                  style="Small.TLabel").pack(anchor="w", padx=10, pady=8)

        # 输入区
        in_frame = ttk.LabelFrame(parent, text="输入链接（随意粘贴，一行一个或粘连均可）")
        in_frame.pack(fill="both", expand=True, **pad)
        self.sort_in_text = tk.Text(in_frame, height=10, wrap="none", font=("Consolas", 10), undo=True)
        self.sort_in_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        in_sb = ttk.Scrollbar(in_frame, command=self.sort_in_text.yview)
        in_sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.sort_in_text.configure(yscrollcommand=in_sb.set)

        # 操作栏
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text="提取去重并排序", command=self.do_sort).pack(side="left")
        ttk.Button(bar, text="从文件加载", command=self.sort_load_file).pack(side="left", padx=4)
        ttk.Button(bar, text="清空输入", command=lambda: self.sort_in_text.delete("1.0", "end")).pack(side="left", padx=4)
        ttk.Label(bar, text="排序:").pack(side="left", padx=(16, 4))
        self.sort_desc_var = tk.BooleanVar(value=self.cfg["sort_desc"])
        self.sort_desc_var.trace_add("write", self._on_cfg_change)
        ttk.Radiobutton(bar, text="升序", variable=self.sort_desc_var, value=False).pack(side="left")
        ttk.Radiobutton(bar, text="降序", variable=self.sort_desc_var, value=True).pack(side="left", padx=4)
        self.sort_count_var = tk.StringVar(value="有效链接: 0 条")
        ttk.Label(bar, textvariable=self.sort_count_var, style="Small.TLabel").pack(side="right")

        # 输出区
        out_frame = ttk.LabelFrame(parent, text="结果（已提取去重并排序，每行一条）")
        out_frame.pack(fill="both", expand=True, **pad)
        self.sort_out_text = tk.Text(out_frame, height=10, wrap="none", font=("Consolas", 10))
        self.sort_out_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        out_sb = ttk.Scrollbar(out_frame, command=self.sort_out_text.yview)
        out_sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.sort_out_text.configure(yscrollcommand=out_sb.set)

        # 输出操作栏
        obar = ttk.Frame(parent)
        obar.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(obar, text="复制结果", command=self.sort_copy_result).pack(side="left")
        ttk.Button(obar, text="保存到文件", command=self.sort_save_file).pack(side="left", padx=4)
        ttk.Button(obar, text="发送到下载 Tab", command=self.sort_send_to_download).pack(side="left", padx=4)
        ttk.Button(obar, text="清空结果", command=lambda: self.sort_out_text.delete("1.0", "end")).pack(side="left", padx=4)

    # ---------- 配置持久化 ----------
    def _on_cfg_change(self, *args):
        self.cfg.update({
            "out_dir": self.dir_var.get(),
            "threads": self.threads_var.get(),
            "limit": self.limit_var.get(),
            "proxy": self.proxy_var.get(),
            "video_only": self.video_only_var.get(),
            "video_ext": self.video_ext_var.get(),
            "takeout": self.takeout_var.get(),
            "group": self.group_var.get(),
            "desktop_path": self.desktop_var.get(),
            "namespace": self.ns_var.get(),
            "sort_desc": self.sort_desc_var.get(),
            "aria2_path": self.aria2_path_var.get(),
            "quota_enable": self.quota_enable_var.get(),
            "quota_gb": self.quota_gb_var.get(),
        })
        save_config(self.cfg)

    def _on_close(self):
        self._on_cfg_change()
        self._save_persisted_links()
        # 必须杀掉子进程：残留的 tdl.exe 会一直占着数据库锁，
        # 导致下次启动后所有下载都失败（Current database is used by another process）
        self._rpc_stop = True
        kill_leftover_processes()
        self.root.destroy()

    # ---------- 登录 ----------
    def _refresh_login_status(self):
        if is_logged_in():
            self.login_status_var.set("登录状态: 已登录 ✓")
        else:
            self.login_status_var.set("登录状态: 未登录 ✗（请先点击登录）")

    def open_tdl_data(self):
        if os.path.isdir(TDL_DATA_DIR):
            os.startfile(TDL_DATA_DIR)
        else:
            os.makedirs(os.path.dirname(TDL_DATA_DIR), exist_ok=True)
            os.startfile(os.path.dirname(TDL_DATA_DIR))

    def open_login_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("TDL 登录")
        dlg.geometry("440x300")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="选择登录方式", font=("Microsoft YaHei UI", 12, "bold")).pack(pady=10)

        frame = ttk.Frame(dlg)
        frame.pack(padx=20, pady=5, fill="x")

        ttk.Label(frame, text="代理:").grid(row=0, column=0, sticky="w", pady=4)
        login_proxy = tk.StringVar(value=self.cfg["proxy"])
        ttk.Entry(frame, textvariable=login_proxy, width=32).grid(row=0, column=1, sticky="we", padx=4, pady=4)

        ttk.Label(frame, text="桌面客户端路径:").grid(row=1, column=0, sticky="w", pady=4)
        login_desktop = tk.StringVar(value=self.cfg["desktop_path"])
        ttk.Entry(frame, textvariable=login_desktop, width=32).grid(row=1, column=1, sticky="we", padx=4, pady=4)

        ttk.Label(frame, text="命名空间:").grid(row=2, column=0, sticky="w", pady=4)
        login_ns = tk.StringVar(value=self.cfg["namespace"])
        ttk.Entry(frame, textvariable=login_ns, width=32).grid(row=2, column=1, sticky="we", padx=4, pady=4)

        frame.columnconfigure(1, weight=1)

        method = tk.IntVar(value=0)
        methods = ttk.Frame(dlg)
        methods.pack(pady=10, fill="x", padx=20)
        ttk.Radiobutton(methods, text="桌面客户端登录（推荐，需 Telegram Desktop 已登录）",
                        variable=method, value=0).pack(anchor="w", pady=2)
        ttk.Radiobutton(methods, text="二维码登录（手机 Telegram 扫码）",
                        variable=method, value=1).pack(anchor="w", pady=2)
        ttk.Radiobutton(methods, text="手机号 + 验证码",
                        variable=method, value=2).pack(anchor="w", pady=2)

        def do_login():
            proxy = login_proxy.get().strip()
            desktop = login_desktop.get().strip()
            ns = login_ns.get().strip()
            self.cfg["proxy"] = proxy
            self.cfg["desktop_path"] = desktop
            self.cfg["namespace"] = ns
            self.proxy_var.set(proxy)
            self.desktop_var.set(desktop)
            self.ns_var.set(ns)
            save_config(self.cfg)

            args = [TDL_EXE, "login", "-n", ns]
            if method.get() == 0:
                if desktop:
                    args += ["-d", desktop]
            elif method.get() == 1:
                args += ["-T", "qr"]
            else:
                args += ["-T", "code"]
            if proxy:
                args += ["--proxy", proxy]

            self._log("启动登录程序（在新窗口中交互完成）...", "info")
            self._log("登录命令: " + " ".join(args), "info")
            try:
                self.login_process = subprocess.Popen(
                    args,
                    cwd=BASE_DIR,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                dlg.destroy()
                threading.Thread(target=self._watch_login, daemon=True).start()
            except Exception as e:
                messagebox.showerror("错误", "启动登录失败: %s" % e)

        btns = ttk.Frame(dlg)
        btns.pack(pady=10)
        ttk.Button(btns, text="开始登录", command=do_login).pack(side="left", padx=8)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=8)

    def _watch_login(self):
        try:
            if self.login_process:
                self.login_process.wait()
        except Exception:
            pass
        self.root.after(0, self._refresh_login_status)
        self._log("登录程序已退出，已刷新登录状态", "info")

    # ---------- 链接整理 Tab ----------
    def sort_load_file(self):
        path = filedialog.askopenfilename(
            title="选择链接文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialdir=BASE_DIR)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.sort_in_text.delete("1.0", "end")
            self.sort_in_text.insert("1.0", content)
        except Exception as e:
            messagebox.showerror("错误", "读取文件失败: %s" % e)

    def do_sort(self):
        """提取去重并按结尾数字排序"""
        content = self.sort_in_text.get("1.0", "end")
        unique = extract_and_dedup(content)
        # 按结尾数字排序
        unique.sort(key=url_tail_number, reverse=self.sort_desc_var.get())
        self.sort_out_text.delete("1.0", "end")
        self.sort_out_text.insert("1.0", "\n".join(unique))
        self.sort_count_var.set("有效链接: %d 条" % len(unique))
        self._log("链接整理：提取去重后 %d 条，已按结尾数字%s排序" % (
            len(unique), "降序" if self.sort_desc_var.get() else "升序"), "ok")

    def sort_copy_result(self):
        result = self.sort_out_text.get("1.0", "end").strip()
        if not result:
            messagebox.showinfo("提示", "结果为空，请先执行提取排序。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(result)
        self._log("结果已复制到剪贴板", "ok")

    def sort_save_file(self):
        result = self.sort_out_text.get("1.0", "end").strip()
        if not result:
            messagebox.showinfo("提示", "结果为空，请先执行提取排序。")
            return
        path = filedialog.asksaveasfilename(
            title="保存结果",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt")],
            initialdir=BASE_DIR,
            initialfile="链接_排序结果.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(result + "\n")
            self._log("已保存到: %s" % path, "ok")
        except Exception as e:
            messagebox.showerror("错误", "保存失败: %s" % e)

    def sort_send_to_download(self):
        result = self.sort_out_text.get("1.0", "end").strip()
        if not result:
            messagebox.showinfo("提示", "结果为空，请先执行提取排序。")
            return
        self.link_text.delete("1.0", "end")
        self.link_text.insert("1.0", result)
        self.nb.select(self.tab_download)
        self.extract_links()
        self._log("已发送到下载 Tab 并完成提取去重", "ok")

    # ---------- 下载 Tab 功能 ----------
    def toggle_video_filter(self):
        self.video_ext_entry.configure(state="normal" if self.video_only_var.get() else "disabled")

    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or BASE_DIR)
        if d:
            self.dir_var.set(d)

    def browse_aria2(self):
        d = filedialog.askopenfilename(
            title="选择 aria2c.exe",
            filetypes=[("aria2c", "aria2c.exe"), ("所有文件", "*.*")],
            initialdir=os.path.join(BASE_DIR, "aria2"))
        if d:
            self.aria2_path_var.set(d)

    def find_aria2c(self):
        """返回 aria2c.exe 完整路径，找不到返回 None"""
        p = self.aria2_path_var.get().strip()
        if not p:
            p = os.path.join(BASE_DIR, "aria2", "aria2c.exe")
        if p and os.path.isfile(p):
            return p
        return None

    def browse_desktop(self):
        d = filedialog.askdirectory(initialdir=self.desktop_var.get() or BASE_DIR)
        if d:
            self.desktop_var.set(d)

    def open_dir(self):
        d = self.dir_var.get()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo("提示", "目录不存在：" + d)

    def load_from_file(self):
        path = filedialog.askopenfilename(
            title="选择链接文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialdir=BASE_DIR)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.link_text.delete("1.0", "end")
            self.link_text.insert("1.0", content)
            self.extract_links()
        except Exception as e:
            messagebox.showerror("错误", "读取文件失败: %s" % e)

    def extract_links(self):
        content = self.link_text.get("1.0", "end")
        unique = extract_and_dedup(content)
        self.link_text.delete("1.0", "end")
        self.link_text.insert("1.0", "\n".join(unique))
        self.link_count_var.set("有效链接: %d 条" % len(unique))
        self._log("提取完成，去重后共 %d 条链接" % len(unique), "ok")
        self._save_persisted_links()
        return unique

    def _log(self, msg, tag="info"):
        """写一条日志（只后台记录到文件，不在界面显示）"""
        self.out_q.put((msg + "\n", tag))

    def _poll_output(self):
        try:
            while True:
                msg, tag = self.out_q.get_nowait()
                if isinstance(tag, tuple):      # 进度更新消息
                    kind, gid, txt = tag
                    if gid == "__init__":
                        self._prog_init(txt)
                    elif gid == "__finish__":
                        self._prog_finish()
                    else:
                        self._prog_update(gid, txt)
                    continue
                self._render_log_line(msg, tag)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_output)

    def _render_log_line(self, msg, tag):
        """日志只写入后台文件；界面不显示，仅提取少量关键信息做提示"""
        raw = msg.rstrip("\n")
        clean = strip_ansi(raw)
        if not clean:
            return

        # aria2 自身打印的进度行/摘要块：进度区已负责，丢弃
        if strip_dup_prog(clean):
            return
        if self._is_aria2_noise(clean):
            return

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write("[%s] %s\n" % (ts, clean))
            except Exception:
                pass

        # 关键信息同步到底部提示栏（不铺日志）
        if tag in ("warn", "err"):
            self.hint_var.set("%s %s" % (ts, clean[:80]))

    def _is_aria2_noise(self, line):
        """过滤 aria2 的无意义噪音：
        - "Failed to write into the segment file" 是 1.37.0 Windows 上的
          无害告警（控制文件收尾竞争），不影响下载结果
        - "Exception caught" / 退出码 16 之类的重试提示，同理无害
        - 结果汇总表的分隔线/表头
        """
        if "Failed to write into the segment file" in line:
            return True
        if "Exception caught" in line:
            return True
        if line.startswith("Download Results") or line.startswith("Status Legend"):
            return True
        if line.startswith("======+") or line.startswith("(OK):") or line.startswith("(ERR):"):
            return True
        if re.match(r"^gid\s*\|stat\|avg speed", line):
            return True
        if re.match(r"^[0-9a-f]{6}\|(OK|ERR)\s*\|", line):
            return True
        return False

    # ---------- 前台下载进度列表 ----------

    def _dl_clear(self):
        """清空下载列表，重置总体进度"""
        for iid in self.dl_tree.get_children():
            self.dl_tree.delete(iid)
        self._dl_iids = {}          # 显示名 -> 行 iid
        self._dl_meta = {}          # 显示名 -> {total, done, status}
        self._skipped_existing = []  # 本次被自动跳过的已存在文件（显示为已完成）
        self.overall_var.set(0.0)
        self.overall_text_var.set("准备中...")

    def _prog_init(self, payload):
        """payload: (gid_names, sizes, skipped)

        gid_names: {gid: 显示名}，sizes: {显示名: 字节数}，
        skipped: 已存在被自动跳过的文件名列表（直接标注为已完成）。
        行的排列顺序按链接原顺序（_ordered_names），不再跟随 aria2 的
        active/waiting 返回顺序，也不把已存在的行堆到末尾。
        """
        gid_names, sizes, skipped = {}, {}, []
        if isinstance(payload, tuple):
            if len(payload) > 0:
                gid_names = payload[0]
            if len(payload) > 1:
                sizes = payload[1]
            if len(payload) > 2:
                skipped = payload[2]
        self._prog_gids = list(gid_names.keys())
        self._prog_names = dict(gid_names)
        self._prog_marks = {}
        self._dl_clear()

        skipped_set = set(skipped)
        # 用链接原顺序作为行的排列依据：_ordered_names 含本次全部文件（待下载 + 已跳过）。
        # gid_names 已由 _start_progress 按同一顺序排好，这里做一次合并，
        # 落在 _ordered_names 之外的名字（理论上没有）追加在末尾。
        gid_of_name = {}
        for gid in self._prog_gids:
            gid_of_name.setdefault(gid_names[gid], gid)
        seq = []
        seen = set()
        for name in getattr(self, "_ordered_names", []):
            if name in gid_of_name or name in skipped_set:
                seq.append(name)
                seen.add(name)
        for name in list(gid_of_name.keys()) + list(skipped):
            if name not in seen:
                seq.append(name)
                seen.add(name)

        for name in seq:
            total = int(sizes.get(name, 0) or 0)
            gid = gid_of_name.get(name)
            if gid is None:
                # 已存在、本次不重新下载：就地标为已完成，不挪到底部
                size_txt = "-" if total <= 0 else "%s / %s" % (fmt_size(total), fmt_size(total))
                iid = self.dl_tree.insert("", "end", text=name,
                                          values=(make_bar(100.0), "100.0%", size_txt, "-", "-", "已完成"),
                                          tags=("done",))
                self._dl_iids[name] = iid
                self._dl_meta[name] = {"total": total or 1, "done": total or 1, "status": "已完成"}
                continue
            size_txt = "-" if total <= 0 else "%s / %s" % (fmt_size(0), fmt_size(total))
            iid = self.dl_tree.insert("", "end", text=name,
                                      values=("", "0.0%", size_txt, "-", "-", "等待中"),
                                      tags=("wait",))
            self._dl_iids[name] = iid
            self._dl_meta[name] = {"total": total, "done": 0, "status": "等待中"}
            self._prog_marks[gid] = "等待中"
        total_files = len(gid_names) + len(skipped)
        known = sum(1 for m in self._dl_meta.values() if m["total"] > 0)
        if known:
            self.overall_text_var.set("已发现 %d 个文件，已知总量 %s" % (
                total_files, fmt_size(sum(m["total"] for m in self._dl_meta.values()))))
        else:
            self.overall_text_var.set("已发现 %d 个文件" % total_files)
        self._update_overall()

    def _prog_update(self, key, text):
        """更新某个任务的进度行

        key 为 aria2 的 gid 或 tdl 模式下的文件名；
        text 为 (百分数, 大小显示, 速度, 剩余, 状态)。
        """
        name = self._prog_names.get(key, key)
        iid = self._dl_iids.get(name)
        if not iid or not self.dl_tree.exists(iid):
            return

        pct, size_txt, speed, eta, status = text
        if self._prog_marks.get(key) == text:
            return
        self._prog_marks[key] = text

        bar = make_bar(pct)
        tags = ()
        if status == "已完成":
            tags = ("done",)
            # 完成一个就立刻转正名，不等全部结束（否则一直显示 .part 分不清）
            # 带上 aria2 回报的总大小，让 _try_finalize 能校验 .part 是否真的完整
            expect = 0
            if isinstance(size_txt, tuple) and len(size_txt) == 2:
                expect = int(size_txt[1] or 0)
            self._try_finalize(name, expect)
        elif status == "出错":
            tags = ("error",)
        else:
            tags = ("active",)

        self.dl_tree.item(iid, values=(bar, "%.1f%%" % pct, size_txt,
                                       speed, eta, status), tags=tags)
        # 更新元数据用于总体进度（tdl 模式只能按百分比估算）
        meta = self._dl_meta.setdefault(name, {"total": 0, "done": 0, "status": ""})
        meta["status"] = status
        if isinstance(size_txt, tuple):
            meta["done"], meta["total"] = size_txt
        else:
            meta["total"] = meta["total"] or 100.0
            meta["done"] = meta["total"] * pct / 100.0
        meta["speed"] = _parse_speed(speed)
        self._update_overall()

    def _update_overall(self):
        """按所有文件的总字节数计算总体进度，并预估整体剩余时间"""
        tot = sum(m["total"] for m in self._dl_meta.values())
        done = sum(m["done"] for m in self._dl_meta.values())
        if tot > 0:
            pct = done * 100.0 / tot
            self.overall_var.set(pct)
        n_done = sum(1 for m in self._dl_meta.values() if m["status"] == "已完成")
        n_err = sum(1 for m in self._dl_meta.values() if m["status"] == "出错")
        n_all = len(self._dl_meta)
        # 剩余时间：已知总量时按「(总量-已下载) / 当前合计速度」估算
        eta_txt = ""
        speed_sum = sum(m.get("speed", 0) for m in self._dl_meta.values())
        if tot > done and speed_sum > 0:
            eta_txt = "  剩余约 %s" % fmt_eta((tot - done) / speed_sum)
        self.overall_text_var.set(
            "%d/%d 完成%s  总计 %s  %s%s" % (
                n_done, n_all,
                ("，%d 个出错" % n_err) if n_err else "",
                fmt_size(tot), ("%.1f%%" % self.overall_var.get()) if tot else "",
                eta_txt))

    def _prog_finish(self):
        """下载结束，收尾进度区：未完成的标记为已停止，并汇总"""
        for name, meta in self._dl_meta.items():
            if meta.get("status") in ("等待中", "下载中"):
                meta["status"] = "已停止"
                iid = self._dl_iids.get(name)
                if iid and self.dl_tree.exists(iid):
                    vals = list(self.dl_tree.item(iid, "values"))
                    vals[5] = "已停止"
                    self.dl_tree.item(iid, values=vals)
        self._prog_gids = []
        self._update_overall()

    def _filter_existing(self, final_items, out_dir):
        """剔除已下载过的文件：正式文件已存在即自动跳过（不弹窗）

        .part/.aria2 残留是上次中断留下的，不能让它导致已存在的
        完整文件被当作新任务重新下载。
        返回过滤后的任务列表。
        """
        existing, fresh = [], []
        for final, url, size in final_items:
            path = os.path.join(out_dir, final)
            # 正式文件已存在即视为已下载过：残留的 .part 是上次中断留下的，
            # 不能因为它在就让已存在的完整文件被当作新任务重新下载
            if os.path.isfile(path):
                existing.append(final)
            else:
                fresh.append((final, url, size))
        # 记下完整的有序文件名（含已跳过的），进度列表要按链接原顺序排列
        self._ordered_names = [f for f, _, _ in final_items]
        self._skipped_existing = existing
        if existing:
            self._log("自动跳过 %d 个已存在的文件（如需强制重新下载，请先删除原文件）"
                      % len(existing), "warn")
        return fresh

    def _start_progress(self, rpc_port, finals, out_dir, sizes=None):
        """等 aria2 RPC 就绪 → 建立每个任务的进度行 → 启动后台查询线程

        sizes: {文件名: 字节数}，来自 HEAD 的 Content-Length。
        有了它，进度列表一开始就能显示总量并给出总体剩余时间，
        不必等 aria2 回报 totalLength。
        """
        sizes = sizes or {}
        gid_names = {}
        deadline = time.time() + 20
        # 目标文件名 -> 显示名（用 basename 匹配 aria2 返回的 files[].path）
        want = {}
        for f in finals:
            want[os.path.normcase(os.path.join(out_dir, f + ".part"))] = f
        while time.time() < deadline:
            try:
                items = (self._rpc_call(rpc_port, "aria2.tellActive", [])
                         + self._rpc_call(rpc_port, "aria2.tellWaiting", [0, 500]))
            except Exception:
                items = []
            for it in items:
                gid = it.get("gid", "")
                path = ""
                for fl in it.get("files", []):
                    path = fl.get("path", "") or path
                key = os.path.normcase(os.path.abspath(path)) if path else ""
                name = want.get(key) or (os.path.basename(path) if path else gid)
                name = re.sub(r"\.part$", "", name)
                gid_names[gid] = name
            if len(gid_names) >= len(finals):
                break
            time.sleep(0.5)

        if not gid_names:
            if self._skipped_existing:
                self.out_q.put((None, ("prog", "__init__", ({}, {}, self._skipped_existing))))
            else:
                self._log("未能从 aria2 获取任务列表，进度条不可用（下载不受影响）", "warn")
            return
        # 按 finals（即链接原顺序）重排 gid。aria2 返回的顺序是
        # tellActive（下载中）+ tellWaiting（排队），同一时刻的下载中任务
        # 会整体排在排队任务前面，导致列表看着忽上忽下、和链接顺序无关。
        # 这里以 finals 的顺序为准重建 gid_names。
        rank = {f: i for i, f in enumerate(finals)}
        ordered_gids = sorted(gid_names.keys(),
                              key=lambda g: (rank.get(gid_names[g], len(rank)), gid_names[g]))
        gid_names = {g: gid_names[g] for g in ordered_gids}
        self._log("开始实时进度显示（%d 个任务）" % len(gid_names), "info")
        self.out_q.put((None, ("prog", "__init__", (gid_names, sizes, self._skipped_existing))))
        self._rpc_stop = False
        threading.Thread(target=self._rpc_progress_loop, args=(rpc_port,), daemon=True).start()

    def _stop_progress(self):
        """停止进度查询线程"""
        self._rpc_stop = True

    def _rpc_call(self, port, method, params):
        """调用 aria2 JSON-RPC（无 secret）"""
        payload = json.dumps({
            "jsonrpc": "2.0", "id": "gui", "method": method, "params": params,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/jsonrpc" % port,
            data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("result", [])

    def _rpc_progress_loop(self, port):
        """后台线程：每秒查询 aria2 任务进度，按 GID 更新对应进度行"""
        # 上次看到的各任务已完成字节数，用于计算这一秒真实新增的流量
        seen = {}
        while not self._rpc_stop:
            try:
                active = self._rpc_call(port, "aria2.tellActive", [])
                waiting = self._rpc_call(port, "aria2.tellWaiting", [0, 100])
                stopped = self._rpc_call(port, "aria2.tellStopped", [0, 100])
            except Exception:
                time.sleep(1)
                continue

            # 累计新增流量（跨下载器换源会重置 completedLength，
            # 所以只在「增加」时计入，回落部分忽略，避免负数）
            for item in (active + waiting + stopped):
                gid = item.get("gid", "")
                done = int(item.get("completedLength", 0) or 0)
                prev = seen.get(gid, 0)
                if done > prev:
                    if self.quota_add(done - prev):
                        self.out_q.put((None, ("quota", None)))
                        self._rpc_stop = True
                        break
                seen[gid] = done
            if self._rpc_stop:
                break

            for item in (active + waiting):
                gid = item.get("gid", "")
                total = int(item.get("totalLength", 0) or 0)
                done = int(item.get("completedLength", 0) or 0)
                speed = int(item.get("downloadSpeed", 0) or 0)
                if total <= 0:
                    continue
                pct = done * 100.0 / total
                eta = fmt_eta((total - done) / speed) if speed > 0 else "-"
                info = (pct,
                        (done, total),
                        "%s/s" % human_size(speed) if speed else "-",
                        eta or "-",
                        "下载中")
                self.out_q.put((None, ("prog", gid, info)))

            for item in stopped:
                gid = item.get("gid", "")
                total = int(item.get("totalLength", 0) or 0)
                st = item.get("status")
                if st == "complete":
                    info = (100.0, (total, total), "-", "-", "已完成")
                elif st == "error":
                    done = int(item.get("completedLength", 0) or 0)
                    info = (0.0, (done, total), "-", "-", "出错")
                else:
                    continue
                self.out_q.put((None, ("prog", gid, info)))

            time.sleep(1)

    def open_log_dir(self):
        if os.path.isdir(LOG_DIR):
            os.startfile(LOG_DIR)
        else:
            os.makedirs(LOG_DIR, exist_ok=True)
            os.startfile(LOG_DIR)

    def start_download(self):
        if not is_logged_in():
            if not messagebox.askyesno("未登录", "检测到尚未登录 tdl，下载可能失败。是否继续？\n（建议先点顶部「登录」按钮）"):
                return

        # 流量限额：本月已用尽则直接拦下，避免白跑一趟
        self._quota = load_quota()
        self.refresh_quota()
        if self._quota_tripped():
            limit = max(1, int(self.quota_gb_var.get() or 1))
            messagebox.showwarning(
                "流量已达限额",
                "本月已用流量 %s，已达上限 %d GB。\n\n"
                "可在「下载设置」Tab 中提高限额或重置本月用量后重试。" % (
                    human_size(self._quota["used"]), limit))
            return

        links = self.extract_links()
        if not links:
            messagebox.showwarning("提示", "没有有效的 t.me 链接，请先粘贴链接。")
            return
        if not os.path.isfile(TDL_EXE):
            messagebox.showerror("错误", "找不到 tdl.exe: %s" % TDL_EXE)
            return

        if not self.find_aria2c():
            messagebox.showerror("错误",
                "未找到 aria2c.exe。\n请将 aria2c.exe 放到 %s 目录，\n或在「aria2c 路径」框中指定。" %
                os.path.join(BASE_DIR, "aria2"))
            return

        out_dir = self.dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("提示", "请设置保存目录。")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("错误", "创建目录失败: %s" % e)
            return

        ns = self.ns_var.get().strip() or "default"
        args = [TDL_EXE, "dl",
                "-n", ns,
                "-d", out_dir,
                "-t", str(int(self.threads_var.get())),
                "-l", str(int(self.limit_var.get()))]
        if self.takeout_var.get():
            args += ["--takeout"]
        if self.group_var.get():
            args += ["--group"]
        proxy = self.proxy_var.get().strip()
        if proxy:
            args += ["--proxy", proxy]
        if self.video_only_var.get():
            exts = self.video_ext_var.get().strip()
            if exts:
                args += ["-i", exts]
        for u in links:
            args += ["-u", u]

        self.running = True
        self.stop_requested = False
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("下载中... %d 个链接" % len(links))
        self._dl_clear()
        self.hint_var.set("")
        # 清空本次会话流量基线与 RPC 停止标志（上次可能因流量超限被置位）
        self._quota_session_base = self._quota["used"]
        self._rpc_stop = False
        # 清理上次残留的 tdl/aria2c（它们会占着数据库锁导致本次必失败）
        left = kill_leftover_processes()
        if left:
            self.hint_var.set("已清理残留进程: %s" % ", ".join(left))
        if self.log_file:
            self.log_path_var.set("本次日志文件: %s" % self.log_file)
        self._log("=" * 60, "info")
        self._log("开始下载: %d 个链接" % len(links), "info")
        self._log("保存到: %s" % out_dir, "info")
        if self.log_file:
            self._log("日志文件: %s" % self.log_file, "info")
        self._log("下载方式: aria2c 真断点续传（tdl --serve + aria2c，网络中断后重跑可续传）", "ok")
        if proxy:
            self._log("代理: %s" % proxy, "info")
        self._log("=" * 60, "info")

        threading.Thread(target=self._run_aria2_process, args=(args, out_dir, links), daemon=True).start()

    def _snapshot_files(self, out_dir):
        """快照输出目录当前文件集合（相对路径）

        排除 aria2 的临时/控制文件：它们是下载中间产物，
        记进历史清单会产出 `xxx.mp4.part.aria2__temp` 这类脏记录，
        也会让「导入文件夹」识别不到真实文件名。
        """
        res = set()
        try:
            if os.path.isdir(out_dir):
                for root, _, files in os.walk(out_dir):
                    for f in files:
                        low = f.lower()
                        if low.endswith(".aria2") or low.endswith(".aria2__temp"):
                            continue
                        res.add(os.path.relpath(os.path.join(root, f), out_dir))
        except Exception:
            pass
        return res

    def _record_history(self, links, mode, status, new_files, out_dir=""):
        """记录一次下载会话，并把其中的文件录入平铺清单"""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_history({
            "time": ts,
            "links": links,
            "mode": mode,
            "status": status,
            "files": sorted(new_files),
        })

        # 录入平铺文件清单：用 群ID_消息ID 作为唯一键
        # 链接形如 http://127.0.0.1:port/1710039486/5793，可推出 ID
        url_by_id = {}
        for u in links:
            m = re.search(r"/(\d{6,})/(\d{1,})/?$", u.strip())
            if m:
                url_by_id["%s_%s" % (m.group(1), m.group(2))] = u.strip()

        items = []
        for fn in sorted(new_files):
            fid = extract_file_id(fn)
            full = os.path.join(out_dir, fn) if out_dir else ""
            # 未下完的 .part 不记成「已下载」，否则会覆盖掉同一文件已有的正确记录
            st = "未完成" if fn.lower().endswith(".part") else "已下载"
            size = 0
            if full and os.path.isfile(full):
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = 0
            items.append((fid, fn, size, st, ts, full, url_by_id.get(fid, "")))
        if items:
            up_items = [(fid, name, size, st, t) for fid, name, size, st, t, _, _ in items]
            add, upd = upsert_file_records(up_items)
            # 补写 URL 与本地路径（供「重新下载」「定位文件」使用）
            records = load_file_records()
            for fid, name, _, _, _, full, url in items:
                key = fid or ("__" + name)
                rec = records.get(key)
                if rec:
                    if url:
                        rec["url"] = url
                    if full and os.path.isfile(full):
                        rec["path"] = full
            save_file_records(records)
            self._log("历史清单：新增 %d，更新 %d" % (add, upd), "info")

    def _try_finalize(self, final, expect_size=0):
        """单个文件下载完成就立刻把 .part 改回正式名

        返回 (是否已结束, 是否改名成功)：
          - 已结束 = 该文件要么已转正，要么本次确实下完了
          - 没下完的文件返回 (False, False)，上层不会再重试它

        判断「下完」不能只看 .aria2 控制文件：aria2c 被强杀时会先删掉
        自己的控制文件再退出，只下了一半的 .part 也会变成「无 .aria2」，
        从而被误判成下完。所以这里以 HEAD 拿到的总大小为准做校验。

        注意：expect_size 来自下载前 HEAD 的 Content-Length。Telegram 的
        分片/多版本文件实际落盘大小可能与它不符，所以大小对不上只当作
        「没下完」处理，不能据此判断改名失败。
        """
        out_dir = self.dir_var.get().strip()
        if not out_dir:
            return True, False
        tmp = os.path.join(out_dir, final + ".part")
        target = os.path.join(out_dir, final)
        if not os.path.isfile(tmp):
            # 没有 .part：要么已转正，要么本次根本没这个任务。
            # 这里必须用 _exists_final 做宽松匹配：finals 里可能是未展开的
            # 分片模板名（如 xxx%01d1.mp4），而落盘的是展开后的真实文件名，
            # 直接用 os.path.isfile(target) 会匹配不上，把「已改名成功」误判成
            # 「改名失败」，进而报出「文件仍被占用」的假错误。
            return True, self._exists_final(out_dir, final)
        if os.path.isfile(tmp + ".aria2"):
            return False, False                  # 明确还在下，保留 .part
        if expect_size > 0:
            try:
                if os.path.getsize(tmp) != expect_size:
                    return False, False          # 大小对不上，没下完，保留 .part
            except OSError:
                return False, False
        try:
            # 同名正式文件存在时覆盖：能走到这里说明该任务确实下完了
            os.replace(tmp, target)
            self._log("下载完成: %s" % final, "ok")
            return True, True
        except Exception as e:
            # 到这里才是真的被占用（残留进程句柄未释放），交给上层重试
            self._log("重命名失败 %s: %s" % (final, e), "warn")
            return True, False

    @staticmethod
    def _exists_final(out_dir, final):
        """判断某个任务名对应的正式文件是否已存在

        优先精确匹配；匹配不到再用通配兜底，处理 finals 里带 printf 占位符
        （%d / %01d / %s）而落盘名字已展开的情况。通配只在名字含 % 时启用，
        避免每次都做一次目录扫描。
        """
        if os.path.isfile(os.path.join(out_dir, final)):
            return True
        if "%" not in final:
            return False
        try:
            stem = os.path.basename(final).replace("%01d", "*").replace("%d", "*") \
                .replace("%s", "*")
            return bool(glob.glob(os.path.join(out_dir, stem)))
        except Exception:
            return False

    def _rename_completed(self, out_dir, finals, sizes=None):
        """收尾：把所有已下完的 .part 改回正式文件名（兜底，正常已在完成时改过）

        只有 os.replace 真的抛异常（文件被占用）才重试并报错；
        未下完的文件直接跳过，不报错——它们保留 .part 是预期行为，下次续传。

        返回值 (改名成功数, 仍未完成数, 真失败列表)，供上层汇总说明。
        """
        sizes = sizes or {}
        prev = self.dir_var.get()
        try:
            self.dir_var.set(out_dir)
            done = 0
            unfinished = 0
            failed = []
            for attempt in range(5):
                failed = []
                for final in finals:
                    finished, ok = self._try_finalize(final, sizes.get(final, 0))
                    if finished and ok:
                        done += 1
                    elif finished and not ok:
                        # 只有「已结束但改名没成」才值得重试
                        failed.append(final)
                    else:
                        unfinished += 1
                if not failed:
                    return done, unfinished, []
                # 句柄释放需要一点时间，等一下再试
                time.sleep(0.5 * (attempt + 1))
            for final in failed:
                self._log("改名失败（文件被占用），请手动把 .part 去掉: %s.part" % final, "err")
            return done, unfinished, failed
        finally:
            self.dir_var.set(prev)

    def _kill_and_finalize(self, out_dir, finals, sizes=None):
        """收尾加固：先确保 tdl/aria2c 全部退出，再统一转正名

        残留进程会一直占着文件的句柄，导致 os.replace 改名失败
        （表现为「下完了却还是 .part」）。所以这里先等自己的子进程退出，
        再清一次残留进程，确认没有进程占用了才改名。

        sizes: {文件名: 字节数}，用来判断 .part 是否真的下完（见 _try_finalize）。
        """
        proc = self.process
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        left = kill_leftover_processes()
        if left:
            self._log("收尾：清理残留进程 %s" % ", ".join(left), "warn")
            self._wait_no_leftover()
        done, unfinished, failed = self._rename_completed(out_dir, finals, sizes)
        if done:
            self._log("收尾：%d 个文件已转正名" % done, "ok")
        if unfinished:
            # 没下完保留 .part 是预期行为（serve 断连/手动停止/流量超限），
            # 不是错误，重跑即可续传，所以这里只提示不报错
            self._log("收尾：%d 个文件未下完，保留 .part，重跑可续传" % unfinished, "info")
        self._sweep_orphan_parts(out_dir, finals)

    @staticmethod
    def _wait_no_leftover(timeout=8.0):
        """等本目录下的 tdl/aria2c 全部退出，让它们释放文件句柄

        taskkill 返回不代表进程已经死透、句柄已经释放；
        直接改名会撞上「文件被占用」，所以这里轮询确认。
        """
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "(Get-CimInstance Win32_Process -Filter "
              "\"Name='tdl.exe' or Name='aria2c.exe'\" | "
              "Where-Object { $_.ExecutablePath -like '%s*' }).Count")
        base = os.path.normcase(BASE_DIR).replace("'", "''")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     ps % base.replace("\\", "\\\\")],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=10).stdout or ""
                if out.strip() in ("", "0"):
                    return True
            except Exception:
                return True
            time.sleep(0.5)
        return False

    def _sweep_orphan_parts(self, out_dir, finals):
        """清掉本次任务对应的 .part.aria2 / .aria2__temp 残留控制文件

        正常下完时 aria2 会自己删掉它们；异常退出（被杀、断连）会留下，
        体积很小但会干扰「导入文件夹」识别，这里顺手清掉。
        """
        removed = 0
        for final in finals:
            for suffix in (".part.aria2", ".part.aria2__temp"):
                p = os.path.join(out_dir, final + suffix)
                if not os.path.isfile(p):
                    continue
                try:
                    os.remove(p)
                    removed += 1
                except Exception:
                    pass
        if removed:
            self._log("清理残留控制文件 %d 个" % removed, "info")

    def _on_download_done(self):
        self._prog_finish()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("就绪")
        self.refresh_history()

    # ---------- aria2 真断点续传模式 ----------
    def _http_open(self, url, method=None, timeout=3):
        """直连本机 HTTP（不走代理），返回响应对象"""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, method=method)
        return opener.open(req, timeout=timeout)

    def _http_get(self, url, timeout=3):
        with self._http_open(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")

    def _http_head(self, url, timeout=5):
        """HEAD 取 (文件名, 文件字节数)

        文件名来自 Content-Disposition（urllib 返回的 header 是 latin-1，
        需要转回 UTF-8 原始字节再解码，否则中文名会变成乱码/控制字符）；
        大小来自 Content-Length，用于计算总体进度与预估剩余时间。
        """
        with self._http_open(url, method="HEAD", timeout=timeout) as r:
            cd = r.headers.get("Content-Disposition", "")
            clen = r.headers.get("Content-Length", "")
        name = ""
        m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            try:
                name = m.group(1).encode("latin-1", "ignore").decode("utf-8", "replace")
            except Exception:
                name = m.group(1)
        try:
            size = int(clen)
        except Exception:
            size = 0
        return name, size

    def _stop_serve(self):
        sp = self.serve_process
        self.serve_process = None
        kill_tree(sp)
        if sp is not None:
            # taskkill 返回 ≠ 进程已退出、文件句柄已释放。
            # 不等它死透，后面的 .part 改名会撞上「文件被占用」。
            try:
                sp.wait(timeout=8)
            except Exception:
                pass

    def _reset_after_finish(self):
        self.process = None
        self.serve_process = None
        self.running = False
        self.root.after(0, self._on_download_done)

    def _drain_serve_output(self, proc):
        """后台读取 tdl serve 的输出，留作启动失败时的原因说明"""
        try:
            for line in proc.stdout:
                line = strip_ansi(line.rstrip("\r\n"))
                if line.strip():
                    self._serve_output = line.strip()
        except Exception:
            pass

    def _run_aria2_process(self, args, out_dir, links):
        """aria2 模式：先启动 tdl --serve，再让 aria2c 下载（真断点续传）"""
        before = self._snapshot_files(out_dir)
        status = "未知"
        try:
            aria2c = self.find_aria2c()
            port = find_free_port()
            serve_args = list(args) + ["--serve", "--port", str(port)]
            self._log("启动 tdl serve 模式（端口 %d），等待文件列表..." % port, "info")
            self._serve_output = ""
            try:
                self.serve_process = subprocess.Popen(
                    serve_args,
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    bufsize=1,
                )
            except Exception as e:
                self._log("启动 tdl serve 失败: %s" % e, "err")
                self.hint_var.set("启动 tdl serve 失败: %s" % e)
                status = "启动失败"
                return
            # 后台收集 serve 的输出：它一启动就退出时，这里就是失败原因
            threading.Thread(target=self._drain_serve_output,
                             args=(self.serve_process,), daemon=True).start()

            base = "http://127.0.0.1:%d" % port
            items = None
            deadline = time.time() + 300
            while time.time() < deadline:
                sp = self.serve_process
                if sp is None:  # 用户点了停止
                    status = "已停止"
                    return
                if sp.poll() is not None:
                    why = self._serve_output or "(无输出)"
                    self._log("tdl serve 提前退出（退出码 %s）：%s" % (sp.returncode, why), "err")
                    self.hint_var.set("启动失败: %s" % why[:100])
                    status = "启动失败"
                    self._stop_serve()
                    return
                try:
                    page = self._http_get(base + "/", timeout=3)
                    items = re.findall(r'href="(\d+/\d+)"', page)
                    if items:
                        break
                except Exception:
                    pass
                time.sleep(1)
            if not items:
                self._log("等待 tdl serve 文件列表超时（300 秒）", "err")
                status = "超时"
                self._stop_serve()
                return

            # 文件名与大小：优先用本地缓存（同一批链接第一次下载时已记录），
            # 只有缓存里没有的才去 HEAD 取，避免每次续传都重新问一遍服务器
            meta_cache = load_meta_cache()
            final_items = []
            cache_hits = cache_miss = 0
            for it in items:
                url = "%s/%s" % (base, it)
                peer, mid = it.split("/", 1)
                fid = "%s_%s" % (peer, mid)
                rec = meta_cache.get(fid) or {}
                cd_name = rec.get("name", "")
                total_size = int(rec.get("size", 0) or 0)
                if cd_name:
                    cache_hits += 1
                else:
                    # 缓存缺失：联网补一次，取到就落缓存，下次直接命中
                    try:
                        cd_name, total_size = self._http_head(url, timeout=5)
                    except Exception:
                        pass
                    if cd_name:
                        cache_miss += 1
                        upsert_meta_cache([(fid, cd_name, total_size)])
                        meta_cache[fid] = {"name": cd_name, "size": total_size}
                prefix = "%s_%s_" % (peer, mid)
                # 给「目录 + 前缀 + .part + .aria2」留够空间，保证总路径 < 260
                room = MAX_PATH_LEN - len(os.path.abspath(out_dir)) - 1 - len(prefix) - 12
                stem = sanitize_filename(cd_name, max_len=max(20, room)) if cd_name else it.replace("/", "_")
                if cd_name:
                    # 截断后保留扩展名，便于播放器识别
                    ext = os.path.splitext(cd_name)[1]
                    if ext and len(ext) < 12 and not stem.lower().endswith(ext.lower()):
                        stem = stem[: max(20, room) - len(ext)] + ext
                final = prefix + stem
                if self.video_only_var.get():
                    allowed = [e.strip().lower().lstrip(".") for e in self.video_ext_var.get().split(",") if e.strip()]
                    ext = os.path.splitext(cd_name)[1].lower().lstrip(".")
                    if cd_name and allowed and ext not in allowed:
                        self._log("跳过非视频: %s" % cd_name, "info")
                        continue
                final_items.append((final, url, total_size))
            self._log("文件名/大小：本地缓存 %d 个，联网获取 %d 个" % (cache_hits, cache_miss),
                      "ok" if cache_miss == 0 else "info")
            if not final_items:
                self._log("没有符合条件（扩展名过滤）的文件", "warn")
                status = "无文件"
                self._stop_serve()
                return

            # 已下载过的文件：自动跳过，不重新下载
            final_items = self._filter_existing(final_items, out_dir)
            if not final_items:
                self._log("所选文件都已下载过，本次无需下载", "ok")
                # 在下载列表里把已存在的文件标注为已完成，而不是直接消失
                if self._skipped_existing:
                    self.out_q.put((None, ("prog", "__init__", ({}, {}, self._skipped_existing))))
                status = "已跳过"
                self._stop_serve()
                return

            self._log("符合条件 %d 个文件，交给 aria2c 下载" % len(final_items), "ok")
            finals = [final for final, _, _ in final_items]
            sizes = {final: size for final, _, size in final_items if size > 0}
            total_known = sum(sizes.values())
            if total_known:
                self._log("已获取文件总大小 %s" % human_size(total_known), "info")
            # 注意：aria2 命令行里的多个 URL 会被当作「同一文件的镜像源」，
            # 下载多个文件必须用 -i 输入文件（每行一个 URL，缩进写该 URL 的选项）
            input_text = ""
            for final, url, _size in final_items:
                input_text += "%s\n  out=%s.part\n" % (url, final)
            rpc_port = find_free_port()
            a2 = [aria2c,
                  "--continue=true",
                  "--split=8", "--max-connection-per-server=8",
                  "--file-allocation=none",
                  "--auto-file-renaming=false",
                  "--allow-overwrite=true",
                  "--max-tries=0", "--retry-wait=5",
                  "--timeout=30", "--connect-timeout=15",
                  "--max-concurrent-downloads=%d" % max(1, int(self.limit_var.get())),
                  "--console-log-level=notice",
                  "--summary-interval=0", "--show-console-readout=true",
                  "--truncate-console-readout=true",
                  "--enable-rpc=true", "--rpc-listen-port=%d" % rpc_port,
                  "--rpc-listen-all=false",
                  "-d", out_dir,
                  "-i", "-"]

            self._log("aria2c 启动（断点续传开启，网络中断后重跑可续传）...", "info")
            try:
                self.process = subprocess.Popen(
                    a2,
                    cwd=BASE_DIR,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    bufsize=1,
                )
                self.process.stdin.write(input_text)
                self.process.stdin.close()
            except Exception as e:
                self._log("启动 aria2c 失败: %s" % e, "err")
                status = "启动失败"
                self._stop_serve()
                return

            # 等 RPC 起来，取到全部任务的 GID，为它们建立进度行
            self._start_progress(rpc_port, finals, out_dir, sizes)

            serve_died = False
            quota_hit = False
            try:
                while True:
                    # 流量到量：RPC 线程已设置 _rpc_stop，这里负责真正掐断
                    if self._rpc_stop:
                        quota_hit = self._quota_tripped()
                        if quota_hit:
                            self._log("已用流量达到本月限额，正在停止下载...", "warn")
                        kill_tree(self.process)
                        break
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    if line.strip():
                        tag = "err" if ("error" in line.lower() or "fail" in line.lower()) else "info"
                        self._log(line, tag)
                    if self.serve_process and self.serve_process.poll() is not None:
                        serve_died = True
                        self._log("tdl serve 连接已断开，正在终止 aria2c...", "err")
                        kill_tree(self.process)
                        break
                code = self.process.wait()
            except Exception as e:
                self._log("aria2c 运行异常: %s" % e, "err")
                code = -1

            if quota_hit:
                self._log("流量已达上限，下载已停止。未完成的文件保留 .part，可在下月或提高限额后续传",
                          "warn")
                status = "流量超限停止"
                self.hint_var.set("⚠ 本月流量已达限额，下载已停止")
            elif serve_died:
                self._log("下载中断：tdl serve 断连（网络/代理问题）。修复后重跑即可续传", "warn")
                status = "网络中断"
            elif self.stop_requested:
                status = "已停止"
            elif code == 0:
                self._log("=" * 60, "ok")
                self._log("aria2c 全部下载完成", "ok")
                status = "成功"
            else:
                self._log("=" * 60, "warn")
                self._log("aria2c 结束，退出码 %s（有任务失败，重跑可续传）" % code, "warn")
                status = "部分失败"
            # 先停掉 tdl serve，再确保所有子进程退出，最后统一转正名
            self._stop_serve()
            self._kill_and_finalize(out_dir, finals, sizes)
            self.process = None
        finally:
            self._stop_progress()
            new_files = self._snapshot_files(out_dir) - before
            self._record_history(links, "aria2", status, new_files, out_dir)
            self._reset_after_finish()

    def stop_download(self):
        self.stop_requested = True
        if self.process and self.running:
            try:
                kill_tree(self.process)
                self._log("已发送停止信号，正在终止下载进程...", "warn")
            except Exception as e:
                self._log("停止失败: %s" % e, "err")
        self._stop_progress()
        self._stop_serve()
        self.status_var.set("已停止")


def main():
    root = tk.Tk()
    TDLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
