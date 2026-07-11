#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书开播检测工具 - 多主播版

监控多个小红书用户主页的直播状态,在「开播 / 下播」状态变化时通过 Bark 推送到手机。
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
    "xhs_users": [],
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
    merged = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                merged.update(json.load(f))
        except Exception as e:
            log.warning("读取 %s 失败: %s", CONFIG_FILE, e)
    env_map = {
        "XHS_USERS": "xhs_users",
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
        if cfg_key == "xhs_users":
            try:
                val = json.loads(val)
            except Exception:
                continue
        elif cfg_key in ("check_interval_seconds", "remind_interval_seconds", "request_timeout"):
            try:
                val = int(val)
            except ValueError:
                pass
        merged[cfg_key] = val
    # 兼容旧版单主播配置
    single_url = merged.get("xhs_user_url") or os.environ.get("XHS_USER_URL")
    if single_url and not merged.get("xhs_users"):
        merged["xhs_users"] = [{"name": "主播", "url": single_url}]
    missing = []
    if not merged.get("xhs_users"):
        missing.append("xhs_users (或环境变量 XHS_USERS)")
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
    return {"users": {}}


def save_state(state_file, state):
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("保存状态文件失败: %s", e)


# ---------- 抓取 & 解析 ----------

def _get_headers(ua, cookie):
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
    if "xhslink.com" not in url:
        return url
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    try:
        opener.open(req, timeout=timeout)
        return url
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
                return True, {"content": text, "title": text}
            return False, {"content": text}
        return False, None
    except Exception:
        return False, None


def extract_nickname(state):
    try:
        user = state.get("user", {})
        ui = user.get("userPageData", {}).get("userInfo", {}) or user.get("userInfo", {})
        if ui.get("nickname"):
            return ui["nickname"]
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

def check_user(user_cfg, cookie, ua, timeout, debug=False):
    url = user_cfg["url"]
    custom_name = user_cfg.get("name", "")
    html = fetch_page(url, cookie, ua, timeout)
    state = extract_initial_state(html)
    nickname = custom_name or "小红书用户"
    living = False
    live_info = None
    widget_text = ""
    if state:
        page_nickname = extract_nickname(state)
        if page_nickname and page_nickname != "小红书用户":
            nickname = custom_name or page_nickname
        living, live_info = find_live_info(state)
        widget_text = (live_info or {}).get("content", "")
    else:
        log.warning("[%s] 未解析到 __INITIAL_STATE__", custom_name or url[:40])
    return living, nickname, widget_text


def run_once(cfg, debug=False):
    remind_interval = int(cfg.get("remind_interval_seconds", 3600))
    state_file = cfg.get("state_file", "state.json")
    cookie = cfg.get("xhs_cookie", "")
    ua = cfg.get("user_agent")
    timeout = cfg.get("request_timeout", 15)
    users = cfg.get("xhs_users", [])
    state = load_state(state_file)
    if "users" not in state:
        state["users"] = {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    any_change = False
    for user_cfg in users:
        url = user_cfg["url"]
        user_id = user_cfg.get("id") or url
        user_state = state["users"].get(user_id, {"living": False, "last_notify_time": 0})
        try:
            living, nickname, widget_text = check_user(user_cfg, cookie, ua, timeout, debug)
        except Exception as e:
            log.error("[%s] 检测异常: %s", user_cfg.get("name", url[:30]), e)
            import traceback
            if debug:
                log.error(traceback.format_exc())
            continue
        was_living = user_state.get("living", False)
        last_notify = user_state.get("last_notify_time", 0)
        log.info("[%s] %s: %s%s", now_str, nickname,
                 "直播中" if living else "未开播",
                 f" ({widget_text})" if widget_text else "")
        if living and not was_living:
            title = f"📢 {nickname} 开播啦"
            body = f"{nickname} 正在小红书直播\n时间: {now_str}\n主页: {url}"
            send_bark(cfg["bark_server"], cfg["bark_key"], title, body)
            user_state["last_notify_time"] = time.time()
            any_change = True
        elif living and was_living and (time.time() - last_notify) > remind_interval:
            title = f"📺 {nickname} 仍在直播中"
            body = f"{nickname} 直播持续中\n时间: {now_str}"
            send_bark(cfg["bark_server"], cfg["bark_key"], title, body)
            user_state["last_notify_time"] = time.time()
            any_change = True
        elif not living and was_living:
            title = f"💤 {nickname} 下播了"
            body = f"{nickname} 已结束直播\n时间: {now_str}"
            send_bark(cfg["bark_server"], cfg["bark_key"], title, body)
            any_change = True
        user_state["is_live"] = living
        user_state["living"] = living
        user_state["name"] = nickname
        user_state["note"] = widget_text if widget_text else ("正在直播" if living else "监控中")
        user_state["last_check"] = now_str
        state["users"][user_id] = user_state
        time.sleep(2)
    state["last_check"] = now_str
    save_state(state_file, state)
    return any_change


def main():
    cfg = load_config()
    interval = int(cfg.get("check_interval_seconds", 600))
    debug = "--debug" in sys.argv
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
    log.info("小红书开播检测已启动(本地长驻模式)")
    log.info("监控主播数: %d", len(cfg.get("xhs_users", [])))
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
