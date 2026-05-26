#!/opt/homebrew/bin/python3.12
"""
Steam 代注册 GUI 桌面版
基于 steam_register_auto.py 核心逻辑，tkinter 界面
"""

import re
import json
import time
import imaplib
import email
import logging
import random
import configparser
import threading
import sys
import signal
import queue
from pathlib import Path
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import *
from tkinter import scrolledtext, messagebox, ttk
from PIL import Image, ImageTk

import requests

# ============================================================
# 配置
# ============================================================

BASE_URL = "http://101.33.205.205"
API_QUERY = BASE_URL + "/index.php/UserApiController/web_query"
API_SUBMIT = BASE_URL + "/index.php/UserApiController/web_submit"
API_STOP = BASE_URL + "/index.php/UserApiController/web_stop"

POLL_INTERVAL = 5
MAX_RETRY_ON_DUPLICATE = 3

# ============================================================
# 日志（双输出：Python logging + GUI 队列）
# ============================================================

log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        log_queue.put(self.format(record))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("steam_gui")
log.addHandler(QueueHandler())

# ============================================================
# 工具函数
# ============================================================

def generate_username() -> str:
    chars = "0123456789"
    return "".join(random.choice(chars) for _ in range(8))


def generate_password() -> str:
    lowercase = "a"
    uppercase = "A"
    numbers = "0123456789"
    all_chars = lowercase + uppercase + numbers
    while True:
        pwd = "".join(random.choice(all_chars) for _ in range(8))
        if re.search(r"[A-Z]", pwd) and re.search(r"[a-z]", pwd) and re.search(r"[0-9]", pwd):
            return pwd


_DESKTOP = str(Path.home() / "Desktop")
_RESULT_FILE = _DESKTOP + "/Steam注册成功账号.txt"
_file_lock = threading.Lock()


def save_result(data: dict):
    line = (
        "[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] "
        "卡密: " + data.get("k", "N/A") + " | "
        "Steam账号: " + data.get("username", "N/A") + " | "
        "密码: " + data.get("password", "N/A") + " | "
        "邮箱: " + data.get("mail", "N/A") + " | "
        "地区: " + data.get("country", "N/A")
    )
    with _file_lock:
        with open(_RESULT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def extract_steam_link_from_email(raw_email: str) -> Optional[str]:
    pattern = r"https://store\.steampowered\.com/account/newaccountverification\?stoken=[^&\s]+&creationid=[^&\s]+"
    match = re.search(pattern, raw_email)
    return match.group(0) if match else None


# ============================================================
# 邮箱获取模块
# ============================================================

class EmailFetcher:
    def __init__(self, imap_server: str, email_addr: str, password: str, port: int = 993):
        self.imap_server = imap_server
        self.email_addr = email_addr
        self.password = password
        self.port = port

    def fetch_latest_steam_link(self, wait_max: int = 180, poll_interval: int = 10) -> Optional[str]:
        log.info("📧 轮询邮箱 %s，等 Steam 验证邮件（最多等 %d 秒）...", self.email_addr, wait_max)
        deadline = time.time() + wait_max
        while time.time() < deadline:
            if _stop_event.is_set():
                return None
            try:
                if self.port == 993 or self.port == 0:
                    conn = imaplib.IMAP4_SSL(self.imap_server, self.port if self.port != 0 else 993)
                else:
                    conn = imaplib.IMAP4(self.imap_server, self.port)
                conn.login(self.email_addr, self.password)
                conn.select("INBOX")
                status, messages = conn.search(None, "UNSEEN")
                if status == "OK":
                    for mid in reversed(messages[0].split()[-15:]):
                        status, msg_data = conn.fetch(mid, "(RFC822)")
                        if status != "OK":
                            continue
                        raw_bytes = msg_data[0][1]
                        raw = raw_bytes.decode("utf-8", errors="ignore")
                        link = extract_steam_link_from_email(raw)
                        if link:
                            conn.logout()
                            return link
                        msg = email.message_from_bytes(raw_bytes)
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() in ("text/html", "text/plain"):
                                    try:
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            link = extract_steam_link_from_email(
                                                payload.decode("utf-8", errors="ignore"))
                                            if link:
                                                conn.logout()
                                                return link
                                    except Exception:
                                        continue
                conn.logout()
            except imaplib.IMAP4.abort:
                time.sleep(3)
            except Exception as e:
                log.warning("邮箱检查异常: %s", e)
            log.info("⏳ 还没收到验证邮件，%ds 后重试...", poll_interval)
            time.sleep(poll_interval)
        log.error("❌ 超时，未收到 Steam 验证邮件")
        return None


# ============================================================
# 全局状态
# ============================================================

_stop_event = threading.Event()
_manual_link_queue = queue.Queue()  # 手动输入验证链接


# ============================================================
# 核心注册类
# ============================================================

class SteamAutoRegister:
    def __init__(self, card_key: str, email: str, country: str = "CN",
                 email_fetcher: Optional[EmailFetcher] = None,
                 preset_username: str = "", preset_password: str = ""):
        self.card_key = card_key
        self.email = email
        self.country = country
        self.email_fetcher = email_fetcher
        self.username = preset_username
        self.password = preset_password
        self._pending_link = None  # 供 GUI 手动填入验证链接

    def _query_card(self) -> dict:
        resp = requests.post(API_QUERY, data={"k": self.card_key}, timeout=15)
        return resp.json()

    def _submit(self, emailurl: str = "") -> dict:
        data = {
            "username": self.username,
            "password": self.password,
            "email": self.email,
            "country": self.country,
            "k": self.card_key,
        }
        if emailurl:
            data["emailurl"] = emailurl
        resp = requests.post(API_SUBMIT, data=data, timeout=15)
        return resp.json()

    def _stop(self):
        try:
            requests.post(API_STOP, data={"k": self.card_key}, timeout=10)
        except Exception:
            pass

    def _poll(self, targets: set, timeout: int = 600) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _stop_event.is_set():
                raise RuntimeError("用户终止")
            result = self._query_card()
            if result.get("code") != 200:
                time.sleep(POLL_INTERVAL)
                continue
            data = result.get("data", {})
            status = data.get("status", "")
            log.info("📊 当前状态: %s", status)
            if status in targets:
                return data
            if status in ("卡密禁用", "超次禁用"):
                raise RuntimeError("卡密已禁用: " + status)
            if status in ("注册失败", "账密不可用"):
                raise RuntimeError(status + "|" + data.get("message", ""))
            time.sleep(POLL_INTERVAL)
        raise TimeoutError("轮询超时 " + str(timeout) + "s")

    def _is_duplicate_error(self, err_msg: str) -> bool:
        keywords = ["重复", "已被注册", "已经被别人注册", "请修改", "请选择一个不同的"]
        return any(k in err_msg for k in keywords)

    def _refresh_creds(self, retry_num: int):
        self.username = generate_username()
        self.password = generate_password()
        log.warning("⚠️ 账号重复，换全新账号 %s / %s 重试 (第 %d 次)",
                    self.username, self.password, retry_num)

    def run(self) -> Optional[dict]:
        log.info("=" * 50)
        log.info("🚀 卡密: %s | 📧 %s | 🌍 %s", self.card_key, self.email, self.country)

        try:
            card_info = self._query_card()
        except Exception as e:
            log.error("❌ 查询卡密失败: %s", e)
            return None
        if card_info.get("code") != 200:
            log.error("❌ 卡密无效: %s", card_info.get("message", ""))
            return None
        data = card_info.get("data", {})
        status = data.get("status", "")
        log.info("卡密状态: %s", status)
        if status in ("卡密禁用", "超次禁用"):
            log.error("❌ 卡密已被禁用")
            return None

        if status in ("准备打码", "正在打码", "打码成功", "准备注册", "正在注册"):
            if not self.username:
                self.username = data.get("username", "")
            if not self.password:
                self.password = data.get("password", "")
            self.email = data.get("mail", self.email)
            log.info("📋 恢复订单: %s / %s", self.username, self.password)
        else:
            self.username = generate_username()
            self.password = generate_password()
            log.info("🎲 生成: %s / %s", self.username, self.password)

        retries = 0
        while retries <= MAX_RETRY_ON_DUPLICATE:
            if _stop_event.is_set():
                return None

            # 阶段1：提交账号密码
            try:
                sub = self._submit()
                log.info("📤 提交结果: %s", sub.get("message", ""))
            except Exception as e:
                log.error("❌ 提交异常: %s", e)
                return None

            # 阶段2：轮询到打码成功/注册成功/失败
            try:
                polled = self._poll({"打码成功", "注册成功", "注册失败", "账密不可用"})
            except (RuntimeError, TimeoutError) as e:
                err = str(e)
                if self._is_duplicate_error(err) and retries < MAX_RETRY_ON_DUPLICATE:
                    retries += 1
                    self._refresh_creds(retries)
                    continue
                log.error("❌ %s", err)
                return None

            cs = polled.get("status", "")
            if cs == "注册成功":
                log.info("🎉 注册成功！")
                save_result(polled)
                return polled
            if cs in ("注册失败", "账密不可用"):
                msg = polled.get("message", "")
                if self._is_duplicate_error(msg) and retries < MAX_RETRY_ON_DUPLICATE:
                    retries += 1
                    self._refresh_creds(retries)
                    log.info("🔄 重新提交新账号...")
                    continue
                else:
                    log.error("❌ 注册失败: %s", msg)
                    return None

            # 阶段3：取验证链接
            log.info("✅ 打码成功，准备提交验证链接...")
            link = None
            if self.email_fetcher:
                link = self.email_fetcher.fetch_latest_steam_link()
            else:
                log.info("📎 请在 GUI 中粘贴 Steam 验证链接...")
                link = _manual_link_queue.get()
                if _stop_event.is_set():
                    return None

            if not link:
                log.error("❌ 无验证链接，终止")
                return None

            try:
                r = self._submit(emailurl=link)
                log.info("📤 链接提交: %s", r.get("message", ""))
            except Exception as e:
                log.error("❌ 提交链接失败: %s", e)
                return None

            # 阶段4：等最终注册结果
            log.info("⏳ 等待最终结果...")
            try:
                final = self._poll({"注册成功", "注册失败", "账密不可用"}, timeout=180)
            except (RuntimeError, TimeoutError) as e:
                err = str(e)
                if self._is_duplicate_error(err) and retries < MAX_RETRY_ON_DUPLICATE:
                    retries += 1
                    self._refresh_creds(retries)
                    log.info("🔄 重新提交新账号...")
                    continue
                log.error("❌ %s", err)
                try:
                    c = self._query_card()
                    d = c.get("data", {})
                    if d.get("status") == "注册成功":
                        save_result(d)
                        return d
                except Exception:
                    pass
                return None

            cs = final.get("status", "")
            if cs == "注册成功":
                log.info("🎉🎉🎉 注册成功！")
                log.info("   Steam: %s | 密码: %s", final.get("username", ""), final.get("password", ""))
                save_result(final)
                return final
            msg = final.get("message", "")
            if self._is_duplicate_error(msg) and retries < MAX_RETRY_ON_DUPLICATE:
                retries += 1
                self._refresh_creds(retries)
                continue
            log.error("❌ 最终注册失败: %s", msg)
            return None

        log.error("❌ 已达最大重试次数 %d，放弃", MAX_RETRY_ON_DUPLICATE)
        return None


# ============================================================
# 批量处理
# ============================================================

def process_one_task(key: str, email: str, email_password: str, imap_server: str,
                      imap_port: int, country: str) -> tuple:
    if _stop_event.is_set():
        return (key, email, None)

    fetcher = None
    if imap_server and email_password:
        log.info("📧 %s → 已配置自动提取验证链接（IMAP: %s:%d）", email, imap_server, imap_port)
        fetcher = EmailFetcher(imap_server, email, email_password, port=imap_port)
    else:
        log.info("📧 %s → 未配置IMAP，需手动粘贴验证链接", email)

    runner = SteamAutoRegister(
        card_key=key, email=email,
        country=country, email_fetcher=fetcher,
    )
    try:
        result = runner.run()
        return (key, email, result)
    except Exception as e:
        log.error("❌ 异常: %s", e)
        return (key, email, None)


# ============================================================
# tkinter GUI
# ============================================================

class SteamRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Steam 代注册 v3 🦞")
        self.root.geometry("820x720")
        self.root.minsize(700, 600)
        self._set_icon()

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=BOTH, expand=True)

        # ===== 顶部 Logo =====
        try:
            img = Image.open(self._logo_path())
            img = img.resize((80, 80), Image.LANCZOS)
            self.logo_img = ImageTk.PhotoImage(img)
            logo_frame = ttk.Frame(main)
            logo_frame.pack(fill=X, pady=(0, 6))
            logo_label = Label(logo_frame, image=self.logo_img)
            logo_label.pack(side=LEFT, padx=(0, 10))
            title_label = Label(logo_frame, text="Steam 代注册", font=("Helvetica", 18, "bold"))
            title_label.pack(side=LEFT)
            version_label = Label(logo_frame, text="v3", font=("Helvetica", 12), fg="gray")
            version_label.pack(side=LEFT, padx=(6, 0))
        except Exception:
            pass

        # ===== 上：配置面板 =====
        cfg = ttk.LabelFrame(main, text="配置", padding=10)
        cfg.pack(fill=X, pady=(0, 8))

        # 第一行：IMAP 服务器 + 端口 + 地区
        row1 = ttk.Frame(cfg)
        row1.pack(fill=X, pady=2)
        ttk.Label(row1, text="IMAP 服务器:", width=12).pack(side=LEFT)
        self.imap_entry = ttk.Entry(row1, width=20)
        self.imap_entry.insert(0, "luoyue.cc")
        self.imap_entry.pack(side=LEFT)
        ttk.Label(row1, text="端口:").pack(side=LEFT, padx=(6, 0))
        self.port_entry = ttk.Entry(row1, width=6)
        self.port_entry.insert(0, "993")
        self.port_entry.pack(side=LEFT, padx=(4, 20))
        ttk.Label(row1, text="地区:", width=6).pack(side=LEFT)
        self.country_combo = ttk.Combobox(row1, values=["CN", "US", "RU", "BR", "SG", "JP", "HK", "KR"], width=6)
        self.country_combo.set("CN")
        self.country_combo.pack(side=LEFT)

        # 第二行：统一邮箱密码
        row2 = ttk.Frame(cfg)
        row2.pack(fill=X, pady=2)
        ttk.Label(row2, text="邮箱密码:", width=12).pack(side=LEFT)
        self.pwd_entry = ttk.Entry(row2, width=40, show="●")
        self.pwd_entry.pack(side=LEFT, padx=(0, 10))
        self.show_pwd_var = BooleanVar()
        ttk.Checkbutton(row2, text="显示", variable=self.show_pwd_var,
                        command=self._toggle_pwd).pack(side=LEFT)

        # 第三行：线程数
        row3 = ttk.Frame(cfg)
        row3.pack(fill=X, pady=2)
        ttk.Label(row3, text="并发线程:", width=12).pack(side=LEFT)
        self.thread_spin = ttk.Spinbox(row3, from_=1, to=20, width=5)
        self.thread_spin.set(1)
        self.thread_spin.pack(side=LEFT)

        # ===== 输入区 =====
        inputs = ttk.Frame(main)
        inputs.pack(fill=BOTH, expand=True, pady=(0, 8))

        # 左右分区：邮箱（小）| 卡密（大），中间带分隔条可拖动
        paned = ttk.PanedWindow(inputs, orient=HORIZONTAL)
        paned.pack(fill=BOTH, expand=True)

        left = ttk.LabelFrame(paned, text="📧 邮箱列表（支持 email@xx.com----密码）", padding=5)
        self.email_text = scrolledtext.ScrolledText(left, height=5, width=20, font=("Menlo", 10))
        self.email_text.pack(fill=BOTH, expand=True)
        paned.add(left, weight=1)

        right = ttk.LabelFrame(paned, text="🔑 卡密列表（每行一个）", padding=5)
        self.key_text = scrolledtext.ScrolledText(right, height=8, width=35, font=("Menlo", 10))
        self.key_text.pack(fill=BOTH, expand=True)
        paned.add(right, weight=2)

        # ===== 按钮行 =====
        btn_row = ttk.Frame(main)
        btn_row.pack(fill=X, pady=(0, 8))

        self.start_btn = ttk.Button(btn_row, text="▶ 开始注册", command=self._start_registration)
        self.start_btn.pack(side=LEFT, padx=(0, 8))
        self.stop_btn = ttk.Button(btn_row, text="⏹ 停止", command=self._stop_registration, state=DISABLED)
        self.stop_btn.pack(side=LEFT, padx=(0, 8))
        self.clear_btn = ttk.Button(btn_row, text="🧹 清空日志", command=self._clear_log)
        self.clear_btn.pack(side=LEFT, padx=(0, 8))
        self.open_file_btn = ttk.Button(btn_row, text="📂 打开结果文件", command=self._open_result_file)
        self.open_file_btn.pack(side=LEFT)

        # ===== 验证链接输入行 =====
        link_row = ttk.Frame(main)
        link_row.pack(fill=X, pady=(0, 8))
        ttk.Label(link_row, text="🔗 验证链接:").pack(side=LEFT)
        self.link_entry = ttk.Entry(link_row)
        self.link_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        self.link_btn = ttk.Button(link_row, text="提交链接", command=self._submit_link, state=DISABLED)
        self.link_btn.pack(side=LEFT)

        # ===== 日志输出 =====
        log_frame = ttk.LabelFrame(main, text="📋 运行日志", padding=5)
        log_frame.pack(fill=BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, font=("Menlo", 9),
                                                   state=DISABLED, wrap=WORD)
        self.log_text.pack(fill=BOTH, expand=True)

        # ===== 状态栏 =====
        self.status_var = StringVar(value="就绪")
        status_bar = ttk.Label(main, textvariable=self.status_var, relief=SUNKEN, anchor=W)
        status_bar.pack(fill=X, pady=(4, 0))

    def _toggle_pwd(self):
        show = self.show_pwd_var.get()
        self.pwd_entry.config(show="" if show else "●")

    def _log(self, msg: str):
        self.log_text.config(state=NORMAL)
        self.log_text.insert(END, msg + "\n")
        self.log_text.see(END)
        self.log_text.config(state=DISABLED)

    def _clear_log(self):
        self.log_text.config(state=NORMAL)
        self.log_text.delete(1.0, END)
        self.log_text.config(state=DISABLED)

    def _poll_log_queue(self):
        while True:
            try:
                msg = log_queue.get_nowait()
                self._log(msg)
            except queue.Empty:
                break
        self.root.after(200, self._poll_log_queue)

    def _open_result_file(self):
        import subprocess, sys, os
        if sys.platform == "win32":
            os.startfile(os.path.dirname(_RESULT_FILE))
        else:
            subprocess.run(["open", "-R", _RESULT_FILE], check=False)

    def _set_running(self, running: bool):
        if running:
            self.start_btn.config(state=DISABLED)
            self.stop_btn.config(state=NORMAL)
            self.link_btn.config(state=NORMAL)
            self.status_var.set("运行中...")
        else:
            self.start_btn.config(state=NORMAL)
            self.stop_btn.config(state=DISABLED)
            self.link_btn.config(state=DISABLED)
            self.status_var.set("就绪")

    def _stop_registration(self):
        _stop_event.set()
        # 如果正在等手动验证链接，唤醒它
        _manual_link_queue.put(None)
        self._log("⏹ 正在停止...")
        self._set_running(False)
        # 调用 API 停止
        try:
            keys = self._get_active_keys()
            for k in keys:
                requests.post(API_STOP, data={"k": k}, timeout=10)
        except Exception:
            pass

    def _get_active_keys(self) -> list:
        raw = self.key_text.get(1.0, END).strip()
        return [k.strip() for k in raw.splitlines() if k.strip()]

    def _submit_link(self):
        link = self.link_entry.get().strip()
        if link:
            _manual_link_queue.put(link)
            self.link_entry.delete(0, END)
            self._log(f"📎 已提交验证链接")

    def _start_registration(self):
        # 解析输入
        raw_emails = self.email_text.get(1.0, END).strip().splitlines()
        raw_keys = self.key_text.get(1.0, END).strip().splitlines()
        imap_server = self.imap_entry.get().strip()
        imap_port = self.port_entry.get().strip()
        imap_pwd = self.pwd_entry.get().strip()
        country = self.country_combo.get().strip()
        threads_str = self.thread_spin.get().strip()

        if not raw_emails:
            messagebox.showwarning("提示", "请填写邮箱列表")
            return
        if not raw_keys:
            messagebox.showwarning("提示", "请填写卡密列表")
            return

        try:
            threads = int(threads_str) if threads_str else 1
        except ValueError:
            threads = 1

        # 解析邮箱及密码
        emails = []
        email_passwords = {}
        for raw in raw_emails:
            raw = raw.strip().strip(",")
            if not raw:
                continue
            if "----" in raw:
                e, p = raw.split("----", 1)
                e, p = e.strip(), p.strip()
                if e:
                    emails.append(e)
                    email_passwords[e] = p
            elif "@" in raw:
                emails.append(raw)
                # 用全局密码
                if imap_pwd:
                    email_passwords[raw] = imap_pwd

        card_keys = [k.strip() for k in raw_keys if k.strip()]
        n = min(len(emails), len(card_keys))
        if n == 0:
            messagebox.showwarning("提示", "邮箱和卡密数量不匹配或为空")
            return

        pairs = list(zip(card_keys[:n], emails[:n]))

        self._log(f"=" * 50)
        self._log(f"🚀 开始注册 {n} 个任务，{threads} 线程")
        self._set_running(True)
        _stop_event.clear()

        # 在后台线程执行注册
        try:
            imap_port = int(imap_port) if imap_port else 993
        except ValueError:
            imap_port = 993
        t = threading.Thread(target=self._run_batch, args=(pairs, email_passwords, imap_server, imap_port, country, threads),
                             daemon=True)
        t.start()

    def _run_batch(self, pairs, email_passwords, imap_server, imap_port, country, threads):
        results = {"success": [], "failed_keys": []}
        results_lock = threading.Lock()

        if threads <= 1:
            for i, (key, email) in enumerate(pairs, 1):
                if _stop_event.is_set():
                    break
                log.info("")
                log.info("📌 [%d/%d] %s → %s", i, len(pairs), key, email)
                _, _, result = process_one_task(
                    key, email, email_passwords.get(email, ""),
                    imap_server, imap_port, country
                )
                if result:
                    with results_lock:
                        results["success"].append(result)
                else:
                    with results_lock:
                        results["failed_keys"].append((key, email))
                if i < len(pairs):
                    time.sleep(3)
        else:
            log.info("🚀 %d 线程并发处理 %d 个任务", threads, len(pairs))
            with ThreadPoolExecutor(max_workers=threads) as ex:
                futs = {
                    ex.submit(process_one_task, k, e, email_passwords.get(e, ""),
                              imap_server, imap_port, country): k
                    for k, e in pairs
                }
                for fut in as_completed(futs):
                    key, email, result = fut.result()
                    if result:
                        with results_lock:
                            results["success"].append(result)
                    else:
                        with results_lock:
                            results["failed_keys"].append((key, email))

        # 完成
        success = len(results["success"])
        failed = len(results["failed_keys"])

        # 在主线程更新 UI
        self.root.after(0, lambda: self._log(f"=" * 50))
        self.root.after(0, lambda: self._log(f"📊 完成: ✅ {success} 成功  ❌ {failed} 失败"))
        if failed == 0:
            self.root.after(0, lambda: self._log("🎉 全部成功！"))
        else:
            self.root.after(0, lambda: self._log("💡 失败项可修改配置后再次点击开始注册"))
        self.root.after(0, lambda: self._set_running(False))


    def _set_icon(self):
        """设置窗口图标和 dock 图标"""
        try:
            img = Image.open(self._logo_path())
            img = img.resize((64, 64), Image.LANCZOS)
            icon = ImageTk.PhotoImage(img)
            self.root.iconphoto(True, icon)
            # 保持引用防止 GC
            self._app_icon = icon
        except Exception:
            pass

    @staticmethod
    def _logo_path():
        """返回 logo.png 路径（兼容 PyInstaller 打包）"""
        import sys
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            return Path(sys._MEIPASS) / "logo.png"
        return Path(__file__).parent / "logo.png"


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    root = Tk()
    app = SteamRegisterGUI(root)
    root.mainloop()
