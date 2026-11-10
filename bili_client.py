import json
import os
import socket
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class DirectHTTPSConnection( urllib.request.HTTPSHandler ):
    """
    Bypasses local Clash/Mihomo Fake-IP (198.18.x.x) TLS EOF issues by resolving
    bilibili/hdslb domains against domestic public DNS (223.5.5.5 / 119.29.29.29)
    while preserving proper SNI for TLS handshake.
    """

    _dns_cache: Dict[str, str] = {}

    @classmethod
    def resolve_host(cls, host: str) -> str:
        if host in cls._dns_cache:
            return cls._dns_cache[host]
        # Query AliDNS DoH JSON API over direct IP 223.5.5.5 (validates IP SAN TLS cert)
        try:
            ctx = ssl.create_default_context()
            url = f"https://223.5.5.5/resolve?name={urllib.parse.quote(host)}&type=A"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ctx),
            )
            with opener.open(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1 and ans.get("data"):
                        ip = ans["data"]
                        if not ip.startswith("198.18."):
                            cls._dns_cache[host] = ip
                            return ip
        except Exception:
            pass
        # Fallback to system socket and always cache to avoid repeated DoH timeouts
        try:
            ip = socket.gethostbyname(host)
            cls._dns_cache[host] = ip
            return ip
        except Exception:
            cls._dns_cache[host] = host
            return host


class SNIHTTPSConnection(urllib.request.http.client.HTTPSConnection):
    def connect(self):
        real_ip = DirectHTTPSConnection.resolve_host(self.host)
        sock = socket.create_connection((real_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        server_hostname = self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


class SNIHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(SNIHTTPSConnection, req, context=self._context)


class BiliClient:
    """
    Unified client for Bilibili Prediction Plaza (/x/act-community/predict/*)
    and Esports Match Guess (/x/esports/guess/*).
    """

    PLAZA_REFERER = "https://www.bilibili.com/blackboard/era/FrfMUTqjlci54bkq.html"
    ESPORTS_REFERER = "https://www.bilibili.com/v/game/match/"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, cookie_path: Optional[Path] = None, use_doh_sni: bool = False):
        self.cookie_path = cookie_path or (Path(__file__).parent / "data" / "cookies.json")
        self.cookies: Dict[str, str] = {}
        self.load_cookies()
        if "buvid3" not in self.cookies:
            self.cookies["buvid3"] = f"{uuid.uuid4()}infoc".upper()
        if "b_nut" not in self.cookies:
            self.cookies["b_nut"] = str(int(time.time()))

        ctx = ssl.create_default_context()
        https_handler = SNIHTTPSHandler(context=ctx) if use_doh_sni else urllib.request.HTTPSHandler(context=ctx)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            https_handler,
        )

    def load_cookies(self) -> None:
        if self.cookie_path.exists():
            try:
                data = json.loads(self.cookie_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.cookies.update({str(k): str(v) for k, v in data.items() if v is not None})
            except Exception:
                pass

    def save_cookies(self, new_cookies: Dict[str, str]) -> None:
        sanitized = {
            str(k).replace("\r", "").replace("\n", "").strip(): str(v).replace("\r", "").replace("\n", "").strip()
            for k, v in new_cookies.items()
            if k and v is not None
        }
        self.cookies.update(sanitized)
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cookie_path.with_suffix(f"{self.cookie_path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(self.cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if os.name != "nt":
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
        os.replace(tmp_path, self.cookie_path)

    @property
    def is_logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA") and self.cookies.get("bili_jct"))

    @property
    def csrf(self) -> str:
        return self.cookies.get("bili_jct", "")

    def _cookie_header(self) -> str:
        parts = []
        for k, v in self.cookies.items():
            if not k or not v:
                continue
            ck = str(k).replace("\r", "").replace("\n", "").strip()
            cv = str(v).replace("\r", "").replace("\n", "").strip()
            if ck and cv:
                parts.append(f"{ck}={cv}")
        return "; ".join(parts)

    def request_json(
        self,
        url: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        referer: Optional[str] = None,
        retries: int = 6,
        timeout: float = 8.0,
    ) -> Dict[str, Any]:
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"

        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": referer or self.PLAZA_REFERER,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Cookie": self._cookie_header(),
        }

        body_bytes = None
        if method.upper() == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body_str = urllib.parse.urlencode(data or {})
            body_bytes = body_str.encode("utf-8")

        max_attempts = max(1, int(retries))
        last_err = None
        for attempt in range(max_attempts):
            try:
                req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
                with self.opener.open(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw)
            except Exception as e:
                last_err = e
                if attempt + 1 < max_attempts:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"Request failed after {max_attempts} retries ({url}): {last_err}")

    # =========================================================================
    # Account & Auth APIs
    # =========================================================================
    def get_nav_info(self) -> Dict[str, Any]:
        return self.request_json("https://api.bilibili.com/x/web-interface/nav")

    def get_coin_balance(self) -> float:
        res = self.request_json("https://account.bilibili.com/site/getCoin")
        if res.get("code") == 0 and res.get("data"):
            money = res["data"].get("money")
            if money is not None:
                return float(money)
        nav = self.get_nav_info()
        if nav.get("code") == 0 and nav.get("data"):
            return float(nav["data"].get("money") or 0.0)
        return 0.0

    # =========================================================================
    # Channel 1: Prediction Plaza (Gaming/Sports/Entertainment/Tech/ACG/Life)
    # =========================================================================
    def get_plaza_tags(self) -> List[Dict[str, Any]]:
        res = self.request_json("https://api.bilibili.com/x/act-community/predict/tags")
        if res.get("code") != 0:
            raise RuntimeError(f"get_plaza_tags error: {res}")
        return res.get("data", {}).get("list", [])

    def get_plaza_list(
        self,
        classify_id: int = 0,
        state_filter: int = 1,
        order_type: int = 0,
        scene: int = 1,
        group_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 30,
        displayed_list: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "scene": scene,
            "state_filter": state_filter,
            "displayed_list": displayed_list,
            "page": page,
            "page_size": page_size,
        }
        if scene == 1:
            payload["classify_id"] = classify_id
            payload["order_type"] = order_type
        elif scene == 2 and group_id is not None:
            payload["group_id"] = group_id

        res = self.request_json(
            "https://api.bilibili.com/x/act-community/predict/list",
            method="POST",
            data=payload,
        )
        if res.get("code") != 0:
            raise RuntimeError(f"get_plaza_list error: {res}")
        return res.get("data") or {"list": [], "has_more": False}

    def get_plaza_all_items(
        self,
        classify_id: int = 0,
        state_filter: int = 1,
        order_type: int = 0,
        expand_groups: bool = True,
        max_pages: int = 5,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Fetches all prediction items and topic groups under a classify_id.
        If expand_groups is True, automatically queries scene=2 for each topic group
        so no sub-questions inside a topic are missed.
        """
        predicts_by_id: Dict[int, Dict[str, Any]] = {}
        groups_by_id: Dict[int, Dict[str, Any]] = {}
        displayed: List[str] = []

        for page in range(1, max_pages + 1):
            data = self.get_plaza_list(
                classify_id=classify_id,
                state_filter=state_filter,
                order_type=order_type,
                scene=1,
                page=page,
                page_size=30,
                displayed_list=",".join(displayed),
            )
            items = data.get("list") or []
            for item in items:
                ct = item.get("card_type")
                if ct == 1 and item.get("predict"):
                    p = item["predict"]
                    pid = p["id"]
                    predicts_by_id[pid] = p
                    displayed.append(str(pid))
                elif ct == 2 and item.get("group"):
                    g = item["group"]
                    gid = g["id"]
                    groups_by_id[gid] = g
                    for sub in g.get("preview_predicts") or []:
                        if sub.get("id"):
                            displayed.append(str(sub["id"]))

            if not data.get("has_more") or not items:
                break

        if expand_groups:
            for gid, ginfo in groups_by_id.items():
                g_displayed: List[str] = []
                for gpage in range(1, 5):
                    gdata = self.get_plaza_list(
                        scene=2,
                        group_id=gid,
                        state_filter=state_filter,
                        page=gpage,
                        page_size=30,
                        displayed_list=",".join(g_displayed),
                    )
                    gitems = gdata.get("list") or []
                    for item in gitems:
                        if item.get("card_type") == 1 and item.get("predict"):
                            p = item["predict"]
                            p["_group_id"] = gid
                            p["_group_title"] = ginfo.get("title", "")
                            predicts_by_id[p["id"]] = p
                            g_displayed.append(str(p["id"]))
                    if not gdata.get("has_more") or not gitems:
                        break

        return list(predicts_by_id.values()), list(groups_by_id.values())

    def get_plaza_detail(self, predict_id: int) -> Dict[str, Any]:
        """Requires login cookie; returns full detail including settle_reason and coin_cnt per option."""
        res = self.request_json(
            "https://api.bilibili.com/x/act-community/predict/detail",
            params={"predict_id": predict_id},
        )
        return res

    def calc_plaza_award(self, predict_id: int, opt_idx: int, coins: int) -> float:
        """
        Official Bilibili payout estimator (works WITHOUT login!).
        Returns the predicted total coins received (including stake) if `coins` are bet on `opt_idx` right now.
        """
        res = self.request_json(
            "https://api.bilibili.com/x/act-community/predict/calc",
            params={"predict_id": predict_id, "opt_idx": opt_idx, "coins": coins},
        )
        if res.get("code") == 0 and res.get("data"):
            try:
                return float(res["data"].get("predicted", 0.0))
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    def place_plaza_bet(
        self,
        predict_id: int,
        opt_idx: int,
        coins: int,
        platform: str = "h5",
    ) -> Dict[str, Any]:
        """Places a coin bet on a Prediction Plaza item."""
        if not self.is_logged_in:
            raise RuntimeError("Not logged in! Run `python cli.py login` first.")
        payload = {
            "predict_id": predict_id,
            "opt_idx": opt_idx,
            "coins": coins,
            "platform": platform,
            "csrf": self.csrf,
        }
        for attempt in range(3):
            res = self.request_json(
                "https://api.bilibili.com/x/act-community/predict/bet",
                method="POST",
                data=payload,
                retries=1,
                timeout=12.0,
            )
            if res.get("code") == -702 and attempt < 2:
                time.sleep(3.5 * (attempt + 1))
                continue
            return res
        return res

    def get_plaza_mine_overview(self) -> Dict[str, Any]:
        return self.request_json("https://api.bilibili.com/x/act-community/predict/mine/overview")

    def get_plaza_mine_records(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        return self.request_json(
            "https://api.bilibili.com/x/act-community/predict/mine/records",
            params={"page": page, "page_size": page_size},
        )

    # =========================================================================
    # Channel 2: Esports Match Center (/x/esports/guess/*)
    # =========================================================================
    def get_esports_questions(self, pn: int = 1, ps: int = 50) -> List[Dict[str, Any]]:
        res = self.request_json(
            "https://api.bilibili.com/x/esports/guess/collection/question",
            params={"pn": pn, "ps": ps},
            referer=self.ESPORTS_REFERER,
        )
        if res.get("code") != 0:
            return []
        return res.get("data", {}).get("list", [])

    def place_esports_bet(
        self,
        contest_id: int,
        question_id: int,
        detail_id: int,
        coins: int,
    ) -> Dict[str, Any]:
        if not self.is_logged_in:
            raise RuntimeError("Not logged in! Run `python cli.py login` first.")
        payload = {
            "oid": contest_id,
            "main_id": question_id,
            "detail_id": detail_id,
            "count": coins,
            "is_fav": 0,
            "csrf": self.csrf,
        }
        return self.request_json(
            "https://api.bilibili.com/x/esports/guess/add",
            method="POST",
            data=payload,
            referer=self.ESPORTS_REFERER,
            retries=1,
            timeout=12.0,
        )
