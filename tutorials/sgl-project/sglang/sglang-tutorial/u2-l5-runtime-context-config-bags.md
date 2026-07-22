# u2-l5 RuntimeContext 与配置命名空间（分层结构）

> 本讲承接 [u2-l2 启动流程与 ServerArgs 只读配置模型](./u2-l2-launch-and-server-args.md)。上一篇讲清了「配置从哪里来、为什么 `ServerArgs` 在 `__post_init__` 之后只读」。本讲回答下一个自然的问题：**这些只读配置进入运行期之后，进程里的各个子系统到底去哪里读它、又如何被允许修改它？** 答案就是 `RuntimeContext`。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `RuntimeContext` 是什么、为什么 SGLang 需要这么一个「中枢容器」，以及它由哪几个分层组成。
2. 区分 `ParallelContext`（拓扑透传）与 `_ConfigBag`（配置命名空间袋）这两类特殊访问对象。
3. 认得 `get_parallel()` / `get_exec()` / `get_schedule()` / `get_spec()` 等「访问器族」，并知道它们各自返回哪个命名空间袋。
4. 讲清 `publish(server_args, role=)` 如何把一份只读的 `ServerArgs` 快照成命名空间袋，以及运行期唯一的审计式改写入口 `get_context().override(...)`。
5. 看懂 `Resources` 层里的 `graph_memory_pool` / `streams` / `buffers` 等进程级句柄。

---

## 2. 前置知识

本讲默认你已经掌握 u2-l2 的两个结论：

- **resolve-at-end 契约**：`ServerArgs.__post_init__` 执行完毕后，字段上就是「最终解析好的配置」。解析过程中各 handler 只**声明**改动（写进一个 stash），最后由 `materialize_declarations()` 一次性应用并冻结。
- **只读 + `override()`**：冻结之后，裸的 `server_args.x = y` 会被重写过的 `__setattr__` 拦截报错；唯一合法改写入口是 `ServerArgs.override(source, **fields)`，它会记账（provenance）。

本讲要补充两个新概念：

- **命名空间（namespace）**：配置字段在逻辑上属于某个子系统（比如 `chunked_prefill_size` 属于「调度」、`tp_size` 属于「并行」）。SGLang 用 `NS("schedule")` 这样的标注把每个字段归进一个命名空间，这和字段在文件里的物理位置**无关**。
- **进程级单例（process singleton）**：一个 OS 进程只有一份 `RuntimeContext`，进程内所有代码都通过它读写运行期状态，而不是各自维护一堆模块级全局变量。

> 术语速查：**访问器（accessor）** 指形如 `get_exec()` 的模块级函数；**袋（bag）** 指它返回的 `_ConfigBag` 对象，像一个只读的命名空间抽屉。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/sglang/srt/runtime_context.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py) | 本讲主角。定义 `RuntimeContext` 容器、`ParallelContext`、`_ConfigBag`、各 `Flags`/`Resources`/`ForwardFlags` 分层，以及所有 `get_*()` 访问器和 `publish()`。 |
| [python/sglang/srt/server_args.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py) | 配置源头。`ServerArgs.override()` / `__setattr__` 只读守卫、`__post_init__` 末尾调用 `materialize_declarations`、字段上的 `NS(...)` 标注，以及遗留发布 shim。 |
| [python/sglang/srt/arg_groups/arg_utils.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/arg_groups/arg_utils.py) | `A` / `Arg` / `NS` 三个标注类，以及把 `NS(...)` 元数据翻译成 `{字段: "命名空间路径"}` 的 `namespace_of()`。 |
| [python/sglang/srt/arg_groups/overrides.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/arg_groups/overrides.py) | `materialize_declarations()` 的定义（解析尾声一次性应用声明）。 |
| [python/sglang/srt/managers/scheduler.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py) | 「消费方」范例。调度器通过 `get_schedule()` / `get_parallel()` / `get_context().override(...)` 读写运行期配置。 |

---

## 4. 核心概念与源码讲解

### 4.1 RuntimeContext：进程级运行期状态的中枢容器

#### 4.1.1 概念说明

在引入 `RuntimeContext` 之前，SGLang 的运行期状态散落在很多地方：一份全局 `server_args`、若干模块级全局变量（并行拓扑、MoE 后端、CUDA 池……）。这带来两类典型问题：

- **读写不一致**：某处改了「配置的真相」，但另一处读的是旧副本，于是行为漂移。
- **难测试**：想强制走某条代码路径，只能去 monkeypatch 模块里的某个函数，而生产代码可能从另一个绑定读取，patch 静默失效。

`RuntimeContext` 的设计目标就是**用一个结构化容器收口所有进程级运行期状态**：每个子系统都通过同一组访问器去读，通过同一个审计入口去改。它本身是一个进程级单例，由模块级变量 `_CONTEXT` 持有：

[python/sglang/srt/runtime_context.py:999-1004](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L999-L1004) —— 模块加载即创建唯一的 `ParallelContext` 与 `RuntimeContext`，`get_context()` 返回这个单例。

#### 4.1.2 核心流程

`RuntimeContext` 把状态组织成几个**分层（tier）**，每层有各自的访问器、持有的内容和生命周期。看一眼 `__slots__` 就能数清楚代码里到底有几层：

[python/sglang/srt/runtime_context.py:700-719](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L700-L719) —— `__slots__` 列出了容器持有的全部槽位，`__init__` 把它们初始化好。

> **关于「四层」与「五层」**：本系列大纲把这个中枢概括为「四层结构」（并行拓扑 / 配置袋 / 运行态标志 / 资源句柄）。这是对**进程级持久分层**的精炼概括。代码在此基础上还多了一个**单次前向作用域**的 `forward` 层（用 contextvar 实现），所以我们按代码真实结构介绍**五个分层**：

| 分层 | 容器属性 | 访问器 | 持有什么 | 生命周期 |
| --- | --- | --- | --- | --- |
| **parallel**（并行拓扑） | `parallel` | `get_parallel()` | tp/pp/moe/attn 等 size、rank、进程组句柄 | 无状态透传 |
| **config**（配置袋） | `_server_args` + `_config_bags` | `get_server_args()` + `get_exec()` 等 11 个 | 解析后的配置（只读） | `publish` 时快照 |
| **flags**（运行态） | `flags` | `get_flags()` | 不是配置纯函数的运行态（capture / MoE / DP） | 子系统 init 时物化 |
| **resources**（资源句柄） | `resources` | `get_resources()` / `get_stream()` / `get_buffer()` | graph 内存池、命名 stream/buffer、EPLB 状态等 | 懒创建；`reset_context()` 清空 |
| **forward**（单次前向） | `forward` | `get_forward()` | 仅作用在本次前向的标志（多流开关、MoE 输出缓冲等） | contextvar，每次前向 |

一个进程的生命周期大致是：

```
进程启动
  └─ ServerArgs(...) 构造 → __post_init__ 解析 → materialize_declarations 冻结（只读）
  └─ runtime_context.publish(server_args, role="scheduler"/"tokenizer"/...)
        └─ set_server_args：存 server_args + 把 NS 字段快照成配置袋 + 接好 parallel
  └─ 各子系统 init（Scheduler / ModelRunner / …）
        └─ 通过 get_schedule()/get_exec()/get_parallel() 读配置
        └─ 物化 flags（如 initialize_moe_config）、懒创建 resources
  └─ 稳态服务
        └─ 每次前向：get_forward().scoped(...) 设临时标志
        └─ 运行期需要改配置：get_context().override(source, ...)
  └─ 测试 teardown：reset_context() 清空
```

#### 4.1.3 源码精读

`Resources` 层是理解「句柄」最直观的入口——它就是一组命名槽位：

[python/sglang/srt/runtime_context.py:407-438](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L407-L438) —— `Resources` 数据类。`graph_memory_pool` 是 prefill/decode 两套 graph 后端共享的 CUDA 内存池；`streams` / `buffers` 是「按名字取或建」的命名 stream 与持久缓冲；`expert_distribution_recorder` 等 EPLB 状态也挂在这里。

对应的「按名字取或建」访问器：

[python/sglang/srt/runtime_context.py:721-748](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L721-L748) —— `get_stream(name)` 第一次调用时创建 `torch.cuda.Stream()` 并缓存；`get_buffer(name, factory)` 同理用 `factory()` 建缓冲。注意注释强调：创建 stream/buffer 是 driver 调用，**必须在 cuda-graph 捕获之外**进行，所以租用点放在 init/warmup。

测试结束时，`reset_context()` 把整个容器恢复成干净状态：

[python/sglang/srt/runtime_context.py:1120-1134](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1120-L1134) —— 丢掉已发布的 `server_args`、清空配置袋、重置 flags/resources/forward。`parallel` 因为不持有状态而无需处理。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「一个进程 = 一个 `RuntimeContext`，状态分层存放」的直觉。
2. **步骤**：
   - 打开 [runtime_context.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py)，定位 `__slots__`（L700）与 `__init__`（L711）。
   - 在仓库内搜索 `get_context()` 的调用点，数一数有多少个子系统在用这个单例。
3. **观察**：你会看到 `get_context()` 主要出现在两类位置——运行期改配置（`get_context().override(...)`）和测试（`get_context().override_server_args(...)`）。
4. **预期结果**：能口头说出五个分层各自的一句话职责。

```bash
# 在仓库根目录执行（只读检索）
grep -rn "get_context()" python/sglang/srt | head -20
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 `reset_context()` 里没有重置 `parallel`？
**答案**：`ParallelContext` 是「无状态透传」层——它不自己存拓扑，而是每次读取都委托给 `parallel_state` / `dp_attention` 里的规范 getter（见 4.2）。没有状态可清。

**练习 2**：`Resources` 为什么用「按名字取或建（keyed-lazy）」而不是在 `__init__` 里一次性分配？
**答案**：因为这些句柄（CUDA stream、持久缓冲）属于不同子系统、按需才存在，且创建是 driver 调用、依赖 GPU 已就绪。懒创建让未用到的子系统零开销，也避免在捕获 cuda-graph 时误触发创建。

---

### 4.2 ParallelContext 与配置命名空间袋 `_ConfigBag`

#### 4.2.1 概念说明

`get_parallel()` 和 `get_exec()` 返回的对象长得不一样，理解它们的差别是本讲的关键：

- **`ParallelContext`（拓扑透传）**：并行拓扑是「运行起来的事实」——`tp_size` 取决于进程组真正建好了没有。所以 `ParallelContext` 不缓存值，而是用 `@property` **每次都现读** `parallel_state.get_tensor_model_parallel_world_size()` 这类规范 getter。同时，少数**并行配置叶子**（如 `pp_max_micro_batch_size`）也从这里读。
- **`_ConfigBag`（配置命名空间袋）**：解析后的配置是「启动时快照的事实」。`publish` 时，SGLang 根据 `NS(...)` 标注把 `ServerArgs` 的字段投影成一棵袋树（如 `exec.moe.eplb`），此后这棵树就是配置的**唯一真相源**。袋是只读的（裸赋值会报错），读取是普通属性链 `get_exec().moe.moe_runner_backend`。

#### 4.2.2 核心流程

`ParallelContext` 的读取逻辑（伪代码）：

```
读取 pc.tp_size:
  若 _overrides 里有（测试 override）→ 返回它
  否则 → 调 parallel_state.get_tensor_model_parallel_world_size()  # 透传

读取 pc.<某个非 property 名字>:
  走 __getattr__ → 从 parallel 配置袋里找（如 pp_max_micro_batch_size）
  找不到 → AttributeError
```

配置袋树的构建（`_build_config_bags`，伪代码）：

```
namespace_of(ServerArgs) → {"chunked_prefill_size": "schedule",
                            "moe_runner_backend":   "exec.moe",
                            "disable_cuda_graph":   "exec.graph", …}
for 字段, 路径 in 该映射:
    value = getattr(server_args, 字段)         # 解析后的值
    按路径拆段，逐级 get-or-create 子 _ConfigBag
    在最末袋上 _set(字段, value)               # 同时写成真实实例属性
```

#### 4.2.3 源码精读

`ParallelContext` 的透传模式：

[python/sglang/srt/runtime_context.py:109-140](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L109-L140) —— 类文档说明「live 拓扑走 `@property`，配置叶子走 `__getattr__`」；`__getattr__` 只在没有命中 property/slot 时才触发，从 `_config`（parallel 配置袋）取叶子。

`_v` 是「测试 override 优先，否则透传」的统一读法，`tp_size` 是典型例子：

[python/sglang/srt/runtime_context.py:142-170](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L142-L170) —— `_v(name, getter)`：`_overrides` 里有就用它（测试注入），否则调 `getter()`。`tp_size` 透传到 `_ps().get_tensor_model_parallel_world_size`。

测试专用拓扑注入入口：

[python/sglang/srt/runtime_context.py:146-158](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L146-L158) —— `ParallelContext.override(**kwargs)`：临时强制拓扑值，校验 key 必须在 `_PARALLEL_FIELDS` 白名单内，退出时恢复（支持嵌套）。

`_ConfigBag` 是「只读命名空间抽屉」：

[python/sglang/srt/runtime_context.py:568-645](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L568-L645) —— 类文档强调：值在 `publish` 时从 `server_args` 快照，此后是唯一真相源；裸赋值被 `__setattr__`（L607）拒绝， sanctioned 写者是 `get_context().override(...)`（永久）与 `.override(**kw)`（测试作用域）。`_set`（L613）是内部写，**同时更新记账字典 `_fields` 和真实实例属性**——后者保证 `bag.leaf` 是普通属性读取，可被 `torch.compile`/dynamo 追踪（编译过的模型前向里读配置不会断图）。

袋树的构造器：

[python/sglang/srt/runtime_context.py:647-692](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L647-L692) —— `_build_config_bags(server_args)`：遍历 `namespace_of(...)`，按点分路径逐级建子袋，叶子调 `bag._set`。注意两处硬错误：同一层一个名字既是叶子又是子组、或字段缺失，都直接 raise（不静默丢弃），避免后续出现「这个叶子为什么没投影」的迷惑。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把「字段的 `NS(...)` 标注 → 配置袋路径」这条链走通。
2. **步骤**：
   - 在 [server_args.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py) 找到 `chunked_prefill_size` 字段，确认它标注了 `NS("schedule")`（L748-L752）。
   - 在 [arg_utils.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/arg_groups/arg_utils.py) 读 `namespace_of()`（L102-L119），理解它如何从类型注解里抽出 `NS.path`。
   - 在 [scheduler.py:1001](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L1001) 看到 `self.chunked_prefill_size = get_schedule().chunked_prefill_size`——调度器正是从 `schedule` 袋里读这个叶子。
3. **观察**：字段在 `server_args.py` 里的物理位置（在「memory」注释块附近）和它的命名空间（`schedule`）**不必一致**——归属完全由 `NS(...)` 决定。
4. **预期结果**：能说出「`NS("schedule")` → 顶层袋 `schedule` → `get_schedule().chunked_prefill_size`」这条完整路径。

#### 4.2.5 小练习与答案

**练习 1**：`ParallelContext.tp_size` 和配置袋里某个叫 `tp_size` 的叶子同名时，读 `get_parallel().tp_size` 会返回哪个？
**答案**：返回 live property（运行事实）。`ParallelContext` 文档明确：同名时 property 胜出；而分布式建好之后「同名==同值」成立，两者一致。

**练习 2**：为什么 `_ConfigBag` 故意**不**用 `__slots__`，而是把叶子存成真实实例属性？
**答案**：为了让 `bag.leaf` 是普通属性加载，能被 `torch.compile`/dynamo 追踪——模型前向被编译时（如 piecewise cuda graph 会编译整个前向），在编译图里读配置不能断图。`__dict__` 正是「可追踪读取」的前提。

---

### 4.3 `get_*()` 访问器族与 11 个配置命名空间

#### 4.3.1 概念说明

为了让调用方「一个 import、一种命名」，`runtime_context.py` 在模块级暴露了一组自由函数——**访问器族**。它们分三类：

1. **分层访问器**：`get_context()` / `get_parallel()` / `get_server_args()` / `get_flags()` / `get_resources()` / `get_forward()`，分别返回五个分层。
2. **配置命名空间访问器（11 个）**：每个返回一个顶层 `_ConfigBag`。
3. **资源快捷访问器**：`get_stream(name)` / `set_stream(name, stream)` / `get_buffer(name, factory)`。

11 个配置命名空间是：`device` / `model` / `exec` / `schedule` / `memory` / `spec` / `lora` / `mm` / `disagg` / `serving` / `observability`。

> **关键区分**：`get_server_args()` 返回的是**纯净的启动记录**（留作 debug/复现，本身只读）；而业务代码读配置应当读**命名空间袋**（`get_exec()` 等），因为运行期的 `override()` 会写进袋、却**不**回写 `server_args`。读 `server_args` 会拿到过时值。

#### 4.3.2 核心流程

读取一条配置的典型路径：

```
# 想知道调度相关的 chunked_prefill_size
get_schedule().chunked_prefill_size
   │
   ├─ get_schedule() → _CONTEXT.config_bag("schedule") → 顶层 schedule 袋
   └─ .chunked_prefill_size → 袋上的真实实例属性（publish 时快照的值）
```

如果 publish 之前就读，`config_bag()` 会「fail closed」直接报错（见下方源码），避免读到半成品配置。

#### 4.3.3 源码精读

分层访问器都很薄，直接转发到单例的槽位：

[python/sglang/srt/runtime_context.py:1003-1024](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1003-L1024) —— `get_context/get_parallel/get_server_args/get_flags/get_resources/get_forward` 全部是一行转发。`get_server_args()` 走 `server_args` property，未发布时会抛 `"Global server args is not set yet!"`（这条文案被测试和用户脚本匹配，刻意保留）。

11 个命名空间访问器共享同一个 `config_bag(name)` 实现：

[python/sglang/srt/runtime_context.py:1032-1073](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1032-L1073) —— `get_device/get_model/get_exec/get_schedule/get_memory/get_spec/get_lora/get_mm/get_disagg/get_serving/get_observability` 逐一调用 `config_bag(name)`。

`config_bag` 的「fail closed」语义：

[python/sglang/srt/runtime_context.py:785-793](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L785-L793) —— 若袋未发布或名字不在已投影的袋里，抛 `ValueError("config namespace ... not published")`。注释列出了全部 11 个合法名字。

字段的 `NS(...)` 标注决定了它落进哪个袋。几个真实例子：

| 字段 | NS 标注 | 落进的袋 | 读取示例 |
| --- | --- | --- | --- |
| `model_path` | `NS("model")` | `model` | `get_model().model_path` |
| `tp_size` / `dp_size` | `NS("parallel")` | `parallel`（也经 `get_parallel()`） | `get_parallel().tp_size` |
| `mem_fraction_static` | `NS("schedule")` | `schedule` | `get_schedule().mem_fraction_static` |
| `chunked_prefill_size` | `NS("schedule")` | `schedule` | `get_schedule().chunked_prefill_size` |
| `disable_cuda_graph` | `NS("exec.graph")` | `exec.graph` | `get_exec().graph.disable_cuda_graph` |

对应源码：`model_path` 标注 [server_args.py:456-463](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L456-L463)、`mem_fraction_static` [server_args.py:721-725](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L721-L725)、`chunked_prefill_size` [server_args.py:748-752](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L748-L752)、`tp_size` [server_args.py:952-959](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L952-L959)、`dp_size` [server_args.py:984-991](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L984-L991)、`disable_cuda_graph` [server_args.py:1760](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L1760)。

把标注翻译成映射的函数：

[python/sglang/srt/arg_groups/arg_utils.py:102-119](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/arg_groups/arg_utils.py#L102-L119) —— `namespace_of(cls)`：带 `lru_cache`，用 `get_type_hints(..., include_extras=True)` 读出 `Annotated` 元数据里的 `NS`，返回 `{字段名: 路径}`。没有 `NS` 标记的字段不出现在映射里（会有覆盖率 lint 提醒）。

「消费方」真实用法——调度器各处都通过访问器读配置：

[python/sglang/srt/managers/scheduler.py:1001](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L1001) —— `self.chunked_prefill_size = get_schedule().chunked_prefill_size`（读调度袋）。
[python/sglang/srt/managers/scheduler.py:901](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L901) —— `get_schedule().min_free_slots_delay`。
[python/sglang/srt/managers/scheduler.py:919-921](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L919-L921) —— `get_parallel().attn_tp_group` / `get_parallel().attn_cp_group`（读并行层的进程组句柄）。
[python/sglang/srt/managers/scheduler.py:485-486](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L485-L486) —— `get_disagg().disaggregation_mode`（分离部署袋）。

#### 4.3.4 代码实践（本讲指定实践）

> 这是本讲规格要求的实践任务。

1. **目标**：列出访问器族返回的命名空间袋，并验证一个真实子系统（scheduler）通过哪个访问器读配置。
2. **步骤**：
   - 在 [runtime_context.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py) 列出 11 个配置命名空间访问器（L1032-L1073）及其返回的顶层袋名。
   - 用下面的检索，统计调度器分别用了哪些访问器：
     ```bash
     grep -nE "get_(schedule|exec|parallel|spec|disagg|memory|model|device|lora|mm|serving|observability)\(\)" \
       python/sglang/srt/managers/scheduler.py | head -30
     ```
   - 任选一条（如 `get_schedule().chunked_prefill_size`），回溯到它在 `server_args.py` 的 `NS("schedule")` 标注。
3. **观察**：调度器大量使用 `get_schedule()` 与 `get_parallel()`，偶尔用 `get_exec()` / `get_spec()` / `get_disagg()`——每个子系统「偏好」的命名空间和它的职责高度对应。
4. **预期结果**：能画出「字段 → NS 标注 → 顶层袋 → 访问器 → 调度器读取点」的对照表。

#### 4.3.5 小练习与答案

**练习 1**：代码里 `get_global_server_args()` 和 `get_server_args()` 都能拿到 `ServerArgs`，新代码该用哪个？
**答案**：用 `runtime_context.get_server_args()`。`get_global_server_args()` 是遗留 shim（[server_args.py:8587-8592](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L8587-L8592)），其调用点数量被棘轮测试「只减不增」，禁止新增。

**练习 2**：为什么业务代码读配置应当读袋（`get_schedule().x`）而不是读 `get_server_args().x`？
**答案**：因为运行期 `override()` 只写袋、不回写 `server_args`（见 4.4）。读 `server_args` 会拿到启动时的旧值，读袋才能拿到「进程当前真正在跑」的配置。

---

### 4.4 `publish(role)` 发布与 `override()` 审计式改写

#### 4.4.1 概念说明

配置从「只读的 `ServerArgs`」变成「可被各处读取的命名空间袋」，需要一个**发布**动作；运行期少数合法的配置改动，需要一个**审计式改写**入口。本模块把这两件事讲清楚，并区分代码里好几个同名 `override`。

先看几个容易混淆的 `override`：

| 入口 | 位置 | 何时用 | 写哪里 |
| --- | --- | --- | --- |
| `ServerArgs.override(source, **fields)` | server_args.py | 解析期间 / publish 前后 | 写 `server_args` 字段（持 `_in_override` 令牌放行只读守卫），可解析字段并入声明 stash |
| `RuntimeContext.override(source, **fields)` | runtime_context.py | **运行期**业务改写 | **只写配置袋**，不碰 `server_args`；全或无校验；记 `_overrides_log` |
| `_ConfigBag.override(**kwargs)` / `ParallelContext.override(...)` / flags 组 `.override(...)` | runtime_context.py | **仅测试**，作用域内临时改 | 退出时恢复 |
| `RuntimeContext.override_server_args(**fields)` | runtime_context.py | **仅测试**，过渡性 | 发布一个携带覆盖的 dummy `ServerArgs` |

#### 4.4.2 核心流程

发布与运行期改写的时序：

```
1) 解析尾声：materialize_declarations(server_args)
     └─ 把累积的声明一次性 setattr 到字段，置 _declarations_materialized = True（冻结）

2) 发布：publish(server_args, role="scheduler")
     ├─ 记录进程角色 role
     └─ set_server_args(server_args)
          ├─ 用 server_args.enable_torch_compile 给 capture 标志层播种
          ├─ 存 server_args 到 _server_args
          ├─ _build_config_bags(server_args) → 快照出配置袋树到 _config_bags
          ├─ 把 parallel 配置袋接到 ParallelContext._config
          └─ 清空 _overrides_log（新配置生命周期，旧 provenance 失效）

3) 稳态运行期改写（如调度器算出 pp_max_micro_batch_size 默认值）：
     get_context().override("scheduler.pp_max_micro_batch_size_default",
                            pp_max_micro_batch_size=…)
     └─ 按 NS 路由到对应袋 → bag._set(字段, 值) → 追加 _overrides_log

4) 上报端点（/server_info、get_internal_state）想读「当前真正在跑的配置」：
     resolved_server_args_dict()
     └─ 以 vars(server_args) 为底，把 _overrides_log 里的覆盖叠加上去
```

#### 4.4.3 源码精读

发布函数本身很薄：

[python/sglang/srt/runtime_context.py:1076-1088](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1076-L1088) —— `publish(server_args, *, role, hf_config=None)`：记录 `role`（`tokenizer` / `scheduler` / `encoder` / `expert_backup` / `launcher` / `test`），调 `set_server_args`。注释指出「一进程一次发布」；draft worker **跳过** publish（不能覆盖 target 的配置）。`role` 当前只作 provenance，按角色投影命名空间是后续工作。

`set_server_args` 干了发布的所有重活：

[python/sglang/srt/runtime_context.py:759-783](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L759-L783) —— 播种 capture 层、存 server_args、调 `_build_config_bags` 快照配置、把 parallel 配置袋接到 `ParallelContext._config`、清空 `_overrides_log`。注释强调允许覆盖发布（测试每个用例重新发布），但生产侧的顺序纪律在调用方。

运行期业务改写入口（重点）：

[python/sglang/srt/runtime_context.py:795-838](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L795-L838) —— `RuntimeContext.override(source, **fields)`：**不碰 `server_args`**，只写配置袋，从根上杜绝「写了一个仓、读另一个仓」的不一致；扁平字段名按 `NS` 元数据路由到对应袋；**全或无**校验（未知/未投影字段在任何写入前就中止）；`source` 记进 `_overrides_log` 供复现。

改写来源的可追溯：

[python/sglang/srt/runtime_context.py:840-866](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L840-L866) —— `overrides_log()` 返回 `[(source, {field: value})]`；`resolved_server_args_dict()` 把这些覆盖叠加到 `vars(server_args)` 上，供 `/server_info` 等**上报端点**读「当前正在跑的配置」（否则运行期改的权重版本、tunable 永远不会出现在读回里）。

对照看 `ServerArgs` 侧的 override 与只读守卫：

[python/sglang/srt/server_args.py:7845-7877](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L7845-L7877) —— `ServerArgs.override`：可解析字段（白名单 `resolvable_fields`）并入声明 stash（这样**重新发布也能解析出同样的值**），其余字段记 `_runtime_mutations`；置 `_in_override=True` 令牌后再 `setattr`，以便放行下面的守卫。
[python/sglang/srt/server_args.py:7879-7896](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L7879-L7896) —— `__setattr__`：物化之后，凡是不以 `_` 开头、且没持 `_in_override` 令牌的裸赋值，一律 `raise AttributeError`，并提示用 `get_context().override(...)`。（注释说明该守卫**曾经**由 `SGLANG_STRICT_CONFIG_MUTATION` 控制，现已无条件生效。）

冻结动作的调用点与定义：

[python/sglang/srt/server_args.py:3463-3469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L3463-L3469) —— `__post_init__` 末尾调用 `materialize_declarations(self)`。
[python/sglang/srt/arg_groups/overrides.py:216-225](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/arg_groups/overrides.py#L216-L225) —— `materialize_declarations`：按 gate 顺序（后写者胜）把声明 `setattr` 到字段，最后置 `_declarations_materialized = True`。

最后看一个真实的「运行期 override」调用——调度器在 PP 场景算出 `pp_max_micro_batch_size` 默认值：

[python/sglang/srt/managers/scheduler.py:909-915](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L909-L915) —— 当 `get_parallel().pp_max_micro_batch_size` 未设置时，调 `get_context().override("scheduler.pp_max_micro_batch_size_default", pp_max_micro_batch_size=…)`。注意它**没有**裸写 `server_args.pp_max_micro_batch_size = …`——那会触发只读守卫报错。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解「为什么运行期改配置必须走 `get_context().override(...)`，而不是直接赋值」。
2. **步骤**：
   - 读 [runtime_context.py:795-838](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L795-L838) 的 `override`，确认它只写袋、不写 `server_args`。
   - 读 [server_args.py:7879-7896](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L7879-L7896) 的 `__setattr__`，确认裸赋值会被拦截。
   - 在仓库里搜索所有 `get_context().override(` 调用，看看哪些子系统在做运行期改写：
     ```bash
     grep -rn "get_context().override(" python/sglang/srt | head
     ```
3. **观察**：运行期改写点很少且都带 `source` 字符串（如 `"scheduler.pp_max_micro_batch_size_default"`），便于事后追溯「这个值是谁改的」。
4. **预期结果**：能解释「`server_args` 是只读启动记录；袋是唯一可写真相源；`override()` 只写袋并记账」这套设计的动机（杜绝读写双仓不一致）。

> **可选运行型实践（待本地验证）**：若你已安装完整的 sglang GPU 栈，可在 Python 里亲手感受 fail-closed 与只读守卫（dummy 模型会提前 return `__post_init__`，不触发物化守卫，适合做轻量实验）：
>
> ```python
> # 示例代码（非项目原有，仅供在本地 REPL 验证概念）
> from sglang.srt.server_args import ServerArgs
> from sglang.srt import runtime_context as rc
>
> sa = ServerArgs(model_path="dummy")        # dummy: __post_init__ 提前返回
> rc.publish(sa, role="test")                # 发布：投影出配置袋
> print(rc.get_parallel())                   # <ParallelContext object>
> # 读取某叶子（是否投影取决于 dummy 下字段的 NS 标注与默认值）
> rc.get_context().override("demo", chunked_prefill_size=2048)  # 运行期改写袋
> rc.reset_context()                         # 收尾清理
> ```
> 若环境无 GPU 栈，`import sglang.srt` 可能失败——此时以源码阅读实践为准即可。

#### 4.4.5 小练习与答案

**练习 1**：`RuntimeContext.override` 为什么坚持「不碰 `server_args`」？
**答案**：为了避免「写了一个仓、读另一个仓」的不一致：业务代码统一从袋读（`get_schedule().x`），所以改写也只改袋；`server_args` 始终保持为启动时的纯净记录，仅供 debug/复现。

**练习 2**：`/server_info` 想展示「当前正在跑的配置」，应该序列化 `server_args` 还是调 `resolved_server_args_dict()`？
**答案**：调 `resolved_server_args_dict()`（[runtime_context.py:847-866](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L847-L866)）。它在 `vars(server_args)` 之上叠加 `_overrides_log` 里的运行期覆盖；直接序列化 `server_args` 会丢掉这些改动。

**练习 3**：draft worker（投机解码的草稿 worker）为什么不调 `publish`？
**答案**：`publish` 的文档（[runtime_context.py:1080-1082](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1080-L1082)）写明：draft worker 与 target 共享同一个 `server_args` 对象，若 draft 调 publish 会把 target 的配置袋覆盖掉。所以 draft 跳过 publish。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「配置追踪」小任务：

> **任务**：选定一个配置字段，追踪它从 CLI 到运行期读取的完整一生。

建议字段：`chunked_prefill_size`（CLI `--chunked-prefill-size`）。

1. **标注**：在 [server_args.py:748-752](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L748-L752) 找到字段，记下它的 `NS("schedule")` 标注——说明它归入 `schedule` 命名空间。
2. **解析**：说明该字段在 `__post_init__` 期间可能被某个 handler 声明改动，最终由 [materialize_declarations](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/arg_groups/overrides.py#L216-L225) 应用并冻结。
3. **发布**：追踪 `publish` → `set_server_args` → `_build_config_bags`，说明该字段的值如何被快照进 `schedule` 袋（用 `namespace_of` 的映射驱动）。
4. **读取**：定位调度器在 [scheduler.py:1001](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L1001) 通过 `get_schedule().chunked_prefill_size` 读到它。
5. **（选做）改写**：举一个**会触发只读守卫报错**的反例（`server_args.chunked_prefill_size = …`），并改写成正确形式 `get_context().override("demo", chunked_prefill_size=…)`。

产出一张「字段生命周期」表，列出每个阶段的代码位置（文件:行号）。

---

## 6. 本讲小结

- `RuntimeContext` 是**一个进程一个**的运行期状态中枢，按 `__slots__` 分成五个分层：`parallel`（拓扑透传）/ config（`server_args` + 配置袋）/ `flags`（运行态）/ `resources`（句柄）/ `forward`（单次前向）。大纲的「四层」是对前四个进程级持久分层的概括。
- **配置袋（`_ConfigBag`）** 是解析后配置的唯一真相源：`publish` 时按字段上的 `NS(...)` 标注把 `ServerArgs` 快照成命名空间袋树；袋只读，读取是可被 `torch.compile` 追踪的普通属性访问。
- **11 个命名空间**（device/model/exec/schedule/memory/spec/lora/mm/disagg/serving/observability）各对应一个 `get_*()` 访问器；业务代码读配置应当读袋，而不是读 `get_server_args()`（后者是只读启动记录，会过时）。
- **`publish(server_args, role=)`** 一进程一次，记录角色并把袋投影出来；draft worker 故意跳过以免覆盖 target。
- 运行期改配置的唯一审计式入口是 **`get_context().override(source, **fields)`**：只写袋、不写 `server_args`、全或无校验、记 `_overrides_log`；裸赋值 `server_args.x = y` 会被 `__setattr__` 守卫拦截。
- **`Resources`** 用「按名字取或建」管理 `graph_memory_pool` / `streams` / `buffers` 等句柄，创建是 driver 调用，须在 cuda-graph 捕获之外进行。

---

## 7. 下一步学习建议

- **进入请求生命周期**：本讲是 [u2-l3 请求端到端流转](./u2-l3-request-end-to-end.md) 与 [u3-l1 Scheduler 核心与事件循环](./u3-l1-scheduler-event-loop.md) 的直接前置——理解了「子系统通过 `get_schedule()`/`get_parallel()` 读配置、通过 `get_context().override()` 改配置」，你就能看懂调度器初始化里那一堆访问器调用。
- **精读消费方**：带着本讲的地图去读 [scheduler.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py) 的 `__init__` 与 `init_chunked_prefill`，验证每个 `get_*()` 调用都对应一个你认识的命名空间袋。
- **进阶阅读**：若你打算改 `server_args` / 模型覆盖 / 模块级状态 / 单次前向状态，务必先加载项目内的 `sglang-runtime-context` skill（`.claude/skills/sglang-runtime-context/SKILL.md`），里面有 CI 守卫（strict mutation guard、各种 ratchet 测试）和「load-time vs resolution-time」等硬核陷阱。
- **测试视角**：读 `test/registered/unit/test_runtime_context.py`——它兼作每个分层语义的「可执行文档」。
