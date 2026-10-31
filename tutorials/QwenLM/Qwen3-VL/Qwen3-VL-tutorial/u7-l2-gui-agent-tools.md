# GUI Agent 与工具调用：mobile_use 工具实现

## 1. 本讲目标

上一讲（u7-l1）我们梳理了 cookbooks 的能力地图，把 13 本笔记本归并为四族，并预告了本讲的任务：精读 Agent 族背后的工具定义源码。本讲结束后，你应该能够：

1. 说清 `qwen_agent.tools.base.BaseTool` 的工具契约——一个工具由哪几部分组成，`description`、`parameters`、`call()` 各自承担什么角色。
2. 逐个解释 `mobile_use` 的 9 个 action 与 `computer_use` 的 14 个 action 的语义差异（触摸语义 vs 鼠标键盘语义），并跟踪 `call()` 的分发逻辑。
3. 理解参数 schema 的「扁平结构 + 条件必填 + 枚举约束」设计模式，以及为什么这套 schema 是写给模型看的而不是写给校验器看的。
4. 掌握 `@register_tool` 注册机制与完整消费链路：从 `tool.function` 渲染进 system prompt，到解析模型输出的 `<tool_call>` JSON，再到 0-1000 相对坐标回投到真实屏幕。
5. 独立继承 `BaseTool` 实现一个自定义工具（综合实践中的 `pc_use`）。

## 2. 前置知识

### 2.1 什么是 Function Call（工具调用）

大模型本身只能输出文本，不能直接「点击屏幕」。工具调用（tool call / function call）的解法是：把每个可执行操作描述成一份 JSON 说明（工具名、功能描述、参数格式），放进 system prompt；模型阅读后，不再输出自然语言，而是输出一段结构化 JSON 声明「我要调用哪个工具、传什么参数」；外部执行器解析这段 JSON、真正操作设备，再把执行结果（通常是一张新截图）喂回模型，进入下一轮。这个「感知→决策→执行→反馈」的循环就是 Agent 循环。

Qwen3-VL 官方采用的工具调用输出协议是用 XML 标签包裹 JSON：

```
<tool_call>
{"name": "mobile_use", "arguments": {"action": "click", "coordinate": [500, 500]}}
</tool_call>
```

### 2.2 JSON Schema 最小概念

JSON Schema 是描述「一份合法 JSON 长什么样」的规范，本讲会用到它的几个关键字：

| 关键字 | 含义 | 本讲示例 |
| --- | --- | --- |
| `type` | 数据类型 | `"type": "string"`、`"type": "array"` |
| `properties` | 对象各字段的子 schema | `action`、`coordinate`、`text` 等 |
| `enum` | 只允许枚举出的取值 | `["click", "swipe", ...]` |
| `required` | 哪些字段必填 | `["action"]` |

### 2.3 坐标系承接（来自 u7-l1 / u6-l4）

u7-l1 已确立：Qwen-VL 系列的 grounding 输出统一使用 **0-1000 相对坐标**（图像左上角为原点，右下角为 1000×1000），u6-l4 的 ODinW-13 判分侧 `is_rel=1` 分支（除以 1000）也是同一口径。本讲会看到 GUI Agent 如何**主动利用**这一约定：把虚拟屏幕分辨率声明为 999×999 或 1000×1000，模型输出的「像素坐标」就天然落在相对坐标系里。

### 2.4 依赖说明

`agent_function_call.py` 依赖外部库 `qwen_agent`（`pip install qwen-agent`，见两本 notebook 的安装单元格）。本仓库只写了工具的「协议层」；`BaseTool`、`register_tool`、`NousFnCallPrompt` 等基础设施来自 qwen-agent 库（[github.com/QwenLM/qwen-agent](https://github.com/QwenLM/qwen-agent)）。本讲对 BaseTool 契约的描述全部从本仓库的实际用法反推，细节以库源码为准。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [cookbooks/utils/agent_function_call.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py) | **本讲主角**。定义 `MobileUse`（注册名 `mobile_use`）与 `ComputerUse`（注册名 `computer_use`）两个工具类：description 文案、参数 schema、`call()` 分发骨架 |
| [cookbooks/mobile_agent.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mobile_agent.ipynb) | `mobile_use` 的消费示例：手写 system prompt（内嵌 schema 快照）+ 调 OpenAI 兼容 API + 解析 `<tool_call>` + 可视化点击坐标 |
| [cookbooks/computer_use.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb) | `computer_use` 的消费示例：用 `NousFnCallPrompt` 从 `tool.function` 自动渲染 system prompt，分 API 与本地模型两条路径，含 0-1000 坐标回投 |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | Cookbooks 总表，L120-L121 列出 Mobile Agent 与 Computer-Use Agent 两行 |

提示：两本 `.ipynb` 是 JSON 文件，下文给出的 `#L` 行号锚点指向其**原始 JSON 行**（GitHub 上需在 Code 视图查看）；mobile_agent.ipynb 是压缩成单行的 JSON，无法给行号，故按单元格名称指认。

## 4. 核心概念与源码讲解

### 4.1 qwen_agent 工具基类：BaseTool 契约

#### 4.1.1 概念说明

qwen-agent 库把「一个工具」抽象为 `BaseTool` 类。从本仓库 `agent_function_call.py` 的用法可以反推出契约的四要素：

| 契约成员 | 形式 | 作用 |
| --- | --- | --- |
| `description` | `@property`，返回字符串 | 告诉模型**这个工具是什么、什么时候用、有什么注意事项**。会原文进入 system prompt |
| `parameters` | 类属性，JSON Schema 字典 | 告诉模型**调用时要传哪些参数、什么类型、哪些必填**。同样会进入 system prompt |
| `call(params, **kwargs)` | 方法 | 执行入口。`params` 可以是 JSON 字符串或 dict，由基类助手 `_verify_json_format_args` 统一成 dict |
| `@register_tool("名字")` | 类装饰器 | 把类注册进 qwen-agent 的工具注册表，注册名与类名解耦 |

还有一个由基类提供的只读属性 `.function`（在 computer_use.ipynb 中以 `computer_use.function` 的形式使用），它把「名字 + description + parameters」打包成一份函数签名字典，供 prompt 渲染器消费。

关键认知：`description` 和 `parameters` 不是给人看的注释，而是**直接喂给模型的提示词的一部分**——文案质量决定模型调用的准确率，这正是本讲要精读它们的原因。

#### 4.1.2 核心流程

一个 GUI Agent 步骤的完整循环（伪代码）：

```text
tools = [MobileUse(cfg=...), ...]                     # ① 实例化工具（写入虚拟分辨率）
system_prompt = render(tools=[t.function for t in tools])  # ② schema 渲染进 <tools> 块
loop:
    output = LLM(system_prompt, query, 截图)            # ③ 模型输出 Thought + <tool_call> JSON
    action = parse(output, "<tool_call>", "</tool_call>")   # ④ 抽取 JSON
    result = tools[action.name].call(action.arguments)  # ⑤ 分发执行（驱动真机/桌面）
    screenshot = environment.step(result)               # ⑥ 执行后截新图
    history.append(action, screenshot)                  # ⑦ 回填，进入下一轮
    直到 action == terminate
```

本讲关注的 ①②③④⑤ 中，仓库源码负责 ①（工具定义）与 ⑤（`call` 分发），②③④ 在 notebook 里。

#### 4.1.3 源码精读

**导入与注册**——从 qwen-agent 库引入基类与注册装饰器：

- [cookbooks/utils/agent_function_call.py:L1-L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L1-L7)：第 3 行 `from qwen_agent.tools.base import BaseTool, register_tool` 引入契约；第 6-7 行 `@register_tool("mobile_use")` 把 `MobileUse` 类以 `mobile_use` 之名注册——注意模型输出里的工具名是注册名 `mobile_use`，不是类名 `MobileUse`。

**description 是 f-string，动态注入分辨率**：

- [cookbooks/utils/agent_function_call.py:L8-L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L8-L16)：`description` 用 f-string 写成，第 14 行 `The screen's resolution is {self.display_width_px}x{self.display_height_px}` 把实例的分辨率写进模型的世界观。文案其余三条全是**行为约束**：说明这是触摸屏接口、提示「应用启动慢，要 wait 并连续截图」、要求「点击元素中心而非边缘」。每一条都在纠正模型常见的失败模式。

**构造函数：先读 cfg 再交给基类**：

- [cookbooks/utils/agent_function_call.py:L84-L87](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L84-L87)：`__init__` 从 `cfg` 字典取出 `display_width_px` / `display_height_px` 存为实例属性（供 description 的 f-string 使用），再调用 `super().__init__(cfg)` 完成基类初始化。这决定了实例化必须传 cfg：`MobileUse(cfg={"display_width_px": 999, "display_height_px": 999})`，裸调 `MobileUse()` 会因 `cfg["display_width_px"]` 对 `None` 取下标而报错。

**call 入口与参数归一化**：

- [cookbooks/utils/agent_function_call.py:L89-L91](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L89-L91)：`call(self, params: Union[str, dict], **kwargs)` 第一行 `params = self._verify_json_format_args(params)`——`_verify_json_format_args` 是基类提供的助手，把 JSON 字符串或 dict 统一成 dict。类型注解 `Union[str, dict]` 呼应了这一点：无论执行器把模型输出原样传字符串，还是先 `json.loads` 成字典，工具都能接。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「工具定义 → 函数签名字典」的打包结果，建立对 `.function` 的直觉。
2. **操作步骤**：
   ```python
   # 示例代码（待本地验证，需先 pip install qwen-agent）
   from cookbooks.utils.agent_function_call import MobileUse
   import json

   tool = MobileUse(cfg={"display_width_px": 999, "display_height_px": 999})
   print(json.dumps(tool.function, indent=2, ensure_ascii=False))
   print(tool.description)   # 对比 description 是否被完整打包
   ```
   > 注意：`cookbooks` 目录不是包，实际运行时要么把脚本放在 `cookbooks/` 目录下用 `from utils.agent_function_call import MobileUse`（notebook 就是这么做的），要么自行把路径加入 `sys.path`。
3. **需要观察的现象**：打印出的 `tool.function` 应包含 `name`、`description`、`parameters` 三个键；`description` 中的分辨率处应显示 `999x999`。
4. **预期结果**：`name` 为 `"mobile_use"`（注册名而非类名），`parameters` 与源码 L18-L82 的字典一字不差。具体输出样式待本地验证。
5. 若想继续对照，把 `tool.function` 的 JSON 与 mobile_agent.ipynb「English App & Query with API」单元格中手写的 system_prompt 里 `<tools>` 块对比——结构相同（该单元格把工具签名包在 `{"type": "function", "function": {...}}` 里）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `description` 要写成 `@property` 而不是普通字符串类属性？
**答案**：因为它需要引用实例属性 `self.display_width_px` / `self.display_height_px`（f-string 在每次访问时求值），而类属性在定义时无法访问实例状态。参数 schema 与分辨率无关，所以 `parameters` 可以是普通类属性。

**练习 2**：`call()` 为什么接收 `Union[str, dict]`？
**答案**：模型的工具调用输出本质是文本中的 JSON 段，执行器可能原样传字符串、也可能先解析成 dict。基类的 `_verify_json_format_args` 负责归一化，使工具实现对两种输入都健壮。

**练习 3**：如果不传 `cfg` 直接 `MobileUse()`，会在哪一行、以什么方式失败？
**答案**：在 [L85](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L85) `cfg["display_width_px"]` 处失败，因为默认 `cfg=None`，对 `None` 做下标操作抛 `TypeError`。

---

### 4.2 mobile_use 动作集：九个 action 与 call() 分发

#### 4.2.1 概念说明

`mobile_use` 把「操作一台手机」离散化为 **9 个原子动作**。设计哲学是：动作集合要覆盖完成任务所需的最小操作原语，且每个动作的参数尽量少。移动端是**触摸语义**（click/swipe/long_press），与之对照，`computer_use` 是**鼠标键盘语义**（left_click/double_click/scroll/key），两套动作集的差异本身就是「设备形态决定动作原语」的教材。

另一个关键点：本文件的所有底层执行方法都是 `raise NotImplementedError()` 的**空壳**。仓库只交付「协议层」（schema + 分发），「驱动层」（真的去 adb tap、真的去 pyautogui.click）留给使用者按自己的环境实现——这是一个典型的**骨架/适配器**设计。

#### 4.2.2 核心流程

`call()` 的分发流程：

```text
call(params)
  ├─ params = _verify_json_format_args(params)   # str → dict
  ├─ action = params["action"]                    # 唯一必填键
  ├─ if/elif 链按 action 取出各自参数：
  │    click      → _click(coordinate)
  │    long_press → _long_press(coordinate, time)
  │    swipe      → _swipe(coordinate, coordinate2)
  │    type       → _type(text)
  │    system_button → _system_button(button)
  │    open/wait/terminate → _open(text)/_wait(time)/_terminate(status)
  └─ 未知 action → raise ValueError
```

九个动作的参数依赖关系表（这也是模型需要从 schema 中学会的全部内容）：

| action | 携带参数 | 语义 |
| --- | --- | --- |
| `key` | `text` | 按 adb `keyevent` 语法的硬件键，如 volume_up、power |
| `click` | `coordinate` | 点击 (x, y) |
| `long_press` | `coordinate` + `time` | 长按指定秒数 |
| `swipe` | `coordinate` + `coordinate2` | 从起点滑到终点 |
| `type` | `text` | 向已激活的输入框输入文本 |
| `system_button` | `button`（Back/Home/Menu/Enter） | 按系统导航键 |
| `open` | `text` | 打开指定 App |
| `wait` | `time` | 等待变化发生 |
| `terminate` | `status`（success/failure） | 终止任务并汇报结果，Agent 循环的出口 |

#### 4.2.3 源码精读

**action 字段：一个 description 同时充当文档与枚举说明**：

- [cookbooks/utils/agent_function_call.py:L20-L47](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L20-L47)：`action` 的 `description`（L21-L34）用 Markdown 列表逐一解释九个动作的语义与示例（如 `key` 支持 adb `keyevent` 语法，举例 "volume_up"、"power"）；`enum`（L35-L45）把合法取值硬性锁死为九个字符串，`type` 为 string。**描述讲道理，枚举划边界**，两者配合把模型的输出空间压缩到可分发范围。

**call() 的 if/elif 分发**：

- [cookbooks/utils/agent_function_call.py:L89-L117](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L89-L117)：`call()` 先归一化参数、取出 `action`，然后用 if/elif 链把每个动作映射到对应的私有方法，并从 `params` 中**按需解包**各自所需的键（如 L98-L101 的 `long_press` 取 `coordinate` 与 `time` 两个键）；L116-L117 对未知动作抛 `ValueError(f"Unknown action: {action}")`，兜住模型幻觉出不存在的动作的情况。

**全部空壳的驱动层**：

- [cookbooks/utils/agent_function_call.py:L119-L144](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L119-L144)：`_key`、`_click`、`_long_press`、`_swipe`、`_type`、`_system_button`、`_open`、`_wait`、`_terminate` 九个私有方法全部只有一行 `raise NotImplementedError()`。注意 Python 的方法解析顺序：**子类覆盖任意一个方法即可替换对应动作的执行**，无需改动 `call()`——这就是骨架设计的扩展点。

**对照组：computer_use 的鼠标键盘语义**：

- [cookbooks/utils/agent_function_call.py:L146-L226](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L146-L226)：`ComputerUse` 同样以 `@register_tool("computer_use")` 注册（L146-L147），但动作集换成 14 个桌面原语：`key`（按键序列）、`type`、`mouse_move`、五种点击（left/right/middle/double/triple）、`left_click_drag`、`scroll`/`hscroll`、`wait`、`terminate`、`answer`。它的 description（L150-L158）也更「桌面化」：强调「没有终端和开始菜单，必须点桌面图标启动应用」「点击前先看截图确定坐标」。
- [cookbooks/utils/agent_function_call.py:L233-L257](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L233-L257)：`ComputerUse.call()` 的分发与 MobileUse 同构，但 L236 把五种点击动作合并为一个分支 `if action in ["left_click", ...]: return self._mouse_click(action)`——按参数签名分组而非逐一枚举，是分发代码的小型重构示范。

#### 4.2.4 代码实践

1. **实践目标**：验证「子类覆盖一个空壳方法即可接入分发」，并确认 `call()` 对字符串参数与未知动作的行为。
2. **操作步骤**：
   ```python
   # 示例代码（待本地验证）
   from cookbooks.utils.agent_function_call import MobileUse

   class MyMobileUse(MobileUse):
       def _click(self, coordinate):
           print(f"[fake adb] input tap {coordinate[0]} {coordinate[1]}")
           return "clicked"

   tool = MyMobileUse(cfg={"display_width_px": 999, "display_height_px": 999})
   # ① 传 JSON 字符串（模拟执行器原样透传模型输出）
   print(tool.call('{"action": "click", "coordinate": [500, 500]}'))
   # ② 传 dict
   print(tool.call({"action": "click", "coordinate": [100, 200]}))
   # ③ 传未知动作
   try:
       tool.call({"action": "poke"})
   except ValueError as e:
       print("ValueError:", e)
   ```
3. **需要观察的现象**：①② 都应打印 `[fake adb] input tap ...`（证明 `_verify_json_format_args` 完成了 str→dict 归一化、分发命中 `_click`）；③ 应抛出 `ValueError: Unknown action: poke`。
4. **预期结果**：如上三条全部命中即通过；具体返回值格式待本地验证（注意：若不覆盖 `_click`，①② 会抛 `NotImplementedError`，因为基类空壳只抛异常不返回）。

#### 4.2.5 小练习与答案

**练习 1**：`terminate` 动作在 Agent 循环中扮演什么角色？它没有坐标参数，靠什么终结循环？
**答案**：它是循环的显式出口。模型判断任务完成（或无法完成）时输出 `{"action": "terminate", "status": "success"|"failure"}`，执行器（Agent 框架）看到 terminate 即停止循环并汇报 `status`，靠的是**控制流约定**而非参数。

**练习 2**：为什么 `swipe` 需要两个坐标参数，而 `click` 只需要一个？
**答案**：`swipe` 是有向操作，需要起点 `coordinate` 与终点 `coordinate2` 才能确定滑动向量；`click` 是无向的点操作，一个落点即可。

**练习 3**：如果要给 `mobile_use` 增加一个 `screenshot` 动作，最少要改源码里的哪几处？
**答案**：四处——action 的 `description` 列表（L21-L34）加一条说明、`enum`（L35-L45）加 `"screenshot"`、`call()` 的 if/elif 链（L89-L117）加一个分支、私有方法区（L119-L144）加 `_screenshot` 空壳或实现。改完后 `.function` 会自动携带新 schema。

---

### 4.3 参数 schema 设计：扁平结构 + 条件必填 + 枚举约束

#### 4.3.1 概念说明

仔细看 [L18-L82](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L18-L82) 的 `parameters`，会发现一个反直觉的设计：九个动作的参数**全部平铺在同一个 object 的 `properties` 里**，而 `required` 只有 `["action"]`。「click 才需要 coordinate、type 才需要 text」这种条件依赖关系，没有用 JSON Schema 的 `oneOf`/`if-then` 表达，而是**写进了每个参数的 description 自然语言里**（"Required only by `action=click`, ..."）。

这是一种「为 LLM 优化」的 schema 风格：

1. **扁平优于嵌套**：模型生成扁平 JSON 的可靠度显著高于深层嵌套或条件分支 schema；执行端解包也简单（`call()` 里就是 `params["coordinate"]`）。
2. **描述即文档**：参数何时必填的约束写在 description 里，模型读得懂；严格校验交给执行端的代码逻辑（缺键时自然 KeyError/ValueError）。
3. **枚举收边界**：取值集合有限的参数（`action`、`button`、`status`）一律用 `enum` 锁死，把生成空间从自由文本压缩为选择题。

#### 4.3.2 核心流程

schema 各参数与动作的条件依赖全表（`required` 仅有 `action`，其余全部「按动作条件必填」）：

```text
parameters.properties
├─ action      : string + enum[9]        ← 全局必填（required）
├─ coordinate  : array  ← click / long_press / swipe
├─ coordinate2 : array  ← swipe
├─ text        : string ← key / type / open
├─ time        : number ← long_press / wait
├─ button      : string + enum[Back, Home, Menu, Enter] ← system_button
└─ status      : string + enum[success, failure] ← terminate
```

#### 4.3.3 源码精读

**条件必填的自然语言表达**：

- [cookbooks/utils/agent_function_call.py:L48-L63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L48-L63)：`coordinate` 的 description 明写「Required only by `action=click`, `action=long_press`, and `action=swipe`」（L49）；`coordinate2` 只被 `swipe` 需要（L53）；`text` 被 `key`/`type`/`open` 需要（L57）；`time` 被 `long_press`/`wait` 需要（L61）。四个参数的描述采用完全相同的句式模板，便于模型形成稳定预期。

**枚举约束的三个层次**：

- [cookbooks/utils/agent_function_call.py:L35-L45](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L35-L45)：`action` 的九值枚举，决定 `call()` 能否分发；
- [cookbooks/utils/agent_function_call.py:L64-L73](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L64-L73)：`button` 的四值枚举（Back/Home/Menu/Enter），description 还逐个解释语义（「Back 返回上一界面，Home 回桌面…」），是**枚举 + 逐值释义**的范本；
- [cookbooks/utils/agent_function_call.py:L74-L78](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L74-L78)：`status` 的二值枚举（success/failure）。

**required 收口**：

- [cookbooks/utils/agent_function_call.py:L80-L82](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L80-L82)：`"required": ["action"]` 与 `"type": "object"` 收尾。只有 `action` 全局必填，其余参数的条件性全部下放到各字段的 description——schema 的机器校验强度与模型的自由度在此取得平衡。

**coordinate 的类型与坐标口径**：

- [cookbooks/utils/agent_function_call.py:L48-L51](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L48-L51)：`coordinate` 是 `"type": "array"` 的二元数组 `(x, y)`，描述称 x 是「pixels from the left edge」。**注意这里的措辞陷阱**：它声称是像素坐标，但消费端（notebook）把虚拟分辨率设为 999/1000，使「999×999 屏幕上的像素」事实上等价于 0-999 相对坐标——这正是 4.4 节要讲的关键设计。

#### 4.3.4 代码实践

1. **实践目标**：用机器校验体会「schema 只强约束 action，其余靠描述约定」的宽松度。
2. **操作步骤**：
   ```python
   # 示例代码（待本地验证，需 pip install jsonschema qwen-agent）
   import jsonschema
   from cookbooks.utils.agent_function_call import MobileUse

   tool = MobileUse(cfg={"display_width_px": 999, "display_height_px": 999})
   schema = tool.parameters

   cases = [
       {"action": "click", "coordinate": [500, 500]},   # 合法调用
       {"action": "swipe"},                              # 缺 coordinate：schema 层面合法？
       {"action": "poke"},                               # 非法动作：enum 应拦下
       {"action": "type", "text": "hello"},              # 合法调用
   ]
   for c in cases:
       try:
           jsonschema.validate(c, schema)
           print("PASS :", c)
       except jsonschema.ValidationError as e:
           print("FAIL :", c, "->", e.message[:60])
   ```
3. **需要观察的现象**：第 1、3、4 条的结果符合直觉（1、4 通过，3 被 enum 拦截）；**重点观察第 2 条**：`{"action": "swipe"}` 在 schema 校验下是**通过**的——因为 coordinate 不在 `required` 里。
4. **预期结果**：由此得出结论：条件必填不由 schema 保证，而由模型遵守 description + 执行端解包时报 KeyError 兜底（`call()` 里 `params["coordinate"]` 直接取键）。运行输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 schema 写成 `oneOf`（每个动作一个子 schema、各自 required 自己的参数）？
**答案**：`oneOf` 嵌套深、token 更长，模型生成与解析的出错率都会上升；扁平 + 自然语言条件在 LLM 场景下实测更稳，且 `call()` 只需 `params["xxx"]` 直接取键。这是「为模型优化」而非「为校验器优化」的取舍。

**练习 2**：`button` 参数的 description 为什么不厌其烦地解释 Back/Home/Menu/Enter 各自的含义？
**答案**：因为这四个词的语义模型无法从枚举值本身推断（尤其 Menu 指「应用后台菜单」这类设备相关语义）；description 是模型唯一的信息来源，逐值释义能显著降低选错键的概率。对比 `status` 的 success/failure 语义自明，就无需释义。

**练习 3**：`time` 参数是 `"type": "number"` 而非 `string`，`text` 反之。为什么？
**答案**：`time` 是数值语义（等待秒数、长按时长），数值类型让模型输出 `2.5` 这类小数成为合法值；`text` 是任意字符串（要输入的文本、按键名、App 名），没有数值结构。类型即语义提示。

---

### 4.4 工具注册机制与消费链路：从 .function 到 `<tool_call>` 解析

#### 4.4.1 概念说明

`@register_tool` 解决「工具从哪来」（注册表按名查找，qwen-agent 框架可用注册名实例化工具），而完整消费链路回答「工具怎么用」。computer_use.ipynb 展示了最标准的三步：

1. **实例化**：`ComputerUse(cfg={"display_width_px": 1000, "display_height_px": 1000})`——注意这个 **1000×1000 的「虚拟分辨率」**。
2. **渲染**：`NousFnCallPrompt().preprocess_fncall_messages(messages, functions=[computer_use.function], lang=None)` 用库里的提示词模板，把 `.function` 打包的签名渲染成 system prompt 中的 `<tools>` JSON 块与 `<tool_call>` 输出格式说明。
3. **解析与回投**：模型输出含 `<tool_call>...</tool_call>`，用字符串切分抽出 JSON；坐标按 `/1000 × resized_width` 从相对坐标回投到 smart_resize 后的截图上。

**虚拟分辨率技巧**是本讲最重要的设计洞察：把 description 里的屏幕分辨率写成 999×999 或 1000×1000，模型就相信自己面对一块 1000 见方的屏幕，它输出的「像素坐标」天然落在 0-1000 相对坐标系——与 Qwen-VL grounding 的坐标约定（u7-l1、u6-l4、u7-l3）完全同构。好处是：无论真实设备分辨率如何、截图如何缩放，坐标语义恒定，只需在执行端做一次比例换算。

#### 4.4.2 核心流程

```text
注册期：  @register_tool("mobile_use") → qwen-agent 工具注册表
使用期：  ComputerUse(cfg={1000×1000})
              ↓ tool.function
          NousFnCallPrompt.preprocess_fncall_messages(functions=[...])
              ↓ 渲染
          system prompt（含 <tools> JSON 与输出格式约定）
              ↓ LLM（附截图）
          "Thought... <tool_call>{"name":"computer_use","arguments":{...}}</tool_call>"
              ↓ 字符串切分 + json.loads
          {"name": ..., "arguments": {"action": "left_click", "coordinate": [x, y]}}
              ↓ 坐标回投
          绝对坐标 = 相对坐标 / 1000 × smart_resize 后的宽高
              ↓
          执行器 call(arguments) → 环境反馈新截图 → 循环
```

#### 4.4.3 源码精读

**API 路径：从实例化到渲染**（computer_use.ipynb「Computer Use with Inference API」单元格，以下锚点为原始 JSON 行）：

- [cookbooks/computer_use.ipynb:L158-L166](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L158-L166)：L158 附近 `ComputerUse(cfg={"display_width_px": 1000, "display_height_px": 1000})` 声明虚拟分辨率；L162 调 `NousFnCallPrompt().preprocess_fncall_messages(...)`，L166 把 `functions=[computer_use.function]` 传入——`.function` 打包的签名在此被模板消费，产出含 `<tools>` 块的 system 消息（随后 `system_message[0].model_dump()` 转回普通 dict 拼进 OpenAI 请求）。

**API 路径：解析 `<tool_call>` 与坐标回投**：

- [cookbooks/computer_use.ipynb:L205-L207](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L205-L207)：L205 `action = json.loads(output_text.split('<tool_call>\n')[1].split('\n</tool_call>')[0])`——用两次 split 抽出标签内的 JSON；L206-L207 `coordinate_absolute = [coordinate_relative[0] / 1000 * resized_width, coordinate_relative[1] / 1000 * resized_height]`——**除以 1000 再乘以缩放后的宽高**，正是 0-1000 相对坐标 → smart_resize 图上绝对坐标的换算（与 u6-l4 ODinW 判分侧 `is_rel=1` 分支同构）。

**本地模型路径：分辨率预算与 patch 口径**：

- [cookbooks/computer_use.ipynb:L398-L399](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L398-L399)：本地路径从 processor 动态取 `patch_size` 与 `merge_size` 调 `smart_resize(factor=patch_size*merge_size, min_pixels=p²m²·16, max_pixels=p²m²·6400)`。对 Qwen3-VL（patch 16、merge 2）即 factor=32、单图 16~6400 个视觉 token——正是 u2-l2/u3-l1 反复强调的 32 口径像素预算公式，这里用于「坐标可视化画在哪张缩放图上」。
- [cookbooks/computer_use.ipynb:L405-L417](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L405-L417)：本地路径同样实例化 `ComputerUse(cfg={1000×1000})` 并经 `NousFnCallPrompt` 渲染，区别在于消息里用 `ContentItem(image=f"file://{screenshot_path}")` 携带本地图片，再走 `processor.apply_chat_template(tokenize=False)` + `model.generate` 的 transformers 链路（u1-l3 已精读）。
- [cookbooks/computer_use.ipynb:L432-L435](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/computer_use.ipynb#L432-L435)：本地路径的解析与回投 `display_x = action['arguments']['coordinate'][0] / 1000 * resized_width`，与 API 路径逐字一致——**换引擎不换协议**。

**反例警示：mobile_agent.ipynb 的手写快照漂移**：

- [cookbooks/mobile_agent.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/mobile_agent.ipynb)（单行 JSON，无行号锚点，见「English App & Query with API」单元格）：该单元格虽然也 `from utils.agent_function_call import MobileUse` 并 import 了 `NousFnCallPrompt`，但 system_prompt 是**手写的 schema 快照**。把它与 [agent_function_call.py:L35-L45](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py#L35-L45) 对比会发现漂移：快照的枚举没有 `key` 与 `open`、多出一个 `answer`，且 `text` 的适用动作说明也不同。同时它把分辨率写成 `999x999`，解析侧配套用 `rescale_coordinates`（除以 **999**）回投。结论：**schema 快照会过期，最佳实践是用 `NousFnCallPrompt` + `tool.function` 现场生成**（computer_use.ipynb 即如此）；999 与 1000 两种虚拟分辨率并存也提醒我们：回投分母必须与 prompt 中声明的分辨率严格一致。

**README 定位**：

- [README.md:L120-L121](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L120-L121)：Cookbooks 总表中 Mobile Agent 与 Computer-Use Agent 两行的官方描述分别是「Locate and think for mobile phone control」与「Locate and think for controlling computers and Web」——「定位（Locate）+ 思考（think）」正对应本讲的两大支柱：0-1000 坐标输出与工具调用决策。

#### 4.4.4 代码实践

1. **实践目标**：用官方渲染器生成一份带工具 schema 的 system prompt，并亲手复现快照漂移的对比。
2. **操作步骤**：
   ```python
   # 示例代码（待本地验证，需 pip install qwen-agent）
   from qwen_agent.llm.fncall_prompts.nous_fncall_prompt import (
       NousFnCallPrompt, Message, ContentItem,
   )
   from cookbooks.utils.agent_function_call import MobileUse

   tool = MobileUse(cfg={"display_width_px": 999, "display_height_px": 999})
   msgs = NousFnCallPrompt().preprocess_fncall_messages(
       messages=[Message(role="system", content=[ContentItem(text="You are a helpful assistant.")])],
       functions=[tool.function],
       lang=None,
   )
   rendered = msgs[0].model_dump()
   text = "".join(m["text"] for m in rendered["content"])
   print(text)
   ```
3. **需要观察的现象**：渲染文本中应出现 `<tools>` 标签包裹的 JSON（内含 `mobile_use` 的 name/description/parameters），以及 `<tool_call>` 输出格式说明；description 中分辨率应为 `999x999`。
4. **预期结果**：拿这份渲染结果与 mobile_agent.ipynb 手写 system_prompt 逐字段 diff，确认 action 枚举是否包含 `key`/`open`、是否没有 `answer`，验证「快照漂移」结论。具体渲染文案随 qwen-agent 版本可能略有差异，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：虚拟分辨率设成 1000×1000 与 999×999 各有什么讲究？混用会发生什么？
**答案**：两者都是相对坐标口径（0-1000 与 0-999，Qwen-VL grounding 训练数据两种口径都有出现）。关键不在选哪个，而在**声明与回投必须配套**：prompt 声明 999×999 却按 /1000 回投，坐标会系统性偏差约 0.1%；mobile_agent.ipynb 全程用 999（`rescale_coordinates` 除以 999），computer_use.ipynb 全程用 1000，各自自洽。

**练习 2**：`output_text.split('<tool_call>\n')[1].split('\n</tool_call>')[0]` 这个解析为什么脆弱？可以怎么加固？
**答案**：它依赖标签后紧跟换行符、且全文只有一个 tool_call 块；若模型输出 `<tool_call>`（无换行）或一次输出多个调用就会错位或漏取。加固方式：用正则 `<tool_call>\s*(\{.*?\})\s*</tool_call>`（DOTALL）做 `findall` 取全部调用，或用 `json` 增量解析并捕获异常降级。

**练习 3**：为什么 computer_use.ipynb 本地路径要先 `smart_resize` 出 `resized_width/resized_height` 再回投坐标，而不是直接除以原图宽高？
**答案**：因为模型看到的（也被 smart_resize 处理的）就是缩放后的图，其 grounding 坐标针对那张图；画点可视化也画在 `input_image.resize((resized_width, resized_height))` 上，分母与画布必须一致，否则点会偏移。

---

## 5. 综合实践

**任务：实现一个 `pc_use` 工具（click / type / scroll / hotkey 四动作），注册并打印 schema，写出模型应生成的调用参数示例。** 这个任务把本讲四个最小模块全部串起来：继承 BaseTool（4.1）、设计动作集与分发（4.2）、写扁平 schema（4.3）、注册并说明消费方式（4.4）。

### 5.1 实践目标

- 会用 `@register_tool` + `BaseTool` 子类定义一个新工具；
- 会写「描述讲道理、枚举划边界、条件必填写进 description」的参数 schema;
- 能写出与自己的 schema 严格匹配的模型调用示例，并说明执行端如何解析。

### 5.2 参考实现

```python
# 示例代码：pc_use.py（待本地验证，需 pip install qwen-agent）
from typing import Union, Tuple, List

from qwen_agent.tools.base import BaseTool, register_tool


@register_tool("pc_use")
class PcUse(BaseTool):
    @property
    def description(self):
        return f"""
Use mouse and keyboard to operate a desktop PC, and take screenshots.
* This is an interface to a desktop GUI with mouse and keyboard.
* Some applications may take time to start, so you may need to wait and take
  successive screenshots to see the results of your actions.
* The screen's resolution is {self.display_width_px}x{self.display_height_px}.
* Make sure to click the center of buttons, links and icons, not their edges.
""".strip()

    parameters = {
        "properties": {
            "action": {
                "description": """
The action to perform. The available actions are:
* `click`: Click the left mouse button at coordinate (x, y).
* `type`: Input the specified text into the activated input box.
* `scroll`: Scroll the mouse wheel. Positive values scroll up, negative scroll down.
* `hotkey`: Press a key combination, e.g. ["ctrl", "c"].
""".strip(),
                "enum": ["click", "type", "scroll", "hotkey"],
                "type": "string",
            },
            "coordinate": {
                "description": "(x, y): The x (pixels from the left edge) and y (pixels from the top edge) coordinates. Required only by `action=click`.",
                "type": "array",
            },
            "text": {
                "description": "The text to input. Required only by `action=type`.",
                "type": "string",
            },
            "pixels": {
                "description": "The amount of scrolling. Required only by `action=scroll`.",
                "type": "number",
            },
            "keys": {
                "description": "The key combination to press, in order. Required only by `action=hotkey`.",
                "type": "array",
            },
        },
        "required": ["action"],
        "type": "object",
    }

    def __init__(self, cfg=None):
        self.display_width_px = cfg["display_width_px"]
        self.display_height_px = cfg["display_height_px"]
        super().__init__(cfg)

    def call(self, params: Union[str, dict], **kwargs):
        params = self._verify_json_format_args(params)
        action = params["action"]
        if action == "click":
            return self._click(params["coordinate"])
        elif action == "type":
            return self._type(params["text"])
        elif action == "scroll":
            return self._scroll(params["pixels"])
        elif action == "hotkey":
            return self._hotkey(params["keys"])
        else:
            raise ValueError(f"Unknown action: {action}")

    # 驱动层：接真实桌面自动化时替换以下四个方法（如 pyautogui）
    def _click(self, coordinate: Tuple[int, int]):
        raise NotImplementedError()

    def _type(self, text: str):
        raise NotImplementedError()

    def _scroll(self, pixels: int):
        raise NotImplementedError()

    def _hotkey(self, keys: List[str]):
        raise NotImplementedError()
```

### 5.3 操作步骤

1. 把上述代码保存为 `pc_use.py`（放在 `cookbooks/` 目录旁或自行加入 `sys.path`），先 `pip install qwen-agent`。
2. 写一个驱动脚本：
   ```python
   # 示例代码（待本地验证）
   import json
   from pc_use import PcUse

   tool = PcUse(cfg={"display_width_px": 1000, "display_height_px": 1000})
   print(json.dumps(tool.function, indent=2))        # 打印 schema（三键：name/description/parameters）
   ```
3. 参考 4.4.4 的 `NousFnCallPrompt` 代码，把 `functions=[tool.function]` 渲染成 system prompt，确认 `<tools>` 块中出现 `pc_use`。
4. 写出「模型应如何生成调用参数」的示例（见 5.4），并手写一个假 `output_text` 字符串，用 `split('<tool_call>\n')[1].split('\n</tool_call>')[0]` 解析，验证能还原出 arguments。

### 5.4 需要观察的现象与预期结果

模型在一次 `pc_use` 交互中应输出（示例，非项目原有代码）：

```
Thought: I need to click the search box first.
Action: click the search box
<tool_call>
{"name": "pc_use", "arguments": {"action": "click", "coordinate": [512, 300]}}
</tool_call>
```

四类动作的合法 arguments 一览（坐标落在 0-1000 相对坐标系，因为虚拟分辨率声明为 1000×1000）：

| 模型输出 arguments | 执行端分发到 | 真实操作（换算示例） |
| --- | --- | --- |
| `{"action": "click", "coordinate": [512, 300]}` | `_click` | 屏幕 1920×1080 上点击 (512/1000×1920, 300/1000×1080) ≈ (983, 324) |
| `{"action": "type", "text": "Qwen3-VL"}` | `_type` | 向聚焦输入框键入文本 |
| `{"action": "scroll", "pixels": -300}` | `_scroll` | 向下滚动 300 像素 |
| `{"action": "hotkey", "keys": ["ctrl", "s"]}` | `_hotkey` | 依次按下 ctrl、s 再反序释放 |

预期结果：`tool.function` 打印出 name 为 `"pc_use"` 的三键字典；假输出解析出的 arguments 与上表逐字一致；非法 action（如 `{"action": "drag"}`）被 schema 的 enum 拦截或被 `call()` 的 `ValueError` 兜底。全部待本地验证。

## 6. 本讲小结

- **BaseTool 契约四要素**：`description`（property，喂给模型的功能说明）、`parameters`（JSON Schema 类属性）、`call()`（执行入口，经 `_verify_json_format_args` 归一化 str/dict）、`@register_tool`（注册名与类名解耦）；`.function` 把前三者打包成函数签名供 prompt 渲染。
- **agent_function_call.py 是协议层骨架**：`mobile_use` 九动作（触摸语义）与 `computer_use` 十四动作（鼠标键盘语义）的 `call()` 只做 if/elif 分发，九个/十四个私有执行方法全部 `NotImplementedError`，驱动层留给子类覆盖——扩展点清晰。
- **schema 是写给模型看的**：扁平结构 + 唯一必填 `action` + 条件必填写进 description 自然语言 + 有限取值一律 enum，牺牲机器校验强度换取模型生成可靠度。
- **虚拟分辨率技巧**：把 description 中分辨率声明为 999×999/1000×1000，模型输出的「像素坐标」即 0-1000 相对坐标，与 Qwen-VL grounding 口径同构；回投换算 `相对/1000×缩放宽高`，分母必须与声明严格一致。
- **标准消费链路**：`tool.function` → `NousFnCallPrompt().preprocess_fncall_messages` 渲染 `<tools>` → 模型输出 `<tool_call>` JSON → split 解析 → 坐标回投到 smart_resize 后的图（本地路径的 factor 与像素预算取自 processor 的 patch_size/merge_size，即 32 口径）。
- **快照漂移警示**：mobile_agent.ipynb 的手写 system prompt 与 agent_function_call.py 当前 schema 已不一致（缺 `key`/`open`、多 `answer`），工具 schema 应由 `tool.function` 现场生成而非手抄。

## 7. 下一步学习建议

1. **下一讲 u7-l3（2D/3D Grounding 实践与坐标格式体系）**：本讲的 0-1000 相对坐标在那里得到完整闭环——用 grounding 提示模板让模型输出 bbox/point JSON、画框可视化，并用 `qwen-vl-finetune/tools/process_bbox.ipynb` 把 COCO 标注转成训练格式，是本讲坐标概念向训练侧的延伸。
2. **阅读 qwen-agent 库源码**（[github.com/QwenLM/qwen-agent](https://github.com/QwenLM/qwen-agent)）：重点看 `tools/base.py` 的 `BaseTool`（`.function` 的打包逻辑、`_verify_json_format_args`）与 `llm/fncall_prompts/nous_fncall_prompt.py`（`<tools>`/`<tool_call>` 模板原文），把本讲「从用法反推的契约」落实为库源码。
3. **动手接一个真实驱动**：为综合实践的 `pc_use` 补上 `pyautogui` 实现并在自己桌面跑通一步循环（截图→模型→click→新截图），体会协议层/驱动层分离带来的收益；再对照 mobile_agent.ipynb 的「Task progress」组织方式，把单步扩展为多步历史。
4. **向训练侧回望**：若想让模型更稳地输出你自定义工具的调用格式，可回到单元五，参考 u4-l2 的 grounding 标注格式与 u7-l4 的端到端微调闭环，把工具调用轨迹做成 SFT 数据。
