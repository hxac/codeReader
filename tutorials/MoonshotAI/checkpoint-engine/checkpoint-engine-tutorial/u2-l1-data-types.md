# data_types.py：贯穿全项目的核心数据模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ParameterMeta`、`H2DBucket`、`MemoryBuffer(Metas)`、`DataToGather` 各自描述什么、分别被谁生产、被谁消费。
2. 理解 `_TorchDtype`、`_TorchSize`、`_TorchTensor` 三个 pydantic `Annotated` 类型是如何让 `torch.dtype`、`torch.Size`、`torch.Tensor` 这些"不可原生 JSON 化"的对象顺利通过校验与序列化的。
3. 知道这套模型如何支撑两条出口：HTTP API 的 `/v1/metas` 端点，以及 `examples/update.py` 的 metas 导出/导入（join 复用模式）。
4. 能在纯 CPU 环境下用 `TypeAdapter` 手工构造、序列化、反序列化这些模型。

## 2. 前置知识

### 2.1 为什么需要"元数据"这套数据模型

回顾上一讲（u1-l4）的三阶段数据流：权重被放进一块**扁平的字节缓冲区**里传输，worker 拿到的是一整块 buffer，再按"每个张量在 buffer 里的偏移量"把张量一个个切出来。既然传输的是裸字节，就必须有一份**清单**说明：

- 每个参数叫什么名字、是什么 dtype、什么 shape；
- 它在缓冲区的哪个位置、占多少字节。

这份清单就是本讲的主角——元数据（metas）。它是 PS 与 worker 之间的"共同语言"：PS 按清单打包，worker 按清单拆包。

### 2.2 pydantic 是什么

[pydantic](https://docs.pydantic.dev/) 是 Python 最常用的数据校验库。你继承 `BaseModel` 声明字段类型，它就自动完成三件事：

1. **校验（validate）**：传入的数据类型不对就报错，例如字段声明为 `int` 却传了字符串。
2. **序列化（serialize）**：把 Python 对象转成 JSON。
3. **反序列化**：把 JSON 转回校验过的 Python 对象。

本讲会用到三个 pydantic 工具（都在 [data_types.py:4](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L4) 导入）：

| 工具 | 作用 |
|---|---|
| `PlainValidator(func)` | 用自定义函数 `func` 做校验/转换，完全接管 pydantic 默认行为 |
| `PlainSerializer(func)` | 用自定义函数 `func` 做序列化输出 |
| `WithJsonSchema(...)` | 为该类型声明 JSON Schema（告诉外界它长什么样） |

另一个概念是 `Annotated[T, ...]`：Python 的原生语法，把一串"注解"挂到类型 `T` 上，pydantic 读取这些注解来定制校验与序列化行为。本讲的 `_TorchDtype` 等就是 `Annotated` 出来的"定制类型"。

### 2.3 为什么要定制：torch 对象不懂 JSON

- `torch.float16` 是 `torch.dtype` 实例，不是 Python 基本类型，JSON 里没有对应表示。
- `torch.Size([2, 3])` 本质是 tuple 的子类，JSON 只有数组（list），直接序列化会失败或形状不符。
- `torch.Tensor` 更复杂：既不能 JSON 化，也不该被随意深拷贝。

所以 data_types.py 的前半部分就是三组"校验器 + 序列化器"，把这些 torch 对象接到 pydantic 的世界里。

### 2.4 NamedTuple 与 TypedDict

- `NamedTuple`：带字段名的元组。轻量、不可变、可解包，适合纯内部使用的小结构。
- `TypedDict`：带类型提示的 dict。注意它**不做任何运行时校验**，只是给类型检查器看的"dict 形状说明书"。

项目里 `BucketRange` 用 `NamedTuple`，worker 侧的 `FlattenedTensorMetadata` 用 `TypedDict`——这个差异后面会解释。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [checkpoint_engine/data_types.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py) | 全部核心数据模型，仅 111 行 | 三个自定义校验器 + 七个模型类 |
| [checkpoint_engine/__init__.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py) | 包门面，声明公共 API | 哪些模型被导出为公共 API |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | ParameterServer 主链路 | 模型的生产端：`gather_metas`、`_gen_h2d_buckets` |
| [checkpoint_engine/pin_memory.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py) | checkpoint 加载与锁页 | `ParameterMeta` 的诞生地、`aligned_size` 的计算 |
| [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py) | HTTP API | `/v1/metas` 端点如何用这些模型自动校验 |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | 推理引擎侧消费端 | 对照 `FlattenedTensorMetadata` 与 `ParameterMeta` 的关系 |
| [tests/test_api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py) | CPU 可跑的 API 测试 | 本讲实践的依据 |

一句话概括：`pin_memory.py` 生产 `ParameterMeta` → `ps.py` 聚合成 `DataToGather` 并广播 → 广播回来的东西裁剪成 `MemoryBufferMetaList` 存起来 → 需要时切成 `H2DBucket` 用于传输，或经 `api.py`/`examples/update.py` JSON 化导出。

## 4. 核心概念与源码讲解

### 4.1 三个自定义类型：让 torch 对象通过 pydantic 这道门

#### 4.1.1 概念说明

pydantic 只认识 Python 内置类型。要让 `BaseModel` 的字段能装 `torch.dtype` / `torch.Size` / `torch.Tensor`，需要为每种类型定义两条规则：

- **进门（validate）**：什么输入算合法？进来后转成什么？
- **出门（serialize）**：输出 JSON 时变成什么？

`_TorchDtype` 和 `_TorchSize` 两条规则都齐全；`_TorchTensor` 只需要进门规则——张量永远只在进程内传递，从不进 JSON。

#### 4.1.2 核心流程

以 `_TorchDtype` 为例：

```
输入 "torch.float16" (str)
  └─ PlainValidator(_dt_validate)
       ├─ 是 str？→ 必须以 "torch." 开头，否则 ValueError
       ├─ getattr(torch, "float16") → torch.float16
       └─ 不是 torch.dtype 实例？→ TypeError
输出 torch.float16

序列化 torch.float16
  └─ PlainSerializer(str) → "torch.float16"
```

`_TorchSize` 的进门规则接受 `list | tuple | torch.Size` 三种输入，统一转成 `torch.Size`；出门时 `tuple(x)` 转成元组，JSON 里表现为整数数组。

#### 4.1.3 源码精读

dtype 校验器，先处理字符串再统一断言类型（[checkpoint_engine/data_types.py:22-32](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L22-L32)）：

```python
def _dt_validate(value: Any) -> torch.dtype:
    if isinstance(value, str):
        if not value.startswith("torch."):
            raise ValueError(f"dtype {value} should start with torch.")
        try:
            value = getattr(torch, value.split(".")[1])
        except AttributeError as e:
            raise ValueError(f"unknown dtype: {value}") from e
    if not isinstance(value, torch.dtype):
        raise TypeError(f"dtype {value} should be torch.dtype, got {type(value)}")
    return value
```

关键点：它**同时接受** `"torch.float16"`（字符串，来自 JSON）和 `torch.float16`（对象，来自 Python 代码）。这一步是"同一个模型既能被 Python 构造、又能被 JSON 反序列化"的前提。

把校验器和序列化器组装成可复用类型（[checkpoint_engine/data_types.py:35-40](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L35-L40)）：

```python
_TorchDtype = Annotated[
    torch.dtype,
    PlainValidator(_dt_validate),
    PlainSerializer(lambda x: str(x), return_type=str),
    WithJsonSchema({"type": "string"}, mode="serialization"),
]
```

`str(torch.float16)` 恰好等于 `"torch.float16"`，与校验器接受的输入格式形成闭环——序列化的输出可以直接被反序列化吃回去。

`_TorchSize` 的做法完全平行（[checkpoint_engine/data_types.py:43-56](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L43-L56)），注意 `isinstance(value, list | tuple)` 这个 Python 3.10+ 的联合类型写法：

```python
def _size_validate(value: Any) -> torch.Size:
    if isinstance(value, list | tuple):
        return torch.Size(value)
    if not isinstance(value, torch.Size):
        raise TypeError(f"size {value} should be torch.Size, got {type(value)}")
    return value
```

`_TorchTensor` 只有校验没有序列化，是"只进不出"的类型（[checkpoint_engine/data_types.py:59-68](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L59-L68)）：

```python
def _tensor_validate(value: Any) -> torch.Tensor:
    if isinstance(value, torch.Tensor):
        return value
    raise TypeError(f"tensor {value} should be torch.Tensor, got {type(value)}")
```

文件顶部还有一个只在类型检查时存在的 `FileMeta`（[checkpoint_engine/data_types.py:12-17](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L12-L17)），它描述 safetensors 文件头里每条记录的形状，运行时不会真正定义，仅作为 `pin_memory.py` 内部 dict 的类型说明书。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `_TorchDtype` 的"字符串 ⇄ 对象"双向转换。

**操作步骤**：在项目根目录执行 `python` 进入交互环境，依次输入：

```python
from checkpoint_engine.data_types import ParameterMeta
import torch

# 1) 用 torch 对象构造
m1 = ParameterMeta(name="w", dtype=torch.bfloat16, shape=torch.Size([4, 8]), aligned_size=64)
print(m1.dtype, type(m1.dtype))

# 2) 用 JSON 字符串构造（模拟从 HTTP 收到的请求体）
m2 = ParameterMeta.model_validate_json(
    '{"name":"w","dtype":"torch.bfloat16","shape":[4,8],"aligned_size":64}'
)
print(m2.dtype, type(m2.dtype))

# 3) 序列化回去
print(m1.model_dump_json())

# 4) 观察非法输入
ParameterMeta.model_validate_json(
    '{"name":"w","dtype":"fp16","shape":[4,8],"aligned_size":64}'
)
```

**需要观察的现象**：第 1、2 步打印的都是 `torch.bfloat16 <class 'torch.dtype'>`（说明字符串被校验器转成了真正的 dtype 对象）；第 3 步输出中 dtype 是 `"torch.bfloat16"`、shape 是 `[4,8]`；第 4 步抛出 `ValidationError`，错误信息包含 `dtype fp16 should start with torch.`。

**预期结果**：字符串进、对象存、字符串出，三者闭环。本实践纯 CPU 可运行（只依赖 torch 与 pydantic，不触碰 CUDA）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_dt_validate` 里 `value.split(".")[1]` 改成 `value.split(".")[0]`，会发生什么？

**答案**：`split(".")` 得到 `["torch", "float16"]`，`[0]` 是 `"torch"`，`getattr(torch, "torch")` 抛 `AttributeError`，被转成 `ValueError: unknown dtype: torch.float16`——所有合法 dtype 都会构造失败。（读源码即可推断，不需要改源码验证。）

**练习 2**：为什么 `_TorchTensor` 不需要 `PlainSerializer`？

**答案**：`_TorchTensor` 只被 `MemoryBuffer.buffer` 字段使用，而 `MemoryBuffer` 只在单进程内传递（PS 自己的 `_memory_pool`），永远不会走 JSON 序列化；真正跨进程的是它的**元数据**（`MemoryBufferMetas`，只含指针和大小）或 IPC 句柄，而不是张量本身。

### 4.2 ParameterMeta：单个参数的"身份证"

#### 4.2.1 概念说明

`ParameterMeta` 是整个体系的最小单元，描述**一个**参数（权重张量）的全部静态信息。它回答四个问题：叫什么（`name`）、什么精度（`dtype`）、什么形状（`shape`）、在扁平 buffer 里占多少字节（`aligned_size`）。

注意它**不包含** offset（偏移量）。偏移是相对的：同一份参数列表放到不同的 bucket 里，偏移会变。所以偏移在打包时动态计算，静态信息留在 meta 里。4.4 节的 `_to_named_tensor` 会展示这一点。

#### 4.2.2 核心流程

`aligned_size` 是"对齐后字节数"。项目把张量摊平进 uint8 缓冲区时，每个张量按 256 字节对齐（[checkpoint_engine/pin_memory.py:22-27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L22-L27)）：

\[ \text{aligned\_size} = \left\lceil \frac{\text{itemsize} \times \text{numel}}{256} \right\rceil \times 256 \]

代码里用整数运算实现上取整（加 255 再整除再乘回）：

```python
_ALIGN_SIZE = 256

def _align_size(dtype: torch.dtype, shape: torch.Size) -> int:
    return (dtype.itemsize * shape.numel() + _ALIGN_SIZE - 1) // _ALIGN_SIZE * _ALIGN_SIZE
```

例如一个 `[2, 3]` 的 float16 张量：\(2 \times 3 \times 2 = 12\) 字节，对齐后 `aligned_size = 256`。对齐的目的：让每个张量在 buffer 中的起点都是 256 的倍数，便于 H2D 拷贝和按块切桶（下一讲 u2-l2 会展开）。

#### 4.2.3 源码精读

模型本体只有四个字段（[checkpoint_engine/data_types.py:71-74](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L71-L74)）：

```python
class ParameterMeta(BaseModel):
    name: str
    dtype: _TorchDtype
    shape: _TorchSize
    aligned_size: int
```

`ParameterMeta` 的诞生地在 checkpoint 加载环节（[checkpoint_engine/pin_memory.py:158-163](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L158-L163)），从 safetensors 文件头读出的 meta 在这里被固化成模型实例：

```python
parameter_metas[parameter_name] = ParameterMeta(
    name=parameter_name,
    shape=meta["shape"],
    dtype=meta["dtype"],
    aligned_size=_align_size(meta["dtype"], meta["shape"]),
)
```

消费端的一个典型例子是 `_to_named_tensor`：把一串 meta 变成 worker 侧的"装载清单"，偏移量 `offset` 在这里被动态累加出来（[checkpoint_engine/ps.py:35-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48)）：

```python
def _to_named_tensor(metas: list[ParameterMeta], offset: int = 0) -> list[dict]:
    ret = []
    for meta in metas:
        size = meta.aligned_size
        ret.append({"name": meta.name, "dtype": meta.dtype, "shape": meta.shape, "offset": offset})
        offset += size
    return ret
```

看到 `offset += size` 用的是 `aligned_size` 而不是裸的 `numel × itemsize`——正是对齐保证了后续每个张量的偏移也天然 256 对齐。

顺带对照 worker 侧的镜像结构（[checkpoint_engine/worker.py:31-36](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L31-L36)）：

```python
class FlattenedTensorMetadata(TypedDict):
    name: str
    shape: torch.Size
    dtype: torch.dtype
    # specify the start offset of this tensor in shared ipc_buffer tensor
    offset: int
```

`FlattenedTensorMetadata ≈ ParameterMeta 的前三个字段 + offset`。它用 `TypedDict` 而不是 `BaseModel`，因为 worker.py 在 vLLM 进程里运行，这段数据经 ZMQ 用 pickle 传输（进程间、同构环境），不需要 JSON 校验，dict 更轻。

#### 4.2.4 代码实践

**实践目标**：验证 `aligned_size` 的 256 字节对齐规律。

**操作步骤**（纯 CPU）：

```python
import torch
from checkpoint_engine.pin_memory import _align_size

for dtype, shape in [
    (torch.float16, (2, 3)),      # 12 字节
    (torch.float16, (128, 128)),  # 32768 字节
    (torch.float32, (10,)),       # 40 字节
    (torch.bfloat16, (1, 1)),     # 2 字节
]:
    raw = dtype.itemsize * torch.Size(shape).numel()
    print(f"{dtype} {list(shape)}: raw={raw}, aligned={_align_size(dtype, torch.Size(shape))}")
```

**需要观察的现象**：12 → 256；32768 → 32768（恰好是 256 的倍数则不变）；40 → 256；2 → 256。

**预期结果**：所有非整倍数都被抬升到下一个 256 的倍数，恰为倍数的保持原值。

#### 4.2.5 小练习与答案

**练习 1**：一个 `float32`、shape 为 `[1000, 1000]` 的张量，`aligned_size` 是多少？

**答案**：\(4 \times 10^6 = 4000000\) 字节，\(4000000 / 256 = 15625\) 恰好整除，所以 `aligned_size = 4000000`。大张量几乎总是天然对齐，对齐主要影响小张量（如 bias、norm 的 scale）。

**练习 2**：`_to_named_tensor(metas, offset=10)` 中初始 `offset=10` 有什么用？

**答案**：`offset` 参数让这段参数列表可以"接在"别的数据后面排布——bucket 的第一个张量往往不是从 0 开始（详见 4.3 节 `BucketRange.offset`）。

### 4.3 H2DBucket 与 BucketRange：传输桶的切分描述

#### 4.3.1 概念说明

上一讲说过，广播是按"桶"（bucket）组织的：一整个 checkpoint 不会被一次性传完，而是切成若干个不超过 `bucket_size` 的桶，逐桶走"H2D → broadcast → reload"流水线。`H2DBucket` 就是**一个桶**的描述：

- `size`：桶内所有参数 `aligned_size` 之和；
- `ranges`：桶里的字节来自哪些**内存桶**（memory bucket，即锁页内存池里第几块 buffer）的哪一段；
- `items`：桶里装了哪些参数的 `ParameterMeta`。

`BucketRange` 是三元组 `(idx, offset, size)`：内存池中第 `idx` 块 buffer 的、从 `offset` 开始的、长度为 `size` 的连续区间。它回答"数据从哪拷"，`items` 回答"拷过去之后怎么解释"。

`BucketRange` 用 `NamedTuple` 而非 `BaseModel`（[checkpoint_engine/data_types.py:78-81](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L78-L81)）：

```python
class BucketRange(NamedTuple):
    idx: int  # bucket_idx of MemoryBucket in memory_pool
    offset: int
    size: int
```

理由：它只存在于 PS 进程内部（构造桶、执行 `_copy_to_buffer`），从不跨进程、从不进 JSON，轻量元组就够了。这也是本项目的一个设计取向：**用 pydantic 的只留给需要跨进程/JSON 边界的模型**。

#### 4.3.2 核心流程

`_gen_h2d_buckets` 的切桶逻辑（简化伪代码）：

```
for owner_rank, 该 rank 的全部参数元数据:
    新建一个空桶
    for 每块内存 buffer (idx), 其中每个参数 meta:
        若 当前桶.size + meta.aligned_size > bucket_size:
            把 [start_offset, offset) 这段记为当前桶的一个 BucketRange
            新开一个桶
        当前桶.size += aligned_size; 当前桶.items.append(meta)
    收尾: 把最后一段记入当前桶的 ranges
```

注意"参数顺序不跨 owner 混合"：每个桶都属于唯一一个 `owner_rank`（数据来源），这保证了广播时 `src` 明确。

#### 4.3.3 源码精读

`H2DBucket` 模型（[checkpoint_engine/data_types.py:84-87](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L84-L87)）：

```python
class H2DBucket(BaseModel):
    size: int
    ranges: list[BucketRange]
    items: list[ParameterMeta]
```

切桶主体（[checkpoint_engine/ps.py:68-96](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L68-L96)），摘关键段：

```python
for owner_rank, items in global_metas.items():
    buckets.append((owner_rank, H2DBucket(size=0, ranges=[], items=[])))
    for idx, metas in enumerate(items.memory_buffer_metas_list):
        start_offset, offset = 0, 0
        for meta in metas.metas:
            s = meta.aligned_size
            if buckets[-1][1].size + s > bucket_size:
                if offset - start_offset > 0:
                    buckets[-1][1].ranges.append(
                        BucketRange(idx, start_offset, offset - start_offset)
                    )
                start_offset = offset
                buckets.append((owner_rank, H2DBucket(size=0, ranges=[], items=[])))
            offset += s
            buckets[-1][1].size += s
            buckets[-1][1].items.append(meta)
        buckets[-1][1].ranges.append(BucketRange(idx, start_offset, offset - start_offset))
```

一个重要细节：`if buckets[-1][1].size + s > bucket_size` 是**先判断后放入**，所以单个超大张量（比 `bucket_size` 还大）仍会被放进一个独立桶，桶的实际 size 会超过 `bucket_size`——桶大小是软上限。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：手工推演一个小规模切桶结果，验证对 `(owner_rank, ranges, items)` 关系的理解。

**操作步骤**：阅读上面引用的 `_gen_h2d_buckets` 代码，然后在纸上推演：

- 输入：`global_metas = {0: MemoryBufferMetaList(memory_buffer_metas_list=[一块含 5 个参数的 buffer])}`，5 个参数的 `aligned_size` 依次为 `256, 256, 512, 256, 1024`，`bucket_size = 1024`。
- 逐轮填写表格：每放入一个参数，记录当前桶的 `size`、`items` 数量；判断是否触发开新桶；触发时写出被固化的 `BucketRange`。

**需要观察的现象**：放入前两个参数后桶 size 为 512；第三个参数 `512 + 512 = 1024` 不大于 1024，仍放入（size=1024）；第四个参数 `1024 + 256 > 1024`，固化 `BucketRange(idx=0, offset=0, size=1024)` 并开新桶；最终得到 3 个桶，ranges 分别是 `(0,0,1024)`、`(0,1024,1280)`、`(0,2304,1024)`。

**预期结果**：每个桶的 `ranges` 区间首尾相接覆盖 `[0, 3328)`，`sum(各桶 size) = 256+256+512+256+1024 = 2304`——注意 ranges 总跨度 3328 大于参数总字节 2304 是推演陷阱：实际上 offset 按 aligned_size 累加，总跨度应恰为 2304。请以自己逐轮累加的结果为准；若与本文数字有出入，以代码逻辑为唯一标准。**待本地验证**（可用 4.5 节实践中的脚本把这段推演跑成真实代码）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BucketRange` 需要记录 `offset` 而不是让每个 `ParameterMeta` 自己带 `offset`？

**答案**：同一参数列表会被切进不同桶，同一参数在不同桶里的起始位置不同。`BucketRange` 描述的是"这一段字节区间的起点"，配合桶内 `items` 的顺序与各自 `aligned_size`，就能推出每个参数的实际偏移（`_to_named_tensor` 的初始 `offset` 参数正是为此服务）。

**练习 2**：把 `bucket_size` 设得非常大（比如超过整个 checkpoint），会有什么后果？

**答案**：只有一个桶，`_update_per_bucket` 退化为"整块传输"：双缓冲失去意义（没有相邻桶可重叠），H2D buffer 需要一次装下全部权重，显存压力大——这正是 u1-l4 讲过的"流水线退化为串行"的一种人为极端。

### 4.4 从 MemoryBuffer 到 DataToGather：元数据的层级聚合

#### 4.4.1 概念说明

剩下四个模型构成一条**由实到虚的聚合链**——越往下越"实在"（带真实指针/张量），越往上越"可传输"（只剩数字和字符串）：

```
MemoryBuffer            （本进程私有：真实张量 + metas）
    ↓ 提取 ptr/size，丢弃张量本身
MemoryBufferMetas       （一块锁页 buffer 的"名片"：指针 + 大小 + 参数清单）
    ↓ 多块 buffer 打包
MemoryBufferMetaList    （本 rank 拥有的全部内存 + p2p 地址 + RDMA 网卡）
    ↓ 追加主机与设备标识
DataToGather            （all_gather_object 广播的信封）
```

各自职责：

| 模型 | 字段 | 谁生产 | 谁消费 |
|---|---|---|---|
| `MemoryBuffer` | `buffer`(真实张量), `size`, `metas`, `manually_pinned` | `pin_memory.py` 的 pin 流程 | PS 本进程（H2D 拷贝源） |
| `MemoryBufferMetas` | `metas`, `ptr`(指针地址), `size` | `gather_metas` 从 `MemoryBuffer` 提取 | 其他 rank（P2P 远端读） |
| `MemoryBufferMetaList` | `p2p_store_addr`, `memory_buffer_metas_list`, `rdma_device` | `gather_metas` 裁剪 `DataToGather` | `_gen_h2d_buckets`、`load_metas`、HTTP API |
| `DataToGather` | 继承 `MemoryBufferMetaList` + `host_ip`, `device_uuid` | `gather_metas` 构造 | `dist.all_gather_object` |

设计动机：**张量不能进集合通信**。`all_gather_object` 走 pickle 序列化，把几个 GB 的权重张量塞进去等于把权重再传一遍。所以广播的只是"名片"（指针 + 大小 + 清单），真实数据随后按需通过 IPC（Broadcast 路径）或 RDMA 远端读（P2P 路径）获取。`host_ip` 与 `device_uuid` 是为了让对端能定位"这张名片对应的机器和设备"。

继承关系上，`DataToGather(MemoryBufferMetaList)` 直接继承而不是复制字段——广播回来之后，`gather_metas` 只是把继承来的三个字段重新装进父类实例，自然丢弃了 `host_ip`/`device_uuid`（[checkpoint_engine/data_types.py:103-111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L103-L111)）：

```python
class MemoryBufferMetaList(BaseModel):
    p2p_store_addr: str | None
    memory_buffer_metas_list: list[MemoryBufferMetas]
    rdma_device: str


class DataToGather(MemoryBufferMetaList):
    host_ip: str
    device_uuid: str
```

#### 4.4.2 核心流程

`gather_metas`（下一单元 u3-l3 精讲，这里只看数据流向）：

```
每个 rank:
    从 _memory_pool 取出 list[MemoryBuffer]
    对每块 buffer: 取 buffer.data_ptr() 与 size → MemoryBufferMetas
    打包成 DataToGather(含 p2p_store_addr, host_ip, device_uuid, rdma_device)
    dist.all_gather_object(metas_lst, metas)   # 只传元数据，不传权重
广播回来后:
    逐个 rank 装回 MemoryBufferMetaList → self._current_global_parameter_metas[rank]
    同时用 rdma_device/p2p_store_addr/host_ip 构建 RDMA 拓扑
```

#### 4.4.3 源码精读

`MemoryBuffer` 与 `MemoryBufferMetas` 的对照（[checkpoint_engine/data_types.py:90-100](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L90-L100)）：

```python
class MemoryBufferMetas(BaseModel):
    metas: list[ParameterMeta]
    ptr: int
    size: int


class MemoryBuffer(BaseModel):
    buffer: _TorchTensor
    size: int
    metas: list[ParameterMeta]
    manually_pinned: bool = False
```

两者字段几乎一一对应，唯一本质区别是 `buffer: _TorchTensor`（真张量）换成了 `ptr: int`（裸地址）。`manually_pinned` 标记这块内存是否走了 `cudaHostRegister` 手动锁页（u2-l4 会讲注销时要用它决定是否手动 unpin）。

`gather_metas` 中从 `MemoryBuffer` 到 `DataToGather` 的提取过程（[checkpoint_engine/ps.py:476-489](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L476-L489)）：

```python
metas = DataToGather(
    memory_buffer_metas_list=[
        MemoryBufferMetas(
            metas=x.metas,
            ptr=x.buffer.data_ptr(),
            size=x.size,
        )
        for x in memory_pool
    ],
    p2p_store_addr=None if self._p2p_store is None else self._p2p_store.addr,
    host_ip=get_ip(),
    device_uuid=self._device_uuid,
    rdma_device=self._rdma_device or "",
)
```

注意 `ptr=x.buffer.data_ptr()`——把指针数值化，这是后续 P2P 路径发起 RDMA 远端读的地址。广播回来后裁剪回 `MemoryBufferMetaList` 存入全局表（[checkpoint_engine/ps.py:504-509](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L504-L509)）：

```python
self._current_global_parameter_metas[i] = MemoryBufferMetaList(
    memory_buffer_metas_list=metas_buckets.memory_buffer_metas_list,
    p2p_store_addr=metas_buckets.p2p_store_addr,
    rdma_device=metas_buckets.rdma_device,
)
```

`device_uuid` 与 `host_ip` 在这里被就地丢弃，它们只在 gather 阶段用于构建拓扑（`_local_rdma_devices` 等），全局表只保留后续 update 需要的三个字段。这个"信封拆掉、信纸留下"的模式值得记住。

#### 4.4.4 代码实践

**实践目标**：用真实张量复现 `MemoryBuffer → MemoryBufferMetas → MemoryBufferMetaList` 的聚合链。

**操作步骤**（纯 CPU，`data_ptr()` 在 CPU 张量上同样有效）：

```python
import torch
from checkpoint_engine.data_types import (
    DataToGather, MemoryBuffer, MemoryBufferMetaList, MemoryBufferMetas,
)
from checkpoint_engine.pin_memory import _align_size

# 模拟 pin_memory 产出的两块 buffer
t1 = torch.arange(12, dtype=torch.float16).reshape(3, 4)
t2 = torch.zeros(100, dtype=torch.bfloat16)
bufs = [
    MemoryBuffer(buffer=t1, size=t1.nbytes,
                 metas=[__import__("checkpoint_engine.data_types", fromlist=["ParameterMeta"]).ParameterMeta(
                     name="w1", dtype=t1.dtype, shape=t1.shape, aligned_size=_align_size(t1.dtype, t1.shape))]),
    MemoryBuffer(buffer=t2, size=t2.nbytes,
                 metas=[__import__("checkpoint_engine.data_types", fromlist=["ParameterMeta"]).ParameterMeta(
                     name="w2", dtype=t2.dtype, shape=t2.shape, aligned_size=_align_size(t2.dtype, t2.shape))]),
]

# 模拟 gather_metas 的提取动作
d = DataToGather(
    memory_buffer_metas_list=[
        MemoryBufferMetas(metas=b.metas, ptr=b.buffer.data_ptr(), size=b.size) for b in bufs
    ],
    p2p_store_addr="10.0.0.1:12345", host_ip="10.0.0.1",
    device_uuid="GPU-abc", rdma_device="mlx5_0",
)

# 模拟广播回来后的裁剪
lst = MemoryBufferMetaList(
    memory_buffer_metas_list=d.memory_buffer_metas_list,
    p2p_store_addr=d.p2p_store_addr, rdma_device=d.rdma_device,
)
print(d.model_dump_json(indent=2)[:600])
print("裁剪后是否还含 device_uuid:", "device_uuid" in lst.model_dump())
```

**需要观察的现象**：JSON 里两块 buffer 各有一个十进制 `ptr`（很大的数字，形如 1_400_000_000_000 量级）；`p2p_store_addr`、`rdma_device`、`host_ip`、`device_uuid` 都是普通字符串/整数，整份 JSON 很小（几百字节）。

**预期结果**：`裁剪后是否还含 device_uuid: False`，验证继承裁剪行为；同时直观感受"只广播元数据"的代价之低。

#### 4.4.5 小练习与答案

**练习 1**：`MemoryBufferMetas.ptr` 是十进制整数，为什么 JSON 里不用十六进制字符串？

**答案**：pydantic 的 `int` 序列化为 JSON number，简单且反序列化零成本；十六进制只是人类阅读习惯，程序侧（RDMA 注册、地址计算）用整数更直接。`tests/test_api.py` 里 `ptr=0x12345678` 也是 Python 字面量写法，JSON 中就是十进制。

**练习 2**：如果两个 rank 的 `rdma_device` 都为空字符串、`p2p_store_addr` 都为 None，`gather_metas` 中 `_local_rdma_devices` 的键会是什么？

**答案**：回退用 `host_ip` 作键（见 [ps.py:511-515](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L511-L515) 的条件表达式：有 p2p 地址时用 `rdma_device@ip`，否则用 `host_ip`）。这表示"没有 RDMA 拓扑信息，按主机聚合"。

### 4.5 JSON 化出口：HTTP API 与 metas 导出/导入

#### 4.5.1 概念说明

前面四个模型最终有两条"出门"路径，都依赖 4.1 节的自定义序列化：

1. **HTTP API**：`/v1/metas` GET 返回全局参数表，POST 接收并装载。FastAPI 直接用 `dict[int, MemoryBufferMetaList]` 作为响应/请求类型注解，pydantic 自动完成 JSON ⇄ 模型。
2. **文件导出/导入（join 复用模式）**：老实例把 metas 写成 JSON 文件（或经 URL 提供），新实例读入后 `load_metas` 重建远端拓扑，再走 P2P 把权重拉过来——不用重新加载 checkpoint 文件。

这就是为什么 `ParameterMeta` 必须能把 dtype 表示成 `"torch.float16"` 这样的字符串：JSON 是唯一通用语。

#### 4.5.2 核心流程

```
导出:  ps.get_metas() → dict[int, MemoryBufferMetaList]
       → TypeAdapter.dump_json → 写文件 / HTTP GET /v1/metas
导入:  读文件 / HTTP GET metas_url
       → TypeAdapter.validate_json → dict[int, MemoryBufferMetaList]
       → ps.load_metas(metas)   # 覆盖全局参数表与远端拓扑
```

`TypeAdapter` 是 pydantic 提供的工具：`BaseModel` 只能序列化自身，而这里顶层是 dict，需要 `TypeAdapter(dict[int, MemoryBufferMetaList])` 包一层。

#### 4.5.3 源码精读

API 端点直接把模型写进签名，FastAPI 据此自动生成校验与文档（[checkpoint_engine/api.py:83-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L83-L93)）：

```python
@app.get("/v1/metas")
async def get_metas() -> dict[int, MemoryBufferMetaList]:
    try:
        return ps.get_metas()
    ...

@app.post("/v1/metas")
async def load_metas(metas: dict[int, MemoryBufferMetaList]) -> Response:
    return wrap_exception(lambda: ps.load_metas(metas))
```

示例脚本的导出与导入是严格对称的一对（[examples/update.py:115-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L115-L117) 与 [examples/update.py:141-147](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L141-L147)）：

```python
# 导出（update 路径，rank 0 执行）
with open(save_metas_file, "wb") as f:
    f.write(_METAS_ADAPTER.dump_json(ps.get_metas()))

# 导入（join 路径）
with open(load_metas_file, "rb") as f:
    metas = _METAS_ADAPTER.validate_json(f.read())
...
ps.load_metas(metas)
```

其中 `_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])`（[examples/update.py:22](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L22)）。

测试里可以完整看到 JSON 往返的验收方式（[tests/test_api.py:21-39](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L21-L39) 与 [tests/test_api.py:57-65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L57-L65)）：

```python
_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])

def _make_meta(rdma_device: str, ip: str) -> MemoryBufferMetaList:
    return MemoryBufferMetaList(
        p2p_store_addr=f"{ip}:12345",
        rdma_device=rdma_device,
        memory_buffer_metas_list=[
            MemoryBufferMetas(
                metas=[ParameterMeta(name="w", dtype=torch.float16,
                                     shape=torch.Size([2, 3]), aligned_size=12)],
                ptr=0x12345678, size=1024,
            )
        ],
    )
```

最后，包门面把这些模型全部纳入公共 API（[checkpoint_engine/__init__.py:7-15](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L7-L15)、[checkpoint_engine/__init__.py:22-40](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L22-L40)）——外部脚本（如 examples/update.py）正是从这里 import 的。

#### 4.5.4 代码实践

**实践目标**：完整跑通一次"构造 → dump_json → validate_json"往返，并对照 pytest 验证。

**操作步骤**（纯 CPU）：

```bash
# 1) 项目根目录下确认依赖可用
python -c "import torch, pydantic; print(pydantic.VERSION)"

# 2) 运行现成的 API 测试（不依赖 GPU）
python -m pytest tests/test_api.py -q

# 3) 交互式做一次往返
python -c "
from pydantic import TypeAdapter
from checkpoint_engine.data_types import MemoryBufferMetaList, MemoryBufferMetas, ParameterMeta
import torch

adapter = TypeAdapter(dict[int, MemoryBufferMetaList])
m = {0: MemoryBufferMetaList(
        p2p_store_addr='10.0.0.1:12345', rdma_device='mlx5_0',
        memory_buffer_metas_list=[MemoryBufferMetas(
            metas=[ParameterMeta(name='w', dtype=torch.float16,
                                 shape=torch.Size([2,3]), aligned_size=12)],
            ptr=0x12345678, size=1024)])}
blob = adapter.dump_json(m)
print(blob.decode())
back = adapter.validate_json(blob)
print('round-trip equal:', back == m)
print('dtype restored as:', type(back[0].memory_buffer_metas_list[0].metas[0].dtype))
"
```

**需要观察的现象**：第 2 步若干测试通过（`test_get_metas_returns_json` 等）；第 3 步打印的 JSON 中 `dtype` 是 `"torch.float16"`、`shape` 是 `[2,3]`、`ptr` 是十进制 `305419896`；`round-trip equal: True`；dtype 恢复为 `<class 'torch.dtype'>`。

**预期结果**：JSON 往返无损，dtype/shape 在出口处降级为字符串/数组、在入口处自动还原为 torch 对象。

#### 4.5.5 小练习与答案

**练习 1**：向 `/v1/metas` POST 一个 `{"0": {...}}`（rank 键是字符串而非整数）会怎样？

**答案**：pydantic 会尝试把字符串 `"0"` 强制转换为 int `0`，校验通过（pydantic v2 默认宽松数值转换）；但传 `"abc"` 会得到 FastAPI 的 422 错误。`tests/test_api.py` 中专门有一条"JSON 合法但形状不符 → 422"的测试。

**练习 2**：为什么 `H2DBucket` 不需要进 `__init__.py` 的 JSON 化链条也能被导出？

**答案**：`H2DBucket` 确实在 `__all__` 里（供外部代码使用类型），但它从不跨进程：切桶和消费桶都发生在 PS 进程内部，所以不需要 JSON Schema/序列化路径——它只是恰好继承了 `BaseModel` 的字段校验好处。

## 5. 综合实践

**任务：手工构造一份"双机四卡"的假想 metas，导出成文件，再作为 join 模式的输入导入。**

背景：join 复用模式（u6-l3 精讲）依赖"新实例读取旧实例的 metas JSON"。本实践在纯 CPU 环境模拟这条数据通路。

步骤：

1. 写一个脚本 `fake_metas.py`（放在项目根目录运行即可，不要放进 `checkpoint_engine/`）：
   - 用 `torch.zeros(...)` 造 4 个不同 dtype/shape 的张量，算出各自 `aligned_size`，构造 `ParameterMeta`；
   - 把它们分成两组，分别包装成两个 `MemoryBufferMetas`（`ptr` 用 `buf.data_ptr()`，`size` 用 `buf.nbytes`）；
   - 组装 `dict[int, MemoryBufferMetaList]`，键为 0 和 1，`rdma_device` 分别设 `"mlx5_0"`、`"mlx5_1"`，`p2p_store_addr` 设为对应 IP 的 `ip:9527`；
   - 用 `TypeAdapter(dict[int, MemoryBufferMetaList]).dump_json(...)` 写入 `fake_metas.json`。
2. 再写导入脚本：`validate_json` 读回，断言 `back == 原对象`，并打印 rank 1 的第一个参数的 name/dtype/shape/aligned_size。
3. 打开 `fake_metas.json` 人工检查：所有 `dtype` 是否都是 `torch.` 前缀字符串？所有 `shape` 是否都是整数数组？所有 `ptr` 是否为十进制数？
4. 对照阅读 [examples/update.py:141-155](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L141-L155)，确认你读回的对象正是 `ps.load_metas` 期待的入参形状。

验收标准：步骤 2 断言通过；步骤 3 检查全部符合；你能口头回答"这份 JSON 里为什么没有 `host_ip` 和 `device_uuid`"（答案在 4.4.3）。

## 6. 本讲小结

- `data_types.py` 只有 111 行，却是 PS 与 worker、Python 与 JSON 之间的通用语言；核心聚合链是 `MemoryBuffer → MemoryBufferMetas → MemoryBufferMetaList → DataToGather`。
- `_TorchDtype`/`_TorchSize` 用 `Annotated + PlainValidator + PlainSerializer + WithJsonSchema` 打通了"torch 对象 ⇄ JSON"的双向转换，且序列化输出能被自己的校验器无损吃回。
- `ParameterMeta` 是最小单元（name/dtype/shape/aligned_size），不含偏移——偏移在打包时动态累加；`aligned_size` 按 256 字节向上取整。
- `H2DBucket + BucketRange` 描述"一桶字节从哪几段内存区间来、装了哪些参数"，桶按 owner_rank 分组、大小是软上限（先放后判）。
- 设计取舍清晰：要跨进程/JSON 边界的用 `BaseModel`（带校验），纯进程内的用 `NamedTuple`（`BucketRange`）或 `TypedDict`（worker 侧 `FlattenedTensorMetadata`）。
- 两条 JSON 出口：FastAPI `/v1/metas` 端点（模型直接作签名注解）与 `TypeAdapter` 驱动的 metas 文件导出/导入（join 模式的基础）。

## 7. 下一步学习建议

本讲只讲了"数据长什么样"，还没讲"数据怎么来"。下一讲 **u2-l2《checkpoint 文件加载：safetensors 解析与 TP 权重拼接》** 将深入 `pin_memory.py`，看 `ParameterMeta` 的原材料——safetensors 文件头——是如何被解析、多个张量并行分片如何按 `tp_concat_dim` 拼接的。阅读时可以带着两个问题：文件头里的 `dtype`/`shape` 如何变成 `FileMeta`？`_ALIGN_SIZE` 对齐在大模型真实权重上浪费了多少字节？如果你已经完成本讲综合实践，可以把 `fake_metas.json` 留着——u6-l3 讲 join 模式时会用到同样的文件格式。
