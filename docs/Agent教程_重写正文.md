# 轮辋逆向重建 Agent：理解机制并完成复现

本文面向第一次学习 Agent 的读者。阅读主线只有一条：用户提交 STL → 程序请求大模型 → 模型选择工具 → 本地执行 → 结果进入下一轮 → 创建 FreeCAD 实体。

正文按任务执行顺序讲解，详细文件解读放在附录。想先运行的读者，可先完成第八章环境准备，再回到第二至六章理解机制。

**版本说明：**以 2026 年 11 月 11 日本机 `E:/hubagent/CadAgent-main` 核对的源码为准。它是已整合轮辋功能的本地版本，不等于任意同名上游仓库。文中的 `call-A`、`call-B` 和 `call-C` 都是教学代号，不是可以照抄的真实路径；API 密钥与模型名称须自行配置。

## 第一章：先看这套系统到底完成什么

### 1.1 从输入到输出

用户在 FreeCAD 的 CadAgent 面板中选择“轮辋逆向重建”，提交类似任务：

> 请重建 E:/hub_stl/hubmain3processed.stl 的轮辋回转主体，输入单位为毫米。

系统尝试从三角网格提取轮辋材料截面，然后建立草图和 360° 回转特征。输出不是把 STL 换个文件格式，而是创建以下原生特征树：

```text
RimReconstruction（FreeCAD 文档，名称可能带后缀）
└─ RimBody                  PartDesign::Body
   ├─ RimProfile            Sketcher::SketchObject
   └─ RimRevolution         PartDesign::Revolution
```

STL 可以理解为由很多三角面拼成的外形；草图与回转特征则保留了“用一条材料截面绕轴旋转”的建模步骤。FCStd 可以保存这套特征树，STEP 用于交换最终几何。

### 1.2 谁在做决定，谁在做计算

```text
用户：任务、STL 路径
            ↓
Agent 程序 ←→ 大模型 API
    │          提出工具调用与参数
    │ 校验后执行
    ├─ C++ Worker：读网格、估轴、提取与处理截面
    ├─ Python：筛选候选、拟合 B 样条
    └─ FreeCAD：创建草图和回转实体
            ↓
真实结果 → 写入对话与阶段状态 → 下一次请求
```

模型负责理解任务和选择动作，例如决定先探索哪些角度、选择哪个候选。几何算法负责实际计算。Agent 程序负责组织消息、执行工具、检查引用和控制循环。模型服务不会直接访问本机 FreeCAD。

### 1.3 本教程中“成功”的含义

工具返回成功，只说明该工具执行完成；分析工具成功仍可能没有合格候选。完整重建成功要求本轮确实建立草图和有效单实体，且体积大于零。

这仍不等于工程验收通过。当前重建主流程的 PCA 轴线是初值，返回 `axis_validated=false`；默认 0.08 mm 检查的是所选点列的拟合残差，不能直接视为整个回转体相对 STL 的误差。草图可编辑，但没有自动补齐壁厚、宽度等全部尺寸约束。先记住这个边界，后面分别解释每一步检查什么。

## 第二章：接入大模型 API，让程序收到结构化响应

### 2.1 先理解 API 请求包含什么

API 是本地程序向模型服务发送任务的接口。一次请求主要包含：模型名称 `model`、对话消息 `messages`、可用工具规范 `tools`。服务响应可能包含普通文字，也可能包含工具调用 `tool_calls`。

先不要读网络重试或界面线程。我们要验证的第一件事是：程序能把任务发出去，并收到一个参数完整的工具调用。

### 2.2 配置地址、密钥和模型

操作位置：整合源码根目录。首次将 `.env.example` 复制为 `.env`；已有配置时不要覆盖。也可在 CadAgent Settings 中填写。

```ini
API_BASE_URL=https://你的服务根地址/v1
API_KEY=你的实际密钥
MODEL_NAME=支持原生工具调用的模型名称
MAX_ITERATIONS=20
LLM_TIMEOUT=180
```

上面的地址和模型都是占位符，不能原样运行。`core/config.py` 读取根 `.env` 并形成配置。已经存在的进程环境变量优先于 `.env`，因此修改后应重启 FreeCAD，必要时检查启动环境。

客户端会给根地址追加 `/chat/completions`。如果配置已经包含这个后缀，就会重复拼接。`API_KEY` 放在 HTTP 请求头中，不是提示词的一部分，也不应写入共享文档。

### 2.3 核心代码：把 Python 数据变成 HTTP 请求

位置：`core/llm_client.py` 的 `_make_request()`。下面省略函数注释，保留核心语句：

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

执行前，`payload` 是本地 Python 字典。`json.dumps()` 把它转换为 JSON 文本，`encode()` 再转成发送给服务的字节。`Content-Type` 指定 JSON 格式，`Authorization` 用于认证。这个函数构造请求；随后 `_urlopen_with_retry()` 才真正连接服务。

轮辋界面使用流式函数 `call_llm_streaming()`，其载荷包括：

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

`stream=True` 让响应分块到达。界面线程按调用索引累积工具名和参数，收齐后验证参数 JSON，最后交给 AgentLoop。半段参数不会直接执行。

### 2.4 验证接入：能聊天还不够

先在 Settings 中测试连接，再用已经声明的轮辋工具发起任务。应检查返回是否含 `tool_calls`，其中 `function.name` 属于项目工具，`function.arguments` 能解析为 JSON。

下例是协议示意，不是实际运行记录：

```json
{
  "role": "assistant",
  "content": "先检查网格。",
  "tool_calls": [{
    "id": "call_01",
    "type": "function",
    "function": {
      "name": "rim_inspect_stl",
      "arguments": "{\"stl_path\":\"E:/hub_stl/hubmain3processed.stl\"}"
    }
  }]
}
```

`call_01` 用于识别这一次调用。`arguments` 是包含 JSON 的字符串。普通回复“我将检查 STL”没有这些字段，不等于工具执行。轮辋模式要求原生 Tool Calling，不使用通用模式的 XML 回退。

如果收到 401，先核对认证；404 先核对地址；只有文字无工具调用时，再检查服务和模型是否支持该协议。不要把网络连接成功当作 Agent 已经运行成功。

## 第三章：让模型调用第一个工具

### 3.1 一项工具需要三层内容

| 层次 | 解决什么问题 | 代码位置 |
| --- | --- | --- |
| 声明 | 模型知道工具叫什么、参数怎么填 | `agent/tool_defs.py` |
| 注册和处理器 | 本地知道这个名字对应哪个函数 | `agent/tools.py`、`rim_reconstruction/tool_handlers.py` |
| 服务实现 | 真正检查 STL、调用 Worker、验证结果 | `rim_reconstruction/services/` |

Schema 是参数规范，可以理解为给模型看的表单说明。它不会自动执行 Python，也不能代替服务层验证。

### 3.2 给模型看的工具声明

下面用检查工具演示声明结构。说明文字做了教学简化，名称和必填参数与当前接口对应：

```json
{
  "type": "function",
  "function": {
    "name": "rim_inspect_stl",
    "description": "读取并检查 STL，返回统计信息与本次调用目录。",
    "parameters": {
      "type": "object",
      "properties": {
        "stl_path": {"type": "string"},
        "unit": {"type": "string", "enum": ["mm"]}
      },
      "required": ["stl_path"],
      "additionalProperties": false
    }
  }
}
```

`name` 必须与注册名相同；`properties` 列出字段；`required` 说明路径必填；`additionalProperties=false` 声明不接收额外参数。模型填了参数后，程序仍会检查路径、单位和数据身份。

### 3.3 本地怎样找到真实函数

`agent/tools.py` 注册检查工具：

```python
from rim_reconstruction.tool_handlers import rim_inspect_stl
register_tool("rim_inspect_stl", rim_inspect_stl)
```

注册表将字符串名称与函数关联。只写 Schema 不注册，模型能提出调用，执行时仍找不到工具；只注册不声明，模型通常不知道它存在。

工具真正执行的路径是：

```text
模型返回 rim_inspect_stl 和 arguments
    ↓ AgentLoop 检查调用形式
面板检查当前阶段是否允许
    ↓
RimToolWorker 中的 handler 映射
    ↓
rim_inspect_stl 处理器解析 JSON
    ↓
inspect_model() 自动生成任务 ID
    ↓
CppWorkerAdapter → geometry_worker.exe
    ↓
校验结果 → 返回统计和真实 call_directory
```

通用/主线程工具通过 `dispatch_tool()` 路由，耗时轮辋工具走后台 worker 映射，不是所有工具都经过完全相同的线程路径。暂时只需记住：模型提供调用描述，真实函数在本地运行。

### 3.4 执行完毕后返回什么

检查结果只展示核心字段。`call-A` 是本教程的代号，实际路径由程序返回：

```json
{
  "status": "success",
  "operation": "inspect_stl",
  "unit": "mm",
  "task_id": "rim_example",
  "call_directory": "E:/example/geometry_calls/call-A",
  "metrics": {"triangle_count": 10000},
  "warnings": []
}
```

数字和目录均为教学示例。真正运行时，应保留工具原样返回的任务 ID 和目录。下一步估轴需要这个目录，而不是由模型猜一个目录。

到这里还只是“模型请求一次工具，本地执行一次”。要成为 Agent，必须把结果送回模型并继续决策。

## 第四章：把一次工具调用变成 Agent 循环

### 4.1 为什么要分多轮

开始时模型不知道网格统计和候选轴，不能可靠地一次填写所有步骤参数。检查完成后，下一轮才有检查目录；估轴完成后，再下一轮才有轴线目录。

每轮只有一个结构化动作，后一步使用前一步的真实结果。这是完整轮辋模式的约束，不是模型一次随意列出六个工具然后程序盲目执行。

### 4.2 状态机只有三类对外动作

状态机是记录当前运行状态、根据输入决定下一步的程序。这里是 `AgentLoop`，不是另一个大模型。

```text
用户任务
   ↓ start()
CALL_LLM：面板启动模型请求
   ↓ 模型响应
handle_stream_done()
   ├─ 请求工具 → EXECUTE_TOOLS
   │                ↓ 真实执行结果
   │         handle_tool_results()
   │                ↓
   └──────── 下一轮 CALL_LLM
   │
   └─ 请求结束 → 完成条件检查 → FINISH（成功或失败）
```

`AgentController` 保存会话和结果；`AgentLoop` 决定动作；`AgentPanel` 启动线程、显示消息并执行动作。这三个对象不是三个自主规划的 Agent。

### 4.3 从同一个任务走完前三轮

| 轮次 | 请求时已有证据 | 模型本轮动作 | 程序新增结果 |
| --- | --- | --- | --- |
| 1 | 用户给出的 STL 路径 | 检查 STL，调用 ID 为 `call_01` | 检查目录 `call-A` |
| 2 | 检查统计及 `call-A` | 使用 `call-A` 估轴，ID 为 `call_02` | 轴线目录 `call-B`、未验证警告 |
| 3 | 前两轮历史及 `call-B` | 使用 `call-B` 与显式角度切片，ID 为 `call_03` | 截面目录 `call-C`、闭合统计 |

这些代号会在下一章继续使用，避免读者在不同例子之间重新猜测对应关系。

第二轮请求的消息结构可写成下面这样。为突出关系，示例工具结果和提示词缩短；生产请求包含更多字段：

```json
[
  {"role": "system", "content": "按规定顺序重建轮辋；使用真实工具返回的引用。"},
  {"role": "user", "content": "重建 E:/hub_stl/hubmain3processed.stl 的轮辋回转主体"},
  {"role": "assistant", "tool_calls": [{
    "id": "call_01", "type": "function",
    "function": {"name": "rim_inspect_stl", "arguments": "{\"stl_path\":\"E:/hub_stl/hubmain3processed.stl\"}"}
  }]},
  {"role": "tool", "tool_call_id": "call_01",
   "content": "{\"status\":\"success\",\"call_directory\":\"E:/example/geometry_calls/call-A\"}"},
  {"role": "system", "content": "当前下一步 rim_estimate_axis；检查产物是 call-A。"}
]
```

第二轮模型据此提出：

```json
{
  "id": "call_02",
  "type": "function",
  "function": {
    "name": "rim_estimate_axis",
    "arguments": "{\"inspection_call_directory\":\"E:/example/geometry_calls/call-A\"}"
  }
}
```

这里最重要的不是 JSON 层数，而是 `call_01` 匹配上一条工具结果，且结果中的 `call-A` 原样进入 `inspection_call_directory`。

### 4.4 两行代码怎样闭合循环

下面两处代码来自 `AgentLoop.handle_tool_results()`，在原函数中不相邻，此处只摘出闭环语句：

```python
self._controller.session.add_tool_result(ex.tool_id, result_text)
# 中间还会记录日志、错误和质量状态
return self.prepare_llm_call()
```

执行前，`ex.tool_id` 是这次模型调用 ID，`result_text` 是实际结果。第一行追加 tool 消息；第二处再次组装模型请求。下轮请求因此比上轮多了真实执行记录，而不是从头重复用户的任务。

### 4.5 循环什么时候结束

达到 `MAX_ITERATIONS`、用户停止、不可恢复的 API/协议错误都会失败结束。模型输出结束文本时，完整轮辋模式还会调用 `RimPipeline.completion()`，要求实际建成草图、回转特征与单实体。

对“下一步已经就绪但模型提前结束”的情况，程序最多提供一次推进提醒。它不是无限追加“继续”，也不保证所有失败都会被修复。

## 第五章：组织提示词，并让下一轮知道上一轮做过什么

### 5.1 先写清角色、行动、完成和失败规则

下面是一份教学提示词，用于理解组织方式，不是生产提示词的完整替代品：

```text
角色与目标：你是轮辋重建助手，目标是建立原生草图和回转主体。
行动规则：先检查，再估轴，再截面、分析、拟合、建模；每轮一个工具。
数据规则：参数中的目录和 ID 必须来自当前真实结果，不生成点列或轴坐标。
完成条件：只有实际建成有效单实体后，才报告完整重建成功。
失败规则：无候选、不闭合或拟合失败时，依据反馈调整或报告原因，不擅自放宽阈值。
结果边界：说明 PCA 轴线和整体重建精度尚未认证。
```

实际规则在 `agent/prompts.py` 的 `RIM_RECONSTRUCTION_SYSTEM_PROMPT`。`AgentLoop.start()` 根据 workflow 选择提示词，用已有文档上下文替换 `{context}`，再调用 `session.set_system_prompt()`。

| 信息 | 来源 | 每轮是否相同 |
| --- | --- | --- |
| 角色和工作规则 | 专用系统提示词 | 同类任务基本稳定 |
| 用户任务 | user 消息 | 用户提交或补充时增加 |
| 模型调用与工具结果 | assistant/tool 历史 | 随执行增加 |
| 当前阶段和有效目录 | `RimPipeline.request_context()` | 根据真实结果更新 |
| 工具格式 | 请求顶层的 `tools` | 完整重建模式保持工具集可见 |

通用 CAD 模式会按首次执行、质量失败和重复错误注入 `references.py` 中的提醒。轮辋模式不照搬通用 CQ 检查清单，而使用专用规则与实时阶段摘要。

### 5.2 第一种上下文：消息历史

`ChatSession.add_tool_result()` 实际保存的结构是：

```python
self.messages.append({
    "role": "tool",
    "tool_call_id": tool_call_id,
    "content": result,
})
```

还是前面的任务：第二轮估轴返回 `call-B` 后，历史新增以下两条。为了阅读清楚，结果是缩短后的教学示例：

```json
[
  {"role": "assistant", "tool_calls": [{
    "id": "call_02", "type": "function",
    "function": {"name": "rim_estimate_axis", "arguments": "{\"inspection_call_directory\":\"E:/example/geometry_calls/call-A\"}"}
  }]},
  {"role": "tool", "tool_call_id": "call_02",
   "content": "{\"status\":\"success\",\"call_directory\":\"E:/example/geometry_calls/call-B\",\"axis_validated\":false}"}
]
```

第三轮会把它们连同已有历史再次发送给模型。模型因此知道“估轴已执行、输出在 call-B、轴线未验证”。下一步 `rim_process_sections_auto` 的 `axis_call_directory` 就应填 `call-B`。

API 本身没有替本地程序保存这些状态。每轮发送什么，模型才能依据什么做决定。

### 5.3 第二种上下文：当前阶段与有效引用

仅有历史还不够：历史可能很长，也可能包含已失效的旧目录。`RimPipeline` 专门记录本轮检查、轴线、截面、分析、拟合和实体状态。

```text
执行前：inspection=call-A，axis=空
估轴成功：record(ex) 保存 axis=call-B
下次请求：next_step=rim_process_sections_auto，axis_call_directory=call-B
执行前再次校验：模型参数是否真的引用当前 call-B
```

每次请求都会追加 `request_context()` 的精简状态，包含 `executable_tools`、`next_step`、`artifacts` 和 `last_tool_failed`。阶段门禁决定动作能否执行；模型看到全部声明不等于可以跳步。

如果重新切片，分析、拟合、实体的当前状态都会失效，包括这次切片失败的情况。旧文件仍可留作追溯，但不能继续作为本轮成功依据。

### 5.4 第三种上下文：下一步真正读取的本地文件

完整点列不放进聊天消息，而保存在独立调用目录中：

```text
call-C/
├─ request.json             本次截面的来源、轴线、角度
├─ result.json              截面结果与产物索引
├─ geometry/run-*/section_*.json
├─ analyses/analysis_*.json  候选、片段与源索引
└─ fits/fit_*.json           完整拟合系数
```

模型只收到摘要和引用；Python 服务通过引用读取文件，并校验任务、来源、ID 与坐标。消息告诉模型做过什么，阶段对象告诉程序哪些结果仍有效，文件提供完整计算数据。三者共同支撑下一步。

### 5.5 对话过长或退出程序以后会怎样

`trim_messages()` 估算 token，优先压缩较旧的中间内容，并把旧工具调用与结果成组处理，避免孤立 tool 消息；必要时还会进一步裁剪。这不是无限记忆，关键细节仍应留在本地产物。

`SessionStore` 把会话 JSON 写入磁盘，包含消息、摘要、文档状态和参数等。但恢复聊天历史不等于自动恢复 `RimPipeline`。当前新建重建 Loop 会创建新的阶段对象，不会从旧历史自动重建全部阶段。需要断点复用时必须另做明确的状态恢复设计，不能把“聊天记录还在”当作已经实现。

## 第六章：错误怎样进入反馈，哪些错误会直接结束

### 6.1 先区分三条不同处理路径

| 代表错误 | 谁处理 | 后续路径 |
| --- | --- | --- |
| API 返回 429 或部分网络故障 | HTTP 客户端 | 有限重试；仍失败则结束本轮 |
| 参数使用旧目录 | 阶段校验/服务 | 形成 tool 错误结果，模型下一轮重新决策 |
| 曲线拟合通过但材料截面不闭合 | FreeCAD builder | 拒绝建模并反馈，不能报告完整成功 |

### 6.2 案例一：API 返回 429

`_MAX_RETRIES=2` 表示首次请求之后最多再试两次。当前客户端对 429、5xx 和一部分连接错误退避重试，参考数值型 `Retry-After`，每次等待最多 30 秒。401 等非重试分支直接抛出。

```text
发出请求 → 429 → 等待并重试 → 仍失败
    → 模型线程 error 信号
    → AgentLoop.handle_error()
    → FINISH(success=False)
```

这条路径不会把同一次失败无限交给模型修理。地址、认证等问题应该在配置层解决。流式工具参数 JSON 没收完整，以及轮辋一次请求多个工具等协议违规，也可能直接失败结束。

### 6.3 案例二：模型填写了已经失效的截面目录

假设当前截面目录是 `call-C2`，模型却要求分析旧 `call-C`。`validate_call()` 拒绝执行，面板生成类似结果：

```json
{
  "status": "error",
  "error": {
    "code": "RIM_STAGE_INVALID",
    "message": "必须使用本轮截面返回的 call_directory"
  }
}
```

这条错误与原调用 ID 一起写入历史。下一轮又携带当前阶段中的 `call-C2`，模型可以修正参数再调用。程序没有擅自把错误目录偷偷改成正确目录，也没有执行旧数据。

`handle_tool_results()` 会记录近期错误。重复错误时附加定向提醒，要求改变方法；但是否重试、怎样调整仍取决于有效动作与模型反馈，不保证一定修好。

### 6.4 案例三：拟合合格，但无法回转成实体

拟合只检查所选片段与源点的残差。若端点缺口大于连接容差、进入负半径或形成无效面，builder 仍会拒绝建模。

```text
局部曲线误差通过
    ↓
检查完整闭合材料边界 → 不通过
    ↓
拒绝创建/回滚本次特征 → 返回原因
    ↓
模型调整选择或报告失败；完整流程 success=False
```

不能因为“想要成功”就补不存在的边界或放宽 0.08 mm。通用 `execute_code` 使用快照支撑撤销，轮辋 builder 使用 FreeCAD 文档事务处理建模失败，两者不同。

### 6.5 停止、迟到结果和提前结束

用户停止时，Loop 禁止继续推进并通知后台取消。旧 worker 如果已经不属于当前 Loop，其迟到结果不会写入新任务。SciPy 单次求解与 FreeCAD 内核操作不能保证立即中断。

模型说“完成”只是一次文本响应。完整重建必须由阶段对象记录本轮成功实体；如果没有满足完成条件，就提醒一次或失败结束。错误反馈和验收条件共同防止假成功。

## 第七章：把通用 Agent 接入轮辋逆向重建

### 7.1 程序规定步骤，模型选择参数与调整动作

当前没有额外独立的 Planner Agent。主流程顺序由业务规则规定，模型在这个范围中规划角度、候选和片段，并依据失败结果决定是否重新采样或修补。

```text
检查 → 估轴 → 多角度截面 → 分析 → 选择并拟合 → 回转实体
                 │           ↑
                 └→ 可选修补 ┘
```

普通完整重建声明七个工具：六个主步骤加一个修补工具。不要把旧注释中的“六个工具”误认为当前实际数量。

### 7.2 每一步读什么、做什么、输出给谁

| 工具 | 本轮读取 | 主要计算或决策 | 传给下一步 |
| --- | --- | --- | --- |
| `rim_inspect_stl` | 用户路径 | C++ 读取网格并统计 | `inspection_call_directory` |
| `rim_estimate_axis` | 检查目录 | C++ PCA 候选轴，程序正交化径向 | `axis_call_directory` |
| `rim_process_sections_auto` | 轴线目录、模型角度及理由 | C++ 截面求交、清洗与条件闭合 | 截面 `call_directory` |
| `rim_repair_sections`（可选） | 当前截面目录 | 沿原轴和角度重新提取/修补，不覆盖旧产物 | 新截面目录 |
| `rim_analyze_candidates` | 当前截面目录 | 筛选外围闭合候选、跨角度验证 | `analysis_path`、候选与片段 ID |
| `rim_fit_profile` | 分析路径与所选 ID | Python B 样条拟合和误差检查 | 成功 `result_path` |
| `rim_build_revolution` | 成功拟合路径 | 原生草图、回转和拓扑检查 | 文档/对象名、单实体与体积 |

模型不能提供完整点列或凭空编造轴线；程序和服务校验每步引用。

### 7.3 一个需要调整的例子：某些角度没有候选

检查摘要不足以确定轮辐相位。模型先探索多个角度，并明确说明理由。截面反馈显示开放轮廓多或只有一条有效闭合轮廓时，该角度可能不能输出候选。

模型可根据这些证据调整角度。自动截面要求 3～360 个不重复的 `[0,360)` 角度及 `angle_selection_reason`，没有默认角度列表。重试要提交完整新集合，需要保留的旧角度也要列入。

正常截面处理中已经包含条件修补；独立修补工具不是每次必调。修补不能保证闭合，也不证明轮廓是完整轮辋材料区域。

### 7.4 候选、拟合和实体各自检查什么

候选选择：每个角度至少两条有效闭合轮廓，按弧长加权平均半径选择最大者；并列或条件不足则不输出。跨角度双向匹配用于验证与排序，不裁掉不匹配的边。只存在一个候选时，跨角度支持率为 null，表示未验证。

拟合：服务重新核对源点与连续索引，将点转换为 `[axial, radius]`。默认阈值 0.08 mm，检查弦长参数位置的最大残差，不是全三维表面误差。尖角需适当分段，不能宣称已自动保持全部工程特征。

建模：`build_rim_from_fit()` 按拟合系数导入曲线，检查连接、非负半径、闭合 Wire、有效 Face 和试回转单实体，再创建 Sketcher 与 PartDesign 特征。默认端点连接容差 `1e-7 mm` 与拟合阈值用途不同，不应混用。

### 7.5 新增的后续评估入口与本教程范围

本次源码核对发现已加入独立 `rim_evaluate_revolution`。用户明确请求评估时，Loop 使用专用评估规则与工具，读取当前文档；它不是普通重建自动追加的一步。

这次教程重点复现重建 Agent，不把新评估模块的全部指标纳入验收承诺。若使用评估结果，还要核对参考网格范围、采样方法与阈值。不要把“现在有评估工具”写成“所有重建已达到精度”。

## 第八章：在自己的机器上部署并完成复现

### 8.1 先确认复现材料齐全

需要完整整合源码、匹配 DLL 的 Worker、FreeCAD、支持原生 Tool Calling 的模型服务，以及合法可用的输入 STL。本地源码没有可用 Git 元数据，当前无法给出已核对的上游 URL/commit；因此不能仅凭本文去下载任意同名 CadAgent 就期待得到相同扩展。

本机核对环境是 Windows、FreeCAD 1.1.4，FreeCAD 说明记录 SciPy 1.16，实际还需导入检查。Worker 配置指向 `runtime/build-repair/geometry_worker.exe`。所有本机绝对路径都必须替换成自己的路径。

### 8.2 检查运行依赖

操作位置：PowerShell，源码根目录。以下使用本机路径示例：

```powershell
Set-Location E:/hubagent/CadAgent-main
& D:/FreeCAD1.1/bin/python.exe -c "import FreeCAD, Part, Sketcher, numpy, scipy; from scipy.interpolate import make_splprep; print(FreeCAD.Version()); print(numpy.__version__, scipy.__version__)"
Test-Path ./runtime/build-repair/geometry_worker.exe
& ./runtime/build-repair/geometry_worker.exe --help
```

预期：导入成功、路径检查 True、Worker 打印帮助并退出。若系统 Python 可以导入而 FreeCAD Python 不能，应修复 FreeCAD 使用的环境。缺 DLL 时恢复同次构建的依赖，不能从别处随便复制同名 OCCT DLL。

### 8.3 配置 Worker

操作文件：根 `config/rim_geometry_worker.json`，不是历史子目录副本。

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

替换路径后，确认程序存在、输出目录可写。大网格确实超时时可根据日志调整超时，但不要混同为放宽几何容差。根 `rim_reconstruction_config.json` 含早期方案字段，并不是当前自动角度的默认配置。

### 8.4 安装工作台，并确认加载的是这份源码

1. 在 FreeCAD Python 控制台运行 `print(App.getUserAppDataDir())`，记录实际用户目录。
2. 关闭 FreeCAD，备份已有插件和 `.env`。
3. 将整合插件部署到该目录的 `Mod/CadAgent`，保证 `InitGui.py` 直接在 CadAgent 文件夹内。
4. 在插件根配置 `.env` 与根 Worker JSON。Worker 可指向外部源码目录，但绝对路径必须有效。
5. 重启 FreeCAD，选择 CadAgent 工作台。

本机曾检测到安装位置 `C:/Users/Wang/AppData/Roaming/FreeCAD/v1-1/Mod/CadAgent`，不要只看通用安装路径猜测实际位置。

检查实际导入：

```python
import agent.loop
from rim_reconstruction.adapters.cpp_worker_adapter import CppWorkerAdapter
print(agent.loop.__file__)
adapter = CppWorkerAdapter.from_config()
print(adapter.executable)
print(adapter.call_root)
```

预期全部指向当前部署和配置。修改源码目录不会自动同步到 Mod 副本；重启也不会替你复制文件。

### 8.5 先跑不依赖模型决策的集成检查

操作位置：整合源码根目录。

```powershell
& D:/FreeCAD1.1/bin/python.exe -B -m unittest tests.test_rim_pipeline tests.test_rim_workflow tests.test_rim_tool_defs tests.test_rim_end_to_end tests.test_freecad_rim_builder
```

预期通过且不跳过真实内核测试。端到端测试会生成合成双回转环网格，运行真实 Worker、筛选、拟合和 FreeCAD，角度与候选决策使用测试替身，不调用模型 API。

2026 年 11 月 11 日本文重写时，上述所选 26 项全部通过、无跳过。旧版教程记录的一项工具列表测试失败现已修正，不再作为当前失败记录。该结果不等于全套仓库测试通过，也不等于真实扫描 STL 的 LLM 对话和工程精度已验证。

### 8.6 再运行完整聊天 Agent

确认自己的 STL 文件存在，在面板选择“轮辋逆向重建”，输入：

> 请重建 E:/hub_stl/hubmain3processed.stl 的轮辋回转主体，单位毫米。先检查和估轴，再依据反馈规划角度、分析候选、选择片段，以 0.08 mm 阈值拟合并创建原生草图和回转单实体。无合格候选、不闭合或拟合失败时请报告原因，不补线、不擅自放宽阈值。

必须替换路径。预期看到检查、估轴、截面、分析、拟合、实体工具的实际结果。若某阶段不通过，应按第六章检查对应路径，不是期待每份扫描网格都无条件建成。

### 8.7 验收、保存与复现记录

从建模返回的文档名定位对象，确认草图与回转特征存在。对象名可能有后缀，以下仅作检查示例：

```python
doc = App.ActiveDocument
rev = doc.getObject("RimRevolution")
print(rev.Shape.isValid())
print(len(rev.Shape.Solids))
print(rev.Shape.Volume)
```

预期 True、1、正数。多文档时使用返回的 `document_name`，不要默认 ActiveDocument 就是本轮结果。

保存 FCStd，按需要选择最终 Body/回转特征导出 STEP，再重新打开检查。普通完整重建工具集没有通用 `export_step`，实体创建成功不意味着文件已保存。

最后保留：源码版本/文件哈希、脱敏配置、依赖版本、模型名称、输入样本标识、完整调用目录、测试输出、FCStd 与 STEP。这样后来者可以追踪每一步的数据来源。

## 附录 A：文件职责速查

| 文件 | 职责 | 正文对应章节 |
| --- | --- | --- |
| `core/config.py`、`core/llm_client.py` | 配置与模型网络请求 | 第二章 |
| `agent/tool_defs.py`、`agent/tools.py` | 工具声明与注册 | 第三章 |
| `agent/tool_dispatch.py`、`tool_handlers.py` | 路由与参数封装 | 第三章 |
| `agent/controller.py`、`agent/loop.py` | 共享状态与循环决策 | 第四章 |
| `agent/prompts.py`、`agent/references.py` | 基础规则与按需提醒 | 第五章 |
| `core/session.py`、`session_store.py`、`token_budget.py` | 对话、落盘与预算 | 第五章 |
| `agent/rim_pipeline.py` | 阶段状态和有效产物引用 | 第五至七章 |
| `core/quality.py`、`core/snapshot.py` | 通用质量反馈与撤销 | 第六章 |
| `rim_reconstruction/services/`、`adapters/` | 业务计算与 C++ 桥接 | 第七章 |
| `rim_reconstruction/cad/freecad_rim_builder.py` | 草图与回转实体 | 第七章 |
| `InitGui.py`、`ui/panel.py`、`ui/rim_tool_worker.py` | 工作台入口与线程调度 | 第八章及附录 B |

## 附录 B：按需查阅的核心源码片段

这里保留逐文件代码解读，供读完主线后定位实现。部分函数含新增评估分支；普通重建与评估应分别阅读，不混成同一条必经流程。

@@SOURCE_APPENDIX@@

## 附录 C：自行编译 Worker 与常见故障

### C.1 需要重新编译时

仅适用于没有可运行 Worker 或需要修改 C++ 的读者。在已初始化 MSVC x64 编译环境的终端执行下面模板，Qt/OCCT 必须替换为自己的匹配安装：

```powershell
cmake -S ./stlload_hub -B ./runtime/build-local -G Ninja -DCMAKE_BUILD_TYPE=Release -DSTLLOAD_BUILD_GUI=OFF -DBUILD_TESTING=ON -DCMAKE_PREFIX_PATH="D:/Qt/6.11.2/msvc2022_64" -DOpenCASCADE_DIR="E:/opencascade/install/cmake"
cmake --build ./runtime/build-local --target geometry_worker test_contour_repair
ctest --test-dir ./runtime/build-local --output-on-failure
```

本机原缓存是 Ninja、Release、MSVC x64 与 Qt 6.11.2；不要把 CMakeCache 搬到另一台机器。配置完成后将 Worker JSON 指向实际产出路径，保留匹配 DLL。本次教程重写未重新执行 C++ 构建。

### C.2 常见问题定位

| 症状 | 优先检查 |
| --- | --- |
| 工作台不出现 | Mod 目录层级、InitGui.py、FreeCAD 报告视图 |
| 仍是旧界面 | 模块 `__file__` 与 Mod 副本同步 |
| 模型只有文字 | 原生 Tool Calling 支持与返回格式 |
| Worker 启动失败 | exe、匹配 DLL、架构和配置 |
| 截面成功但无候选 | 有效闭合轮廓数、筛选条件、源网格 |
| 拟合失败 | 源片段连续性、点数、尖角分段和残差 |
| 拟合通过但建模失败 | 闭合、负半径、自交与有效单实体条件 |
| 停止没有立刻响应 | 当前不可即时中断的数值/内核调用 |
| 恢复聊天后不能越级建模 | 新 Loop 的管线状态没有自动断点恢复 |

项目根 LICENSE 声明 CadAgent 为 MIT。分发整合源码、C++ 模块和二进制依赖前，仍需核对对应组件的许可与分发要求。
