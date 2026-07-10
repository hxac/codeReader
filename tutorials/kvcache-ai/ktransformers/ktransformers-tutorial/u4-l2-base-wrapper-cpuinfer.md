# BaseMoEWrapper 与 CPUInfer 引擎

## 1. 本讲目标

上一讲（[u4-l1](./u4-l1-ktmoewrapper-factory.md)）我们看到 `KTMoEWrapper` 是一个工厂，它按 `method` 把请求分发给 `AMXMoEWrapper`、`NativeMoEWrapper`、`LlamafileMoEWrapper`、`GeneralMoEWrapper` 等后端子类。这些后端虽然各做各的权重加载与量化，但它们都继承自同一个基类 `BaseMoEWrapper`。

本讲我们就钻进这个基类，搞清楚所有后端「共享的那一层基础设施」。学完后你应当能够：

- 说清 **CPUInfer 单例**是什么、为什么所有 MoE 层共用同一个，以及它「先到先得」的配置规则。
- 看懂 **`WorkerPoolConfig`** 三个字段 `subpool_count` / `subpool_numa_map` / `subpool_thread_count` 的含义，并能手算给定总线程数时每个 NUMA 子池分到几条线程。
- 了解 **`_validate_base_config`** 校验的五条约束，以及它在当前代码里到底被谁调用。
- 把 `BaseMoEWrapper.__init__` 的初始化流程串起来，明白从构造参数到 `self.cpu_infer` 的完整路径。

## 2. 前置知识

在阅读本讲前，建议你先建立以下概念（均来自前序讲义）：

- **MoE 与专家**：混合专家模型里，每个 token 只会激活少数几个「专家」（一组 FFN 权重）。KTransformers 把热专家放 GPU、冷专家放 CPU，CPU 侧专家的计算由本讲的 CPUInfer 引擎驱动（见 [u1-l1](./u1-l1-project-overview.md)）。
- **`KTMoEWrapper` 工厂**：调用它返回的是后端子类实例，而这些子类都继承 `BaseMoEWrapper`（见 [u4-l1](./u4-l1-ktmoewrapper-factory.md)）。
- **CPU 指令集变体**：`import kt_kernel` 时会按本机能力选择一个 `.so` 变体，`kt_kernel_ext` 即为加载后的 C++ 扩展模块（见 [u2-l3](./u2-l3-cpu-variant-detect.md)）。
- **NUMA**：多插槽服务器里，每个 CPU 插槽对应一个 NUMA 节点，访问「本地」内存比「远端」内存快得多。把线程和它要读的权重绑在同一个 NUMA 节点上，能大幅提升内存带宽利用率。

还有一个贯穿全讲的软件设计概念需要先讲清：

> **单例（Singleton）**：一种「全局只有一个实例」的设计模式。本讲里，无论模型有多少层 MoE、每层构造多少个后端 wrapper，整个进程只会创建**一个** `CPUInfer` 对象来管理 CPU 线程池。好处是避免线程爆炸与资源重复申请；代价是「第一个」wrapper 的配置会决定全局。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `kt-kernel/python/experts_base.py` | 本讲主角。定义 `_MoEBase`（共享基类）、`BaseMoEWrapper`（推理后端基类）、`KExpertsCPUBuffer`（CPU 缓冲区）等。 |
| `kt-kernel/python/experts.py` | 工厂层。回顾 `KTMoEWrapper.__new__` 如何分发到后端、后端如何 `super().__init__()` 进入本讲的基类。 |
| `kt-kernel/cpu_backend/worker_pool.h` | C++ 侧 `WorkerPoolConfig` 结构体定义，是 Python 三个 `subpool_*` 字段的最终落点。 |
| `kt-kernel/cpu_backend/cpuinfer.h` | C++ 侧 `CPUInfer` 类定义，说明单例真正持有的线程池与任务队列。 |
| `kt-kernel/ext_bindings.cpp` | pybind11 绑定，把 `WorkerPoolConfig` / `CPUInfer` 暴露给 Python。 |

## 4. 核心概念与源码讲解

### 4.1 CPUInfer 单例

#### 4.1.1 概念说明

一个几百 B 的 MoE 模型可能有几十上百层，每层又有几十上百个专家。如果在每一层都为 CPU 侧专家「新开一个线程池」，进程里就会冒出成千上万条线程，彼此抢核、抢内存带宽，NUMA 亲和性也无从谈起。

KTransformers 的做法是：**整个进程共享一个 `CPUInfer` 引擎**。这个引擎内部持有一个 `WorkerPool`（线程池）和一个 `TaskQueue`（任务队列）。所有 MoE 层在需要算 CPU 专家时，都把任务「提交（submit）」到这同一个引擎里排队执行。

这个单例由共享基类 `_MoEBase` 管理：

#### 4.1.2 核心流程

```text
任意后端 wrapper __init__
        │
        ▼
BaseMoEWrapper.__init__  ──►  self._get_cpu_infer(threads, count, numa_nodes)
        │                              │
        │              ┌───────────────┴───────────────┐
        │              ▼                                ▼
        │   _cpu_infer_instance is None?     否 → 直接返回旧实例（忽略本次参数）
        │              │ 是
        │              ▼
        │   用本次参数构造 WorkerPoolConfig
        │              │
        │              ▼
        │   kt_kernel_ext.CPUInfer(config)  →  存入 _cpu_infer_instance
        │              │
        └──────────────┴────────────►  返回这个全局唯一的 CPUInfer
```

关键点有两个：

1. **「先到先得」**：单例只在第一次被请求时创建。第一个 wrapper 传入的 `cpuinfer_threads` / `threadpool_count` / `numa_nodes` 会固化成全局配置；此后所有层再传这些参数，**都会被忽略**。实际使用中每层都传同一套配置，所以不会出问题，但理解这一点很重要——改线程配置必须在「第一个 wrapper 构造前」生效。
2. **跨推理/SFT 共享**：`_cpu_infer_instance` 是挂在 `_MoEBase` 上的类属性，推理基类 `BaseMoEWrapper` 和 SFT 基类 `BaseSFTMoEWrapper` 都继承自它，因而共用同一个槽位（当然，一次通常只跑推理或只跑 SFT，不会冲突）。

#### 4.1.3 源码精读

单例槽位与获取方法都位于 `_MoEBase` 内：

[kt-kernel/python/experts_base.py:145-156](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L145-L156) —— `_MoEBase` 类定义及类属性 `_cpu_infer_instance = None`，这就是「全局唯一」的那个槽位。

[kt-kernel/python/experts_base.py:158-198](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L158-L198) —— `_get_cpu_infer` 方法，先判断 `None`，再构造配置，最后 `kt_kernel_ext.CPUInfer(worker_config)` 创建引擎。其骨架为：

```python
if cls._cpu_infer_instance is None:
    worker_config = kt_kernel_ext.WorkerPoolConfig()
    # ... 填充三个字段（见 4.2）...
    cls._cpu_infer_instance = kt_kernel_ext.CPUInfer(worker_config)
return cls._cpu_infer_instance
```

推理基类在 `__init__` 末尾调用它，把结果挂到实例上：

[kt-kernel/python/experts_base.py:313](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L313) —— `self.cpu_infer = self._get_cpu_infer(cpuinfer_threads, threadpool_count, numa_nodes=numa_nodes)`。

到了 C++ 侧，`CPUInfer` 的构造函数才真正「起线程」。注意它接收 `WorkerPoolConfig`：

[kt-kernel/cpu_backend/cpuinfer.h:55-62](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h#L55-L62) —— `CPUInfer(WorkerPoolConfig config)`，内部 `backend_ = new WorkerPool(config)`（建线程池）并 `task_queue_ = new TaskQueue()`（建任务队列），还预计算了一张 f16→f32 查表。

引擎建好后，Python 通过绑定层暴露的接口提交/同步任务：

[kt-kernel/ext_bindings.cpp:490-501](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp#L490-L501) —— `CPUInfer` 暴露给 Python 的 `submit` / `sync` / `submit_with_cuda_stream` / `sync_with_cuda_stream`。其中 `submit_with_cuda_stream` 把 CPU 计算挂到 CUDA 流上调度，是 CPU-GPU 异构流水线的关键（其细节留到 [u4-l3](./u4-l3-cpu-buffer-async-forward.md) 与 [u6-l4](./u6-l4-deferred-experts-pipelining.md)）。

#### 4.1.4 代码实践

**实践目标**：在不依赖大模型的前提下，亲眼看一次「单例只建一次」的行为。

**操作步骤**：

1. 打开 [experts_base.py:158-198](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L158-L198)，确认 `_get_cpu_infer` 里只有 `is None` 这一个分支会创建实例。
2. 阅读构造点 [cpuinfer.h:55-62](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h#L55-L62)，注意构造时打印了 `CPUInfer[0x...]: Hello`，析构时打印 `Goodbye`——这是单例「生死」的可见信号。
3. 若本地已 `pip install` 过 kt-kernel，可在一个干净进程里连续构造两个同层 wrapper（用合法的最小参数），观察终端只打印**一次** `Hello`。

**预期结果**：无论构造多少个 wrapper，`CPUInfer[0x...]: Hello` 只出现一次，说明所有层共享同一个引擎。

> 待本地验证：第 3 步需要编译好的 `kt_kernel_ext` 与合法的构造参数；若环境不具备，仅完成第 1、2 步的源码阅读同样达成目标。

#### 4.1.5 小练习与答案

**练习 1**：如果某次推理你希望把线程数从 64 改成 128，但在「第 5 层 wrapper 构造时」才传入 `cpuinfer_threads=128`，会生效吗？

**参考答案**：不会。单例在第 0 层（第一个 wrapper）构造时就已经用当时的值创建好了，后续 wrapper 传入的 `cpuinfer_threads` 会被 `_get_cpu_infer` 直接忽略。正确的做法是在「任何 wrapper 构造之前」就通过配置/命令行参数定好这个值。

---

### 4.2 WorkerPoolConfig：把线程切成 NUMA 子池

#### 4.2.1 概念说明

光有「一个线程池」还不够。多 NUMA 节点的服务器上，如果把所有线程塞进一个扁平池子，线程会跨节点访问远端内存，带宽利用率大跌。KTransformers 的做法是把总线程数按 NUMA 节点**切成多个子池（subpool）**，每个子池绑定到一个 NUMA 节点，专吃该节点本地内存里的专家权重。

这个「怎么切」的描述，就是 `WorkerPoolConfig` 的三件套：

- `subpool_count`：子池个数（通常等于 NUMA 节点数，也即张量并行 TP 数）。
- `subpool_numa_map`：长度等于 `subpool_count` 的列表，第 i 个子池绑定到哪个 NUMA 节点。
- `subpool_thread_count`：长度等于 `subpool_count` 的列表，第 i 个子池给几条线程。

#### 4.2.2 核心流程

`_get_cpu_infer` 把 Python 侧的两个参数 `cpuinfer_threads`（总线程数 T）和 `threadpool_count`（子池数 N），加上可选的 `numa_nodes`，翻译成上面三件套：

1. **NUMA 映射**：
   - 若 `numa_nodes is None`：默认 `subpool_numa_map = list(range(N))`，即第 i 个子池→NUMA 节点 i（`[0, 1, ..., N-1]`）。
   - 若显式给了 `numa_nodes`：要求其长度必须等于 N，否则抛 `ValueError`；直接用它做映射。
2. **线程切分**：把 T 尽量均匀地分到 N 个子池，**余数从前往后各分一条**。第 i 个子池的线程数为：

\[
\text{threads}_i = \left\lfloor \frac{T}{N} \right\rfloor + \begin{cases} 1, & i < T \bmod N \\ 0, & \text{otherwise} \end{cases}
\]

3. **装配**：`subpool_count = N`，把上面两个列表填入，交给 `CPUInfer(config)`。

举个例子，`T=65, N=4`：\(65 = 16\times4 + 1\)，余数为 1，所以前 1 个子池拿 17 条、其余拿 16 条，结果 `subpool_thread_count = [17, 16, 16, 16]`，`subpool_numa_map = [0, 1, 2, 3]`。

#### 4.2.3 源码精读

[kt-kernel/python/experts_base.py:176-198](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L176-L198) —— `_get_cpu_infer` 的配置装配主体。其中：

```python
if numa_nodes is not None:
    if len(numa_nodes) != threadpool_count:
        raise ValueError(...)            # 长度必须匹配
    subpool_numa_map = list(numa_nodes)
else:
    subpool_numa_map = list(range(threadpool_count))  # 默认顺序映射

subpool_thread_count = [
    cpuinfer_threads // threadpool_count
    + (1 if i < cpuinfer_threads % threadpool_count else 0)
    for i in range(threadpool_count)
]
```

这两段正是 NUMA 映射与「余数前补」线程切分的实现。

C++ 侧的 `WorkerPoolConfig` 结构体只有这三个字段，与 Python 一一对应：

[kt-kernel/cpu_backend/worker_pool.h:132-136](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/worker_pool.h#L132-L136) —— `struct WorkerPoolConfig { int subpool_count; std::vector<int> subpool_numa_map; std::vector<int> subpool_thread_count; };`。

绑定层用 `def_readwrite` 把这三个字段双向暴露给 Python，因此 Python 可以先 `cfg = WorkerPoolConfig()` 再逐字段赋值：

[kt-kernel/ext_bindings.cpp:484-488](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp#L484-L488) —— `WorkerPoolConfig` 的 pybind 绑定。

#### 4.2.4 代码实践

**实践目标**：阅读 `_get_cpu_infer`，手算 `cpuinfer_threads=64`、`threadpool_count=2`、`numa_nodes=None` 时三个字段的取值，并解释默认 NUMA 映射。

**操作步骤**：

1. 打开 [experts_base.py:176-198](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L176-L198)。
2. 代入 `T=64, N=2`：\(64 \div 2 = 32\)，余数 \(64 \bmod 2 = 0\)，于是两个子池各 32 条线程。
3. `numa_nodes=None` → 走 `else` 分支，`subpool_numa_map = list(range(2)) = [0, 1]`。
4. 用下面这段**示例代码**（非项目代码，纯 Python 复现切分公式）核对你的手算结果：

```python
# 示例代码：复现 _get_cpu_infer 的切分逻辑，仅用于验证手算
def split(cpuinfer_threads, threadpool_count, numa_nodes=None):
    N = threadpool_count
    if numa_nodes is not None:
        assert len(numa_nodes) == N, "长度必须匹配"
        subpool_numa_map = list(numa_nodes)
    else:
        subpool_numa_map = list(range(N))
    subpool_thread_count = [
        cpuinfer_threads // N + (1 if i < cpuinfer_threads % N else 0)
        for i in range(N)
    ]
    return N, subpool_numa_map, subpool_thread_count

print(split(64, 2))          # (2, [0, 1], [32, 32])
print(split(65, 4))          # (4, [0, 1, 2, 3], [17, 16, 16, 16])
```

**预期结果**：`split(64, 2)` 输出 `(2, [0, 1], [32, 32])`。也就是说每个子池 32 条线程，分别绑定到 NUMA 节点 0 与 1。

**默认映射解释**：`numa_nodes=None` 时，代码假设「第 i 个子池就放在 NUMA 节点 i」。这对大多数「NUMA 节点编号恰好从 0 连续」的双路/四路服务器是成立的；如果你的机器 NUMA 拓扑特殊（例如想跳过某个节点），就显式传 `numa_nodes=[...]`。

#### 4.2.5 小练习与答案

**练习 1**：`cpuinfer_threads=65, threadpool_count=4` 时，`subpool_thread_count` 与 `subpool_numa_map` 各是多少？

**参考答案**：\(65 = 16\times4 + 1\)，余数 1，前 1 个子池 +1。`subpool_thread_count = [17, 16, 16, 16]`；`numa_nodes=None` 时 `subpool_numa_map = [0, 1, 2, 3]`。

**练习 2**：若调用方传入 `numa_nodes=[3, 5]` 但 `threadpool_count=3`，会发生什么？

**参考答案**：`len([3,5])=2 ≠ 3`，触发 `ValueError`，提示 numa_nodes 长度必须等于 threadpool_count。这是 `_get_cpu_infer` 里唯一的长度校验。

---

### 4.3 配置校验与 BaseMoEWrapper 初始化

#### 4.3.1 概念说明

`_MoEBase` 还提供了两个共享能力：一个静态校验方法 `_validate_base_config`，以及由推理基类 `BaseMoEWrapper` 实现的初始化流程。

需要先澄清一个容易踩坑的事实：**`_validate_base_config` 定义在共享基类 `_MoEBase` 上，推理基类 `BaseMoEWrapper` 和 SFT 基类 `BaseSFTMoEWrapper` 都能继承到它，但目前在「推理」路径里并没有被调用——只有 SFT 基类在 `__init__` 里主动调用了它**（见 [kt-kernel/python/sft/base.py:151](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/sft/base.py#L151)）。因此本节把它当作「共享的、可供两条路径复用的基础校验」来理解它的约束含义即可，不要误以为推理 wrapper 一构造就会跑这些检查。

#### 4.3.2 核心流程

`_validate_base_config` 对四个结构参数做「合理性下界」检查，共五条：

| # | 约束 | 触发的 `ValueError` |
| --- | --- | --- |
| 1 | `num_experts > 0` | 专家数必须为正 |
| 2 | `hidden_size > 0` | 隐藏维必须为正 |
| 3 | `moe_intermediate_size > 0` | 中间维必须为正 |
| 4 | `num_experts_per_tok > 0` | 每个 token 激活的专家数（top-k）必须为正 |
| 5 | `num_experts_per_tok <= num_experts` | top-k 不能超过专家总数 |

这些都是「物理上不可能为 0/负数」的硬约束，属于最早期的 fail-fast：把错误参数挡在权重加载和引擎创建之前。

而 `BaseMoEWrapper.__init__` 做的事可以概括为「存配置 → 处理 GPU 掩码 → 拿单例 → 留后端钩子」：

```text
__init__(layer_idx, num_experts, ..., gpu_experts_mask, cpuinfer_threads,
         threadpool_count, weight_path, ..., numa_nodes, swiglu_limit)
   │
   ├─ 存基本结构参数（layer_idx/num_experts/hidden_size/...）
   ├─ 处理 gpu_experts_mask：转成 pinned 的 bool 张量，并算出 num_gpu_experts
   ├─ 初始化跨层延迟状态 _layer_has_pending_deferred[layer_idx] = False
   ├─ 记录 method / swiglu_limit
   ├─ self.cpu_infer = self._get_cpu_infer(...)   ← 取/建 CPUInfer 单例（4.1）
   └─ self.moe = None                              ← 留给后端子类填充
```

#### 4.3.3 源码精读

[kt-kernel/python/experts_base.py:200-224](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L200-L224) —— `_validate_base_config`，五条 `if ... raise ValueError` 即上表五条约束。

[kt-kernel/python/experts_base.py:227-316](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L227-L316) —— `BaseMoEWrapper(_MoEBase, ABC)` 的 `__init__`。其中 GPU 掩码处理值得单独看：

[kt-kernel/python/experts_base.py:282-292](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L282-L292) —— 把 `gpu_experts_mask` 转成**锁页（pinned）**的 CPU bool 张量并统计 `num_gpu_experts`。锁页内存是为了后续与 GPU 之间做异步拷贝（见 [u4-l4](./u4-l4-gpu-expert-mask.md)）。

[kt-kernel/python/experts_base.py:304-316](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L304-L316) —— 设置跨层延迟状态、记录 `swiglu_limit`（仅 MXFP4/MXFP8 路径生效，见 [u4-l1](./u4-l1-ktmoewrapper-factory.md)），调用 `_get_cpu_infer` 拿到单例，并把 `self.moe` 置 `None`（等子类在 `load_weights` 里填）。

顺带回顾工厂层如何进入这里：`_create_inference_wrapper` 选好 `backend_cls` 后，会把全部参数（含 `cpuinfer_threads`、`threadpool_count`、`numa_nodes`）透传给后端构造函数：

[kt-kernel/python/experts.py:336-383](https://github.com/kvcache-ai-ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L336-L383) —— 后端选择与构造；后端 `__init__`（如 `AMXMoEWrapper`）先做完自己的指令集可用性检查，再 `super().__init__(...)` 进入本讲的 `BaseMoEWrapper.__init__`（可对照 [kt-kernel/python/utils/amx.py:282-297](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/amx.py#L282-L297)）。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `_validate_base_config` 的五条断言，理解它挡住了哪些「非法模型结构」。

**操作步骤**：

1. 打开 [experts_base.py:200-224](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L200-L224)，逐条核对五条约束。
2. 再打开 [sft/base.py:151-156](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/sft/base.py#L151-L156)，确认目前真正调用它的位置。
3. 自检：构造一个「top-k 大于专家总数」的假想配置，例如 `num_experts=4, num_experts_per_tok=8`，判断 `_validate_base_config` 会因第 5 条约束而抛错。

**预期结果**：第 3 步会命中 `num_experts_per_tok (8) cannot exceed num_experts (4)` 的 `ValueError`。

> 说明：因为推理 `BaseMoEWrapper.__init__` 当前不调用此方法，要真正触发它需走 SFT 路径或在自定义代码里显式调用。本实践以「读懂断言」为主。

#### 4.3.5 小练习与答案

**练习 1**：`_validate_base_config` 会校验「权重文件是否存在」「moe_intermediate_size 能否被 TP 整除」吗？

**参考答案**：不会。它只做四个结构参数的正数性与 top-k 上界这五条最基础的检查。TP 整除性这类后端专属约束由具体后端自行校验（例如 Llamafile 后端要求 `moe_intermediate_size` 能被 `QK_K=256` 切分，见 [u5-l2](./u5-llamafile-backend.md)）。

**练习 2**：为什么把 `_validate_base_config` 放在共享基类 `_MoEBase`，而不是各自在推理/SFT 基类里各写一份？

**参考答案**：因为推理与 SFT 都面对「同样的 MoE 结构参数」，五条约束对两条路径完全一致，放在共享基类可避免重复。这正是 `_MoEBase` 作为「推理与 SFT 共享底座」的设计意图（见类注释 [experts_base.py:146-154](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L146-L154)）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从工厂到单例」的完整追踪：

1. **起点**：阅读 [experts.py:120-228](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L120-L228) 的 `KTMoEWrapper.__new__`，确认它校验 `mode`/`method` 后把请求导向 `_create_inference_wrapper`。
2. **分发**：在 [experts.py:336-346](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L336-L346) 选定一个后端类（例如 `method="AMXINT8"` → `AMXMoEWrapper`）。
3. **进基类**：跟踪后端 `__init__` 的 `super().__init__(...)` 进入 [experts_base.py:235-316](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L235-L316) 的 `BaseMoEWrapper.__init__`。
4. **建引擎**：定位其中第 313 行的 `_get_cpu_infer`，跳到 [experts_base.py:158-198](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L158-L198)，画出 `cpuinfer_threads=64 / threadpool_count=2 / numa_nodes=None` 时 `WorkerPoolConfig` 三个字段的最终值。
5. **落 C++**：顺着 [ext_bindings.cpp:484-501](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp#L484-L501) 看到 `WorkerPoolConfig` 与 `CPUInfer` 的绑定，最终落到 [cpuinfer.h:55-62](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h#L55-L62) 起线程。

**交付物**：一张标注了「Python 参数 → WorkerPoolConfig 三字段 → CPUInfer/WorkerPool」的数据流图，并写明 `T=64, N=2` 时的取值（`subpool_count=2`，`subpool_numa_map=[0,1]`，`subpool_thread_count=[32,32]`）。

## 6. 本讲小结

- 所有推理后端都继承 `BaseMoEWrapper`，而真正共享的底座是 `_MoEBase`，它持有**全局唯一的 `CPUInfer` 单例**（`_cpu_infer_instance`）。
- 单例遵循「先到先得」：第一个 wrapper 构造时传入的线程/NUMA 配置决定全局，之后所有层复用同一引擎、忽略自身参数。
- `WorkerPoolConfig` 三件套——`subpool_count`（子池数）、`subpool_numa_map`（子池→NUMA 节点）、`subpool_thread_count`（每子池线程数）——把总线程按 NUMA 节点切成多个子池；线程按「余数前补」均匀分配。
- `numa_nodes=None` 时默认顺序映射 `[0,1,...,N-1]`；显式给 `numa_nodes` 则要求长度等于 `threadpool_count`，否则报错。
- `_validate_base_config` 提供五条结构参数下界检查，定义在共享基类、当前由 SFT 路径调用；推理路径靠各后端自行校验。
- `BaseMoEWrapper.__init__` 的脉络是：存参数 → 处理 GPU 掩码（转 pinned bool、算 num_gpu_experts）→ 取单例 → `self.moe=None` 留给子类。

## 7. 下一步学习建议

- 想了解拿到 `cpu_infer` 之后如何**异步提交前向、用双缓冲与 CUDA 流做 CPU-GPU 流水线**，请继续 [u4-l3 CPU 缓冲区与异步前向](./u4-l3-cpu-buffer-async-forward.md)。
- 想搞清 `gpu_experts_mask` 是怎么按激活频率生成、如何决定哪些专家上 GPU，请看 [u4-l4 GPU 专家掩码与放置](./u4-l4-gpu-expert-mask.md)。
- 想理解 NUMA 子池在 C++ 层的真实实现（`InNumaPool`、`NumaJobDistributor`、hwloc 绑核），可预习 [u6-l3 NUMA 感知线程池](./u6-l3-numa-thread-pool.md)，并直接阅读 [cpu_backend/worker_pool.h](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/worker_pool.h) 与 [cpu_backend/worker_pool.cpp](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/worker_pool.cpp)。
