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

### B.1 InitGui.py：让 FreeCAD 找到工作台和面板

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

### B.2 ui/panel.py：把状态机动作变成真实操作

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

### B.3 agent/controller.py：保存会话和任务结果

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

### B.4 agent/loop.py 的 start()：任务怎样进入模型会话

源码：`agent/loop.py`，片段起始行 172。

```python
def start(self, user_text: str) -> LoopAction:
    self._evaluation_requested = requests_evaluation(user_text)
    if self._evaluation_requested:
        self._mode = "tool_calling"
        # A follow-up evaluation reads the existing document; no reconstruction gate.
        self._rim_pipeline = None
    ctx = self._build_context()
    # 轮辋逆向扩展：按任务选择提示词，避免截面阶段被引导生成 CAD 代码。
    prompt = RIM_SECTIONS_SYSTEM_PROMPT if self._workflow in ("rim_sections", "rim_reconstruction") else AGENT_SYSTEM_PROMPT
    # 轮辋逆向扩展：完整重建模式必须一直推进到原生回转实体。
    if self._workflow == "rim_reconstruction":
        prompt = RIM_RECONSTRUCTION_SYSTEM_PROMPT
    if self._evaluation_requested:
        prompt = EVALUATION_PROMPT + "\n当前文档上下文：\n{context}"
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

### B.5 prepare_llm_call()：选择模型本轮可见的工具

源码：`agent/loop.py`，片段起始行 277。

```python
rim_names = {t["function"]["name"] for t in RIM_TOOL_DEFINITIONS}
workflow_tools = (RIM_TOOL_DEFINITIONS if self._workflow in ("rim_sections", "rim_reconstruction") else
                  [t for t in TOOL_DEFINITIONS if t["function"]["name"] not in rim_names])
# 轮辋逆向扩展修复：完整工具集始终声明，前置条件由执行门禁校验。
if self._rim_pipeline is not None:
    workflow_tools = [t for t in workflow_tools
                      if t["function"]["name"] in RIM_RECONSTRUCTION_TOOL_NAMES]
tools = workflow_tools if self._mode != "react" else None
# Not advertised on ordinary reconstruction turns; no automatic seventh step.
if self._evaluation_requested:
    tools = [RIM_EVALUATION_TOOL_DEFINITION]
elif tools is not None:
    tools = [t for t in tools if t["function"]["name"] != "rim_evaluate_revolution"]

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

### B.6 handle_stream_done()：解析模型响应，而不是执行模型文字

源码：`agent/loop.py`，片段起始行 358。

```python
    calls = assistant_msg.get("tool_calls") or []
    if calls:
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "rim_evaluate_revolution":
            return self.handle_error("评估任务仅允许调用一个 rim_evaluate_revolution")
        return LoopAction(kind=LoopActionKind.EXECUTE_TOOLS, tool_calls=calls)
    if not self._evaluation_attempted and self._rim_continuation_prompts < 1:
        self._rim_continuation_prompts += 1
        self._controller.session.add_user_message(
            "本轮明确要求实际评估，尚未调用 rim_evaluate_revolution；请先调用工具读取当前模型，不要估计指标。")
        return self.prepare_llm_call()
    return LoopAction(kind=LoopActionKind.FINISH, success=self._evaluation_completed,
                      summary=content, from_stream=has_streaming_text, iterations=self._iteration)

# 轮辋逆向扩展：统一处理所有截面结束分支，禁止首轮纯文本绕过阶段校验。
if self._workflow in ("rim_sections", "rim_reconstruction"):
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

### B.7 工具声明：告诉模型参数应该怎样填写

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

### B.8 工具注册：把声明名称接到 Python 函数

源码：`agent/tools.py`，片段起始行 889。

```python
from rim_reconstruction.tool_handlers import rim_build_revolution
register_tool("rim_build_revolution", rim_build_revolution)
```

**核心是最后一行：**`register_tool("rim_build_revolution", rim_build_revolution)` 把字符串名称和处理函数关联起来。

字符串给模型和路由器使用，函数对象给 Python 调用。只有声明而没有注册，模型能提出调用，程序仍会报 Unknown tool。只注册而不声明，模型通常也不知道可以使用它。

`rim_build_revolution` 的 handler 再调用 CAD 服务和 builder。它会修改 FreeCAD 文档，必须走主线程；其他耗时截面/拟合工具可交给 `RimToolWorker`。

### B.9 agent/tool_dispatch.py：按工具名称分发

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

### B.10 core/llm_client.py：把会话和工具规范发送给模型

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

### B.11 call_llm_streaming()：从流式响应中拿到数据块

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

### B.12 prompts.py 与 references.py：固定规则和按需提醒

源码：`agent/loop.py`，片段起始行 227。

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

### B.13 core/session.py：让下一轮模型看见上一步真实结果

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

### B.14 core/token_budget.py：控制越聊越长的上下文

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

### B.15 core/quality.py：把几何异常转为可操作的检查结果

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

### B.16 core/snapshot.py：执行出错后怎样恢复文档

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

### B.17 core/session_store.py：退出 FreeCAD 后怎样保留聊天历史

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

### B.18 把这些文件串起来：一次真实调用的阅读顺序

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
