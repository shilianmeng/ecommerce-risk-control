# 电商风控系统

实时特征 + 规则 DSL 决策 + 关联图谱 + 案件闭环 + RAG 审核辅助。面试展示项目，不做商用。

一个事件（登录、领券、下单、支付、售后申请）进来，系统在一次请求里完成：幂等去重 → 名单过滤 → 滑动窗口特征 → 图谱关联 → 规则打分 → 仲裁 → 落库建案 → 写审计链。每个决策都能回溯到「命中了哪条规则、哪个条件、实际值是多少、阈值是多少」。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python · FastAPI · SQLModel · Uvicorn（端口 8092，接口前缀 `/api/v1`） |
| 业务库 | MySQL `RiskControl`（10 张表） |
| 实时特征 | Redis ZSET 滑动窗口（6379） |
| 关联图谱 | Neo4j 5（7687），测试和降级时用 NetworkX 内存图 |
| 前端 | Vue 3 · TypeScript · Vite · Element Plus · ECharts（端口 5181） |
| 审核辅助 | BM25 检索 + 可选 OpenAI 兼容大模型（8093：云端 vLLM 经 SSH 隧道，或本地 transformers；Qwen2.5-1.5B + 风控 LoRA，见下文「LoRA 微调」） |

## 快速启动

前置：本机 MySQL 3306 已运行（root / root），Redis、Neo4j 安装在 `scripts/start_infra.ps1` 里写明的目录。

```powershell
# 1. 起 Redis + Neo4j（已在监听的会跳过；进程脱离调用方，终端关了也不会停）
powershell -ExecutionPolicy Bypass -File scripts\start_infra.ps1

# 2. 后端（首次启动自动建表、建账号、灌 12 条默认规则和演示事件）
cd backend
python run.py

# 3. 前端
cd frontend
npm install
npx vite --port 5181 --strictPort
```

VS Code 里直接 F5 选「电商风控（后端 + 前端）」，会先自动跑 `start_infra.ps1`。要用大模型生成审核建议，选「电商风控 + 本地模型」，会多起 8093（跑隔壁 `lanhong_小盟_金融知识库微调/scripts/serve_hf.py`，transformers 加载本地模型）；也可以把云端 vLLM 经 SSH 隧道转到 8093。

- 控制台：http://127.0.0.1:5181
- 接口文档：http://127.0.0.1:8092/docs
- 健康检查：http://127.0.0.1:8092/health（返回 Redis 状态、图谱后端、当前规则版本）

### 演示账号（密码都是 `123456`）

| 账号 | 角色 | 能做什么 |
|---|---|---|
| admin | 管理员 | 全部 |
| strategist | 策略师 | 规则增删改、仿真、名单 |
| auditor | 审核员 | 案件认领 / 处置 / 归档、名单 |

## 页面（11 个）

| 路由 | 页面 | 看点 |
|---|---|---|
| `/login` | 登录 | |
| `/dashboard` | 态势大盘 | 拦截率、风险分布、分数直方图、规则命中排行、按事件类型、趋势；时间段可切 |
| `/simulator` | 事件接入 | 手工构造五类事件实时打分，返回命中规则与特征 |
| `/decisions` | 决策回溯 | 按类型/结果/账号筛选，抽屉里看每条命中规则的条件级追踪（实际值 vs 阈值） |
| `/cases` | 案件审核 | 待审 → 审核中 → 已处置 → 已归档；处置选「拉黑账号 / 封禁设备」会自动写入黑名单 |
| `/cases/:no` | 案件详情 | 账号画像、特征快照、决策回溯、2 跳关系图、审计记录、RAG 审核辅助 |
| `/graph` | 关联图谱 | 团伙识别（连通分量），共用设备/IP/地址，可下钻 |
| `/rules` | 规则管理 | DSL 编辑、保存前静态类型检查、样例特征试算 |
| `/simulation` | 规则仿真 | 候选规则在历史快照上回放，看命中数、召回、误伤 |
| `/namelists` | 名单管理 | 白 / 黑 / 灰 × 账号 / 设备 / IP / 手机 / 地址，可设过期 |
| `/audit` | 审计日志 | 哈希链，一键校验有没有被篡改 |

## 核心设计

### 1. 决策链路（`backend/app/services/decision.py`）

```
POST /events
  └─ 事务开始：锁 DecisionGuard 单行（串行化）
     ├─ 幂等：idem_key = 业务单号(无则账号) + 事件类型 + 毫秒时间戳；已存在直接返回原决策
     ├─ 名单：白名单 → PASS；黑名单 → REJECT 100；灰名单 → +20 分
     ├─ 特征：从 SQL 已提交历史计算（权威特征）
     ├─ 图谱：related_account_count = 2 跳内共用实体的账号数
     ├─ 规则：命中的分值累加，截断到 0–100
     ├─ 仲裁：0–59 PASS · 60–79 REVIEW · 80–100 REJECT
     ├─ 落库：事件 + 决策快照；REVIEW/REJECT 自动建案 + 写审计
     └─ 同事务写 EventProjection 投影任务
  └─ 提交后：投影到 Redis / Neo4j（失败留在 SQL，后台每 5 秒重试）
```

关键取舍：**SQL 是事实源，Redis 和 Neo4j 是可重试的投影**。提交前不写外部存储，所以数据库失败（返回 503）不会留下孤立的 Redis 计数或图谱边；外部存储挂了也不影响后续决策，恢复后自动补投。

### 2. 规则 DSL（`backend/app/engine/dsl.py`）

自己写的词法 + 递归下降语法分析 + AST，不用 `eval`，只能访问特征字段和 `abs / min / max / coalesce` 四个函数。

```
user_coupon_count_1h >= 3 AND is_new_account == true
refund_amount > amount * 0.8 AND coalesce(account_age_days, 0) < 30
event_type IN ['order_create', 'order_pay'] AND NOT (ip_account_count_24h <= 2)
```

让规则「可回溯」的三件事：
- **静态类型检查**：`refund_reason > 3`、`amount AND true` 这类写错的规则保存前就拦下。
- **条件级追踪**：求值不短路，每个原子条件都记下实际值、阈值、是否成立。
- **反事实**（`engine/dsl.py` 的 `counterfactual()` + `engine/whatif.py`）：两层。规则层逐个强制翻转原子条件，找出单独翻转就改变规则结果的关键条件，并给出相对距离最小的那一处改动（如「user_refund_count_30d 当前 4，降到 2，规则不再命中」）；决策层逐条去掉命中规则重新仲裁，看结论会不会变，并给出离上下阈值各差几分。用的是决策当时存下的表达式和特征快照，只读计算。决策回溯抽屉、案件详情、规则编辑的「校验 / 试算」三处都能看到；有 7 条单元测试和 1 条接口测试。

### 3. 滑动窗口特征（`backend/app/engine/features.py`）

Redis ZSET，事件时间作 score，`ZCOUNT [t-窗口, t]` 精确统计。不用 `INCR + EXPIRE` 固定窗口，避免 59:59 和 00:01 落在两个窗口里导致特征突变。去重窗口（同设备账号数、同 IP 账号数、同地址账号数）的 member 是 `[账号, 事件键]`，查询时按账号去重，乱序事件也能算对。共 20+ 个特征，注册表 `FEATURES` 同时给 DSL 校验和前端展示用。

### 4. 关联图谱（`backend/app/engine/graph.py`）

账号 —USES_DEVICE / USES_IP / USES_PHONE / SHIPS_TO→ 实体。N 跳查询走 Cypher 变长路径；团伙 = 账号-实体二部图里账号数 ≥ N 的连通分量。Neo4j 和内存图实现同一个接口，后端启动时连不上 Neo4j 会降级为内存图，并从事件表重建（运行中途 Neo4j 掉线不会切换，靠投影任务重试）。

### 5. 迟到事件重算（`backend/app/services/reliability.py`）

迟到事件到达时，在同一事务里重算它之后的决策，用的是**当时的规则、阈值和名单快照**（存在 `EventProjection` 里），不拿当前规则冒充历史规则。需要时补建案件，修正前后内容都记审计；回溯接口同时返回 `original` 和 `corrections`。

### 6. 审计哈希链（`backend/app/engine/audit.py`）

`hash = SHA256(prev_hash | actor | action | target | detail | time)`，首条 prev_hash 为 64 个 0。改、删、插任何一条，从那条往后都对不上；写入时进程内加锁并 `SELECT … FOR UPDATE` 锁链尾。

### 7. 规则仿真（`backend/app/services/simulation.py`）

候选规则在历史决策保存的特征和名单快照上重新评估，不读当前图谱、名单、实时窗口，不落库不建案。标签来自已处置案件的人工结论（fraud / normal），据此算召回和误伤。

### 8. RAG 审核辅助（`backend/app/services/rag.py`）

知识库 = `app/data/knowledge_base.json`（政策、判例、FAQ 共 24 条）+ 规则表自动生成的规则说明，启动时同步到 `knowledge_doc`；规则增删改时同步更新。检索是中文 bigram + 词元的 BM25，命中规则编号额外加权。配置了 `RAG_LLM_URL` 就调大模型，要求只依据片段作答、标引用、资料不足时拒答；模型不可用时**立即降级为检索模式**，审核流程不受影响。

**LoRA 微调**（脚本在隔壁 `lanhong_小盟_金融知识库微调`）：基座 Qwen2.5-1.5B-Instruct，训练数据从本项目知识库和真实决策案例生成，没有编造的数据。共 991 条训练、100 条留出不参与训练。2026-11-25 在云端 RTX 4090 上训练约 6 分钟。留出集结果（`reports/对比.md`）：

| 指标 | 微调前 | 微调后 |
|---|---|---|
| 引用正确率 | 0% | 91.0% |
| 引用召回 | 0% | 95.8% |
| 误拒率（越低越好） | 56.7% | 0% |
| 不编造率 | 90.0% | 100% |
| 正确拒答率 | 75.8% | 100% |

适配器在 `outputs/qwen-risk-lora`。F5「本地模型 transformers」启动 `serve_hf.py --lora outputs/qwen-risk-lora`，挂到 8093。`/cases/{no}/assist` 实测返回 `mode=llm`，引用的规则和判例都在检索结果里。

### 9. 业务系统接入

`POST /api/v1/events` 两种调用方：
- 控制台「事件接入」页：登录 Token。
- 业务系统（如电商小二）：请求头 `X-Risk-Api-Key`，密钥在 `config.py` 的 `EVENT_API_KEYS`。

无凭证或密钥错误返回 401 且不落库。同一 `ts` 重复调用只算一次。接入说明见 `../lanhong_小盟_电商风控-docs/电商小二_电商风控_接入方案.md`。

## 默认规则（12 条，覆盖 5 个场景）

| 场景 | 规则 |
|---|---|
| 羊毛党 | R-001 新账号高频领券 · R-002 同设备多账号领券 · R-012 同设备多账号登录 |
| 团伙 | R-003 团伙关联账号多（图谱 2 跳 ≥5） |
| 黄牛 | R-004 同设备高频下单 · R-005 同地址多账号下单 · R-006 新账号大额订单 |
| 刷单 | R-007 同 IP 多账号支付 · R-010 账号高频下单 |
| 恶意退款 | R-008 多次未收到货 · R-009 收货手机号退款率高 · R-011 新账号高额退款 |

## 数据说明

- **演示事件是场景脚本生成的**（`services/bootstrap.py`，固定随机种子 42）：40 个正常用户，加羊毛党、黄牛、刷单、恶意退款四类风险场景。生成的是**原始事件**，全部走真实决策链路灌入，所以窗口特征、图谱、决策快照、案件、审计链都是系统算出来的，不是直接往结果表里插数。
- 首次启动且 `risk_event` 为空时才灌数据；之后重启不会重灌。事件时间以灌入时刻为基准往前铺 3 天左右，所以放久了大盘「近 1 天 / 近 7 天」会变空。
- 当前库（2026-11-25 按当时时间重灌）：367 条决策（PASS 321 / REVIEW 5 / REJECT 41），46 个案件，Neo4j 识别出 3 个团伙（12 / 6 / 4 个账号）。联调留下的 `ITC` 测试数据已清理。
- 46 个案件已由 auditor 账号通过真实接口逐个认领、处置，结论全部是「确认欺诈」，备注写了命中规则和证据。处置前核对过：这几伙人用的设备和 IP 没有正常账号共用。正常用户没有进案件，所以没有「误判」标签。
  - 黑名单 20 条：16 个账号、4 台设备（刷单 b4005 的设备只有它自己用，没封；羊毛党 w2000、w2001 没触发案件，没拉黑）。
  - 审计链 157 条，校验通过。
  - 仿真：现有规则召回 46/46；停掉 R-003 召回降到 58.7%；R-012 从 ≥5 放宽到 ≥8，召回降到 42/46。
- 审计链里有 10 条 `decision.recompute` 和 8 条 `case.recompute`：羊毛党场景里有 10 处是两个账号在同一毫秒、同一台设备上领券。后灌入的那条按迟到事件处理，把先灌入那条的 `device_coupon_count_24h` 补加 1（其中 8 条有案件，所以同时记了案件重算）。分数和结论都没变，正好能演示迟到重算是怎么留痕的。
- 重灌方法：备份整库后清空 `risk_event`、`decision`、`risk_case`、`eventprojection` 和除 `rule.init` 外的审计记录，重启后端即按当前时间重新灌入；Redis、Neo4j 会在灌入前清空。

## 数据表（MySQL `RiskControl`）

| 表 | 用途 |
|---|---|
| `sys_user` | 系统账号（bcrypt） |
| `risk_event` | 原始事件，`idem_key` 唯一 |
| `decision` | 决策快照：命中规则、单项分值、特征、规则版本、耗时 |
| `rule` | 规则 DSL、分值、优先级、版本号 |
| `name_list` | 名单，(类型, 实体, 值) 唯一 |
| `risk_case` | 案件及状态流转、处置结论 |
| `audit_log` | 审计哈希链 |
| `knowledge_doc` | RAG 知识库片段 |
| `decisionguard` | 事务串行化用的单行锁 |
| `eventprojection` | 投影任务 + 重算用的规则/阈值/原始结果快照 |

## 目录

```
backend/
  app/
    api/v1/        dashboard（登录/大盘）· events（事件/决策）· rules（规则/名单/仿真）· cases（案件/图谱/审计/RAG）
    core/          config（端口、连接、阈值、密钥）· security（bcrypt + HMAC Token + 角色）· logging
    engine/        dsl · features · graph · audit
    services/      decision · reliability · simulation · rag · cases · bootstrap · runtime
    models/        全部 SQLModel 表
    data/          knowledge_base.json
  tests/           59 个单元测试
  run.py           启动入口
  RELIABILITY.md   可靠性设计与已知边界
frontend/src/
  views/           11 个页面
  components/      Layout · Chart · DecisionTrace（回溯）· RelationGraph（关系图）
  api/             http（axios + Token）· dict（枚举中文映射）
scripts/
  start_infra.ps1        起 Redis + Neo4j（WMI 脱离启动）
  integration_check.py   连运行中的服务 + 真库跑 13 项联调
```

## 测试

```powershell
cd backend
python -m unittest discover -s tests -t .      # 59 条；隔离 SQLite + fakeredis + 内存图，不碰真库

python ..\scripts\integration_check.py         # 13 项真实联调：会往真库写 ITC 开头的测试数据

cd ..\frontend
npm run build                                   # vue-tsc 类型检查 + vite 打包
```

单元测试覆盖：DSL 语法/类型检查/追踪、特征窗口、图谱与审计链、幂等与并发重复请求、提交失败无外部污染、写操作在响应发出前已提交、投影失败后台补偿、Redis 故障期间的权威特征、迟到重算与原规则快照、仿真隔离、接入鉴权、规则与知识库同步。

## 已知边界（如实说明）

- **吞吐**：全局 SQL 行锁串行决策，每次扫描历史重建内存图，只适合演示规模；没做并发压测。扩容要按实体分区加锁、增量物化特征。
- **一致性**：是「SQL 事务 + 持久化投影任务」，不是跨存储分布式事务。图谱展示可能短暂滞后；外部存储投影成功后自己丢数据，目前不会自动发现。
- **多实例**：规则和名单是单进程内存缓存，没有做多实例广播失效。
- **投影补投**：2026-11-24 在真实环境里分别停掉 Redis、Neo4j 验证过：停服期间决策照常返回，投影任务留在 SQL，恢复后自动补齐。没做长时间断连和数据丢失后的对账。
- **RAG**：检索是 BM25，没有向量检索。LoRA 的评测是按规则检查编号、分值和引用范围，不等于完整的语义正确；本机 4G 显卡上生成一次约 28 秒。8093 不开时只有检索模式。
- **数据**：标签只有「确认欺诈」一类（46 个），没有「确认正常」的案件，所以仿真页的误伤率是空的。召回能算，误伤要等有人工判为正常的案件才有数。
- **密钥**：`SECRET_KEY`、`EVENT_API_KEYS`、Neo4j 和 MySQL 密码都是本地开发默认值，写在 `config.py` 里，可用 `RISK_` 前缀的环境变量覆盖。

详细的可靠性设计见 [backend/RELIABILITY.md](backend/RELIABILITY.md)，演示流程见 [docs/演示脚本.md](docs/演示脚本.md)。
