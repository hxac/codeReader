# CPU 缓冲区与异步前向

## 1. 本讲目标

在上一讲（u4-l2）里，我们已经知道所有 MoE 推理后端共享同一个 `CPUInfer` 单例引擎，CPU 专家的计算任务会被提交到引擎的 `WorkerPool` 上执行。但还有一个关键问题没有回答：**GPU 上的 token 隐藏状态是怎么交到 CPU 去算、算完又是怎么交回 GPU 的？这两步搬运如何与 GPU 上的其它计算重叠，而不是互相等待？**

本讲围绕 `KExpertsCPUBuffer` 和 `BaseMoEWrapper` 的三个异步前向方法，回答这个问题。学完本讲你应当能够：

1. 说清 `KExpertsCPUBuffer` 为什么要用 **pinned memory（页锁定内存）**、为什么要做 **双缓冲（`buffer_depth=2`）**，以及 `capture_bs`（捕获批大小）的缓存作用。
2. 描述 `submit_forward` 的**非阻塞执行模型**：它如何把数据搬到 CPU、如何把 CPU 的前向任务"挂"到 CUDA 流上，提交后立刻返回而**不等待**算完。
3. 解释 `sync_forward` 如何通过 `cudaLaunchHostFunc` 把同步点排进 CUDA 流、`allow_pending` 参数如何放行延迟专家任务，以及 `forward = submit + sync` 的同步快捷写法。

> 本讲只讲"缓冲区与异步前向"这条机制。其中"延迟专家（deferred experts）"的完整策略（`select_deferred_experts`、`max_deferred_experts_per_token`、流水线收益）属于 u6-l4，本讲只在它**与本讲双缓冲流转相关**的地方点到为止。

---

## 2. 前置知识

在进入源码前，先建立三个直觉概念。它们是理解本讲的前提。

### 2.1 pinned memory（页锁定内存）与非阻塞拷贝

普通 CPU 内存可以被操作系统换页（swap），地址会"漂移"，所以 GPU 无法安全地直接对它做 DMA 搬运。**pinned memory** 是被锁定在物理内存里、保证不会被换页的内存，GPU 可以对它做**异步（non-blocking）拷贝**——拷贝发起后 CPU 线程不必等它完成就能继续干别的活。

在 PyTorch 里，`torch.zeros(..., device="cpu", pin_memory=True)` 分配的就是 pinned memory；而 `tensor.copy_(src, non_blocking=True)` 当**目标或源是 pinned memory** 时，才会真正走异步 DMA。本讲会反复看到这对组合：**先 pin，再 `non_blocking=True` 拷贝**——这正是 GPU↔CPU 数据能"不阻塞"地流转的物理基础。

### 2.2 CUDA 流（stream）与 cudaLaunchHostFunc

CUDA 流是一个**有序**的命令队列：排在前面的命令完成后，后面的才会开始。GPU kernel、显存拷贝都排进流里顺序执行。

`cudaLaunchHostFunc(stream, func, args)` 是一个特殊的 CUDA API：它把一段**在 CPU 上执行的函数 `func`** 当成一个"任务"插进 `stream`。当流推进到这个位置时，CUDA 运行时会用一个专用线程去调用 `func`。也就是说：

- 把 CPU 计算用 `cudaLaunchHostFunc` 挂到流上，CPU 计算就和 GPU 计算拥有了**统一的流顺序**；
- 谁先挂谁先执行，天然形成"GPU 做完 A → CPU 做 B → GPU 做 C"的流水线，**不需要手动插 event**。

本讲里 CPU 的 MoE 前向、以及"等 CPU 算完"的同步，都是用 `cudaLaunchHostFunc` 挂到同一条 CUDA 流上实现的。

### 2.3 双缓冲（double buffering）

如果 GPU 算第 N 层、CPU 同时在算第 N-1 层的结果，它们不能写同一块输出内存，否则会互相覆盖。**双缓冲**就是准备**两块**输出缓冲区，让相邻两层交替使用不同的一块，从而允许两层计算在时间上重叠而不冲突。本讲的 `buffer_depth = 2` 就是双缓冲。

> 名词速查：pinned memory（页锁定内存）、non-blocking copy（异步拷贝）、CUDA stream（CUDA 流）、`cudaLaunchHostFunc`（把 CPU 函数排入流）、double buffer（双缓冲）、slot（缓冲槽位）。若对 `CPUInfer` 单例与 `WorkerPool` 不熟，请先读 u4-l2。

---

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
|------|------|
| [kt-kernel/python/experts_base.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py) | 本讲主角。包含 `KExpertsCPUBuffer`（缓冲区管理）和 `BaseMoEWrapper` 的 `submit_forward` / `sync_forward` / `forward` 三个方法。 |
| [kt-kernel/cpu_backend/cpuinfer.h](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h) | C++ 侧 `CPUInfer` 引擎，定义 `submit_with_cuda_stream` 和 `sync_with_cuda_stream`，用 `cudaLaunchHostFunc` 把 CPU 任务挂到 CUDA 流上。 |
| [kt-kernel/ext_bindings.cpp](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp) | pybind11 绑定层，把 `CPUInfer` 的两个流同步方法与 MoE 类的 `forward_task` 暴露给 Python。 |

本讲涉及的方法（都在 `experts_base.py` 中）：

| 方法 | 性质 | 一句话职责 |
|------|------|-----------|
| `KExpertsCPUBuffer.get_buffer` | 类方法 | 按 batch size 取/建一组 pinned memory 双缓冲。 |
| `BaseMoEWrapper.submit_forward` | 非阻塞 | 把输入搬到 CPU，把 CPU 前向任务挂到 CUDA 流上，立即返回。 |
| `BaseMoEWrapper.sync_forward` | 同步点 | 在 CUDA 流上等 CPU 算完，把结果拷回 GPU 并返回。 |
| `BaseMoEWrapper.forward` | 同步封装 | `submit_forward` + `sync_forward` 的顺序组合快捷写法。 |
| `set_capture_batch_sizes` | 静态方法 | 声明要预先缓存缓冲的批大小清单。 |

---

## 4. 核心概念与源码讲解

### 4.1 缓冲区管理：KExpertsCPUBuffer 与 pinned memory 双缓冲

#### 4.1.1 概念说明

`KExpertsCPUBuffer` 是一个**纯数据管家**，它自己不做任何计算，只负责"按需分配并复用一组 pinned memory 缓冲区"。

为什么需要这么一个管家？

- MoE 推理每一层、每个 token 都要在 GPU 和 CPU 之间来回搬运数据。如果每次都临时 `torch.zeros` 分配内存，不仅慢，而且分配的是**可换页**的普通内存，GPU 没法对它做异步拷贝。
- 推理过程中 batch size 往往是几种固定值（decode 阶段常常是 1，prefill 阶段常常是若干预设 chunk 大小）。把这些"常用 batch size"对应的缓冲区**预先建好并缓存**，后续命中就能零开销复用。

`KExpertsCPUBuffer` 的设计正是围绕这两点：用 pinned memory 保证可异步搬运，用缓存避免重复分配。

#### 4.1.2 核心流程

`get_buffer(hidden_states, num_experts_per_tok)` 的决策树：

```text
                 输入 hidden_states（GPU 张量，shape=[batch, hidden]）
                              │
            ┌─────────────────┴──────────────────┐
            ▼                                     ▼
   batch 命中 capture_buffers?            batch == temp_bs?
   （预先捕获并长期缓存）                  （上一次临时建的那组）
            │ 是                                  │ 是
            ▼                                     ▼
      返回缓存的那组                          返回临时那组
            │ 否                                  │ 否
            ▼                                     ▼
                  新建一组 buffer_depth=2 的 pinned 缓冲
                              │
                 ┌────────────┴────────────┐
                 ▼                          ▼
        batch ∈ capture_bs?        记为 temp_bs/temp_buffer
        是 → 存入 capture_buffers  （不存，仅本次复用）
                 │
                 ▼
              返回这组新缓冲
```

要点：

- "一组缓冲"是一个 **7 元组**，里面每一项又都是一个长度为 `buffer_depth`（=2）的列表，也就是"双缓冲"的体现。
- `capture_bs`：被声明为"长期捕获"的批大小清单，命中后缓冲会被永久缓存（`capture_buffers` 字典）。
- `temp_bs` / `temp_buffer`：只缓存**最近一次**临时新建的批大小，是一种"LRU=1"的轻量缓存，给那些不在 `capture_bs` 里但短时间内反复出现的 batch size 复用机会。

#### 4.1.3 源码精读

先看类属性，它们决定了缓冲的行为：

[kt-kernel/python/experts_base.py:82-86](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L82-L86) —— 这段定义了缓冲池的核心状态：`capture_bs` 是要捕获的批大小列表，`capture_buffers` 是命中后长期保管的字典，`temp_bs/temp_buffer` 是"只留最近一次"的临时缓存，`buffer_depth=2` 是双缓冲深度。

再看 `get_buffer` 怎么按 batch size 复用或新建。先看缓存命中逻辑：

[kt-kernel/python/experts_base.py:88-98](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L88-L98) —— 命中 `capture_buffers`（长期缓存）或 `temp_buffer`（临时缓存）就直接返回，避免重复分配。

未命中时，新建一组 7 元组，每一项都是长度为 `buffer_depth` 的列表（注意每一项都 `pin_memory=True`）：

[kt-kernel/python/experts_base.py:100-137](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L100-L137) —— 新建 7 类缓冲，每类两份：输入张量、立即专家 id、延迟专家 id（初值 -1）、路由权重、CPU 输出、batch size 标量、GPU 输出。全部 pinned（GPU 输出除外，它在 GPU 上）。

最后决定是否长期缓存：

[kt-kernel/python/experts_base.py:138-142](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L138-L142) —— 只有当当前 batch 在 `capture_bs` 名单里，才把缓冲存进 `capture_buffers` 长期保管；否则只更新临时缓存 `temp_bs/temp_buffer`。

7 类缓冲各自的用途（对照上面的新建代码）：

| 字段（每项长度=2） | dtype / 设备 | 用途 |
|------|------|------|
| `input_tensor_cpu` | bfloat16 / pinned | 收 GPU 传来的输入 hidden states |
| `immediate_experts_ids_cpu` | long / pinned | 本层**立即**要算的专家 id |
| `deferred_experts_ids_cpu` | long / pinned，初值 -1 | 本层**延迟**到下一槽算的专家 id |
| `weights_cpu` | float32 / pinned | 路由权重（专家加权） |
| `output_cpu` | bfloat16 / pinned | CPU 算完的输出，等拷回 GPU |
| `bsz_tensor_cpu` | int32 / pinned，存 batch size | 告诉 C++ 核这次处理多少 token |
| `output_gpu` | hidden.dtype / GPU | 结果落地的 GPU 张量 |

> **为什么要两份（`buffer_depth=2`）？** 因为相邻两层会交替使用不同槽位（见 4.2.2 的槽位映射），两份足以让两层输出共存而不冲突。这是本讲综合实践要重点回答的问题。

配置入口 `set_capture_batch_sizes`（由 `BaseMoEWrapper` 静态方法转发）：

[kt-kernel/python/experts_base.py:507-521](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L507-L521) —— 把要长期缓存的批大小清单写入 `KExpertsCPUBuffer.capture_bs`。配好后，这些 batch size 第一次出现时会分配并缓存，之后命中即复用。

配套还有查询和清理：

[kt-kernel/python/experts_base.py:523-543](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L523-L543) —— `get_capture_batch_sizes` 查询当前清单，`clear_buffer_cache` 清空 `capture_buffers` 与临时缓存，用于释放内存或重置状态。

#### 4.1.4 代码实践

**实践目标**：通过源码追踪 + 一段纯 Python 模拟，验证 `get_buffer` 的"命中即复用"行为与 `buffer_depth=2` 的双缓冲结构，而无需真正编译运行 C++ 扩展。

**操作步骤**：

1. 阅读上面三处源码，确认：缓存命中优先级是 `capture_buffers` > `temp_buffer` > 新建。
2. 下面这段**示例代码**（非项目代码，纯模拟）复刻了缓存命中逻辑，你可以直接运行观察"是否复用了同一对象"：

```python
# 示例代码：模拟 KExpertsCPUBuffer.get_buffer 的命中与复用逻辑
class FakeBuffer:
    capture_bs = []
    capture_buffers = {}
    temp_bs = 0
    temp_buffer = None
    buffer_depth = 2

    @classmethod
    def get_buffer(cls, batch_size):
        if batch_size in cls.capture_buffers:   # ① 长期缓存命中
            return cls.capture_buffers[batch_size], "hit-capture"
        if batch_size == cls.temp_bs:           # ② 临时缓存命中
            return cls.temp_buffer, "hit-temp"
        buf = ([0] * cls.buffer_depth, "new")   # ③ 新建一组双缓冲（这里用占位）
        result = (buf, "new")
        if batch_size in cls.capture_bs:        # 在名单里才长期保管
            cls.capture_buffers[batch_size] = result
        cls.temp_bs = batch_size
        cls.temp_buffer = result
        return result

FakeBuffer.capture_bs = [1, 2, 4, 8]            # 等价于 set_capture_batch_sizes([1,2,4,8])
print(FakeBuffer.get_buffer(4)[1])   # 第一次 4：new
print(FakeBuffer.get_buffer(4)[1])   # 第二次 4：hit-capture（在名单里，已长期缓存）
print(FakeBuffer.get_buffer(3)[1])   # 3 不在名单：new，且只进 temp
print(FakeBuffer.get_buffer(3)[1])   # 紧接着再 3：hit-temp
print(FakeBuffer.get_buffer(5)[1])   # 5 把 temp 顶替掉
print(FakeBuffer.get_buffer(3)[1])   # 3 已被 5 顶替，又变 new
```

**需要观察的现象**：第一次访问 batch=4 是 `new`，之后都是 `hit-capture`；batch=3 不在名单，只能享受"最近一次"的临时复用，一旦被 batch=5 顶替就要重新建。

**预期结果**：依次输出 `new, hit-capture, new, hit-temp, new, new`。

> 若想用真实对象验证：在本机安装好 kt-kernel 后，`import kt_kernel`，调用 `kt_kernel.experts_base.BaseMoEWrapper.set_capture_batch_sizes([1,2,4,8])`，再用 `KExpertsCPUBuffer.get_buffer` 对同一 batch size 取两次，比较 `id()` 是否相同。**真实运行待本地验证**（需要编译好的 `_kt_kernel_ext` 与一个 GPU 张量作输入）。

#### 4.1.5 小练习与答案

**练习 1**：如果推理过程中出现一个**既不在 `capture_bs` 名单、也不是上一次 batch size** 的批大小，`get_buffer` 会怎么做？

**答案**：缓存全部不命中，新建一组 `buffer_depth=2` 的 pinned 缓冲，因为不在名单所以**不**进入 `capture_buffers`，只更新 `temp_bs/temp_buffer`，供下一次同 batch 复用。

**练习 2**：`input_tensor_cpu` 为什么必须 `pin_memory=True`？

**答案**：因为它是 GPU→CPU 异步拷贝（`non_blocking=True`）的目的地；只有 pinned memory 才能被 GPU 安全地 DMA 异步写入，否则 `non_blocking=True` 会退化为阻塞拷贝，失去与 GPU 计算重叠的能力。

---

### 4.2 异步提交：submit_forward 的非阻塞执行模型

#### 4.2.1 概念说明

`submit_forward(hidden_states, topk_ids, topk_weights, cuda_stream)` 是异步前向的**发起端**。它的关键性质是：**调用返回时，CPU 专家计算通常还没开始或还没结束**——它只是把"要做的事"排队挂到了 `cuda_stream` 上，真正的执行交给流和 CPU 线程池。

它解决了两个问题：

1. **数据搬运**：把 GPU 上的输入、专家 id、权重异步拷到 pinned 的 CPU 缓冲。
2. **任务排队**：把"在 CPU 上跑 MoE 前向"这件事，用 `submit_with_cuda_stream` 挂到 CUDA 流上，使它与 GPU 上其它工作按流顺序协调，而 Python 线程立即继续。

为什么这种"提交即返回"很重要？因为 MoE 推理是逐层进行的，如果每层都要"等 CPU 算完才能进下一层"，CPU 和 GPU 就会**串行等待**，吞吐大降。异步提交让 GPU 可以在 CPU 算第 N 层的同时，去做第 N-1 层的收尾或第 N+1 层的准备，形成流水线。

#### 4.2.2 核心流程

`submit_forward` 的执行步骤（先忽略延迟专家，4.2.3 再补）：

```text
1. 把 hidden_states 拍平成 [batch, hidden]
2. 取一组双缓冲（按 batch size 命中或新建）
3. 算槽位：
        current_slot = layer_idx % buffer_depth   # 本层输出落点
        next_slot    = (layer_idx+1) % buffer_depth
4. 异步拷贝输入/权重/专家id → CPU 缓冲的 current_slot（non_blocking=True）
5. 读 incremental = 上一层是否有未完成的延迟专家
6. cpu_infer.submit_with_cuda_stream(stream, moe.forward_task(...))
        —— 把 CPU 前向挂到 CUDA 流上，立即返回
```

**槽位映射**是双缓冲的精髓。设 \(D = \text{buffer\_depth} = 2\)，第 \(L\) 层的槽位为：

\[
\text{slot}(L) = L \bmod D
\]

因为 \(D=2\)，相邻两层必然落进**不同**槽位：

\[
\text{slot}(L) \ne \text{slot}(L+1)
\]

本层的"立即专家"输出写到 \(\text{slot}(L)\)；如果本层有"延迟专家"，它们的输出写到 \(\text{slot}(L+1)\)，正好等于下一层 \(L+1\) 的 `current_slot`。两层输出落在不同物理缓冲里，互不覆盖——这就是 `buffer_depth=2` 的核心作用（详见 4.2.4 小练习与综合实践）。

#### 4.2.3 源码精读

整体方法签名与拍平、取缓冲：

[kt-kernel/python/experts_base.py:377-404](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L377-L404) —— 把 `hidden_states` 拍平、按 batch 取双缓冲，并解包出 7 类缓冲。

槽位计算与立即/延迟专家划分：

[kt-kernel/python/experts_base.py:406-420](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L406-L420) —— `current_slot = layer_idx % buffer_depth`，`next_slot` 紧随其后；当 `max_deferred_experts_per_token > 0` 时把 top-k 专家拆成"立即"和"延迟"两组，否则全部立即。

**异步拷贝**（注意三个 `non_blocking=True`，目标都是 pinned 缓冲）：

[kt-kernel/python/experts_base.py:422-424](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L422-L424) —— 输入、权重、立即专家 id 全部异步拷到 CPU 缓冲的 `current_slot`，不阻塞 Python 线程。

**关键：把 CPU 前向挂到 CUDA 流上**。`incremental` 读取的是**上一层**是否有未完成延迟专家（决定本层输出是"累加"还是"覆盖"，见延迟专家机制）：

[kt-kernel/python/experts_base.py:426-438](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L426-L438) —— `incremental` 来自上一层；`cpu_infer.submit_with_cuda_stream(cuda_stream, self.moe.forward_task(...))` 把 CPU 前向任务挂到流上，调用立即返回。

这里 `forward_task` 返回的是一个 `std::pair<intptr_t, intptr_t>`（函数指针 + 参数块），交给 `submit_with_cuda_stream` 去排队。看 C++ 侧 `submit_with_cuda_stream` 怎么把它真正"挂"到流上：

[kt-kernel/cpu_backend/cpuinfer.h:87-95](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h#L87-L95) —— 用 `cudaLaunchHostFunc` 把 CPU 前向函数排进 `user_cuda_stream`。流推进到这里时，CUDA 运行时才调用它，从而 CPU 计算与 GPU 计算共享同一条流顺序。

**延迟专家的第二次提交**（与双缓冲紧密相关）：

[kt-kernel/python/experts_base.py:440-455](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L440-L455) —— 若本层有延迟专家，先把延迟 id 拷进缓冲，再次 `submit_with_cuda_stream` 提交一个前向任务，但这次输出写到 `output_cpu[next_slot]`，并把本层标记为"有未完成延迟专家"（`_layer_has_pending_deferred[layer_idx]=True`）。

注意两次提交输出落点不同：立即专家写 `current_slot`，延迟专家写 `next_slot`——这正是双缓冲的用武之地，两组结果共存于不同槽位。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 模拟 `submit_forward` 的槽位映射，画出 `current_slot/next_slot` 的双缓冲流转，定量验证 `buffer_depth=2` 下相邻两层不冲突。

**操作步骤**：运行下面这段**示例代码**：

```python
# 示例代码：模拟 submit_forward 的槽位映射与双缓冲流转
D = 2  # buffer_depth
num_layers = 4

for L in range(num_layers):
    current = L % D
    nxt = (L + 1) % D
    print(f"layer {L}: current_slot={current} (立即专家输出)  "
          f"next_slot={nxt} (延迟专家输出)")
```

**需要观察的现象**：相邻层的 `current_slot` 严格交替（0,1,0,1…）；层 L 的 `next_slot` 恰好等于层 L+1 的 `current_slot`。

**预期结果**：

```text
layer 0: current_slot=0 (立即专家输出)  next_slot=1 (延迟专家输出)
layer 1: current_slot=1 (立即专家输出)  next_slot=0 (延迟专家输出)
layer 2: current_slot=0 (立即专家输出)  next_slot=1 (延迟专家输出)
layer 3: current_slot=1 (立即专家输出)  next_slot=0 (延迟专家输出)
```

> 把这个交替关系画成图（见综合实践），就能直观看到为什么"双缓冲"让相邻两层可以并发而不互相覆盖输出。

#### 4.2.5 小练习与答案

**练习 1**：`submit_forward` 返回时，CPU 上的 MoE 前向一定已经算完了吗？为什么？

**答案**：没有。它只是用 `cudaLaunchHostFunc` 把前向函数**排进了 CUDA 流**，调用立即返回。真正执行要等流推进到那个位置、由 CPU 线程池跑完。

**练习 2**：第 4 步三个 `copy_(..., non_blocking=True)` 为什么能安全地"不等就返回"？

**答案**：因为它们的目标 `*_cpu` 缓冲都是 `pin_memory=True` 的页锁定内存，GPU 可以对它做异步 DMA。同时这些拷贝与后续的 `submit_with_cuda_stream` 排在同一条 `cuda_stream` 上，流顺序保证了"拷贝完成 → CPU 前向开始"的先后，无需显式等待。

---

### 4.3 流同步：sync_forward 与 cudaLaunchHostFunc

#### 4.3.1 概念说明

`submit_forward` 把活排进了流就返回，那**什么时候、靠什么来保证 CPU 结果已经就绪**？这就是 `sync_forward` 的职责。它不靠 `torch.cuda.synchronize()` 这种粗暴的全局阻塞，而是把一个"同步点"也用 `cudaLaunchHostFunc` **排进同一条流**。

这样设计的好处是：同步点本身是流的一部分，它会**恰好排在 CPU 前向任务的后面**。当流推进到同步点，就说明前面的 CPU 前向（也是挂在流上的 host func）已经完成，此时 CPU 输出缓冲里的结果已就绪，可以安全地拷回 GPU。

还有一个精巧的参数 `allow_pending`（对应 C++ 的 `allow_n_pending`）：当本层有"延迟专家任务"仍在跑时，它的输出写的是 `next_slot`，**不影响**当前要读的 `current_slot`。于是同步时可以"容忍 1 个未完成任务"——不必等延迟专家算完就能返回当前槽的结果，让延迟专家的计算与后续 GPU 工作**重叠**起来。

#### 4.3.2 核心流程

`sync_forward` 的步骤：

```text
1. 拍平 hidden_states，取同一组双缓冲
2. current_slot = layer_idx % buffer_depth
3. allow_pending = 本层是否有未完成延迟专家 ? 1 : 0
4. cpu_infer.sync_with_cuda_stream(stream, allow_pending)
        —— 把"等 CPU 任务队列"挂到流上
5. output_gpu[current_slot].copy_(output_cpu[current_slot], non_blocking=True)
6. 返回 output_gpu[current_slot]
```

而同步封装 `forward` 就是"提交后立刻同步"的顺序组合：

```text
forward(...) = submit_forward(...) ; return sync_forward(...)
```

> 注意：`sync_forward` 之所以能用同一个 `hidden_states` 重新取缓冲，是因为 `get_buffer` 是**按 batch size 缓存**的——只要 batch size 不变，取到的是同一组缓冲、同一个槽位，`sync` 读的正是 `submit` 写过的那块。

#### 4.3.3 源码精读

`sync_forward` 主体：

[kt-kernel/python/experts_base.py:457-483](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L457-L483) —— 取同一缓冲、算 `current_slot`，`allow_pending` 来自 `_layer_has_pending_deferred`，调用 `sync_with_cuda_stream`，再把 CPU 输出拷回 GPU 输出并返回。

注意 `allow_pending` 的来源（4.2 里被置位）：

[kt-kernel/python/experts_base.py:480-482](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L480-L482) —— 若本层提交过延迟专家任务，`allow_pending=1`，允许同步在还有 1 个任务未完成时返回；随后把 `current_slot` 的 CPU 结果拷回 GPU。

看 C++ 侧 `sync_with_cuda_stream` 如何把同步排进流：

[kt-kernel/cpu_backend/cpuinfer.h:98-106](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h#L98-L106) —— `SyncArgs` 携带 `allow_n_pending`；`sync_` 调用 `task_queue_->sync(allow_n_pending)`，即"等任务队列里只剩不超过 `allow_n_pending` 个任务"。

[kt-kernel/cpu_backend/cpuinfer.h:113-119](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/cpu_backend/cpuinfer.h#L113-L119) —— `sync_with_cuda_stream` 同样用 `cudaLaunchHostFunc` 把 `sync_` 排进 `user_cuda_stream`。于是同步点与前向任务同处一条流，顺序自然得到保证。

最后是同步封装 `forward`：

[kt-kernel/python/experts_base.py:485-505](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L485-L505) —— `forward` 只是依次调用 `submit_forward` 再 `sync_forward` 并返回结果，等价于"提交后立即同步"的同步前向。

补充：C++ 侧 `forward_task` 有两个重载，Python 用的是带 `incremental` 布尔的 7 参数版本：

[kt-kernel/ext_bindings.cpp:426-431](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp#L426-L431) —— 两个 `forward_task` 重载，区别仅在于最后那个 `bool`（即 `incremental`，决定输出累加还是覆盖）。

而把这两个流同步方法暴露给 Python 的绑定：

[kt-kernel/ext_bindings.cpp:497-499](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/ext_bindings.cpp#L497-L499) —— `sync_with_cuda_stream` 的 Python 第二参数默认 `allow_n_pending=0`，`submit_with_cuda_stream` 无额外默认值。

#### 4.3.4 代码实践

**实践目标**：通过追踪"提交—同步"两个方法对同一缓冲槽的读写，理解 `allow_pending` 如何放行延迟专家、形成重叠。

**操作步骤**：

1. 假设 layer 1 提交了延迟专家（`_layer_has_pending_deferred[1]=True`）。
2. 追踪 `sync_forward` 在 layer 1 的执行：`current_slot = 1 % 2 = 1`，`allow_pending = 1`。
3. 回答：此时 layer 1 的延迟专家任务（写 `output_cpu[next_slot=0]`）是否可能还没跑完？`sync_with_cuda_stream(stream, 1)` 会不会因此阻塞等待它？

**需要观察的现象 / 思考结论**：延迟专家写的是 `next_slot(=0)`，而 `sync_forward` 读的是 `current_slot(=1)`。两者是**不同的物理缓冲**，所以读 `current_slot` 的结果不依赖延迟专家是否完成。`allow_pending=1` 正是告诉任务队列"允许还剩 1 个任务没做完就放行"，于是同步可以提前返回，延迟专家的计算与后续 GPU 工作**重叠执行**。

**预期结果**：在延迟专家写 `next_slot`、同步读 `current_slot` 互不冲突的前提下，`allow_pending=1` 是安全的，且能让 CPU 与 GPU 重叠。**真实时序（延迟收益大小）待本地在 GPU 上 benchmark 验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sync_forward` 不直接调用 `torch.cuda.synchronize()`，而是用 `sync_with_cuda_stream`？

**答案**：`torch.cuda.synchronize()` 会阻塞**整条**默认流/设备，粒度太粗，会破坏流水线重叠。`sync_with_cuda_stream` 把同步点用 `cudaLaunchHostFunc` 排进**指定的流**，且只等 CPU 任务队列到 `allow_n_pending` 阈值，粒度精细、只阻塞必要部分。

**练习 2**：如果某层 `max_deferred_experts_per_token=0`（没有延迟专家），`sync_forward` 的 `allow_pending` 是多少？为什么？

**答案**：是 0。因为没有延迟专家任务被提交，`_layer_has_pending_deferred[layer_idx]` 为 `False`，必须等到 CPU 队列里本层任务**全部**完成才放行，保证 `current_slot` 结果就绪。

**练习 3**：`forward` 方法适合什么场景？`submit_forward` + `sync_forward` 拆开又适合什么场景？

**答案**：`forward`（提交即同步）适合单层需要立刻拿到结果的同步调用；拆开后，可以在 `submit_forward` 返回到 `sync_forward` 调用之间**插入其它 GPU 工作**，让 CPU 计算与这些 GPU 工作重叠，这正是流水线推理获得加速的写法。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿任务**：用 `set_capture_batch_sizes([1,2,4,8])` 配置缓冲，画出 `submit_forward` 中 `current_slot/next_slot` 的双缓冲流转示意图，并**定量解释为何 `buffer_depth=2`**。

### 步骤 1：配置缓冲

阅读 [set_capture_batch_sizes](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L507-L521)，确认它会把这些批大小写入 `capture_bs`，使得这些 batch size 第一次出现后被长期缓存在 `capture_buffers` 里。

### 步骤 2：追踪双缓冲流转

以一个 4 层、`max_deferred_experts_per_token=2`、`num_experts_per_tok=8` 的 MoE 为例（即每层 6 个立即专家 + 2 个延迟专家），追踪每个槽位的写入。用本讲 4.2.4 的模拟，可得：

```text
              slot 0 (output_cpu[0])          slot 1 (output_cpu[1])
layer 0  →  立即6专家 写入 ←                   延迟2专家 写入 ←
layer 1  →  延迟2专家 写入 ← (来自 layer0)      立即6专家 写入(累加 layer0 延迟) ←
layer 2  →  立即6专家 写入(累加 layer1 延迟) ←   延迟2专家 写入 ←
layer 3  →  延迟2专家 写入 ←                    立即6专家 写入(累加 layer2 延迟) ←
```

读法：层 L 的"立即专家"写到 `current_slot = L%2`；"延迟专家"写到 `next_slot = (L+1)%2`。而层 L+1 的 `current_slot` 恰好等于层 L 的 `next_slot`，于是层 L 的延迟结果会落进层 L+1 立即专家要写的同一个槽，靠 `incremental=True` 累加合并（`incremental` 来自 `_layer_has_pending_deferred[L-1]`，见 [源码 L426](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py#L426)）。

### 步骤 3：解释为何 `buffer_depth=2`

请结合上面的流转图，回答三点（这是本实践的核心）：

1. **为什么不能是 1？** 若 `buffer_depth=1`，则 `current_slot == next_slot`，层 L 的延迟专家输出会和它自己的立即专家输出**写同一块缓冲**，互相覆盖，结果错误。
2. **为什么 2 就够？** 槽位映射 `slot(L)=L%2` 保证**相邻两层必然不同槽**；而延迟机制只"回看一层"（`incremental` 只读 `_layer_has_pending_deferred[L-1]`），任意时刻最多只有两层（当前层 + 上一层的延迟）需要各自的输出缓冲，两个槽位刚好一一对应。
3. **为什么不用 3？** 多一个槽位就要多分配一整组 pinned 缓冲（输入、id、权重、输出都翻倍），纯属浪费内存，而流水线深度并不需要第三个并发输出槽。

**自检**：把 `buffer_depth` 改成 1，重跑 4.2.4 的模拟，观察 `current_slot` 与 `next_slot` 是否变成相等——若相等，即印证"双缓冲是避免写冲突的最小配置"。

> 延迟专家的具体选择策略（`select_deferred_experts` 怎么挑出哪几个延迟、`protected_k` 含义、对精度的影响）属于 **u6-l4「延迟专家与流水线执行」**，本实践只用到它的"输出写 next_slot"这一与缓冲相关的性质。

---

## 6. 本讲小结

- `KExpertsCPUBuffer` 是纯数据管家：用 **pinned memory** 保证 GPU 可异步搬运，用 `capture_bs`/`capture_buffers` 长期缓存常用批大小的缓冲，`temp_bs` 提供"最近一次"轻量复用。
- 每组缓冲是一个 7 元组，每一项长度为 `buffer_depth=2`，即**双缓冲**；相邻两层靠 `slot = layer_idx % 2` 交替使用不同槽位，避免输出互相覆盖。
- `submit_forward` 是**非阻塞**的：用 `non_blocking=True` 把数据搬到 pinned CPU 缓冲，再用 `submit_with_cuda_stream`（底层 `cudaLaunchHostFunc`）把 CPU 前向挂到 CUDA 流上，调用立即返回。
- `sync_forward` 把"等 CPU 队列"也用 `cudaLaunchHostFunc` 排进**同一条流**，从而同步点自然排在前向任务之后；`allow_pending` 在有延迟专家时放行 1 个未完成任务，让延迟计算与 GPU 工作重叠。
- `forward` 是 `submit_forward` + `sync_forward` 的同步快捷写法；拆开调用则可在两步之间插入 GPU 工作，构建 CPU↔GPU 流水线。
- 贯穿全讲的物理基础是 **pinned memory + CUDA 流顺序**：前者让拷贝非阻塞，后者让 CPU 计算与 GPU 计算无需显式 event 即可按序协调。

---

## 7. 下一步学习建议

- **下一步读 u4-l4「GPU 专家掩码与放置」**：本讲的缓冲流转默认 `num_experts_per_tok` 个专家，而哪些专家在 GPU、哪些在 CPU，由 `gpu_experts_mask` 决定，那是下一讲的主题。
- **深入延迟专家流水线，读 u6-l4「延迟专家与流水线执行」**：本讲只解释了 `next_slot`/`incremental`/`allow_pending` 的缓冲侧语义，延迟专家的**选择策略**与流水线收益分析在那里展开。
- **下探 C++ 同步实现，读 u8-l1「pybind11 绑定层」**：本讲引用了 `submit_with_cuda_stream` / `sync_with_cuda_stream` / `forward_task` 的绑定，绑定层的全貌与 `CPUInfer` 接口在那一讲系统讲解。
- **建议阅读的源码**：`kt-kernel/cpu_backend/cpuinfer.h`（`cudaLaunchHostFunc` 的两处用法）、`kt-kernel/cpu_backend/worker_pool.*`（`task_queue_->sync` 的真正实现），把"流同步"这条链补到最底端。
