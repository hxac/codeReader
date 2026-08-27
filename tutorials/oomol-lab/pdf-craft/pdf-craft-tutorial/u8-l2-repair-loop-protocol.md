# 修复循环：协议驱动的重试

## 1. 本讲目标

上一讲（u8-l1）我们读了 `LLMRuntime`：它负责把请求发出去、在**传输层**重试网络错误、并管理缓存。但还有一类失败它管不了——**请求成功了，内容却是坏的**：模型返回了半截 JSON、缺字段的表格、没闭合的 XML。这时重发同样的请求往往得到同样的坏结果，必须「告诉模型它错在哪，让它改」。

本讲精读 pdf-craft 的语义层修复机制。读完本讲你应该能够：

1. 掌握 `ResponseProtocol` 的三个方法与 `ProtocolSuccess` / `ProtocolRetry` / `ProtocolPartial` / `ProtocolFailure` 四种判定结果的确切语义。
2. 理解 `run_repair_loop` 的编排逻辑：反馈历史如何构造、如何裁剪、`state` 如何跨轮传递、耗尽后如何兜底。
3. 理解 `request_guaranteed_json` 这层「保证式执行」包装的三级校验漏斗，以及 `increasable` 模块如何让重试随尝试次数「升温」。
4. 能独立实现一个自定义 `ResponseProtocol` 并用单元测试验证反馈消息确实进入了第二次请求。

## 2. 前置知识

**传输层重试 vs 语义层修复。** 这是本讲最重要的一组对照。u8-l1 讲过，`LLMContext.request` 内部有一个针对网络异常的重试循环（超时、5xx 等，见 [pdf_craft/llm/runtime.py:121-146](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L121-L146)），它的假设是「请求本身没问题，只是没发出去，原样再发一次即可」。而语义层修复针对的是「请求成功返回了，但内容不合法」，它需要**改变对话内容**——把坏答案和错误描述追加进消息列表，让模型看着自己的错误重写。两层各管各的，互不替代。

**协议（Protocol）。** Python 的 `typing.Protocol` 是结构化类型：只要一个类「长得像」（方法名与签名对得上），就算实现了协议，无需继承。u7-l1 的 `PackageTransformer` 已见过这种风格。本讲的 `ResponseProtocol` 同样是一个协议：任何提供 `validate` / `empty` / `exhausted` 三个方法的对象都能塞进修复循环。

**json-repair 与 pydantic。** `json-repair` 是一个容错 JSON 解析库，能修复尾逗号、缺引号、截断等小毛病（如把 `{"value":}` 修成 `{"value": null}`）。`pydantic` 的 `BaseModel.model_validate` 则做**结构校验**：字段类型不对、必填字段缺失都会抛 `ValidationError`。两者分工：前者救「语法」，后者验「结构」。

**对话式纠错。** 大模型的 chat 接口是无状态的：每次请求都要把完整历史发过去。所谓「反馈历史」，就是在重试时把 `[assistant 的坏答案, user 的错误反馈]` 追加到原始消息后面——模型看到自己上一轮的输出和出错原因，修正率会显著提高。

**前置讲义。** 本讲依赖 u8-l1（LLM 配置与运行时、缓存键、`retry_index` 接口）；u7-l2 与 u7-l5 已从消费者视角见过 `run_repair_loop`，本讲打开它的黑盒。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pdf_craft/llm/loop.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py) | 修复循环核心：四种判定结果、`ResponseProtocol` 协议、`run_repair_loop` 编排器（约 90 行） |
| [pdf_craft/llm/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/types.py) | `Message` 与 `MessageRole`（SYSTEM/USER/ASSISTANT）两个基础类型 |
| [pdf_craft/llm/guaranteed.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py) | 「保证式执行」上层包装：JSON 提取、json-repair、pydantic 校验、业务校验三级漏斗 |
| [pdf_craft/llm/increasable.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/increasable.py) | 采样参数（temperature/top_p）的区间归一化与「升温」器 |
| [pdf_craft/llm/runtime.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py) | 运行时侧：`_scheduled` 如何消费修复循环传来的尝试序号（u8-l1 已精读，本讲只看升温相关行） |
| [tests/test_llm_loop.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_loop.py) | 修复循环的五个单元测试，全部用假 request 函数，零网络 |
| [tests/test_llm_guaranteed.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_guaranteed.py) | guaranteed 层的两个测试：坏 JSON 修复重试、空响应耗尽 |
| pdf_craft/extractor/toc/llm_analyser.py | 消费者一：目录层级 LLM 分析走 `request_guaranteed_json` |
| pdf_craft/transformer/xml_translator/xml_translator/translator.py | 消费者二：XML 回填走自定义 `_XMLProtocol` + `run_repair_loop` |

## 4. 核心概念与源码讲解

### 4.1 响应协议：ResponseProtocol 与四种判定结果

#### 4.1.1 概念说明

修复循环要面对一个决策问题：拿到 LLM 的回答之后，「这个回答算不算合格？不合格的话下一步怎么办？」如果把这套判定写死在循环里，每换一种输出格式（JSON、XML、纯文本）都要改循环本身。

pdf-craft 的解法是**把判定权外包**：循环只负责「请求 → 判定 → 追加反馈 → 再请求」的骨架，每次判定则委托给一个「响应协议」对象。协议是一个只有三个方法的纯 Python 类，没有基类约束（`typing.Protocol` 结构化类型），因此可以为任何输出格式现场写一个。

判定的全部可能出口被建模为**四种冻结 dataclass**，它们是本讲的核心词汇表。

#### 4.1.2 核心流程

四种判定结果与循环的对应行为：

| 结果 | 携带数据 | 语义 | 循环行为 |
|---|---|---|---|
| `ProtocolSuccess` | `value`, `state` | 校验通过，产出最终值 | 立即返回 `value` |
| `ProtocolPartial` | `value`, `state`, `warning` | 不完美但可用（带警告） | 立即返回 `value`（不重试） |
| `ProtocolRetry` | `feedback`, `state`, `include_response`, `reset_history` | 不合格但值得再试 | 把反馈追加进对话，进入下一轮 |
| `ProtocolFailure` | `error`, `state` | 不合格且不可修复 | 立即抛出 `error`，不再重试 |

`ResponseProtocol` 协议要求实现三个方法，分别覆盖三种输入情形：

```text
validate(response, state, attempt, max_attempts)   # 非空回答 → 四种结果之一
empty(state, attempt, max_attempts)                # 空白回答 → 通常是 ProtocolRetry
exhausted(state, attempts, response)               # 所有轮次用完 → 返回兜底值 T，或抛异常
```

注意两个细节：

- `validate` 与 `empty` 收到的是 `(attempt, max_attempts)`（当前轮与总轮数），协议可以据此调整策略——比如 guaranteed 层用 `attempt` 实现「第二次还像在拒绝就快败」。
- `exhausted` 的返回类型是 `T`（与成功值同型），所以它**既可以返回降级值，也可以直接抛异常**——两种风格在 pdf-craft 里都有真实用户（见 4.3.3）。

#### 4.1.3 源码精读

四种结果是四个泛型冻结 dataclass，定义在 [pdf_craft/llm/loop.py:13-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L13-L40)：

```python
@dataclass(frozen=True)
class ProtocolSuccess(Generic[T, S]):
    value: T
    state: S

@dataclass(frozen=True)
class ProtocolRetry(Generic[S]):
    feedback: str
    state: S
    include_response: bool = True    # 重试时是否把坏答案也发给模型
    reset_history: bool = True       # 重试前是否清空之前的反馈历史
```

两个泛型参数的含义：`T` 是**成功值的类型**（解析出的 JSON 对象、回填结果等），`S` 是**协议自定义状态的类型**。`ProtocolRetry` 上两个开关默认值都为 `True`，即默认行为是「让模型看见自己上次的坏答案，且只保留最近一轮纠错上下文」。

`ProtocolResult` 联合类型与 `ResponseProtocol` 协议在 [pdf_craft/llm/loop.py:40-48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L40-L48)：

```python
ProtocolResult = ProtocolSuccess[T, S] | ProtocolRetry[S] | ProtocolPartial[T, S] | ProtocolFailure[S]

class ResponseProtocol(Protocol[T, S]):
    def validate(self, response: str, state: S, attempt: int, max_attempts: int) -> ProtocolResult[T, S]: ...
    def empty(self, state: S, attempt: int, max_attempts: int) -> ProtocolResult[T, S]: ...
    def exhausted(self, state: S, attempts: int, response: str | None) -> T: ...
```

消息类型极简，只有角色与文本两个字段（[pdf_craft/llm/types.py:5-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/types.py#L5-L14)）：

```python
@dataclass
class Message:
    role: "MessageRole"
    message: str

class MessageRole(Enum):
    SYSTEM = auto()
    USER = auto()
    ASSISTANT = auto()
```

这一层全部被导出为公开 API（[pdf_craft/llm/\_\_init\_\_.py:4-10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/__init__.py#L4-L10)），所以下游（如 XMLTranslator）直接 `from pdf_craft.llm import ProtocolRetry, run_repair_loop` 使用。

关于 `ProtocolPartial`：它表示「能凑合用但有问题」——循环对它的处理与 `Success` 完全一样（立即返回 `value`），`warning` 字段循环本身不消费，留给协议自己记录（比如放进 `state` 或日志）。目前仓库内还没有生产代码返回 `Partial`，它是给「宁可要次优解也不要失败」的场景预留的出口。

#### 4.1.4 代码实践

**实践目标**：不写代码，先建立「测试即规格」的直觉——五个单元测试各自锁定了循环的哪条行为。

**操作步骤**：

1. 打开 [tests/test_llm_loop.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_loop.py)，通读五个测试方法。
2. 注意所有测试的 `request` 都是普通 Python 函数（如 [tests/test_llm_loop.py:23-25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_loop.py#L23-L25) 的 `request` 第一次返回 `"bad"`、第二次返回 `"ok"`），完全不碰网络——这正是循环与传输解耦的红利。
3. 对照下表填写每个测试验证的行为（答案见 4.1.5）：

| 测试方法 | 验证的行为 |
|---|---|
| `test_retries_with_typed_protocol_and_bounded_history` | ？ |
| `test_preserves_initial_messages_when_history_is_cropped` | ？ |
| `test_reset_history_discards_previous_retry_messages_but_keeps_initial` | ？ |
| `test_max_attempts_is_total_requests_and_transport_errors_propagate` | ？ |
| `test_protocol_callback_exceptions_propagate` | ？ |

**需要观察的现象**：`seen` 列表里每次请求收到的消息条数与角色顺序。

**预期结果**：能够口头说出每个测试对应的循环分支；其中第四个测试断言 `calls == [(0, 2), (1, 2), (2, 2)]`，说明 `max_attempts=3` 是**总请求次数**而非额外重试次数，且传给 `request` 的第三个参数是最大轮次下标 `2`。待本地验证：可运行 `python -m pytest tests/test_llm_loop.py -v` 确认五个测试全绿。

#### 4.1.5 小练习与答案

**练习 1**：`ProtocolPartial` 和 `ProtocolSuccess` 在循环里行为相同，为什么还要单独设计一个类型？

**参考答案**：语义不同。`Success` 表示「完全合格」，`Partial` 表示「勉强可用」——值会被返回，但 `warning` 字段给了协议一个记录瑕疵的通道（可写入 `state`、日志或回调）。对调用方而言，看到 `Partial` 就知道结果可能需要降级处理。类型上的区分让「凑合」在代码里显式可见，而不是被无声地当作成功。

**练习 2**：协议的 `exhausted` 方法签名返回 `T`，但 guaranteed 层在里面抛异常。这矛盾吗？

**参考答案**：不矛盾。Python 里任何返回 `T` 的函数都可以选择抛异常代替返回。签名上写 `-> T` 表达的是「正常路径下必须给出一个与成功值同型的兜底值」；抛异常是合法的异常路径。仓库内两种风格并存：guaranteed 层抛 `GuaranteedExhaustedError`（调用方必须处理失败），XML 回填协议返回 `None`（调用方降级为保留原文，不让单章失败中断全书）。

**练习 3**：`ResponseProtocol` 用 `typing.Protocol` 而不是抽象基类，好处是什么？

**参考答案**：结构化类型——实现类无需 `import` 也无需继承任何基类，只要方法签名匹配即可。这让协议类可以是闭包内定义的局部类（如 guaranteed.py 的 `_JsonProtocol`、translator.py 的 `_XMLProtocol`），捕获外层变量，零样板代码。

### 4.2 修复循环：run_repair_loop 的编排

#### 4.2.1 概念说明

`run_repair_loop` 是一个**与 LLM 完全解耦的纯编排器**：它不知道 openai，不知道 HTTP，只认识一个 `request` 回调（`Callable[[list[Message], int, int], str]`）。这个设计有两个直接后果：

1. **可测性**：单元测试传入假函数即可覆盖全部分支（4.1.4 已见）。
2. **职责纯净**：网络重试属于 `request` 回调内部的传输层（u8-l1 的 `LLMContext`），`run_repair_loop` 只管语义修复。回调抛出的传输异常会原样穿透循环（[tests/test_llm_loop.py:98-102](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_loop.py#L98-L102) 验证了这一点）。

它要解决的核心问题是**反馈历史的管理**：重试时给模型看多少上下文？看全部历史会随轮数膨胀（浪费 token 且模型容易被旧错误带偏），只看初始提示又丢失了「错在哪」的信息。pdf-craft 的答案是一个带裁剪窗口的滚动历史。

#### 4.2.2 核心流程

```text
初始化: initial = 初始消息列表（system + 任务），不可变
       current = initial；retry_history = []

for attempt in 0..attempts-1:            # attempts = max(1, max_attempts)
    response = request(current, attempt, attempts-1)
    result = (response 为空白) ? protocol.empty(...) : protocol.validate(...)
    state = result.state                  # 每种结果都携带新状态，跨轮传递

    Success | Partial → 返回 result.value
    Failure          → 抛出 result.error
    已到最后一轮      → 跳出循环，调用 protocol.exhausted(state, attempts, last_response)

    # 还没到最后一轮，为下一轮构造反馈历史：
    if result.reset_history: retry_history = []          # 默认清空
    additions = ([ASSISTANT(response)] if include_response 且 response 非空 else [])
                + [USER(result.feedback)]
    retry_history = (retry_history + additions)[-history_limit:]   # 只留最近 N 条
    current = initial + retry_history
```

一条关键的尺寸换算：`history_limit` **数的是消息条数，不是轮数**。默认 `include_response=True` 时每轮追加 2 条（坏答案 + 反馈），默认 `history_limit=2` 恰好保留「最近一轮纠错对话」。若想保留最近两轮，需要 `history_limit=4`。

#### 4.2.3 源码精读

循环的配置对象 [pdf_craft/llm/loop.py:51-58](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L51-L58)：

```python
@dataclass
class RepairLoopOptions(Generic[T, S]):
    messages: Sequence[Message]                       # 初始消息（永不裁剪）
    request: Callable[[list[Message], int, int], str] # (消息, 当前轮, 最大轮下标) -> 回答
    protocol: ResponseProtocol[T, S]
    state: S                                          # 协议初始状态
    max_attempts: int = 1                             # 总请求次数（含首次）
    history_limit: int = 2                            # 反馈历史窗口（消息条数）
```

主循环 [pdf_craft/llm/loop.py:61-87](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L61-L87)，逐段看四个要点：

**要点一：请求与判定**（[L69-71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L69-L71)）——第三个实参是 `attempts - 1`（最大轮次下标），测试断言 `(0, 2), (1, 2), (2, 2)` 印证了这一点；空白响应走 `empty`，否则走 `validate`：

```python
response = options.request(current, attempt, attempts - 1)
last_response = response
result = options.protocol.empty(...) if not response.strip() else options.protocol.validate(response, state, attempt, attempts)
state = result.state
```

**要点二：三种终点**（[L73-78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L73-L78)）——`Success` 与 `Partial` 返回值，`Failure` 抛错，均在判定当轮立即生效，不再构造历史：

```python
if isinstance(result, ProtocolSuccess):
    return result.value
if isinstance(result, ProtocolPartial):
    return result.value
if isinstance(result, ProtocolFailure):
    raise result.error
```

**要点三：历史构造与裁剪**（[L79-86](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L79-L86)）——注意 `if attempt + 1 >= attempts: break` 保证最后一轮的 `Retry` 不会白费功夫构造历史：

```python
if attempt + 1 >= attempts:
    break
if result.reset_history:
    retry_history = []
additions = ([Message(MessageRole.ASSISTANT, response)] if result.include_response and response else [])
additions.append(Message(MessageRole.USER, result.feedback))
retry_history = [*retry_history, *additions][-max(1, options.history_limit):]
current = [*initial, *retry_history]
```

**要点四：耗尽兜底**（[L87](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L87)）——`exhausted` 拿到总尝试数与最后一次响应，由协议决定返回降级值还是抛异常：

```python
return options.protocol.exhausted(state, attempts, last_response)
```

再看一个真实消费者，体会 `reset_history` 与 `include_response` 的实战取值。XML 回填协议（[pdf_craft/transformer/xml_translator/xml_translator/translator.py:194-227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L194-L227)）：

```python
def validate(self, response, state, attempt, max_attempts):
    ...
    return ProtocolRetry(error, state, include_response=True, reset_history=True)
```

这里显式写出默认组合：每轮让模型看到「上一份坏 XML + 具体错误（如『No complete <xml>...</xml> block found』）」，且不累积多轮旧错——因为回填错误通常是彼此独立的语法问题，旧上下文只会添乱。它的 `exhausted`（[L213-219](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L213-L219)）发出最终的 `FillFailedEvent` 后返回 `None`，调用方据此把该片段保持原文——单片段失败不中断全书翻译（u7-l5 的降级策略）。

#### 4.2.4 代码实践

**实践目标**：亲手观察 `history_limit`、`reset_history`、`include_response` 三个旋钮对每轮请求消息序列的影响。

**操作步骤**（示例代码，可在仓库根目录新建 `practice_loop_history.py` 运行）：

```python
# 示例代码：观察修复循环的反馈历史窗口
from pdf_craft.llm import Message, MessageRole, ProtocolRetry, ProtocolSuccess, RepairLoopOptions, run_repair_loop

class AlwaysRetry:
    def validate(self, response, state, attempt, max_attempts):
        return ProtocolRetry(f"fix {attempt}", state)          # 默认 include_response=True, reset_history=True
    def empty(self, state, attempt, max_attempts):
        return ProtocolRetry("empty", state)
    def exhausted(self, state, attempts, response):
        return "exhausted"

def request(messages, attempt, maximum):
    print(f"第 {attempt} 轮请求，共 {len(messages)} 条消息：",
          [(m.role.name, m.message[:20]) for m in messages])
    return "bad"

result = run_repair_loop(RepairLoopOptions(
    messages=[Message(MessageRole.SYSTEM, "你是翻译器")],
    request=request, protocol=AlwaysRetry(), state=None,
    max_attempts=4, history_limit=2,
))
print("最终结果：", result)
```

运行 `python practice_loop_history.py`，然后做三组改动各跑一次：

1. 把 `history_limit` 从 2 改为 4。
2. 给 `ProtocolRetry` 加 `reset_history=False`（保持 `history_limit=2` 先跑一次，再把 `history_limit` 也改成 4 跑一次）。
3. 给 `ProtocolRetry` 加 `include_response=False`。

**需要观察的现象**：每轮打印的消息条数与角色序列如何变化；初始的 SYSTEM 消息是否始终在场。

**预期结果**（依据 loop.py L81-L86 的裁剪逻辑推演，待本地验证）：

- 原版（默认值）：每轮都是 3 条消息 = SYSTEM + ASSISTANT("bad") + USER("fix N")——`reset_history=True` 清空后只追加本轮 2 条，窗口 2 恰好容纳。
- 改动 1：仍是 3 条——因为历史每轮都被重置，窗口再大也只装得下 2 条新消息。
- 改动 2（关键观察点）：只加 `reset_history=False`、窗口仍为 2 时，打印结果与原版**看不出差别**——历史虽在累积，但每轮都被裁到最近 2 条，恰是一轮的量。再把 `history_limit` 改为 4，第 3 轮起请求变成 5 条 = SYSTEM + 最近两轮的「坏答案 + 反馈」对——这才是跨轮保留纠错上下文的形态。
- 改动 3：每轮只剩 2 条 = SYSTEM + USER("fix N")——模型看不到自己的坏答案，只知道要改什么。

**预期最终结果**：四轮全部失败后返回 `"exhausted"`（`exhausted` 的兜底返回值）。

#### 4.2.5 小练习与答案

**练习 1**：`max_attempts=5` 时模型最多被请求几次？若首轮就返回 `ProtocolFailure` 呢？

**参考答案**：最多 5 次（`max_attempts` 是总请求次数，循环里 `attempts = max(1, options.max_attempts)` 兜底至少 1 次）。若首轮返回 `ProtocolFailure`，循环在第 73-78 行的判定处立即 `raise result.error`，只请求了 1 次。

**练习 2**：`request` 回调在第二轮抛出 `LLMTransportError`（网络耗尽），会发生什么？

**参考答案**：异常原样穿透 `run_repair_loop` 向上传播——循环没有任何 try/except 包裹 `options.request(...)`。这是刻意设计：传输层重试是 `LLMContext` 的职责（u8-l1），语义修复循环不该越权吞掉传输错误。`test_max_attempts_is_total_requests_and_transport_errors_propagate` 的后半段验证的正是这一点。

**练习 3**：为什么初始消息 `initial` 永不裁剪，而只裁剪 `retry_history`？

**参考答案**：初始消息携带任务定义（system 提示词、原始输入），丢掉它模型就不知道自己在干什么，重试毫无意义；而反馈历史只是「最近犯了什么错」的参考，越旧的信息价值越低、干扰越大。裁剪窗口既控制 token 成本，又避免模型被多轮旧错误带偏。

### 4.3 保证式执行：guaranteed 与 increasable 上层包装

#### 4.3.1 概念说明

有了 `run_repair_loop`，每种输出格式仍要手写一遍协议。`guaranteed.py` 针对**最常见的需求——「必须拿到一个合法且结构正确的 JSON」**——提供了一个开箱即用的协议实现 `request_guaranteed_json`，称为「保证式执行」：要么返回一个通过全部校验的值，要么抛出带完整上下文的异常，绝不悄悄返回半成品。

它的校验是一个**三级漏斗**，每级失败都产生更具体的反馈：

```text
LLM 响应
  │
  ├─ ① 提取 + json-repair + json.loads   ──失败──→ 重试："只返回完整合法 JSON，不要解释/代码围栏"
  │      （若看起来像自然语言拒绝 → 直接 ProtocolFailure 快败，不浪费轮次）
  ├─ ② pydantic schema 校验             ──失败──→ 重试：逐字段列出结构问题
  └─ ③ 业务校验（parse 回调）            ──失败──→ 重试：附上业务异常原文
         │
         └── 全部通过 → ProtocolSuccess(parse(...) 的返回值)
```

与 guaranteed 配套的 `increasable.py` 解决另一个问题：**同样的消息 + 同样的采样参数 = 大概率同样的坏答案**。重试时应当「升温」——让 temperature 沿配置区间随尝试次数攀升，把模型从失败的采样模式里推出来。

#### 4.3.2 核心流程

`request_guaranteed_json` 的调用形态（消费者视角）：

```text
GuaranteedOptions(
    messages    = 初始消息
    request     = (消息, 轮次, 最大轮次) -> 响应文本     # 由调用方接入 LLMRuntime
    schema      = pydantic BaseModel 类                  # 结构契约
    parse       = (已验证数据, 轮次, 最大轮次) -> 结果    # 业务校验 + 值变换
    max_retries = 12（默认，指额外重试次数，总请求 = max_retries + 1）
    extractor   = 可选的自定义 JSON 提取器
)
```

「升温」的数学。`Increasable` 把配置归一化为区间：标量 `0.7` 变成 `(0.7, 0.7)`（恒定），元组 `(0.7, 1.2)` 表示区间。修复循环把轮次下标 \(k\) 与最大下标 \(m\) 传给 `LLMContext.request(retry_index=k, retry_max=m)`，运行时按**线性插值**取值：

\[ t_k = t_{start} + (t_{end} - t_{start}) \cdot \frac{\min(\max(k, 0),\, m)}{m} \]

首轮 \(k=0\) 取区间起点，末轮 \(k=m\) 取区间终点，中间线性过渡。另一条路径是上下文内的几何升温（`LLMContext.request` 的 `finally` 块每次调用 `increase()`）：

\[ c_{n+1} = c_n + \tfrac{1}{2}\,(c_{end} - c_n) \]

即每次向区间终点走完剩余距离的一半——单调逼近且永不越界。

#### 4.3.3 源码精读

**错误类型体系**先立于流程之前（[pdf_craft/llm/guaranteed.py:21-44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L21-L44)）：基类 `GuaranteedRequestError` 统一携带 `attempts`（尝试次数）、`response`（最后响应）、`cause`（根因异常），派生出 `GuaranteedProtocolError`（像拒绝）、`GuaranteedSchemaError`（结构不合）、`GuaranteedBusinessError`（业务校验失败）、`GuaranteedExhaustedError`（语法始终没修好）、`GuaranteedEmptyResponseError`（一直空响应）。调用方捕获一个基类即可拿到完整事故报告。

三级漏斗在闭包类 `_JsonProtocol.validate` 里（[pdf_craft/llm/guaranteed.py:62-80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L62-L80)）：

```python
try:
    extractor = options.extractor or _extract_json
    data = json.loads(repair_json(extractor(response)))          # 第①级：语法
except (ValueError, json.JSONDecodeError) as error:
    if _looks_like_refusal(response) and attempt >= min(1, options.max_retries):
        return ProtocolFailure(GuaranteedProtocolError(...), state)   # 拒绝快败
    self.last_error = GuaranteedExhaustedError(...)
    return ProtocolRetry("Return complete valid JSON only; do not explain or use markdown fences.", state)
```

拒绝快败的门条件 `attempt >= min(1, options.max_retries)` 很精巧：默认 `max_retries=12` 时门槛是 1，即**首个像拒绝的回答仍给一次改正机会**（也许只是忘了带 JSON），第二次仍在「道歉/无法/抱歉」就判定为拒绝、立即失败——对执意拒绝的模型重试 12 次纯属浪费 token。判定函数 [_looks_like_refusal](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L100-L101)（[L100-101](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L100-L101)）要求「命中拒绝词（中英文）**且**全文没有任何 `[` 或 `{`」——带括号的回答不算拒绝。

第②级 pydantic 校验（[L71-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L71-L75)）失败时，反馈不是笼统一句话，而是由 [_schema_feedback](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L108-L109)（[L108-109](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L108-L109)）逐字段生成问题清单（`- 字段路径: 错误信息`），让模型能精确定位。

第③级业务校验（[L76-80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L76-L80)）：`parse` 回调抛任何异常都转为 `ProtocolRetry`，异常原文进反馈。

注意一个实现细节：`_JsonProtocol` 的 `state` 泛型槽填的是 `None`，它改用实例属性 `self.last_error` 记录「最近一次失败该归为哪类异常」（[L69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L69)、[L74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L74)），最终在 [exhausted](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L85-L88)（[L85-88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L85-L88)）抛出——`state` 槽是通用机制，不是必填项。

默认 JSON 提取器 [_extract_json](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L94-L97)（[L94-97](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L94-L97)）：先剥掉 ```` ```json ```` 代码围栏，再在全文中找最早出现的 `[...]` 或 `{...}` 片段——容忍模型在 JSON 前后加解释文字。

组装处在 [pdf_craft/llm/guaranteed.py:90-91](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L90-L91)：`max_attempts=options.max_retries + 1`——再次印证「重试次数是额外的」。

**升温机制**。配置归一化与升温器在 [pdf_craft/llm/increasable.py:1-37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/increasable.py#L1-L37)：

```python
class Increaser:
    def increase(self):
        if self._value_range is not None and self._current is not None:
            _, end_value = self._value_range
            self._current = self._current + 0.5 * (end_value - self._current)   # 走剩余距离的一半

class Increasable:
    def __init__(self, param):          # 标量→(x,x)；二元组→区间；其他长度→ValueError
        ...
    def context(self) -> Increaser:     # 每个作用域一个独立升温器
        return Increaser(self._value_range)
```

运行时在构造时把 `top_p` 与 `temperature` 配置各包成一个 `Increasable`（[pdf_craft/llm/runtime.py:46](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L46)），请求前用三级调度取值（[pdf_craft/llm/runtime.py:62-70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L62-L70)）：显式传参 > 按 `retry_index` 线性插值 > 区间起点。上下文正常退出时还会调用 `increase()` 推进上下文本地升温器（[pdf_craft/llm/runtime.py:147-149](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L147-L149)）。细心的读者会发现：`_scheduled` 的兜底分支每次新建 `Increaser`（取区间起点），上下文内累计的升温并未参与取值——修复循环中真正生效的升温走的是 `retry_index` 线性插值路径，这也是两个消费者都认真传递 `retry_index`/`retry_max` 的原因。

**消费者一：目录层级分析**。[pdf_craft/extractor/toc/llm_analyser.py:590-602](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L590-L602) 用 `request_guaranteed_json` 请求目录层级：schema 用一个「包裹任意 JSON 对象」的传输层模型，真正的 RESULT/ID 语义校验放在 `parse` 回调里（业务校验层）；`max_retries=_MAX_RETRIES - 1`（`_MAX_RETRIES = 3`，即总请求 3 次）；request 回调接入 `LLMRuntime` 并传递轮次下标、关闭缓存。外层把一切异常包装成 `LLMAnalysisError`——正是 u4-l2 讲过的「LLM 分析失败自动回退统计法」的入口。

**消费者二：XML 回填**。[pdf_craft/transformer/xml_translator/xml_translator/translator.py:221-227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L221-L227) 不走 guaranteed（要的不是 JSON），直接手写 `_XMLProtocol` 复用循环；request 回调同样传 `retry_index=index, retry_max=maximum, use_cache=False`。两个消费者都显式 `use_cache=False`——重试的消息与首轮几乎相同，若命中缓存会原样重放坏答案，「升温重采样」就失效了（缓存键虽含 temperature，但语义修复的重试本就不该走缓存）。

两个消费者的对照恰好说明分层价值：**要 JSON 找 guaranteed，要别的格式手写协议接 `run_repair_loop`，要升温传 `retry_index`**。

#### 4.3.4 代码实践

**实践目标**：实现一个校验 JSON 的 `ResponseProtocol`（借鉴 json-repair 思路），并写单元测试验证「第二次请求确实包含反馈消息」——这是规格指定的本讲主实践。

**操作步骤**：

1. 在仓库根目录新建 `practice_json_protocol.py`（示例代码，不修改任何源码）：

```python
# 示例代码：一个最小 JSON 修复协议 + 单元测试
import unittest
from json_repair import repair_json
from pdf_craft.llm import (Message, MessageRole, ProtocolFailure, ProtocolRetry,
                           ProtocolSuccess, RepairLoopOptions, run_repair_loop)


class JsonSyntaxError(Exception):
    pass


class JsonProtocol:
    """校验 LLM 回答是否为合法 JSON；非法时返回 ProtocolRetry 并附带错误反馈。"""

    def validate(self, response, state, attempt, max_attempts):
        try:
            repaired = repair_json(response)          # json-repair 思路：先修小毛病
            import json
            value = json.loads(repaired)              # 仍失败说明坏得离谱
        except Exception as error:
            return ProtocolRetry(
                f"Your response is not valid JSON ({error}). "
                "Return complete valid JSON only, no explanations.",
                state,
            )
        return ProtocolSuccess(value, state)

    def empty(self, state, attempt, max_attempts):
        return ProtocolRetry("Response is empty. Return a JSON object.", state)

    def exhausted(self, state, attempts, response):
        raise JsonSyntaxError(f"no valid JSON after {attempts} attempts, last={response!r}")


class TestJsonProtocol(unittest.TestCase):
    def test_second_request_contains_feedback(self):
        calls = []

        def fake_request(messages, index, maximum):
            calls.append(list(messages))
            # 首轮给一段完全不含 JSON 结构的自然语言，保证修复后依然解析失败
            return "好的，翻译如下：这是一段纯文本回答" if index == 0 else '{"a": 1}'

        result = run_repair_loop(RepairLoopOptions(
            messages=[Message(MessageRole.USER, "把上一段翻译成 JSON")],
            request=fake_request,
            protocol=JsonProtocol(),
            state=None,
            max_attempts=3,
        ))

        self.assertEqual(result, {"a": 1})            # 第二轮成功
        self.assertEqual(len(calls), 2)               # 恰好请求两次
        second = calls[1]
        self.assertEqual(second[0].message, "把上一段翻译成 JSON")   # 初始消息保留
        self.assertIs(second[1].role, MessageRole.ASSISTANT)         # 坏答案被带回
        self.assertIn("not valid JSON", second[2].message)           # 反馈消息在场
        self.assertIs(second[2].role, MessageRole.USER)


if __name__ == "__main__":
    unittest.main()
```

2. 运行：`python -m unittest practice_json_protocol -v`（或 `python practice_json_protocol.py`）。

**需要观察的现象**：测试是否通过；`calls[1]` 的三条消息分别是初始任务、assistant 的坏答案、user 的错误反馈。

**预期结果**：测试通过。第一轮返回纯文本，`repair_json` 找不到任何可修复的 JSON 结构（通常返回空串），`json.loads` 抛错，协议返回 `ProtocolRetry`；循环按 loop.py L81-L86 构造 `current = 初始 + [ASSISTANT(坏答案), USER(反馈)]` 发起第二轮；第二轮返回合法 JSON，`ProtocolSuccess` 立即返回 `{"a": 1}`。（推演依据源码逻辑，待本地验证；若你安装的 json-repair 版本对纯文本的行为不同，把 `repair_json(...)` 换成直接 `json.loads(response)` 同样能触发重试路径。）

3. 进阶验证耗尽路径：把 `fake_request` 改成永远返回坏 JSON，断言 `JsonSyntaxError` 被抛出且消息里的 `attempts` 等于 3（`assertRaisesRegex(JsonSyntaxError, "3 attempts")`）。

**预期结果**：三轮全败后 `exhausted` 抛出 `JsonSyntaxError`——这正是 guaranteed 层「耗尽即抛带上下文异常」的微缩版。

#### 4.3.5 小练习与答案

**练习 1**：`GuaranteedOptions(max_retries=0)` 时，一个语法全错的回答会触发几次请求？一个像拒绝的回答呢？

**参考答案**：语法全错：1 次请求后耗尽——`max_attempts = 0 + 1 = 1`，首轮 `Retry` 后 `attempt + 1 >= attempts` 直接 break 进 `exhausted` 抛 `GuaranteedExhaustedError`。像拒绝的回答：同样 1 次即失败——拒绝快败门条件是 `attempt >= min(1, max_retries) = 0`，首轮就满足，立即 `ProtocolFailure(GuaranteedProtocolError)`。

**练习 2**：LLM 配置里 `temperature=0.7` 与 `temperature=(0.7, 1.2)` 在修复循环中行为有何不同？

**参考答案**：`Increasable` 把标量归一化为 `(0.7, 0.7)`，线性插值 \( t_k = 0.7 + 0 \cdot k/m \) 恒等于 0.7——每轮同样的采样温度。区间 `(0.7, 1.2)` 则让温度从 0.7 线性爬升到 1.2：轮次越靠后越「放飞」，把模型推出重复失败的模式。这就是配置文档建议翻译任务给温度区间的底层原因。

**练习 3**：TOC 分析消费者为什么把「RESULT/ID 语义校验」放进 `parse` 回调而不是 pydantic `schema`？

**参考答案**：分层校验的粒度不同。pydantic schema 只能表达「结构性」约束（是对象、字段类型对），反馈给模型的是逐字段问题清单；而 TOC 的业务规则（如页码范围、ID 语义）需要访问载荷上下文做过程式判断，放进 `parse` 抛异常即转为 `ProtocolRetry`，异常原文直接成为反馈。两级反馈各司其职：结构错给精确字段清单，业务错给规则描述。

## 5. 综合实践

把 4.3.4 的协议升级为一个「迷你 guaranteed 层」，串联本讲全部三个模块（协议判定、循环历史、保证式封装）：

1. **扩展协议**：在 4.3.4 的 `JsonProtocol` 基础上增加两个行为——(a) 借鉴 [guaranteed.py:100-101](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L100-L101) 的思路实现 `_looks_like_refusal`（命中「抱歉/无法/sorry」且无括号即拒绝），从第二轮起返回 `ProtocolFailure`；(b) 增加一个 pydantic schema（如要求 `{"items": [...]}` 结构），失败时返回逐字段反馈（参考 [guaranteed.py:108-109](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L108-L109)）。
2. **写三个测试**（参考 [tests/test_llm_loop.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_loop.py) 的假 request 风格）：
   - 首轮语法错、次轮结构错、第三轮合格 → 断言恰好 3 次请求，且第三轮消息里能找到第二轮的**结构**反馈（区别于第一轮的**语法**反馈）。
   - 连续两轮拒绝文本 → 断言第 2 次请求后抛出你的拒绝异常（快败，不该有第 3 次）。
   - 永远坏 → 断言耗尽异常携带 `attempts` 与最后响应文本。
3. **对照复盘**：运行通过后打开 [pdf_craft/llm/guaranteed.py:57-91](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/guaranteed.py#L57-L91)，列出你的实现与官方的至少三处差异（提示：`last_error` 记账、`extractor` 注入点、`max_retries` 与 `max_attempts` 的换算、拒绝快败门条件）。

**预期结果**：三个测试全绿；能够解释「为什么 guaranteed 把拒绝判定放在语法失败的分支内部而不是独立的预先检查」（答案：拒绝文本通常也通不过 JSON 解析，寄生在语法失败分支可以省一次独立扫描，且用 `attempt` 门条件避免首轮误杀）。

## 6. 本讲小结

- 修复循环与传输重试是两层机制：传输层原样重发（`LLMContext` 内），语义层带反馈改写对话（`run_repair_loop`）；传输异常穿透循环不被吞掉。
- `ResponseProtocol` 把「回答是否合格」的判定权外包为三个方法（`validate`/`empty`/`exhausted`），四种判定结果中 `Success`/`Partial` 立即返回值、`Failure` 立即抛错、`Retry` 携带反馈进入下一轮。
- 反馈历史是滚动窗口：默认每轮追加 `[assistant 坏答案, user 反馈]` 两条并清空前史，`history_limit` 按**消息条数**裁剪（默认 2 恰好一轮）；初始消息永不裁剪；`include_response`/`reset_history` 两个开关控制上下文策略。
- `max_attempts` 是总请求次数（guaranteed 的 `max_retries` 是额外次数，换算 `+1`）；轮次下标一路传给 `LLMContext.request(retry_index=...)`，驱动 temperature 沿配置区间线性升温，且修复请求一律 `use_cache=False` 防止缓存重放坏答案。
- `request_guaranteed_json` 是三级漏斗（json-repair 语法 → pydantic 结构 → 业务回调），每级失败产生更具体的反馈；对「像拒绝」的回答从第二轮起快败；耗尽时抛出携带 `attempts`/`response`/`cause` 的分类异常。
- 两类真实消费者代表两种兜底哲学：TOC 分析耗尽抛 `LLMAnalysisError`（调用方回退统计法），XML 回填耗尽返回 `None` 并发 `FillFailedEvent`（调用方降级保留原文，不中断全书）。

## 7. 下一步学习建议

本讲补齐了 LLM 基础设施的最后一块拼图。接下来建议：

1. **顺着消费者往下读**：带着本讲的词汇表重读 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:194-227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L194-L227)（XML 回填协议如何与 u7-l5 的爬山修复协作）和 [pdf_craft/extractor/toc/llm_analyser.py:560-606](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L560-L606)（TOC 分析的 guaranteed 用法），体会「同一循环、两种兜底」的工程取舍。
2. **进入 u9（EPUB 翻译管线）**：看 `translate()` 主流程如何把本章的 XMLTranslator（其回填侧正是本讲的 `_XMLProtocol`）编排进整本 EPUB 的翻译，以及进度权重与并发如何组织。
3. **动手方向**：如果你有自己的结构化输出场景（如配置抽取、元数据生成），试着像 4.3.4 那样写一个专用协议接入 `run_repair_loop`，并接入带温度区间的 `LLM` 配置观察升温重采样的效果——这是检验本讲掌握程度的最好方式。
