# py12306 生产化改造（P0 交付）

> ⚠️ **风险提示（必读）**
> - 本工具**违反 12306 服务条款**，使用即承担**账号被封、订单被取消**的风险。
> - 12306 风控会识别高频请求；**即使做了指数退避和熔断，也无法保证不被封**。
> - 账号密码与实名信息由使用者自行提供；本工具不应经手存储明文（P0 已按此改造）。
> - **官方候补功能是更稳妥、且合规的选择**，应优先使用；脚本硬抢只作为最后手段。
> - 本文档不构成法律建议，合规性由使用者自行确认。

本仓库是 `spec.md` 第 11 节中 **P0 三项**的交付物：

| P0 项 | 交付内容 |
|---|---|
| Python 升级 + 依赖重整 | `Dockerfile`（3.11-slim）、`requirements.in` / `requirements-lock.txt` / `constraints-py311.txt`、`PIP_INDEX_URL` 可切换 |
| 风控熔断 + 告警 | `railkit/risk.py`（分类 → 退避 → 熔断 → 探针复归）、`railkit/timing.py`（独立抖动 / 指数退避）、`railkit/notifier.py`（统一接口 + 适配器，含风控告警） |
| 密钥环境变量化 + 日志脱敏 | `railkit/config.py`（启动即校验、失败关闭）、`railkit/redaction.py`（全局 redact）、`railkit/runtime_state.py`（登录态 AES-GCM + 0600 + 一键清除）、`.env.example` |

**未做（P1/P2/P3）**：compose 自带 Redis 与健康检查编排、候补购票、通知层全量迁移、Prometheus 指标、车站缓存。见文末「边界」。

---

## 1. 快速开始

### 1.1 Docker（推荐）

```bash
cp .env.example .env
# 至少填：USER_ACCOUNTS_JSON、REDIS_URL、JWT_SECRET_KEY、RUNTIME_ENC_KEY
chmod 600 .env

docker build -t py12306:p0 .
docker run --rm --env-file .env py12306:p0 -t           # 自检
docker run --rm --env-file .env -p 8008:8008 py12306:p0 # 常驻
```

国内网络切镜像源不用改文件：

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t py12306:p0 .
```

升级依赖验证（先跑不锁版本那一支）：

```bash
docker build --build-arg USE_LOCK=0 -t py12306:next .
docker run --rm py12306:next -t
```

### 1.2 本地

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-lock.txt              # 可复现
cp .env.example .env && $EDITOR .env
python main.py -t
```

### 1.3 自检能看到什么

`python main.py -t` 依次检查并按 `PASS/WARN/FAIL` 输出：

```
Python 版本 / 配置校验 / 密钥卫生 / 日志脱敏 / 目录与权限 / 登录态加密
Redis 连通性 / 通知适配器 / 风控熔断演练 / 抖动间隔抽样
```

退出码：`0` 通过（可以有 WARN），`1` 有 FAIL —— 可直接用于容器 `HEALTHCHECK`。

其他开关：

```bash
python main.py -t --offline        # 不连 Redis
python main.py -t --json           # 机器可读
python main.py --health-only       # 健康检查用的最小自检
python main.py -t --notify-test    # 实发一条测试通知，验证告警链路
python main.py --purge-login-state # 一键清除登录态
```

---

## 2. P0-1：环境现代化

### 2.1 变更点

| 项 | 改造前 | 改造后 |
|---|---|---|
| 基础镜像 | `python:3.6.6-slim` | `python:3.11-slim` |
| Python 生命周期 | 3.6 已 EOL 多年 | 3.11（安全补丁到 2027-10） |
| 系统依赖 | 缺字体等 | `libxml2-dev` `libxslt1-dev` `gcc` `fonts-noto-cjk`（缺 CJK 字体中文验证码识别率会暴跌）`curl` `ca-certificates` `tzdata` |
| 运行用户 | root | 非 root（uid/gid 10001） |
| 日志缓冲 | 依赖 `-u` | `PYTHONUNBUFFERED=1` 显式声明 |
| pip 源 | 硬编码清华源 | `PIP_INDEX_URL` 环境变量，默认走官方源 |
| 依赖表达 | 一份锁死版本的清单 | 三层：`requirements.in`（直接依赖+区间）/ `requirements-lock.txt`（精确锁）/ `constraints-py311.txt`（3.11 兼容上限） |

### 2.2 三层依赖怎么用

- **`requirements.in`** —— 只写直接依赖和较宽区间。升级验证时看这个文件。
- **`constraints-py311.txt`** —— 只写「上限」，并解释每条为什么有上限（`lxml<6` 是因为 6.x 要求 3.13；`cssselect<2` 是因为接口重写等）。
- **`requirements-lock.txt`** —— 本轮实际验证过的精确版本。

锁文件生成方式（已执行，不要再手改版本号）：

```bash
pip install --dry-run --ignore-installed --python-version 3.11 --only-binary=:all: \
  --report report.json -r requirements.in -c constraints-py311.txt
```

**验证结果（本轮）**：

- 锁文件里每一个版本都确认在 PyPI 上有 `cp311` 的 manylinux / musllinux wheel，或纯 Python wheel → `python:3.11-slim` 上**不会触发源码编译**。
- 唯一例外 `pyppeteer-box==0.0.27`：PyPI 上只有 sdist（无 wheel），必须源码安装；它自身无 C 扩展，装得上。
- 在干净虚拟环境里 `pip install -r requirements-lock.txt` 全流程跑通（42 个包）。

### 2.3 升级时的已知风险（按规格书 1.2）

- `redis` 3.0.1 → 5.3.1：连接池与 pipeline 行为有变化，**接入业务代码时要实测**。
- `requests-html` 依赖的 `pyppeteer` 已长期停更：P0 先钉在 `requests-html==0.10.0` + `pyppeteer==2.0.0` 保证能跑；**迁 Playwright 属于后续动作**，不要和本次升级混在一起。
- `pyppeteer-box` 是第三方 fork（0.0.26/0.0.27 之后无新版）：若继续不可用，验证码方案要换。
- `Jinja2` / `MarkupSafe` / `itsdangerous` 已从 2018 年的锁死版本放开（2.10/1.1.0 → 3.1.6/3.0.3/2.2.0），Web 模板渲染需要回归一次。

---

## 3. P0-2：风控熔断 + 告警

### 3.1 状态机

```
CLOSED ──连续 N 次 5xx/超时──▶ BACKOFF ──等待结束──▶ CLOSED
   │                              （3s→6s→12s…封顶 120s，带抖动）
   └──命中风控特征──────────────▶ OPEN ──冷却结束──▶ HALF_OPEN ──探针成功──▶ CLOSED
                                   │    （30s→60s…封顶 30min）        │
                                   └──────────探针失败────────────────┘
```

粒度是 **「任务 × 日期 × 车站组合」**（`railkit/risk.RiskBreaker`）。撞墙的往往只是一个组合，全局熔断会误伤其他正常组合。

### 3.2 风控特征识别

`classify_response(status, headers, body)` 是纯函数，覆盖：

| 输入 | 判定 |
|---|---|
| 5xx | `server` → 计入连续异常，退避 |
| 超时 / 连接错误 / TLS 错误 | `transport` → 同上 |
| 401 / 302 | `auth` → **不计入熔断**（该重新登录，撞墙没用），但立刻告警 |
| 403 / 429 | `rate_limit` → 立即熔断 |
| **HTTP 200 + 正文含「您的访问过于频繁」等** | `risk_control` → 立即熔断（12306 最常见的形态） |
| 响应头 `X-Risk-Control` / `X-Captcha-Challenge` | `risk_control` |
| `Retry-After` 响应头 | 熔断等待取 `max(指数退避, Retry-After)`，并受 `max_wait_seconds` 约束 |

特征串默认内置（`DEFAULT_RISK_PATTERNS`），可通过 `RISK_CONTROL_PATTERNS` 覆盖；正文只扫前 4KB，不拖慢主循环。

### 3.3 抖动必须逐组合独立

规格书 5.1 的关键点：多任务/多日期之间也要抖动，否则「同步抖动 = 没有抖动」。

```python
stream = new_stream("task-1|2026-10-01|北京-上海")   # 由 key 派生确定性随机流
delay = next_query_delay(timing, pre_sale=False, stream=stream, override=4.0)
```

同一个组合可复现，不同组合互不相关（单测 `test_different_keys_do_not_synchronize` 保证）。

### 3.4 告警

`railkit/notifier.py` 立起了规格书第 4 节的接口：

```python
class Adapter:
    name: str
    events: tuple[str, ...]          # 只订阅关心的事件，可单独启停
    def send(self, message, timeout) -> SendResult: ...
```

- 事件枚举化：`TICKET_SUCCESS` / `NO_TICKET` / `LOGIN_EXPIRED` / `CAPTCHA_FAILED` / `RISK_CONTROL` / `TICKET_ALL_FAILED` / `TASK_ERROR` / `SYSTEM`
- 适配器：`console`（默认，零配置）、`dingtalk`（含加签）、`serverchan`、`bark`、`webhook`、`memory`（测试用）
- **一个适配器失败不影响其他**；失败重试（指数退避）+ 连续失败后进入冷却；通知失败永远不会影响主流程
- 只依赖标准库发 HTTP，测试可完全离线

### 3.5 怎么接进查询循环

```python
from railkit.risk import BreakerRegistry, classify_exception, classify_response
from railkit.notifier import NotifyHub, make_notifier

hub = NotifyHub.from_env()
registry = BreakerRegistry(max_keys=5, notifier=make_notifier(hub))

breaker = registry.get("%s|%s|%s-%s" % (task_id, date, from_station, to_station))
decision = breaker.before_query(pre_sale=is_pre_sale_window())
if not decision.allow:
    time.sleep(min(decision.wait_seconds, 60))    # 别把主循环卡死
    continue

try:
    resp = session.get(QUERY_URL, params=params, timeout=15)
except requests.RequestException as exc:
    detail = classify_exception(exc)
    breaker.report_failure(detail.category, detail.reason)
else:
    result = classify_response(resp.status_code, resp.headers, resp.text)
    if result.ok:
        breaker.report_success()
    else:
        breaker.report_failure(result.category, result.reason, retry_after=result.retry_after)
```

`breaker.snapshot()` 直接给 Web 界面用：当前状态、上次查询距今、连续失败次数、是否已熔断、下次探针时间、分类计数（规格书第 7 条要求的状态字段全在里面）。

---

## 4. P0-3：密钥环境变量化 + 日志脱敏

### 4.1 配置

- **`env.py` 不再承载密钥**，改为环境变量；`settings.py` 只留非敏感常量。
- 兼容迁移：`LOAD_LEGACY_ENV=1` 时可以从旧 `env.py` 读取密钥，并**告警提醒删除**（默认关闭）。
- **启动即校验，失败关闭**：字段名写错、区间格式错、JWT 密钥缺失/过短、`WEB_BIND=0.0.0.0` 却没配 IP 白名单、启用 dingtalk 却没给 webhook……都会**一次性列出所有问题并拒绝启动**，不再静默失效。
- 部分校验示例：
  - `QUERY_INTERVAL < 1` 直接失败；`1 <= x < 3` 通过但告警「太激进」
  - `JWT_SECRET_KEY` 必须 >=32 字符（`DEV_MODE=1` 才放宽）
  - 跨零点时段 `22:00-06:00` 正确解析（`in_period()`，修掉规格书 5.4 的字符串比较 bug）
- `Config.scrub()` 给出可安全打印/上报的视图；`Secret` 类型在 `str()`/`repr()` 里只显示 `<secret>`，取明文必须显式 `.reveal()`。

### 4.2 登录态落盘

`railkit/runtime_state.py`：

- AES-GCM 加密（key 由 `RUNTIME_ENC_KEY` 经 scrypt 派生；遇到 OpenSSL 内存上限时自动降级 PBKDF2-HMAC-SHA256 600k 次）
- 原子写 + 收紧权限（POSIX `0600`；Windows 用 `icacls` 去继承、只留当前用户）
- **失败关闭**：没配 `RUNTIME_ENC_KEY` 就拒绝明文落盘，除非显式 `ALLOW_PLAINTEXT_STATE=1`
- 写入加密态时自动清理同名明文文件
- `python main.py --purge-login-state` 一键清除

### 4.3 脱敏

`railkit/redaction.py` + `install_everywhere()`（挂在 root 和所有 handler 上，不是只挂 logger——否则通过 propagate 转发的日志不会被过滤）。

覆盖：身份证（18/15 位）、手机号（含 `+86`、含 `138****5678` 形式）、邮箱、`password`/`token`/`api_key` 等具名赋值、Cookie（`JSESSIONID`/`RAIL_DEVICEID`… 保留 cookie 名、值打掉）、JSON 里的 `cookie`、长不透明串（>=32 位含字母+数字，或 >=30 位纯十六进制）。

三个容易踩的坑（都有单测锁住）：

1. 通用长串规则**不能**只按长度匹配 —— `RAIL_DEVICEID` 这种 15 位驼峰词会被误伤，结果反而看不到是哪个 cookie 泄漏。
2. 规则顺序上**身份证必须在手机号之前** —— 身份证里有 11 位数字片段，先匹配手机号会把尾号切碎。
3. 替换日志参数时**必须重新包成元组**，否则 `msg % args` 会抛 `TypeError` 把日志打崩；`%s` 占位符也不能被当成密码值替换。

另外支持注册「已知密钥明文」（配置里的真实密码/token 出现在任何位置都会被替换），以及 `LOG_REDACT_EXTRA` 自定义正则。

---

## 5. 测试

```bash
pip install -r requirements-lock.txt
pytest -q                                  # 全部离线：不连网、不连 Redis、不 sleep
pytest -q --basetemp=.pytest-tmp           # 受限沙箱/临时目录写不了时用这个
```

当前 **211 个用例全部通过**，覆盖：

| 文件 | 覆盖 |
|---|---|
| `tests/test_timing.py` | 抖动区间上下界、逐组合独立、开售前激进窗、指数退避阶梯（含 30s→30min）、`Retry-After` 解析、**跨零点时段判断** |
| `tests/test_risk.py` | 响应/异常分类（含 200+风控串）、退避触发与恢复、熔断立即触发、探针单飞、探针成功复归、探针失败翻倍、软信号累计与衰减、`auth`/`captcha` 不计入熔断、告警事件、通知失败不影响熔断、组合数上限、快照字段 |
| `tests/test_redaction.py` | 各类密钥脱敏、误伤防护、自定义正则、logging Filter（含 dict/单值 args） |
| `tests/test_config.py` | 账号解析与报错、字段名写错即报错、时段解析、密钥长度、公网绑定需白名单、`.env` 优先级、旧 `env.py` 迁移、`scrub()` 不泄漏、布尔/数字校验 |
| `tests/test_security.py` | 登录态加解密往返、篡改检测、明文拒绝、一键清除、通知重试与冷却、适配器隔离 |

时序类测试全部用**假时钟**，不 `sleep@@，所以跑得快且不 flaky。

---

## 6. 目录结构

```
Dockerfile                  # P0-1
requirements.in             # 直接依赖 + 宽区间
requirements-lock.txt       # 已验证的精确版本
constraints-py311.txt       # 3.11 兼容上限（含每条的理由）
main.py                     # 入口：-t 自检 / --purge-login-state / --health-only
settings.py                 # 非敏感常量
.env.example                # 所有环境变量说明，不含真实密钥
railkit/
  config.py                 # 环境变量配置 + 启动即校验
  redaction.py              # 日志脱敏
  notifier.py               # 通知接口 + 适配器
  timing.py                 # 抖动 / 退避纯函数
  risk.py                   # 风控分类 + 熔断器
  runtime_state.py          # 登录态加密落盘
  selfcheck.py              # 自检实现
tests/                      # 离线用例
```

---

## 7. 边界（务必知悉）

本次改造**没有修改上游业务代码**，原因如实说明：本次执行环境**无法访问 GitHub**（`git clone pjialin/py12306` 超时），工作目录里也没有上游代码副本。因此 P0 以「可独立验证的模块 + 部署/配置文件」形式交付，接入上游需要三步：

1. 把 `railkit/`、`main.py`、`settings.py`、`Dockerfile`、三份 requirements、`.env.example` 放进仓库；
2. 应用启动处调用一次 `railkit.redaction.install_everywhere()`，并把业务里的 logging 初始化换成读取 `Config.logs`；
3. 按第 3.5 节把查询循环里的「sleep + requests」换成 `RiskBreaker` 的 `before_query` / `report_*`。

`Dockerfile` 里的 `COPY . /app` 与 `ENTRYPOINT ["python", "main.py"]` 目前指向本交付物的入口；上游 `main.py` 合入后按上游命令调整 `ENTRYPOINT` 与 `HEALTHCHECK`。

**下一优先级（P1）**：compose 自带 `redis:7-alpine --appendonly yes` + 健康检查 + 数据卷分离；自适应抖动接进真实查询循环；候补购票模式。
