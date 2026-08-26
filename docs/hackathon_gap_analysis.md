# ap-agent 现状审计 & 差距分析

> 面向：SimplifyNext Agentic AI Hackathon（Digital track）
> 基准日期：2026-08-17 ｜ 方案提交：2026-09-07 12:00（还剩 3 周）
> 判定标准：能现场 demo 的 MVP —— 上传发票 → agent 识别匹配 → 排付款计划 → 输出结果

---

## 一、现状审计

### 1.1 一句话结论

**运行时骨架已完成且质量不错（有测试、有 CI、有文档），但业务逻辑完成度为 0。**
现在的代码能让一个 agent 跑通"多轮调工具 → 出决策"的循环，但它还没有任何真正的
AP（应付账款）工具可调，也没有任何数据可处理。

### 1.2 已实现（5 个 commit）

| 模块 | 文件 | 状态 | 说明 |
|---|---|---|---|
| 数据契约 | `src/apagent/schemas.py` | ✅ 完成 | 全部 Pydantic 模型：Document/LineItem（PO、GRN、发票三合一）、Discrepancy、MatchResult、AgentDecision、ToleranceConfig。注释非常详细，钱一律用分（int），百分比用百分点。这是全项目的唯一契约源 |
| LLM 客户端 | `src/apagent/llm/client.py` | ✅ 可用 | 双 provider：`anthropic`（含 DeepSeek 的 Anthropic 兼容端点）和 `openai`（含 DeepSeek 原生端点）。环境变量切换，响应归一化为统一 dict，带 prompt caching 标记 |
| 工具注册表 | `src/apagent/agent/registry.py` | ✅ 完成 | 插件式：各模块自己注册工具，loop 不用改。`execute()` 永不抛异常——"发票找不到"是证据不是 bug，agent 要看到错误并自己决策 |
| Agent 循环 | `src/apagent/agent/loop.py` | ✅ 完成 | 手写 tool-calling loop（刻意不用框架，为了 demo 时能逐行讲决策路径）。多轮调用、完整 tool_calls 审计轨迹、max_rounds 强制止损（超限返回 ESCALATE 而不是挂死）、JSON 解析失败自动降级 |
| 测试 | `tests/`（11 个） | ✅ 全绿 | 全部离线跑（monkeypatch 掉 call_model），不需要 API key。覆盖：直接回答 / 调工具后回答 / 死循环止损 / 解析失败降级 / markdown 围栏剥离 / 空响应 |
| 工程化 | CI + ruff + pre-commit | ✅ 完成 | GitHub Actions 跑 lint + pytest |

### 1.3 未实现（全部是空目录）

| 模块 | 现状 | 它应该做什么 |
|---|---|---|
| `extraction/` | 空 | PDF/图片 → `Document` 对象（发票识别）。依赖里已装 pdfplumber |
| `matching/` | 空 | 三方匹配引擎（PO ↔ GRN ↔ 发票，含无 SKU 时按描述相似度配对，依赖里已装 scipy 说明计划用匈牙利算法） |
| `rules/` | 空 | 容差检查（ToleranceConfig 已定义，检查逻辑没写） |
| `scheduling/` | 空 | 付款排程（依赖里已装 pulp，说明计划用线性规划做现金流/折扣优化） |
| `api/` | 空 | FastAPI 接口（依赖已装 fastapi + uvicorn，一行代码没写） |
| `eval/` | 空 | 评测集 + 指标（STP 率、touchless 率、错误批准率——CLAUDE.md 里定义了指标口径，没有实现） |
| `scripts/` | 空 | 无样例数据生成器（依赖里装了 reportlab，说明计划自己生成发票 PDF 测试集） |
| 前端 | 不存在 | 无任何 UI |
| 数据层 | 不存在 | sqlalchemy 在依赖里，但没有任何表定义和存取代码 |
| 真实工具 | 不存在 | agent 目前只有 echo/add/broken 三个自测工具，没有 lookup_po、lookup_grn 等任何业务工具 |
| System prompt | 不存在 | agent 的决策指引（何时 APPROVE/HOLD/EMAIL/ESCALATE）还没写 |

### 1.4 审计中发现的两个技术隐患（不紧急，但要知道）

1. **loop.py 没有按 provider 协议回传 tool_use/tool_result 块。**
   现在的做法是：把工具结果拼成纯文本、当作 user 消息发回去。这能跑通（测试和
   DeepSeek 实测都过），但不是 Anthropic/OpenAI 的标准 tool-calling 协议
   （标准做法要求 assistant 消息带 tool_use 块、下一条消息用 tool_result 引用 id）。
   偏离协议的代价是模型对"我调过什么工具"的理解会弱一些，多工具并发时更明显。
   **迁移到 Bedrock AgentCore 时这里大概率要改**，先记下。
2. **prompt caching 标记给每个工具都打了 `cache_control`。**
   Anthropic 限制每次请求最多 4 个 cache 断点。现在工具少没事，等注册的业务工具
   超过 3 个（加上 system prompt 就超 4 个断点）会直接报 API 错。改法很简单：
   只在最后一个工具上打标记。做任务 2 时顺手修。

### 1.5 对照评审要求的判断

- **"必须是真 agent"——架构上已经站住了。** 多轮规划、调工具、看结果再决策、
  完整审计轨迹（AgentDecision.tool_calls），这正是评审要看的东西。缺的是让它
  有真工具可调、有真数据可看。
- **"服务明确目标用户"——定位清晰（新加坡 SME 财务/AP），但现在没有任何
  能拿给评审看的东西。** 没有界面、没有样例发票、没有端到端流程。
- **"落在 Bedrock AgentCore + MCP"——现在的手写 loop 和 Bedrock 不冲突**：
  provider 抽象层加一个 bedrock 分支即可（任务 2 的占位），AgentCore 的
  runtime/gateway 等拿到 credits 和培训内容后再评估怎么接。

---

## 二、差距分析（按 demo 主线排优先级）

Demo 主线：**上传发票 → agent 识别匹配 → 排付款计划 → 输出结果**

### P0 —— 没有它 demo 不成立（建议本周内完成）

| # | 缺什么 | 建议做法 | 依赖 |
|---|---|---|---|
| 1 | **样例数据集** | `scripts/` 写生成器：用 reportlab 生成 15–30 张合成发票 PDF + 配套 PO/GRN（JSON 直接构造 Document），故意埋入缺陷（价差、数量差、缺 GRN、重复发票、无 SKU）。没有数据，后面所有模块都没法验证 | 无，**最先做** |
| 2 | **extraction/** | PDF → Document。两条路：pdfplumber 抽文本 + LLM 结构化（推荐，本身就是 agent 卖点），纯规则解析当 fallback。单位换算（箱→件）只在这一层做 | #1 |
| 3 | **matching/** | 三方匹配引擎：按 vendor+ref_doc_id 找 PO/GRN（缺 ref 时按 vendor+金额回搜），行级配对（有 SKU 直接对，无 SKU 用描述相似度+匈牙利算法），产出 MatchResult + Discrepancy 列表 | #1 |
| 4 | **rules/** | 容差检查：把每条 Discrepancy 对着 ToleranceConfig 打 within_tolerance 标；实现 per-vendor override 和人工复核金额线 | #3 |
| 5 | **真实 agent 工具 + system prompt** | 注册 lookup_po / lookup_grn / get_vendor_history / check_duplicate_invoice / get_tolerance_config 等工具；写决策指引（什么情况 APPROVE/HOLD/EMAIL/ESCALATE，HOLD 要给 hold_reason）。这是"真 agent"的灵魂，值得花最多心思调 | #3 #4 |
| 6 | **数据层（最简版）** | demo 不需要真数据库：内存 dict + JSON 文件就够。sqlalchemy 有空再上，别让它挡路 | #1 |

### P1 —— 有它 demo 才完整（第 2 周）

| # | 缺什么 | 建议做法 | 依赖 |
|---|---|---|---|
| 7 | **scheduling/** | 付款排程：输入一批 APPROVE 的发票 + 现金约束，输出付款日历（考虑 due date、早付折扣）。pulp 线性规划，或先用贪心排序——demo 效果一样，别过度工程 | #5 |
| 8 | **api/** | FastAPI：`POST /invoices/upload`、`POST /run`、`GET /decisions/{id}`。薄薄一层就好，业务逻辑全在 Python 模块里（项目 CLAUDE.md 的硬性约束） | #2–#7 |
| 9 | **Demo 前端** | 一页就够：上传框 + 处理进度 + 决策结果卡片 + **tool_calls 轨迹可视化**。轨迹展示是评审最想看的（"agent 调了什么工具、看到什么、所以决定什么"），数据都在 AgentDecision 里，只差展示 | #8 |

### P2 —— 加分项（第 3 周，视进度取舍）

| # | 缺什么 | 说明 |
|---|---|---|
| 10 | **eval/** | 在合成缺陷集上跑批量，报 STP 率 / touchless 率 / 错误批准率。有数字的 demo 比"看起来能跑"有说服力得多，评审是行业客户，就吃这个 |
| 11 | **Bedrock AgentCore / MCP 对接** | 等 credits 和培训（8/25 前后）。provider 层占位已在任务 2 里做 |
| 12 | **EMAIL 动作落地** | 现在 Action 里有 EMAIL 但没有执行器。demo 可以只生成邮件草稿展示，不真发 |

### 已定的两个技术决策（2026-08-17，Norman 拍板）

**做 RAG（合同检索）—— 已实现。** 每个供应商一份合同 PDF（`data/synthetic/contracts/`），
其中 V004（3%）和 V005（5%）谈了比默认 2% 更宽的价差容忍条款。
`src/apagent/retrieval/` 里是 BM25 检索器 + `search_vendor_contract` 工具
（刻意不用向量库——30 个 chunk 用不上，理由写在模块 docstring 里）。
Demo 主打案例：发票 `INV-V005-3018` 价差 4%，只看默认规则会 HOLD，
agent 查了合同发现 5% 以内可付，改判 APPROVE 并引用条款出处。
**接入点**：`rules/` 实现时要把检索结果落到 `ToleranceConfig.per_vendor_overrides`；
system prompt 要写"判断差异前先查合同"。

**不做 CoT prompting。** 理由与模型无关：我们的算术在 Python（rules/）里，
模型只做"这个差异意味着什么"的判断，不做心算。CoT 解决的是让模型心算不出错的
问题，我们没有这个问题；且 CoT 的散文推理会让 `_parse_final_answer` 更难解析。
多轮 tool_calls 轨迹 + `AgentDecision.reasoning` 已经是更可审计的"思维链"。

### 时间线建议

- **第 1 周（8/17–8/24）**：P0 全部。周末应该能端到端跑通命令行版 demo
  （无 UI：脚本喂 PDF，打印决策 + 轨迹）
- **第 2 周（8/25–8/31）**：P1 + Bedrock 培训后评估迁移量
- **第 3 周（9/1–9/7）**：P2、打磨 demo 剧本、录备份视频、写提交材料
- 评分 rubric 培训时才公布——**拿到 rubric 当天回来对照这份清单调整优先级**

---

## 三、给队友的三句话总结

1. 骨架是好的：agent 循环、工具系统、审计轨迹都在，测试全绿——我们不是从零开始。
2. 但 demo 主线上的每个业务模块（识别、匹配、规则、排程、界面）都还是空的——
   从"能跑"到"能演示"的全部距离都在这里。
3. 最先做的一件事是**合成数据集**（#1）：它不阻塞任何人，且所有人都被它阻塞。

---

## 四、2026-08-23 更新：聊天软件确认收货

> 这份文档写于 8/17，当时的判断是"业务逻辑完成度为 0"。那之后全部 P0–P2 都已完成，
> 本节记录在其之上新增的一个功能，以及它带来的新缺口。

### 4.1 解决的问题

`INV-V006-3019` 这个案例的 manifest 注释原话是 *"the warehouse confirmed by phone,
nobody typed a GRN"*。中小企业在群里确认收货，货真的到了，只是这个确认从来没变成
一条记录。此前第 6 关（proof-of-delivery）无条件 HOLD，这类发票只能人工追。

现在：群里 @ 机器人 → 读前后消息 → 提取 → 用**我方**的 PO 校验 → 生成一张标记为
`EvidenceSource.CHAT` 的**非正式收货单**。是否放行由 `pipeline.grn_gate` 判断，
聊天模块本身没有任何放行权力。

### 4.2 新增模块

| 模块 | 职责 |
|---|---|
| `chat/adapters.py` | Telegram 完整实现（长轮询）；WeCom / Slack / WhatsApp 为带说明的桩 |
| `chat/buffer.py` | 每群环形缓冲 + 时间窗（Bot API 无法回溯历史，必须常驻缓存） |
| `chat/roster.py` | 绑定群 + 授权确认人白名单，**按平台数字 user id** 索引 |
| `chat/extract.py` | LLM：消息窗口 → 结构化收货声明 |
| `chat/resolve.py` | 代码：声明 → 校验过的收货单，或失败即拒 |
| `chat/templates.py` | 机器人回复，代码套模板 |
| `chat/harvest.py` | 编排 |
| `chat/runner.py` | 进程内后台轮询线程 |

### 4.3 关键决策

**策略可配（`ToleranceConfig.chat_grn_policy`）** —— `OFF` / `EVIDENCE_ONLY` /
`TIERED`（默认）/ `TRUSTED`，支持按供应商覆盖。放在**代码**里而不是网页上，
与 `manual_review_threshold_cents` 同一套规矩；Settings 页只读展示。

**审核员认可是终态，不是"去补一张正式收货单"。** 目标客户本来就不做正式收货单，
这才是他们在群里确认的原因；要求补录会让 HOLD 事实上变成永久。

**任何策略都不豁免两件事**：数量是否覆盖开票（算术，不是政策）、人工复核金额线
（关于大额付款的承诺，与收货证明无关）。

**聊天证据是会话状态**，永不写入 `data/synthetic/`。`_benchmark_view` 让基准指标继续
按提交进仓库的 ERP 数据集计分，`false approvals = 0` 不受影响。

### 4.4 新的已知缺口

| # | 缺什么 | 说明 |
|---|---|---|
| 13 | **送货单照片** | 只处理文字。拍一张签收的送货单是最常见的确认方式，但需要多模态模型 + 图片下载/存储 |
| 14 | **WeCom / Slack 实现** | 都是桩。WeCom 需要认证主体 + 公网回调 + AES 解密；Slack 反而最简单（`conversations.history` 可回溯，连 `buffer.py` 都不需要） |
| 15 | **WhatsApp 只能 1:1** | Business Cloud API 不支持群聊。1:1 可行（仓库同事发到企业号），但要处理 24 小时回复窗口 |
| 16 | **一次确认覆盖该 PO 下所有发票** | 上限是按发票计的，同一个 PO 下 N 张刚好低于上限的发票都能过。与重复检测那条残余风险同类 |
| 17 | **授权确认人本身作恶** | 没有技术解。职责分离需要 `Document` 上有"请购人"字段，目前没有 |
| 18 | **消息保留的隐私说明** | privacy mode 关闭意味着机器人收到群里每一条消息。目前只在内存里保留有限条数 + TTL，但部署前应向企业明确告知 |
