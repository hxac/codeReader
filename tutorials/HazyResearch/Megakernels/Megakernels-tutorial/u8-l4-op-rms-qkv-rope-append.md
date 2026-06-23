# 实战 op：rms_qkv_rope_append

## 1. 本讲目标

本讲以低延迟 Llama demo 中的第一个 op——`rms_qkv_rope_append`——为对象，做一次"从指令解析到全局存储"的完整源码精读。学完后你应当能够：

1. 读懂 `parsed_instruction` 如何把一条 32 个 `int` 的指令解析成 `layer_idx / start_block_idx / end_block_idx / iters`。
2. 读懂 op 是如何通过 `pipeline_specifics` 的三个回调（`gmem_wait`、`load_iter`、`store`）插入到通用 matvec 流水线里的，并理解每个回调分别在流水线的哪一阶段被调用。
3. 讲清楚对 Q、K 做的 **旋转位置编码（RoPE）** 的逐 lane 计算逻辑，以及为什么 V 不做 RoPE。
4. 讲清楚 `store` 末尾如何用 **TMA store** 把 K/V 追加进 KV cache，并用一个全局 `atomicAdd` 更新 `globals.Bar` 屏障，从而"解锁"下游的 attention op。

本讲是 `u8-l3`（通用 matvec 流水线 `matvec_pipeline.cuh`）的延续，重点放在"op 特有"的那部分代码上。

## 2. 前置知识

- **Megakernel 虚拟机模型**：每条 GPU 指令（这里指"虚拟机指令"，不是 SASS）是一个固定宽度的 `int` 数组。控制器、loader、launcher、consumer、storer 这几类 warp 协作执行一条指令。详见 `include/util.cuh` 里的 `state` 与 `include/config.cuh`。
- **TMA（Tensor Memory Accelerator）**：Hopper/Blackwell 上异步搬运大块 tensor 的硬件单元，配合 `kittens::semaphore`（mbarrier）做完成通知。`tma::load_async` / `tma::store_async` 是发起搬运，`expect` 是预先声明目标地址，`*_wait` 是等待搬运真正完成（对 store 来说是"已对全局可见"）。
- **matvec（矩阵-向量乘）分块**：把一个 `[out_dim, in_dim]` 的权重按 `st_bf<16, 512>` 切块，每条指令通过若干个 iter 把整条 2048 维 hidden state 投影成若干个 16 元素的输出块。
- **GQA（Grouped-Query Attention）**：Llama-1B 有 32 个 query 头、8 个 KV 头，GQA ratio = 4，即每个 KV 头被 4 个 Q 头共享。这直接决定了下面 Q/K/V 输出块的区间划分。
- **RoPE（Rotary Position Embedding）**：对每个 head 内的成对元素做旋转。Llama 用的是 **interleaved（交错）** 变体，即 `(x0,x1), (x2,x3), ...` 配对，而非"前半/后半"配对。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [demos/low-latency-llama/rms_matvec_rope_append.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu) | 本讲主角。定义 op `rms_qkv_rope_append`，包含 `parsed_instruction`、`pipeline_specifics`（`gmem_wait`/`load_iter`/`store`）以及 loader/launcher/consumer/storer 的入口。 |
| [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | 通用 matvec 流水线（含 RMSNorm 变体 `rms_matvec_pipeline`）。本讲关注它如何调用 op 提供的回调。 |
| [demos/low-latency-llama/llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | 模型全局结构体 `globals_t`：权重、KV cache、`Bar` 屏障、各种维度常量与 opcode 宏。 |
| [demos/low-latency-llama/utils.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh) | `rms_norm`、`matvec`、`matvec_reduce` 三个内联工具函数。 |
| [demos/low-latency-llama/attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu) | 下游 attention op。本讲用它来验证"`Bar` 屏障被谁等待、阈值是多少"。 |
| [megakernels/demos/latency/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py) | 与 CUDA 语义一一对应的 Python 参考实现，是理解屏障计数和 Q/K/V 区间划分的"权威说明书"。 |

---

## 4. 核心概念与源码讲解

### 4.1 parsed_instruction：从 32 个 int 解析参数

#### 4.1.1 概念说明

Megakernel 虚拟机里，每条指令是一条固定宽度为 32 个 `int`（即 128 字节）的数组：

- [include/config.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14-L15) 定义了 `INSTRUCTION_WIDTH = 32` 和 `instruction_t = int[INSTRUCTION_WIDTH]`。

调度框架用 `instruction[0]` 作为 opcode 来分发到具体 op（见 [include/util.cuh:288-291](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L288-L291) 把 `mks.instruction()[0]` 当 opcode 传入 `dispatch_op`）。剩下的 `instruction[1..]` 就由每个 op 自己解释。

`parsed_instruction` 就是"把裸的 `int[32]` 翻译成有名字的字段"的小结构体。它本质是一个构造函数：吃进指令数组，输出几个语义清晰的整数。

#### 4.1.2 核心流程

对本 op 而言，4 个槽位的含义是：

| 槽位 | 字段 | 单位 / 含义 |
| --- | --- | --- |
| `instruction[0]` | opcode | 固定为 `OPCODE_RMS_QKV_MatVecRopeAppend = 1`（见 [llama.cuh:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L7)），由框架读取、本 op 不用 |
| `instruction[1]` | `layer_idx` | 层号，单位 1 |
| `instruction[2]` | `start_block_idx` | 起始输出块号，单位 16 个元素（一个 matvec 块） |
| `instruction[3]` | `end_block_idx` | 结束输出块号，单位同上 |
| — | `iters = end - start` | 本指令要处理多少个 16 元素块 |

之所以用"块号"而不是"元素下标"，是因为整个 matvec 是按 16 元素一块来流水线化的；后面你会看到 `block_idx = start_block_idx + iter` 直接驱动循环和 TMA 寻址。

#### 4.1.3 源码精读

解析逻辑本身极短：

```cpp
struct parsed_instruction {
    int layer_idx, start_block_idx, end_block_idx, iters;
    __device__ inline parsed_instruction(typename Config::instruction_t &instruction) {
        layer_idx       = instruction[1];       // in units of 1
        start_block_idx = instruction[2];       // in units of 16 elements
        end_block_idx   = instruction[3];       // in units of 16 elements
        iters           = end_block_idx - start_block_idx;
    }
    __device__ inline parsed_instruction(megakernel::state<Config> &s)
        : parsed_instruction(s.instruction()) {}
};
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:34-45](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L34-L45)。第二个构造函数是"语法糖"：直接传 `state`，它再调 [state::instruction()](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L83-L89) 拿到当前指令数组，然后委托给第一个构造函数。这样在 op 各处都能用 `parsed_instruction inst{s};` 一行完成解析。

> 对照参考实现：[python_vm.py:168-201](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L168-L201) 的 `layer_norm_matvec_rope_append` 同样用 `start_output_block_idx / end_output_block_idx` 以 16 元素块为单位循环，语义完全一致。

#### 4.1.4 代码实践

**实践目标**：亲手把几条假想指令翻译成 `parsed_instruction` 的字段，建立"块号 ↔ 元素区间"的直觉。

**操作步骤**（纯阅读/心算，无需运行）：

1. 假设一条指令为 `{1, 3, 0, 192}`（opcode=1，layer=3，start=0，end=192）。
2. 写出 `layer_idx / start_block_idx / end_block_idx / iters`。
3. 计算这条指令覆盖的 **元素** 区间：`[start_block_idx*16, end_block_idx*16)`。

**需要观察的现象 / 预期结果**：

- `layer_idx=3, start_block_idx=0, end_block_idx=192, iters=192`。
- 元素区间 = `[0, 3072)`。这恰好是 Q(2048) + K(512) + V(512) = 3072，对应 32 个 Q 头 + 8 个 K 头 + 8 个 V 头（`32*64 + 8*64 + 8*64 = 3072`）。也就是说，**一条 QKV 指令通常一次性产出整层的 Q、K、V**，共 192 个 16 元素块。

> 待本地验证：可在 host 端打印生成 QKV 指令时写入 `instruction[2]/[3]` 的值（搜索生成指令的调度代码），确认 `end - start` 是否就是 192。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `instruction[0]` 不在 `parsed_instruction` 里出现？
**答案**：`instruction[0]` 是 opcode，由虚拟机调度框架（`dispatch_op`）读取并决定分发到哪个 op；当代码已经进入 `rms_qkv_rope_append` 内部时，opcode 已知，op 只需要解释 `[1..]`。

**练习 2**：如果把 `iters = end_block_idx - start_block_idx` 改成 `iters = end_block_idx - start_block_idx + 1`，会发生什么？
**答案**：流水线的 `loader_loop` / `consumer_loop` / `storer_loop` 全部用 `for (int i = 0; i < inst.iters; i++)` 驱动，`iters` 偏大 1 会导致多处理一个越界块、对错误的权重/输出地址发起 TMA，属于典型 off-by-one 越界。区间是半开 `[start, end)`，所以必须用减法不加 1。

---

### 4.2 pipeline_specifics：load 侧回调（gmem_wait + load_iter）

#### 4.2.1 概念说明

通用流水线 `matvec_pipeline` / `rms_matvec_pipeline` 负责所有"与 op 无关"的机械动作：分配 page、驱动输入/输出流水线、调用 `matvec` 做乘加、用 `matvec_reduce` 跨 warp 归约。但有三件事是 **每个 op 特有** 的，必须由 op 自己提供，流水线在固定位置回调它们：

1. `gmem_wait`：在 consumer 真正读全局输入之前，等待 **上游 op** 把数据写完（跨 op、跨 SM 的同步）。
2. `load_iter`：loader 在加载每个权重块时，告诉它"这一块权重在全局里的坐标是什么"（即 TMA load 的寻址）。
3. `store`：storer 在写出每个输出块时，告诉它"这个 16 元素结果该写到全局的哪里、写之前/之后还要做什么"（RoPE、三路分发、屏障更新）。

本节讲前两个（load 侧）；`store` 放到 4.3、4.4。

#### 4.2.2 核心流程

```
loader_loop（流水线）          consumer_loop（流水线）
  ├─ 读 activation page         ├─ gmem_wait(inst)        ← op 回调：等上游屏障
  ├─ 对每个 iter：                ├─ 读 activation / rms_norm
  │    └─ load_iter(inst,iter,col_idx, tile, sem) ← op 回调：定 TMA 寻址
  └─ ...                         ├─ matvec × 多 warp
                                  ├─ matvec_reduce
                                  └─ （输出留给 storer）
```

注意三个回调分别落在不同阶段、甚至不同 warp 类里：

- `load_iter` 在 **loader warp** 的 `loader_loop` 里被调用（每个 iter 调 4 次，对应 4 个 `col_idx` 权重块）。
- `gmem_wait` 在 **consumer warp** 的 `rms_matvec_pipeline::consumer_loop` 开头被调用（`kittens::laneid()==0 && warpid()==0` 那一支），见 [matvec_pipeline.cuh:325-327](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L325-L327)。
- `store` 在 **storer warp** 的 `storer_loop` 里被调用。

#### 4.2.3 源码精读

**(a) gmem_wait：跨层等待**

```cpp
static __device__ inline void gmem_wait(const Globals &g, megakernel::state<Config> &s) {
    parsed_instruction inst{s};
    if (inst.layer_idx > 0) {
        while (*(volatile int *)&g.Bar[{inst.layer_idx - 1,
                                        OPCODE_DownProjResidual - 1, 0}] <
               EXPECTED_ARRIVAL_COUNT) {
            __nanosleep(Config::GMEM_SPIN_LOOP_SLEEP_NANOS);
        }
    }
}
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:49-60](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L49-L60)。

含义：**第 0 层不需要等**（没有上游）；从第 1 层起，本 op 要读的 `hidden_states` 是由 **上一层** 的 `DownProjResidual`（opcode 6）写出来的。`OPCODE_DownProjResidual - 1 = 5` 是上一层在 `Bar` 里"属于自己的那一格"opcode 平面，`0` 是该平面下的第 0 号计数槽。它自旋等待这个计数达到 `EXPECTED_ARRIVAL_COUNT = 512`。

> 这个 512 是怎么来的？参考实现 [python_vm.py:107-110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L107-L110) 直接断言 `op_barriers[0] == 512  # 8192 / 16`：downproj 把 8192 维 intermediate 按 16 元素块切，共 512 块，每块完成累加一次，所以"上一层 downproj 全部完成"的标志就是计数到 512。

**(b) load_iter：权重块的 TMA 寻址**

```cpp
static __device__ inline void
load_iter(megakernel::state<Config> &s, const globals &g, parsed_instruction &inst,
          int iter, int col_idx, kittens::st_bf<16, 512> &weight_chunk,
          kittens::semaphore &sem) {
    auto block_idx = inst.start_block_idx + iter;
    kittens::tma::load_async<dim::ROW, cache_policy::EVICT_FIRST>(
        weight_chunk, g.qkv_weights,
        {inst.layer_idx, block_idx, col_idx}, sem);
}
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:62-70](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L62-L70)。

- `block_idx = start_block_idx + iter`：这是本块输出在整个 3072 维 QKV 空间里的块号。
- `g.qkv_weights` 的类型见 [llama.cuh:77-79](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L77-L79)：`gl<bf16, 1, -1, -1, hidden_dim=2048, st_bf<16,512>>`，即三维可变坐标 `{layer, block, col}`，最内维 2048 被 `st_bf<16,512>` 切成 4 块（`col_idx ∈ {0,1,2,3}`）。所以一次 `load_iter` 载入的是"第 layer 层、第 block_idx 个 16 元素输出、输入 512 列"的那一片权重。
- `EVICT_FIRST`：这块权重只用一次（每个 block 的权重不同），用完希望尽快被 L2 驱逐，避免污染缓存。
- 载入由 `loader_loop`（[matvec_pipeline.cuh:117-160](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L117-L160)）驱动：它对每个 iter 内层 `for(i=0;i<4;i++)` 调用 `load_iter(..., i, ...)` 把 4 片权重都搬进 smem。

#### 4.2.4 代码实践

**实践目标**：定位三个回调在流水线里的"调用点"，建立"回调落在哪类 warp、哪个阶段"的地图。

**操作步骤**（源码阅读型）：

1. 在 `matvec_pipeline.cuh` 里搜索 `pipeline_specifics::load_iter`、`pipeline_specifics::store`、`pipeline_specifics::gmem_wait`，分别记录它们所在的函数和行号。
2. 对照 [include/loader.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/loader.cuh)（或 `rms_matvec_pipeline::loader_loop`）、[include/storer.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/storer.cuh) 确认它们分别属于 loader / consumer / storer。

**需要观察的现象 / 预期结果**：

- `load_iter` 在 `matvec_pipeline::loader_loop`（[matvec_pipeline.cuh:149-150](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L149-L150)）被 loader warp 调用。
- `gmem_wait` 在 `rms_matvec_pipeline::consumer_loop`（[matvec_pipeline.cuh:326](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L326)）被 consumer 的 0 号 warp / 0 号 lane 调用。
- `store` 在 `matvec_pipeline::storer_loop`（[matvec_pipeline.cuh:233](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L233)）被 storer warp 调用。

> 待本地验证：若开启 `MK_DEBUG`（见 [include/util.cuh:250-258](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L250-L258)），可在三个回调入口各加一行 `printf`，运行单层推理确认打印顺序确实是 load → (gmem_wait 已在 consumer 起始) → store。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gmem_wait` 只在 `layer_idx > 0` 时才等？第 0 层的 `hidden_states` 从哪来？
**答案**：第 0 层没有上一层，其输入是 token embedding 直接写入的 `hidden_states`，由 host 在 launch 前准备好，无需等待屏障；`layer_idx > 0` 才依赖上一层 `DownProjResidual` 的残差累加结果。

**练习 2**：`load_iter` 里用了 `EVICT_FIRST`，而 loader 加载 RoPE 表（见 4.3）却用 `EVICT_LAST`，为什么不同？
**答案**：每个输出块的权重各不相同、只用一次，`EVICT_FIRST` 让它尽快离开 L2；而 RoPE 的 cos/sin 表在整条指令的所有 192 个块里被反复读取（每个块都要查表），是热点复用数据，用 `EVICT_LAST` 让它尽量留在 L2。

---

### 4.3 store 回调 I：RoPE 旋转（对 Q/K）

#### 4.3.1 概念说明

`store` 回调拿到的是一个已经跨 warp 归约好的、16 元素的浮点向量 `qkv_proj`（一个 matvec 输出块）。它要做三件事：① 对 Q/K 做旋转位置编码；② 转成 bf16；③ 按 block_idx 落在 Q/K/V 哪个区间，TMA 写到不同的全局目标（4.4 讲）。

本节聚焦 ①：**RoPE**。Llama 的 RoPE 对每个 head 内的元素两两配对做旋转。设一个 head 维度为 \(d\)（这里 \(d=64\)），把元素按下标两两交错配对 \((x_{0},x_{1}), (x_{2},x_{3}), \dots\)，第 \(i\) 对的旋转角为 \(\theta_i\)，则：

\[
\begin{aligned}
x'_{2i}   &= x_{2i}\cos\theta_i - x_{2i+1}\sin\theta_i \\
x'_{2i+1} &= x_{2i}\sin\theta_i + x_{2i+1}\cos\theta_i
\end{aligned}
\]

注意一个关键事实：每个 16 元素输出块只覆盖某个 head 的 \(1/4\)（因为 `head_dim=64`，`16×4=64`）。所以 `block_idx % 4` 正好告诉我们"这是 head 内的第几段 16 元素"，用它去 RoPE 表里取对应的 \(\cos/\sin\)。而 V 不参与 attention 的点积位置编码，所以 **V 不做 RoPE**。

#### 4.3.2 核心流程

```
store(inst, output_idx, output_stage):
  block_idx = start_block_idx + output_idx
  matvec_reduce(...) → qkv_proj           # 跨 16 个 consumer warp 归约出 16 个 float
  wait(rope_arrived)                       # 等 loader 把 cos/sin 表搬进 scratch
  head_chunk = block_idx % 4               # 本块在 head 内的段号 0..3
  从 scratch 取该段的 cos[head_chunk] / sin[head_chunk]
  if block_idx < V_BLK_START:              # Q 和 K 才做 RoPE
      mod = (laneid 奇?) ? -1 : +1
      pair_val = __shfl(lane, laneid+mod)  # 取配对邻居
      lane < 16: qkv_proj = qkv_proj*cos + (-mod)*pair_val*sin
  store qkv_proj → smem(bf16)              # 供 TMA 发出
```

#### 4.3.3 源码精读

**(a) 归约 + 等 RoPE 表 + 取段**

```cpp
kittens::rv_fl<16> qkv_proj, rope_cos, rope_sin;
matvec_reduce<Config, kittens::sv_fl<16>, kittens::rv_fl<16>,
              pipeline::SCRATCH_BYTES_PER_WARP>(output_scratch_start, qkv_proj);
kittens::wait(rope_arrived(s), 0);
auto head_chunk = block_idx % 4;
kittens::sv_fl<16> &rope_cos_sv = *reinterpret_cast<kittens::sv_fl<16> *>(
    get_rope_cos_ptr(s) + head_chunk * 64);
kittens::sv_fl<16> &rope_sin_sv = *reinterpret_cast<kittens::sv_fl<16> *>(
    get_rope_sin_ptr(s) + head_chunk * 64);
kittens::warp::load(rope_cos, rope_cos_sv);
kittens::warp::load(rope_sin, rope_sin_sv);
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:88-104](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L88-L104)。

- `matvec_reduce`（[utils.cuh:103-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L103-L120)）把 16 个 consumer warp 各自的 16 元素部分和加总，得到最终的 `qkv_proj`。
- `rope_arrived` 是本 op 额外加的一个信号量（[rms_matvec_rope_append.cu:175-177](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L175-L177)），loader 在把 cos/sin TMA 载入 scratch 后会 arrive 它（见 [rms_matvec_rope_append.cu:198-206](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L198-L206)）。storer 在用表之前必须 `wait`。
- RoPE 表放在 scratch 顶部：cos 占 `[SCRATCH_BYTES-512, SCRATCH_BYTES-256)`，sin 占 `[SCRATCH_BYTES-256, SCRATCH_BYTES)`，见 [rms_matvec_rope_append.cu:21-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L21-L32)。loader 一次性把整个 64 维 cos/sin 载入；`head_chunk*64` 字节（=16 个 float）就选中本块需要的那 16 个角度。

**(b) 逐 lane 旋转（核心）**

```cpp
if (block_idx < V_BLK_START) { // only Q & K need RoPE
    int mod = (kittens::laneid() & 0b1) ? -1 : 1; // 1 for even, -1 for odd
    kittens::warp::sync();
    float pair_val = __shfl_sync(MASK_ALL, qkv_proj[0][0], kittens::laneid() + mod);
    if (kittens::laneid() < 16) {
        qkv_proj[0][0] =
            float(qkv_proj[0][0]) * rope_cos[0][0] +
            float(-1 * mod) * float(pair_val) * rope_sin[0][0];
    }
}
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:106-121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L106-L121)。逐 lane 拆解（lane `l` 持有 \(x_l\)，只看 `l<16` 的有效元素）：

- `mod = +1`（偶数 lane，即 \(x_{2i}\)）/ `mod = -1`（奇数 lane，即 \(x_{2i+1}\)）。
- `pair_val = x_{l+mod}`：偶数 lane 取下一个（奇数邻居），奇数 lane 取上一个（偶数邻居）——正好拿到配对的那一个。
- 系数 `(-1 * mod)`：偶数 lane（mod=+1）为 `-1`，奇数 lane（mod=-1）为 `+1`。代入即得：

\[
\text{偶数 lane: } x' = x\cos\theta - x_{\text{pair}}\sin\theta,\qquad
\text{奇数 lane: } x' = x\cos\theta + x_{\text{pair}}\sin\theta
\]

与 4.3.1 的交错 RoPE 公式完全一致。`__shfl_sync` 用 warp 内寄存器交换完成"取邻居"，无需访问共享内存，非常高效。

> 与参考实现对照：[python_vm.py:211-230](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L211-L230) 调用 `apply_rotary_pos_emb_interleaved`，`mode in ["q","k"]` 才旋转，V 跳过——和 `block_idx < V_BLK_START` 的判断等价。

#### 4.3.4 代码实践

**实践目标**：验证 `head_chunk = block_idx % 4` 与"本块在 head 内的段号"是一致的。

**操作步骤**（心算 + 对照参考实现）：

1. 取 `block_idx = 130`（属于 K，见 4.4）。
2. 计算 `head_chunk = 130 % 4 = 2`。
3. 该块元素区间是 `[130*16, 130*16+16) = [2080, 2096)`，而 K 起点是 `k_start = 2048`，所以相对 K 的偏移 `[32, 48)`，即落在 K head 0 的第 2 段（每段 16，第 2 段是 `[32,48)`）。
4. 对照参考实现 [python_vm.py:218-222](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L218-L222)：`head_segment = start % head_dim`，`start=2080`，`2080 % 64 = 32`，对应第 2 段。

**预期结果**：`head_chunk` 与 Python 的 `head_segment/16` 相同，都是 2。这说明 CUDA 用 `block_idx % 4` 取 RoPE 表段号、与 K/V 寻址用的 `dim_idx`（见 4.4）其实是同一个量——`block_idx % 4`。

#### 4.3.5 小练习与答案

**练习 1**：为什么旋转公式里要 `kittens::warp::sync()` 后再 `__shfl_sync`？
**答案**：`__shfl_sync` 要求参与 warp 同步执行的 mask 与实际活跃 lane 一致；前面不同 lane（奇/偶）走的是同一分支，但出于保守（确保所有 lane 对 `qkv_proj` 的写入对 shfl 可见）先做一次 warp 同步，再用全 mask `MASK_ALL` 进行寄存器交换。

**练习 2**：如果把判断从 `block_idx < V_BLK_START` 改成 `block_idx < K_BLK_START`（即只对 Q 做 RoPE），结果会怎样？
**答案**：K 会缺少旋转。下游 attention 计算 \(QK^\top\) 时，Q 带旋转、K 不带，二者处于不同的"旋转坐标系"，位置编码失效，注意力分布会严重错误。RoPE 必须同时对 Q 和 K 施加相同变换，才能在点积中消去旋转、保留相对位置信息。

---

### 4.4 store 回调 II：Q/K/V 三路 TMA store、KV cache 追加与 atomicAdd 屏障

#### 4.4.1 概念说明

RoPE 之后，`qkv_proj` 是 16 个 float。`store` 把它转成 bf16 写回 smem，然后 **按 `block_idx` 落在哪个区间，写到三个不同的全局目标**：

| 区间（块号） | 元素区间 | 对应 | 写到哪 |
| --- | --- | --- | --- |
| `[0, 128)` | `[0, 2048)` | Q（32 头 × 64） | `g.q_post_rope` |
| `[128, 160)` | `[2048, 2560)` | K（8 头 × 64） | `g.k_cache`（追加） |
| `[160, 192)` | `[2560, 3072)` | V（8 头 × 64） | `g.v_cache`（追加） |

区间边界来自常量（[rms_matvec_rope_append.cu:15-16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L15-L16)）：`K_BLK_START = 2048/16 = 128`，`V_BLK_START = 2560/16 = 160`。2048 = `num_attention_heads*head_dim`，2560 = 2048 + `num_kv_heads*head_dim`，与参考实现 [python_vm.py:186-201](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L186-L201) 的 `k_start/v_start` 完全一致。

对 K/V，这不是"覆盖写"，而是把当前 token 的 K/V **追加**进 KV cache（写入 `[layer, pos_id, head, dim]` 位置），供后续 token 的 attention 读取。写完后还要 `atomicAdd` 更新全局屏障 `g.Bar`，通知下游 attention："这一头的 K/V 已就绪"。

#### 4.4.2 核心流程

```
（接 4.3，已有 qkv_proj）
store qkv_proj → qkv_proj_smem_bf (bf16)
if laneid == 0:
    if block_idx < K_BLK_START:                      # Q
        tma::store_async(q_post_rope, smem, {0,0,0,block_idx})
    elif block_idx < V_BLK_START:                    # K
        base = (block_idx - K_BLK_START)*16
        head_idx = base / 64
        dim_idx  = (base % 64) / 16
        tma::store_async(k_cache, smem, {layer, pos_id, head_idx, dim_idx})
    else:                                            # V
        base = (block_idx - V_BLK_START)*16
        head_idx = base / 64
        dim_idx  = (base % 64) / 16
        tma::store_async(v_cache, smem, {layer, pos_id, head_idx, dim_idx})
    tma::store_async_wait()                          # 必须对全局可见
    atomicAdd(&g.Bar[{layer, opcode-1, block_idx/4}], 1)   # 屏障 +1
```

#### 4.4.3 源码精读

**(a) 三路分发**

```cpp
if (kittens::laneid() == 0) {
    if (block_idx < K_BLK_START) { // Q
        kittens::tma::store_async<cache_policy::EVICT_LAST>(
            g.q_post_rope, qkv_proj_smem_bf, {0, 0, 0, block_idx});
    } else if (block_idx < V_BLK_START) { // K
        int base_index = (block_idx - K_BLK_START) * Globals::matvec_block_size;
        int head_idx   = base_index / Globals::head_dim;
        int dim_idx    = (base_index % Globals::head_dim) / Globals::matvec_block_size;
        kittens::tma::store_async<cache_policy::EVICT_LAST>(
            g.k_cache, qkv_proj_smem_bf,
            {inst.layer_idx, static_cast<int>(g.pos_id), head_idx, dim_idx});
    } else { // V
        int base_index = (block_idx - V_BLK_START) * Globals::matvec_block_size;
        int head_idx   = base_index / Globals::head_dim;
        int dim_idx    = (base_index % Globals::head_dim) / Globals::matvec_block_size;
        kittens::tma::store_async<cache_policy::EVICT_LAST>(
            g.v_cache, qkv_proj_smem_bf,
            {inst.layer_idx, static_cast<int>(g.pos_id), head_idx, dim_idx});
    }
    ...
}
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:127-152](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L127-L152)。

- **Q 分支**：直接写 `q_post_rope`，坐标 `{0,0,0,block_idx}`——这是个 4 维表，前三维退化为 0，第四维就是块号（每个块 16 元素，连续铺成 2048 维的 Q）。
- **K/V 分支**：把块号换算成 KV cache 里的 `{layer, pos_id, head_idx, dim_idx}`。
  - `base_index = (block_idx - K/V_BLK_START) * 16`：本块在 K（或 V）空间内的元素起点。
  - `head_idx = base_index / 64`：除以 `head_dim=64` 得到 KV 头号（0..7）。
  - `dim_idx = (base_index % 64) / 16`：head 内的段号（0..3）。
  - `g.pos_id` 是当前生成 token 在序列里的位置；KV cache 维度见 [llama.cuh:94-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L94-L95) 的 `kv_cache_t`。所以这就是"把当前 token 的 K/V 写进 cache 的对应槽位"，即 **append**。
- 注意：`dim_idx` 的取值范围是 0..3，正好等于 `block_idx % 4`——和 4.3 里取 RoPE 表段号的 `head_chunk` 是同一个量。这是设计上的自洽。

**(b) 全局可见 + 屏障更新**

```cpp
    s.record(megakernel::TEVENT_AT_GMEM_STORE);
    kittens::tma::store_async_wait(); // not just read wait! full wait! must be visible in global!
    atomicAdd(&g.Bar[{inst.layer_idx, opcode - 1, block_idx / 4}], 1);
    s.record(megakernel::TEVENT_DONE_GMEM_STORE);
```

见 [demos/low-latency-llama/rms_matvec_rope_append.cu:154-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L154-L163)。这里有两点必须讲清楚：

**第一，为什么 `store_async_wait` 必须在 `atomicAdd` 之前？**
`atomicAdd` 是在向下游发"数据已就绪"的信号。如果先 `atomicAdd` 再等 store 完成，下游 attention 一看到屏障达标就可能去读 K/V cache，但此时 TMA store 可能还没真正落到全局内存——下游会读到旧值/未定义值。所以必须先 `store_async_wait`（注释强调"full wait, must be visible in global"，不是只等 TMA 的读完成通知），确保写入对全局可见，再更新屏障。这是典型的"用屏障传递可见性"的模式。

**第二，`atomicAdd(&g.Bar[{layer, opcode-1, block_idx/4}], 1)` 到底解锁了谁？**

- `opcode = OPCODE_RMS_QKV_MatVecRopeAppend = 1`，所以 `opcode - 1 = 0`，是 `Bar` 里属于本 op 的那一格 opcode 平面。
- 关键是第三维 `block_idx / 4`：每个 head 是 64 维 = 4 个 16 元素块，所以 `block_idx/4` 把 4 个连续块归并到 **同一个 head**。于是这个屏障计数槽 = "某个 head"。具体映射：
  - Q 块 `block_idx ∈ [0,128)` → `block_idx/4 ∈ [0,32)` → Q head 0..31
  - K 块 `block_idx ∈ [128,160)` → `block_idx/4 ∈ [32,40)` → K head 0..7
  - V 块 `block_idx ∈ [160,192)` → `block_idx/4 ∈ [40,48)` → V head 0..7

  最后这一维大小 `num_attention_heads + 2*num_kv_heads = 32+8+8 = 48`（见 [llama.cuh:104-105](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L104-L105)），正好容下这 48 个 head 计数槽。
- 每个 head 的 4 个块各 `atomicAdd` 一次，所以该槽累加到 **4** 就表示"这一头的 K/V（或 Q）4 段全部写完"。

下游 `attention_partial` 正是自旋等待这个 4：

```cpp
while (*(volatile int *)&g.Bar[{inst.layer_idx, OPCODE_RMS_QKV_MatVecRopeAppend - 1,
                                q_head_start_idx + head_offset}] < 4) { __nanosleep(...); }
```

见 [attention_partial.cu:395-406](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L395-L406)（等 4 个 Q 头）和 [attention_partial.cu:308-329](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L308-L329)（等对应的 K/V 头，下标 `NUM_ATTENTION_HEADS + kv_head_idx` 与 `NUM_ATTENTION_HEADS + NUM_KV_HEADS + kv_head_idx`，与本 op 的 `[32,40)`/`[40,48)` 一一对应）。参考实现 [python_vm.py:248](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L248) 的 `barriers[block_idx // 4] += 1` 与 [python_vm.py:282-290](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L282-L290) 的 `assert ... == 4` 把这件事讲得一目了然。

> 为什么用全局 `atomicAdd` 而不是 mbarrier 信号量？因为 QKV op（生产者）和 attention op（消费者）跑在 **不同的 SM** 上，无法共享 smem 里的 `kittens::semaphore`；全局原子计数器是跨 SM 同步的最简单手段。这是 megakernel "持久化 kernel + 跨 op 全局屏障"模型的核心套路。

#### 4.4.4 代码实践

**实践目标（本讲核心任务）**：给定若干 `block_idx`，判断它落在 Q/K/V 哪个区间，给出 `store` 的对应分支、KV 寻址结果与屏障槽号。

**操作步骤**（心算 + 对照源码）：

对下面三个 `block_idx`，逐个填写表格（`matvec_block_size=16`，`head_dim=64`）：

| block_idx | 区间 | 分支 | head_idx | dim_idx | TMA 目标坐标 | 是否 RoPE | 屏障槽 `block_idx/4` |
| --- | --- | --- | --- | --- | --- | --- | --- |

1. `block_idx = 5`
2. `block_idx = 130`
3. `block_idx = 165`

**需要观察的现象 / 预期结果**：

| block_idx | 区间 | 分支 | head_idx | dim_idx | TMA 目标坐标 | RoPE | 屏障槽 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5 | `[0,128)` | Q | — | — | `q_post_rope {0,0,0,5}` | 是（5<160） | 1（Q head 1） |
| 130 | `[128,160)` | K | `(130-128)*16=32 → 32/64=0` | `(32%64)/16=2` | `k_cache {layer,pos_id,0,2}` | 是（130<160） | 32（K head 0，即 `32+0`） |
| 165 | `[160,192)` | V | `(165-160)*16=80 → 80/64=1` | `(80%64)/16=1` | `v_cache {layer,pos_id,1,1}` | 否（165≥160） | 41（V head 1，即 `40+1`） |

**额外验证（atomicAdd 的作用）**：

- 解释"为什么 K head 0 的屏障槽是 32"：K 的 8 个头占据屏障最后维的 `[32,40)`，第 0 个 K 头就是 32；这与 attention 端等待的 `NUM_ATTENTION_HEADS + kv_head_idx = 32 + 0` 完全一致。
- 解释"为什么阈值是 4"：每个 head 64 维 = 4 个 16 元素块，4 个块各 +1 才凑齐一个完整 head；attention 端 `< 4` 的自旋正是等齐这 4 段。

> 待本地验证：可在 `store` 的 `atomicAdd` 前后各加一行 `printf("block=%d slot=%d\n", block_idx, block_idx/4)`（仅 `MK_DEBUG` 下），运行单层、单 token 推理，观察同一个 `block_idx/4` 是否恰好被打印 4 次（来自 4 个相邻块），以及 attention 是否在其后才启动。

#### 4.4.5 小练习与答案

**练习 1**：`block_idx = 128` 属于哪个分支？写出它的 KV 寻址和屏障槽。
**答案**：`128` 满足 `block_idx >= K_BLK_START(128)` 且 `< V_BLK_START(160)`，所以是 **K** 分支。`base_index = (128-128)*16 = 0`，`head_idx = 0/64 = 0`，`dim_idx = (0%64)/16 = 0`，即 `k_cache {layer, pos_id, 0, 0}`——这是 K head 0 的第 0 段。屏障槽 `128/4 = 32`（K head 0）。注意 `block_idx < K_BLK_START` 用的是严格小于，所以恰好 128 落在 K 而非 Q。

**练习 2**：如果把 `atomicAdd` 删掉、只保留 `store_async_wait`，会发生什么？
**答案**：TMA store 会照常把 K/V 写进 cache，但屏障永远不会到 4，下游 `attention_partial` 的自旋 `while (... < 4)` 会永远 spin，整个推理死锁。反之，如果保留 `atomicAdd` 但删掉 `store_async_wait`，则可能"信号先到、数据后到"，attention 读到不完整的 K/V，结果错误。二者必须按"先确保可见、再发信号"的顺序成对出现。

**练习 3**：`store` 里所有 TMA 都用 `EVICT_LAST`，与 `load_iter` 的 `EVICT_FIRST` 相反，为什么？
**答案**：`q_post_rope` / `k_cache` / `v_cache` 是下游 attention 紧接着就要读的热点数据，`EVICT_LAST` 让它们留在 L2；而每个输出块的权重只用一次，所以载入时用 `EVICT_FIRST`。

---

## 5. 综合实践

**任务**：在纸上完整跟踪一个 K 块从"指令解析 → 权重载入 → matvec 归约 → RoPE → TMA 追加进 KV cache → 屏障解锁"的全过程，并写出每一步对应的源码位置。

假设指令 `{1, 2, 128, 132}`（opcode=1，layer=2，`start_block_idx=128`，`end_block_idx=132`，即只产出 4 个块，正好覆盖 K head 0 的 4 段）。请按下列顺序作答：

1. **解析**：`parsed_instruction` 得到什么字段？（参考 4.1）→ `layer_idx=2, start_block_idx=128, end_block_idx=132, iters=4`。源码 [rms_matvec_rope_append.cu:36-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L36-L42)。
2. **跨层等待**：本 op 会等哪个屏障到多少？（参考 4.2）→ 等 `Bar[{1, OPCODE_DownProjResidual-1=5, 0}]` 到 `512`。源码 [rms_matvec_rope_append.cu:52-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L52-L58)。
3. **权重载入**：对 `iter=0`，`load_iter` 以什么坐标载入哪片权重？（参考 4.2）→ `block_idx=128`，`g.qkv_weights {2, 128, col_idx}`，`col_idx=0..3`。源码 [rms_matvec_rope_append.cu:66-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L66-L69)。
4. **归约 + RoPE**：`head_chunk = 128 % 4 = 0`，取 RoPE 表第 0 段；`128 < 160` 所以做 RoPE。源码 [rms_matvec_rope_append.cu:96-121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L96-L121)。
5. **TMA 追加**：落到 K 分支，`base=0, head_idx=0, dim_idx=0`，写 `k_cache {2, pos_id, 0, 0}`。源码 [rms_matvec_rope_append.cu:132-141](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L132-L141)。
6. **屏障解锁**：4 个块（`iter=0..3`，`block_idx=128..131`）都写完后，屏障槽 `128/4=32` 被累加到 **4**，解锁等待 K head 0 的 attention。源码 [rms_matvec_rope_append.cu:156-162](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L156-L162)。

**延伸思考**：这条指令只写了 K head 0。要让 attention 真正跑起来，还需要写 Q 的对应 4 个头、以及 V head 0，它们由另外的 QKV 指令（`start/end` 落在 `[0,128)` 和 `[160,192)`）完成，各自解锁不同的屏障槽。这正是"一条指令只负责一段输出区间、多条指令协作完成整层 QKV"的设计。

## 6. 本讲小结

- `parsed_instruction` 把 32 个 `int` 的裸指令解析成 `layer_idx / start_block_idx / end_block_idx / iters`，块号以 16 元素为单位，区间为半开 `[start, end)`。
- op 通过 `pipeline_specifics` 的三个回调（`gmem_wait`/`load_iter`/`store`）插入通用 matvec 流水线：`gmem_wait` 在 consumer 起始等待 **上一层 downproj** 的屏障到 512；`load_iter` 在 loader 里给每个权重块定 TMA 寻址；`store` 在 storer 里完成 RoPE 与三路写出。
- RoPE 只对 Q、K 做（`block_idx < V_BLK_START`），用 `__shfl_sync` 取交错配对邻居，逐 lane 完成 `x' = x·cos ∓ pair·sin`；`head_chunk = block_idx % 4` 选 RoPE 表段号。
- `store` 按 `block_idx` 落在 `[0,128)/[128,160)/[160,192)` 分别写 `q_post_rope / k_cache / v_cache`，K/V 用 `{layer,pos_id,head_idx,dim_idx}` 寻址完成 **append**。
- `store_async_wait` 必须先于 `atomicAdd`，保证"数据全局可见"后再发信号；`atomicAdd(&g.Bar[{layer, opcode-1, block_idx/4}], 1)` 把 4 个块归并到一个 head，累加到 4 即解锁下游 attention。

## 7. 下一步学习建议

1. **顺 op 链往下走**：本讲是 opcode 1。建议下一讲精读 [attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu)（opcode 2），看它如何 **消费** 这里写出的 `q_post_rope / k_cache / v_cache` 和 `Bar` 屏障，并体会"屏障成对：生产者 `atomicAdd`、消费者自旋 `< 4`"的另一半。
2. **对比另一种 matvec op**：读 [matvec_adds.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu)（downproj/o_proj），它们复用同一个 `matvec_pipeline`，但 `store` 用的是 `tma::store_add_async`（残差累加）并把屏障一次性 `+= iters`，与本 op 的"逐块 `+= 1`"形成对照。
3. **回到流水线本身**：如果对 `loader_loop / consumer_loop / storer_loop` 的 page 分配与信号量轮转还不够熟，建议重读 `u8-l3` 对应的 [matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) 与 [rms_matvec_pipeline::consumer_loop](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L303-L348)，重点看 RMSNorm 是如何在 consumer_loop 开头与 `gmem_wait` 衔接的。
