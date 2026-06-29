#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cupertino 网球场 — 可用监控 + ntfy 推送
================================================
不需要登入。流程：
  1) GET 落地页 → 拿匿名会话 cookie + 从 HTML 取 window.__csrfToken
  2) 带 cookie + X-CSRF-Token 头 POST 查可用接口
  3) 检查"未来 N 天"每个可订日的目标时段（工作日 20:00 / 周末 18:30）
  4) 有空场就用 ntfy 推送到你手机；你自己去订（答验证题 + 确认）

红线：本程序只读取公开可用数据并提醒，绝不自动下单、不自动答人工验证题、不绕过 reCAPTCHA。

用法：
  python monitor.py --check          只打印未来几天目标时段的可订情况（不推送）
  python monitor.py --test           发一条测试推送，确认 ntfy 通了
  python monitor.py --once           跑一轮检查（适合 cron 每 N 分钟调一次）
  python monitor.py --loop           常驻轮询（适合云服务器 / systemd）
  python monitor.py --snooze 2026-07-05   手动把某天静音（订到了就不用再提醒）
  python monitor.py --status         打印当前状态/静音的日期
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

PT = ZoneInfo("America/Los_Angeles")
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://anc.apm.activecommunities.com/cupertino"
LANDING = BASE + "/reservation/landing/quick?locale=en-US&groupId=1"
AVAIL = BASE + "/rest/reservation/quickreservation/availability?locale=en-US"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def log(msg):
    ts = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] {msg}", flush=True)


# ----------------------------- 配置 / 状态 -----------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 本地私密配置（gitignored）覆盖：放 ntfy_topic / control_topic
    local = os.path.join(os.path.dirname(os.path.abspath(path)), "secrets.json")
    if os.path.exists(local):
        try:
            with open(local, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"读 secrets.json 失败（忽略）：{e}")
    # 环境变量优先（GitHub Actions 用 repo secrets 注入）
    for env_key, cfg_key in (("NTFY_TOPIC", "ntfy_topic"),
                             ("NTFY_CONTROL_TOPIC", "control_topic"),
                             ("NTFY_SERVER", "ntfy_server")):
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]
    return cfg


def state_path(cfg):
    return os.path.join(HERE, cfg.get("state_file", "state.json"))


def load_state(cfg):
    p = state_path(cfg)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"dates": {}, "snoozed": {}, "control_since": None}


def save_state(cfg, st):
    with open(state_path(cfg), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def prune_state(st, today_iso):
    """删掉已过去日期的记录。"""
    for key in ("dates", "snoozed"):
        for d in list(st.get(key, {}).keys()):
            if d < today_iso:
                st[key].pop(d, None)


# ----------------------------- 站点客户端 -----------------------------

class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
        self.token = None

    def bootstrap(self):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        r = s.get(LANDING, timeout=20)
        r.raise_for_status()
        token = self._extract_token(r.text)
        if not token:
            raise RuntimeError("落地页里没找到 __csrfToken")
        self.session, self.token = s, token
        log("已建立匿名会话 + CSRF token")

    @staticmethod
    def _extract_token(html):
        for pat in (r'__csrfToken[^A-Za-z0-9]+([A-Za-z0-9-]{36})',
                    r'csrfToken["\'\s:=]+([0-9a-fA-F-]{36})'):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    def availability(self, date_iso):
        if self.session is None:
            self.bootstrap()
        body = {
            "facility_group_id": self.cfg["facility_group_id"],
            "customer_id": 0, "company_id": 0,
            "reserve_date": date_iso,
            "start_time": "08:00:00", "end_time": "21:30:00",
            "resident": True, "reload": False, "change_time_range": False,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": self.token,
            "page_info": '{"page_number":1,"total_records_per_page":20}',
        }
        j = self._post(headers, body)
        code = j.get("headers", {}).get("response_code")
        if code == "0012":  # Invalid CSRF -> 重新拿 token 再试一次
            log("CSRF 失效，重建会话")
            self.bootstrap()
            headers["X-CSRF-Token"] = self.token
            j = self._post(headers, body)
            code = j.get("headers", {}).get("response_code")
        if code != "0000":
            msg = j.get("headers", {}).get("response_message")
            raise RuntimeError(f"查可用 {date_iso} 返回 {code} {msg}")
        return j["body"]["availability"]

    def _post(self, headers, body):
        r = self.session.post(AVAIL, headers=headers, data=json.dumps(body), timeout=20)
        r.raise_for_status()
        return r.json()


# ----------------------------- 目标时段逻辑 -----------------------------

def target_slot(cfg, d):
    """周一~周五 -> 工作日时段；周六日 -> 周末时段。返回 'HH:MM:SS'。"""
    return cfg["weekday_slot"] if d.weekday() < 5 else cfg["weekend_slot"]


def find_openings(cfg, avail, d):
    """返回 (目标时段, [(court_id, court_name), ...] 可订的场地)。"""
    slots = avail.get("time_slots", [])
    want = target_slot(cfg, d)
    if want not in slots:
        return want, []
    idx = slots.index(want)
    flt = set(cfg.get("courts_filter") or [])
    out = []
    for res in avail.get("resources", []):
        if flt and res["resource_id"] not in flt:
            continue
        tsd = res.get("time_slot_details", [])
        if idx < len(tsd) and tsd[idx].get("status") == 1:
            out.append((res["resource_id"], res["resource_name"]))
    return want, out


def target_dates(cfg, today):
    start = 0 if cfg.get("include_today") else 1
    return [today + timedelta(days=n) for n in range(start, cfg["days_ahead"] + 1)]


def hhmm(t):  # "20:00:00" -> "8:00 PM"
    h, m, _ = t.split(":")
    h, m = int(h), int(m)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ap}"


# ----------------------------- ntfy 推送 -----------------------------

_PRIO = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def ntfy_publish(cfg, title, message, click=None, tags=None, actions=None, priority=None):
    topic = cfg.get("ntfy_topic")
    if not topic:
        raise RuntimeError("没配置 ntfy_topic（放 secrets.json，或设环境变量 NTFY_TOPIC）")
    payload = {"topic": topic, "title": title, "message": message}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    if actions:
        payload["actions"] = actions
    if priority:
        payload["priority"] = _PRIO.get(priority, priority) if isinstance(priority, str) else priority
    r = requests.post(cfg.get("ntfy_server", "https://ntfy.sh"), json=payload, timeout=15)
    if not r.ok:
        raise RuntimeError(f"ntfy {r.status_code}: {r.text[:200]}")


def snooze_action(cfg, date_iso):
    """通知里的按钮：点了就往控制 topic 发一条 'snooze <date>'，监控会读到并静音这天。"""
    ctl = cfg.get("control_topic")
    if not ctl:
        return None
    return {
        "action": "http",
        "label": f"搞定这天，别再提醒",
        "method": "POST",
        "url": f"{cfg.get('ntfy_server', 'https://ntfy.sh')}/{ctl}",
        "body": f"snooze {date_iso}",
        "clear": True,
    }


# ----------------------------- 控制 topic（订到即停） -----------------------------

def poll_control(cfg, st):
    """读控制 topic 的新消息，处理 'snooze YYYY-MM-DD'。"""
    ctl = cfg.get("control_topic")
    if not ctl:
        return
    since = st.get("control_since") or int(time.time())
    url = f"{cfg.get('ntfy_server', 'https://ntfy.sh')}/{ctl}/json"
    try:
        r = requests.get(url, params={"poll": "1", "since": str(since)}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log(f"读控制 topic 失败（忽略）：{e}")
        return
    newest = since
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        newest = max(newest, ev.get("time", newest))
        if ev.get("event") != "message":
            continue
        msg = (ev.get("message") or "").strip()
        m = re.match(r"snooze\s+(\d{4}-\d{2}-\d{2})", msg)
        if m:
            st["snoozed"][m.group(1)] = True
            log(f"已静音日期：{m.group(1)}（控制指令）")
    st["control_since"] = newest + 1


# ----------------------------- 一轮检查 -----------------------------

def run_once(cfg, client, st, notify=True):
    now = datetime.now(PT)
    today_iso = now.date().isoformat()
    prune_state(st, today_iso)
    poll_control(cfg, st)

    for d in target_dates(cfg, now.date()):
        diso = d.isoformat()
        if st["snoozed"].get(diso):
            continue
        try:
            avail = client.availability(diso)
        except Exception as e:
            log(f"{diso} 查可用出错：{e}")
            continue
        want, open_courts = find_openings(cfg, avail, d)
        wd = WEEKDAYS[d.weekday()]
        rec = st["dates"].setdefault(diso, {"alerted": False})

        if open_courts:
            names = ", ".join(n.split(" - ")[-1].replace(" Tennis Court", "")
                              for _, n in open_courts)
            log(f"{diso} {wd} {hhmm(want)} -> 可订 {len(open_courts)} 片：{names}")
            if notify and not rec["alerted"]:
                title = f"🎾 有空场 {wd} {d.strftime('%-m/%-d')} {hhmm(want)}"
                body = (f"{len(open_courts)} 片可订：{names}\n"
                        f"点开去订（自己答验证题 + 确认）")
                actions = [{"action": "view", "label": "打开订场页", "url": LANDING, "clear": True}]
                sa = snooze_action(cfg, diso)
                if sa:
                    actions.append(sa)
                ntfy_publish(cfg, title, body, click=LANDING, tags=["tennis"],
                             actions=actions, priority="high")
                rec["alerted"] = True
                log(f"  → 已推送 ntfy")
        else:
            log(f"{diso} {wd} {hhmm(want)} -> 无空场")
            rec["alerted"] = False  # 归零：下次再出现空场会重新提醒

    save_state(cfg, st)


def within_active_hours(cfg, now):
    return cfg["active_start_hour_pt"] <= now.hour < cfg["active_end_hour_pt"]


# ----------------------------- 入口 -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只打印可订情况，不推送")
    g.add_argument("--test", action="store_true", help="发一条测试推送")
    g.add_argument("--once", action="store_true", help="跑一轮（cron 用）")
    g.add_argument("--loop", action="store_true", help="常驻轮询")
    g.add_argument("--snooze", metavar="YYYY-MM-DD", help="手动静音某天")
    g.add_argument("--status", action="store_true", help="打印状态")
    args = ap.parse_args()

    cfg = load_config(args.config)
    st = load_state(cfg)

    if args.snooze:
        st["snoozed"][args.snooze] = True
        save_state(cfg, st)
        print(f"已静音 {args.snooze}")
        return
    if args.status:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return
    if args.test:
        ntfy_publish(cfg, "🎾 测试推送", "如果你在手机上看到这条，说明 ntfy 通了。",
                     click=LANDING, tags=["tennis"], priority="high")
        print(f"已发测试推送到 topic: {cfg['ntfy_topic']}")
        return

    client = Client(cfg)

    if args.check:
        run_once(cfg, client, st, notify=False)
        return
    if args.once:
        now = datetime.now(PT)
        if not within_active_hours(cfg, now):
            log(f"当前 {now.strftime('%H:%M %Z')} 不在监控时段 "
                f"[{cfg['active_start_hour_pt']}:00-{cfg['active_end_hour_pt']}:00 PT]，跳过")
            save_state(cfg, st)  # 保证 state.json 存在，供 CI 缓存
            return
        run_once(cfg, client, st, notify=True)
        return
    if args.loop:
        log("常驻轮询启动")
        while True:
            now = datetime.now(PT)
            if within_active_hours(cfg, now):
                try:
                    run_once(cfg, client, st, notify=True)
                except Exception as e:
                    log(f"本轮异常：{e}")
                time.sleep(cfg["poll_seconds"])
            else:
                time.sleep(300)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
