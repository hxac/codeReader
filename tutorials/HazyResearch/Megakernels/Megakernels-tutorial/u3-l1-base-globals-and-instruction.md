# BaseGlobals 与 Instruction 序列化

> 本讲对应手册单元 U3·L1，承接 [U2·L2]（调度器如何把一串指令"拉平"）。建议你已经知道：Megakernels 把一次前向编译成一串**指令**，并把所有权重、缓冲、常量收进一个叫 `globs` 的全局对象。本讲就钻进 `instructions.py`，看清"`globs` 里到底装了什么字段"以及"一条指令是怎么被打成一串 int 的"。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **`BaseGlobals` 这个 dataclass 把哪些东西收在了一起**：逐层堆叠的权重、KV cache、非逐层的 RoPE 表、模型超参常量、标量超参（`attn_scale` / `rms_norm_eps` / `device`）、运行时缓冲（`hidden_states` / `barriers`）、以及位置指针 `pos_id`；并能解释 `__post_init__` 里多出来的 `instructions` / `timings` 两个字段是干嘛的。
2. **手算 `Instruction.serialize()` 的输出**：给定一个指令子类，按 opcode → 各字段（int / tuple / list / None）的编码规则，写出它会被序列化成哪一串 int。
3. 理解 **`NoOp` 与 `PrintState` 两条"特殊"指令**的定位：`NoOp(opcode=0)` 是用来填空的占位指令；`PrintState` 携带 `str` 和 `PrintInfo` 对象字段，是给**宿主机侧（Python）**用的调试指令，不走标准整型序列化。

## 2. 前置知识

- **dataclass**：Python 的 `@dataclass` 装饰器会根据你写的类型注解，自动生成 `__init__` 等方法。更重要的是，`dataclasses.fields(obj)` 能**按声明顺序**返回对象的所有字段。`serialize()` 能工作，全靠"字段有确定的声明顺序"这一点。
- **globals（全局状态对象）**：Megakernels 里的一次前向计算，需要"全部权重 + 全部激活缓冲 + 模型常量 + 本步要执行的指令队列"都放在一处。这个"一处"就是 `globs`（类型是 `BaseGlobals` 的某个子类）。运行时把整个 `globs` 一次性喂给 megakernel。
- **指令（instruction）**：一次前向被编译成一串指令，每条指令描述"在哪些层、对哪些块做某种计算"。指令最终要被打包成一串 **int**，装进一个 `[num_sms, 步数, 32]` 的张量（`INTS_PER_INSTRUCTION = 32`），交给 GPU 上的解释器逐条读取。
- **opcode（操作码）**：每条指令的第一个 int 就是它的操作码，用来告诉解释器"这是哪一种操作"。类比 CPU 机器码的操作码。

如果上面某几个词还陌生，记住一句话即可：**`globs` 是"全局背包"，`serialize()` 是"把一条指令压平成一串整数"的打包器**。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | 本讲主角：`BaseGlobals`（全局背包）、`Instruction`（序列化基类）、`NoOp` / `PrintState` |
| [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) | `serialize()` 的**消费者**：`serialize_and_pad` 把每条指令补齐到 32 个 int，`tensorize_instructions` 再拼成张量塞回 `globs.instructions` |
| [megakernels/model_types.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/model_types.py) | `DeviceType = torch.device | str` 类型别名，`device` 字段的类型来源 |
| [megakernels/demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) | 真实的 `Instruction` 子类样例（`opcode()` / `prev_opcode()` / 字段如何声明），用来对照本讲的设计 |

## 4. 核心概念与源码讲解

### 4.1 BaseGlobals：把"一次前向需要的全部家当"装进一个 dataclass

#### 4.1.1 概念说明

`BaseGlobals` 是所有 `globs` 对象的**基类**（注意是 base——各个 demo 会再 `class Globals(BaseGlobals)` 往里加自己的激活缓冲和 block size 常量）。它的职责只有一件事：**用一个 dataclass，把"跑一次前向"必须攥在手里的所有张量、常量、缓冲，按固定顺序列清楚**。

之所以要全部塞进一个对象，是因为 megakernel 的执行模型是"**一次性把整个 `globs` 交给 GPU**"。字段声明顺序在 `serialize()` 里也会复用（见 4.2），所以这里的顺序不只是好看，而是**有语义的**。

#### 4.1.2 核心流程：BaseGlobals 字段的五个分组

`BaseGlobals` 的字段可以分成五组，理解时按组记忆即可：

| 组别 | 字段 | 含义 |
| --- | --- | --- |
| ① 逐层堆叠的权重 | `qkv_proj_weights`、`attn_ln_weights`、`o_proj_weights`、`mlp_ln_weights`、`up_proj_weights`、`gate_proj_weights`、`down_proj_weights`、`lm_head_norm_weights`、`lm_head_weights` | 注释写明 "all layers stacked together in order"：所有层的权重沿第 0 维堆在一起，形状形如 `[num_layers, ...]` |
| ② KV cache | `k_cache`、`v_cache` | 推理时存放历史 K/V 的缓存 |
| ③ 非逐层的 RoPE 表 | `rope_cos`、`rope_sin` | 注释 "not stacked for each layer"：RoPE 的 cos/sin 预计算表与层数无关，只存一份 |
| ④ 模型常量（int） | `num_hidden_layers`、`num_attention_heads`、`num_kv_heads`、`head_dim`、`hidden_size`、`intermediate_size`、`vocab_size` | 模型结构超参，纯整数 |
| ⑤ 标量超参 + 缓冲 + 位置 | `attn_scale: float`、`rms_norm_eps: float`、`device: DeviceType`；`hidden_states: Tensor`、`barriers: Tensor`；`pos_id: int` | 注意力缩放、RMSNorm epsilon、设备；当前隐状态缓冲、跨 SM 同步用的 barrier；当前生成的位置序号 |

> 小提醒：`device` 不是张量，而是 `DeviceType`（即 `torch.device | str`），它不会进指令缓冲，只用来在主机侧查询 GPU 属性。

此外，`__post_init__` 会额外挂上两个字段，它们**不在 dataclass 字段声明里**：

```
__post_init__ 执行后：
  self.instructions : Tensor | None = None   # 本步的指令张量，由 scheduler 稍后填入
  self.timings      : Tensor | None = None   # 本步每条指令的计时槽，由 scheduler 稍后填入
```

这一点很关键：`serialize()` 用 `fields(self)` 遍历字段时，**看不到** `instructions` 和 `timings`（因为它们是 `__post_init__` 里 `self.x = ...` 普通赋值，不是声明的 dataclass 字段）。所以它们不会被序列化——这也符合直觉：指令张量本身不该再被"打包成一条指令"。

#### 4.1.3 源码精读

`BaseGlobals` 类定义与权重字段（逐层堆叠）：

[megakernels/instructions.py:10-23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L23) —— 第 10–11 行是 `@dataclass class BaseGlobals`；第 12–23 行是第 ①② 组：九个权重张量 + `k_cache`/`v_cache`，注释明确"all layers stacked together in order"。

RoPE 表与模型常量：

[megakernels/instructions.py:25-36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L25-L36) —— 第 26–27 行 `rope_cos`/`rope_sin`（第 ③ 组，注释"not stacked"）；第 29–36 行七个 int 常量（第 ④ 组）。

标量、缓冲、位置与 `__post_init__`：

[megakernels/instructions.py:38-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L38-L49) —— 第 ⑤ 组：`attn_scale: float`、`rms_norm_eps: float`、`device: DeviceType`、`hidden_states`、`barriers`、`pos_id: int`。`__post_init__`（第 47–49 行）挂上 `instructions`/`timings`。

两个便利方法：

[megakernels/instructions.py:51-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L51-L69) —— `sm_count()` 查 GPU 的 SM 数（转发给 `get_sm_count`）；`num_total_heads()` 返回 `num_attention_heads + num_kv_heads * 2`（注意力头 + 两倍 KV 头，因为 K 和 V 各算一份）。`diff(other, ...)` 用于和另一份 `globs` 逐张量比对数值差异（权重和 RoPE 表默认跳过）。

#### 4.1.4 代码实践

**目标**：亲手验证"`instructions` / `timings` 不是声明的字段，所以 `fields()` 看不到它们"——这正是它们不参与序列化的根本原因。

操作步骤（这是源码阅读型实践，可直接在能 import 项目的 Python 里跑）：

```python
# 示例代码：验证 __post_init__ 挂的字段不在 fields() 里
from dataclasses import fields, dataclass
from torch import Tensor

# 用一个最小化的 BaseGlobals，省去全部真实字段，只保留一个占位字段和 __post_init__
@dataclass
class TinyGlobals:
    pos_id: int

    def __post_init__(self):
        self.instructions = None
        self.timings = None

g = TinyGlobals(pos_id=0)
print([f.name for f in fields(g)])   # 预期: ['pos_id']
print(g.instructions, g.timings)      # 预期: None None
```

需要观察的现象：`fields(g)` 只列出 `['pos_id']`，**不包含** `instructions`/`timings`，尽管对象上确实能访问到这两个属性。

预期结果：列表只有 `pos_id`，但 `g.instructions`/`g.timings` 都是 `None`。这条性质决定了 4.2 里 `serialize()` 不会误把它们打包。

#### 4.1.5 小练习与答案

**练习 1**：`num_total_heads()` 为什么是 `num_attention_heads + num_kv_heads * 2`，而不是直接 `+ num_kv_heads`？
**答案**：注意力需要 Q/K/V 三种头。Q 用 `num_attention_heads` 个；而 K 和 V 各自使用 `num_kv_heads` 个（这是 GQA 里的分组配置）。K 一份、V 一份，所以 KV 部分是 `num_kv_heads * 2`。

**练习 2**：`diff(other, skip_kv_cache=True)` 会跳过哪些字段？
**答案**：跳过两类——(a) 名字含 `"weights"` 的，或名字是 `rope_cos`/`rope_sin` 的；(b) 当 `skip_kv_cache=True` 时，名字含 `"cache"` 的（即 `k_cache`/`v_cache`）。逻辑见 [instructions.py:59-64](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L59-L64)。

---

### 4.2 Instruction.serialize()：把一条指令压平成一串 int

#### 4.2.1 概念说明

`Instruction` 是所有指令的基类。它本身**不声明任何字段**，真正的字段由子类按需添加。它只规定两件事：

1. **每个子类要能报出自己的 `opcode()`**（操作码，一个 int）——基类里是 `raise NotImplementedError`，逼子类实现。
2. **所有子类共用同一个 `serialize()`**：把"操作码 + 各字段"按一套固定规则，压平成一个 `list[int]`。

为什么是 int 列表？因为最终这些指令要塞进一个 `int32` 张量（`globs.instructions`），交给 GPU 解释器读。所以序列化规则只支持"能变成 int"的东西：整数、整数组成的 tuple/list、以及 `None`（占位为 0）。

#### 4.2.2 核心流程：serialize 的编码规则

`serialize()` 的执行逻辑（伪代码）：

```
words = [opcode]                      # 第一个 int 永远是操作码
for 字段 in fields(self) 按声明顺序:
    if 字段名 == "global_idx": continue   # 预留字段，刻意跳过
    取字段值 attr:
        int   → words.append(attr)
        tuple → words.append(len(attr)); words.extend(attr)   # 先写长度，再逐个写
        list  → words.append(len(attr)); words.extend(attr)   # 同上
        None  → words.append(0)          # 注释 "for convenience"，给 Optional 字段兜底
        其它  → raise ValueError          # str / 对象 / Tensor 一律不支持
return words
```

四条编码规则记牢即可：

| 字段值类型 | 编码方式 | 举例（attr 值） | 写进 words 的内容 |
| --- | --- | --- | --- |
| `int` | 原样追加 | `3` | `3` |
| `tuple` | **先写长度，再逐个写** | `(10, 20)` | `2, 10, 20` |
| `list` | **先写长度，再逐个写** | `[1, 2, 3]` | `3, 1, 2, 3` |
| `None` | 写一个 `0` | `None` | `0` |

两个要点：

- **"先长度后元素"是自描述编码**：解释器读到一条指令时，知道"下一个 int 是这个变长字段的元素个数"，从而知道该读几个 int。tuple 和 list 走完全相同的编码。
- **`global_idx` 被刻意跳过**：这是一个预留的"运行时调度序号"字段名。`serialize()` 见到它就 `continue`，保证这个主机侧的簿记序号**不会**被写进指令缓冲。即使某个子类加了 `global_idx` 字段，它也不会改变序列化输出。

`serialize()` 之外，`Instruction` 还有两个类方法，**它们不参与序列化**，但值得知道：

- `prev_opcode()`：返回"本指令之前应当出现的那条指令的操作码"，用来在调度里表达**指令之间的顺序链**（例如低延迟 demo 里 `PartialAttention.prev_opcode()` 指向 `LayerNorm_QKV_MatVecRopeAppend.opcode()`）。
- `tags()`：返回一个元信息字典，默认 `{}`；调度器在 `"pool"` 模式下会读 `ins.tags()["pool"]`（见 [scheduler.py:227](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L227)）。

#### 4.2.3 源码精读

`Instruction` 基类与三个类方法：

[megakernels/instructions.py:83-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L83-L95) —— `opcode()`/`prev_opcode()` 都抛 `NotImplementedError`，`tags()` 默认返回 `{}`。

`serialize()` 主体：

[megakernels/instructions.py:97-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119) —— 第 98 行 `words = [self.opcode()]`；第 99–102 行遍历字段并跳过 `global_idx`；第 105–106 行处理 int；第 107–112 行对 tuple/list **先 append 长度再 extend 元素**；第 114–115 行把 `None` 写成 `0`（注释 `# for convenience`）；第 116–117 行对其它类型抛 `ValueError`；第 119 行返回 `words`。

序列化结果如何被消费（补齐到 32 个 int 并拼成张量）：

[megakernels/scheduler.py:274-309](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L274-L309) —— `serialize_and_pad`（第 274–278 行）调用 `serialize()` 后，用 `INTS_PER_INSTRUCTION - len(serialized)` 个 `0` 补齐到 **32**；`tensorize_instructions`（第 281–308 行）把所有 SM 的指令队列补齐到等长（第 289 行用 `NoOp()` 填空），逐条 `serialize_and_pad` 后拼平（第 293 行），再 `view(num_sms, -1, 32)` 成张量（第 297–299 行），最后写入 `globs.instructions`（第 307 行）。`INTS_PER_INSTRUCTION = 32` 定义在 [scheduler.py:13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L13)。

真实子类样例（对照设计）：

[megakernels/demos/latency/instructions.py:61-66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L61-L66) —— `PartialAttention` 有四个 `int` 字段（`layer_idx`/`kv_head_idx`/`num_partials`/`partial_idx`），`opcode()` 返回 `2`。它的 `serialize()` 输出就是 `[2, layer_idx, kv_head_idx, num_partials, partial_idx]`，共 5 个 int。

#### 4.2.4 代码实践

**目标**：为一个假想 op 写 `Instruction` 子类，**手算**它的 `serialize()` 输出，再用真实代码核对。

操作步骤（可在能 import 项目的环境里跑；这是可运行示例）：

```python
# 示例代码：自定义一条带 layer_idx（int）和 block_range（tuple）的指令
from dataclasses import dataclass
from megakernels.instructions import Instruction

@dataclass
class MyOp(Instruction):
    layer_idx: int
    block_range: tuple

    @classmethod
    def opcode(cls) -> int:
        return 7

ins = MyOp(layer_idx=3, block_range=(10, 20))
print(ins.serialize())
```

**手算过程**（按 `serialize()` 规则一步步推）：

```
words = [opcode] = [7]
字段 layer_idx = 3 (int)            → append 3       → [7, 3]
字段 block_range = (10, 20) (tuple) → append len=2   → [7, 3, 2]
                                    → extend 10, 20  → [7, 3, 2, 10, 20]
返回 [7, 3, 2, 10, 20]
```

需要观察的现象 / 预期结果：程序输出正是 `[7, 3, 2, 10, 20]`。

再补一步——它被送进 `serialize_and_pad` 后会变成多少个 int？`len([7,3,2,10,20]) = 5`，`INTS_PER_INSTRUCTION = 32`，所以补 `32 - 5 = 27` 个 `0`，最终是 `[7, 3, 2, 10, 20] + [0]*27`。你可以用下面这句核对补齐长度：

```python
# 示例代码：模拟 serialize_and_pad 的补齐
INTS_PER_INSTRUCTION = 32
serialized = ins.serialize()
print(serialized + [0] * (INTS_PER_INSTRUCTION - len(serialized)))
```

预期结果：长度为 32 的列表，前 5 个是 `[7, 3, 2, 10, 20]`，其余为 `0`。**待本地验证**：若你的环境暂不能 import `megakernels`，可直接套用手算结果理解。

#### 4.2.5 小练习与答案

**练习 1**：若把 `MyOp` 多加一个字段 `reduction_list: list = None`，实例化时该字段用默认值 `None`，则 `MyOp(layer_idx=3, block_range=(10,20)).serialize()` 输出是什么？
**答案**：`[7, 3, 2, 10, 20, 0]`。前半不变；`reduction_list=None` 走 None 分支，追加一个 `0`。

**练习 2**：如果给 `MyOp` 加一个 `name: str` 字段并赋值 `"attn"`，调用 `serialize()` 会怎样？
**答案**：会抛 `ValueError(f"Unsupported field type: attn")`。`str` 不在 int/tuple/list/None 之内，命中第 116–117 行的 else 分支。这正是 `PrintState` 不能走标准序列化的原因（见 4.3）。

**练习 3**：某子类声明了字段 `global_idx: int = 0` 并赋值 42，`serialize()` 输出里会包含 42 吗？
**答案**：不会。第 101–102 行对 `name == "global_idx"` 直接 `continue`，该字段被刻意跳过。

---

### 4.3 NoOp 与 PrintState：两条定位特殊的指令

#### 4.3.1 概念说明

绝大多数指令子类是"真正的计算指令"（layernorm、matvec、attention reduction……）。但有两条例外，理解它们的区别，能帮你把"`serialize()` 适用边界"看透：

- **`NoOp`**：空操作，`opcode()` 返回 `0`，**不声明任何字段**。它是"填充剂"——不同 SM 的指令队列长度不一，调度器会用 `NoOp()` 把短队列补齐（见 4.2.3 引用的 `tensorize_instructions` 第 289 行）。`NoOp().serialize()` 恒等于 `[0]`。
- **`PrintState`**：调试指令，携带 `layer_idx: int`、`name: str` 和一个 `print_info: PrintInfo` 对象。它的字段里有 `str` 和**自定义对象**，`serialize()` 完全不支持这两类（会 `ValueError`）。所以 `PrintState` 不是设计来"压平进整型指令缓冲"的，而是给**宿主机侧（Python，例如 `pyvm`）**消费的调试钩子——在解释器遇到它时，按 `name` / `print_info` 打印中间状态。

#### 4.3.2 核心流程

```
NoOp:
  opcode() = 0
  无字段 → serialize() = [0]            # 可安全补齐、占位

PrintState:
  字段: layer_idx:int, name:str, print_info:PrintInfo
  → 含 str 与对象 → serialize() 会抛 ValueError
  → 不进入整型指令缓冲；仅在 Python 宿主侧解释器中被识别和处理
```

`PrintInfo` 本身也是一个 dataclass，三个字段都是 `list | None = None`，用来做"过滤"：只在满足条件的层/名字/状态上打印。

#### 4.3.3 源码精读

`NoOp`：

[megakernels/instructions.py:122-126](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L122-L126) —— `@dataclass class NoOp(Instruction)`，只重写 `opcode()` 返回 `0`，没有自己的字段，所以 `serialize()` 走到 `words=[0]` 后字段循环为空，直接返回 `[0]`。

`PrintInfo` 与 `PrintState`：

[megakernels/instructions.py:129-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L129-L140) —— `PrintInfo`（第 129–133 行）三个过滤字段 `layer_filter`/`name_filter`/`state_filter`；`PrintState`（第 136–140 行）字段为 `layer_idx: int`、`name: str`、`print_info: PrintInfo`。注意它**没有**重写 `opcode()`（继承基类的 `NotImplementedError`），也印证了它不走标准序列化路径。

`NoOp` 被用来补齐队列的实际调用点：

[megakernels/scheduler.py:287-289](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L287-L289) —— 先求最长队列 `max_queue_len`，再把每个队列 `[NoOp()] * (max_queue_len - len(queue))` 补齐。

#### 4.3.4 代码实践

**目标**：用真实代码确认"`NoOp` 序列化是 `[0]`"以及"`PrintState` 无法标准序列化"。

操作步骤（可运行示例）：

```python
# 示例代码：观察 NoOp 与 PrintState 的序列化行为
from megakernels.instructions import NoOp, PrintState, PrintInfo

print(NoOp().serialize())   # 预期: [0]

ps = PrintState(layer_idx=0, name="attn", print_info=PrintInfo())
try:
    ps.serialize()
except ValueError as e:
    print("ValueError:", e)   # 预期: Unsupported field type: attn
```

需要观察的现象 / 预期结果：`NoOp().serialize()` 输出 `[0]`；`PrintState(...).serialize()` 抛出 `ValueError`，提示不支持 `str` 类型。**待本地验证**：若无法 import，按上面的规则推演即可得出相同结论——`name="attn"` 是 `str`，命中 else 分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么调度器敢放心用 `NoOp()` 去补齐任意一条指令队列？
**答案**：`NoOp` 无字段、`opcode=0`，`serialize()` 恒为 `[0]`，补齐到 32 后就是一串 `0`，GPU 解释器读到 opcode `0` 即"什么都不做"。它对计算结果无副作用，是天然的填充剂。

**练习 2**：如果想让 `PrintState` 也能进整型指令缓冲，从 `serialize()` 的支持类型看，它的哪个字段是"硬伤"？
**答案**：`name: str`（和 `print_info: PrintInfo` 对象）都是硬伤。`serialize()` 只认 int/tuple/list/None；`str` 和自定义对象都会触发 `ValueError`。要让它能序列化，至少得把 `name` 改成 int 编号、把 `print_info` 拆成基本类型字段。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个"纸笔 + 代码"小任务。

**任务**：假设你要新增一条假想指令 `GatherOp`，需求如下：

- 操作码为 `9`；
- 字段（按声明顺序）：`layer_idx: int`、`head_range: tuple`、`block_list: list`、`partial_idx: int = None`。

请完成：

1. **写出 `GatherOp` 的 dataclass 定义**（含 `opcode()`）。
2. **手算** `GatherOp(layer_idx=4, head_range=(0, 4), block_list=[1, 3, 5], partial_idx=None).serialize()` 的输出。
3. **判断**：它被 `serialize_and_pad` 后会补几个 `0`？最终长度是多少？
4. **对照运行**：把定义贴进能 import `megakernels` 的环境，`print(...serialize())` 核对你的手算。

**参考答案**：

1. 定义（示例代码）：
   ```python
   from dataclasses import dataclass
   from megakernels.instructions import Instruction

   @dataclass
   class GatherOp(Instruction):
       layer_idx: int
       head_range: tuple
       block_list: list
       partial_idx: int = None

       @classmethod
       def opcode(cls) -> int:
           return 9
   ```

2. 手算：
   ```
   words = [9]
   layer_idx=4 (int)              → [9, 4]
   head_range=(0,4) (tuple)       → 追加 len=2, extend 0,4 → [9, 4, 2, 0, 4]
   block_list=[1,3,5] (list)      → 追加 len=3, extend 1,3,5 → [9, 4, 2, 0, 4, 3, 1, 3, 5]
   partial_idx=None (None)        → 追加 0 → [9, 4, 2, 0, 4, 3, 1, 3, 5, 0]
   ```
   输出：`[9, 4, 2, 0, 4, 3, 1, 3, 5, 0]`（共 10 个 int）。

3. 补齐：`32 - 10 = 22` 个 `0`，补齐后总长 32。

4. 程序应打印 `[9, 4, 2, 0, 4, 3, 1, 3, 5, 0]`。**待本地验证**。

> 这个任务同时覆盖了三个最小模块：`BaseGlobals` 的 dataclass 思想（字段按声明顺序、`fields()` 按序遍历）、`Instruction.serialize()` 的 int/tuple/list/None 四条编码规则、以及"`None` 用 `0` 占位"这条在 `partial_idx` 上的体现。

## 6. 本讲小结

- `BaseGlobals` 用一个 dataclass 把一次前向所需的全部家当分类收好：逐层堆叠权重、KV cache、RoPE 表、int 模型常量、标量超参（`attn_scale`/`rms_norm_eps`/`device`）、运行时缓冲（`hidden_states`/`barriers`）与 `pos_id`。
- `__post_init__` 挂上的 `instructions`/`timings` **不是声明的 dataclass 字段**，所以 `fields()` 看不到它们，它们也自然不参与序列化。
- `Instruction.serialize()` 的输出 = `[opcode] + 各字段按声明顺序编码`；编码规则只有四条：int 原样、tuple/list 先写长度再写元素、None 写 `0`、其它类型抛 `ValueError`。
- `global_idx` 是被 `serialize()` **刻意跳过**的预留字段名；`prev_opcode()`/`tags()` 是元信息，不进序列化。
- `NoOp`（`opcode=0`、无字段）是补齐队列用的填充剂，`serialize()` 恒为 `[0]`；`PrintState` 因携带 `str`/对象字段**无法**标准序列化，是给宿主机侧调试用的钩子。
- 序列化结果经 `serialize_and_pad` 补齐到 `INTS_PER_INSTRUCTION = 32`，再由 `tensorize_instructions` 拼成 `[num_sms, -1, 32]` 的张量写入 `globs.instructions`。

## 7. 下一步学习建议

- **顺着消费者继续读**：本讲停在"`globs.instructions` 被填好"。下一步建议读 [scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) 里**如何把模型的一层编译成这些指令**（各种 `Instruction` 子类的构造、`prev_opcode()` 形成的顺序链），把"序列化"和"生成"两头连起来。
- **读真实子类**：[demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) 与 [demos/throughput/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py) 里能看到 `opcode()`/`prev_opcode()`/`cost()`/`tags()` 的完整用法，以及 `Optional[int]` 字段在序列化时如何靠 `None→0` 兜底。
- **看解释器如何反序列化**：当指令缓冲送进 `pyvm`（[python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py)）或 `mk` 解释器时，它们如何按 opcode 分派、按"先长度后元素"的规则读回字段——这正是 `serialize()` 编码规则的对称面。
