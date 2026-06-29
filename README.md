# Cupertino 网球场 · 空场监控 + 手机推送

监控 Cupertino Sports Center 网球场（ActiveCommunities）未来 7 天的目标时段，
**有空场就推送到你手机**（ntfy）。你收到提醒后自己去订、自己答验证题、自己确认。

> 本工具**只读取公开的可用数据并提醒**。不自动下单、不自动答"人工验证/bot check"题、不绕过 reCAPTCHA。

目标时段：
- 周一~周五：**20:00–21:30**（8:00 PM）
- 周六/周日：**18:30–20:00**（6:30 PM）

---

## 1. 手机收推送

1. 手机装 **ntfy** App（iOS App Store / Android Play 商店搜 "ntfy"）
2. Subscribe to topic，服务器默认 `ntfy.sh`，topic 填你 `secrets.json` 里的 `ntfy_topic`
3. 允许通知

> topic 是随机串、相当于密码——别人不知道就收不到、也发不了你的提醒。

## 2. 本地跑（测试用）

```bash
pip install -r requirements.txt

python monitor.py --check     # 只打印未来 7 天目标时段可订情况（不推送）
python monitor.py --test      # 发一条测试推送
python monitor.py --once      # 跑一轮（按太平洋时间时段判断）
python monitor.py --loop      # 常驻轮询
python monitor.py --snooze 2026-07-05   # 手动静音某天（订到了就不用再提醒）
python monitor.py --status    # 看当前状态/静音的日期
```

配置见 `config.json`（时段、监控天数、轮询间隔、活跃时段等）。
ntfy topic 放在 `secrets.json`（已 gitignore，不进仓库）：

```json
{ "ntfy_topic": "你的topic", "control_topic": "你的topic-ctl" }
```

## 3. 部署到 GitHub Actions（免费 24/7）

> 用**公开仓库**才有无限免费分钟；topic 不进代码、放 GitHub Secrets。

1. 在 github.com 新建一个 **public** 仓库（如 `tennis-monitor`）
2. 把本目录推上去：
   ```bash
   git remote add origin https://github.com/<你的用户名>/tennis-monitor.git
   git push -u origin main
   ```
3. 仓库 **Settings → Secrets and variables → Actions → New repository secret**，加两个：
   - `NTFY_TOPIC` = 你的 ntfy_topic
   - `NTFY_CONTROL_TOPIC` = 你的 control_topic
4. **Actions** 标签页 → 若提示启用 workflow，点启用 → 选 `tennis-monitor` → **Run workflow** 手动触发一次，确认绿勾
5. 之后它每 5 分钟自动跑一轮（cron 是 UTC；脚本内部只在太平洋时间 8:00–21:00 工作）

> GitHub 定时任务高峰期可能延迟几分钟，属正常；要更准时就改用 VPS + systemd。

## 4. "订到了就别再提醒这天"

推送通知里有个 **"搞定这天，别再提醒"** 按钮，点一下会让监控静音那一天（通过控制 topic 传指令）。
也可以本地 `python monitor.py --snooze YYYY-MM-DD`。静音记录过了那天会自动清除。

## 5. Mac 预填助手（prefill.user.js）

收到提醒、在 Mac 上订场时，这个 userscript 自动把表单填好，**只留人工验证题给你本人答**。

安装（一次）：
1. Chrome 装 **Tampermonkey** 扩展
2. Tampermonkey → 新建脚本 → 粘贴 `prefill.user.js` 全部内容 → 保存
   （或把 `prefill.user.js` 拖进 Chrome，会提示安装）

用法：
1. 打开订场页，登入，选好日期
2. **点你要的空场格子**（这步是真实点击，脚本不替你点——网格只认真实事件）
3. 点 **Confirm bookings** → 弹出 Custom questions 时，脚本自动：
   勾 Singles + I agree + 90 分钟下拉选 "I agree"，并把光标停在验证题框
4. 你只需**答验证题**（如 first letter of "Friday" → F）→ 点 **Save**

红线：脚本**绝不自动答验证题、绝不自动 Save / 下单**。Event name 会自动填 "Tennis"。

## 已知边界

- 订场窗口 = 7 天；可用接口对超过 7 天的日期不拦（会显示全空），所以只盯到 +7。
- 8 点整"秒光"的热门新场，如果你在睡觉，等看到推送多半已被订走——本工具主要价值是**catch 取消/陆续放出的空场**。
- 真正下单 + 人工验证题，由你本人在浏览器/App 完成。
