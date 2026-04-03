# 成员 B：Agent 主循环详细设计文档

> 文档目的：本文件用于先行评审成员 B 的实现方案，不直接写业务代码；待 review 通过后，再据此落地 `agent/react_agent.py` 与 `agent/parser.py`。

---

## 1. 本文档解决什么问题

根据当前分工，成员 B 负责的是 **ReAct 核心推理引擎实现**，即把 A 同学已经产出的 Prompt 方案，和 C 同学后续提供的 Tool 接口，真正串成一个可运行的 Agent 主循环。

成员 B 的交付目标不是“再写一版 Prompt”，而是把下面这条链路跑通：

**Question → Prompt 组装 → LLM 生成 Thought/Action → Parser 解析 → Tool 调用 → Observation 回填 → 下一轮推理 → finish[答案]**

同时，B 还需要负责几件关键工程工作：

1. 实现 `react_agent()` 主循环  
2. 封装 LLM 调用接口（兼容 OpenAI / 本地模型）  
3. 实现模型输出解析器（提取 Thought / Action / Action Input）  
4. 实现超步数回退（CoT-SC fallback）  
5. 实现防重复循环检测  
6. 产出可供 D 评测、E 演示的结构化运行结果

---

## 2. 当前项目现状与 B 的真实起点

### 2.1 仓库现状

当前仓库中已经存在：

- `docs/任务背景.md`：解释了任务核心是 ReAct
- `prompt/system_prompt.txt`：A 同学写好的系统提示词
- `prompt/few_shot_examples.txt`：A 同学写好的 6 条 few-shot 示例

当前还不存在：

- `agent/react_agent.py`
- `agent/parser.py`
- 实际的工具注册/调用层
- 评测脚本
- 可运行的项目骨架

因此，B 的文档必须起到 **“中间层接口说明书 + 实现蓝图”** 的作用。

---

## 3. B 模块的职责边界

### 3.1 B 负责什么

B 负责：

- 读取 A 提供的 Prompt 文件
- 接收 C 提供的工具注册表 / mock 工具
- 驱动 LLM 按 ReAct 规范逐步输出
- 解析 Thought / Action
- 调用对应工具
- 把 Observation 继续拼回上下文
- 在结束时输出结构化结果

### 3.2 B 不负责什么

B 不负责：

- 设计 few-shot 内容本身（A 负责）
- 实现真实工具数据源（C 负责）
- 计算 EM/F1 等评测指标（D 负责）
- 项目总集成、README、最终 demo 编排（E 负责）

### 3.3 B 与上下游的核心依赖

#### 对 A 的依赖

1. Prompt 输出格式必须稳定  
2. `Action: tool_name[input]` 的书写规范必须固定  
3. few-shot 示例中对“搜索失败/改词重试”的风格应与 B 的异常处理逻辑兼容

#### 对 C 的依赖

1. 工具的**准确名称**必须明确  
2. 工具调用接口必须统一  
3. 至少先提供 mock 版本，便于调试主循环

#### 对 D 的依赖

B 需要为 D 提供可评测的输出结构，例如：

- final answer
- trace
- tool calls
- step count
- finish reason

#### 对 E 的依赖

E 最后要做端到端集成，因此 B 的结果结构应该尽量清晰、可打印、可演示。

---

## 4. 先明确一个关键风险：抽象任务描述与当前 Prompt 工具集尚未完全对齐

### 4.1 当前现象

`docs/任务背景.md` 中提到的工具更像是：

- `search`
- `lookup`
- `finish`

但 A 当前写好的 `system_prompt.txt` 和 `few_shot_examples.txt` 中实际使用的是：

- `search`
- `sql_interpreter`
- `calculator`
- `finish`

### 4.2 这对 B 的影响

这意味着 B **不能把工具名硬编码死在主循环里**，否则后续一旦 C 的工具命名与 A 不一致，就会直接失配。

更准确地说，这里不一定是“文档互相矛盾”，也可能只是：

- `任务背景.md`：站在方法论层，给出抽象动作空间
- `prompt/*.txt`：站在当前项目实现层，给出具体工具集

### 4.3 仅靠“B 不硬编码”还不够

如果：

- A 的 prompt 让模型输出 `sql_interpreter[...]`
- C 最终只提供 `lookup(...)`

那么即使 B 完全不硬编码工具名，运行时仍会持续收到 `unknown tool`。

所以这个问题必须在项目层面闭环，而不是只在 B 内部“动态判断”。

### 4.4 当前推荐决策

主循环应该遵循以下原则：

1. **当前项目以 A 的 Prompt 中实际出现的工具名作为 canonical tool set**
   - `search`
   - `sql_interpreter`
   - `calculator`
   - `finish`
2. **C 最终应优先适配这套 canonical 名称**
3. **B 允许提供可选 alias 映射层作为过渡**
   - 例如：`lookup -> sql_interpreter`
4. **B 只对 `finish` 做特殊处理**
5. 其余 action 是否有效，应由“工具注册表 + alias 映射”共同判断
6. Parser 负责解析字符串格式，不负责维护工具白名单

### 4.5 alias 映射层的定位

alias 映射层是一个**工程兼容措施**，不是长期替代方案。

它的作用是：

- 在 A/C 尚未完全对齐前，保证 B 可以联调
- 避免因为工具名小差异导致主循环完全跑不起来

但长期仍建议：

> A 的 Prompt、B 的主循环、C 的工具封装最终统一到一套共享的 canonical tool spec。

---

## 5. B 模块的总体设计目标

为了让后续实现可控，B 模块应满足以下目标：

### 5.1 可测试

- 能在没有真实 API 的情况下，用 mock LLM + mock tools 跑通
- 能对 parser 单独做单元测试

### 5.2 可替换

- LLM 接口不要绑死某一家 SDK
- 工具层不要绑死某一个具体实现

### 5.3 可观测

- 每一步 Thought / Action / Observation 都能记录下来
- 方便 D 做失败分析
- 方便 E 做 PPT 展示

### 5.4 可容错

- 解析失败时能重试
- 工具报错时能给模型反馈
- 超步数时能 fallback
- 重复循环时能终止或强制改策略

---

## 6. 推荐目录与文件职责

本阶段建议 B 最终产出如下两个核心文件：

```text
agent/
├─ parser.py
└─ react_agent.py
```

### 6.1 `agent/parser.py`

负责：

- 从模型输出中提取 `Thought`
- 从模型输出中提取 `Action`
- 解析 `tool_name[input]`
- 判断格式是否合法
- 对异常格式给出错误信息

### 6.2 `agent/react_agent.py`

负责：

- 读取 Prompt
- 构造当前轮上下文
- 调 LLM
- 调 parser
- 调 tools
- 回填 observation
- 维护 trace
- 做 loop detection
- 做 fallback
- 返回最终结构化结果

---

## 7. 对外接口约定（这是本次设计最重要的部分）

## 7.1 A → B：LLM 输出格式契约

A 给 B 的最关键接口，不是 Python 函数，而是 **Prompt 约束出的模型输出格式**。

### 7.1.1 Canonical 格式

模型每一轮必须输出：

```text
Thought k: ...
Action k: tool_name[input]
```

注意：

1. 每轮只允许一个 Thought 和一个 Action
2. 不允许模型自己生成 Observation
3. Action 必须严格写成 `tool_name[input]`
4. 最终答案必须写成 `finish[答案]`

### 7.1.2 Parser 要兼容的格式

为了兼容 A 目前的 Prompt 书写风格，B 的 parser 至少应支持：

```text
Thought 1: ...
Action 1: search[xxx]
```

同时建议兼容不带数字的形式：

```text
Thought: ...
Action: search[xxx]
```

### 7.1.3 推荐解析策略

虽然 A 给出的建议是按正则：

```text
Action: (.*?)\[(.*?)\]
```

但为了兼容带编号的版本，B 实现时更建议采用：

```python
r"^Action(?:\s+\d+)?:\s*([A-Za-z_][A-Za-z0-9_]*)\[([^\]]*)\]\s*$"
```

说明：

- `(?:\s+\d+)?`：兼容 `Action 1:` / `Action 2:`
- 第一组：工具名
- 第二组：工具输入

这里有一个**协议层假设**需要明确：

- 当前设计默认 `Action` 的输入部分 **不再嵌套 `]`**
- 即 `tool_name[input]` 只按**最外层一对中括号**解析

原因不是单纯“贪婪/非贪婪”的正则技巧问题，而是：

> 一旦允许 `action_input` 内部再自由出现 `]`，整个 `tool_name[input]` 语法本身就会变得不稳定。

如果后续确实存在复杂输入（例如 SQL、JSON）里可能包含 `]` 的场景，那么更推荐在代码实现中：

1. 先定位 `Action ...:` 前缀
2. 再用“找到第一个 `[` 与最后一个 `]`”的方式手工切分
3. 而不是完全依赖单个正则表达式

对于 Thought，可采用类似思路：

```python
r"Thought(?:\s+\d+)?:\s*(.*?)(?=\nAction(?:\s+\d+)?:)"
```

并启用 `re.S` 以支持多行 Thought。

### 7.1.4 建议的实际做法

实际实现中，不建议只靠一个超长正则一次性解析全文；更稳妥的策略是：

1. 先从模型输出中定位 `Thought ...:` 段
2. 再定位 `Action ...:` 行
3. 单独解析 Action 行里的 `tool_name[input]`

这样更容易报错、更方便调试。

### 7.1.5 A/B 还需额外确认的格式细节

除 `Thought/Action` 规范外，A 与 B 还应显式确认以下两点：

1. **Observation 前缀是否固定为 `Observation`**
   - 因为主循环里常会使用 stop token，例如 `"\nObservation"`
   - 这要求 A 的 Prompt 延续当前的字段命名风格

2. **模型是否被严格约束为“每轮只输出一组 Thought + Action”**
   - 如果 Prompt 里没有反复强调这一点，模型更可能一次性生成多步
   - 这会直接影响 B 的 parser 与重试逻辑

因此，`stop_tokens` 虽然在代码里是可配置的，但它背后依赖的仍然是 **A/B 的格式契约**。

---

## 7.2 C → B：工具层接口契约

### 7.2.1 推荐最小接口

B 最理想接收的是一个工具注册表：

```python
tools: dict[str, Callable[[str], Any]]
```

也就是说，B 只需要通过工具名和输入串来调用：

```python
observation = tools[action_name](action_input)
```

如果联调阶段存在工具别名，还建议额外提供：

```python
tool_aliases: dict[str, str]
```

例如：

```python
{
    "lookup": "sql_interpreter"
}
```

### 7.2.2 为什么推荐这个形式

因为这样：

- 简单
- 易 mock
- 与 A 的 `Action: tool_name[input]` 一一对应
- 不需要 B 知道工具内部逻辑

### 7.2.3 B 对 C 的最低要求

C 至少需要尽快确认以下内容：

1. 最终工具名到底是什么  
   - 当前项目建议以 prompt 中的 canonical 名称为准
   - 如果 C 暂时使用别名，需要明确 alias 映射表
   - 是否保留 `calculator`

2. 工具输入是否一律为字符串

3. 工具返回值可能是什么类型  
   - `str`
   - `list`
   - `tuple`
   - `dict`

4. 空结果的规范返回是什么  
   - `""`
   - `None`
   - `"Could not find results."`

### 7.2.3.1 当前推荐的 canonical tool set

除非小组后续统一改 Prompt，否则当前项目建议直接固定为：

- `search`
- `sql_interpreter`
- `calculator`
- `finish`

这是因为：

> 最终真正驱动模型产出 Action 名称的是 Prompt，而不是抽象背景文档。

### 7.2.4 B 侧的兼容策略

为了降低耦合，B 在主循环中应统一把工具返回值转为字符串 observation：

```python
def stringify_observation(raw) -> str:
    ...
```

建议规则：

- `None` → `"No result returned."`
- `""` → `"Could not find results."`
- `Exception` → `"ToolError: ..."`
- `str` → 原样返回
- `list / dict` → 优先考虑 `json.dumps(raw, ensure_ascii=False, default=str)`
- SQL 行结果等若需与 few-shot 保持一致，可保留紧凑 `repr` 风格
- 其他对象 → 兜底 `str(raw)`

这样可以让模型在 few-shot 风格下继续自我修正。

这里的重点不是“统一全部 `str(raw)`”或“统一全部 `json.dumps`”，而是：

> **Observation 的字符串化应尽量同时满足：可读、稳定、与 A 的 few-shot 风格一致。**

---

## 7.3 B → D：评测接口契约

D 需要的是“好评测”的输出，而不是一个仅能 print 的字符串。

因此 B 的主循环最终建议返回结构化结果，例如：

```python
{
    "question": "...",
    "final_answer": "...",
    "trace": [
        {
            "step": 1,
            "thought": "...",
            "action": "search",
            "action_input": "...",
            "observation": "...",
            "raw_llm_output": "...",
            "status": "ok"
        }
    ],
    "finish_reason": "finish",
    "used_fallback": False,
    "step_count": 2,
    "tool_call_count": 1
}
```

### 7.3.1 D 至少会用到的字段

- `final_answer`
- `trace`
- `step_count`
- `tool_call_count`
- `finish_reason`

### 7.3.3 建议现在就写死的统计口径

为了避免 D 后续评测脚本与 B 的运行逻辑口径不一致，建议明确如下定义：

1. `step_count`
   - 统计**成功解析出的 ReAct step**
   - **包含最终 `finish` 那一步**

2. `tool_call_count`
   - 只统计**真实发生的、非 `finish` 的工具调用**
   - 不包含 `finish`

3. `parse retry`
   - 仅是同一 step 内的纠错重试
   - **不计入** `step_count`

4. `loop_blocked`
   - 属于 synthetic observation
   - 因为未真正调用工具，**不计入** `tool_call_count`

5. `used_fallback`
   - 只表示是否进入 fallback 阶段
   - 不改变前面 ReAct 部分的 `step_count` 统计口径

6. `fallback trace`
   - 如需记录 fallback 过程，建议单独放入 `fallback_trace`
   - 不强行混入 ReAct 的主 `trace`

### 7.3.2 为什么要保留 trace

因为 D 后面不仅要算准确率，还要分析失败 case：

- 是推理错了
- 还是搜索错了
- 还是 parser 失效
- 还是工具空结果

没有 trace，就很难还原失败原因。

---

## 7.4 B → E：集成展示接口契约

E 最后需要做主程序串联和演示，因此 B 返回结果时建议额外保证：

1. `trace` 可直接格式化打印
2. `final_answer` 单独可取
3. `finish_reason` 可用于演示“正常结束/回退结束”
4. `raw_llm_output` 可保留，便于展示 parser 前后的对比

---

## 8. 建议的数据结构设计

虽然目前只是写文档，但为了后续代码可读性，建议 B 在实现时使用 dataclass。

## 8.1 Parser 输出结构

```python
ParsedStep:
    thought: str
    action: str
    action_input: str
    raw_text: str
    is_valid: bool
    error_message: str | None
```

用途：

- 统一 parser 返回结果
- 避免每次都靠多个元组字段记忆顺序

## 8.2 每步 trace 结构

```python
StepRecord:
    step: int
    thought: str
    action: str
    action_input: str
    observation: str
    raw_llm_output: str
    status: str
```

`status` 可选值例如：

- `ok`
- `parse_error`
- `tool_error`
- `loop_blocked`
- `fallback_triggered`

## 8.3 主循环配置结构

```python
ReactConfig:
    max_steps: int = 7
    max_parse_retries: int = 2
    max_consecutive_repeats: int = 1
    fallback_enabled: bool = True
    cot_sc_samples: int = 5   # debug 默认值
    stop_tokens: list[str] | None = None
    generation_kwargs: dict | None = None
    max_observation_chars: int = 1200
    fallback_tie_break: str = "first_seen"
```

注意：

- 根据 PDF/背景文档，`max_steps` 推荐默认值为 **7**
- 正式评测时 `cot_sc_samples` 可提升到 **21**
- 本地调试阶段可先设为 **5**
- `generation_kwargs` 用于兼容 `temperature / max_tokens / model` 等生成参数
- `max_observation_chars` 用于避免单条 observation 过长导致 context 膨胀
- `fallback_tie_break` 用于明确多数投票平票时的处理规则

---

## 9. `parser.py` 的详细设计

## 9.1 parser 的职责不是“猜”，而是“严格抽取”

parser 不应该试图替模型补全缺失字段，而应尽量做到：

- 成功时严格返回结构化结果
- 失败时明确说明失败原因

因为 parser 太“聪明”反而会掩盖 Prompt 问题。

## 9.2 推荐函数设计

建议 `parser.py` 至少包含以下函数：

### 9.2.1 `parse_llm_output(text: str) -> ParsedStep`

职责：

- 从完整模型输出中提取一个 Thought 和一个 Action

### 9.2.2 `parse_action_line(action_line: str) -> tuple[str, str]`

职责：

- 解析 `tool_name[input]`
- 返回 `(action_name, action_input)`

### 9.2.3 `normalize_multiline_text(text: str) -> str`

职责：

- 清理首尾空白
- 标准化换行

## 9.3 parser 的错误分类建议

建议将 parser 错误分成几类：

1. `missing_thought`
2. `missing_action`
3. `bad_action_format`
4. `empty_action_name`
5. `empty_action_input`（可按需要决定是否允许）

这样后续调试时非常清晰。

## 9.4 parser 的容错边界

建议：

- 允许 Thought 为多行
- 允许 Action 前有编号
- 不允许缺失 `[]`
- 不允许一个输出中出现多个 Action 却仍被静默接受

如果模型输出多组 Thought/Action，parser 可采用：

- **优先报出 `multiple_actions` / `multiple_steps_generated` 错误**
- 主循环收到该错误后，先触发一次“格式纠正重试”
- 若重试后仍然多组输出，再进入 fallback 或按解析失败处理

原因是：如果 parser 长期“默认取第一组”，模型可能会逐步学会一次生成多步，进而削弱 ReAct 单步交互的约束。

---

## 10. `react_agent.py` 的详细设计

## 10.1 推荐对外主入口

建议对外暴露一个函数：

```python
react_agent(
    question: str,
    tools: dict,
    llm_generate,
    system_prompt_path: str = "prompt/system_prompt.txt",
    few_shot_path: str = "prompt/few_shot_examples.txt",
    config: ReactConfig | None = None,
)
```

### 参数含义

- `question`：当前用户问题
- `tools`：C 提供的工具注册表
- `llm_generate`：模型调用函数或适配器
- `system_prompt_path`：A 的 system prompt 文件
- `few_shot_path`：A 的 few-shot 文件
- `config`：主循环配置

## 10.2 为什么建议把 LLM 设计成“可注入”

不要在主循环里写死：

- OpenAI SDK 调用
- 某本地模型接口
- 某个平台特定客户端

更好的方式是把 LLM 抽象成一个“可调用对象”，例如：

```python
llm_generate(
    prompt_text: str,
    stop: list[str] | None = None,
    generation_config: dict | None = None,
    **kwargs,
) -> str
```

这样好处很多：

1. 更容易做单元测试
2. 更容易接 mock LLM
3. 后续切换 OpenAI / 本地模型更方便

说明：

- 文档里给出的是**抽象接口形态**，不是强制唯一签名
- 实际实现时，完全可以把 `temperature / max_tokens / model` 放进 `generation_config` 或 `ReactConfig.generation_kwargs`
- 核心原则仍然是：**主循环依赖抽象能力，而不是依赖某家 SDK 的固定参数列表**

### 10.3 Fallback 需要独立的 Prompt 契约

这是实现时必须明确的一点：

- **ReAct 主循环使用 A 提供的 ReAct Prompt**
- **fallback 不应继续沿用同一套 ReAct system prompt**

原因是当前 `prompt/system_prompt.txt` 已经明确要求：

- 必须且只能使用工具
- 必须按 `Thought -> Action -> Observation` 交替
- 最终输出 `finish[...]`

如果 fallback 还沿用同一套提示词，模型很可能会：

- 继续输出 ReAct 格式
- 继续尝试调用工具
- 而不是直接产出最终答案文本

因此，建议在实现中明确拆分为两类生成接口：

```python
react_generate(...)     # 用于主循环，输出 Thought/Action
fallback_generate(...)  # 用于 fallback，直接输出答案文本
```

或者至少保证：

- fallback 阶段使用独立 instruction
- fallback 的目标输出是“最终答案文本”
- 而不是继续 `finish[...]` 或继续多步 ReAct

### 10.4 推荐的 fallback prompt 约束

文档建议 fallback 至少满足以下约束：

1. 输入：原始问题
2. 可选输入：ReAct 失败原因摘要（例如超步数、解析失败、循环）
3. 输出：最终答案文本
4. 不再要求 Thought/Action/Observation 格式
5. 不再要求调用工具

可用伪模板：

```text
请直接回答下面的问题，只输出最终答案，不要输出 Thought、Action、Observation、工具调用或额外解释。

Question: {question}
```

如果需要做 CoT-SC，可在内部采样多次，但对外部返回时仍只保留最终答案。

---

## 11. 主循环执行流程（核心）

下面是 B 模块真正要实现的核心流程。

## 11.1 初始化阶段

1. 读取 `system_prompt.txt`
2. 读取 `few_shot_examples.txt`
3. 初始化：
   - `scratchpad = ""`
   - `trace = []`
   - `step = 1`
   - `tool_call_count = 0`

## 11.2 每一轮 prompt 拼装

建议当前轮的输入上下文由三部分组成：

1. **system prompt**
2. **few-shot examples**
3. **当前问题 + 历史 scratchpad**

推荐拼装思路：

```text
[few-shot examples]

---
[真实问题]
Question: {question}
{已有 Thought/Action/Observation 轨迹}
Thought {next_step}:
```

这样模型会自然续写：

```text
Thought {next_step}: ...
Action {next_step}: tool_name[input]
```

这里的“字符串拼接”只是说明主流程的一种表达方式。  
如果后续接的是 chat API，也可以改为：

- `system_prompt` → system role
- few-shot / scratchpad → assistant / user 历史消息
- 当前问题 → user role

也就是说，**文档约束的是逻辑结构，不强制限定只能使用 completion-style 纯字符串接口**。

### 11.2.1 Context 长度控制假设

虽然 `max_steps=7` 已经限制了轨迹长度，但仍然要考虑单次 observation 过长的问题，例如：

- search 返回长文本
- sql 返回很多行
- 工具异常信息过长

因此，建议在实现中增加 observation 截断或压缩策略，例如：

1. 先将工具返回值字符串化
2. 若长度超过 `config.max_observation_chars`
3. 则截断为：

```text
{前若干字符}
...[TRUNCATED]...
{后若干字符}
```

这样能在不破坏主循环逻辑的前提下，降低 context 爆炸风险。

## 11.3 单轮执行流程

每一轮按以下顺序：

1. 调用 LLM 生成当前轮输出
2. 用 parser 解析出：
   - thought
   - action
   - action_input
3. 判断 action 是否为 `finish`
4. 若不是 `finish`，则调用对应 tool
5. 拿到 observation 后写入 trace
6. 将当前轮结果拼回 scratchpad
7. 进入下一轮

## 11.4 主循环伪代码

```python
def react_agent(...):
    load prompts
    trace = []
    scratchpad = ""

    for step in range(1, max_steps + 1):
        prompt_text = build_prompt(question, few_shot, scratchpad, step)
        raw_output = llm_generate(
            prompt_text,
            stop=config.stop_tokens or ["\nObservation"],
            generation_config=config.generation_kwargs,
        )

        parsed = parse_llm_output(raw_output)
        if not parsed.is_valid:
            # 解析失败重试；超限则 fallback
            ...

        if parsed.action == "finish":
            return AgentResult(...)

        if is_repeated_action(parsed.action, parsed.action_input, trace):
            observation = "LoopWarning: repeated action detected. Please change strategy."
            status = "loop_blocked"
        else:
            observation = truncate_observation(
                call_tool(parsed.action, parsed.action_input, tools),
                config.max_observation_chars,
            )
            tool_call_count += 1
            status = "ok"

        trace.append(...)
        scratchpad += format_step(...)

    return fallback(...)
```

---

## 12. 重复循环检测设计（B 的重点难点之一）

根据背景文档与 A 的反馈，B 必须显式实现防死循环逻辑。

## 12.1 为什么必须单独做

虽然 system prompt 里已经写了：

- 不允许死循环
- 连续两次相同 Action 必须改变策略

但这只是“提示模型”，不能保证模型一定遵守。  
所以 B 必须在代码层再做一次硬约束。

## 12.2 推荐检测粒度

建议按 `(action, action_input)` 二元组做检测，而不只是 action 名称。

例如：

- `search[Attention Is All You Need]`
- `search[Attention Is All You Need]`

这属于明显重复。

但：

- `search[Attention Is All You Need]`
- `search[Transformer paper author]`

这不应算死循环，因为虽然工具相同，但策略已经变化。

## 12.3 推荐策略

### 一级策略：阻断相同调用

若当前轮与上一轮完全相同：

- 不真正执行工具
- 直接写入一个 synthetic observation，例如：

```text
LoopWarning: repeated action detected. Please change strategy.
```

目的是强制模型下一步改变查询词或换工具。

### 二级策略：多次仍不改，则 fallback

如果模型收到 warning 后，下一轮仍继续重复完全相同的 action/input，则说明已陷入循环。此时：

- 直接结束 ReAct 主循环
- 进入 fallback

## 12.4 为什么不建议“无限提醒”

如果只是一直写 warning，不触发终止，那么循环仍然只是从“工具死循环”变成“提示死循环”。

---

## 13. 工具调用异常处理设计

## 13.1 未知工具

若 parser 解析出的 action 不在 `tools` 中，且不是 `finish`：

建议不要直接崩溃，而是写入 observation：

```text
ToolError: unknown tool 'xxx'. Available tools: [...]
```

这样模型有机会下一轮修正。

## 13.2 工具运行时报错

若工具抛异常：

```text
ToolError: {异常信息}
```

并记录到 trace 的 `status = "tool_error"`。

## 13.3 空结果

若工具返回空：

推荐统一回填为：

```text
Could not find results.
```

原因：

- 与 A 的 few-shot 示例风格一致
- 模型更容易学会“改关键词再试”

---

## 14. 解析失败处理设计

Parser 失败是主循环必须考虑的高频异常之一。

## 14.1 推荐处理策略

### 第一次解析失败

重新向模型发起一次“格式纠正”请求，提示：

```text
格式错误。请严格只输出一组：
Thought k: ...
Action k: tool_name[input]
```

以下情况都应视为“格式错误”的一部分：

- 缺失 Thought
- 缺失 Action
- Action 不符合 `tool_name[input]`
- 一次性输出了多组 Thought/Action

### 第二次仍失败

若超过 `max_parse_retries`：

- 触发 fallback
- 或返回 `finish_reason = "parse_failure_fallback"`

## 14.2 为什么不建议无限重试

因为这会造成：

- 浪费 token
- 调试困难
- 看起来像死循环

---

## 15. 超步数回退（CoT-SC fallback）设计

根据背景文档和 A 的反馈，B 需要实现超步数后的回退逻辑。

## 15.1 何时触发 fallback

建议在以下情形触发：

1. `step > max_steps`
2. parser 连续失败
3. 循环检测确认已卡死
4. 出现连续严重工具错误且无法恢复（可选）

## 15.2 fallback 的目标

不是继续工具调用，而是用“独立的答案生成策略”尽快给出最终答案。

这里再次强调：

> fallback 是**切换到独立答题模式**，而不是继续沿用 ReAct Prompt 再跑一轮。

## 15.3 推荐 fallback 方案

按照论文背景，推荐设计成 **CoT-SC（Chain-of-Thought + Self-Consistency）**：

1. 对同一个问题采样多次
2. 每次要求模型输出最终答案
3. 对答案做轻量规范化后再统计频次
4. 取频次最高的答案作为 fallback 结果

### 15.3.1 答案规范化建议

建议至少做以下轻量规范化：

1. 去掉首尾空白
2. 合并多余空格
3. 对大小写不敏感任务可转小写
4. 如任务中存在固定答案格式，可做最小限度格式归一

例如：

- `"21:43"` 与 `" 21:43 "` 应视为同一答案
- 是否把 `"21:43"` 与 `"21:43:00"` 视为同一答案，则应谨慎决定  
  这一类规则不建议在 B 中做过度语义化处理，更适合由 D 在评测层统一规范

### 15.3.2 平票时的处理

如果多次采样后出现平票，建议采用以下规则：

1. 先按规范化答案统计频次
2. 选频次最高者
3. 若频次并列，则取**最早出现**的那个答案
4. 同时在结果中可增加低置信度标记，例如：
   - `fallback_low_confidence = True`

这样既保留可执行性，又不会让 tie-breaking 规则变得过于复杂。

## 15.4 工程上可分两阶段落地

### 阶段一：先做可运行版

本地调试时可先做一个较轻量版本：

- `cot_sc_samples = 5`
- 输出只取多数票

### 阶段二：正式评测版

如果 API 成本允许，再对齐背景文档：

- `cot_sc_samples = 21`

## 15.5 fallback 的返回标记

建议结果中明确带上：

- `used_fallback = True`
- `finish_reason = "max_steps_fallback"` 或 `"loop_fallback"`

这样 D/E 后续都能直接利用。

---

## 16. 建议的最终返回结果格式

建议 `react_agent()` 最终返回一个结构化对象或 dict，至少包含：

```python
{
    "question": str,
    "final_answer": str,
    "trace": list,
    "step_count": int,
    "tool_call_count": int,
    "used_fallback": bool,
    "finish_reason": str
}
```

进一步建议补充：

```python
{
    "available_tools": list[str],
    "parser_error_count": int,
    "raw_final_output": str | None
}
```

---

## 17. B 模块的实现优先级建议

由于当前项目还没进入真正编码阶段，建议 B 按如下顺序开发：

### 第一优先级：先把骨架写通

目标：

- 能读取 Prompt 文件
- 能调 mock LLM
- 能调 mock tools
- 能跑完 1~2 个示例

对应实现：

- `parser.py`
- `react_agent.py` 的基础 loop

### 第二优先级：补齐鲁棒性

目标：

- 支持 parse retry
- 支持工具空结果/异常
- 支持 loop detection

### 第三优先级：补 fallback

目标：

- 超步数进入 CoT-SC
- 输出带 `used_fallback`

### 第四优先级：补输出可观测性

目标：

- trace 字段完善
- 方便 D 和 E 使用

---

## 18. 建议的联调顺序（结合图片中的协作建议）

结合图片底部给出的关键协作点，B 的实际工作顺序建议为：

### 第一步：先向 C 确认工具接口

需要立即确认：

1. 工具名清单
2. 调用签名
3. 返回值格式
4. mock 工具是否可先提供

### 第二步：与 A 对齐输出格式

重点确认：

1. 是否始终使用 `Thought k:` / `Action k:`  
2. 是否强制只输出一组 Thought/Action  
3. `finish[答案]` 是否作为唯一结束格式
4. Observation 字段前缀是否稳定为 `Observation`
5. stop tokens 是否按 `"\nObservation"` 或兼容形式配置

### 第三步：B 独立实现 parser 与主循环

依赖：

- A 的 Prompt 已稳定
- C 的 mock tools 已可调用

### 第四步：向 D 交付结构化结果格式

让 D 提前知道：

- `final_answer` 从哪里读
- `trace` 长什么样
- `finish_reason` 怎么判断

### 第五步：为 E 准备 demo case

建议提前保留一个“最经典、最稳定”的 demo 问题，方便后续汇报展示完整轨迹。

---

## 19. 推荐 demo case（给 E 预留）

根据 A 的反馈，建议提前准备一个最稳的演示题目。

### 推荐选择

优先使用 A 的 few-shot 里已经出现过的、路径最清晰的题型，例如：

**航班查询题**

```text
2022年6月5日从MIA飞往LAS的AA2319航班的起飞时间是什么？
```

原因：

1. 轨迹清晰
2. 工具路径简单
3. 结果明确
4. PPT 中最容易展示

对应展示链路可以非常标准：

```text
Question
→ Thought 1
→ Action 1: sql_interpreter[...]
→ Observation 1: [("21:43",)]
→ Thought 2
→ Action 2: finish[21:43]
```

---

## 20. 本文档对应的后续代码实现建议

待你 review 通过后，代码实现可以按下面顺序推进：

1. 新建 `agent/` 目录
2. 先写 `parser.py`
3. 再写 `react_agent.py` 骨架
4. 用 mock LLM / mock tools 跑通单题
5. 加循环检测
6. 加 fallback
7. 补 trace 输出

---

## 21. B 当前最需要你 review 的决策点

在正式写代码前，最值得先确认的是以下几点：

### 21.1 是否采用“工具注册表动态判断”方案

即：

- 只硬编码 `finish`
- 其他工具名全部由 C 的 registry 决定

我认为这是最稳妥的。

### 21.2 parser 是否按“兼容有编号/无编号”来设计

我建议：

- **兼容** `Action 1:` 与 `Action:`
- 不把 parser 设计得过窄

### 21.3 fallback 是否先做“轻量调试版”，再做正式版

我建议：

- 本地先 `5` 次采样
- 需要提交/评测时再提高到 `21`

### 21.4 主循环返回值是否采用结构化 dict / dataclass

我强烈建议：

- 不要只 return 一个字符串答案
- 一定要把 `trace` 和 `finish_reason` 一起返回

---

## 22. 最终结论

成员 B 的本质工作，不是“写个 while 循环”这么简单，而是：

> 把 A 的 Prompt 规范、C 的工具接口、D 的评测需求、E 的演示需求，统一收敛到一个**可运行、可调试、可分析、可集成**的 ReAct Agent 核心引擎里。

因此，B 的实现应优先遵循以下原则：

1. **接口先行**：先把 A/B/C/D/E 的契约讲清楚  
2. **主循环最小可跑通**：先打通一题  
3. **鲁棒性逐层补**：再加 parser retry、tool error、loop detection  
4. **结果结构化**：方便 D 评测，方便 E 展示  
5. **fallback 可配置**：本地调试轻量，正式评测对齐论文

---

## 23. 下一步

如果你认可这份设计文档，下一步我建议直接进入：

1. 创建 `agent/parser.py`
2. 创建 `agent/react_agent.py`
3. 先用 mock 版本把主循环跑通

如果你想先改文档，我也可以继续按你的意见修改这一版设计稿。
