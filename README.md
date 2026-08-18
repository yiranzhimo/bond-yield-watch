# Bond Watch · 中美日国债监控

监控中国、美国、日本三国主权债收益率曲线、期限结构、跨国利差与异动告警。跑在 GitHub Actions 上，每交易日更新，产出静态看板、JSON/CSV 数据、README 摘要与可选邮件日报。

数据源全部免费公开，**不需要任何 API key**。

<!-- SNAPSHOT:START -->

**数据日期 2026-08-17** · 更新于 2026-08-18T09:39:55 UTC

| 市场 | 2Y | 5Y | 10Y | 30Y | 10Y 1日 | 10Y-2Y | 曲线 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 中国国债 | 1.230% | 1.389% | 1.692% | 2.162% | -0.4bp | 46.3bp | 正常 |
| 美国国债 | 4.190% | 4.380% | 4.720% | 5.310% | +4.0bp | 53.0bp | 正常 |
| 日本国债 | 1.696% | 2.177% | 2.919% | 4.050% | +4.1bp | 122.3bp | 陡峭 |

**跨国利差**

- 中美 10Y 利差：-302.8bp（1日 -4.4bp，两年分位 2）
- 日美 10Y 利差：-180.1bp（1日 +0.1bp，两年分位 95）
- 中日 10Y 利差：-122.7bp（1日 -4.5bp，两年分位 0）
- 中美 2Y 利差：-296.1bp（1日 -2.0bp，两年分位 12）

**异动告警**：当前无触发项。

<!-- SNAPSHOT:END -->

## 数据源

| 市场 | 来源 | 期限 | 说明 |
| --- | --- | --- | --- |
| 中国 | [东方财富数据中心](https://data.eastmoney.com/cjsj/zmgzsyl.html) | 2Y / 5Y / 10Y / 30Y | 日频 |
| 美国 | 东方财富 + [FRED](https://fred.stlouisfed.org/) `DGS2/5/10/30` | 2Y / 5Y / 10Y / 30Y | 双源，每日交叉校验 |
| 日本 | [日本财务省 MOF](https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/) | 1Y–40Y（取 2/5/10/30Y） | 官方 CSV |

关于数据源的两个实现要点：

- **东财的期限字段没有语义**。返回值里期限是 `EMM00166466` 这类不透明 id，且顺序与期限不对应。好在同一接口也发布 10Y-2Y 利差列，用它做恒等式校验能唯一确定映射（已在 468/468 行上验证）。`validate_eastmoney_mapping()` 每次运行都会重跑这个校验，若上游字段重排会直接报错并回退到缓存，而不是发布错标的曲线。
- **MOF 把序列拆成两个文件**。`jgbcme.csv` 只含当月但最新，`historical/jgbcme_all.csv` 有 1974 年至今的全历史但滞后数周。脚本两个都抓，当月文件后写入以覆盖重叠部分。
- FRED 会在收到浏览器 User-Agent 时挂住直到超时，用普通 urllib 请求则立即返回，因此该源单独走 `browser_ua=False`。

## 产出

```
data/yields.json    看板数据：各国曲线、期限结构、跨国利差、温度、告警
data/history.csv    长表历史 (market,date,tenor,yield)，保留十年
data/alerts.json    仅告警，便于外部工具消费
```

## 指标口径

**期限结构**：10Y-2Y、30Y-10Y、10Y-5Y，单位 bp。曲线形态按 10Y-2Y 分档：`< -25` 深度倒挂、`< 0` 倒挂、`< 25` 平坦、`< 100` 正常、其余陡峭。

**跨国利差**：中美 10Y、日美 10Y、中日 10Y、中美 2Y。只在两国都有报价的交易日计算，避免假期错配造成的跳空。

**债市温度（0-100）**：`50% 收益率分位 + 20% 曲线斜率 + 30% 一个月动量`。读数越高表示收益率相对自身历史越高且在上行，即金融条件收紧、债券承压；越低则相反。分位取过去约两年（504 个交易日）的滚动窗口。

> 权重是实验性的，未经回测验证，当作可读的相对指标而非交易信号。

**异动告警**（阈值见 `ALERT_RULES`）：

| 类型 | 阈值 |
| --- | --- |
| 单期限单日变动 | ±10bp（±20bp 记为 high） |
| 单期限一周变动 | ±25bp |
| 跨国利差单日变动 | ±10bp |
| 10Y-2Y 倒挂 | 出现即告警 |

## 本地运行

```bash
python scripts/update_yields.py          # 抓数、算指标、写 data/
python scripts/update_readme.py          # 刷新 README 快照区块
python scripts/send_daily_email.py --dry-run   # 生成 email_preview.html 不发信
python3 -m http.server 8000              # 打开 http://localhost:8000 看看板
```

脚本只用 Python 标准库，没有 `requirements.txt` 需要装。

## 部署

1. 推到 GitHub，Settings → Pages → Source 选 **GitHub Actions**。
2. Actions 里手动跑一次 `Refresh yields and deploy Pages` 验证。
3. 工作流每周一至周五 08:10 UTC（北京 16:10 / 东京 17:10）运行，此时中日已收盘、MOF 当日曲线已发布；美债为前一交易日收盘值。

刷新后的 `data/` 与 README 会由工作流提交回仓库，因此历史会随时间累积。这需要 `contents: write` 权限，已在工作流中声明。

### 邮件日报（可选）

设 Variables `EMAIL_ENABLED=true`，并配置 Secrets：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`EMAIL_FROM`、`EMAIL_TO`（多个收件人用逗号分隔）。另可设 `SITE_URL` 指向你的 Pages 地址、`EMAIL_ONLY_ON_ALERT=true` 只在有异动时发信。

## 容错

每个数据源独立 try/except 并带三次重试。任一源失败时脚本沿用已提交的 `data/history.csv` 继续出图，并在 `data_quality` 里标记 `degraded` 与失败原因，看板顶部与邮件也会显示降级提示。只有在三源全挂且无任何缓存历史时才以非零码退出。

---

仅供研究参考，不构成投资建议。
