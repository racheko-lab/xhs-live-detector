#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书开播检测工具

监控指定小红书用户主页的直播状态,在「开播 / 下播」状态变化时通过 Bark 推送到手机。

使用方法:
  - GitHub Actions 部署:用 Secrets 注入配置,定时每 10 分钟触发 `python xhs_live_detector.py --once`
  - 本地长驻:填写 config.json 后运行 `python xhs_live_detector.py`,Ctrl+C 停止

说明:
  - 小红书无公开 API,本工具通过抓取网页版主页 HTML、解析 window.__INITIAL_STATE__
    中的直播状态字段来判断是否开播,并辅以关键词兜底。
  - 网页版主页通常需要登录 Cookie 才能看到完整信息,请在 config.json 或环境变量填入。
  - 状态会写入 state.json,避免重启或重复触发时重复推送。
"""

import json
import os
import re
import sys
import time
import logging
from datetime import datetime


CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "xhs_user_url": "",
    "xhs_cookie": "",
    "bark_key": "",
    "bark_server": "https://api.day.app",
    "check_interval_seconds": 600,
    "remind_interval_seconds": 3600,
    "state_file": "state.json",
    "request_timeout": 15,
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("xhs-live")


# ---------- 配置 & 状态 ----------

def load_config():
    # 优先用环境变量(GitHub Actions Secrets 注入),其次读 config.json
    merged = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                merged.update(json.load(f))
        except Exception as e:
            log.warning("读取 %s 失败: %s", CONFIG_FILE, e)
    # 环境变量覆盖(适配 GitHub Actions / 任意 CI)
    env_map = {
        "XHS_USER_URL": "xhs_user_url",
        "XHS_COOKIE": "xhs_cookie",
        "BARK_KEY": "bark_key",
        "BARK_SERVER": "bark_server",
        "CHECK_INTERVAL_SECONDS": "check_interval_seconds",
        "REMIND_INTERVAL_SECONDS": "remind_interval_seconds",
        "STATE_FILE": "state_file",
        "REQUEST_TIMEOUT": "request_timeout",
        "USER_AGENT": "user_agent",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val is None or val == "":
            continue
        if cfg_key in ("check_interval_seconds", "remind_interval_seconds", "request_timeout"):
            try:
                val = int(val)
            except ValueError:
                pass
        merged[cfg_key] = val
    missing = []
    if not merged.get("xhs_user_url"):
        missing.append("xhs_user_url (或环境变量 XHS_USER_URL)")
    if not merged.get("bark_key"):
        missing.append("bark_key (或环境变量 BARK_KEY)")
    if missing:
        log.error("配置缺少必填项: %s", ", ".join(missing))
        sys.exit(1)
    return merged


def load_state(state_file):
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state_file, state):
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("保存状态文件失败: %s", e)


# ---------- 抓取 & 解析 ----------

def _safe_header(val):
    """把 header 值转成 latin-1 安全字符串(非 ascii 字符做 percent-encoding)"""
    if val is None:
        return ""
    s = str(val)
    if all(ord(c) < 128 for c in s):
        return s
    # 含非 ascii 字符时,做 percent-encoding
    from urllib.parse import quote
    return quote(s, safe="=:;,/!?@&=+$()*'\"")

def _get_headers(ua, cookie):
    """构造 iPhone Safari 网页版请求头"""
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Referer": "https://www.xiaohongshu.com/",
    }
    if cookie and cookie.isascii():
        headers["Cookie"] = cookie
    return headers

def _resolve_short_url(url, headers, timeout):
    """xhslink 短链:跟随重定向拿最终 URL(用 urllib,兼容性好)"""
    if "xhslink.com" not in url:
        return url
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    try:
        # 不跟随重定向,只拿 Location
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener2 = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
        try:
            opener2.open(req, timeout=timeout)
            return url  # 没重定向
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            if loc:
                log.info("短链解析: %s -> %s", url[:40], loc[:80])
                return loc
            return url
    except Exception as e:
        log.warning("短链解析失败: %s", e)
        return url

def fetch_page(url, cookie, ua, timeout):
    """抓取小红书页面 HTML。xhslink短链先解析最终URL再请求(参考streamget实现)。"""
    headers = _get_headers(ua, cookie)
    final_url = _resolve_short_url(url, headers, timeout)
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(final_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip()
        return data.decode(charset, errors="replace")


def extract_initial_state(html):
    """从 HTML 中提取 window.__INITIAL_STATE__ 的 JSON 对象"""
    m = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
        html,
        re.DOTALL,
    )
    if not m:
        m = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*$",
            html,
            re.DOTALL | re.MULTILINE,
        )
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw2 = re.sub(r"\bundefined\b", "null", raw)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError:
            return None


def find_live_info(state):
    """
    通过用户主页 SSR 的 widget 数据判断直播状态。
    核心逻辑: profile.userInfo.userPageWidgetsInfo.normalWidgetList 中
    businessType=="live" 的 widget,其 content 字段包含直播状态文案:
      - "直播中" / "正在直播" → 开播
      - "直播回顾" / 其他 → 未开播
    返回 (是否直播中, 直播信息dict)
    """
    if not isinstance(state, dict):
        return False, None
    try:
        widgets = (
            state.get("profile", {})
            .get("userInfo", {})
            .get("userPageWidgetsInfo", {})
            .get("normalWidgetList", [])
        )
        for w in widgets:
            if w.get("businessType") != "live":
                continue
            content_raw = w.get("widgetDataContent", "")
            if not content_raw:
                continue
            try:
                content_obj = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            except Exception:
                content_obj = {}
            text = content_obj.get("content", "") or w.get("title", "") or ""
            if "直播中" in text or "正在直播" in text:
                return True, {"content": text, "title": text, "widget": w}
            return False, {"content": text}
        return False, None
    except Exception:
        return False, None


def extract_nickname(state):
    """尝试多种路径提取主播昵称"""
    try:
        # streamget 路径:user.userPageData.userInfo.nickname
        user = state.get("user", {})
        ui = user.get("userPageData", {}).get("userInfo", {}) or user.get("userInfo", {})
        if ui.get("nickname"):
            return ui["nickname"]
        # 网页版路径:profile.userInfo.nickname
        pui = state.get("profile", {}).get("userInfo", {})
        if pui.get("nickname"):
            return pui["nickname"]
        return "小红书用户"
    except Exception:
        return "小红书用户"


# ---------- Bark 推送 ----------

def send_bark(bark_server, bark_key, title, body, group="小红书开播"):
    url = f"{bark_server.rstrip('/')}/{bark_key}"
    payload = {
        "title": title,
        "body": body,
        "group": group,
        "sound": "bell",
        "icon": "https://www.xiaohongshu.com/favicon.ico",
    }
    try:
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data.get("code") == 200:
                log.info("Bark 推送成功: %s", title)
                return True
            log.warning("Bark 返回异常: %s", data)
    except Exception as e:
        log.warning("Bark 推送失败: %s", e)
    return False


# ---------- 主流程 ----------

def check_once(cfg, debug=False):
    url = cfg["xhs_user_url"]
    cookie = cfg.get("xhs_cookie", "")
    ua = cfg.get("user_agent")
    timeout = cfg.get("request_timeout", 15)

    html = fetch_page(url, cookie, ua, timeout)
    state = extract_initial_state(html)

    if debug:
        log.info("===== DEBUG 诊断 =====")
        log.info("HTML长度: %d", len(html))
        log.info("含__INITIAL_STATE__: %s", "window.__INITIAL_STATE__" in html)
        log.info("含'登录'(被跳登录页): %s", "website-login" in html or "fe-login" in html)
        log.info("含'访问频繁'(被风控): %s", "访问频繁" in html or "error_code" in html)
        log.info("含'直播': %s", "直播" in html)
        log.info("含'liveStream': %s", "liveStream" in html)
        log.info("含'直播中': %s", "直播中" in html)
        log.info("含'正在直播': %s", "正在直播" in html)
        log.info("含'businessType':'live': %s", '"businessType":"live"' in html)
        # 输出 live widget 附近内容
        if '"businessType":"live"' in html:
            idx = html.find('"businessType":"live"')
            log.info("live widget 片段: %s", html[max(0,idx-50):idx+400])
        if state:
            log.info("state 顶层 keys: %s", list(state.keys()))
            # profile 路径
            profile = state.get("profile", {})
            ui = profile.get("userInfo", {})
            log.info("profile.userInfo keys: %s", list(ui.keys()) if isinstance(ui,dict) else "空")
            log.info("===== DEBUG END =====")

    nickname = "小红书用户"
    living = False
    live_info = None

    if state:
        nickname = extract_nickname(state)
        living, live_info = find_live_info(state)
    else:
        log.warning("未解析到 __INITIAL_STATE__,可能被反爬或需要登录 Cookie")

    return living, nickname, live_info


def run_once(cfg, debug=False):
    """执行一次检测 + 推送 + 状态持久化。返回当前是否直播中。"""
    remind_interval = int(cfg.get("remind_interval_seconds", 3600))
    state_file = cfg.get("state_file", "state.json")
    state = load_state(state_file)
    was_living = state.get("living", False)
    last_notify_time = state.get("last_notify_time", 0)

    living, nickname, live_info = check_once(cfg, debug=debug)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    widget_text = (live_info or {}).get("content", "")
    log.info("[%s] %s 直播状态: %s%s", now, nickname,
             "直播中" if living else "未开播",
             f" ({widget_text})" if widget_text else "")

    if living and not was_living:
        title = f"📢 {nickname} 开播啦"
        body = (
            f"{nickname} 正在小红书直播\n"
            f"时间: {now}\n"
            f"主页: {cfg['xhs_user_url']}"
        )
        send_bark(cfg["bark_server"], cfg["bark_key"], title, body)
        last_notify_time = time.time()
    elif living and was_living and (time.time() - last_notify_time) > remind_interval:
        title = f"📺 {nickname} 仍在直播中"
        body = f"{nickname} 直播持续中\n时间: {now}"
        send_bark(cfg["bark_server"], cfg["bark_key"], title, body)
        last_notify_time = time.time()
    elif not living and was_living:
        title = f"💤 {nickname} 下播了"
        body = f"{nickname} 已结束直播\n时间: {now}"
        send_bark(cfg["bark_server"], cfg["bark_key"], title, body)

    state["living"] = living
    state["last_notify_time"] = last_notify_time
    state["last_check"] = now
    save_state(state_file, state)
    return living


def main():
    cfg = load_config()
    interval = int(cfg.get("check_interval_seconds", 600))
    debug = "--debug" in sys.argv

    # --once: 单次检测模式
    if "--once" in sys.argv:
        log.info("单次检测模式")
        try:
            run_once(cfg, debug=debug)
        except Exception as e:
            import traceback
            log.error("检测异常: %s", e)
            log.error("完整堆栈:\n%s", traceback.format_exc())
            sys.exit(1)
        return

    # 默认: 本地长驻循环模式
    log.info("小红书开播检测已启动(本地长驻模式)")
    log.info("监控主页: %s", cfg["xhs_user_url"])
    log.info("检测间隔: %d 秒", interval)
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            log.error("检测异常: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("已停止检测")
