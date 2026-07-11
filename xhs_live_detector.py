#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书开播检测工具 - 多主播版 v2

监控多个小红书用户主页的直播状态,在「开播 / 下播」状态变化时通过 Bark 推送到手机。

改进:
- 正确跟随短链重定向,保留xsec_token等所有参数
- 多路径检测直播状态(liveStream/widget/文本关键词)
- 改进请求头,支持移动端检测
"""

import json
import os
import re
import sys
import time
import logging
from datetime import datetime
from urllib.parse import urljoin


CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "xhs_users": [],
    "xhs_cookie": "",
    "bark_key": "",
    "bark_server": "https://api.day.app",
    "check_interval_seconds": 600,
    "remind_interval_seconds": 3600,
    "state_file": "state.json",
    "request_timeout": 20,
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
                log.info("从环境变量加载 XHS_USERS: %d 个主播", len(val))
            except Exception as e:
                log.warning("解析环境变量 XHS_USERS 失败: %s, 原始值长度: %d", e, len(val))
                continue
        elif cfg_key in ("check_interval_seconds", "remind_interval_seconds", "request_timeout"):
            try:
                val = int(val)
            except ValueError:
                pass
        merged[cfg_key] = val
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

def _get_headers(ua, cookie, referer="https://www.xiaohongshu.com/"):
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Referer": referer,
    }
    if cookie and cookie.isascii():
        headers["Cookie"] = cookie
    return headers


def _follow_redirects(start_url, headers, timeout, ctx):
    """正确跟随所有重定向,返回最终URL和HTML内容"""
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    current = start_url
    for _ in range(10):
        req = urllib.request.Request(current, headers=headers)
        try:
            resp = opener.open(req, timeout=timeout)
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            charset = "utf-8"
            if "charset=" in ctype:
                charset = ctype.split("charset=")[-1].split(";")[0].strip()
            return resp.geturl(), data.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location", "")
                if not loc:
                    break
                if loc.startswith("/"):
                    loc = urljoin(current, loc)
                current = loc
                continue
            raise
    return current, None


def fetch_page(url, cookie, ua, timeout):
    """抓取页面,自动处理短链重定向"""
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    headers = _get_headers(ua, cookie)
    if "xhslink.com" in url:
        log.info("解析短链: %s", url[:50])
        final_url, html = _follow_redirects(url, headers, timeout, ctx)
        if html:
            return final_url, html
    
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip()
        return resp.geturl(), data.decode(charset, errors="replace")


def extract_initial_state(html):
    """从HTML中提取window.__INITIAL_STATE__"""
    patterns = [
        r"<script>window\.__INITIAL_STATE__=(.*?)</script>",
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL | re.MULTILINE)
        if m:
            raw = m.group(1)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raw2 = re.sub(r"\bundefined\b", "null", raw)
                try:
                    return json.loads(raw2)
                except json.JSONDecodeError:
                    continue
    return None


def find_live_info(state, html):
    """
    多路径检测直播状态:
    1. liveStream (直播间页面)
    2. normalWidgetList (用户主页widget)
    3. HTML文本关键词
    返回: (is_live: bool, info: dict|None)
    """
    if not state and not html:
        return False, None
    
    info = {}
    is_live = False
    live_title = ""
    
    # 路径1: 直播间页面的 liveStream
    if isinstance(state, dict):
        live_stream = state.get("liveStream")
        if live_stream and isinstance(live_stream, dict):
            live_status = live_stream.get("liveStatus")
            if live_status == "success":
                room_data = live_stream.get("roomData", {})
                room_info = room_data.get("roomInfo", {}) if isinstance(room_data, dict) else {}
                title = room_info.get("roomTitle", "") if isinstance(room_info, dict) else ""
                if "回放" not in title:
                    is_live = True
                    live_title = title or "正在直播"
                    info["source"] = "liveStream"
                    info["title"] = live_title
                    return True, info
                else:
                    info["note"] = "直播回放"
    
    # 路径2: profile.userInfo.userPageWidgetsInfo.normalWidgetList (移动端)
    if isinstance(state, dict):
        try:
            widgets = (
                state.get("profile", {})
                .get("userInfo", {})
                .get("userPageWidgetsInfo", {})
                .get("normalWidgetList", [])
            )
            if isinstance(widgets, list):
                for w in widgets:
                    if not isinstance(w, dict):
                        continue
                    if w.get("businessType") != "live":
                        continue
                    content_raw = w.get("widgetDataContent", "") or w.get("content", "")
                    text = w.get("title", "") or ""
                    if content_raw:
                        try:
                            content_obj = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                            if isinstance(content_obj, dict):
                                text = content_obj.get("content", "") or text
                        except Exception:
                            text = content_raw[:200]
                    if "直播中" in text or "正在直播" in text:
                        is_live = True
                        live_title = text
                        info["source"] = "widget-mobile"
                        info["title"] = live_title
                        info["content"] = text
                        return True, info
                    elif text:
                        info["widget_text"] = text
        except Exception:
            pass
    
    # 路径3: user.userPageData (PC端路径兜底)
    if isinstance(state, dict):
        try:
            user_data = state.get("user", {}).get("userPageData", {})
            if isinstance(user_data, dict):
                for key in ["result", "extraInfo", "basicInfo", "tabs"]:
                    val = user_data.get(key)
                    if isinstance(val, dict):
                        def search_dict(d, path=""):
                            nonlocal is_live, live_title
                            for k, v in d.items():
                                if isinstance(v, str):
                                    if "直播中" in v or "正在直播" in v:
                                        is_live = True
                                        live_title = v
                                        return True
                                elif isinstance(v, dict):
                                    if search_dict(v, f"{path}.{k}"):
                                        return True
                                elif isinstance(v, list):
                                    for item in v:
                                        if isinstance(item, dict) and search_dict(item, f"{path}[{k}]"):
                                            return True
                            return False
                        if search_dict(val):
                            info["source"] = f"user.userPageData.{key}"
                            info["title"] = live_title
                            return True, info
        except Exception:
            pass
    
    # 路径4: HTML文本中搜索直播关键词(兜底)
    if html:
        for kw in ["正在直播", "直播中", "进入直播间"]:
            if kw in html:
                idx = html.find(kw)
                context = html[max(0, idx-50):idx+100]
                if "直播回顾" not in context and "回放" not in context:
                    is_live = True
                    info["source"] = "html-keyword"
                    info["title"] = kw
                    info["context"] = context
                    return True, info
    
    if not is_live:
        if info.get("widget_text"):
            return False, {"content": info["widget_text"]}
        return False, None
    return is_live, info


def extract_nickname(state, html):
    """从state或HTML中提取主播昵称"""
    if isinstance(state, dict):
        for path in [
            lambda s: s.get("user", {}).get("userPageData", {}).get("basicInfo", {}).get("nickname"),
            lambda s: s.get("profile", {}).get("userInfo", {}).get("nickname"),
            lambda s: s.get("user", {}).get("userInfo", {}).get("nickname"),
        ]:
            try:
                name = path(state)
                if name and isinstance(name, str) and name != "小红书用户":
                    return name
            except Exception:
                continue
    if html:
        m = re.search(r"<title>@?(.*?)(\s*[-|]\s*小红书)?</title>", html)
        if m:
            title = m.group(1).strip()
            if title and "小红书" not in title and len(title) < 50:
                return title.replace("的个人主页", "").strip()
    return "小红书用户"


def extract_user_id(url):
    """从URL中提取小红书用户ID,支持多种URL格式包括登录重定向"""
    if not url:
        return url
    from urllib.parse import urlparse, parse_qs, unquote
    parsed = urlparse(url)
    if "/login" in parsed.path:
        qs = parse_qs(parsed.query)
        redirect = qs.get("redirectPath", [""])[0]
        if redirect:
            redirect = unquote(redirect)
            m = re.search(r"/user/profile/([a-f0-9]+)", redirect)
            if m:
                return m.group(1)
    m = re.search(r"/user/profile/([a-f0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"xiaohongshu\.com/user/([a-f0-9]+)", url)
    if m:
        return m.group(1)
    return url


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
    final_url, html = fetch_page(url, cookie, ua, timeout)
    
    if "/login" in final_url:
        log.warning("[%s] 被重定向到登录页, Cookie可能无效或过期", custom_name or url[:40])
        from urllib.parse import urlparse, parse_qs, unquote
        parsed = urlparse(final_url)
        qs = parse_qs(parsed.query)
        redirect = qs.get("redirectPath", [""])[0]
        if redirect:
            original_url = "https://www.xiaohongshu.com" + unquote(redirect)
            log.info("[%s] 尝试不带Cookie直接访问原始URL...", custom_name or url[:40])
            final_url2, html2 = fetch_page(original_url, "", ua, timeout)
            if "/login" not in final_url2:
                final_url, html = final_url2, html2
    
    state = extract_initial_state(html)
    nickname = custom_name or "小红书用户"
    living = False
    live_info = None
    widget_text = ""
    
    if state:
        page_nickname = extract_nickname(state, html)
        if page_nickname and page_nickname != "小红书用户":
            nickname = custom_name or page_nickname
        living, live_info = find_live_info(state, html)
        widget_text = (live_info or {}).get("title", "") or (live_info or {}).get("content", "") or (live_info or {}).get("widget_text", "")
    else:
        if html:
            page_nickname = extract_nickname(None, html)
            if page_nickname and page_nickname != "小红书用户":
                nickname = custom_name or page_nickname
            living, live_info = find_live_info(None, html)
            widget_text = (live_info or {}).get("title", "")
        if "/login" not in final_url:
            log.warning("[%s] 未解析到 __INITIAL_STATE__", custom_name or url[:40])
    
    if debug and live_info:
        log.info("[%s] 检测来源: %s", nickname, live_info.get("source", "unknown"))
    
    return living, nickname, widget_text, final_url


def run_once(cfg, debug=False):
    remind_interval = int(cfg.get("remind_interval_seconds", 3600))
    state_file = cfg.get("state_file", "state.json")
    cookie = cfg.get("xhs_cookie", "")
    ua = cfg.get("user_agent")
    timeout = cfg.get("request_timeout", 20)
    users = cfg.get("xhs_users", [])
    state = load_state(state_file)
    if "users" not in state:
        state["users"] = {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    any_change = False
    for user_cfg in users:
        url = user_cfg["url"]
        try:
            living, nickname, widget_text, final_url = check_user(user_cfg, cookie, ua, timeout, debug)
        except Exception as e:
            log.error("[%s] 检测异常: %s", user_cfg.get("name", url[:30]), e)
            import traceback
            if debug:
                log.error(traceback.format_exc())
            continue
        user_id = user_cfg.get("id") or extract_user_id(final_url) or extract_user_id(url) or url
        user_state = state["users"].get(user_id, {"living": False, "last_notify_time": 0})
        was_living = user_state.get("living", False)
        last_notify = user_state.get("last_notify_time", 0)
        log.info("[%s] %s: %s%s", now_str, nickname,
                 "直播中" if living else "未开播",
                 f" ({widget_text})" if widget_text else "")
        if living and not was_living:
            title = f"📢 {nickname} 开播啦"
            body = f"{nickname} 正在小红书直播\n时间: {now_str}\n主页: {final_url or url}"
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
        user_state["nickname"] = nickname
        user_state["note"] = widget_text if widget_text else ("正在直播" if living else "监控中")
        user_state["last_check"] = now_str
        user_state["url"] = final_url or url
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
