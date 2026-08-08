# RMSNorm 前向与反向

## 1. 本讲目标

本讲解读 QuACK 的旗舰归约内核 **RMSNorm**（及其反向 `RMSNormBackward`），并学会用 `QuackRMSNorm` 直接替换 `torch.nn.RMSNorm`。

读完本讲你应该能够：

- 说清 RMSNorm 的数学定义，以及 QuACK 如何把 `rms`（均方根）、`weight/bias` 仿射、`residual` 融合进**一次内核启动**。
- 读懂 `RMSNorm` 前向内核：它和 Softmax（[u2-l2](u2-l2-softmax-fwd.md)）共享 `ReductionBase`，但 stage、归约值个数、`reload_from` 都不同。
- 读懂 `RMSNormBackward` 反向内核：为什么它是**持久化内核**（grid 由 `sm_count` 决定）、为什么用 `stage=2`、为什么要把 `rstd`（反标准差）缓存在显存里、`dw` 为什么以 fp32 partial 的形式写出再在 host 端求和。
- 理解 autograd 集成（`RMSNormFunction`）和 drop-in 替换模块 `QuackRMSNorm`。

本讲承接 [u2-l1 ReductionBase 共享基类](u2-l1-reduction-base.md) 与 [u2-l4 归约原语](u2-l4-reduce-primitives.md)，是归约家族的最后一篇核心讲义。

## 2. 前置知识

### 2.1 RMSNorm 的数学

给定输入行 \(x \in \mathbb{R}^N\)，RMSNorm 的前向是：

\[
\text{rms}(x) = \sqrt{\frac{1}{N}\sum_{i=1}^{N} x_i^2 + \varepsilon}, \qquad
\hat{x}_i = \frac{x_i}{\text{rms}(x)} = x_i \cdot \text{rstd}, \qquad
\text{rstd} = \frac{1}{\text{rms}(x)}
\]

\[
y_i = \hat{x}_i \cdot (w + w_{\text{off}}) + b
\]

其中 `rstd`（reciprocal standard deviation，反标准差 \(1/\text{rms}\)）是一次行内归约的结果；\(w_{\text{off}}\) 是 Gemma 风格的「\(1+w\)」常量偏移。

> 和 LayerNorm 的区别：LayerNorm 要先减均值再除标准差，所以需要**两次**行内归约（mean、variance）；RMSNorm 不减均值，只需**一次**行内归约（平方和）。QuACK 用同一个内核同时支持两者，差别就体现在 `stage` 和归约值个数上——这正是本讲的实践要点之一。

### 2.2 反向梯度

设上游梯度为 \(d_{\text{out}}\)，令 \(g = d_{\text{out}}\)，定义 \(wdy = g \cdot (w+w_{\text{off}})\)。则对 \(x\) 的梯度为：

\[
c_1 = \frac{1}{N}\sum_i \hat{x}_i \cdot wdy_i, \qquad
dx_i = (wdy_i - \hat{x}_i \cdot c_1) \cdot \text{rstd}
\]

\[
dw_j = \sum_m g_{m,j}\cdot \hat{x}_{m,j}, \qquad db_j = \sum_m g_{m,j}
\]

关键观察：\(dx\) 需要再一次行内归约（求 \(c_1\)），而且要复用前向算出的 \(\hat{x}\) 与 `rstd`。**这就是为什么反向需要 `rstd` 缓冲**——前向把 `rstd` 写到显存，反向直接读回来，避免在反向里重新算一遍平方和。

参考实现见 [quack/rmsnorm.py:578-594](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L578-L594)，可用作理解内核行为的「标准答案」。

### 2.3 ReductionBase 速记（来自 u2-l1）

所有归约内核共享基类 `ReductionBase`（[quack/reduction_base.py:12-94](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L12-L94)），它固化了「配置 cluster → 取 tiled copy → 分配归约缓冲与 mbarrier → 启动 kernel」的模板：

- `_get_tiled_copy(vecsize)` 由 N、vecsize、threads_per_row、cluster_n 推出 `tiler_mn`（CTA 数据块形状）。
- `_cap_cluster_n(vecsize)` 把过大的 cluster_n 夹回，避免 peer CTA 折叠到同一块而重复计数。
- `_get_reduction_buffer_layout` 决定归约缓冲的三维布局 `(rows, (warps_per_row, cluster_n), num_slots)`。
- `stage` 是**同步阶段数**（双缓冲跨行迭代时为 2）；`reduction_dtype` 是归约缓冲的精度。

本讲关注 RMSNorm 如何配置这两个字段。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/rmsnorm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py) | 主文件：`RMSNorm`/`RMSNormBackward` 内核类、前向/反向的 `cute_op`/`compile`/公共 API、`RMSNormFunction` autograd 包装、`QuackRMSNorm` 模块、参考实现 |
| [quack/rmsnorm_config.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm_config.py) | 启动配置：`RmsNormFwdConfig`/`RmsNormBwdConfig` 数据类、各架构启发式、`get_sm_count`、autotune 配置空间与剪枝 |
| [quack/rms_final_reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rms_final_reduce.py) | `RmsFinalReduce`：融合 GEMM+RMS 流水线末端的「二次归约」内核，把逐 tile 的部分平方和归约成 `rstd` |
| [quack/reduction_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py) | 共享基类（前置讲义已讲） |
| [quack/reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) | `row_reduce`/`cluster_reduce` 归约原语（前置讲义已讲） |

## 4. 核心概念与源码讲解

### 4.1 RMSNorm 前向内核

#### 4.1.1 概念说明

`RMSNorm`（[quack/rmsnorm.py:66-362](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L66-L362)）是单次启动的融合内核，把以下操作压成一个 kernel：

1. 读入 `x`（可选融合 `residual`：`x += residual`）；
2. 行内归约求平方和 → `rstd = rsqrt(mean(x²)+eps)`；
3. 归一化 \(\hat{x}=x\cdot\text{rstd}\)；
4. 仿射 `y = x_hat * (w + w_off) + bias`；
5. 可选写出 `residual_out`（融合残差后的 x）、`rstd`（供反向复用）、`mean`（LayerNorm 用）。

它继承 `ReductionBase`，但与 Softmax 的关键差异在**配置**上。

#### 4.1.2 核心流程

前向是**非持久化**内核（grid 直接按行数切分），数据流为 `gmem → smem → rmem（寄存器）→ 归约 → gmem`，前向走 `cp.async` 而非 TMA：

```
主机侧 __call__（@cute.jit）:
  1. _set_cluster_n()        # 按 arch 选 cluster_n（SM8x=1，SM12x≤8，其余≤16）
  2. 算 vecsize              # gcd(N, 128//width)
  3. _cap_cluster_n(vecsize) # 夹回，避免 peer 折叠
  4. _get_tiled_copy(vecsize)# 得 tiler_mn、num_threads
  5. launch kernel(grid=ceil(M/tiler_mn[0]), cluster_n, num_heads)

设备侧 kernel（@cute.kernel）:
  A. 分配 smem：sX、(sRes)、reduction_buffer、mbar
  B. cluster 初始化（_initialize_cluster，仅 cluster_n>1）
  C. 谓词 predicate_k（仅 N 不整除时）
  D. gmem→smem 异步拷贝（x、residual），commit/wait
  E. smem→rmem，x.load().to(Float32)；若有 residual：x += residual
  F. 归约：
       - RMSNorm: sum_sq_x = row_reduce(x*x, ADD)  → rstd = rsqrt(sum_sq_x/N + eps)
       - LayerNorm: 先 mean，再 sum_sq(x-mean)，两次归约用两个 buffer slot
  G. 写出 rstd（仅列 0 线程 + cluster 主 CTA）
  H. 若 reload_from：从 smem/gmem 重新读 x（省寄存器，代价是带宽）
  I. x_hat = x*rstd；y = x_hat*(w+w_off)+bias；store 回 gmem
```

#### 4.1.3 源码精读

**配置差异点（`__init__`）**。RMSNorm 通过 `is_layernorm` 决定 `stage`：RMSNorm 路径只需 1 个同步阶段（一次归约），LayerNorm 需要 2 个（mean、variance）：

```python
super().__init__(dtype, N, stage=2 if is_layernorm else 1)
```
见 [quack/rmsnorm.py:66-89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L66-L89)。`reduction_dtype` 没显式传，用基类默认 `Float32`（[reduction_base.py:13](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L13)）——这点和 Softmax 不同：Softmax 的 online 路径把 (max,sum) 打包成 Int64 传输，而 RMSNorm 始终用 Float32 标量缓冲。

> **与 Softmax 的 stage/reduction_dtype 对比**（实践要点）：
> - **stage**：RMSNorm fwd 在 RMSNorm 模式下 `stage=1`（单次平方和归约），LayerNorm 模式下 `stage=2`（mean + variance 两次）。Softmax fwd 的 online 路径用「1 个 Int64 槽」耦合 (max,sum)，非 online 用「2 个 Float32 槽」分别做 MAX、ADD。
> - **reduction_dtype**：RMSNorm 全程 `Float32`；Softmax online 把两个 f32 打包成 `Int64`。
> - **归约算子**：RMSNorm 只做 ADD；Softmax 还需 MAX/MIN。

**主机侧启动**（[quack/rmsnorm.py:107-148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L107-L148)）。注意 grid 的第二维是 `cluster_n`、第三维是 `num_heads`（per-head 权重时），cluster 沿第二维展开：

```python
).launch(
    grid=[cute.ceil_div(mX.shape[0], tiler_mn[0]), self.cluster_n, num_heads],
    block=[num_threads, 1, 1],
    cluster=[1, self.cluster_n, 1] if const_expr(self.cluster_n > 1) else None,
    stream=stream,
)
```

**RMSNorm 归约主路径**（[quack/rmsnorm.py:312-324](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L312-L324)）。这是 RMSNorm 最核心的一行——直接对 `x*x` 求和，`hook_fn=cluster_wait` 把集群同步重叠到 warp 归约上（来自 [u2-l4](u2-l4-reduce-primitives.md)）：

```python
sum_sq_x = row_reduce(
    x * x, cute.ReductionOp.ADD, threads_per_row,
    reduction_buffer[None, None, 0], mbar_ptr, init_val=0.0,
    hook_fn=cute.arch.cluster_wait if const_expr(self.cluster_n > 1) else None,
)
rstd = cute.math.rsqrt(sum_sq_x / shape[1] + eps, fastmath=True)
```

**写出 rstd**（[quack/rmsnorm.py:325-332](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L325-L332)）。只有「列 0 线程」且「cluster 内主 CTA」写，避免重复写：

```python
if (tXcX[0][1] == 0 and row < shape[0]
        and (self.cluster_n == 1 or cute.arch.block_idx_in_cluster() == 0)):
    tXrRstd[0] = rstd
```

这个写出的 `rstd` 就是反向要读的「rstd 缓冲」。

**reload_from 的取舍**（[quack/rmsnorm.py:84-85, 338-349](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L338-L349)）。当 N 较大时，归约后寄存器里的 `x` 可能被回收以省寄存器，于是归一化前从 smem（`"smem"`）或 gmem（`"gmem"`）重新加载 x。阈值见 [rmsnorm_config.py:84](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm_config.py#L84)（`reload_threshold = 16*1024 if is_layernorm else 8*1024`）。

**编译缓存与 fake 张量**（[quack/rmsnorm.py:426-470](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L426-L470)）。`_compile_rmsnorm_fwd` 用 `cute.sym_int()` 表示 batch 维（甚至 head 维），让一份编译产物对任意 batch 复用；N 是具体整数，会特化进 cubin。这正是 [u2-l6 cute_op 与 jit_cache](u2-l6-cute-op-and-jit-cache.md) 的模式。

#### 4.1.4 代码实践

**实践目标**：亲手比较 RMSNorm 内核输出与参考实现，并确认「forward 把 rstd 写出、backward 不重算」。

**操作步骤**（需有支持 SM90/100/120 的 GPU，且已 `pip install -e '.[dev]'`）：

1. 写一个最小脚本（保存为文件，不要在 REPL 里定义——见 [u1-l4 DSL 源码落盘约束](u1-l4-cute-dsl-model.md)）：

   ```python
   # rmsnorm_demo.py
   import torch
   from quack.rmsnorm import rmsnorm, rmsnorm_ref, rmsnorm_fwd

   torch.manual_seed(0)
   M, N = 199, 4096
   x = torch.randn(M, N, device="cuda", dtype=torch.bfloat16, requires_grad=True)
   w = torch.randn(N, device="cuda", dtype=torch.float32, requires_grad=True)

   # 用 autograd 入口（默认 store_rstd 由 needs_input_grad 自动决定）
   out = rmsnorm(x, w, eps=1e-6)
   out_ref = rmsnorm_ref(x, w, eps=1e-6)
   print("fwd max abs diff:", (out - out_ref).abs().max().item())

   # 显式 store_rstd=True，拿回 rstd 张量（反向会复用它）
   _, _, rstd = rmsnorm_fwd(x, w, eps=1e-6, store_rstd=True)
   print("rstd shape:", tuple(rstd.shape), "dtype:", rstd.dtype)  # (M,), float32

   out.sum().backward()
   print("x.grad ok:", x.grad is not None, "w.grad ok:", w.grad is not None)
   ```

2. 运行：`python rmsnorm_demo.py`（首次会触发冷编译，可能耗时数十秒到数分钟）。

3. 跑一条数值正确性测试：`pytest tests/test_rmsnorm.py::test_rmsnorm_forward_backward -x -k "bfloat16 and 4096"`。

**需要观察的现象**：
- 前向 diff 应在 bf16 容差内（参考 [tests/test_rmsnorm.py:22-26](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_rmsnorm.py#L22-L26) 的 `TOLERANCES`，bf16 为 `1e-1`）。
- `rstd` 形状是 `(M,)`、dtype 是 `float32`——确认它是一个独立的 fp32 缓冲。
- `backward` 能正常拿到 `x.grad` 和 `w.grad`，无需重新跑前向。

**预期结果**：前向 diff 远小于 `1e-1`；`rstd` 为 `(199,)` 的 float32。若首次编译报 `OSError: could not get source code`，说明你在 REPL 里定义了内核——改用文件即可。

> 如果无法在本地运行 GPU，明确标注「待本地验证」；可改为源码阅读型实践：跟踪 [rmsnorm.py:1635](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1635) 的 `ctx.save_for_backward`，确认前向保存了 `rstd`。

#### 4.1.5 小练习与答案

**练习 1**：把上面 demo 的 `rmsnorm` 换成 `is_layernorm=True` 路径（即调用 `layernorm_fwd`），前向内核会做几次行内归约？为什么？
**答案**：两次。第一次求 `mean`，第二次求 `sum_sq(x-mean)`。对应 [rmsnorm.py:265-311](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L265-L311)，且 `stage=2`、用了 `reduction_buffer[None,None,0]` 与 `[...,1]` 两个 slot。

**练习 2**：为什么 `reload_from` 只在大 N 时启用（`"smem"`/`"gmem"`），小 N 时为 `None`？
**答案**：小 N 时 x 片段占的寄存器少，归约后留在寄存器里直接复用最省带宽；大 N 时寄存器压力大，编译器可能把 x 溢出，主动从 smem 重载反而更稳更快。阈值见 [rmsnorm_config.py:84](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm_config.py#L84)。

---

### 4.2 RMSNormBackward 反向内核

#### 4.2.1 概念说明

`RMSNormBackward`（[quack/rmsnorm.py:597-1205](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L597-L1205)）是 QuACK 归约家族里**最复杂**的内核：它是**持久化内核**（CTA 在多个行 tile 间循环，grid 大小由 `sm_count` 决定而非行数），同时计算 \(dx\)、\(dw\)、\(db\)、\(d_{\text{residual}}\)，并把 \(dw/db\) 以 **fp32 partial** 形式写到 `(sm_count, N)` 缓冲，最后在 host 端 `sum(dim=0)` 收敛。

它依然继承 `ReductionBase`，但与 Softmax 反向（单次启动、双 SMEM）走的是完全不同的路线。

#### 4.2.2 核心流程

```
主机侧 __call__（@cute.jit）:
  1. _set_cluster_n()、_cap_cluster_n()
  2. 若 USE_TMA：构造 X/dO 的 TMA descriptor
  3. launch kernel(grid=[num_blocks=sm_count, cluster_n, num_heads])
     ← grid 第一维是持久 CTA 数，不是行数！

设备侧 kernel（@cute.kernel）持久主循环:
  A. 分配多级 smem：sX、sdO（带 smem_stages 维）、reduction_buffer、TMA mbar
  B. 创建数据流水线（TMA 或 cp.async），预取 NUM_PIPE_STAGES-1 批
  C. 创建归约流水线 PipelineStasAsync（cluster_n>1 时，跨 CTA 交换 partial）
  D. for bidx in 持久循环（步长 gdim，跨行 tile）:
       i.   生产者：预取下一批 X/dO 到 smem
       ii.  消费者：wait，smem→rmem，x_hat = x*rstd，wdy = dout*(w+w_off)
       iii. 行内归约 mean_xhat_wdy = row_reduce(x_hat*wdy)/N
            （LayerNorm 还额外归约 mean_wdy，共用一个 barrier）
       iv.  跨 CTA：若 cluster_n>1，STAS 交换 partial 并等待（phase 驱动）
       v.   dx = (wdy - x_hat*mean_xhat_wdy)*rstd  (+ dresidual_out)
       vi.  store dx；累加 dw_partial += dout*x_hat；db_partial += dout
       vii.推进两个流水线的 phase
  E. 循环后：CTA 内把 dw_partial 各行 tile 归约成一行的 fp32（barrier + 行归约）
  F. producer_tail：等所有 peer CTA 排空
```

#### 4.2.3 源码精读

**配置：stage=2、reduction_dtype=Float32、bufs_per_stage**（[quack/rmsnorm.py:614-639](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L614-L639)）：

```python
self.bufs_per_stage = 2 if is_layernorm else 1
...
super().__init__(dtype, N, stage=2, reduction_dtype=Float32)
```

> **为什么反向 `stage=2`？** 这里 stage 是「双缓冲跨行迭代的同步阶段数」，不是「归约值个数」。持久化内核在多个行 tile 间循环，上一行的跨 CTA 归约还在飞行时，下一行就可以开始归约，于是需要 2 个同步阶段（双缓冲）让两行的集群交换重叠。归约值的个数由 `bufs_per_stage` 决定：RMSNorm 每行归约 1 个值（`mean_xhat_wdy`），LayerNorm 归约 2 个（`mean_xhat_wdy` + `mean_wdy`），它们**共用同一对 barrier**，缓冲只是「加宽」。

**为什么需要额外的 rstd 缓冲**（实践要点）。反向每个行迭代的开头直接读回前向写出的 `rstd`：

```python
rstd = cutlass.Float.zero
if row < M or tiler_mn[0] == 1:
    rstd = mRstd[row]
```
见 [quack/rmsnorm.py:1036-1041](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1036-L1041)。`mRstd` 是一个 `(M,)` 的 float32 张量，由前向写入（见 4.1.3）。反向用它构造 `x_hat = x * rstd`，于是梯度公式 \(dx=(wdy-\hat{x}\cdot c_1)\cdot\text{rstd}\) 里的 `rstd` 直接来自这个缓冲。**如果没有 rstd 缓冲，反向就必须重新对 `x*x` 做一次行内归约来算 rms，再取倒数——多一次跨 CTA 归约往返、多一次寄存器压力。** 把它缓存下来，反向的行内归约只剩一次（求 \(c_1\)）。`x_hat` 也不存，而是用读回的 `rstd` 现算（`x_hat = x * rstd`），既省显存又省一次写出。

**持久化 grid**（[quack/rmsnorm.py:696-721](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L696-L721)）。grid 第一维是 `num_blocks = sm_count`，由 [rmsnorm_config.py:289-293](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm_config.py#L289-L293) 的 `get_sm_count` 给出。持久化让每个 CTA 处理多行，从而把每行的 `dw_partial` 累加进同一份 fp32 寄存器，减少写出次数。

**行内归约主路径**（[quack/rmsnorm.py:1094-1105](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1094-L1105)）：

```python
mean_xhat_wdy = (
    row_reduce(
        x_hat * wdy, cute.ReductionOp.ADD, threads_per_row,
        reduction_buffer[None, None, red_slot],
        red_mbar, phase=cons_state_reduce.phase, init_val=0.0,
    ) / shape[1]
)
```

注意这里传了 `phase=cons_state_reduce.phase`——持久化内核里 mbarrier 是**分相位复用**的（每轮翻转相位），由 `PipelineStasAsync` 的状态机驱动（见 [pipeline.py:387-521](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/pipeline.py#L387-L521)）。这是它与前向（一次性 mbarrier）的本质区别。

**跨 CTA 归约（cluster_n>1）**。`red_mbar` 来自 `pipeline_reduce.producer_get_barrier`（[rmsnorm.py:1066-1070](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1066-L1070)），底层走 [reduce.py:114-161](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L114-L161) 的 `cluster_reduce`：每个 CTA 用 `st.async`（STAS）把自己的 partial 写进每个 peer 的缓冲并 credit 对端的事务屏障，一次 `mbarrier_wait` 收齐所有 peer（全连接广播）。LayerNorm 的两个归约值 `(x_hat*wdy, wdy)` 作为 tuple 传入，**共用一个 barrier**——一次 combined-tx 上膛、一次 wait，省一次集群往返（见 [rmsnorm.py:1079-1090](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1079-L1090) 与 [cluster_reduce 的 docstring](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L114-L129)）。

**dx 与 dw/db 累加**（[quack/rmsnorm.py:1130-1144](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1130-L1144)）：

```python
dx = (wdy - x_hat * mean_xhat_wdy) * rstd        # RMSNorm
# dx = (wdy - mean_wdy - x_hat * mean_xhat_wdy) * rstd  # LayerNorm
...
if const_expr(mdW is not None):
    tXrdW.store(tXrdW.load() + dout * x_hat)     # fp32 累加 dw_partial
if const_expr(mdB is not None):
    tXrdB.store(tXrdB.load() + dout)             # fp32 累加 db_partial
```

**循环后 CTA 内 dw 归约**（[quack/rmsnorm.py:1154-1176](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1154-L1176)）。因为持久化 CTA 沿行 tile 推进，但 `tiler_mn[0]` 可能 >1（一个 CTA 处理多行），同 CTA 的多个行 tile 各自累加了 `dw_partial`，循环结束后用 `barrier` + 行归约把它们合并成一行写出。最终 `dw_partial` 形状是 `(sm_count, N)`，host 端再 `sum(dim=0)`（见 4.3 与 [rmsnorm.py:1378](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1378)）。

**fp32 partial 的精度考量**。注释 [rmsnorm.py:837-838](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L837-L838) 明确：partial 权重梯度始终在 fp32 累加，避免跨大量行累加时丢精度。这正是 `dw_partial` 用 float32 的原因。

#### 4.2.4 代码实践

**实践目标**：理解「持久化 grid + fp32 partial + host 求和」这一套，并验证 `dw` 的数值正确性。

**操作步骤**：

1. 复用 4.1.4 的 demo，加上对 `dw` 的精度检查（对照 [tests/test_rmsnorm.py:104-109](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_rmsnorm.py#L104-L109) 的 weight 容差策略）：

   ```python
   from quack.rmsnorm import rmsnorm_bwd_ref
   x_ref = x.detach().clone().requires_grad_()
   w_ref = w.detach().clone().requires_grad_()
   rmsnorm_ref(x_ref, w_ref, eps=1e-6).sum().backward()
   w_atol = 2 * torch.finfo(w.dtype).eps * w_ref.grad.abs().max()
   print("dw max abs diff:", (w.grad - w_ref.grad).abs().max().item(),
         "tol:", w_atol.item())
   ```

2. 直接调用底层 `rmsnorm_bwd`，观察返回的 `dw_partial`：

   ```python
   from quack.rmsnorm import rmsnorm_bwd, rmsnorm_fwd
   _, _, rstd = rmsnorm_fwd(x.detach(), w, eps=1e-6, store_rstd=True)
   dx, dw, db, dres = rmsnorm_bwd(x.detach(), w, out.detach(), rstd,
                                  has_bias=False, has_residual=False)
   print("dw.dtype:", dw.dtype, "≈ w_ref.grad:", torch.allclose(dw, w_ref.grad, atol=w_atol))
   ```

3. 跑反 向 测试：`pytest tests/test_rmsnorm.py::test_rmsnorm_forward_backward -x -k "float32 and 1024"`。

**需要观察的现象**：
- `dw` 的 diff 应在 `w_atol`（与 weight 的 fp32 eps 与最大梯度相关）以内。
- 调用 `rmsnorm_bwd` 时返回的 `dw` 已经是收敛后的 `(N,)` 张量（host 端做过 `sum(dim=0)`），不是 partial。

**预期结果**：`dw max abs diff` 远小于 `tol`；`dw.dtype` 与 weight 相同（float32）。若想观察 partial 形状，需读源码 [rmsnorm.py:1396](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1396)——partial 在内部 `(sm_count, N)`，对用户不可见。

> 无 GPU 时标注「待本地验证」，改为源码阅读型实践：跟踪 `dw_partial` 的生命周期——分配（[rmsnorm.py:1396](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1396)）→ 内核累加（[rmsnorm.py:1142](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1142)）→ host 求和（[rmsnorm.py:1417](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1417)）。

#### 4.2.5 小练习与答案

**练习 1**：反向内核里 `red_slot = cons_state_reduce.index * self.bufs_per_stage`（[rmsnorm.py:1063](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1063)）。为什么 RMSNorm 时 `bufs_per_stage=1`、LayerNorm 时为 2？
**答案**：RMSNorm 每行只需归约一个值（`mean_xhat_wdy`）；LayerNorm 需要两个（`mean_xhat_wdy` 和 `mean_wdy`）。两个值共用同一对 barrier（见 [cluster_reduce docstring](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L114-L129)），所以只「加宽缓冲槽」而不增加 barrier——`bufs_per_stage` 控制的就是每个同步阶段的槽位数。

**练习 2**：为什么 `dw_partial` 要存成 `(sm_count, N)` 的 fp32，再在 host 求和，而不是让内核直接原子累加到 `(N,)`？
**答案**：(1) 精度——跨大量行的累加在 fp32 partial 上做，最后一次性 `sum` 比边算边原子累加到低精度更稳；(2) 性能——原子累加会有竞争，而每个持久 CTA 写自己的 partial 行无冲突。host 端的一次小张量求和很便宜。

---

### 4.3 autograd 集成与 QuackRMSNorm 模块

#### 4.3.1 概念说明

为了让 `quack.rmsnorm` 像 `torch.nn.functional` 一样支持 `.backward()`，需要两层封装：

1. **`RMSNormFunction(torch.autograd.Function)`**：把前向/反向接成一对，管理 `save_for_backward`。
2. **`rmsnorm()` 公共包装**：负责把任意 batch 形状「拍平」成内核期望的 2D/3D，再进 `.apply()`。

最上层 **`QuackRMSNorm(torch.nn.RMSNorm)`** 直接继承官方模块，只覆盖 `forward` 调 `rmsnorm`，实现 drop-in 替换。

#### 4.3.2 核心流程

```
用户调用 quack.rmsnorm(x, w):
  1. rmsnorm() 把 x 拍平为 (M, N) 或 (M, H, N)
  2. RMSNormFunction.apply(...) → forward:
       a. need_grad = any(ctx.needs_input_grad[:3])  # 是否要反向
       b. rmsnorm_fwd(..., store_rstd=need_grad)     # 需要时才存 rstd
       c. save_for_backward(x or residual_out, weight, rstd)
  3. .backward(dout):
       a. 取回 saved_tensors (含 rstd)
       b. rmsnorm_bwd(...) 算 dx, dw, db, dresidual
       c. 按 forward 的输入顺序返回梯度（None 填充非张量参数）
```

#### 4.3.3 源码精读

**`rmsnorm()` 的拍平**（[quack/rmsnorm.py:1691-1706](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1691-L1706)）。注释解释了为什么要早于 `autograd.Function` 拍平：让张量秩由 `per_head`（dynamo 据此 guard）决定，而非原始输入形状，保证 `torch.compile` 在 per_head 切换时能正确重编译反向子图。

**`forward` 的 save 策略**（[quack/rmsnorm.py:1635](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1635)）：

```python
ctx.save_for_backward(x if residual is None else residual_out, weight, rstd)
```

这里有个与 Softmax（[u2-l3](u2-l3-softmax-bwd-autograd.md)）类似的取舍：当有 `residual` 时，存的是融合后的 `residual_out`（= x+residual）而非原始 x，因为反向的 \(x\) 实际是融合后的输入。`store_rstd=need_grad` 表示只有需要反向时才写 rstd 缓冲——纯推理时省一次写出。

**`backward` 的对称返回**（[quack/rmsnorm.py:1645-1663](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1645-L1663)）。注意返回元组的长度必须与 forward 输入个数对齐：

```python
return dx, dw, db, dresidual, *([None] * 5)
```

后面的 `*([None] * 5)` 对应 forward 里 `out_dtype/residual_dtype/eps/prenorm/weight_offset` 这些非张量/不需要梯度的参数。这与 Softmax 的 autograd 集成是同一种模式（参见 [u2-l3](u2-l3-softmax-bwd-autograd.md)）。

**`QuackRMSNorm` drop-in 替换**（[quack/rmsnorm.py:1709-1738](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1709-L1738)）。它直接继承 `torch.nn.RMSNorm`，复用其 `weight` 参数与 `eps`，只覆盖 `forward`：

```python
class QuackRMSNorm(torch.nn.RMSNorm):
    def forward(self, x: Tensor) -> Tensor:
        return rmsnorm(x, self.weight, eps=self.eps)
```

> **注意限制**：`QuackRMSNorm` 当前只暴露 `rmsnorm(x, weight, eps)`，即**不含 bias / residual / weight_offset**。需要这些融合特性时，应直接调 `quack.rmsnorm()` 或 `rmsnorm_fwd/bwd`。官方 `torch.nn.RMSNorm` 没有 bias，所以这种简化是合理的。

#### 4.3.4 代码实践

**实践目标**：验证 `QuackRMSNorm` 能作为 `torch.nn.RMSNorm` 的 drop-in 替换，且数值一致。

**操作步骤**：

```python
import torch
from quack.rmsnorm import QuackRMSNorm

torch.manual_seed(0)
dim = 4096
x = torch.randn(8, dim, device="cuda", dtype=torch.bfloat16)

ref = torch.nn.RMSNorm(dim, eps=1e-6).cuda().to(torch.bfloat16)
q = QuackRMSNorm(dim, eps=1e-6).cuda().to(torch.bfloat16)
q.weight.data.copy_(ref.weight.data)   # 对齐权重

out_ref = ref(x)
out_q = q(x)
print("QuackRMSNorm vs torch.nn.RMSNorm max diff:", (out_q - out_ref).abs().max().item())
```

**需要观察的现象**：两者输出 diff 在 bf16 容差内（应远小于 `1e-1`），因为数学定义完全相同。

**预期结果**：diff 很小，证明 `QuackRMSNorm` 行为与 `torch.nn.RMSNorm` 一致，可安全替换。**待本地验证**（若无 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：`RMSNormFunction.forward` 里 `store_rstd=need_grad`（[rmsnorm.py:1632](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1632)）。如果把它改成 `store_rstd=True`，对纯前向推理（不调 backward）有什么影响？
**答案**：功能上仍正确，但会多分配一个 `(M,)` fp32 的 `rstd` 张量并多写一次显存——纯推理时这是浪费。所以按需写出是性能优化。

**练习 2**：为什么 `rmsnorm()` 要在 `autograd.Function.apply` **之前**就 `reshape` 拍平，而不是在 `forward` 里拍平？
**答案**：为了让张量秩在 `torch.compile` 的 guard 里固定下来。dynamo 按 `per_head` 分支拍平，决定了反向子图的秩；如果延迟到 `forward` 内拍平，per_head 切换时 dynamo 无法正确重编译反向。详见 [rmsnorm.py:1694-1698](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1694-L1698) 的注释。

---

### 4.4 补充：rms_final_reduce 融合末端

这一小节是选读，对应 `quack/rms_final_reduce.py`，理解「GEMM+RMS 融合流水线」的末端。

#### 4.4.1 概念与流程

`RmsFinalReduce`（[rms_final_reduce.py:27-118](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rms_final_reduce.py#L27-L118)）是一个**极简**的归约内核：输入是 `(M, N)` 的「部分平方和」（由前一个 GEMM 内核按 tile 写出），它对每行求和再 `rsqrt(sum*scale + eps)` 得到 `rstd`。

它继承 `ReductionBase`，`stage=1`、`cluster_n=1`（见 [rms_final_reduce.py:33-44](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rms_final_reduce.py#L33-L44)），本质就是「`RMSNorm` 前向去掉 weight/bias/residual，只保留归约那一步」。

#### 4.4.2 源码精读

核心两行（[rms_final_reduce.py:108-118](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rms_final_reduce.py#L108-L118)）：

```python
sum_x = row_reduce(x, cute.ReductionOp.ADD, threads_per_row,
                   reduction_buffer[None, None, 0], mbar_ptr, init_val=0.0)
rstd = cute.math.rsqrt(sum_x * scale + eps, fastmath=True)
```

`scale` 通常是 `1/total_columns`（把分摊到各 tile 的部分和还原成均值）。这个内核展示了 `ReductionBase` 的复用能力：同一个基类，配置不同，就能服务从 Softmax 到 RMSNorm 到融合末端等多种场景。

## 5. 综合实践

把本讲三部分串起来，完成一个「RMSNorm 全链路体检」：

**任务**：写一个脚本，依次完成：

1. 用 `rmsnorm_fwd` 做前向并 `store_rstd=True`，拿到 `out` 和 `rstd`。
2. 手动用 `rmsnorm_bwd`（绕过 autograd）做反向，传入上一步的 `rstd`，拿到 `dx, dw`。
3. 与 `rmsnorm_ref` / `rmsnorm_bwd_ref` 比对 `out`、`dx`、`dw` 的数值。
4. 在源码里标注：前向在哪一行写出 `rstd`、反向在哪一行读回 `rstd`、`dw_partial` 在哪一行被累加、在哪一行被 host 求和。

**参考骨架**：

```python
import torch
from quack.rmsnorm import rmsnorm_fwd, rmsnorm_bwd, rmsnorm_ref, rmsnorm_bwd_ref

torch.manual_seed(0)
M, N = 8192, 4096
x = torch.randn(M, N, device="cuda", dtype=torch.bfloat16)
w = torch.randn(N, device="cuda", dtype=torch.float32)
dout = torch.randn(M, N, device="cuda", dtype=torch.bfloat16)

# 1. 前向 + rstd
out, res_out, rstd = rmsnorm_fwd(x, w, eps=1e-6, store_rstd=True)

# 2. 手动反向（复用 rstd）
dx, dw, db, dres = rmsnorm_bwd(x, w, dout, rstd, has_bias=False, has_residual=False)

# 3. 比对参考
out_ref = rmsnorm_ref(x, w, eps=1e-6)
dx_ref, dw_ref = rmsnorm_bwd_ref(x, w, dout, rstd, eps=1e-6)
print("out:", (out - out_ref).abs().max().item())
print("dx :", (dx - dx_ref).abs().max().item())
print("dw :", (dw - dw_ref).abs().max().item())
```

**源码标注答案**（第 4 步）：

| 事件 | 位置 |
|------|------|
| 前向写出 rstd | [rmsnorm.py:325-332](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L325-L332) |
| 反向读回 rstd | [rmsnorm.py:1036-1041](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1036-L1041) |
| dw_partial 累加 | [rmsnorm.py:1142](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1142) |
| host 端 sum 收敛 | [rmsnorm.py:1417](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1417)（autograd 路径在 [1378](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L1378)） |

> 无 GPU 时标注「待本地验证」，把第 1-3 步改为阅读 `rmsnorm_ref`/`rmsnorm_bwd_ref`（[rmsnorm.py:563-594](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L563-L594)）并在纸面上推演一行的梯度。

## 6. 本讲小结

- **RMSNorm 前向**继承 `ReductionBase`，RMSNorm 模式 `stage=1`（一次平方和归约）、LayerNorm 模式 `stage=2`（mean + variance）；`reduction_dtype=Float32`，与 Softmax 的 online Int64 打包不同。
- **前向融合**把 `x += residual`、归约求 rstd、`x_hat = x*rstd`、`y = x_hat*(w+w_off)+bias`、写出 `residual_out/rstd/mean` 压进一个内核；大 N 时用 `reload_from` 从 smem/gmem 重载 x 以省寄存器。
- **反向是持久化内核**：grid 由 `sm_count` 决定，CTA 在行 tile 间循环；`stage=2` 是双缓冲同步阶段（不是归约值个数），`bufs_per_stage` 才决定每行归约几个值。
- **rstd 缓冲**是前向写出、反向读回的关键桥梁——反向直接复用前向的 `rstd` 算 `x_hat`，省掉一次额外的平方和归约；这是反向需要「额外缓冲」的根本原因。
- **dw/db 走 fp32 partial**：每个持久 CTA 累加自己的 `(N,)` partial，host 端 `sum(dim=0)` 收敛，兼顾精度（fp32 累加）与性能（无原子竞争）。
- **autograd 集成**用 `RMSNormFunction` 管理保存张量，`rmsnorm()` 负责提前拍平以兼容 `torch.compile`；`QuackRMSNorm` 继承 `torch.nn.RMSNorm` 实现 drop-in 替换（但不含 bias/residual/weight_offset）。

## 7. 下一步学习建议

- **归约家族收尾**：至此 rmsnorm/softmax/cross_entropy 三个归约内核已讲完。建议横向对比它们的 `ReductionBase` 配置（stage、reduction_dtype、reload 策略），自己画一张表。
- **进入 GEMM 体系**：归约内核的「tile/cluster/mbarrier/流水线」全部是 GEMM 的前置技能。下一站建议读 [u3-l1 copy_utils](u3-l1-copy-utils.md) 与 [u3-l5 异步流水线与同步原语](u3-l5-pipeline-sync.md)，那里会更系统地讲本讲反复出现的 `PipelineTmaAsync` 与 mbarrier 协作。
- **继续阅读源码**：想深入可读 [quack/rmsnorm_config.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm_config.py) 的 Blackwell 启发式（`_for_blackwell_bwd`），看 `use_tma`/`reload_x`/`reload_wdy` 如何由 `row_bytes` 与 `num_acc` 决定——这是把「寄存器压力」量化成配置选择的范例。
