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
    for key in ("dates", "snoozed", "counts"):
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
        # status: 0 = 可订(白格), 1 = 不可订(灰格/已订/超窗)。已逐格与 DOM 比对确认。
        if idx < len(tsd) and tsd[idx].get("status") == 0:
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


# ----------------------------- 心跳 / 报错告警 -----------------------------

def maybe_heartbeat(cfg, st, now):
    """每天 19 点后发一条低优先级心跳，让用户知道监控活着、今天查了几轮。"""
    today_iso = now.date().isoformat()
    if now.hour < 19 or st.get("heartbeat_date") == today_iso:
        return
    n = st.get("counts", {}).get(today_iso, 0)
    try:
        ntfy_publish(cfg, "✅ 监控正常",
                     f"今天已检查 {n} 轮。空场一出现会立刻高优先级提醒你。",
                     priority="low", tags=["hourglass_done"])
        st["heartbeat_date"] = today_iso
        log(f"已发心跳（今天第 {n} 轮）")
    except Exception as e:
        log(f"心跳发送失败（忽略）：{e}")


def maybe_error_alert(cfg, st, today_iso):
    """连续 ≥5 次查询失败时告警（每天最多一条），网站改接口/拦截能及时知道。"""
    if st.get("consec_errors", 0) < 5 or st.get("error_alert_date") == today_iso:
        return
    try:
        ntfy_publish(cfg, "⚠️ 监控在报错",
                     "连续 5 次查询失败，网站可能改了接口或在拦截，需要人工看一眼。",
                     priority="high", tags=["warning"])
        st["error_alert_date"] = today_iso
    except Exception as e:
        log(f"错误告警发送失败：{e}")


# ----------------------------- 一轮检查 -----------------------------

REALERT_COOLDOWN = 45 * 60  # 同一天出现"新场地"时的重推冷却


def run_once(cfg, client, st, notify=True):
    now = datetime.now(PT)
    today_iso = now.date().isoformat()
    prune_state(st, today_iso)
    poll_control(cfg, st)

    counts = st.setdefault("counts", {})
    counts[today_iso] = counts.get(today_iso, 0) + 1

    for d in target_dates(cfg, now.date()):
        diso = d.isoformat()
        if st["snoozed"].get(diso):
            continue
        try:
            avail = client.availability(diso)
            st["consec_errors"] = 0
        except Exception as e:
            log(f"{diso} 查可用出错：{e}")
            st["consec_errors"] = st.get("consec_errors", 0) + 1
            maybe_error_alert(cfg, st, today_iso)
            continue
        want, open_courts = find_openings(cfg, avail, d)
        wd = WEEKDAYS[d.weekday()]
        rec = st["dates"].setdefault(diso, {"alerted": False, "courts": [], "last_alert": 0})

        if open_courts:
            ids = sorted(cid for cid, _ in open_courts)
            names = ", ".join(n.split(" - ")[-1].replace(" Tennis Court", "")
                              for _, n in open_courts)
            log(f"{diso} {wd} {hhmm(want)} -> 可订 {len(open_courts)} 片：{names}")
            # 推送条件：这波空场还没提醒过；或开出了新场地且距上次提醒超过冷却时间
            new_courts = [c for c in ids if c not in rec.get("courts", [])]
            cooled = time.time() - rec.get("last_alert", 0) > REALERT_COOLDOWN
            if notify and (not rec["alerted"] or (new_courts and cooled)):
                title = f"🎾 有空场 {wd} {d.strftime('%-m/%-d')} {hhmm(want)}"
                body = (f"{len(open_courts)} 片可订：{names}\n"
                        f"点开去订（自己答验证题 + 确认）")
                actions = [{"action": "view", "label": "打开订场页", "url": LANDING, "clear": True}]
                sa = snooze_action(cfg, diso)
                if sa:
                    actions.append(sa)
                ntfy_publish(cfg, title, body, click=LANDING, tags=["tennis"],
                             actions=actions, priority="max")
                rec["alerted"] = True
                rec["last_alert"] = time.time()
                log(f"  → 已推送 ntfy")
            rec["courts"] = ids
        else:
            log(f"{diso} {wd} {hhmm(want)} -> 无空场")
            rec["alerted"] = False  # 归零：下次再出现空场会重新提醒
            rec["courts"] = []

    maybe_heartbeat(cfg, st, now)
    save_state(cfg, st)


def within_active_hours(cfg, now):
    return cfg["active_start_hour_pt"] <= now.hour < cfg["active_end_hour_pt"]


# ----------------------------- CI 长跑模式 -----------------------------

RELEASE_START = 7 * 60 + 58   # 07:58 PT，提前候场
RELEASE_END = 8 * 60 + 20     # 08:20 PT，冲刺结束
RELEASE_INTERVAL = 20         # 冲刺期间每 20 秒查一轮


def ci_loop(cfg, client, st, max_seconds):
    """GitHub Actions 长跑：一次触发内部持续轮询，把稀疏的免费调度摊成全天覆盖。
    - 07:58–08:20 PT 放场冲刺：每 20 秒一轮（第 7 天的新场就是早 8 点放出来的）
    - 其余 8:00–21:00：每 poll_seconds 一轮
    - 过 21:00 收工；距开窗太远则直接退出把机会留给下一班
    """
    start = time.time()
    end_min = cfg["active_end_hour_pt"] * 60
    log(f"CI 长跑启动（上限 {max_seconds // 60} 分钟）")
    while True:
        remaining = max_seconds - (time.time() - start)
        if remaining <= 0:
            log("到达本班时长上限，收工（下一班接力）")
            break
        now = datetime.now(PT)
        mins = now.hour * 60 + now.minute
        if mins >= end_min:
            log(f"已过 {cfg['active_end_hour_pt']}:00 PT，今天收工")
            break
        if mins < RELEASE_START:
            secs_to_window = (RELEASE_START - mins) * 60
            if secs_to_window > remaining:
                log("距开窗太远，本班退出，等下一班")
                break
            nap = min(600, secs_to_window)
            log(f"未到监控时段，睡 {nap // 60} 分钟候场")
            time.sleep(nap)
            continue
        try:
            run_once(cfg, client, st, notify=True)
        except Exception as e:
            log(f"本轮异常：{e}")
        in_release = RELEASE_START <= mins < RELEASE_END
        interval = RELEASE_INTERVAL if in_release else cfg.get("poll_seconds", 150)
        time.sleep(max(1, min(interval, max_seconds - (time.time() - start))))
    save_state(cfg, st)


# ----------------------------- 入口 -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只打印可订情况，不推送")
    g.add_argument("--test", action="store_true", help="发一条测试推送")
    g.add_argument("--once", action="store_true", help="跑一轮（cron 用）")
    g.add_argument("--loop", action="store_true", help="常驻轮询")
    g.add_argument("--ci-loop", type=int, metavar="SECONDS",
                   help="CI 长跑模式：单次进程内持续轮询，最多 SECONDS 秒")
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
        topic = cfg.get("ntfy_topic", "")
        print(f"已发测试推送（topic {topic[:10]}…，已隐去）")
        return

    client = Client(cfg)

    if args.check:
        run_once(cfg, client, st, notify=False)
        return
    if args.ci_loop:
        ci_loop(cfg, client, st, args.ci_loop)
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
