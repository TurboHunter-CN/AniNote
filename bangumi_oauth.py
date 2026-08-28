# -*- coding: utf-8 -*-
"""
Bangumi OAuth 授权与剧集标记（双向同步）。

授权码模式流程:
1. 打开授权页 https://bgm.tv/oauth/authorize
2. 用户同意后重定向到 http://127.0.0.1:18521/callback?code=xxx
3. 本地临时 HTTP 服务接收 code（60 秒有效）
4. 用 code 换取 access_token + refresh_token
5. 访问需授权 API 时带 Authorization: Bearer <token>
6. token 过期前自动用 refresh_token 续期

前置条件: 发布者需在 Bangumi 开发者平台注册应用，获得 client_id / client_secret
（回调地址填 http://127.0.0.1:18521/callback），并填写到控制面板。
"""

import json
import os
import threading
import time
import webbrowser

import requests

AUTH_URL = "https://bgm.tv/oauth/authorize"
TOKEN_URL = "https://bgm.tv/oauth/access_token"
REDIRECT_PORT = 18521
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
UA = "HunterHasCome/AniNote/4.3.0 (https://github.com/TurboHunter-CN/AniNote)"

# Bangumi 剧集收藏类型（EpisodeCollectionType，实测确认）
# 注意：对已「看过」(2) 的集，只有降回 0（未收藏）才会真正撤销；1（想看）会被忽略
EP_COLLECT_TYPE_NOT_COLLECTED = 0   # 未收藏（撤销"看过"用这个）
EP_COLLECT_TYPE_WATCHLIST = 1       # 想看
EP_COLLECT_TYPE_WATCHED = 2         # 看过
EP_COLLECT_TYPE_DISCARDED = 3       # 抛弃

# 内置应用凭证（作者注册，供所有用户免注册直接授权）。
# 说明：OAuth 授权码模式下 access_token 归各用户授权后存于本机，secret 仅作应用身份标识，
DEFAULT_CLIENT_ID = "bgm69226a8530a0beef3"
DEFAULT_CLIENT_SECRET = "ff1e62344f711fba6809d088c7a598c8"

# 配置持久化回调（由 main.py 注入 save_config，token 续期成功后落盘）
_config_saver = None


def set_config_saver(fn):
    """注入配置保存回调（main.py 传入 save_config）。"""
    global _config_saver
    _config_saver = fn


def _persist_cfg(cfg):
    if _config_saver is not None:
        try:
            _config_saver(cfg)
        except Exception:
            pass


def _creds(oauth):
    """返回 (client_id, client_secret)，配置缺失时回退内置凭证。"""
    return (
        oauth.get("client_id") or DEFAULT_CLIENT_ID,
        oauth.get("client_secret") or DEFAULT_CLIENT_SECRET,
    )


def _proxies(proxy_str=""):
    proxy_str = (proxy_str or "").strip()
    if not proxy_str:
        return None
    if "://" not in proxy_str:
        proxy_str = f"http://{proxy_str}"
    return {"http": proxy_str, "https": proxy_str}


# ---------- 配置存取 ----------

def load_oauth(cfg):
    """从配置字典读取 bangumi_oauth 字段。"""
    return dict(cfg.get("bangumi_oauth", {}) or {})


def save_oauth(cfg, oauth):
    """写回 bangumi_oauth 字段（不负责保存配置文件）。"""
    cfg["bangumi_oauth"] = oauth


# ---------- 授权流程 ----------

def build_authorize_url(client_id, state=""):
    """构造授权页 URL。"""
    from urllib.parse import quote
    return (f"{AUTH_URL}?client_id={quote(str(client_id))}"
            f"&response_type=code&redirect_uri={quote(REDIRECT_URI, safe='')}"
            f"&state={quote(state)}")


def start_callback_server(timeout=60, cancel_event=None):
    """线程中起本地 HTTP 服务等待授权回调，返回 code 或 None（超时/取消/失败）。

    接受 cancel_event：外部 set() 时立即关闭服务并返回 None，便于"取消授权"按钮响应。
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    result = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            result["code"] = qs.get("code", [""])[0]
            ok = bool(result["code"])
            body = (
                "<html><head><meta charset='utf-8'></head><body style='font-family: sans-serif;"
                " text-align: center; padding-top: 60px;'>"
                f"<h3 style='color: {'#2e7d32' if ok else '#c62828'};'>"
                f"{'✅ 授权成功，可以关闭此页面并返回 AniNote' if ok else '❌ 授权失败（缺少 code）'}"
                "</h3></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _Handler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    elapsed = 0
    try:
        while not done.is_set():
            if cancel_event is not None and cancel_event.is_set():
                return None
            if done.wait(1):
                break
            elapsed += 1
            if elapsed >= timeout:
                return None
        return result.get("code") or None
    finally:
        try:
            server.server_close()
        except Exception:
            pass


def exchange_code(code, client_id, client_secret, proxy_str=""):
    """用授权码换取 token。成功返回 dict，失败抛异常。"""
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"User-Agent": UA},
        proxies=_proxies(proxy_str), timeout=20,
    )
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token, client_id, client_secret, proxy_str=""):
    """用 refresh_token 续期。成功返回 dict，失败抛异常。"""
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"User-Agent": UA},
        proxies=_proxies(proxy_str), timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ---------- Token 生命周期 ----------

def get_valid_token(cfg, proxy_str=""):
    """返回可用 access_token（必要时自动刷新）；未授权/刷新失败返回 None。

    - expires_at 缺失的旧授权数据也会尝试刷新（过期是常态，不依赖字段存在）
    - 刷新成功后写回 cfg（save_oauth）并落盘（set_config_saver 注入的回调）
    - 刷新失败打印原因并返回 None，调用方应提示用户重新授权
    """
    oauth = load_oauth(cfg)
    token = oauth.get("access_token", "")
    if not token:
        return None
    exp = oauth.get("expires_at", 0)
    now = time.time()
    # 缺失 expires_at 或已临期/过期（提前 5 分钟）→ 尝试用 refresh_token 续期
    if (not exp) or (now > exp - 300):
        cid, sec = _creds(oauth)
        rf = oauth.get("refresh_token", "")
        if cid and sec and rf:
            try:
                data = refresh_access_token(rf, cid, sec, proxy_str)
                oauth["access_token"] = data.get("access_token", token)
                oauth["refresh_token"] = data.get("refresh_token", rf)
                oauth["expires_at"] = int(time.time()) + int(data.get("expires_in", 604800))
                # 写回 cfg 并落盘，避免重启后仍用旧 token
                save_oauth(cfg, oauth)
                _persist_cfg(cfg)
            except Exception as e:
                print(f"[AniNote] Bangumi token 刷新失败: {e}")
                return None
        elif exp and now > exp:
            # 已过期且缺少 refresh_token/凭证 → 无法自动续期，明确失败提示重新授权
            return None
        # exp 缺失且无法续期：无法判断是否过期，交给 API 决定（兼容令牌登录等场景）
    return oauth.get("access_token")


# ---------- 剧集回写 ----------

def mark_episodes_watched(subject_id, episode_ids, cfg, proxy_str="", watched=True):
    """批量标记某条目下若干剧集为「看过」（type=2）或「撤销看过」（type=0）。

    Args:
        watched: True 标记看过；False 撤销看过（type=0「未看」）。
                 ⚠️ 实测确认：type=1（想看）对已「看过」的集无效，
                 Bangumi 不接受从看过降到想看，必须用 type=0。

    Returns: (True, "") 成功；(False, 错误信息) 失败。
    """
    if not episode_ids:
        return False, "没有可标记的剧集"
    token = get_valid_token(cfg, proxy_str)
    if not token:
        return False, "尚未授权 Bangumi，请先在控制面板完成授权"
    try:
        r = requests.patch(
            f"https://api.bgm.tv/v0/users/-/collections/{subject_id}/episodes",
            json={"episode_id": [int(x) for x in episode_ids],
                  "type": EP_COLLECT_TYPE_WATCHED if watched else EP_COLLECT_TYPE_NOT_COLLECTED},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": UA,
            },
            proxies=_proxies(proxy_str), timeout=20,
        )
        if r.status_code == 204:
            return True, ""
        return False, f"标记失败: HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:
        return False, f"标记失败: {type(e).__name__}: {e}"


# ---------- 剧集读取（精确每集状态，需 token）----------

def fetch_episode_collection(subject_id, cfg, proxy_str=""):
    """获取当前用户在某个条目下的每集收藏状态（type=2 看过）。

    Returns: {episode_id: type}；失败返回 None。
    """
    token = get_valid_token(cfg, proxy_str)
    if not token:
        return None
    try:
        out = {}
        offset = 0
        while True:
            r = requests.get(
                f"https://api.bgm.tv/v0/users/-/collections/{subject_id}/episodes",
                params={"offset": offset, "limit": 100},
                headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                proxies=_proxies(proxy_str), timeout=20,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            for ep in data.get("data", []):
                # 实测结构：{episode: {id, sort, ...}, type, updated_at}
                ep_obj = ep.get("episode") or {}
                out[ep_obj.get("id")] = ep.get("type", 0)
            total = data.get("total", 0)
            offset += len(data.get("data", []))
            if offset >= total or not data.get("data"):
                break
        return out
    except Exception:
        return None
