# 轮辋逆向重建 Agent：从开源 CadAgent 到可复现的 FreeCAD 工作流

面向读者：初次接触大模型 Agent、FreeCAD 插件或逆向建模的学习者。  
整理基准：2026 年 11 月 10 日，`E:/hubagent/CadAgent-main` 当前源码。  
目标：理解系统具体做了什么，并在自己的环境中配置、运行和检查同一个 Agent。

> 本文首先讲通用 CadAgent 的核心，再讲轮辋扩展。所有路径均为本机示例，迁移到另一台机器时必须替换。本文没有实际 API 密钥。当前源码目录未提供可用的 Git 仓库元数据，因此无法标注原上游 URL 与精确 commit；复现时必须保留这份包含轮辋扩展的完整源码，不能仅下载名称相同的上游项目。

## 阅读路线

1. 项目做了什么：理解输入、输出与边界。
2. 先读懂开源 Agent：循环、工具、反馈、质量门禁。
3. 轮辋模块怎样接入：数据、算法、线程与阶段控制。
4. 环境和部署：先验证依赖，再安装工作台。
5. 完整运行：从 STL 到草图、回转实体和文件保存。
6. 验收与排错：区分安装成功、流程成功与工程精度达标。
7. 开发者复现：绕过大模型单独运行确定性工具链。

## 一、这个项目具体做了什么

### 1.1 输入和输出

输入是一份轮毂或轮辋的 STL 三角网格，以及一句自然语言任务。STL 由大量三角面组成，通常没有草图、回转轴和设计参数，不能直接当作具有设计历史的 CAD 模型使用。

系统尝试提取其中的轮辋回转主体：先找到候选轴线，再在多个角度切出材料截面，筛选外围闭合轮廓，用 B 样条拟合，最后在 FreeCAD 中建立可编辑草图与 360° 回转特征。

最终特征树应包含：

```text
RimReconstruction（文档，实际名称可能有后缀）
└─ RimBody                 PartDesign::Body
   ├─ RimProfile           Sketcher::SketchObject
   └─ RimRevolution        PartDesign::Revolution，360°
```

目标是轮辋回转主体，不是完整复原轮辐、安装盘、螺栓孔和气门孔。草图可以编辑控制点，但尚未自动生成轮辋宽度、壁厚等完整工程尺寸约束。

### 1.2 三部分各自负责什么

| 部分 | 负责的工作 | 不应交给它的工作 |
| --- | --- | --- |
| 大模型 Agent | 理解任务、规划截面角度、根据摘要选择候选和片段、根据错误调整流程 | 凭空生成原始截面点、编造轴线或产物路径 |
| 确定性算法 | 读 STL、估轴、求交、清洗、筛选、拟合和数值检查 | 用自然语言判断代替几何验证 |
| FreeCAD | 创建草图、回转实体、检查拓扑并保存模型 | 仅凭外观看起来正确就认证重建精度 |

结构框图：

```text
┌──────────────────────┐
│ 用户：STL 路径 + 任务 │
└──────────┬───────────┘
           ▼
┌─────────────────────────────┐
│ CadAgent：对话、模型请求、阶段门禁 │
└──────┬────────────────┬─────┘
       │结构化工具参数     ▲统计、警告、产物引用
       ▼                │
┌─────────────────────────────┐
│ Python 服务与 CppWorkerAdapter │
├─────────────────────────────┤
│ C++ Worker：STL → 轴线 → 截面 │
│ Python：候选筛选 → B样条拟合   │
└──────────────┬──────────────┘
               │成功拟合文件
               ▼
┌─────────────────────────────┐
│ FreeCAD 主线程：草图 → 回转实体 │
└─────────────────────────────┘
```

### 1.3 必须分清的三种“成功”

- 工具成功：某次读取、切片或分析正常完成；可能仍没有可用候选。
- 完整流程成功：本轮确实建立了草图和有效单实体，体积大于零。
- 工程精度通过：轴线正确、轮辋材料边界完整、相对 STL 的误差满足要求。当前完整流程成功不能替代这项认证。

当前 PCA 轴线返回 `axis_validated=false`，只适合初始试建。默认 0.08 mm 是拟合的检查阈值，不是整件模型相对 STL 的已验证误差。

## 二、先单独读懂开源 CadAgent 的核心

### 2.1 Agent 是怎样工作的

普通聊天只输出文字。这个 Agent 会请求执行程序，读取执行结果，再决定下一步。它采用推理、行动、观察的循环思想：

```text
用户任务 → 准备上下文 → 请求 LLM
                         │
                 ┌───────┴────────┐
                 ▼                ▼
              请求工具          请求结束
                 │                │
           执行并回传结果      检查完成条件
                 │            │通过     │不通过
                 └→ 下一轮 ←──┘         │
                              完成/失败报告
```

模型负责提出动作，程序负责校验和执行。系统提示词虽然重要，但真正阻止跳步、复用旧产物或提前报成功的是程序逻辑。

### 2.2 最重要的文件和函数

以下路径都相对项目根目录，不要求通读整份代码。

| 文件 / 入口 | 核心作用 | 阅读时关注什么 |
| --- | --- | --- |
| `InitGui.py` / `CadAgentWorkbench` | 向 FreeCAD 注册工作台与面板 | 工作台从哪加载、插件路径如何加入 Python 搜索路径 |
| `ui/panel.py` / `AgentPanel` | 解释循环返回的动作，协调线程和界面 | LLM 请求、工具执行、结果回调由谁调度 |
| `agent/controller.py` / `AgentController` | 持有会话和运行结果 | 一次任务如何关联历史消息和停止状态 |
| `agent/loop.py` / `AgentLoop` | 纯逻辑状态机 | 下一步调用模型、执行工具还是结束 |
| `agent/tool_defs.py` | 工具 JSON Schema | 模型能看见哪些工具、必填参数是什么 |
| `agent/tool_dispatch.py` / `register_tool`、`dispatch_tool` | 工具注册及名称路由 | 工具名怎样对应真实函数 |
| `core/llm_client.py` | 兼容 Chat Completions 的模型请求与流式响应 | 地址、模型、密钥及工具调用结果格式 |
| `agent/prompts.py`、`agent/references.py` | 系统指令和按状态补充的参考片段 | 通用模式与轮辋模式的职责约束 |
| `core/session.py`、`core/token_budget.py` | 会话历史、参数与上下文裁剪 | 为什么不会无限累积完整历史 |
| `core/quality.py`、`core/snapshot.py` | 通用建模质量检查与快照撤销 | 错误反馈、几何验收与恢复机制 |

`AgentLoop` 的四个关键入口：

1. `start(user_text)`：把任务放入会话，开启循环。
2. `prepare_llm_call()`：组装系统提示、历史和工具声明，生成下一次模型请求。
3. `handle_stream_done(...)`：解析模型响应；有工具调用时返回执行指令。
4. `handle_tool_results(...)`：记录实际结果，更新阶段状态，再进入下一轮或完成检查。

循环通过 `LoopAction` 返回动作。`CALL_LLM`、`EXECUTE_TOOLS`、`FINISH` 分别表示“调用模型”“执行工具”“结束”。纯逻辑层不直接操作 Qt 控件或 FreeCAD 文档，这也是它能独立测试的原因。

#### 2.2 执行机制串讲：从 API 接入到下一轮决策

先把整个系统看成一个循环：**程序把规则、任务、历史和工具规范发送给模型；模型提出一个工具调用；程序校验并执行；程序把真实结果带入下一次请求。**模型不会自行连接 FreeCAD，也不会在服务端自动记住本地做过的几何操作。

下面以“从 STL 重建轮辋回转主体”为同一个例子，把 API、规划、工具、上下文和错误处理串起来。后面的逐文件代码解读用于继续定位实现。

##### A. 大模型 API 如何接入：先准备地址、密钥、模型和工具能力

配置入口是项目根 `.env` 或 CadAgent Settings。`core/config.py` 加载配置，`core/llm_client.py` 读取运行时配置发请求。

```ini
API_BASE_URL=https://你的模型服务根地址/v1
API_KEY=你的密钥
MODEL_NAME=支持原生工具调用的模型名称
MAX_ITERATIONS=20
LLM_TIMEOUT=180
```

地址是占位示意，不能原样运行。客户端实际访问 `API_BASE_URL.rstrip("/") + "/chat/completions"`，因此不要把完整 `/chat/completions` 重复填进根地址。模型服务必须支持原生 Tool Calling；轮辋模式不会退回 XML 工具协议。

`_make_request()` 中的核心代码是：

```python
return urllib.request.Request(
    _config.API_BASE_URL.rstrip("/") + "/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_config.API_KEY}",
    },
)
```

这里的 `payload` 是程序即将发送的 Python 字典，序列化为 JSON 后交给 HTTP 服务。密钥放在请求头，不是让模型阅读的一段提示词。

`call_llm_streaming()` 构造的载荷核心如下，字段名与当前源码一致：

```python
payload = {
    "model": _config.MODEL_NAME,
    "messages": messages,
    "temperature": temperature if temperature is not None else _config.TEMPERATURE,
    "max_tokens": _config.MAX_TOKENS,
    "stream": True,
}
if tools:
    payload["tools"] = tools
    payload["tool_choice"] = "auto"
```

`messages` 是本次模型能读到的对话，`tools` 是工具名称、说明和参数 Schema。`tool_choice="auto"` 允许模型提出工具调用，并不意味着模型可以绕过程序阶段门禁。`stream=True` 只是逐块返回数据，执行权仍在本地程序。

**检查接入是否成功不能只看有没有文字回复。**还应确认工具调用能完整返回：`function.name` 正确，`function.arguments` 能解析为 JSON。FreeCAD 界面的 `_LlmCallThread` 会按索引累积流式工具参数，结束时再组装成完整消息；参数 JSON 不完整会报错，不会边接收半段 JSON 边执行。

##### B. 系统提示词怎么组织：固定规则、任务、历史与实时状态各有来源

一次请求的材料不是只有一段很长的 prompt：

```text
请求 payload
├─ messages
│  ├─ system：工作流角色与行为规则，填入已有上下文
│  ├─ user：用户的实际任务 / 后续补充
│  ├─ assistant：模型先前的文字与 tool_calls
│  ├─ tool：每次真实执行结果，与 tool_call_id 对应
│  ├─ ...后续历史...
│  └─ system：本轮实时阶段、可执行动作、真实产物引用
└─ tools：工具 JSON Schema，独立于 messages 发送
```

上图表示完整轮辋模式的典型请求；首次调用尚无 assistant/tool 历史。最后一条实时状态由程序临时添加到本次请求副本，不是要求模型自己总结上一轮。

`AgentLoop.start()` 选择提示词并记录任务：

```python
if self._workflow == "rim_reconstruction":
    prompt = RIM_RECONSTRUCTION_SYSTEM_PROMPT
system_content = prompt.replace("{context}", ctx)
self._controller.session.set_system_prompt(system_content)
self._controller.session.add_user_message(user_text)
return self.prepare_llm_call()
```

这是方法中的摘录，`prompt` 的其他默认分支和 `ctx` 的构造在此前完成。轮辋专用提示词定义在 `agent/prompts.py`，主要约束：先检查与估轴；轴坐标由算法产生；角度由模型显式规划；候选和片段只能引用真实 ID；没有建成实体不能宣称完整成功；不能把 PCA 或拟合阈值当作已认证工程精度。

通用模式另外通过 `agent/references.py` 按阶段补充首次执行、质量失败和重复错误提醒。轮辋模式的 `_build_context()` 会提前返回已有上下文，不复用通用 CQ 建模检查清单；它使用专用提示词与 `RimPipeline` 实时状态。

**提示词与工具 Schema 的关系：**提示词解释“什么时候、为什么调用”，Schema 解释“调用叫什么、参数长什么样”，服务层校验决定“这次调用是否合法”。三者必须同时具备，不能仅凭提示词保证正确。

##### C. Agent 如何处理循环：一个状态机，三类动作

`AgentLoop` 不自己启动 Qt 线程或操作 FreeCAD，而是返回 `LoopAction`。面板读取动作并负责执行：

```text
面板收到任务
  ↓
Loop.start() → prepare_llm_call() → CALL_LLM
  ↓ 面板启动后台模型线程
模型响应 → handle_stream_done()
  ├─ 有合法工具调用 → EXECUTE_TOOLS
  │                       ↓ 校验、执行、取得真实结果
  │                 handle_tool_results()
  │                       ↓ 写历史、更新阶段
  │                 prepare_llm_call() → 下一轮
  └─ 没有工具调用 → 检查完成条件
                      ├─ 已完成 → FINISH(success=True)
                      └─ 未完成 → 一次推进提醒或失败结束
```

`prepare_llm_call()` 在每轮前检查停止标志和 `MAX_ITERATIONS`。达到上限返回失败结束，不能无限循环。这里的一轮是一次应用层模型请求；底层 HTTP 连接重试不会自动等于新的一轮 Agent 决策。

为什么工具执行后还要再请求模型？因为本次执行可能没有候选、产生警告或返回新目录，模型只有读到这些真实信息，才能决定继续、调整角度、修补或报告失败。

完成条件由程序决定。完整轮辋模式 `_check_quality_gate()` 调用 `RimPipeline.completion()`，不是看模型有没有说“完成”。`handle_stream_done()` 对已经存在可执行下一步、却提前输出纯文本结束的情况，最多额外提醒推进一次；不会靠无限追加“继续”来制造成功。

##### D. 工具怎么规划与调用：模型决策与程序流程共同作用

当前实现没有额外的独立 Planner Agent，也不是先生成一个永远不变的完整计划列表。它采用“固定业务顺序 + 每轮模型选择参数和动作 + 执行门禁”的方式。

```text
程序规定主顺序：检查 → 估轴 → 截面 → 分析 → 拟合 → 实体
模型决定具体内容：探索哪些角度、为何这样采样、选哪个候选与哪些片段、是否重试
程序校验可执行性：阶段是否就绪、引用是否属于当前任务、实际几何是否通过
```

主流程之外有可选修补与重新采样，因此它不是无条件直线运行的脚本。模型也不能跳过前面步骤直接创建实体。

模型提出的调用示意如下。它是协议示意，不是一个可直接复现的真实调用记录：

```json
{
  "role": "assistant",
  "tool_calls": [{
    "id": "call_axis_example",
    "type": "function",
    "function": {
      "name": "rim_estimate_axis",
      "arguments": "{\"inspection_call_directory\":\"E:/.../call-inspect-example\"}"
    }
  }]
}
```

注意 `arguments` 在返回协议里是 JSON 字符串，需要由 handler 解析，不是直接拿字符串当函数代码执行。

完整模式每轮只接受一个工具调用。面板 `_run_next_tool()` 在执行前再调用 `pipeline.validate_call(...)`；错误则生成 `RIM_STAGE_INVALID` 工具结果。通过后才执行：

```text
function.name
  → 已注册的 handler
  → 解析/校验 JSON 参数
  → services 中的业务函数
  → C++ Worker / Python 数值算法 / FreeCAD builder
  → 字符串或 JSON 工具结果
```

通用和主线程工具通过 `dispatch_tool()` 查注册表；耗时轮辋工具由 `RimToolWorker` 的 handler 映射执行。不要以为所有工具都在同一个线程中经过完全相同的函数路径。`rim_build_revolution` 修改 FreeCAD 文档，因此留在主线程。

角度规划也是这个分工的例子：模型读取网格和轴线摘要，显式给出 `angles_degrees` 和 `angle_selection_reason`；程序读取实际轴线并计算正交径向；C++ 才进行截面求交。模型没有得到轮辐相位证据时只能称为探索采样，不能编造已经避开轮辐。

##### E. 下一步为什么知道上一步做了什么：三种载体共同保留上下文

**第一种是消息历史。** `ChatSession` 保存 user、assistant 和 tool 消息。模型提出的调用先被记录，再把真实结果与原调用 ID 绑定：

```python
# ChatSession.add_tool_result() 中的实际结构
self.messages.append({
    "role": "tool",
    "tool_call_id": tool_call_id,
    "content": result,
})
```

例如 `call_axis_example` 的结果中会有真实轴线目录和 `axis_validated=false`。下一次请求再次携带这段历史，所以模型知道轴线步骤执行过、使用什么目录、还存在什么限制。不是模型服务偷偷访问了本地文件，也不是 API 自动永久保存了任务状态。

**第二种是程序阶段状态。** `RimPipeline` 持有 `inspection`、`axis`、`section`、`analysis`、`fit`、`solid`。每次工具返回后先 `record(ex)`，再由 `request_context()` 产生当前状态摘要，并加入下轮请求。

状态摘要的结构示意如下，省略了一部分实际可执行工具：

```json
{
  "executable_tools": ["rim_inspect_stl", "rim_estimate_axis", "rim_process_sections_auto"],
  "next_step": "rim_process_sections_auto",
  "artifacts": {
    "inspection_call_directory": "上一步真实检查目录",
    "axis_call_directory": "上一步真实轴线目录"
  },
  "last_tool_failed": false
}
```

因此即使对话很长，当前引用仍由程序重新提供。工具虽然全部声明，只有 `allowed_tools()` 中的动作符合当前前置条件。模型填入旧目录时，`validate_call()` 会拒绝。

**第三种是本地文件产物。** 完整线段、点列、分析和拟合系数保存在 `request.json`、`result.json`、`section_*.json`、`analysis_*.json`、`fit_*.json` 等文件中。模型只接收摘要、警告、路径与 ID，下一步服务通过路径读取完整数据，并核对身份与源索引。

```text
消息历史：告诉模型“上一步返回了什么”
阶段状态：告诉程序和模型“当前到底到哪一步、哪些引用仍有效”
本地产物：给算法提供“下一步真正要计算的完整几何数据”
```

这三种载体不能互相替代。只保存一句“切片完成”无法让拟合服务恢复数千个点；只保存一个旧文件，也不能证明它属于当前任务。

**重试时怎样防止用错旧结果：**重做截面会清掉当前分析、拟合与实体状态。即使这次截面失败，也不能继续拿上一次实体宣称成功。旧文件可以保留作追溯，但已不是当前管线的有效下游引用。

##### F. 长任务和跨会话的上下文边界

长任务通过 `trim_messages()` 控制请求体积：估算 token、压缩较旧中间内容、成组处理 tool_calls 与结果，必要时生成检查点式内容并进一步截断。它不是无限保留所有原始消息，也不是保证永不丢细节的语义记忆系统。

`SessionStore.save()` 将 `ChatSession.to_dict()` 写入 JSON，退出后可恢复对话历史、summary、document_state、参数等。保存时使用临时文件再替换正式文件。当前 `summary` 也不能直接等同于“每轮都会重新注入模型的完整记忆”；实际发出的信息由 `prepare_llm_call()` 和上下文构造决定。

**当前实现的重要限制：**每次新建完整轮辋 `AgentLoop` 都创建新的 `RimPipeline`。会话 JSON 没有序列化这套阶段对象，也没有自动从旧 tool 消息重建管线。因此“聊天历史恢复”不等于“从旧拟合阶段自动断点续跑”。新一轮仍应遵守当前阶段要求，不能照抄历史目录越级建模。正式交付还要保存 FCStd 和完整调用产物；聊天 JSON 不能替代它们。

##### G. 遇到错误怎么处理：先区分错误发生在哪一层

**1）API 连接错误：在 HTTP 层有限重试。** `_MAX_RETRIES=2` 表示首次尝试后最多再试两次。429、5xx 和一部分网络/超时错误进入重试；等待时间参考数值型 `Retry-After` 与指数退避，单次等待上限为 30 秒。401、403 等非重试分支直接抛出。最终仍失败时，线程把错误交给 Loop；当前 `handle_error()` 返回 `FINISH(success=False)`，并不会让模型无限“修复网络”。

**2）流式协议错误：参数没收全就拒绝执行。** SSE 结束后，线程对工具 `arguments` 调用 `json.loads()`；不完整则发出 `Stream interrupted: incomplete tool call arguments`，进入错误结束路径。一次返回多个轮辋工具等协议违规也可能直接通过 `handle_error()` 结束，不是所有错误都会进入自纠错循环。

**3）工具参数、阶段或几何错误：形成工具反馈，再由模型决定下一步。** handler/服务返回 `status=error`、`status=cancelled` 或 ERROR/FAIL 文本。界面形成 `ToolExecution`，`handle_tool_results()` 更新阶段状态并将结果写入会话，然后返回 `prepare_llm_call()`。

```text
模型：请求分析旧截面目录
程序：阶段校验拒绝，返回 RIM_STAGE_INVALID 和原因
历史：保留这次 assistant 调用 + 对应 tool 错误结果
下一轮：模型看到错误和当前有效目录，改正参数或明确报告受阻
```

这里是“基于反馈重新决策”，不是程序保证能修好。无候选、轮廓不闭合或拟合失败，都可能需要换角度、合规修补或停止；不能为了成功随意放宽阈值、填补缺口或制造实体。

**4）重复错误：补充更具体的修复提醒。** `handle_tool_results()` 记录近期错误签名，重复时附加定向提示或要求改变方法；最近错误列表最多保留五项。通用 CAD 还会注入质量修复建议。提醒不会改变工具自身验收标准，某些提示也只适用于通用代码建模。

**5）模型想提前结束，但质量没过：由门禁阻止。** 通用模式有质量失败反馈及连续阻止后的失败退出机制，完整轮辋模式按 `RimPipeline.completion()` 验收，不能把通用模式的所有修复分支照搬过来。有效单实体与工程精度也仍是两个独立问题。

**6）用户停止或旧线程迟到：防止污染新任务。** 面板设置停止标记并通知后台取消；尚未执行的调用补写 cancelled 结果，避免留下只有调用没有结果的历史。旧 worker 若不属于当前 Loop 或任务已结束，其返回结果会被丢弃。FreeCAD 内核及单次 SciPy 求解不能保证即时中断。

**7）几何回滚和界面恢复：各自处理。** 通用 `execute_code` 使用快照支撑撤销；轮辋 builder 创建原生特征时使用 FreeCAD 文档事务，失败回滚；面板内部异常还要恢复控件状态并请求停止后台任务。恢复聊天界面不等于几何自动修好，需继续查看真实工具结果。

##### H. 复现时沿同一条链检查，而不是只测试 API 连通

1. 配置生效：实际进程读取到正确根地址和模型，连接测试通过。
2. 工具协议有效：模型能返回一个完整 Tool Calling，而非仅输出工具名称。
3. 注册与服务连通：工具名有 handler，参数合法，Worker 能启动。
4. 反馈闭环成立：调用后历史出现对应 `tool_call_id` 的真实结果。
5. 下轮状态正确：当前 `next_step` 和产物引用与刚才的真实结果一致。
6. 失败不会伪装成功：旧产物失效、错误保留、未建成实体不能结束为成功。
7. 输出可追溯：保留源码版本、模型配置、消息记录、调用文件和最终 FCStd。

读完这条执行链，再阅读下面的逐文件源码片段，就能知道每段代码是在解决哪个具体问题，而不只是记住文件名。


#### 2.2 核心代码逐项解读

下面按程序实际调用顺序阅读。代码均摘自当前源码；省略的上下文会明确注明，片段用于理解，不应独立复制运行。`self` 表示当前对象，`session` 保存对话，`action` 是下一步动作，`ex` 是一次实际工具执行记录。

##### ① InitGui.py：让 FreeCAD 找到工作台和面板

源码：`InitGui.py`，片段起始行 50。

```python
def Activated(self):
    """每次切换到此工作台时调用，创建并显示 Agent panel。"""
    from PySide6 import QtCore
    from ui.panel import AgentPanel
    import FreeCADGui
    if not hasattr(FreeCADGui, '_cadagent_panel') or FreeCADGui._cadagent_panel is None:
        FreeCADGui._cadagent_panel = AgentPanel()
        FreeCADGui.getMainWindow().addDockWidget(QtCore.Qt.RightDockWidgetArea, FreeCADGui._cadagent_panel)
    FreeCADGui._cadagent_panel.show()
    FreeCADGui._cadagent_panel.raise_()

def Deactivated(self):
    """切换离开此工作台时调用。"""
    pass
```

**这段代码做什么：**用户选择 CadAgent 工作台时，FreeCAD 调用 `Activated()`，它创建或重新显示右侧对话面板。

- 方法内部导入 `AgentPanel`，真正的交互入口在 `ui/panel.py`。这里使用局部导入也与 FreeCAD 通过 `exec()` 加载入口脚本的作用域有关。
- `hasattr(...)` 和空值检查避免每切换一次工作台就创建一个新面板。
- `addDockWidget(...)` 把面板挂到主窗口右侧；`show()` 显示，`raise_()` 将其提到前面。

**在轮辋任务中：**这里只打开界面，还没有调用模型，也没有读取 STL。面板不出现时先查这个入口及插件路径，不能先归因于 API 密钥。

##### ② ui/panel.py：把状态机动作变成真实操作

源码：`ui/panel.py`，片段起始行 428。

```python
def _execute_action(self, action: LoopAction):
    if action.kind == LoopActionKind.CALL_LLM:
        self._start_llm_call(action)
    elif action.kind == LoopActionKind.EXECUTE_TOOLS:
        if action.system_message:
            self._append_system_msg(action.system_message)
        self._mode = self._loop.mode
        self._run_tool_calls(action)
    elif action.kind == LoopActionKind.FINISH:
        self._finish_from_action(action)
```

**这段代码做什么：**`AgentLoop` 只返回动作，面板按 `action.kind` 执行。

- `CALL_LLM`：启动模型调用；`EXECUTE_TOOLS`：进入工具队列；`FINISH`：整理最终结果并恢复界面。
- `action.system_message` 是给用户看的运行提示；`self._loop.mode` 是模型调用协议，不是轮辋/通用任务类型。
- 这个分工把“决定做什么”和“调动线程、控件、FreeCAD 去做”分开，便于独立测试状态机。

**沿调用链继续读：**面板发送任务时构造 `AgentController(self._session)`，然后构造带 `workflow=self._workflow` 的 `AgentLoop`，再调用 `start(text)`。模型线程的 `streamDone` 信号接到 `_on_stream_done()`，后者把响应交还给 Loop。

##### ③ agent/controller.py：保存会话和任务结果

源码：`agent/controller.py`，片段起始行 27。

```python
def __init__(self, session: ChatSession):
    self.session = session
    self.result = AgentResult()
    self._stopped = False
    self._mode = "auto"  # "auto" | "tool_calling" | "react"

def stop(self):
    self._stopped = True
```

**这段代码做什么：**控制器是共享状态容器，不负责循环推理和工具路由。

- `self.session = session` 使用已有会话对象，因此后续轮次可以读取前面的工具结果。
- `AgentResult()` 初始化本轮结果，包含 `success`、`summary`、`iterations`、`errors` 和 `tool_calls_log` 等字段。
- `_mode="auto"` 是协议检测初值。`stop()` 在控制器上设置停止标记，但实际循环还由 `AgentLoop.request_stop()` 和界面中断处理协作完成，不能认为这一行就能强制停止 C++/FreeCAD 内核。

**容易误读：**文件注释中还保留“状态机在 panel.py”的历史表述；当前纯逻辑状态机已经在 `agent/loop.py`，panel 负责调度。

##### ④ agent/loop.py 的 start()：任务怎样进入模型会话

源码：`agent/loop.py`，片段起始行 167。

```python
def start(self, user_text: str) -> LoopAction:
    ctx = self._build_context()
    # 轮辋逆向扩展：按任务选择提示词，避免截面阶段被引导生成 CAD 代码。
    prompt = RIM_SECTIONS_SYSTEM_PROMPT if self._workflow in ("rim_sections", "rim_reconstruction") else AGENT_SYSTEM_PROMPT
    # 轮辋逆向扩展：完整重建模式必须一直推进到原生回转实体。
    if self._workflow == "rim_reconstruction":
        prompt = RIM_RECONSTRUCTION_SYSTEM_PROMPT
    system_content = prompt.replace("{context}", ctx)
    self._controller.session.set_system_prompt(system_content)
    self._controller.session.add_user_message(user_text)
    # 轮辋逆向扩展：日志区分业务工作流和实际调用协议。
    log_info(f"Agent loop started: workflow={self._workflow}, mode={self._mode}, context={len(self._context)} chars")
    return self.prepare_llm_call()
```

**按顺序理解：**

1. `_build_context()` 获取已有上下文。
2. 根据 `_workflow` 选提示词。完整轮辋模式会覆盖为 `RIM_RECONSTRUCTION_SYSTEM_PROMPT`，避免模型沿通用 CAD 思路生成任意几何脚本。
3. `replace("{context}", ctx)` 将上下文放进提示词。
4. `set_system_prompt()` 设置规则；`add_user_message()` 记录用户原话。
5. `prepare_llm_call()` 返回第一次模型请求动作。

**关键点：**这里不是直接发 HTTP 请求。它只是准备动作，真正的网络执行由面板启动的后台线程负责。

##### ⑤ prepare_llm_call()：选择模型本轮可见的工具

源码：`agent/loop.py`，片段起始行 265。

```python
rim_names = {t["function"]["name"] for t in RIM_TOOL_DEFINITIONS}
workflow_tools = (RIM_TOOL_DEFINITIONS if self._workflow in ("rim_sections", "rim_reconstruction") else
                  [t for t in TOOL_DEFINITIONS if t["function"]["name"] not in rim_names])
# 轮辋逆向扩展修复：完整工具集始终声明，前置条件由执行门禁校验。
if self._rim_pipeline is not None:
    workflow_tools = [t for t in workflow_tools
                      if t["function"]["name"] in RIM_RECONSTRUCTION_TOOL_NAMES]
tools = workflow_tools if self._mode != "react" else None

request_messages = self._controller.session.get_messages()
if self._rim_pipeline is not None:
    # 轮辋逆向扩展：当前请求附上实际工具列表及可执行步骤，不使用模型记忆推断。
    request_messages.append({"role": "system", "content":
        "本次请求确实提供以下完整工具：" + ", ".join(RIM_RECONSTRUCTION_TOOL_NAMES)
        + "。阶段未就绪不等于工具未安装。前置步骤成功后继续调用下一步。当前程序状态："
        + json.dumps(self._rim_pipeline.request_context(), ensure_ascii=False)})
```

**这是 `prepare_llm_call()` 的中间片段，省略了停止与迭代上限判断。**

- `rim_names` 收集轮辋工具名，用于把通用工具和轮辋工具分开。
- `_rim_pipeline is not None` 表示完整轮辋流程；它将声明限制为 `RIM_RECONSTRUCTION_TOOL_NAMES`，当前实际有七项，包含可选修补工具。
- `tools` 是要随模型请求发送的 JSON Schema，不是函数对象列表。
- `session.get_messages()` 取得消息列表副本；追加当前 `request_context()` 后，模型会读到真实产物路径、当前可执行工具和失败状态。

**为什么要每轮提供程序状态：**模型可能忘记上一轮返回的目录，或者误把“前置条件未满足”说成“工具未安装”。动态状态用于减少这类错误，最终仍由执行门禁检查。

该函数随后调用 `trim_messages()`、`token_summary()`，最后返回 `LoopAction(kind=CALL_LLM, messages=messages, tools=tools, ...)`。

##### ⑥ handle_stream_done()：解析模型响应，而不是执行模型文字

源码：`agent/loop.py`，片段起始行 342。

```python
calls = assistant_msg.get("tool_calls") or []
if calls:
    # 轮辋逆向扩展：完整流程一次只执行一个步骤，下一步须等待真实返回。
    if self._rim_pipeline is not None and len(calls) != 1:
        return self.handle_error("完整轮辋流程每轮只允许一个工具调用")
    allowed_names = {t["function"]["name"] for t in RIM_TOOL_DEFINITIONS}
    if any(tc.get("function", {}).get("name") not in allowed_names for tc in calls):
        return self.handle_error("rim_sections 工作流只允许已声明的轮辋工具")
    return LoopAction(kind=LoopActionKind.EXECUTE_TOOLS, tool_calls=calls)
```

**这是轮辋响应分支的片段。**`assistant_msg` 是模型最终响应的消息对象。

- 读取 `tool_calls`；纯自然语言里出现工具名，不算工具调用。
- 完整流程要求一次只有一个工具，确保下一步依赖真实返回结果。
- 检查调用名属于已声明轮辋工具；然后返回 `EXECUTE_TOOLS`，实际计算交给面板。

**这里还没完成所有验证：**`RimPipeline.validate_call()` 还会在执行路径中检查本阶段资格和产物引用。若模型只回复“已经完成”，Loop 还会走 `_check_quality_gate()`；未实际建成实体就不能报完整流程成功。

##### ⑦ 工具声明：告诉模型参数应该怎样填写

源码：`agent/tool_defs.py`，片段起始行 261。

```python
{"type": "function", "function": {"name": "rim_estimate_axis",
 "description": "读取检查产物，调用 C++ 几何算法估计轴原点、方向；当前为 PCA 候选而非精度认证。",
 "parameters": {"type": "object", "properties": {"inspection_call_directory": {"type": "string"}},
     "required": ["inspection_call_directory"], "additionalProperties": False}}},
```

**这是工具列表中的一个字典项，不是独立 Python 程序。**

- `name` 是模型请求调用时必须使用的精确名称。
- `description` 解释用途及限制：轴线来自 C++ 候选，不是精度认证。
- `parameters.type="object"` 表示参数是 JSON 对象；`properties` 描述字段类型。
- `required` 要求必须有 `inspection_call_directory`；`additionalProperties=False` 声明不接收其他参数。

**输入示意：**`{"inspection_call_directory": "上一步真实返回的绝对目录"}`。这是数据引用，不应把假定的轴坐标塞进参数。Schema 帮助模型正确生成参数，但不能代替服务层真正的参数、路径和身份检查。

##### ⑧ 工具注册：把声明名称接到 Python 函数

源码：`agent/tools.py`，片段起始行 889。

```python
from rim_reconstruction.tool_handlers import rim_build_revolution
register_tool("rim_build_revolution", rim_build_revolution)
```

**核心是最后一行：**`register_tool("rim_build_revolution", rim_build_revolution)` 把字符串名称和处理函数关联起来。

字符串给模型和路由器使用，函数对象给 Python 调用。只有声明而没有注册，模型能提出调用，程序仍会报 Unknown tool。只注册而不声明，模型通常也不知道可以使用它。

`rim_build_revolution` 的 handler 再调用 CAD 服务和 builder。它会修改 FreeCAD 文档，必须走主线程；其他耗时截面/拟合工具可交给 `RimToolWorker`。

##### ⑨ agent/tool_dispatch.py：按工具名称分发

源码：`agent/tool_dispatch.py`，片段起始行 40。

```python
def dispatch_tool(name: str, args_json: str) -> str:
    """Dispatch a tool call by name. Returns result string."""
    handler = _TOOL_MAP.get(name)
    if handler is None:
        return f"ERROR: Unknown tool '{name}'. Available: {available_tools()}"
    try:
        log_info(f"Tool call: {name}({args_json[:200]})")
        result = handler(args_json)
```

**这是路由函数的前半段，后面省略了成功/失败日志和异常转换。**

- `_TOOL_MAP.get(name)` 查注册表；查不到就返回明确错误，不能假装执行成功。
- `args_json` 是 JSON 字符串；`handler(args_json)` 由对应处理器完成解析、校验和业务计算。
- `try` 用于捕获执行异常；函数后续会将异常转成错误结果，而不是让面板直接崩溃。

**典型路径：**`rim_estimate_axis` → 对应 handler → `estimate_model_axis()` → `CppWorkerAdapter.run()` → C++ Worker → 校验后的 JSON 结果。

##### ⑩ core/llm_client.py：把会话和工具规范发送给模型

源码：`core/llm_client.py`，片段起始行 74。

```python
def _make_request(payload: dict) -> urllib.request.Request:
    """Build a standard POST request for the LLM chat/completions endpoint."""
    return urllib.request.Request(
        #聊天地址
        _config.API_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_config.API_KEY}",
        },
    )
```

**逐项看请求：**

- 地址由 `API_BASE_URL` 拼上 `/chat/completions`；因此根地址填错会导致 404 或调用到错误接口。
- `json.dumps(payload).encode("utf-8")` 将 Python 字典转为网络发送的 JSON 字节；此 Request 含 data，因此用于 POST。
- `Content-Type` 告诉服务按 JSON 解码；`Authorization` 从运行时配置取密钥，文档不提供实际密钥。

此函数只构造 Request。`_urlopen_with_retry()` 负责连接、超时和部分重试；`call_llm_streaming()` 负责流式读取，见下一段。

##### ⑪ call_llm_streaming()：从流式响应中拿到数据块

源码：`core/llm_client.py`，片段起始行 155。

```python
payload: dict = {
    "model": _config.MODEL_NAME,
    "messages": messages,
    "temperature": temperature if temperature is not None else _config.TEMPERATURE,
    "max_tokens": _config.MAX_TOKENS,
    "stream": True,
}
if tools:
    payload["tools"] = tools
    payload["tool_choice"] = "auto"
```

**这是流式函数的请求载荷片段。**

- `model` 选择模型；`messages` 是角色化会话历史。
- `stream=True` 请求逐块返回。`max_tokens` 限制本次输出，不能当作模型总上下文大小。
- `if tools` 时加入工具 Schema 和 `tool_choice="auto"`，允许模型自行决定调用哪个工具。

函数后续读取 SSE 的 `data:` 行，遇到 `[DONE]` 结束，否则将每行 JSON `yield` 给线程消费。界面侧 `_LlmCallThread` 累积文本与分段工具参数，最终发出 `streamDone`；Loop 接收的是整理后的完整响应，不是将每个字直接拿去执行。

##### ⑫ prompts.py 与 references.py：固定规则和按需提醒

源码：`agent/loop.py`，片段起始行 215。

```python
if self._iteration == 0:
    parts.append(REF_FIRST_ITERATION)

# --- Phase-aware: quality failure guidance ---
if self._last_quality_passed is False:
    parts.append(REF_QUALITY_FAILURE)

# --- Phase-aware: repeated errors ---
if len(self._recent_errors) >= 2:
    parts.append(REF_REPAIR_LOOP)
```

**这里展示提示词如何被使用，而不是把整份长提示词复制出来。**

- 第一次执行加 `REF_FIRST_ITERATION`，提醒通用 CQ API 的常见用法。
- 质量失败时加 `REF_QUALITY_FAILURE`，帮助模型理解失败原因。
- 近期至少两次错误时加 `REF_REPAIR_LOOP`，要求改变修复策略。

这些常量定义在 `agent/references.py`，基础角色、工具使用约定等定义在 `agent/prompts.py`。完整轮辋专用提示词明确要求自动检查与估轴、显式规划角度、禁止编造点列和未经验证的精度声明。

**适用范围很重要：**`_build_context()` 在轮辋模式会提前返回已有上下文，所以这里的通用 CQ 检查清单不会照搬进轮辋流程。提示词只是指导；阶段是否通过要看程序和几何结果。

##### ⑬ core/session.py：让下一轮模型看见上一步真实结果

源码：`core/session.py`，片段起始行 76。

```python
def add_tool_result(self, tool_call_id: str, result: str):
    """添加工具执行结果。"""
    with self._lock:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })

def get_messages(self) -> list[dict]:
    """获取完整对话历史。"""
    with self._lock:
        return list(self.messages)
```

**一次工具反馈有三个关键字段：**`role="tool"` 表示来源是工具，`tool_call_id` 将结果与模型之前提出的调用绑定，`content` 保存实际结果文本/JSON。

例如模型请求 ID 为 call_A 的估轴工具，程序应把真实结果绑定到 call_A，而不是新造一个 ID。下一轮模型因此能使用工具返回的 `call_directory`。

`with self._lock` 保护多线程访问；`get_messages()` 返回列表副本，避免请求侧追加状态时直接改变会话列表。这里是列表层面的浅复制，不能理解为所有嵌套对象都深复制了。

**还需要闭环：**`AgentLoop.handle_tool_results()` 先调用 `self._rim_pipeline.record(ex)` 更新流程状态，再用 `session.add_tool_result(ex.tool_id, result_text)` 写回消息，最后准备下一次模型调用。缺少反馈消息，模型就无法依据真实结果迭代。

##### ⑭ core/token_budget.py：控制越聊越长的上下文

源码：`core/token_budget.py`，片段起始行 104。

```python
if max_tokens is None:
    max_tokens = _config.MAX_CONTEXT_TOKENS
if not messages:
    return []

total = _calibrated_total(messages)
if total <= max_tokens:
    return list(messages)

result = [dict(m) for m in messages]
```

**这是 `trim_messages()` 的开头。**默认预算从配置读取；空历史直接返回；估计未超限时返回列表副本，超限时才创建待裁剪副本。

后续策略先压缩中间的大段工具/助手内容，再把旧 `assistant(tool_calls)` 与连续 tool 结果作为组合删除，避免留下有工具结果却找不到对应调用的历史。还会使用检查点压缩和必要的内容截断，尽量保留 system 与近期信息。

估计是近似值，并结合供应商 usage 做校准，不是精确 tokenizer。轮辋把完整点列留在本地、只发摘要和路径，也是在源头降低上下文体积。

##### ⑮ core/quality.py：把几何异常转为可操作的检查结果

源码：`core/quality.py`，片段起始行 57。

```python
if code == "NO_SOLID":
    issues.append(QualityIssue(
        code="NO_SOLID", severity="fail", message=message,
        suggestion="Use Part.makeBox/makeCylinder instead of "
                   "makePolygon+extrude to create a solid.",
    ))
elif code == "MULTI_SOLID":
    issues.append(QualityIssue(
        code="MULTI_SOLID", severity="fail", message=message,
        suggestion="Shapes must PHYSICALLY OVERLAP for fuse() to "
                   "merge into one solid. Extend one shape INTO the "
                   "other by at least 0.5mm.",
    ))
```

**这是 `_map_topology_issues()` 的部分分支。**它使用稳定错误码识别问题，而不是靠匹配一段提示文字。

- `NO_SOLID` 表示没有可用实体，分类为 fail，并附带建模建议。
- `MULTI_SOLID` 表示出现多个实体，同样给出失败分类及建议。
- 最终 `QualityReport` 汇总 `passed`、`severity`、`issues` 和 `summary`。模型看到的应是这种具体反馈，而不是只有“模型不对”。

这些片段中的修复建议面向通用 CAD 建模，不能直接作为轮辋材料边界修补策略。完整轮辋模式的 `_check_quality_gate()` 会委托 `RimPipeline.completion()`，要求本轮实体步骤真实通过；轴线和扫描重建误差仍须另验。

##### ⑯ core/snapshot.py：执行出错后怎样恢复文档

源码：`core/snapshot.py`，片段起始行 154。

```python
if hasattr(doc, "saveCopy"):
    doc.saveCopy(snapshot_path)
else:
    doc.saveAs(snapshot_path)
    doc.FileName = original_path
    doc.Label = original_label

self._stacks.setdefault(doc.Name, []).append({
    "path": snapshot_path,
    "doc_name": doc.Name,
    "original_path": original_path,
    "original_label": original_label,
    "session_id": self._session_id,
    "time": time.time(),
})
```

**这是快照保存方法中的核心片段，文件名与目录在此前已生成。**

- 优先 `saveCopy()`，避免改变当前文档的正式文件路径和标签。
- 兼容分支调用 `saveAs()` 后，恢复原来的 FileName/Label。
- `_stacks.setdefault(doc.Name, []).append(...)` 按文档保存快照记录，避免多个文档的撤销互相混淆。

`restore_latest_with_entry()` 在恢复失败时把记录放回栈，成功才删除对应快照。快照服务主要支撑通用 `execute_code`/`undo_last`；轮辋 builder 还使用自己的 FreeCAD 文档事务回滚。它们不是同一套机制，也不是无限历史备份，应另行保存正式 FCStd。

##### ⑰ core/session_store.py：退出 FreeCAD 后怎样保留聊天历史

源码：`core/session_store.py`，片段起始行 31。

```python
data = session.to_dict()
path = os.path.join(self._dir, f"{session.session_id}.json")
tmp_path = path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp_path, path)
```

**这段是 `SessionStore.save()` 的核心。**`to_dict()` 将会话转成可序列化数据；先写 `.tmp` 再 `os.replace()`，降低写到一半留下损坏正式 JSON 的风险。

消息历史持久化与几何模型保存不同：会话 JSON 不能代替 FCStd，单独一份拟合摘要也不能代替完整 Worker 调用目录。`core/paths.py` 统一决定 sessions、snapshots、log 等用户数据位置。

##### 把这些文件串起来：一次真实调用的阅读顺序

```text
InitGui.Activated() → AgentPanel
  → 创建 Controller(session) 与 AgentLoop(workflow)
  → Loop.start() → prepare_llm_call() → CALL_LLM
  → Panel._execute_action() → 后台 _LlmCallThread
  → llm_client.call_llm_streaming() → 模型返回 tool_calls
  → Panel._on_stream_done() → Loop.handle_stream_done()
  → EXECUTE_TOOLS → 阶段校验 → 后台算法/主线程 CAD
  → ToolExecution → RimPipeline.record() + ChatSession.add_tool_result()
  → 下一轮 CALL_LLM，或质量门禁通过后 FINISH
```

**建议练习：**给一次 `rim_estimate_axis` 调用找到四样东西：声明中的必填参数、注册入口、实际服务函数、返回结果在下一轮消息中的位置。能沿这四处追踪，就理解了这个 Agent 的工具闭环。阅读业务主链时先关注这些字段和分支，不必先弄懂所有 Qt 样式、日志或完整 B 样条求解细节。

### 2.3 工具调用如何接到真实函数

工具有两份必须对应的东西：声明和实现。

```text
tool_defs.py：工具名 + 参数规范
             ↓ 随请求发送给模型
模型响应：工具名 + JSON 参数
             ↓ 阶段/参数校验
dispatch_tool：按名称查找已注册 handler
             ↓
handler → 服务函数 → 真实计算
             ↓
工具结果写回会话 → 模型读取反馈
```

新增工具只写 JSON Schema 不会让它自动执行。还要注册 handler，并处理对应线程、错误结果和阶段状态。当前注册入口位于 `agent/tools.py` 文件末尾。

`tool_result_is_error()` 同时兼容原工具的 `ERROR` / `FAIL` 文本与轮辋工具的 JSON `status=error/cancelled`，避免把失败结果当成成功。

### 2.4 通用 CAD 模式原来有什么

原项目提供 `execute_code`、`undo_last`、`capture_view`、`analyze_image` 和 `export_step` 五个通用工具。模型可以生成类似 CadQuery 的链式代码，通过 `agent/cq.py` 转成 FreeCAD Part 操作。

例如 `cq.Workplane("XY").box(10,20,30)` 表示建立长方体。这是项目内置的 CQ 风格翻译层，不需要安装完整 CadQuery。

代码执行后，系统检查固体、拓扑和尺寸，并将错误或质量反馈交给模型修正。截图可辅助观察外观，无法代替数值检查。通用执行器运行模型生成的代码，不应把它理解成用于执行任意不可信代码的强安全隔离环境。

### 2.5 调用协议与任务模式是两回事

- 通用模式可适配原生 Tool Calling，也有 ReAct XML 回退路径。
- 轮辋流程在 `AgentLoop.__init__` 中强制使用原生 Tool Calling，因此模型服务必须真正支持结构化工具调用。
- `workflow="rim_reconstruction"` 表示完整轮辋任务；它与模型调用协议 `mode` 不是同一个概念。

只配置一个会回复文字的模型还不足以复现轮辋 Agent，必须验证它能返回有效的工具调用参数。

## 三、轮辋逆向模块怎样与原 Agent 结合

### 3.1 接入位置

项目根目录下的 `agent/` 是当前工作台实际使用的 Agent。`rim_reconstruction/agent/` 中还保留了早期圆柱演示代码，不能把那个目录的 `controller.py` 或 `tool_defs.py` 当作完整工作台入口。

```text
CadAgent-main/
├─ InitGui.py                 工作台入口
├─ agent/                     实际 Agent
│  ├─ loop.py                 通用循环 + 轮辋接入
│  ├─ rim_pipeline.py         轮辋阶段门禁
│  ├─ prompts.py              专用提示词
│  ├─ tool_defs.py            实际工具声明
│  └─ tools.py                实际工具注册
├─ ui/rim_tool_worker.py      后台轮辋计算
├─ config/rim_geometry_worker.json  默认生效的 Worker 配置
├─ rim_reconstruction/
│  ├─ tool_handlers.py        JSON 参数/结果封装
│  ├─ adapters/               Python 与 C++ 桥接
│  ├─ services/               业务步骤
│  ├─ geometry/               筛选与拟合算法
│  └─ cad/                    FreeCAD 原生特征构建
├─ stlload_hub/               C++ 几何源码与 CMake
├─ runtime/geometry_calls/    本地计算产物
└─ tests/                    工作台与扩展测试
```

### 3.2 六个主步骤，加一个可选修补工具

当前 `RIM_RECONSTRUCTION_TOOL_NAMES` 实际列出七个工具。部分注释与旧 WORKFLOW 文档仍写“六个工具”，那是尚未计入修补工具的表述。

| 顺序 | 工具 | 输入要点 | 输出与下一步 |
| --- | --- | --- | --- |
| 1 | `rim_inspect_stl` | `stl_path`，单位默认 mm | 自动任务 ID、网格统计、检查目录 |
| 2 | `rim_estimate_axis` | 上一步的 `inspection_call_directory` | PCA 候选轴、径向参考、轴线目录 |
| 3 | `rim_process_sections_auto` | 轴线目录、角度列表及选择理由 | 有序截面、闭合统计、截面目录 |
| 可选 | `rim_repair_sections` | 当前截面 `call_directory` | 沿原轴和原角度重新切片、修补，返回新目录 |
| 4 | `rim_analyze_candidates` | 当前截面目录 | 分析文件、候选 ID、片段 ID 和摘要 |
| 5 | `rim_fit_profile` | 分析路径、候选 ID、片段 ID | 完整拟合文件 `result_path`、误差摘要 |
| 6 | `rim_build_revolution` | 成功拟合的 `fit_path` | 草图、回转特征、单实体体积和对象名 |

完整模式不向模型开放任意几何脚本。工具签名中的目录与 ID 必须来自本轮真实结果，不能手工猜测。

### 3.3 检查、估轴与角度规划

`services/model_service.py` 中：

- `inspect_model()` 自动创建任务 ID，然后调用 C++ `inspect_stl`。
- `estimate_model_axis()` 复查检查产物，再调用 C++ `estimate_axis`。它不接受模型手填的轴坐标。
- `_radial()` 用正交化计算径向基准，避免把这项向量运算交给模型。
- `process_model_sections()` 读取真实轴线，要求模型显式提供 3～360 个不重复的 `[0,360)` 角度，以及 `angle_selection_reason`。

角度没有固定默认值。首次只是探索性采样，不能凭包围盒摘要声称已经识别轮辐相位。后续可根据开放轮廓、闭合数量与跨角度支持情况调整。重新采样必须提交本轮完整角度集合，需要保留的旧角度也要列入。

### 3.4 C++ 截面和条件修补

`stlload_hub/geometry_worker.cpp` 是无 GUI 命令行入口，复用几何核心。一个进程处理一个 JSON 请求，通过 `--request` 和 `--result` 读写文件。

`compute_sections` 保存原始求交线段；生产流程使用 `process_sections`，进一步进行端点去重、排序、条件拼接/闭合和可选毛刺清理。截面是经过回转轴、沿指定径向延伸的半平面。

`contours` 中每条轮廓独立保存，`closed=true` 时首点通常不重复，绘图时连末点到首点。`closed=false` 不允许自行补线闭合。条件修补不保证能修复所有孔洞，也不认证材料边界语义或无自交。

`repair_sections()` 是重新提取式封装：保留原 STL、轴线、角度和清洗参数，生成新调用目录，不原地覆盖旧点列。正常 `process_sections` 本身已包含修补逻辑，不必每次重复调用独立修补工具。

### 3.5 筛选轮辋候选的规则

`geometry/rim_profile_selector.py` 的 `analyze_candidates()` 对每个角度的轮廓进行分析：

1. 至少存在两条有效闭合轮廓才参与外围选择。
2. 按弧长加权平均半径选择最大者；这样可减少网格采样密度对排序的干扰。
3. 半径并列、只有一条有效闭合轮廓或只有开放轮廓时，不输出该角度候选。
4. 保留选中轮廓的源点和闭合边；跨角度双向匹配用于验证与排序，不删除不匹配边。

`support_ratio` 是候选之间平均双向覆盖分数，不是旧版逐边投票比例。只有一个候选时为 `null`，表示缺少跨角度验证。`source_length_coverage=1` 只说明保留了源轮廓长度，不代表已识别正确、完整的轮辋材料区域。

服务层 `analyze_rim_candidates()` 只把候选摘要传给模型，完整点列留在 `analyses/analysis_*.json`。`status=success` 只代表分析执行完成，还要检查 `analysis_status` 是否为 `candidates_found`。

### 3.6 B 样条拟合：模型选 ID，程序处理坐标

`services/fitting_service.py` 的 `fit_selected_profile()` 验证候选属于当前分析，片段属于源轮廓，并重新从原始截面计算二维坐标。坐标顺序固定为 `[axial, radius]`，即轴向距离与径向距离。

若源点、片段索引或保存坐标不一致则拒绝拟合；不平均多个角度的点列，也不跨缺口连接。模型选择候选和片段不需要人工确认弹窗。

`geometry/bspline_fitter.py` 负责实际数值求解：开放轮廓使用非周期三次样条，闭合光滑轮廓使用周期三次样条；尖角需预先分段，当前不能宣称已经自动保持所有尖角。

默认参数：`tolerance_mm=0.08`、`max_control_points=256`、`max_iterations=12`、`sample_count=500`。平滑参数不是毫米误差，程序另行检查输入点在弦长参数位置的最大残差：

```text
最大残差 = max ||拟合曲线 C(u_i) - 输入点 P_i||
```

这个指标不等于原始折线处处的连续误差上界，更不等于整个三维回转体相对 STL 的误差。不能为了“成功”擅自放宽容差。

### 3.7 创建原生草图和回转特征

`cad/freecad_rim_builder.py` 的 `build_rim_from_fit()` 读取完整拟合 JSON，按控制点、节点和重数精确导入曲线，不对采样点再次拟合。

它依次检查：端点连接、非负半径、闭合 Wire、有效 Face、试回转是否产生正体积单实体，然后创建真正的 Sketcher 与 PartDesign 特征。失败时回滚文档事务。

默认连接容差是 `1e-7 mm`，与 0.08 mm 拟合容差用途不同。较大缺口直接拒绝，不补线。小于连接容差的接缝可能经重合约束微调，也不能因此保证误差完全不变。

草图局部 X 为轴向、Y 为径向，绕 `H_Axis` 回转 360°；Body 根据同次截面请求中的轴线放回原位置。应保留完整调用目录，避免孤立拟合文件丢失源坐标信息。

### 3.8 阶段门禁与线程

`agent/rim_pipeline.py` 的 `RimPipeline` 是流程可靠性的核心：

- `allowed_tools()` 给出当前允许执行的动作。
- `validate_call()` 检查前置步骤以及参数是否引用本轮产物。
- `record()` 根据真实工具结果记录状态；上游重试，即使失败或取消，也使下游状态失效。
- `request_context()` 只提供精简状态和引用，不把全部点列送进模型。
- `completion()` 要求本轮已实际创建草图、回转特征和有效单实体。

```text
检查 → 估轴 → 截面 → 分析 → 拟合 → 实体
              │       ▲
              └→ 修补 ┘

重做任意上游步骤 → 清除其下游状态 → 重新计算
```

完整模式每轮只执行一个工具，七个工具声明始终可见，执行资格由阶段门禁决定。

LLM 请求与耗时轮辋算法在后台运行；`RimToolWorker` 负责外部进程、分析与拟合，FreeCAD 文档变更必须回到面板主线程。停止请求在取消检查点处理，SciPy 单次求解与 FreeCAD 内核操作不能保证立即中断。

## 四、准备复现环境

### 4.1 需要准备的东西

| 项目 | 本机核对到的配置 | 复现要求 |
| --- | --- | --- |
| FreeCAD | 1.1.4，`D:/FreeCAD1.1` | 优先使用同系列，带 PySide6、Sketcher、PartDesign |
| Python 数值库 | 工作流说明记录 SciPy 1.16 | 实际运行 Agent 的 FreeCAD Python 必须能导入 NumPy、SciPy；当前拟合器还导入 `make_splprep` |
| 模型服务 | 根 `.env` 或 Settings 配置 | 兼容 Chat Completions，支持原生 Tool Calling |
| C++ Worker | `runtime/build-repair/geometry_worker.exe` | 连同配套 DLL 一起保留 |
| C++ 构建依赖 | Qt 6.11.2 MSVC x64、OCCT、Ninja、CMake | 仅自行编译 Worker 时需要，编译器/架构/配置须匹配 |
| 输入样本 | `E:/hub_stl/hubmain3processed.stl` | 这份大文件不等于随项目自动分发；换机器要单独准备 |

先在 PowerShell 中执行（请替换 FreeCAD 路径）：

```powershell
Set-Location E:/hubagent/CadAgent-main
& D:/FreeCAD1.1/bin/python.exe -c "import FreeCAD, Part, Sketcher, numpy, scipy; from scipy.interpolate import make_splprep; print('FreeCAD:', FreeCAD.Version()); print('NumPy:', numpy.__version__); print('SciPy:', scipy.__version__)"
Test-Path ./runtime/build-repair/geometry_worker.exe
Test-Path E:/hub_stl/hubmain3processed.stl
```

预期：依赖导入成功，两个路径检查返回 True。普通系统 Python 能导入 SciPy，不代表 FreeCAD 自带 Python 也能导入。缺依赖时先修复对应环境，再继续。

### 4.2 拿到正确的源码包

完整保留 `agent`、`core`、`ui`、`resources`、`rim_reconstruction`、`config`、`stlload_hub`、`tests` 和入口文件。使用现成 Worker 时，还需其可执行程序和匹配 DLL。运行产物体积可能很大，可与源码包分开保管。

项目 README 与根 LICENSE 声明 CadAgent 为 MIT；分发整合包仍需同时检查各依赖及 C++ 子项目自身的许可证要求。

迁移时记录：源码包版本/哈希、FreeCAD 版本、NumPy/SciPy 版本、Worker 构建配置、模型服务与模型名称、输入 STL 版本和所有配置。相同提示词不保证不同模型给出相同角度和候选，确定性复现方式见第七节。

### 4.3 已有 Worker：先独立验证

```powershell
& ./runtime/build-repair/geometry_worker.exe --help
```

预期显示 CLI 帮助并正常退出。若提示缺 DLL，恢复同次构建部署的 DLL，不能随便从 FreeCAD 或其他 OCCT 安装复制同名文件。

### 4.4 需要自行编译 Worker 时

本机 CMake 缓存显示：Release、Ninja、MSVC x64、`Qt6_DIR=D:/Qt/6.11.2/msvc2022_64/lib/cmake/Qt6`、`OpenCASCADE_DIR=E:/opencascade/install/cmake`。不要把本机的 CMakeCache 原样搬到另一台电脑。

在已经初始化 MSVC x64 编译环境的终端中运行下面模板，Qt 与 OCCT 路径必须换成自己的实际安装目录：

```powershell
cmake -S ./stlload_hub -B ./runtime/build-local -G Ninja -DCMAKE_BUILD_TYPE=Release -DSTLLOAD_BUILD_GUI=OFF -DBUILD_TESTING=ON -DCMAKE_PREFIX_PATH="D:/Qt/6.11.2/msvc2022_64" -DOpenCASCADE_DIR="E:/opencascade/install/cmake"
cmake --build ./runtime/build-local --target geometry_worker test_contour_repair
ctest --test-dir ./runtime/build-local --output-on-failure
```

构建完成后，检查 `runtime/build-local/geometry_worker.exe` 和部署的依赖文件。`deploy_matching_runtime()` 会尝试复制 CMake 导入目标对应的 DLL。若改用 Visual Studio 等多配置生成器，需要指定 Release 配置，并按真实产出目录修改 Worker 配置。

此命令模板来自当前 CMake 接口；本次文档整理没有重新编译 C++，不能把它当作另一台机器已经通过的构建记录。

### 4.5 配置默认 Worker 路径

编辑根目录 `config/rim_geometry_worker.json`。默认 `CppWorkerAdapter.from_config()` 使用这一份，而不是 `rim_reconstruction/config` 中的历史副本。

现成 Worker 的本机示例：

```json
{
  "schema_version": 1,
  "base_directory": "E:/hubagent/CadAgent-main",
  "executable": "E:/hubagent/CadAgent-main/runtime/build-repair/geometry_worker.exe",
  "call_directory": "E:/hubagent/CadAgent-main/runtime/geometry_calls",
  "timeout_seconds": 120,
  "max_result_bytes": 2097152,
  "max_artifact_bytes": 134217728
}
```

使用自己编译的 Worker 就把 `executable` 改成真实的 `build-local` 产出路径。`call_directory` 必须可写。大 STL 若确实超时，可依据日志合理调整运行超时；不要混同为几何容差调整。

根 `config/rim_reconstruction_config.json` 还含早期方案状态和示例角度，不应当成当前完整 Agent 的默认角度配置。现流程自动生成任务 ID、估计轴线，并要求模型显式规划角度。

### 4.6 配置模型

首次使用时将根 `.env.example` 复制成根 `.env`，已有 `.env` 时保留原配置。也可在 FreeCAD CadAgent Settings 中配置并测试连接。

```ini
API_BASE_URL=https://你的模型服务地址/v1
API_KEY=你的实际密钥
MODEL_NAME=你的支持工具调用的模型
MAX_ITERATIONS=20
LLM_TIMEOUT=180
```

示例地址是占位符，不能直接运行。服务有时不使用 `/v1` 后缀，应以供应商实际接口及项目设置为准。`core/config.py` 默认从根 `.env` 加载，而且不覆盖已存在的进程环境变量；改了文件仍无效时，也要检查启动环境并重启 FreeCAD。

轮辋基本流程不依赖视觉模型。截图/参考图工具另需视觉 API 配置。不要把真实密钥放进共享教程或复现包。

### 4.7 安装到 FreeCAD 工作台

1. 在 FreeCAD Python 控制台执行 `print(App.getUserAppDataDir())`，确认实际用户数据目录。
2. 关闭 FreeCAD，备份已有插件和配置。
3. 将插件源码部署到该目录下的 `Mod/CadAgent`，保证 `InitGui.py` 直接位于 CadAgent 文件夹内，避免多套一层目录。
4. 将实际使用的根 `.env` 与根 Worker 配置部署到插件副本。Worker 可继续指向外部源码目录中的可执行文件和运行目录，但这些绝对路径必须仍有效。
5. 重启 FreeCAD，选择 CadAgent 工作台，确认聊天面板及“轮辋逆向重建”模式出现。

本机曾检测到插件路径：`C:/Users/Wang/AppData/Roaming/FreeCAD/v1-1/Mod/CadAgent`。不要仅根据通用 `%APPDATA%/FreeCAD/Mod` 示例判断当前安装位置。

在 FreeCAD 控制台检查实际导入位置：

```python
import agent.loop
from rim_reconstruction.adapters.cpp_worker_adapter import CppWorkerAdapter
print(agent.loop.__file__)
adapter = CppWorkerAdapter.from_config()
print(adapter.executable)
print(adapter.call_root)
```

预期路径指向你刚部署的版本。修改 `E:/hubagent/CadAgent-main` 不会自动更新已经安装到 Mod 的副本；重启也不会替你同步源码。

## 五、按步骤运行一次完整 Agent

### 5.1 先跑基础检查和确定性测试

在源码根目录执行：

```powershell
& D:/FreeCAD1.1/bin/python.exe -B -m unittest tests.test_rim_pipeline tests.test_rim_tool_defs tests.test_rim_end_to_end tests.test_freecad_rim_builder -v
```

端到端测试会生成合成双回转环 STL，运行真实 Worker、筛选、拟合和 FreeCAD 特征构建。模型角度和候选决策由测试替身给出，不请求付费模型 API。它能确认软件链条衔接，但不能证明任意扫描轮毂都可成功重建。

### 5.2 在界面发起任务

切换到“轮辋逆向重建”，在新任务/会话中输入：

> 请重建 E:/hub_stl/hubmain3processed.stl 的轮辋回转主体，单位为毫米。请先检查 STL 并估计轴线，再依据几何摘要规划多个截面角度，分析外围闭合候选，选择合适候选及片段，以 0.08 mm 拟合阈值创建原生草图和 360° 回转单实体。若无合格候选、拟合失败或材料截面不闭合，请报告原因，不要补线或放宽阈值；说明轴线及整体重建精度尚未验证。

替换为自己的 STL 绝对路径。不必手填任务 ID、轴原点、轴方向和径向参考。

### 5.3 观察执行顺序

通常应看到检查、估轴、自动截面、分析、拟合、创建实体。修补或重新选角会产生额外迭代。模型不同，角度与候选也可能不同。

逐阶段应核对：

| 阶段 | 可继续的证据 | 常见误判 |
| --- | --- | --- |
| 检查 | 真实文件读入，有任务 ID 和调用目录 | 文件存在就认为网格无孔洞或自交 |
| 估轴 | 有候选轴和径向，保留未验证警告 | 把 PCA 初值称作精确回转轴 |
| 截面 | 有 section 产物、轮廓与闭合数量 | `status=success` 就认为已有轮辋 |
| 分析 | `analysis_status=candidates_found` | `needs_review` 时硬选不存在的候选 |
| 拟合 | `fit_success=true`、`compliant=true`、真实 `result_path` | 局部拟合通过就认为全轮廓可回转 |
| 建模 | 草图/回转对象齐全，单实体，正体积 | 截图看起来像轮辋就视为工程精度通过 |

### 5.4 找到实际产物

典型目录结构如下；每次调用的名称由程序生成：

```text
runtime/geometry_calls/
├─ call-检查编号/
│  ├─ request.json
│  └─ result.json
├─ call-轴线编号/
│  ├─ request.json
│  └─ result.json
└─ call-截面编号/
   ├─ request.json
   ├─ result.json
   ├─ geometry/run-编号/section_*.json
   ├─ analyses/analysis_编号.json
   └─ fits/fit_编号.json
```

目录归属和 ID 都被适配器与服务复验。不要移动、改写局部 JSON 后再交给下一步，也不要从旧任务复制路径来“继续”新任务。需要共享复现记录时保留该轮完整目录。

### 5.5 验证并保存结果

在 FreeCAD 树中找到本轮 `RimRevolution`，检查草图存在、回转角度为 360°。可在 Python 控制台检查（实际名称若带后缀请替换）：

```python
doc = App.ActiveDocument
rev = doc.getObject("RimRevolution")
print(rev.Shape.isValid())
print(len(rev.Shape.Solids))
print(rev.Shape.Volume)
```

预期分别为 True、1、正数。多个任务文档并存时，应从建模结果的 `document_name` 定位文档，而不是直接假设当前激活文档正确。

通过 FreeCAD 文件菜单保存 `.FCStd`，保留草图与特征树；需要交换 CAD 数据时选择最终 Body/回转特征导出 STEP。不要把所有中间对象一起导出。STEP 不保存 FreeCAD 原生设计历史，应同时保留 FCStd。

完整轮辋模式列出的七个工具中没有 `export_step`，因此不要指望该模式一定自动调用通用导出工具。本流程的实体成功也不意味着已经保存文件。

## 六、验收记录与故障排查

### 6.1 复现完成检查表

- [ ] FreeCAD 能加载工作台，实际模块路径指向本次部署版本。
- [ ] FreeCAD Python 的 FreeCAD、Sketcher、NumPy、SciPy 依赖检查通过。
- [ ] Worker 可独立启动，根配置中的可执行文件与输出目录有效。
- [ ] 模型能返回真实 Tool Calling，而非仅描述工具名称。
- [ ] 合成端到端测试通过，且没有因为缺依赖而被跳过。
- [ ] 实际 STL 的逐步结果和警告被保存，使用当前产物引用。
- [ ] 若成功，确有草图、回转特征、有效单实体与正体积。
- [ ] FCStd 与需要的 STEP 已另行保存并重新打开检查。
- [ ] 报告中保留轴线未验证、轮辋语义及整体精度尚未认证的限制。

### 6.2 按症状定位

| 症状 | 优先检查 | 处理方法 |
| --- | --- | --- |
| 工作台没有出现 | 目录层级、InitGui.py、报告视图 | 确认实际用户 Mod 路径和插件加载错误 |
| 界面没有轮辋模式 | 导入模块的 `__file__` | 同步新源码到安装副本并重启 |
| 模型只回复“将调用工具” | 原生 Tool Calling 支持 | 验证模型/服务能力和返回格式，轮辋模式不走 XML 回退 |
| Worker 无法启动或异常退出 | exe、DLL、架构、配置 | 保留整套匹配运行时；不要混用 FreeCAD 的同名 OCCT DLL |
| 找不到配置或使用旧路径 | 根 config 与历史副本 | 检查 `CppWorkerAdapter.from_config()` 读取的根配置 |
| 数值库导入失败 | FreeCAD Python 环境 | 用同一个解释器修复依赖，不能只检查系统 Python |
| 截面成功但没有候选 | 闭合数量与筛选规则 | 查看是否每角度至少两条有效闭合轮廓，再考虑修补或换角度 |
| 修补后仍开放 | 修补警告与原网格 | 如实报告，不强制连接不可靠边界 |
| 候选很多但拟合失败 | 分段、尖角、点数、误差 | 检查所选片段连续性和拟合报告，不随意放宽容差 |
| 拟合通过但无法回转 | 缺口、负半径、自交 | 查看 builder 错误；局部曲线拟合不等于完整材料截面 |
| 停止按钮未立刻生效 | 当前内核/数值操作阶段 | 等当前不可中断调用返回；后台取消有检查点 |
| 换角度后旧拟合失效 | 阶段门禁 | 这是预期行为，需要从当前截面重新分析和拟合 |
| 测试输出 OK 但有 skipped | 测试运行环境 | 跳过不等于通过；改用 FreeCAD Python 并确认 Worker 存在 |

Windows 中 Worker 启动桥接会临时清除继承的 DLL 搜索目录，启动后恢复，以降低加载 FreeCAD 不匹配 DLL 的风险。不要通过永久修改整机 DLL 搜索路径来代替部署匹配依赖。

### 6.3 本次实际验证与已知差异

本文整理时在当前源码根目录执行了：

```powershell
& D:/FreeCAD1.1/bin/python.exe -B -m unittest tests.test_rim_pipeline tests.test_rim_workflow tests.test_rim_tool_defs tests.test_rim_end_to_end tests.test_freecad_rim_builder
```

实际结果：26 项测试，25 项通过，1 项失败，无跳过。失败项是 `tests.test_rim_workflow.RimWorkflowTests.test_rim_prompt_tools_and_protocol`：旧测试期望的工具列表遗漏了已加入的 `rim_repair_sections`。它检查的是历史截面模式工具声明，并不意味着真实几何端到端测试失败。本文没有修改业务代码或测试来掩盖这一差异，也不声称全套测试通过。

随后对第五节列出的核心复现命令进行了单独验证：16 项全部通过、无跳过，包含阶段门禁、工具声明、真实几何端到端及原生特征构建检查。

真实几何端到端测试与 FreeCAD builder 测试包含在通过项中；本次没有重新运行真实扫描 STL 的完整 LLM 对话，也没有认证该样本的最终工程精度。

历史 `REPAIR_AND_SELECTION.md` 记录过真实样本修补：14 角度的闭合轮廓 5→15、开放轮廓 25→15，120°、143°、199°、227° 得到四条完整闭合候选。这是历史截面/筛选验证，不能当作本次完整实体重建结果。

## 七、开发者：不依赖模型单独复现工具链

这条路径用于排除网络与模型决策差异。它不能代替最终的聊天 Agent 验证，但可定位“算法坏了”还是“模型参数不对”。

先在源码根目录用 FreeCAD 自带 Python，或在 FreeCAD Python 控制台加入正确源码路径。下例前三步是确定性接口演示；角度仅为示例，不是生产默认，也不是针对实际轮辐的已验证方案。

```python
from rim_reconstruction.services.model_service import (
    inspect_model, estimate_model_axis, process_model_sections,
)
from rim_reconstruction.services.candidate_service import analyze_rim_candidates

inspection = inspect_model({"stl_path": "E:/hub_stl/hubmain3processed.stl"})
axis = estimate_model_axis({
    "inspection_call_directory": inspection["call_directory"]
})
sections = process_model_sections({
    "axis_call_directory": axis["call_directory"],
    "angles_degrees": [0, 47, 103, 169],
    "angle_selection_reason": "接口复现用探索采样；不是生产默认或轮辐识别结果"
})
analysis = analyze_rim_candidates({"call_directory": sections["call_directory"]})
print(analysis)
```

检查 `analysis_status`，读取候选摘要并依据几何选择 `candidate_id` 与 `segment_ids`，不能无条件选第一项。然后调用：

```python
from rim_reconstruction.services.fitting_service import fit_selected_profile

# selected_candidate_id 和 selected_segment_ids 必须先从本轮 analysis 中确定。
fit = fit_selected_profile({
    "analysis_path": analysis["analysis_path"],
    "candidate_id": selected_candidate_id,
    "segment_ids": selected_segment_ids,
    "tolerance_mm": 0.08
})
print(fit)
```

这不是无条件可复制到底的样例，因为真实样本可能没有合格候选；不能为了凑齐示例编造 ID。若要一条命令复现已具备明确目标的完整计算，请使用第五节的合成端到端测试。

确认拟合成功后，仅在 FreeCAD 主线程执行：

```python
from rim_reconstruction.cad.freecad_rim_builder import build_rim_from_fit
result = build_rim_from_fit(fit["result_path"])
print(result)
```

也可在 FreeCAD 宏管理器打开 `scripts/build_rim_from_fit.FCMacro`，选择真实成功的完整拟合 JSON。宏不会自动保存文件。服务直接调用路径跳过了 Agent 的部分阶段编排，因此开发者必须自己按顺序检查每一步的成功状态。

项目还有旧 `verify_worker_bridge.py`，其默认配置文件名与当前文件布局不一致。使用时明确传入参数：

```powershell
& D:/FreeCAD1.1/bin/python.exe -B ./rim_reconstruction/scripts/verify_worker_bridge.py --worker-config ./config/rim_geometry_worker.json --sample-config ./config/rim_reconstruction_config.json
```

该命令只检查 STL 与估轴桥接，不拟合、不建实体，也不验证正式回转轴。不要将它的 `status=success` 解释为完整重建通过。

## 八、继续扩展这个 Agent 时应保留的设计

新增领域能力可沿以下顺序接入：写参数 Schema → 写 handler 和服务 → 注册工具 → 决定后台或主线程 → 更新阶段门禁与提示词 → 增加有意义的流程与几何验证。

本项目最值得复用的是“摘要交给模型、完整几何留在本地、关键判断由程序检查”的分工。后续若要提高轮辋工程可靠性，应优先补充更强的轴线拟合/验证、轮辋材料边界识别、尖角与工程尺寸约束、三维误差评估，而不是只增加提示词或让模型生成更多点。

## 附录：对应源码与参考说明

下面均为本次整理依据的项目内文件，迁移时按相对路径查阅：

- `README.md`：通用工作台设计、安装与原五工具说明。
- `agent/loop.py`、`agent/tool_defs.py`、`agent/rim_pipeline.py`：当前实际协议、工具列表和阶段门禁。
- `agent/tools.py`、`agent/tool_dispatch.py`：工具实现注册与路由。
- `core/config.py`、`core/paths.py`：模型配置及用户数据目录策略。
- `rim_reconstruction/services/`：检查、轴线、截面、修补、分析、拟合、建模服务。
- `rim_reconstruction/adapters/cpp_worker_adapter.py`：子进程、超时、取消与产物验证。
- `rim_reconstruction/geometry/rim_profile_selector.py`、`bspline_fitter.py`：筛选与数值拟合。
- `rim_reconstruction/cad/freecad_rim_builder.py`、`cad/README.md`：特征树及连接条件。
- `ui/rim_tool_worker.py`：后台计算与取消。
- `stlload_hub/CMakeLists.txt`、`geometry_worker.cpp`：构建与 C++ 协议入口。
- `rim_reconstruction/WORKFLOW.md`、`REPAIR_AND_SELECTION.md`：历史流程及真实样本验证记录；与当前代码冲突时以已核对的代码为准。
- `tests/test_rim_end_to_end.py`、`tests/test_freecad_rim_builder.py`：真实 Worker 和 FreeCAD 内核测试。

保留复现包时，建议附上源码快照、脱敏配置、依赖版本、输入样本标识、完整调用产物、测试输出及 FCStd/STEP。这样后来者能追溯模型每一步使用了什么数据，而不仅是看到一张最终截图。
