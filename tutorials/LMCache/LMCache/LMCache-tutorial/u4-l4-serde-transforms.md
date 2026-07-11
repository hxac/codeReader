# SERDE 变换与压缩

## 1. 本讲目标

本讲是专家层（u4）「分布式存储」线的第三篇，承接 [u4-l2 分布式存储架构（L1/L2）](u4-l2-distributed-storage.md) 与 [u4-l3 L2 适配器与可插拔存储](u4-l3-l2-adapters.md)。在 u4-l3 里，`SerdeL2AdapterWrapper` 被当作一个「黑盒」一带而过——它在 `inner`（真正的远端存储适配器）外面套了一层「透明的 (de)serialize」。本讲就打开这个黑盒，讲清楚：

1. **SERDE 是什么**：它是「离开 L1、送往 L2 之前的最后一道可插拔 KV 变换」，与 legacy 的 CacheGen 编解码（见 [u2-l7 KV 编解码与 SERDE](u2-l7-kv-codec-serde.md)）是两套独立的体系。
2. **接口分两层**：同步的 `Serializer` / `Deserializer`（用户只写纯变换逻辑）+ 异步的 `SerdeProcessor`（控制器消费，submit→eventfd→query 三段式）。
3. **如何写一个新的 serde**：仿照内置 `fp8` 量化 serde，写两个同步类 + 一个工厂函数即可，并发、eventfd、生命周期都由 `AsyncSerdeProcessor` 与 wrapper 代办。
4. **多输出（multi）扩展**：当 K 和 V 需要不同 dtype、或某一侧缺失时，用 `MultiSerializer` / `MultiDeserializer` 处理「定长元组」。

学完后，你应当能独立设计并注册一个最小 serde（例如把 KV 截断为 int8），并解释它在 store/load 路径上的完整生命周期。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么要对 KV cache 做 SERDE

KV cache 在 L1（本地内存池）里是「强类型张量」（如 `bfloat16`），直接、零拷贝就能用。但 L2 是远端存储（Redis、S3、文件系统……），它只认**字节**。把一个 `bfloat16` 张量原样写进 L2，固然无损，却一分钱也没省。SERDE 的价值正是在「送进 L2 之前」做一次有损或无损的**字节变换**——最常见的就是**量化压缩**：

\[ \text{L1 张量 (bfloat16, 2 字节/元素)} \xrightarrow{\text{serialize}} \text{L2 字节 (fp8, 1 字节/元素)} \xrightarrow{\text{deserialize}} \text{L1 张量 (bfloat16)} \]

一次往返，L2 存储占用减半，代价是精度损失。这就是本讲所有内置 serde（fp8、turboquant、asym_k16_v8）的共同动机。

> 关键边界：SERDE **只服务于 L2 这类「字节型」远端存储**。L1 内部、以及 GPU 上的热缓存，全程零拷贝、不做任何变换。这与 u2-l7 讲的 legacy CacheGen（服务于单机 `RemoteBackend`）是**并行的两套**体系，互不依赖。

### 2.2 「两层接口」的分工

LMCache 刻意把 SERDE 拆成两层（见设计文档 [`docs/design/v1/distributed/serde/README.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/serde/README.md)）：

| 层 | 接口 | 谁来实现 | 关心什么 |
|---|---|---|---|
| 同步层 | `Serializer` / `Deserializer` | **用户（你）** | 纯字节变换，无线程、无 fd |
| 异步层 | `SerdeProcessor` | `AsyncSerdeProcessor`（框架提供） | submit→eventfd→query 三段式，线程池 |

绝大多数自定义 serde 只需写同步层；异步层由框架的 `AsyncSerdeProcessor` 自动包装。这样新增一种量化方案时，你**完全不需要碰**线程、eventfd、锁或控制器。

### 2.3 submit → eventfd → query 三段式

这与 u4-l3 讲的 L2 适配器完全同构（这是刻意的，见后文）：调用方 `submit_*` 拿到一个 `task_id` 立即返回（**非阻塞**），后台线程跑完后通过 eventfd 通知；调用方在自己的 poll 循环里被唤醒，再 `query_*_result(task_id)` 取走结果。这个「非阻塞 + 事件通知」是 SERDE 能与 L2 异步适配器无缝串联的前提——因为 SERDE 本身就被包成一个「假 L2 适配器」。

## 3. 本讲源码地图

本讲涉及的关键文件，集中在 `lmcache/v1/distributed/serde/`：

| 文件 | 作用 |
|---|---|
| [base.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py) | 定义同步 `Serializer`/`Deserializer`、异步 `SerdeProcessor`、`SerdeConfig`、`SerdeTaskId` |
| [async_processor.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py) | `AsyncSerdeProcessor`：把同步类包成异步处理器（线程池 + 两个 eventfd） |
| [factory.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py) | 注册表 `register_serde_factory` + 构造入口 `create_serde_processor` |
| [fp8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py) | 内置 fp8 量化 serde（最简参考实现） |
| [multi.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py) | 多输出扩展 `MultiSerializer`/`MultiDeserializer` + 单→多适配器 |
| [turboquant/turboquant.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/turboquant/turboquant.py) | 第二个内置 serde：TurboQuant（K/V 异构量化，Triton 内核） |
| [asym_k16_v8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py) | 第三个内置 serde：K 保 16 位 / V 量化 FP8（multi 接口的首个落地实现） |
| [utils.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/utils.py) | `serialized_layout_desc`（算临时缓冲布局）、`make_temp_key`（生成临时 key） |

外围两个文件用于理解「SERDE 如何接进存储链路」：

| 文件 | 作用 |
|---|---|
| [l2_adapters/serde_wrapper.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/serde_wrapper.py) | `SerdeL2AdapterWrapper`：把 serde 串到 store/load 路径（异步接口的唯一消费者） |
| [l2_adapters/config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py) | 从 JSON 的 `"serde"` 子字典解析出 `SerdeConfig` |

## 4. 核心概念与源码讲解

按 4 个最小模块拆分：接口（base + async_processor）、工厂（factory）、内置量化 serde（fp8 + turboquant）、多输出组合（multi + asym_k16_v8）。

### 4.1 SERDE 接口设计：同步变换 + 异步处理器

#### 4.1.1 概念说明

`base.py` 的开篇就把设计意图讲透了：分两层——同步接口由用户实现（纯变换、无线程、无 fd），异步接口由控制器消费（与 L2 适配器同款的 submit→eventfd→query 模式），并用 `AsyncSerdeProcessor` 把前者自动包成后者。这是「关注点分离」的典型：写量化算法的人不需要懂并发，懂并发的人不需要懂量化。

#### 4.1.2 核心流程

数据结构层面，先认识一个「配置载体」与一个「类型别名」：

- [`SerdeConfig`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L26-L40)：镜像 `EvictionConfig` 的角色，从 L2 适配器 spec 里的 `"serde"` JSON 子字典解析而来，含 `type`（注册名）与 `kwargs`（类型专属参数）。
- `SerdeTaskId = int`：异步任务的句柄。

同步层契约（用户实现的部分）：

```text
serialize(src: MemoryObj, dst: MemoryObj) -> int
  # src = KV 强类型张量（已读锁）；dst = 字节缓冲（已写锁，容量 ≥ estimate）
  # 返回「实际写入字节数」（可能 < 缓冲容量，因 estimate 是上界）
estimate_serialized_size(layout_desc) -> int
  # 在任何工作前调用，用于分配 dst 缓冲；必须是上界（含安全余量）
deserialize(src: MemoryObj, dst: MemoryObj) -> None
  # src = L2 读回的字节缓冲；dst = KV 强类型张量（已写锁）；无返回值
```

异步层 `SerdeProcessor` 契约（框架提供、控制器消费）：

```text
submit_serialize(src_objs, dst_objs) -> SerdeTaskId     # 非阻塞，立刻返回
query_serialize_result(task_id) -> bool | None          # 非幂等：每个 id 只返回一次非 None
get_serialize_event_fd() -> int                          # 完成时被 signal；必须与 deserialize fd 不同
（deserialize 侧对称）
estimate_serialized_size(layout_desc) -> int             # 委托给同步层
close() -> None                                          # 释放 fd 与线程
```

> 「非幂等」是关键约束：`query_*_result` 对同一个 `task_id` 只会**返回一次**非 None 值（成功 True / 失败 False），之后永远返回 None。这意味着调用方必须自己记住「我已经取过这个结果了」。

#### 4.1.3 源码精读

**同步层抽象基类** [`Serializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L48-L83) 定义了两个 `@abc.abstractmethod`：

- [`serialize`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L54-L66)：把 src 的 KV 字节写进 dst，返回实际写入字节数。docstring 明确「dst 容量必须 ≥ `estimate_serialized_size()`」。
- [`estimate_serialized_size`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L68-L83)：分配临时缓冲**之前**调用，返回值必须是上界（如压缩器偶发膨胀时留 1.5× 余量）。

[`Deserializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L86-L100) 只有一个 [`deserialize`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L92-L100)，无返回值（完成经由异步层的 eventfd 观测）。

**异步层** [`SerdeProcessor`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L108-L216) 的类 docstring 给出了最关键的不变量：serialize 与 deserialize 的 event fd **必须是两个不同的文件描述符**，否则 wrapper 的 poll 循环无法区分「哪一边完成了」。并明确建议用户**不要直接实现** `SerdeProcessor`，而应实现 `Serializer`/`Deserializer` 再用 `AsyncSerdeProcessor` 包装。

`AsyncSerdeProcessor`（在 [async_processor.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py)）就是那个「自动包装器」。其 [`__init__`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py#L52-L70) 持有：一个共享 `ThreadPoolExecutor`、两个独立的 event notifier（来自 `platform.create_event_notifier`，Linux 用 eventfd、其它 POSIX 退化用 pipe）、一把锁 + 两个 `_completed_*` 字典（按方向分流结果）。

任务执行在 [`_run_task`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py#L160-L233)，里面有一处细节值得记住——它把 serialize 实际返回的字节数 `n` 通过 `dst.set_used_size(n)` 收窄缓冲逻辑大小（[async_processor.py:188-191](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py#L188-L191)）。原因：dst 是按 `estimate_serialized_size`（上界）分配的，但下游 L2 适配器会按 `obj.get_size()` 决定存多少字节——若不收窄，就会把上界的「虚胖」字节也存进 L2，白白吃掉存储空间。最后无论成败都在锁内写结果字典，并 `notifier.notify()` 唤醒 wrapper。

#### 4.1.4 代码实践

**实践目标**：用最小代价跑通「同步层 → 异步层」的包装，观察 submit 是非阻塞的、query 在完成前返回 None。

**操作步骤**（源码阅读型，可在无 GPU 主机上用 fake MemoryObj 完成）：

1. 阅读 [`base.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py) 的四个 ABC，确认同步层只有 3 个方法、异步层有 9 个。
2. 阅读 [`async_processor.py` 的 `_run_task`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py#L160-L233)，画出「submit → 线程池跑 → 写结果字典 → notify」的时序。
3. 写一段伪代码（**示例代码**，非项目原有）：

```python
# 示例代码：仅演示调用形态，不保证可直接运行（需自备 fake MemoryObj）
from lmcache.v1.distributed.serde import AsyncSerdeProcessor
from lmcache.v1.distributed.serde.fp8 import (
    Fp8QuantizationSerializer, Fp8QuantizationDeserializer,
)

proc = AsyncSerdeProcessor(
    Fp8QuantizationSerializer(), Fp8QuantizationDeserializer(), max_workers=1,
)
tid = proc.submit_serialize([src_obj], [dst_obj])   # 立刻返回，非阻塞
assert proc.query_serialize_result(tid) is None     # 完成前应为 None（待本地验证时序）
# ... 等 eventfd 被唤醒 ...
assert proc.query_serialize_result(tid) is True     # 完成后取一次成功
assert proc.query_serialize_result(tid) is None     # 再取即 None（非幂等）
```

**需要观察的现象**：`submit_*` 立即返回 task_id；在后台线程完成前 `query` 返回 `None`；eventfd 被唤醒后 `query` 返回 `True`/`False`；同一个 id 第二次 `query` 又变回 `None`。

**预期结果**：证实「非阻塞 + 非幂等」契约。若你构造的 fake MemoryObj 不支持 `set_used_size`，注意 [`_run_task` 的鸭子类型守卫](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py#L188-L191)（`hasattr(dst, "set_used_size")`）会跳过收窄，不会报错。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `estimate_serialized_size` 必须是「上界」而不是「精确值」？
**参考答案**：因为它在**任何变换之前**就被用来分配 dst 缓冲（见 [base.py:68-83](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L68-L83)）。若它小于实际输出，serialize 会写越界；做成上界最安全，实际写入字节数由 serialize 的返回值 + `set_used_size` 收窄。

**练习 2**：`query_serialize_result` 为什么设计成「非幂等」？
**参考答案**：因为调用方在 poll 循环里会反复查询同一个 task_id 直到拿到结果；若幂等（每次都返回结果），调用方无法区分「这次是新完成」还是「上次的残留」，容易重复处理。返回一次非 None 后即清空，天然防重。

### 4.2 工厂与注册机制

#### 4.2.1 概念说明

有了同步类与异步包装器，还需要一个「按名字造实例」的入口，让 L2 适配器的 JSON 配置能用 `{"type": "fp8", ...}` 引用某种 serde。这就是 [factory.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py) 的职责。它的模式与 u4-l3 讲的 L2 适配器注册表完全一致：进程级字典 `名字 → 工厂函数`，重复注册直接报错（早失败）。

#### 4.2.2 核心流程

```text
启动期：每个 serde 模块在 import 时调用 register_serde_factory("fp8", _create_fp8_serde)
         → _SERDE_FACTORY_REGISTRY["fp8"] = _create_fp8_serde

运行期：L2 适配器配置带 "serde": {"type": "fp8", "fp8_dtype": "float8_e4m3fn"}
         → _parse_serde_config 去掉 "type"，其余键作为 kwargs
         → SerdeConfig(type="fp8", kwargs={"fp8_dtype": "float8_e4m3fn"})
         → create_serde_processor(config)
         → registry[config.type](config.kwargs)
         → 返回一个 SerdeProcessor 实例
```

注意「类型专属参数」的传递：工厂函数只收 `kwargs`（不含 `"type"`），由各 serde 自行解释。`create_serde_processor` 每个 `SerdeL2AdapterWrapper` 构造时调用**恰好一次**，所以每个被包装的适配器都有自己独立的 `SerdeProcessor` 实例。

#### 4.2.3 源码精读

注册表本身是个模块级字典（[factory.py:26](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py#L26)）。[`register_serde_factory`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py#L29-L44) 在重名时 `raise ValueError`——这保证两个不同模块不小心注册同名 serde 时，import 阶段就崩，而不是运行时静默覆盖。

[`create_serde_processor`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py#L52-L69) 找不到名字时，错误信息会列出「所有已注册类型」方便排错（[factory.py:65-68](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py#L65-L68)）。

「serde 如何接进存储链路」由两处完成。其一，配置解析：[l2_adapters/config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py) 的 [`serde_config` 字段](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py#L197) 与 [`_parse_serde_config`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py#L200-L223) 把 JSON 的 `"serde"` 子字典抠出来，去掉 `"type"`，其余键进 `kwargs`。其二，包装：[storage_manager.py:1067-1072](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1067-L1072) 在创建每个 L2 适配器后判断 `if config.serde_config is not None:`，是则用 `SerdeL2AdapterWrapper(inner=adapter, serde=create_serde_processor(...), l1_manager=...)` 把它包起来，对控制器而言它仍是一个普通 `L2AdapterInterface`。

`create_serde_processor` 与 wrapper 是 serde 异步接口的**唯一**消费者（设计文档明确：wrapper is the sole consumer of `SerdeProcessor`'s event fds）。wrapper 的 [`_loop`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/serde_wrapper.py#L396-L427) 同时 poll 四个 fd：内层的 store/load fd + serde 的 serialize/deserialize fd，按「serialize 完成→触发 inner.store」「inner.load 完成→触发 deserialize」串联（见模块 docstring [serde_wrapper.py:10-11](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/serde_wrapper.py#L10-L11)）。

#### 4.2.4 代码实践

**实践目标**：确认内置 serde 已注册，并验证「重复注册报错」与「未知类型报错」。

**操作步骤**：

1. 在 Python 里 `import lmcache.v1.distributed.serde`（触发各 serde 模块自注册）。
2. 调用 `get_registered_serde_types()` 打印已注册名字。预期至少看到 `fp8` 与 `turboquant`（前者在 [fp8.py:103](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L103) 自注册，后者在 [turboquant.py:896](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/turboquant/turboquant.py#L896) 自注册）。
3. 再调用一次 `register_serde_factory("fp8", lambda kw: None)`，预期抛 `ValueError: Serde type already registered: 'fp8'`。
4. 调用 `create_serde_processor(SerdeConfig(type="nope", kwargs={}))`，预期抛 `ValueError: Unknown serde type 'nope'`，且错误信息列出已注册类型。

**需要观察的现象**：步骤 4 的报错信息里应包含 `Registered: fp8, turboquant, ...`，证明 [factory.py:65-68](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py#L65-L68) 的友好提示生效。

**预期结果**：注册表为进程级单例，重名早失败、未知类型给出可操作的错误。**待本地验证**具体注册列表（取决于 `asym_k16_v8` 是否被某处 import——见 4.4）。

#### 4.2.5 小练习与答案

**练习**：为什么工厂函数的签名是 `(kwargs: dict[str, object]) -> SerdeProcessor`，而不是固定的具名参数？
**参考答案**：因为不同 serde 的参数集合完全不同（fp8 要 `fp8_dtype`，turboquant 要 `preset`/`head_dim`/`block_size` 等）。用一个「类型专属 kwargs 字典」让 registry 与 `create_serde_processor` 对所有 serde 通用，类型校验下沉到各 serde 自己的 `_create_*` 函数（如 fp8 用 `getattr(torch, dtype_name)` 校验 dtype 是否存在）。

### 4.3 内置量化 serde：fp8 与 turboquant

#### 4.3.1 概念说明

[fp8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py) 是**最简参考实现**，全文件不到 110 行，是写新 serde 的最佳模板。它把 KV 元素量化到 `torch.float8_e4m3fn`（每元素 1 字节），deserialize 时再 cast 回目标 dtype，有损但简单。

[turboquant](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/turboquant/turboquant.py) 则是「重炮」：用 Triton 内核做 K/V 异构量化（如 K 量化 8 位、V 量化 4 位），支持多种预设（`turboquant_k8v4`、`turboquant_4bit_nc` 等）、可跳过首尾若干层不量化、用 CUDA 临时暂存加速。它展示「同一个同步接口能承载多复杂的变换」。

> 命名直觉：`e4m3fn` = 4 位指数 + 3 位尾数 + finite-only（无 inf/NaN），范围适合推理激活值；`e5m2` 范围更大但精度更低。fp8 serde 默认用前者。

#### 4.3.2 核心流程

**fp8 serialize**（[fp8.py:34-48](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L34-L48)）：

```text
src.tensor (任意 dtype) ──.to(fp8_dtype)──> fp8_tensor
                          ──.view(uint8)──> fp8_as_bytes   # 把 fp8 字节重解释成 uint8
                          ──copy_──> dst.tensor[:n]         # 写进字节缓冲
返回 n = fp8_tensor.numel()  # 每元素 1 字节，故字节数 == 元素数
```

**fp8 estimate**（[fp8.py:50-65](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L50-L65)）：遍历 `layout_desc.shapes` 累乘得到总元素数，**直接返回总元素数**（fp8 是固定 1 字节/元素，确定性映射，无需余量）。注意它的 docstring 特别强调：**不能**膨胀这个估计——因为下游 L2 适配器会按 `get_size()` 存整块 MemoryObj，膨胀会直接侵蚀 fp8 本想省下的存储空间。

> ⚠️ 一个文档/代码不一致：设计文档 [serde/README.md:164-166](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/serde/README.md#L164-L166) 写「fp8 的 estimate 返回 `total_elements × 1.5`」，但**实际源码**返回的是精确的 `total_elements`（无 1.5×）。以源码为准——文档此处已过时。这正是「读代码先于读文档」的一个实例。

**fp8 deserialize**（[fp8.py:74-86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L74-L86)）：取 `n_elements` 字节，`view(fp8_dtype).reshape(dst.shape)` 还原形状，再 `.to(dst.dtype)` cast 回目标 dtype。

#### 4.3.3 源码精读

[`Fp8QuantizationSerializer.__init__`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L31-L32) 只存一个 `_fp8_dtype`，默认 `torch.float8_e4m3fn`。

工厂 [`_create_fp8_serde`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L89-L100) 展示了「工厂如何解释 kwargs」：

- 从 `kwargs` 取 `fp8_dtype`（默认 `"float8_e4m3fn"`），用 `getattr(torch, dtype_name)` 解析成真正的 `torch.dtype`，找不到则 `raise ValueError`；
- 取 `max_workers`（默认 1）；
- 返回 `AsyncSerdeProcessor(serializer, deserializer, max_workers=...)`。

最后一行 [`register_serde_factory("fp8", _create_fp8_serde)`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L103) 在模块 import 时自注册——这是「定义即注册」模式（与 u4-l1 CLI 的 `discover_subclasses`、u4-l3 L2 适配器的 pkgutil 自动发现一脉相承，只是这里更显式）。

turboquant 的复杂度体现在 `_create_turboquant_serde`（[turboquant.py:869-893](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/turboquant/turboquant.py#L869-L893)）解释 5 个 kwargs（`preset`/`head_dim`/`block_size`/`skip_first_layers`/`skip_last_layers`/`max_workers`）并构造 `TurboQuantSerdeConfig`，以及 [`TurboQuantSerializer.serialize`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/turboquant/turboquant.py#L530-L662) 里按层分别走「原始字节直拷 / Triton 量化内核」的分支。但无论多复杂，它对外的同步契约与 fp8 一模一样——这就是分层接口的价值。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：仿照 fp8.py，设计一个「截断为 int8」的最小 serde 骨架，并通过 factory 注册。

**操作步骤**：

1. 通读 [fp8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py) 全文，作为模板。
2. 在 `lmcache/v1/distributed/serde/` 下新建 `int8.py`（**示例代码**，仅为教学骨架，非项目原有文件；不要提交到仓库）：

```python
# 示例代码：截断为 int8 的最小 serde 骨架（教学用）
import torch
from lmcache.v1.distributed.api import MemoryLayoutDesc
from lmcache.v1.distributed.serde.async_processor import AsyncSerdeProcessor
from lmcache.v1.distributed.serde.base import Deserializer, SerdeProcessor, Serializer
from lmcache.v1.distributed.serde.factory import register_serde_factory
from lmcache.v1.memory_management import MemoryObj


class Int8TruncSerializer(Serializer):
    """把浮点 KV 线性映射到 int8（[-128,127]），每元素 1 字节。有损。"""

    def __init__(self, scale: float = 1.0):
        self._scale = scale

    def serialize(self, src: MemoryObj, dst: MemoryObj) -> int:
        src_t = src.tensor
        dst_t = dst.tensor
        if src_t is None or dst_t is None:
            raise ValueError("Int8 serde requires src and dst to have tensors")
        # 浮点 → int8（round + clamp 到 [-128,127]），再当 uint8 字节拷贝
        q = torch.clamp(torch.round(src_t.float() * self._scale), -128, 127).to(torch.int8)
        n_bytes = q.numel()
        dst_t.flatten()[:n_bytes].copy_(q.view(torch.uint8).flatten())
        return n_bytes

    def estimate_serialized_size(self, layout_desc: MemoryLayoutDesc) -> int:
        # int8 固定 1 字节/元素，确定性映射，无需余量（与 fp8 同理）
        total = 0
        for shape in layout_desc.shapes:
            n = 1
            for dim in shape:
                n *= int(dim)
            total += n
        return total


class Int8TruncDeserializer(Deserializer):
    def __init__(self, scale: float = 1.0):
        self._scale = scale

    def deserialize(self, src: MemoryObj, dst: MemoryObj) -> None:
        src_t = src.tensor
        dst_t = dst.tensor
        if src_t is None or dst_t is None:
            raise ValueError("Int8 serde requires src and dst to have tensors")
        n = dst_t.numel()
        q = src_t.flatten()[:n].view(torch.int8).reshape(dst_t.shape)
        dst_t.copy_((q.float() / self._scale).to(dst_t.dtype))


def _create_int8_serde(kwargs: dict[str, object]) -> SerdeProcessor:
    scale = float(kwargs.get("scale", 1.0))
    max_workers = int(kwargs.get("max_workers", 1))  # type: ignore[call-overload]
    return AsyncSerdeProcessor(
        Int8TruncSerializer(scale), Int8TruncDeserializer(scale), max_workers=max_workers,
    )


register_serde_factory("int8", _create_int8_serde)
```

3. 在某处 `from lmcache.v1.distributed.serde import int8  # noqa: F401` 触发自注册（或直接 import 该模块）。
4. 在你的 L2 适配器配置里加 `"serde": {"type": "int8", "scale": 0.1}`。
5. 用 4.2.4 的方法确认 `get_registered_serde_types()` 里出现了 `"int8"`。

**需要观察的现象**：注册成功后，把一个 `bfloat16` 张量经 `Int8TruncSerializer.serialize` 再 `Int8TruncDeserializer.deserialize` 往返，对比前后张量——值应近似但不完全相等（有损）。`estimate_serialized_size` 对一个 `[2, L, T, D]` 形状应返回 `2*L*T*D`，正好是原始 bf16 字节数的一半。

**预期结果**：新 serde 只靠「两个同步类 + 一个工厂函数 + 一行 register」就接入完整异步链路，无需手写线程/eventfd/锁。**待本地验证**端到端 store/load（需构造 MemoryObj 与 L2 适配器，较重；可先用纯 torch 张量单测 serialize/deserialize 的往返正确性）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `Int8TruncSerializer.estimate_serialized_size` 故意返回 `total * 2`（虚报两倍），会发生什么？
**参考答案**：临时 dst 缓冲会按上界分配成两倍大；serialize 实际只写 `total` 字节并返回它，[`_run_task` 的 `set_used_size(n)`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/async_processor.py#L188-L191) 会把逻辑大小收窄回 `total`，所以**功能正确**，但浪费了 L1 临时内存。这正是 fp8 docstring 强调「不要膨胀」的原因——上界是安全垫，不是越大越好。

**练习 2**：为什么 fp8 serde 的 deserialize 要用 `dst_tensor.numel()` 而不是 `src_tensor.numel()` 决定读多少字节？
**参考答案**：因为 src 是字节缓冲，其 `numel` 是按上界分配的字节数（可能 > 实际有用字节）；而 dst 是 KV 强类型张量，其 `numel` 正是元素数 = fp8 字节数。用 dst 的元素数读，恰好读回完整 KV（见 [fp8.py:81-86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L81-L86)）。

### 4.4 多输出 serde：multi 与 asym_k16_v8

#### 4.4.1 概念说明

到目前为止的 `Serializer` 都是「一个张量进、一个字节缓冲出」。这在 K 和 V 共用一个 dtype（被当作一个合并张量）时没问题，但有两种场景失效：

1. **K 与 V 不同 dtype**：一个 typed tensor 只有一个 dtype，无法同时承载「K 用 bf16、V 用 fp8」。
2. **某一侧缺失**：分层放置时，比如 K 留在 L1（CPU pinned），只有 V 流向 L2——serialize 的输入没有 K 这个张量，deserialize 的输出也没有 K 的目的地。

[multi.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py) 定义了「定长元组」形态的**附加性扩展**（additive extension）：它不修改任何现有接口与调用者，`AsyncSerdeProcessor`、factory、wrapper、fp8 全部行为不变。需要多张量的 serde 实现额外混入 `MultiSerializer` / `MultiDeserializer` 即可。

[asym_k16_v8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py) 是首个落地的 multi serde：K 保留原生 dtype 原样拷字节（因 K 直接参与 QKᵀ 点积，量化误差会被放大），V 量化到 FP8（见 [u2-l7](u2-l7-kv-codec-serde.md) 讲过的同样动机），把 `AsymK16V8Codec`（在 `lmcache/v1/kv_codec`）桥接进 multi 契约。

#### 4.4.2 核心流程

核心数据结构（[multi.py:67-76](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L67-L76)）：

```text
MemoryObjGroup = Tuple[Optional[MemoryObj], ...]   # 定长元组，None 表示该槽缺失
LayoutDescGroup = Tuple[Optional[MemoryLayoutDesc], ...]   # 平行的布局描述元组
```

`None` 的语义按方向区分（这是 multi 的精髓）：

- **serialize 输入** 的 `None`：调用方**没提供**该张量（如 V-only 写，K 留在 L1）；实现遇到「必需槽为 None」必须 `raise ValueError`。
- **deserialize 输出** 的 `None`：调用方**不想要**该张量被物化（如 V-only 读）；实现**绝不能**碰缺失槽，且**不能仅因某槽为 None 就失败**。

每个 multi serde 通过 `group_size` 属性声明元组长度，让异步层能在不实例化任何张量的前提下预校验长度。

#### 4.4.3 源码精读

[`MultiSerializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L79-L143) 的三个抽象成员：`group_size`（属性）、`serialize(src: MemoryObjGroup, dst: MemoryObj) -> int`、`estimate_serialized_size(layout_descs: LayoutDescGroup) -> int`。[`MultiDeserializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L146-L171) 对称地有 `group_size` 与 `deserialize(src: MemoryObj, dst: MemoryObjGroup) -> None`。

「单→多」适配器让现有单张量 serde 也能塞进 group 形态的调用点：

- [`_SingleAsMultiSerializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L177-L221)：`group_size == 1`，把长度为 1 的 group 解包后委托给内层 `Serializer`；要求那唯一槽**不能为 None**（单张量 serde 不接受缺失）。
- [`_SingleAsMultiDeserializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L224-L248)：长度为 1 且唯一槽为 None 时当作「刻意跳过」（no-op）而非报错，这样调用方能统一地透传 group。
- 公开入口 [`single_to_multi_serializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L251-L258) / [`single_to_multi_deserializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L261-L270)。docstring 保证「字节级等价」——对同一输入，直接调用 `inner.serialize` 与走适配器产生完全相同的字节。

[`validate_group_size`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L273-L293) 是个纯 arity 校验工具，`group` 类型故意写成 `Sequence[object]` 以同时接受 `MemoryObjGroup` 与 `LayoutDescGroup`，`role` 参数（"src"/"dst"/"layout"）进错误信息方便定位。

`asym_k16_v8.py` 的模块 docstring（[asym_k16_v8.py:9-33](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py#L9-L33)）把两种模式讲得很清楚：

- **storage-only-dequant 模式**：group 大小为 2，serialize 输入 `(K, V)`（都原生 dtype），deserialize 输出 `(K_out, V_out)`（V 从 FP8 反量化）。
- **split-tier / V-only 模式**：serialize 输入 `(None, V)`（K 留 L1），emit `k_payload_len = 0`；deserialize 输出 `(None | K_skip, V_out)`，槽 0 永远 no-op。

实现类 [`AsymK16V8MultiSerializer`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py#L69) 复用 `lmcache/v1/kv_codec` 里的 `quantize_v_fp8` / `dequantize_v_fp8`（[asym_k16_v8.py:53-58](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py#L53-L58)）。

> 注意一个状态细节：`multi.py` 的模块 docstring（[multi.py:24-31](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L24-L31)）写「multi 接口的异步接线（tuple-aware AsyncSerdeProcessor、tuple-aware wrapper）将在一个具体 multi serde 落地后的后续变更中加入」。而 `asym_k16_v8.py` 正是那个落地的 multi serde——它**没有**走 `register_serde_factory`（grep 全包未见其自注册），也未被 [`serde/__init__.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/__init__.py) 导出。这说明 multi 的异步接线仍是进行中（WIP）的工作：契约已定义、首个实现已就位，但「按名字从 JSON 配置拉起 multi serde」的端到端链路尚未完全打通。**待确认**当前 HEAD 是否已补齐该链路——动手前务必 grep 一遍 `AsymK16V8MultiSerializer` 的实际调用点。

#### 4.4.4 代码实践

**实践目标**：理解 multi 的「定长元组 + None 语义」，用单→多适配器把内置 fp8 包成 group 形态调用。

**操作步骤**（源码阅读型）：

1. 阅读 [multi.py 模块 docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L1-L53)，复述「K/V 不同 dtype」与「一侧缺失」两个动机。
2. 阅读 [`_SingleAsMultiSerializer` 的 `serialize`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L193-L204)，确认它对「长度≠1」与「唯一槽为 None」分别如何处理。
3. 写一段伪代码（**示例代码**）验证字节等价：

```python
# 示例代码：演示单→多适配器（需自备 MemoryObj，非项目原有代码）
from lmcache.v1.distributed.serde import single_to_multi_serializer
from lmcache.v1.distributed.serde.fp8 import Fp8QuantizationSerializer

single = Fp8QuantizationSerializer()
multi_s = single_to_multi_serializer(single)
# group 形态调用：长度为 1 的元组
n1 = single.serialize(src_obj, dst_obj)              # 直接调用
n2 = multi_s.serialize((src_obj,), dst_obj)          # 经适配器调用
assert n1 == n2   # 字节级等价（待本地验证）
```

4. 阅读 [asym_k16_v8.py 的模块 docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py#L9-L33)，对照 V-only 模式说明「serialize 输入 `(None, V)`」与「deserialize 输出 `(None, V_out)`」两侧 None 的不同含义。

**需要观察的现象**：经适配器调用的字节数与直接调用完全相同；`_SingleAsMultiSerializer` 收到长度≠1 的 group 会 `raise ValueError`。

**预期结果**：multi 是纯附加扩展，单张量 serde 经适配器后字节不变。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：serialize 输入的 `None` 与 deserialize 输出的 `None`，语义有何不同？
**参考答案**：serialize 输入的 `None` = 调用方**没提供**该张量（如 V-only 写）；实现遇到「必需槽为 None」必须报错。deserialize 输出的 `None` = 调用方**不想要**该张量被物化（如 V-only 读）；实现**绝不能**碰它，且不能仅因 None 就失败。

**练习 2**：为什么 `single_to_multi_deserializer` 对「长度为 1 且唯一槽为 None」选择 no-op 而不是报错？
**参考答案**：为了让调用方能**统一地透传 group**——即使它已经决定不物化那唯一输出，也不必特判。报错会逼调用方为「单元素 + 跳过」这种合法情形写特殊分支（见 [multi.py:241-247](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/multi.py#L241-L247)）。

## 5. 综合实践

把本讲四个模块串起来，做一个完整的「新增并验证一个 serde」的端到端练习：

**任务**：为 LMCache 新增一个 `fp8_e5m2` serde（用 `torch.float8_e5m2` 而非默认的 `e4m3fn`），并验证它能通过 JSON 配置被拉起。

**步骤**：

1. **理解契约**：重读 [base.py 的同步 ABC](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/base.py#L43-L100) 与 [factory.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py)。
2. **复用而非重写**：你**不需要**写新文件——fp8 serde 的工厂 [`_create_fp8_serde`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/fp8.py#L89-L100) 已经支持 `fp8_dtype` kwargs。直接在 JSON 配置里写 `"serde": {"type": "fp8", "fp8_dtype": "float8_e5m2"}` 即可，`getattr(torch, "float8_e5m2")` 会解析成功。先验证这条「零代码」路径。
3. **追踪全链路**：从 JSON 的 `"serde"` 子字典 → [`_parse_serde_config`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py#L200-L223) → [`create_serde_processor`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/factory.py#L52-L69) → [`storage_manager.py:1067-1072` 的包装](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1067-L1072) → wrapper 的 [`_loop`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/serde_wrapper.py#L396-L427) 串联 store/load。画出这条调用链的时序图。
4. **（进阶）真正新增一种 serde**：照 4.3.4 的 int8 骨架，写一个名为 `"int8"` 的新 serde 文件并自注册，然后写一个最小 pytest：构造 src/dst 张量，直接调用你的 `serialize`/`deserialize` 验证往返，断言 `estimate_serialized_size` 返回正确上界。
5. **对照 multi**：思考「如果想让 K 走 int8、V 走 fp8」，你的 int8 serde 应该实现成单张量 `Serializer` 还是 `MultiSerializer`？为什么？（答：multi，因为 K/V 要不同 dtype/变换。）

**验收标准**：

- 能画出 `submit_store_task → submit_serialize → (eventfd) → inner.store` 与 `inner.load → (eventfd) → submit_deserialize` 两条链；
- 能解释为什么 `create_serde_processor` 是 wrapper 的唯一消费者；
- 能说出写一个新 serde 只需「两个同步类 + 一个工厂 + 一行 register」，并发/eventfd/生命周期都由框架代办。

## 6. 本讲小结

- **SERDE 是 L2 边界上的可插拔字节变换**：只服务于远端字节型存储（Redis/S3/FS…），L1 与 GPU 热缓存零拷贝绕过；与 legacy CacheGen 是两套并行体系。
- **接口刻意分两层**：用户只写同步 `Serializer`/`Deserializer`（纯变换），`AsyncSerdeProcessor` 自动包成异步 `SerdeProcessor`（submit→eventfd→query 三段式，与 L2 适配器同构）。
- **关键契约**：`estimate_serialized_size` 必须是上界（但不要无谓膨胀，否则侵蚀存储收益）；`query_*_result` 非幂等；serialize 与 deserialize 的 event fd 必须是两个不同描述符。
- **工厂模式「定义即注册」**：`register_serde_factory("fp8", _create_fp8_serde)` 在 import 期自注册，重名早失败；JSON 的 `"serde"` 子字典去掉 `"type"` 后其余键作为类型专属 kwargs。
- **三个内置 serde 形成梯度**：fp8（最简模板，K/V 同 dtype 同变换）、turboquant（Triton 内核，K/V 异构量化）、asym_k16_v8（multi 接口首个落地，K 保 16 位/V 量化 FP8，支持分层缺失）。
- **multi 是纯附加扩展**：用定长元组 + None 语义解决「K/V 不同 dtype」与「一侧缺失」；单→多适配器保证字节级等价；其异步接线（factory 注册 + wrapper 串联）截至当前 HEAD 仍是 WIP，动手前需 grep 确认。

## 7. 下一步学习建议

- **继续分布式存储线**：本讲的 serde 影响「存多少字节」，而 [u4-l5 淘汰策略与配额管理](u4-l5-eviction-and-quota.md) 关心「存到什么时候被踢」——配额与淘汰同样以字节为单位，理解了 serde 的字节口径，再去读 `quota_manager` 会更顺。
- **回到 legacy 对照**：重读 [u2-l7](u2-l7-kv-codec-serde.md) 的 CacheGen 编解码与本讲的 v1 serde，列一张「输入 → 编码 → 存储 → 解码」对照表，体会两套体系各自的服务对象（单机 RemoteBackend vs 分布式 L2）。
- **深入 multi 的 WIP**：若你对 `asym_k16_v8` 的异步接线感兴趣，可跟踪其 issue/PR，尝试为 multi 接口补一个 tuple-aware 的 `submit_*` 与 wrapper 分支——这是真实且开放的贡献点。
- **写一个真实 serde**：把综合实践里的 int8 serde 扩展为「带 per-channel scale 的 int8」（参考 turboquant 的 `scale`/`zero` 设计），完整跑通 factory 注册 + 单测 + 一个 fs L2 适配器的端到端往返。
