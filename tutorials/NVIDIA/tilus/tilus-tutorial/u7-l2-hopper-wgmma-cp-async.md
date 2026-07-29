# Hopper：wgmma 与 cp_async 异步拷贝

## 1. 本讲目标

本讲聚焦 NVIDIA Hopper 架构（sm_90a）最具代表性的三类硬件能力，并讲解如何用 Tilus 的指令组（instruction group）直接驱动它们：

1. **wgmma 指令组** —— warp-group 级别的异步矩阵乘累加（`d = a @ b + d`），以及它特有的 `fence → mma → commit_group → wait_group` 四段式协议。
2. **cp_async 异步拷贝** —— `cp.async` 系列指令把数据从显存异步搬到共享内存，配合 `commit_group / wait_group` 做分组等待。
3. **mbarrier 同步原语** —— 显存屏障，用于在「异步搬运完成」与「消费数据」之间建立同步，是异步数据流的中枢。

学完本讲，你应当能够：

- 看懂 `examples/hopper_matmul/` 里的 wgmma 内核，理解异步 MMA 与等待的时序关系；
- 说清 wgmma 的四段式协议以及「多个 commit group 同时在飞」为何能隐藏延迟；
- 区分 `cp.async`（`copy_async`）与 TMA（`cp.async.bulk`）两种异步搬运，并理解 mbarrier 的 `arrive / arrive_and_expect_tx / wait` 与相位（phase）模型。

## 2. 前置知识

阅读本讲前，请确保已掌握以下概念（它们在前置讲义中已建立）：

- **Script 骨架与数据流**（u1-l3 / u1-l5）：`__init__` 设编译期超参、`__call__` 写算子逻辑、`global_view / shared_tensor / register_tensor / load_shared / store_global` 的分工。
- **指令与指令组的分层**（u2-l2）：通用指令（`RootInstructionGroup`）与硬件指令组（`wgmma / tma / mbarrier / fence` 等）经 `InstructionInterface` 组合挂到 `self.*` 上，每次 `self.*` 调用即向当前 builder 追加一条 `InstStmt`。
- **线程组上下文**（u2-l3）：`with self.thread_group(...)` / `single_thread()` / `warp_group()` 把一段代码的执行权收窄到一段连续线程。
- **Ampere matmul 进阶**（u7-l1）：分块、共享内存、`mma.m16n8k16`、`ldmatrix`、`cp.async`（`copy_async`）、软件流水线（`num_stages`）等概念。本讲是它在 Hopper 上的延续。
- **发射器与 target 注册**（u6-l2）：每条 IR 指令经 `REGISTRY` 按 `@register_emitter(inst_cls, target)` 派发到具体发射器，Hopper 专属发射器挂在 `nvgpu_sm90a`。

### 几个 Hopper 关键术语

- **warp group（线程束组）**：4 个连续 warp，共 128 个线程。Hopper 的 wgmma 必须由一个完整的 warp group 执行。
- **异步（async）**：指令「发起」后不阻塞线程，由专用硬件单元（Tensor Memory Accelerator / 拷贝引擎 / Tensor Core）在后台完成；线程随后用一条 `wait` 类指令去等结果。
- **commit group（提交组）**：把一批已发起但尚未等待的异步操作打包，后续按组等待，从而允许多组操作同时在飞（in flight），用来隐藏延迟。

## 3. 本讲源码地图

本讲涉及的关键文件按「用户编程面 → IR 定义 → 后端发射 → PTX 原语」四层组织：

| 层 | 文件 | 作用 |
| --- | --- | --- |
| 用户编程面 | `python/tilus/lang/instructions/wgmma.py` | `WgmmaInstructionGroup`：`fence / mma / commit_group / wait_group` |
| 用户编程面 | `python/tilus/lang/instructions/mbarrier.py` | `BarrierInstructionGroup`：`alloc / arrive / arrive_and_expect_tx / wait` |
| 用户编程面 | `python/tilus/lang/instructions/root.py` | `copy_async / copy_async_commit_group / copy_async_wait_group`（通用异步搬运） |
| 用户编程面 | `python/tilus/lang/instructions/fence.py` | `proxy_async` 等代理栅栏 |
| 组合挂载 | `python/tilus/lang/instructions/__init__.py` | `InstructionInterface` 把 `wgmma / mbarrier / fence` 挂为 `self.*` |
| IR 定义 | `python/tilus/ir/instructions/cuda/wgmma.py` | `WgmmaFenceInst / WgmmaMmaSSInst / WgmmaCommitGroupInst / WgmmaWaitGroupInst` |
| IR 定义 | `python/tilus/ir/instructions/cuda/cp_async.py` | `CopyAsyncInst / CopyAsyncCommitGroupInst / CopyAsyncWaitGroupInst` |
| 后端发射 | `python/tilus/backends/emitters/cuda/wgmma.py` | wgmma 各指令的发射器（构造 smem 描述符、发射 `wgmma_async`） |
| 后端发射 | `python/tilus/backends/emitters/cuda/cp_async.py` | `cp.async` 的向量化发射 |
| PTX 原语 | `python/tilus/hidet/ir/primitives/cuda/wgmma.py` | `WgmmaConfig` 与内联 PTX 模板 |
| 示例 | `examples/hopper_matmul/matmul_v0.py` | TMA + 通用 `dot`（不含显式 wgmma） |
| 示例 | `examples/hopper_matmul/matmul_v1.py` | TMA + 显式 wgmma（本讲主线） |
| 示例 | `examples/hopper_matmul/matmul_v3.py` | 生产者—消费者双 warp-group 软件流水线 |

## 4. 核心概念与源码讲解

### 4.1 wgmma 指令组：warp-group 异步 MMA

#### 4.1.1 概念说明

在 Ampere 上，矩阵乘累加用的是 `mma` 指令，每个 warp 独立算一个 `m16n8k16` 的小块。Hopper 把粒度提升到了 **warp group（128 线程）**：一条 `wgmma.mma_async` 指令就能让 4 个 warp 协同计算一个更大的矩阵块（`M` 固定为 64，`N` 可达 256，`K` 取决于数据类型）。

相比 `mma`，wgmma 有两个本质变化：

1. **操作数可以留在共享内存**。Ampere 的 `mma` 要求操作数先 `ldmatrix` 进寄存器；wgmma 允许 A、B 直接以「共享内存描述符（descriptor）」形式喂给 Tensor Core，省掉了 `ldmatrix` 这一步寄存器搬运（称为 SS 形态：shared×shared）。A 也可以来自寄存器（RS 形态）。
2. **执行是异步的**。wgmma 发起后立即返回，线程可以继续干别的活；必须用 `commit_group` + `wait_group` 显式等待结果落回累加器寄存器。

正因为异步，wgmma 有一套**严格的四段式协议**，缺少任何一段都会得到错误结果（甚至编译失败）。

#### 4.1.2 核心流程

wgmma 的协议在指令组的类文档里写得很清楚：

[python/tilus/lang/instructions/wgmma.py:L31-L43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L31-L43) —— 定义了 `fence → mma → commit_group → wait_group` 的顺序。

用伪代码描述一次完整使用：

```
self.wgmma.fence()            # 1. 建立内存序：让此前对操作数的写可见
self.wgmma.mma(sa, sb, acc)   # 2. 发起异步 MMA（可连续发起多条）
# ... 这里可以发起更多 mma，或做其它不依赖 acc 的工作 ...
self.wgmma.commit_group()     # 3. 把上述 mma 打包成一个提交组
self.wgmma.wait_group(0)      # 4. 等到最多剩 0 个提交组未完成（即全部完成）
```

关键点：

- **fence 的作用**：wgmma 通过异步代理（async proxy）读共享内存，而此前对共享内存的写（如 `store_shared`）走的是通用代理（generic proxy）。`wgmma.fence` 建立「通用写 → 异步读」的可见性序，否则 Tensor Core 可能读到旧数据。
- **wait_group(n) 的语义**：等待到「最多还剩 `n` 个提交组未完成」。`n=0` 表示全部等完；`n=1` 表示允许最近 1 组还在飞——这正是**软件流水线**隐藏延迟的手段：发起下一组 MMA 的同时，等待上一组完成。
- **线程组要求**：整个协议必须由一个完整的 warp group（128 线程，且起始线程号是 128 的倍数）执行。

#### 4.1.3 源码精读

**(a) 用户编程面：四个方法**

`WgmmaInstructionGroup` 把四个协议步骤一一委托给 builder，最终生成对应 IR 指令：

[python/tilus/lang/instructions/wgmma.py:L45-L58](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L45-L58) —— `fence()` 调 `self._builder.wgmma_fence()`，对应 PTX `wgmma.fence.sync.aligned`。

[python/tilus/lang/instructions/wgmma.py:L94-L130](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L94-L130) —— `mma(a, b, d)` 计算 `d = a @ b + d`。注意它根据 A 的存储位置分两种形态：A 是 `SharedTensor` 走 `wgmma_mma_ss`（SS），A 是 `RegisterTensor` 走 `wgmma_mma_rs`（RS）。

```python
# wgmma.py:121-130（节选）
if any(len(tensor.shape) != 2 for tensor in (a, b, d)):
    raise InstructionError(...)              # 必须是 2D
if isinstance(a, SharedTensor):
    self._builder.wgmma_mma_ss(a, b, d)      # SS 形态：A、B 都在共享内存
elif isinstance(a, RegisterTensor):
    self._builder.wgmma_mma_rs(a, b, d)      # RS 形态：A 在寄存器、B 在共享内存
```

[python/tilus/lang/instructions/wgmma.py:L60-L72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L60-L72) 与 [python/tilus/lang/instructions/wgmma.py:L74-L92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L74-L92) —— `commit_group()` 与 `wait_group(n)` 分别对应 `wgmma.commit_group.sync.aligned` 与 `wgmma.wait_group.sync.aligned N`。

**(b) IR 定义：指令形态与原子粒度**

wgmma 的 IR 指令都很「薄」——大多数没有输出（`output=None`），因为它们是副作用指令（同步、发起 MMA），结果直接写回既有的累加器寄存器：

[python/tilus/ir/instructions/cuda/wgmma.py:L27-L47](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/wgmma.py#L27-L47) —— `WgmmaFenceInst / WgmmaCommitGroupInst` 无输入无输出；`WgmmaWaitGroupInst` 只多一个 `n` 字段。

最关键的是 `WgmmaMmaSSInst.get_inst_mnk`，它决定一条 wgmma 指令的**原子粒度**（即一条硬件指令覆盖的 M×N×K）：

[python/tilus/ir/instructions/cuda/wgmma.py:L50-L77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/wgmma.py#L50-L77) —— `inst_m` 恒为 64，`inst_n = gcd(n, 256)`（N 粒度必须是实际 N 与上限 256 的公约数），`inst_k` 随数据类型变化：f16/bf16 取 16，tf32 取 8，fp8/i8 取 32，1-bit 取 256。

> 通俗理解：wgmma 永远以 `M=64` 为行粒度；列方向用 `gcd(n,256)` 找一个既能整除你的列数、又不超过硬件上限 256 的步长；K 方向的步长则由「一个数据类型占多少位」决定（位宽越小，一条指令塞得下越多 K）。

**(c) 后端发射：把块级 MMA 展开成一串原子指令**

用户写的是块级的 `self.wgmma.mma(sa, sb, acc)`（比如 `sa` 是 `[128, 64]`），但一条硬件 wgmma 指令只能算 `64×inst_n×inst_k`。发射器的职责就是把这个大块拆成一连串原子 wgmma，并为每个共享内存操作数构造**描述符**：

[python/tilus/backends/emitters/cuda/wgmma.py:L114-L145](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/wgmma.py#L114-L145) —— `WgmmaMmaSSEmitter` 先算出 `repeat_m / repeat_n / repeat_k`（每个方向要重复多少条原子指令），再用 `canonicalize_shared_layout` 把共享内存布局规范成 wgmma 能识别的 swizzle 形态。

[python/tilus/backends/emitters/cuda/wgmma.py:L162-L191](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/wgmma.py#L162-L191) —— 三重循环 `k → i → j`，每次为 A、B 各构造一个 `SharedMatrixDescriptor`，然后发射一条 `wgmma_async`。描述符里编码了共享内存基地址（右移 4 位）、leading/stride 偏移、base offset 与 swizzle 模式。

描述符的编码格式（64 位）定义在：

[python/tilus/backends/emitters/cuda/wgmma.py:L53-L78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/wgmma.py#L53-L78) —— `SharedMatrixDescriptor.encoded()` 把 `addr/lbo/sbo/base_offset/swizzle_mode` 打包成一个 64 位整数，这正是 PTX `wgmma.mma_async` 操作数所需的「矩阵描述符」格式。

发射器还有一个前置校验，确保当前线程组恰好是 128 线程且对齐：

[python/tilus/backends/emitters/cuda/wgmma.py:L81-L93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/wgmma.py#L81-L93) —— `check_warp_group()` 断言 `begin % 128 == 0` 且 `end - begin == 128`。

**(d) PTX 原语：内联汇编模板**

最底层把 wgmma 变成 PTX 字符串的是 hidet 原语：

[python/tilus/hidet/ir/primitives/cuda/wgmma.py:L84-L92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/cuda/wgmma.py#L84-L92) —— `WgmmaConfig.inst_name()` 生成形如 `wgmma.mma_async.sync.aligned.m64n256k16.f32.f16.f16` 的指令名。

[python/tilus/hidet/ir/primitives/cuda/wgmma.py:L415-L459](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/cuda/wgmma.py#L415-L459) —— `fence / commit_group / wait_group` 三条同步原语的注册，每条都是一段 `asm(...)` 内联模板（`wgmma.fence.sync.aligned;` 等）。

#### 4.1.4 代码实践

**实践目标**：在真实的 Hopper wgmma 内核里定位四段式协议，画出异步 MMA 与等待的时序。

**操作步骤**：

1. 打开 [examples/hopper_matmul/matmul_v1.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v1.py)。这个 `MatmulWGMMA` 内核用 `attrs.warps = 4`（即 128 线程，正好一个 warp group）。
2. 定位 K 维主循环里的 wgmma 调用：

   [examples/hopper_matmul/matmul_v1.py:L74-L77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v1.py#L74-L77) —— 这是完整的四段式（fence / mma / commit_group / wait_group(0)）。

3. 画时序图（文字版）：

   ```
   循环第 i 轮：
     [单线程] mbarrier.arrive_and_expect_tx + tma.global_to_shared(A,B) + mbarrier.wait
     [全块]   sync()                         # 等共享内存里 A、B 就绪
     [全块]   wgmma.fence()                  # 建立共享内存写 → wgmma 读的可见性
     [全块]   wgmma.mma(sa, sb, acc)         # 发起异步 MMA（立即返回）
     [全块]   wgmma.commit_group()           # 打包成提交组
     [全块]   wgmma.wait_group(0)            # 等这一组算完，acc 才可用
     [全块]   sync()
   ```

**需要观察的现象**：

- `wgmma.mma` 之前**必须**先有 `wgmma.fence`，否则可能读到未就绪的共享内存。
- `wgmma.mma` 与 `wgmma.commit_group` 之间**没有** `sync`，说明 MMA 是异步发起的。
- 这里用的是 `wait_group(0)`，即每轮都把上一组算完才进入下一轮——这还**不是**软件流水线。

**预期结果**：v1 是「同步版」wgmma（每轮等满），TFLOPS 会明显高于 Ampere 的 `mma` 版本，但还不及带多级流水的 v3。具体数值**待本地验证**（需要 sm_90a 设备）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 [matmul_v1.py:L77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v1.py#L77) 的 `wait_group(0)` 改成 `wait_group(1)`，会发生什么？

> **答案**：`wait_group(1)` 表示「允许最近 1 个提交组还在飞」。在 v1 这种每轮立即 `sync` 的结构里意义不大，但在流水线内核（v3/v5）里，它正是让「下一轮的 MMA 发起」与「上一轮 MMA 完成」重叠的关键，从而隐藏 MMA 延迟。

**练习 2**：为什么 `WgmmaMmaSSInst.get_inst_mnk` 里 `inst_m` 恒为 64，而 `inst_n = gcd(n, 256)`？

> **答案**：wgmma 硬件指令的 M 维粒度固定为 64（一个 warp group 覆盖 64 行）；N 方向上单条指令最大 256 列，同时必须能整除实际的 N，故取 `gcd(n, 256)` 作为列方向步长。

**练习 3**：`wgmma.fence` 和 `self.sync()` 都是「同步」，它们能互相替代吗？

> **答案**：不能。`self.sync()` 是线程块内**线程执行**同步（bar.sync，保证所有线程到达同一位置）；`wgmma.fence` 是**内存序**栅栏（建立 generic 代理写 → async 代理读的可见性），并不阻塞线程执行。两者解决的是不同层面的问题。

---

### 4.2 cp_async 异步拷贝：cp.async 三段式

#### 4.2.1 概念说明

普通 `load_global` 把数据从显存搬进**寄存器**，会占用寄存器、且搬运期间线程基本在等待。`cp.async`（Ampere sm_80 起引入）则把数据从显存**直接异步搬进共享内存**，绕过寄存器，且发起后立即返回，线程可继续工作。

`cp.async` 同样是三段式（和 wgmma 的 commit/wait 高度同构）：

```
self.copy_async(ga, sa, offsets=[...])     # 1. 发起异步搬运（可连续发起多条）
self.copy_async_commit_group()             # 2. 打包成一个提交组
self.copy_async_wait_group(0)              # 3. 等到最多剩 0 组未完成
```

也有一个等价的便捷写法 `copy_async_wait_all()` = `commit_group()` + `wait_group(0)`。

> **与 TMA 的关系**：`cp.async` 是「逐元素向量化」的异步搬运（每条指令最多 16 字节）。Hopper 还提供了 **TMA**（`cp.async.bulk`），由 Tensor Memory Accelerator 硬件单元搬运**整块**多维 tile，效率更高。`examples/hopper_matmul/` 全部使用 TMA（`self.tma.global_to_shared`）而非 `copy_async`。本节讲 `copy_async` 是因为它最直接地体现了 cp.async 三段式，也是 Ampere 系列内核（u7-l1）的主力搬运手段；理解了它，TMA 的「异步搬运 + mbarrier 等待」模式就是一脉相承的。

#### 4.2.2 核心流程

[python/tilus/lang/instructions/root.py:L744-L798](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L744-L798) —— `copy_async(src, dst, offsets, dims, evict, check_bounds)` 的签名。它把 `src`（`GlobalTensor`）的一个切片异步拷进 `dst`（`SharedTensor`）；越界访问默认**补零**（`check_bounds=True`），这对边界 tile 很重要。

流程要点：

1. 越界补零：`check_bounds=True` 时，落在显存范围之外的元素写 0，避免读到垃圾数据。
2. 分组等待：发起多条 `copy_async` 后，用 `commit_group` + `wait_group(n)` 统一等待，`n` 控制允许同时在飞的组数（用于软件流水线）。
3. 驱逐策略：`evict` 可选 `'evict_normal'`（默认）或 `'evict_first'`（流式访问优先淘汰），提示 L2 缓存行为。

#### 4.2.3 源码精读

**(a) IR 定义**

[python/tilus/ir/instructions/cuda/cp_async.py:L25-L53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/cp_async.py#L25-L53) —— `CopyAsyncInst`，`inputs=(dst, src)`，`offsets/dims/evict/check_bounds` 作为属性。注意它没有输出（`output=None`），是副作用指令——结果直接落在共享内存，不会被死代码消除（参见 u3-l4 / u5-l3）。

[python/tilus/ir/instructions/cuda/cp_async.py:L80-L100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/cuda/cp_async.py#L80-L100) —— `CopyAsyncCommitGroupInst / CopyAsyncWaitGroupInst(n) / CopyAsyncWaitAllInst` 三个同步指令，结构与 wgmma 的 commit/wait 完全对应。

在编译流水线里，高层 `CopyAsyncInst` 会被 `lower_load_store` 降级成更底层的 `CopyAsyncGenericInst`（带裸指针 `ptr` + 偏移闭包 `f_offset` + 掩码 `f_mask`），这与 `load_global`/`store_global` 的降级路径一致（详见 u5-l4）。

**(b) 后端发射：向量化搬运**

发射器的核心是**挑一个最大的搬运粒度**（`cp_size` ∈ {16, 8, 4} 字节），让每条 `cp.async` 指令尽量多搬数据：

[python/tilus/backends/emitters/cuda/cp_async.py:L57-L96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/cp_async.py#L57-L96) —— 按 `16 → 8 → 4` 字节从大到小尝试，用 `analyze_grid` 检查地址整除性、连续性、掩码恒定性等约束，挑出第一个可行粒度与连续维度。

[python/tilus/backends/emitters/cuda/cp_async.py:L154-L174](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/cp_async.py#L154-L174) —— 真正发射 `cp.async` 的地方：算出全局地址与共享地址，用 `src_size = if mask then cp_size else 0` 实现越界补零（mask 为假时拷 0 字节即写零），并设 `prefetch_bytes=256`、按 `cp_size` 选 `cache_level`。

[python/tilus/backends/emitters/cuda/cp_async.py:L199-L214](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/cp_async.py#L199-L214) —— `commit_group / wait_group / wait_all` 三个发射器分别一行调用对应的 hidet PTX 原语（`cp.async.commit_group` 等），全部挂在 `nvgpu_sm80` target。

**(c) 任务到线程的分配**

[python/tilus/backends/emitters/cuda/cp_async.py:L176-L191](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/cp_async.py#L176-L191) —— 把 `num_tasks` 条搬运任务在 `num_threads` 个线程间分配（任务少于线程时用 `if` 守卫，否则按循环均分），这是 elementwise/ldst 发射器共享的套路（详见 u6-l4）。

#### 4.2.4 代码实践

**实践目标**：理解 `copy_async` 三段式与越界补零，并对比它与 TMA 的等价性。

**操作步骤**（源码阅读型，无需 GPU）：

1. 阅读 [python/tilus/lang/instructions/root.py:L800-L815](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L800-L815)，确认 `copy_async_wait_all()` 的文档明确写了它等价于 `commit_group()` + `wait_group(0)`。
2. 对照 [examples/hopper_matmul/matmul_v0.py:L59-L69](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v0.py#L59-L69) 的 TMA 写法，列出两者的对应关系：

   | 语义 | copy_async 写法 | TMA 写法（hopper_matmul） |
   | --- | --- | --- |
   | 发起搬运 | `copy_async(ga, sa, offsets=...)` | `tma.global_to_shared(src=ga, dst=sa, offsets=..., mbarrier=...)` |
   | 打包等待 | `copy_async_commit_group()` + `copy_async_wait_group(0)` | `mbarrier.arrive_and_expect_tx(...)` + `mbarrier.wait(...)` |

3. （可选，需 sm_80+ GPU）写一个最小内核：用 `copy_async` 把一个 `[64, 64]` 的 fp16 tile 从显存搬进共享内存，`copy_async_wait_all()` 后 `load_shared` 读出并 `store_global` 写回，与输入对比验证正确性。结果**待本地验证**。

**预期结果**：`copy_async` 与 TMA 在「异步搬运 + 等待」的骨架上同构，区别在于 `copy_async` 用 commit/wait 组、TMA 用 mbarrier；粒度上 `copy_async` 是向量化逐元素，TMA 是整块。

#### 4.2.5 小练习与答案

**练习 1**：`copy_async` 的 `output` 为什么是 `None`？这对死代码消除（DCE）有什么影响？

> **答案**：因为它把结果直接写进共享内存（副作用），不产生新的张量输出，故 `output=None`。在 Tilus 的 DCE 里，副作用指令（不在功能指令白名单中）**永不删除**，所以即使共享内存看起来没人读，`copy_async` 也不会被误删（参见 u3-l4 / u5-l3）。

**练习 2**：发射器为什么要按 `16 → 8 → 4` 字节的顺序选 `cp_size`？

> **答案**：`cp.async` 单条指令最大支持 16 字节；粒度越大，搬运同样数据所需的指令数越少、效率越高。因此优先尝试 16 字节，只有当地址整除性或连续性不满足时才回退到更小粒度。

---

### 4.3 mbarrier：异步同步原语

#### 4.3.1 概念说明

异步搬运（TMA）和异步计算（wgmma）都「发起即返回」，那线程怎么知道数据/结果准备好了？Hopper 的答案是 **mbarrier（显存屏障）**。

mbarrier 是共享内存里的一个 64 位对象，核心是**相位（phase）模型**：

- 每个 mbarrier 维护一个相位位（0 或 1）、一个待到达计数（pending arrivals）和一个待完成事务字节数（tx-count）。
- 线程用 `arrive`（到达）让计数减一；用 `arrive_and_expect_tx` 既到达又声明「预计有这么多字节的异步事务要来」。
- 当**待到达计数和 tx-count 同时归零**，硬件翻转相位位，表示这一相位完成。
- 线程用 `wait(barrier, phase)` 阻塞，直到屏障的当前相位**不同于** `phase`（即等待的那一相位已完成），随后读数据就是安全的。

`tx-count` 是 mbarrier 区别于普通 barrier 的杀手锏：它能**追踪异步搬运的字节数**。TMA 完成搬运时会自动扣减 tx-count，于是「数据真的搬完」与「屏障放行」天然绑定——不需要额外的 `sync`。

#### 4.3.2 核心流程

一次典型的「TMA 搬运 + mbarrier 同步」（来自 hopper_matmul）：

```
barrier = self.mbarrier.alloc(counts=[1])          # 分配并初始化一个 mbarrier，到达计数=1
phase = 0                                           # 记录当前等待的相位

# 单线程发起 TMA（全块只需一个人发指令）
with self.single_thread():
    self.mbarrier.arrive_and_expect_tx(             # 到达 + 声明字节数
        barrier, transaction_bytes=sa.nbytes + sb.nbytes
    )
    self.tma.global_to_shared(src=ga, dst=sa, ..., mbarrier=barrier)  # TMA 完成时扣 tx-count
# ... 全块所有线程 ...
self.mbarrier.wait(barrier, phase=phase)            # 等到这一相位完成（搬运到位）
phase ^= 1                                          # 翻转：下一轮等相反相位
```

**关键点**：

- **到达计数 `[1]`**：只有发起 TMA 的那「1 个到达」需要计数（单线程 `arrive_and_expect_tx` 算 1 次到达）。
- **`phase ^= 1`**：用异或翻转等待相位。因为硬件每完成一相位就翻转相位位，所以「等当前相位结束」=「等相位变得与记录值不同」。
- **release/acquire 语义**：`arrive` 默认 `release`（保证此前的写在 wait 后可见），`wait` 默认 `acquire`（保证读到的是 release 之前的写）。这套内存序让「生产者写共享内存 → 消费者读」安全。

#### 4.3.3 源码精读

**(a) 用户编程面：四个核心方法**

[python/tilus/lang/instructions/mbarrier.py:L39-L73](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L39-L73) —— `alloc(counts)` 在共享内存分配并初始化 mbarrier，返回一个 `uint32` 寄存器张量持有其共享内存地址；初始 `phase=0`、`pending_arrivals=counts[i]`。单个整数分配一个屏障，序列分配多个。

[python/tilus/lang/instructions/mbarrier.py:L121-L170](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L121-L170) —— `arrive_and_expect_tx(barrier, transaction_bytes, ...)`：到达（计数减一）的同时把 `transaction_bytes` 加到 tx-count。文档明确说明它「typically used with `single_thread`」，让单线程设期望、TMA 引擎做实际搬运。

[python/tilus/lang/instructions/mbarrier.py:L172-L220](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L172-L220) —— `wait(barrier, phase, ...)`：阻塞直到当前相位不同于 `phase`，对应 PTX `mbarrier.try_wait.parity.shared::cta.b64`。

[python/tilus/lang/instructions/mbarrier.py:L75-L119](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L75-L119) —— `arrive(barrier, count, ...)`：纯到达（不带 tx 声明），常用于生产者—消费者里消费者通知「这块我消费完了，你可以复用缓冲」。

**(b) 生产者—消费者相位的约定**

mbarrier 类里有两个很有用的常量，专门服务于生产者—消费者流水线：

[python/tilus/lang/instructions/mbarrier.py:L29-L37](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L29-L37) —— `producer_initial_phase = 1`、`consumer_initial_phase = 0`。生产者一开始等「槽位被释放」（相位从 1 翻走），消费者一开始等「槽位被填满」（相位从 0 翻走）。这套约定在 [matmul_v5.py:L33-L34](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v5.py#L33-L34) 被直接使用。

**(c) 在示例里的真实用法**

[examples/hopper_matmul/matmul_v1.py:L54-L69](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v1.py#L54-L69) —— 经典的单屏障 TMA 流程：`alloc(counts=[1])` → 单线程 `arrive_and_expect_tx` + 两次 `tma.global_to_shared` → 全块 `mbarrier.wait(phase)` → 循环末尾 `phase ^= 1`。

更复杂的双 warp-group 流水线在 v3 里：

[examples/hopper_matmul/matmul_v3.py:L57-L62](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L57-L62) —— 分配两组屏障：`consumer_barriers`（count=1，等 TMA 填满）与 `producer_barriers`（count=128，等消费者 warp group 消费完释放槽位）。

[examples/hopper_matmul/matmul_v3.py:L98-L111](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L98-L111) —— 消费者 warp group（128 线程）的循环：`mbarrier.wait(consumer_barriers[stage])` 等数据 → wgmma 四段式 → `mbarrier.arrive(producer_barriers[stage])` 通知生产者「这块我用完了」。

#### 4.3.4 代码实践

**实践目标**：理解 mbarrier 的相位翻转与「到达计数 / tx-count」双归零机制。

**操作步骤**：

1. 在 [matmul_v1.py:L54-L78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v1.py#L54-L78) 里，逐行标注每一处 mbarrier 操作的「计数变化」：
   - `alloc(counts=[1])`：pending=1, tx=0, phase=0。
   - `arrive_and_expect_tx(..., bytes=sa.nbytes+sb.nbytes)`：pending 0，tx += bytes。
   - 两次 `tma.global_to_shared` 完成：tx 自动扣到 0；pending 与 tx 同时为 0 → 硬件翻转 phase。
   - `wait(barrier, phase=0)`：等 phase 变成 1（即 ≠ 0）后放行。
   - `phase ^= 1`：下一轮等 phase 从 1 翻回 0。

2. 回答：如果把 `alloc(counts=[1])` 改成 `alloc(counts=[2])`，这个内核会怎样？

**预期结果**：counts=2 意味着需要 2 次「到达」才放行，但每轮只发了 1 次 `arrive_and_expect_tx`，pending 永远减不到 0，`wait` 会**永久阻塞**（死锁）。这验证了「到达计数必须与实际到达次数匹配」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `arrive_and_expect_tx` 通常放在 `with self.single_thread():` 里，而 `mbarrier.wait` 放在外面？

> **答案**：TMA 指令只需单线程发起（全块发一遍即可），所以「到达 + 声明 tx」由单线程完成（到达计数为 1）。但 `wait` 之后读共享内存的是**全块所有线程**，所以 `wait` 必须由整个线程组执行，确保每个线程都看到数据就绪。

**练习 2**：在 v3 的双屏障设计里，`producer_barriers` 的 count 为什么是 128，而 `consumer_barriers` 是 1？

> **答案**：`consumer_barriers` 等的是 TMA（单线程发起，count=1）；`producer_barriers` 等的是消费者 warp group（128 线程）消费完释放槽位，所以 count=128——消费者每线程 `arrive` 一次，凑齐 128 次才放行生产者复用该 stage 的共享内存。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「读 hopper_matmul 流水线内核」的综合任务。

**任务**：阅读 [examples/hopper_matmul/matmul_v3.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py)（双 warp-group 软件流水线），完成下面的「时序—同步」对照表，并解释每一处 wgmma 与 mbarrier 配合的作用。

**步骤**：

1. 找到生产者线程组（`thread_begin=128, num_threads=32`，[matmul_v3.py:L64-L96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L64-L96)）与消费者线程组（`thread_begin=0, num_threads=128`，[matmul_v3.py:L98-L115](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L98-L115)）。
2. 对每一轮 `stage`，列出生产者做了什么、消费者做了什么、它们通过哪一对屏障握手。
3. 重点解释 [matmul_v3.py:L106-L110](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L106-L110)：为什么 wgmma 之后要 `mbarrier.arrive(producer_barriers[stage])`？

**参考答案要点**：

| 阶段 | 生产者（1 个 warp） | 消费者（1 个 warp group） | 握手屏障 |
| --- | --- | --- | --- |
| 取数据 | `wait(producer_barriers)` 等槽位空闲 → TMA 搬运 → `arrive_and_expect_tx(consumer_barriers)` 声明字节数 | （并行）算上一 stage 的 wgmma | consumer_barriers（满）/ producer_barriers（空） |
| 算结果 | （并行）搬下一个 stage | `wait(consumer_barriers)` 等数据 → wgmma 四段式 → `arrive(producer_barriers)` 释放槽位 | 同上 |

- wgmma 之后的 `mbarrier.arrive(producer_barriers[stage])`：消费者算完一个 stage 的共享内存数据后，通知生产者「这块缓冲可以覆盖了」，从而允许生产者往同一个 `sa[stage]/sb[stage]` 写下一轮数据——这就是**环形多级缓冲（num_stages）**得以循环复用的同步基础。
- 注意 v3 用了 `wgmma.wait_group(0)`（[matmul_v3.py:L109](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L109)）；真正的「MMA 与搬运重叠」主要来自生产者/消费者**两个 warp group 物理并行**，而非 MMA commit group 在飞。`wait_group(n>0)` 的在飞重叠在 v5 里出现（[matmul_v5.py:L198](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v5.py#L198)）。

**预期结果**：你能用一句话说清「mbarrier 负责让生产者的搬运与消费者的 wgmma 在多级缓冲上正确交替，而 wgmma 自己的 commit/wait 负责 MMA 结果的可见性」——两者的同步层次不同，缺一不可。

## 6. 本讲小结

- **wgmma 是 Hopper 的招牌**：warp-group（128 线程）粒度的异步 MMA，操作数可留共享内存（SS/RS 形态），必须遵循 `fence → mma → commit_group → wait_group` 四段式协议；`wait_group(n)` 的 `n` 控制允许同时在飞的提交组数，是隐藏 MMA 延迟的关键。
- **cp.async 是异步搬运的基础**：`copy_async` 把显存数据异步直搬共享内存（绕过寄存器），配 `commit_group/wait_group` 分组等待；Hopper 上更高效的整块搬运是 TMA（`cp.async.bulk`），hopper_matmul 示例全部使用 TMA，但同步骨架与 cp.async 同构。
- **mbarrier 是异步同步的中枢**：用「待到达计数 + tx-count」双归零与相位翻转模型，天然绑定「异步搬运/计算完成」与「屏障放行」；`arrive_and_expect_tx` 追踪 TMA 字节数，`arrive` 用于纯通知，配合 `producer/consumer_initial_phase` 约定支撑生产者—消费者流水线。
- **三层同步要分清**：`wgmma.fence`（内存序，generic→async 可见性）、`mbarrier.wait`（异步事务完成）、`self.sync`（线程执行同步）解决不同问题，不能互相替代。
- **发射细节**：wgmma 发射器把块级 MMA 拆成 `64×inst_n×inst_k` 的原子指令并构造 64 位共享内存描述符；cp.async 发射器按 `16→8→4` 字节挑最大向量化粒度、用 `src_size=0` 实现越界补零。
- **进阶方向**：把本讲的「单屏障 + wait_group(0)」（v1）升级为「双 warp-group + 多级环形缓冲」（v3），再到「wait_group(n>0) MMA 在飞」（v5），即得到 Hopper 上接近峰值的软件流水线。

## 7. 下一步学习建议

- **u7-l3 Blackwell：TMA、tcgen05 与 TMEM**：本讲的 TMA 仅用于 Hopper 的 `global_to_shared`；Blackwell 进一步用 TMA 做 epilogue（`shared_to_global`）并引入 TMEM 与 `tcgen05` 第五代张量核，是 Hopper 异步数据流的直接演进。
- **u7-l4 异步软件流水线**：系统学习 `Pipeline` 抽象与多级缓冲调度，把本讲 v3/v5 里手工编排的 mbarrier 握手抽象成可复用的流水线原语。
- **精读 `examples/hopper_matmul/matmul_v5.py`**：它用 `wgmma.wait_group(1)` 让 MMA 在飞、并用 mbarrier 替代共享内存标志位做生产者—消费者同步，是 Hopper wgmma 的「完整版」范例。
- **PTX 手册对照**：阅读 [wgmma.py:L26-L29](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/primitives/cuda/wgmma.py#L26-L29) 注释里给出的 PTX 链接，把 Tilus 指令与底层 `wgmma.mma_async` / `mbarrier` / `cp.async` 的硬件语义一一对应，加深理解。
