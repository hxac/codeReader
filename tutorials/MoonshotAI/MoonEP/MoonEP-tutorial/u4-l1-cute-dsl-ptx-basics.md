# u4-l1 CuTe DSL 与 PTX 基础设施

## 1. 本讲目标

从本讲开始，我们离开规划器的算法世界，进入通信内核的实现层。前面三单元里反复出现的一句话是「内核用 CuTe DSL 编写、首次调用时 JIT 编译」——本讲就把这句话拆开讲清楚。读完本讲，你应该能够：

1. 说出 CuTe DSL 的**编译-启动模型**：`@cute.jit`、`@cute.kernel`、`launch()`、`cute.compile` 四者如何配合，以及 `cutlass.const_expr` 带来的**按形特化**（按具体形状生成专用代码）。
2. 读懂 `_common.py` 里用 `llvm.inline_asm` 封装的**共享 PTX 助手**家族：原子操作、带内存序的加载、位运算、`cp.async.bulk` 双向块拷贝，理解约束字符串（`"=r,l,r"`）的每一项含义。
3. 解释 MoonEP **为什么必须绕开** `nvidia-cutlass-dsl 4.4.2` 的两个 TMA lowering 限制，以及直发 PTX 后必须遵守的「单线程指令」约束。
4. 理解**编译缓存** `_get_compiled` 的设计：为什么每个形状维度都要进 `lru_cache` 的键、流水线深度 `stages` 如何按设备 smem 预算自适应选择。

本讲是 u4 单元（通信内核）的地基：u4-l2 之后的每一篇内核精读，都会反复调用本讲建立的词汇表。

## 2. 前置知识

### 2.1 什么是 CuTe DSL

传统 CUDA 内核用 C++ 写、用 nvcc 离线编译。而 CUTLASS 项目提供的 **CuTe DSL**（Python 域特定语言，依赖 `nvidia-cutlass-dsl` 包）允许直接用 Python 写 GPU 内核：Python 代码在**追踪（trace）**阶段被翻译成 MLIR/LLVM IR，再 JIT 编译成 `.cubin`。MoonEP 的整个 `moonep/` 内核层都是用这个 DSL 写的（见 u1-l2 的两层代码结构）。

好处是内核逻辑可以和 Python 侧的张量准备、断言检查、缓存管理写在同一份代码里；代价是 DSL 的能力边界由 CUTLASS Python 端决定——**一旦某个 PTX 指令没有被 DSL 覆盖（或覆盖得不对），就要自己动手内联汇编**，这正是 `_common.py` 存在的原因。

### 2.2 PTX 与内存序速览

PTX 是 NVIDIA GPU 的虚拟指令集。本讲会遇到的几类指令：

- **`atom.*` / `red.*`**：原子读-改-写（`red` 是不返回旧值的「归约」版）。可以带内存序后缀，如 `.relaxed`（只保证原子性）、`.release`/`.acquire`（释放/获取序），以及作用域后缀 `.gpu`（整个 GPU 上可见）与 `.sys`（跨 GPU、经 NVLink 系统级可见）。
- **`ld.*` / `st.*`**：加载/存储，同样可带 `.acquire`、`.sys` 等后缀。
- **`cp.async.bulk`**：TMA（Tensor Memory Accelerator）块拷贝，硬件异步搬运一段连续字节，单向一条指令可搬几十 KB——这是 dispatch/combine 流水线的运力核心。
- **`popc` / `clz` / `brev`**：位计数（population count）、数前导零、位反转。
- **`griddepcontrol.*`**：PDL（Programmatic Dependent Launch，程序化依赖启动）指令对。

作用域后缀在 MoonEP 里有一条清晰的分界线：**`.gpu`** 用于单卡内的 grid/跨 warp 同步；**`.sys`** 用于跨 rank（经 NVLink 对称内存）的发布与获取——这个区分在 u2-l2（对称内存）和 u3-l6（跨 rank 屏障）已经建立，本讲看它们的代码形态。

### 2.3 与前置讲义的衔接

- u2-l2/u2-l4：dispatch 内核直写的 `hidden_buf`/`meta_buf` 是 VMM 对称内存与组播视图，本讲只把它们当作「远端可写的指针」。
- u3-l6：已经讲过 `grid_sync`/`cross_rank_barrier` 的**语义**（自复位、哨兵位翻转、fence.proxy 代理桥）。本讲不重复语义，只看它们**由哪些 inline-asm 助手拼装**、以及 PDL 原语如何让相邻内核流水起来。
- u3-l5：dedup 三件套（`dup_groups`/`dup_loffs`/`dup_counts`）的契约，本讲的 builder warp 代码是它的消费现场之一。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `moonep/_common.py` | 共享 PTX 助手库 | 全部 inline-asm 封装、`grid_sync`/`cross_warp_sync`/PDL 原语、`cp.async.bulk` 包装 |
| `moonep/dispatch.py` | dispatch 内核 + 宿主启动器 | `@cute.jit`/`@cute.kernel`/`launch` 三层结构、`_get_compiled` 编译缓存、助手的使用现场 |
| `moonep/dispatch_epilogue.py` | dispatch 的去重展开内核 | `use_pdl` 启动选项与 `pdl_wait_predecessor` 的等待侧 |
| `moonep/constants.py` | 位宽常量 | `KIDX_BITS`、`DEDUP_BUILDER_WARPS`（builder warp 的编码参数） |
| `moonep/api.py` | Buffer 门面 | `enable_pdl` 如何接线到 `pdl_trigger`/`pdl_launch` |
| `moonep/combine.py`、`moonep/combine_prologue.py`、`moonep/grad_reduce.py`、`moonep/inter_rank_sync.py`、`moonep/planning.py` | 其他内核 | 作为 `_common` 的消费者清单（导入语句） |

`_common.py` 的消费者全景（用 `from moonep._common import` 全库检索可得）：

- `dispatch.py:27-38`：导入 10 个助手（拷贝、屏障、原子、位运算、PDL 触发）；
- `dispatch_epilogue.py:32-36`、`combine.py:28-34`：`cp_async_bulk_g2s/s2g` + `pdl_wait_predecessor`；
- `combine_prologue.py:33`：`cp_async_bulk_g2s` + `pdl_trigger_dependents`；
- `grad_reduce.py:42`、`inter_rank_sync.py:19`：`cross_rank_barrier`；
- `planning.py:27`：`cp_async_bulk_g2s` + `cross_rank_barrier` + `grid_sync`。

也就是说：`_common.py` 是全部七个内核共享的唯一 PTX 层，改它等于改所有内核的底层。

## 4. 核心概念与源码讲解

### 4.1 CuTe DSL 的编译-启动模型：`@cute.jit` / `@cute.kernel` / `launch`

#### 4.1.1 概念说明

一个 CuTe DSL 内核由三层组成，`DispatchKernel` 是标准范本：

1. **设备内核** `@cute.kernel`：写出每个线程真正执行的代码（`if warp_idx == ...` 的分支、循环、PTX 助手调用）。它只是「函数体定义」，不能直接启动。
2. **宿主入口** `@cute.jit`（这里是 `__call__`）：负责把裸指针包装成 `cute.Tensor` 视图、绑定 constexpr、计算 smem，最后调用 `self.kernel(...).launch(...)`。它本身也是被追踪的代码。
3. **编译与调用**：宿主用 `cute.compile(kernel, <示例参数>)` 把入口编译成可执行对象；之后每次启动只是用真实指针**调用**这个已编译对象，不再重新编译。

**constexpr 特化**是理解性能的钥匙：`H`、`S`、`K`、`stages` 等维度通过 `cutlass.const_expr(...)` 声明为**编译期常量**，编译器可以把循环完全展开、把地址计算折叠成立即数。代价是每个形状组合都是一份独立编译产物——这就引出 4.5 节的编译缓存。

#### 4.1.2 核心流程

`launch_dispatch(ctx, ...)` 的一次调用（缓存命中时）：

```
launch_dispatch(ctx, hidden_sh, route_weights, plan)
  ├─ 一长串宿主断言（形状/dtype/contiguity/设备）
  ├─ _get_compiled(H, R, S, K, ..., with_weights, build_dedup_map, device_index, pdl_trigger)
  │     └─ lru_cache 命中 → 直接返回已编译对象（首次未命中才 cute.compile）
  ├─ make_ptr(...) 把每个 torch.Tensor 的 data_ptr 包装成 DSL 指针
  └─ dispatch_compiled(指针..., Int32(rank), ..., stream)   # 在当前 CUDA 流上启动
        └─ @cute.jit __call__ → self.kernel(...).launch(grid=(num_sms,1,1),
                                                          block=(num_threads,1,1),
                                                          smem=..., cooperative=True)
```

注意最后一跳：`launch_dispatch` 每步都传真实的 `data_ptr()` 和当前流；而 `launch` 的 grid/block/smem 几何全部来自 `DispatchKernel` 构造时算好的值——**运行期只换指针，不换几何**。

#### 4.1.3 源码精读

**内核类的骨架与 warp 角色**（角色语义详见 u4-l2，这里只看结构）：

[moonep/dispatch.py:50-81](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L50-L81) —— `DispatchKernel` 的类文档：warp 0 是 G2S 生产者、warp 1 是 S2G 消费者、warp 2 是零填充、warp 3.. 是去重构建 warp；`num_threads = 96 + 32 * DEDUP_BUILDER_WARPS`（3 个固定 warp × 32 线程 + 4 个 builder warp，常量见 [moonep/constants.py:17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L17)）。

**宿主入口里把 Python int 提升为编译期常量**：

[moonep/dispatch.py:169-176](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L169-L176) —— `H = cutlass.const_expr(self.H)` 等八行，把构造参数烧进 IR；内核体内（[dispatch.py:289-308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L289-L308)）再次以 `const_expr` 取出，连同 `H_BYTES = const_expr(H * 2)` 这类派生常量一起参与折叠。

**int64 地址计算**：

[moonep/dispatch.py:178-190](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L178-L190) —— 注释明确指出 `R*NvS_padded*H` 在生产配置下会超过 \(2^{31}\)，行偏移必须按 int64 求值；因此下游所有 `make_layout` 的 stride 都是 `Int64`。这也解释了 4.2 节里内联汇编指针约束为什么是 `l`（64 位寄存器）。

**launch 几何**：

[moonep/dispatch.py:258-264](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L258-L264) —— `.launch(grid=(self.num_sms,1,1), block=(self.num_threads,1,1), smem=smem_bytes, stream=stream, cooperative=True)`。两个关键点：`cooperative=True` 请求协作启动（保证全部 CTA 同时驻留 SM，grid 级屏障 `grid_sync` 才合法）；`smem=smem_bytes` 超过 48 KB 时由 DSL 走 opt-in 路径申请。

**设备内核头部与线程坐标**：

[moonep/dispatch.py:310-312](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L310-L312) —— `cute.arch.block_idx()` / `warp_idx()` / `thread_idx()` 是 DSL 内建的硬件坐标查询；`make_warp_uniform` 把 warp id 广播成 warp 一致值，供 `if warp_idx == self.PRODUCER_WARP` 这类整 warp 分支使用。

对照「依赖内核」的启动选项（PDL 等待侧）：

[moonep/dispatch_epilogue.py:152-164](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L152-L164) —— epilogue 的 `launch` 多了一个 `use_pdl=self.pdl_launch`；这是 PDL 属性要打在**被依赖的（后启动的）内核**上，4.4 节展开。

#### 4.1.4 代码实践

**实践：数一数「一次 dispatch 启动」要穿过多少层**

1. **实践目标**：把 4.1.2 的调用链落实到具体行号，确认「几何在构造期定死、运行期只传指针」。
2. **操作步骤**：
   - 打开 [moonep/dispatch.py:839-984](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L839-L984)（`launch_dispatch`），从下往上读：L965-984 是对 `dispatch_compiled` 的最终调用，数一数传了几个参数（提示：14 个指针 + 3 个 `Int32` 标量 + 1 个 stream）。
   - 再对照 L926-963：每个 `make_ptr` 的 `assumed_align` 取值（大多数是 16，`zfr_ptr`/`dup_counts_ptr` 是 8）。
   - 最后看 L169-176：确认这些运行期传入的 `rank`/`weights_off`/`barrier_off` 是 `Int32`（**运行期值**），而 `H`/`S`/`K` 是 `const_expr`（**编译期值**）——同一份代码里两种参数的分流就是「按形特化」的现场。
3. **需要观察的现象**：哪些量进了编译期、哪些留在运行期；为什么 `rank` 不能是 constexpr（每个 rank 复用同一份编译产物）而 `H` 必须是。
4. **预期结果**：能列出「编译期：H/S/K/NvS/NvS_padded/meta_stride/R/stages/num_sms/num_threads/with_weights/build_dedup_map；运行期：所有指针、rank、weights_off、barrier_off、stream」这样一张两列清单。
5. 本实践为源码阅读型，不需要 GPU，结论可离线核实。

#### 4.1.5 小练习与答案

**练习 1**：`DispatchKernel.__init__` 里 `self.num_threads` 会在 `build_dedup_map=False` 时从 224 降回 96（[dispatch.py:113-115](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L113-L115)）。为什么 `num_threads` 必须进编译缓存键（它确实在 `_get_compiled` 的签名里，见 4.5.3）？

**答案**：`num_threads` 在内核体内是 `const_expr`（[dispatch.py:299](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L299)），还参与 `H_PER_THR_ZERO` 的编译期除法（L343）和 `cross_rank_barrier` 的 `num_threads` 断言。blockDim 是 launch 几何的一部分，两种取值对应两份不同的编译产物；若不进缓存键，lru_cache 可能在 fresh 路径和 reuse 路径间返回错误几何的内核。

**练习 2**：`launch_dispatch` 每次都重新 `make_ptr`，为什么不算浪费？

**答案**：`make_ptr` 只是构造一个轻量的运行期指针描述符（dtype + `data_ptr()` + 地址空间 + 对齐假设），不触发编译；真正昂贵的是 `cute.compile`，它被 `_get_compiled` 的 `lru_cache` 挡住了。指针每次必须重传，因为每次调用张量地址可能不同。

**练习 3**：为什么 dispatch 的 `launch` 必须 `cooperative=True`，而普通计算内核（如纯 GEMM）不需要？

**答案**：dispatch 内核退出前调用 `cross_rank_barrier` → 内部两次 `grid_sync`（[dispatch.py:683-690](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L683-L690)）。grid 级屏障要求所有 CTA 同时驻留在 SM 上互相等待；协作启动正是向驱动申请这个保证。若非协作启动，部分 CTA 可能排队未上 SM，先上的 CTA 在屏障处自旋等不到人，直接死锁。

---

### 4.2 共享 PTX 助手：`llvm.inline_asm` 封装模式

#### 4.2.1 概念说明

`_common.py` 的主体是 14 个 `@dsl_user_op` 函数，每个都是对 `llvm.inline_asm` 的一次薄封装。两个装饰器/机制需要先理解：

- **`llvm.inline_asm`**（来自 MLIR 的 LLVM 方言，[moonep/_common.py:21](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L21)）：把一段汇编字符串原样塞进生成的 IR。参数依次是：结果类型、操作数列表、汇编模板、**约束字符串**、`has_side_effects` 等。
- **`@dsl_user_op`**（[moonep/_common.py:22](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L22)）：把普通 Python 函数注册为 DSL 的「用户算子」，使它可以在 `@cute.jit` 追踪的代码里像内建函数一样被调用（同时正确透传 `loc/ip` 等代码位置信息）。

约束字符串沿用 GCC 内联汇编的字母：`r` = 32 位寄存器、`l` = 64 位寄存器、`=r`/`=l` = 输出操作数；模板里 `$0`、`$1`、`$2` 按位置引用操作数。例如 `atom_add_relaxed_gpu_s32` 的 `"=r,l,r"` 表示「一个 32 位输出（返回的旧值）、一个 64 位输入（指针）、一个 32 位输入（加数）」，对应模板 `"atom.add.relaxed.gpu.global.s32 $0, [$1], $2;"`。

`has_side_effects=True` 告诉编译器这个调用**不能被消除、不能被重排、不能被视作纯函数复用结果**——原子和加载都要置位；唯一例外是 `popc_b32`/`ctz_b32` 这两个真正的纯位运算（`has_side_effects=False`，允许 CSE）。

#### 4.2.2 核心流程

助手按功能分四组：

| 组 | 助手 | PTX 内存序/作用域 | 典型消费者 |
| --- | --- | --- | --- |
| 原子（GPU 域） | `atom_add_release_gpu`、`atom_add_relaxed_gpu_s32`、`atom_min_relaxed_gpu_s32`、`atom_or_relaxed_gpu_s32`、`atom_or_relaxed_gpu_b32` | `.release.gpu` / `.relaxed.gpu` | `grid_sync`、`cross_warp_sync`、builder warp |
| 原子/加载（系统域） | `red_add_release_sys`、`ld_acquire_sys_s32` | `.release.sys` / `.acquire.sys` | `cross_rank_barrier`（跨 rank） |
| 加载（GPU 域） | `ld_acquire_gpu_s32` | `.acquire.gpu` | 各屏障的自旋等待 |
| 杂项 | `clock64`、`device_trap`、`popc_b32`、`ctz_b32` | — | 超时看门狗、位计数 |

设计逻辑与 u3-l6 讲过的屏障语义一一对应：**发布侧**用 `atom.add.release`（或 `red.release.sys`）把写出去，**等待侧**用 `ld.acquire` 自旋读同一个字，release/acquire 配对保证等待成功后能看到发布前的全部写。`.gpu` 与 `.sys` 的选择只看通信对端在不在同一张卡上。

#### 4.2.3 源码精读

**模块头注释——本文件存在的理由**：

[moonep/_common.py:1-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L1-L17) —— 文档字符串写明两个 lowering 限制（4.3 节展开）和「cp.async.bulk 是单线程指令，调用者必须包在 `if cute.arch.lane_idx() == 0:` 里」的纪律。

**一个标准原子封装的完整解剖**：

[moonep/_common.py:91-105](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L91-L105) —— `atom_add_relaxed_gpu_s32`：`Int32(llvm.inline_asm(T.i32(), [ptr_i64, Int32(val).ir_value(...)], "atom.add.relaxed.gpu.global.s32 $0, [$1], $2;", "=r,l,r", has_side_effects=True, ...))`。注意 `Int32(val).ir_value()` 这个显式降级：把 DSL 值转成裸 IR 操作数再交给汇编。其余原子助手（[L74-88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L74-L88)、[L108-122](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L108-L122)、[L124-138](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L124-L138)、[L141-155](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L141-L155)）只差指令名与符号类型（`s32` 有符号 min 语义正确，`b32` 用于按位 or 的位掩码）。

**两条指令拼一个 ctz**：

[moonep/_common.py:175-195](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L175-L195) —— PTX 没有数尾零指令，`ctz_b32` 用 `"brev.b32 $0, $1; clz.b32 $0, $0;"` 两条指令在一个汇编块里完成：先位反转，尾零变前导零，再 `clz`。文档同时警告「输入为 0 时返回 32」，调用方须保证非零。

**看门狗与自旋**：

[moonep/_common.py:40-55](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L40-L55) —— `clock64` 读 GPU 周期计数器（模板里 `%clock64` 是 PTX 特殊寄存器，所以操作数列表为空、只有输出）；[L58-71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L58-L71) 的 `device_trap` 对应 `trap;`。二者配合实现 `BARRIER_TIMEOUT_CYCLES`（100 秒，[L37](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L37)）超时快速失败，避免死锁时再污染屏障状态。

**使用现场之一：builder warp 的「两原子一发射」**（语义见 u3-l5/u4-l3，这里只看助手形态）：

[moonep/dispatch.py:625-641](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L625-L641) —— warp 聚合后由 lane 0 调 `atom_add_relaxed_gpu_s32` 各一次，为整个 warp 预留 `dup_groups`/`dup_loffs` 的紧凑前缀区间；返回值（旧值 = 区间基址）再经 `shuffle_sync` 广播回 32 个 lane。这是「relaxed 足够」的典型场景：区间互不相交，不需要 release 序。

[moonep/dispatch.py:557-576](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L557-L576) —— 选举阶段用 `atom_min_relaxed_gpu_s32`（最小 `packed` 编码胜出当主槽）+ `atom_or_relaxed_gpu_b32`（把 kidx 收进位掩码）。

[moonep/dispatch.py:650-681](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L650-L681) —— 发射阶段用 `popc_b32`（数组重复数）与 `ctz_b32`（逐位取出重复 kidx，`dup_mask & (dup_mask - 1)` 清最低位）。

#### 4.2.4 代码实践

**实践：给每个助手找「谁在用它」**

1. **实践目标**：用一次全库检索建立「助手 → 消费内核」的反向索引，直观感受 `_common.py` 的共享范围。
2. **操作步骤**：在仓库根目录执行（等价的 Grep 工具亦可）：
   ```bash
   grep -rn "atom_add_relaxed_gpu_s32\|atom_min_relaxed_gpu_s32\|atom_or_relaxed_gpu_b32\|popc_b32\|ctz_b32\|ld_acquire_sys_s32\|red_add_release_sys" moonep/ --include="*.py" | grep -v _common.py
   ```
3. **需要观察的现象**：`atom_*`/`popc`/`ctz` 只出现在 `dispatch.py`（builder warp）；`red_add_release_sys`/`ld_acquire_sys_s32` 只出现在 `_common.py` 自身（被 `cross_rank_barrier` 内部消费），`grad_reduce.py`、`inter_rank_sync.py` 只导入 `cross_rank_barrier` 这个成品。
4. **预期结果**：得到一张「底层原子只服务 builder；系统域原语被封装进跨 rank 屏障后再分发」的分层图。
5. 本实践纯静态检索，无需 GPU，可立即验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `atom_or_relaxed_gpu_s32` 和 `atom_or_relaxed_gpu_b32` 两个几乎相同的函数都要存在？

**答案**：PTX 的 `atom.or` 有 `.s32`（有符号）与 `.b32`（按位无类型）两种类型化形式，语义上按位或其实一致，但 DSL 侧的参数类型不同：前者收 `Int32`、后者收 `Uint32`（[L141-155](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L141-L155) 显式做 `Uint32(val)` 转换并返回 `Uint32`）。builder warp 里 `kmask` 张量是 `Uint32`（见 [dispatch.py:735](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L735) 的 `u32_ptr`），用 `b32` 版可避免符号语义混入位移表达式。两个封装换来类型系统上的干净。

**练习 2**：`popc_b32` 的 `has_side_effects=False`，如果误把它和 `atom_*` 一样置 True，会有什么后果？

**答案**：功能仍正确，但编译器失去了「纯函数」的证明：相同的 `popc(mask)` 调用不能被公共子表达式消除，也不能被随意移动到分支外，生成的代码更保守。反之，若把 `atom_*` 误置 False，两次相同参数的原子加可能被合并成一次——直接改变语义。`has_side_effects` 是正确性与优化空间之间的开关。

**练习 3**：`ld_acquire_gpu_s32(b0)` 与先 `ld.global.s32` 再 `fence.acq_rel.gpu` 等价吗？

**答案**：不等价。fence 是**双向**屏障（同时约束其前后的访存），开销更大且语义过强；`ld.acquire` 只对**这一条加载及其后的操作**建立获取序。在屏障自旋循环里（[L269](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L269) 每轮重读计数器），单条 acquire 加载是语义最贴切、成本最低的选择；这也是 PTX 内存模型（sm_70+）推荐的自旋写法。

---

### 4.3 直发 `cp.async.bulk`：两个 TMA lowering 限制与绕开

#### 4.3.1 概念说明

`_common.py` 开头（[L4-10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L4-L10)）白纸黑字写了绕开 DSL 的原因，两个限制：

1. **盒子上限**：DSL 的 `CopyBulkTensorTileG2SOp`（基于 TMA descriptor 的张量块拷贝）把单维 box 限制在 256 个元素。MoonEP 要整行搬运 hidden 状态，一行 H=7168 个 bf16 会被拆成
   \[ \left\lceil \frac{7168}{256} \right\rceil = 28 \]
   条 UTMA 指令，每行 28 次发射的指令开销吃掉了 TMA「一条指令搬大块」的意义。
2. **mbar 状态空间不匹配**：DSL 普通形式的 `CopyBulkG2SOp` 会强制插入 `mapa.shared::cluster`（把 mbarrier 地址转成 cluster 通用地址），但 `PipelineTmaAsync` 分配的 mbar 在 `shared::cta` 空间；两者不匹配的后果不是报错，而是**运行时挂死**——mbarrier 永远等不到 `complete_tx` 事务。

解法是绕开 DSL 的 TMA 封装层，用 `llvm.inline_asm` 直发朴素形态的 `cp.async.bulk`，语义对齐 CUTLASS C++ 的 `cute::SM90_BULK_COPY_{G2S,S2G}::copy`（[L12-13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L12-L13)）。两条指令的完成机制完全不同，必须分清：

- **G2S**（global → shared）：带 `mbarrier::complete_tx::bytes` 后缀，拷贝完成时硬件自动向目标 mbarrier 的事务计数加上 `size` 字节——与 `PipelineTmaAsync` 的 `expect_tx`（期待字节数）配对，事务凑齐 mbarrier 才翻相。
- **S2G**（shared → global）：带 `bulk_group` 后缀，没有 mbar；完成状态由 `cp.async.bulk.commit_group`（把当前在飞的拷贝编成一组）和 `cp.async.bulk.wait_group N`（等到至多 N 组在飞）管理。这两个跟踪指令 DSL 有内建（`cute.arch.cp_async_bulk_commit_group` / `cp_async_bulk_wait_group`），MoonEP 直接用内建，只直发拷贝本体。

最关键的纪律（[L15-16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L15-L16)）：`cp.async.bulk` 是**单线程指令**，一个 warp 里只能由一个 lane 发射，否则同一拷贝被发 32 遍、mbar 事务计数被超加 32 倍。

#### 4.3.2 核心流程

dispatch 数据通路的抽象流水（细节留給 u4-l2）：

```
warp 0（G2S 生产者，每 token 一次）:
  producer_acquire(stage 空闲)
  lane 0: cp_async_bulk_g2s(smem_stage, gmem_row, H*2, mbar)
          └─ 硬件完成后自动给 mbar +H*2 事务字节

warp 1（S2G 消费者，每 token K 次散射）:
  consumer_wait(stage 数据就绪)          ← mbar 事务凑齐
  lane 0: 对 k in 0..K:
            dst >= 0  → cp_async_bulk_s2g(smem_stage, 远端行, H*2)
            dst < 0   → 跳过 payload（负数编码，u3-l5）
            总是散射路由权重（普通 st）
          cp_async_bulk_commit_group()     ← 编组
  每(stage-1)个 token: wait_group(stages-1) + consumer_release  ← 节流并归还 stage
```

节流逻辑保证在飞 bulk_group ≤ stages−1，与 G2S 侧的 stage 数互锁，smem 循环复用。

#### 4.3.3 源码精读

**G2S 包装**：

[moonep/_common.py:414-435](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L414-L435) —— `cp_async_bulk_g2s`：模板 `cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes [$0], [$1], $2, [$3];`，约束 `"r,l,r,r"`——目的 smem 地址用** 32 位** `r`（smem 是 32 位地址空间！），源 gmem 地址 64 位 `l`，字节数与 mbar 地址各 32 位。无返回值（`None`）。

**S2G 包装**：

[moonep/_common.py:438-459](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L438-L459) —— `cp_async_bulk_s2g`：模板 `cp.async.bulk.global.shared::cta.bulk_group [$0], [$1], $2;`，约束 `"l,r,r"`——目的 gmem 64 位在前，源 smem 32 位在后。注意源侧状态空间是 `shared::cta`，正是限制 2 里被 DSL 写坏的那个点，直发时保持朴素形态即可。

**生产者 warp：lane 0 选举与指针降级**：

[moonep/dispatch.py:360-382](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L360-L382) —— `if cute.arch.lane_idx() == 0:` 包住发射；三行 `.toint()` 把 DSL 迭代器（`gmem_src.iterator + Int64(s) * Int64(H)` 等）降级成裸整型地址，再 `.ir_value()` 喂给内联汇编。`H_BYTES = H*2` 是编译期常量，一条指令搬一整行——这就是绕开限制 1 的直接收益。

**流水线对象与事务计数**：

[moonep/dispatch.py:315-337](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L315-L337) —— `PipelineTmaAsync.create(..., tx_count=H * 2, ...)`：DSL 的流水线原语照常使用（mbarrier 的初始化、`producer_acquire/consumer_wait` 状态机），只是「往 stage 里灌数据」的动作换成了自己的 `cp_async_bulk_g2s`——**管线是 DSL 的，搬运是裸 PTX 的**，这正是折中所在：既复用成熟的 stage 管理，又不吃两个 lowering 限制。

**消费者 warp：K 次散射 + 编组节流**：

[moonep/dispatch.py:387-448](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L387-L448) —— L402 的 `if cute.arch.lane_idx() == 0:` 注释写明「32 个 lane 会把 bulk_group 超减、权重重散射」；L434 `commit_group`，L439-441 按 `li >= stages-1` 节流 `wait_group(stages-1)`，L448 `wait_group(0)` 清尾。注释还特意说明最后一次 `consumer_release` 不是必须，但保留它让空 mbar 处于已知状态、内核可安全重放。

**16 字节对齐的由来**：

[moonep/dispatch.py:902](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L902) —— `assert ctx['H'] % 8 == 0`：`cp.async.bulk` 要求地址与大小都是 16 字节倍数，行大小 \(H \times 2\) 字节是 16 的倍数当且仅当 \(H \bmod 8 = 0\)；配合 `make_ptr(..., assumed_align=16)`（基地址对齐）共同满足约束。这是把硬件约束前移到宿主断言的例子。

#### 4.3.4 代码实践

**实践：验证限制 1 的算术与对齐断言**

1. **实践目标**：用纸笔（或本地 Python）复现两个数值结论，理解「为什么非要绕开」。
2. **操作步骤**：
   - 对 H=7168（bf16）：计算 \(\lceil 7168/256 \rceil\)，并与「一条 `cp.async.bulk` 搬 14336 字节」对比指令数。
   - 对 H ∈ {2048, 3584, 4096, 5120, 7168}：逐个检查 `H % 8`，判断哪些形状会被 [dispatch.py:902](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L902) 的断言拦下。
   - 再看 u2-l1 的符号表任取一组 (S,K,E,R)，确认 `H_BYTES = H*2` 与 `tx_count=H*2`（[dispatch.py:337](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L337)）单位一致（都是字节）。
3. **需要观察的现象**：7168/256 = 28（整除，无余数）；所有列出的 H 都是 8 的倍数——主流大模型隐藏维恰好全部放行。
4. **预期结果**：指令数从 28 降到 1；若某框架用了 H=2050 之类的奇维，dispatch 会在宿主断言处直接报错而不是在设备上静默错拷。
5. 本实践为算术验证型，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：G2S 和 S2G 的完成通知机制分别是什么？为什么不统一？

**答案**：G2S 用 mbarrier 事务计数（`complete_tx::bytes`，硬件自动加 `size` 字节，与 `expect_tx` 配对）；S2G 用 bulk_group（`commit_group`/`wait_group N`）。不统一是因为消费方向不同：G2S 的数据进 smem 后要被**同一内核的其他 warp** 消费，需要精确的 per-stage 到达通知（mbarrier 天然带相位）；S2G 写回 gmem 后只有**发射线程自己**关心「smem 什么时候可以回收」，编组等待就够了，不需要 mbar 的开销。

**练习 2**：如果去掉消费者侧的 `if li >= Int32(stages - 1):` 节流（[dispatch.py:439-441](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L439-L441)），会先坏哪一环？

**答案**：`consumer_release` 不会被调用，G2S 生产者的 `producer_acquire` 永远等不到空 stage，流水线在 stages 个 token 后停摆（死锁而非数据错误）。`wait_group` 与 `release` 必须成对：前者保证该 stage 的 smem 数据确实已被 S2G 读走，后者把 stage 归还生产者——只 release 不 wait 会更糟，那是把还没读走的 smem 提前归还。

**练习 3**：为什么 `cp_async_bulk_g2s` 的第一个约束是 `r`（32 位）而 `cp_async_bulk_s2g` 的第一个约束是 `l`（64 位）？

**答案**：模板里两者的操作数顺序都是「目的在前、源在后」。G2S 的目的是 shared memory——PTX 共享地址空间是 32 位的（`shared::cluster` 通用地址在当前用法下以 32 位窗口寻址），所以 `r`；S2G 的目的是 global/NVL 远端显存，必须 64 位（生产规模下 \(R \times NvS\_padded \times H \times 2\) 已超过 \(2^{31}\) 字节，见 [dispatch.py:178-180](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L178-L180) 的注释），所以 `l`。约束字母是地址空间事实的直接投影。

---

### 4.4 同步原语的组装与 PDL：`grid_sync` 的原子拼装与 `use_pdl` 启动

#### 4.4.1 概念说明

u3-l6 已经讲过 `grid_sync`/`cross_rank_barrier`/`cross_warp_sync` 的**语义**（自复位、哨兵位、相位槽、代理桥）与**调用时机**。本讲换一个视角：它们是 4.2 节助手的「组合应用题」，是 `_common.py` 里仅有的三个 `@cute.jit` 函数（其余全是 `@dsl_user_op`）——也就是说，它们不是一行汇编，而是**用 DSL 控制流 + 内联汇编原子拼出来的可复用同步构件**。

PDL（Programmatic Dependent Launch）解决的是另一个问题：默认情况下，同一流上相邻两个内核严格串行（前一个完全退出，后一个才能上 SM）。PDL 允许**后一个内核提前上线**，在前一个内核显式「放行」后立刻接续，把内核启动延迟藏进前一个内核的尾部。指令对：

- 前驱内核调 `griddepcontrol.launch_dependents`（MoonEP 封装为 `pdl_trigger_dependents`）：发布本 CTA 的写并放行依赖者；
- 后继内核以 `use_pdl=True` 启动，并在读前驱数据前调 `griddepcontrol.wait`（`pdl_wait_predecessor`）。

#### 4.4.2 核心流程

`grid_sync` 每轮的增量代数（细节见 u3-l6，此处看代码形态）：

```
tid==0:
  pid==0 ? inc = TAG - (nsm-1)   # 0x80000000 - (nsm-1)
        : inc = 1
  old = atom.add.release.gpu [bar], inc     # 发布到达
  spin: new = ld.acquire.gpu [bar]
        done ⇔ (new ^ old) & 0x80000000 ≠ 0  # 最高位翻转 = 全员到齐
```

每轮的增量总和：SM0 贡献 \(0x80000000-(nsm-1)\)，其余 \(nsm-1\) 个 SM 各贡献 \(1\)，故
\[ \bigl(0x80000000-(nsm-1)\bigr) + (nsm-1)\times 1 = 0x80000000 \]
即低 31 位每轮归零、最高位每轮翻转，**永不需要清零**，屏障可无限复用。

PDL 在 dispatch→epilogue 上的接线：

```
api.py (enable_pdl=True):
  launch_dispatch(..., pdl_trigger=True)          # dispatch 尾部放行
  launch_dispatch_epilogue(ctx, plan, pdl_launch=True)  # epilogue 提前启动 + 等待
```

#### 4.4.3 源码精读

**`grid_sync` 的原子拼装**：

[moonep/_common.py:249-271](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L249-L271) —— 前后两条 `cute.arch.sync_threads()`（CTA 内对齐）夹着 tid==0 的原子协议：`atom_add_release_gpu`（4.2 节助手）发布、`ld_acquire_gpu_s32` 自旋、`(new ^ old) & GRID_SYNC_TAG` 判完成。注意它用 `cute.arch.block_idx()` 在**设备代码里**区分 SM0——这正是 `@cute.jit`（可含控制流）与 `@dsl_user_op`（单算子）的组合价值。

**PDL 触发侧**：

[moonep/_common.py:393-400](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L393-L400) —— `pdl_trigger_dependents`：`sync_threads → fence_acq_rel_sys → sync_threads → (tid==0) griddepcontrol_launch_dependents`。中间的系统级 fence 保证本 CTA 此前的 NVL 写对被放行的依赖内核可见——fence 与放行的顺序不可颠倒。

**PDL 等待侧**：

[moonep/_common.py:403-406](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L403-L406) —— `pdl_wait_predecessor` 只是 `cute.arch.griddepcontrol_wait()` 的一行包装（DSL 已内建该指令）；它必须出现在**任何读取前驱输出之前**。

**两端的接线**：

[moonep/dispatch.py:683-692](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L683-L692) —— dispatch 内核在退出屏障之后、按 `pdl_trigger` 常量调 `pdl_trigger_dependents(tidx)`；
[moonep/dispatch_epilogue.py:182-183](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L182-L183) —— epilogue 内核入口处 `if const_expr(self.pdl_launch): pdl_wait_predecessor()`，而 `use_pdl=self.pdl_launch` 打在 launch 上（[L157-164](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L157-L164)）；
[moonep/api.py:649-653](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L649-L653) —— 顶层把 `self.enable_pdl` 同时接到两侧（`pdl_trigger=` 给 dispatch，`pdl_launch=` 给 epilogue）。同样的配对还出现在 combine 链路（`combine_prologue.py:485` 触发、`combine.py:235` 等待）。

**`cross_warp_sync` 的看门狗**：

[moonep/_common.py:349-390](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L349-L390) —— 与 `grid_sync` 同构（`WARP_SYNC_TAG` 同为 `0x80000000`），参与者是各 CTA 的 builder warp 的 lane 0；自旋里嵌了 `clock64()` 超时 + `printf` + `device_trap()` 快速失败——所有自旋等待都配了这只看门狗（`cross_rank_barrier` 同款，[L331-341](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L331-L341)）。

#### 4.4.4 代码实践

**实践：画出 PDL 配对图并核对 fence 顺序**

1. **实践目标**：把「触发侧内核 + 等待侧内核 + use_pdl 启动属性」的三点式结构在两条链路（dispatch→epilogue、prologue→combine）上各画一张时序图。
2. **操作步骤**：
   - 检索 `pdl_trigger|pdl_launch|use_pdl|pdl_wait|enable_pdl` 在 `moonep/` 的全部命中；
   - 对每条链路标注：谁带 `use_pdl`、谁调 `griddepcontrol_*`、fence 在放行之前还是之后；
   - 回答：dispatch 自己的 launch（[dispatch.py:258-264](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L258-L264)）没有 `use_pdl`，为什么它不需要？
3. **需要观察的现象**：PDL 属性永远打在**后启动**的内核上；触发侧只写 `pdl_trigger=True` 常量并调触发助手。
4. **预期结果**：两张图各含「前驱内核尾部 fence→放行 / 后继内核入口等待」的对称结构；dispatch 不带 `use_pdl` 是因为它前面没有用 PDL 放行的前驱（它前面的 planning 用普通流序同步）。
5. 本实践为源码阅读型，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：`grid_sync` 里为什么是 `atom_add_release_gpu` 而不是 `atom_add_relaxed_gpu_s32`？

**答案**：到达原子不只是改计数器，还承载「我此前的全部写已完成」的发布语义。若用 relaxed，其他 SM 在看到计数变化后未必能看到该 SM 在屏障前的写（u3-l6 讲过的「跨 SM 排序靠 release/acquire」）。配套地，等待侧必须用 acquire 加载。relaxed 版只用于不需要携带写发布的场合，如 builder warp 预约输出区间。

**练习 2**：`pdl_trigger_dependents` 里若把 `fence_acq_rel_sys` 挪到 `griddepcontrol_launch_dependents` 之后，会出什么问题？

**答案**：放行指令先于 fence 生效时，依赖内核可能在前驱 CTA 的 NVL 写对系统域可见之前就开始读取——读到旧数据。PDL 的正确协议是「先发布（fence）再放行（launch_dependents）」，fence 必须在前。这也是该函数注释里「Publish this CTA's writes, **then** trigger」的含义。

**练习 3**：`cross_warp_sync` 断言 `nparticipants < WARP_SYNC_TAG`（[L363-366](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L363-L366)），`grid_sync` 却没有对 `nsm` 的类似断言，为什么？

**答案**：自复位代数要求「普通参与者每轮 +1」的总量不超过哨兵位之外的 31 位计数空间。`cross_warp_sync` 的参与者是 `num_sms`（CTA 级）而 `grid_sync` 也是 `nsm` 个 CTA——两者其实面对同样的约束；区别在于 `cross_warp_sync` 的 `nparticipants` 是调用方自由传入的参数（可能被误用），而 `grid_sync` 的 `nsm` 来自 launch 几何、天然等于 CTA 数且远小于 \(2^{31}\)，所以只在可被误用的入口设断言。另一个实际原因：31 位空间对任何真实 SM 数都绰绰有余，断言只是防呆。

---

### 4.5 编译缓存：`_get_compiled` 的 lru_cache 与流水线深度自适应

#### 4.5.1 概念说明

JIT 的编译成本以秒计，绝不能发生在通信热路径上。MoonEP 的方案朴素而有效：`functools.lru_cache(maxsize=None)` 包住「构造内核对象 + `cute.compile`」，**缓存键 = 所有一切会影响生成代码的量**。同时，dispatch 的流水线深度 `stages` 不是写死的，而是在构造期按设备的 opt-in 共享内存预算从 16 往下试到 2——这意味着 `stages` 是形状 (H) 与设备 (device_index) 的函数，自然也进了缓存键。

两个缓存函数：

- `_max_smem_per_block_optin(device_index)`：设备的 opt-in smem 上限（H100 级为 227 KB 档），按设备缓存；
- `_get_compiled(...)`：按 14 元组键缓存编译产物。

#### 4.5.2 核心流程

```
首次调用某形状组合:
  _get_compiled(H, R, S, K, zero_groups, NvS, NvS_padded, meta_stride,
                SRC_INFO_OFF, num_sms, with_weights, build_dedup_map,
                device_index, pdl_trigger)
    ├─ smem_budget = 设备 optin 上限 − 1024        # 留给非静态 smem 的余量
    ├─ DispatchKernel(...)                          # 构造期: _pick_stages 选深度
    ├─ make_ptr(BFloat16, 0, ...) 等哑指针           # 占位: 只定类型/地址空间/对齐
    └─ cute.compile(kernel, 哑指针..., Int32(0), ..., CUstream(0))
                                                        # 编译 → cubin
后续调用(同键):
  直接返回缓存对象; launch_dispatch 用真实 data_ptr 调用它
```

`_smem_bytes(H, stages)` 的预算公式（全部向上取整到对齐粒度）：

\[ \text{smem} = \lceil stages \cdot H \cdot 2 \rceil_{128} + \lceil H \cdot 2 \rceil_{128} + \lceil stages \cdot 2 \cdot 8 \rceil_{16} + 256 \]

四项分别是：kStages 深的 stage 缓冲（bf16 行）、零填充用的单行 `zero_smem`、每 stage 两个 i64 mbar、以及给 CUTLASS 内部静态 smem 的 256 B 余量。

#### 4.5.3 源码精读

**设备 smem 上限缓存**：

[moonep/dispatch.py:699-701](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L699-L701) —— `torch.cuda.get_device_properties(device_index).shared_memory_per_block_optin` 包上 `lru_cache`；同设备重复查询零成本。

**编译缓存本体**：

[moonep/dispatch.py:704-758](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L704-L758) —— `_get_compiled` 的 14 个参数就是缓存键；L721 先算 `smem_budget`；L733-736 造三个**哑指针**（`make_ptr(BFloat16, 0, cute.AddressSpace.gmem, assumed_align=16)`，地址为 0——编译只需要类型/地址空间/对齐信息，不需要真地址）；L738 `cute.compile(kernel, ...)` 完成从 DSL 到 cubin 的全流程。

**stages 候选表与预算公式**：

[moonep/dispatch.py:124-143](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L124-L143) —— `_pick_stages` 依序尝试 `(16, 14, 12, 10, 8, 6, 4, 2)`，返回第一个预算内的值；全不放则返回 0，由构造函数（[L117-122](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L117-L122)）抛 `RuntimeError`（H 大到连 2 级流水都放不下）。

**缓存键的完备性**：

[moonep/dispatch.py:914-918](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L914-L918) —— `launch_dispatch` 调 `_get_compiled` 时传的 `E + B` 即 `zero_groups`。核对内核体内所有 `const_expr`（L289-308）：每个都出现在缓存键里（`num_threads` 由 `build_dedup_map` 唯一决定，`H_BYTES`/`NvS_BITS` 等是 H/NvS 的派生量）。**键不完备 = 不同配置共享错误 cubin**，这是这类缓存最容易翻车的地方。

**位编码上限检查（编译前的宿主防线）**：

[moonep/dispatch.py:761-784](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L761-L784) —— `_check_dedup_builder_bounds` 在 fresh 路径检查 `S*K <= NvS`、`R*NvS <= int32_max`、`K <= 2^KIDX_BITS - 1`、`NvS <= 2^NvS_BITS - 1`、`K <= 32`——这些是 u3-l5 引入的 packed 编码（`KIDX_BITS=7`，[constants.py:10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L10)）的容量边界，越界会在设备上静默溢出，所以必须在宿主拦下。

#### 4.5.4 代码实践

**实践：手算 H=7168 时的流水线深度**

1. **实践目标**：用 `_smem_bytes` 公式算出 H100 级设备（`shared_memory_per_block_optin = 232448`）上 dispatch 会选几级流水。
2. **操作步骤**：
   - `smem_budget = 232448 − 1024 = 231424`；
   - 对候选 s ∈ {16, 14, 12, ...} 逐个计算（H·2 = 14336）：
     - s=16：\(\lceil 229376 \rceil_{128} + 14336 + \lceil 256 \rceil_{16} + 256 = 229376 + 14336 + 256 + 256 = 244224\)；
     - s=14：\(200704 + 14336 + 224 + 256 = 215520\)；
   - 找出第一个 ≤ 231424 的 s。
3. **需要观察的现象**：s=16 超预算约 12.8 KB 被否决；s=14 有约 15 KB 富余。
4. **预期结果**：`stages = 14`，即 dispatch 在 H=7168 时维护 14 行深度的 smem 环形缓冲。具体设备数值以 `torch.cuda.get_device_properties(...)` 实测为准——不同代卡（如 sm_90 与更新）optin 上限不同，结论**待本地验证**。
5. 无 GPU 时本实践可先完成全部算术；有 GPU 时加一行 `print(torch.cuda.get_device_properties(0).shared_memory_per_block_optin)` 收尾。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `with_weights` 和 `build_dedup_map` 这两个布尔值也要进缓存键？

**答案**：它们在内核体内是 `const_expr` 分支（[dispatch.py:199-206](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L199-L206) 的 `w_tensor` 形状二选一、[L503](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L503) 的整个 builder 分支），编译产物不同；`build_dedup_map` 还决定 `num_threads`（96 vs 224）。若不进键，fresh 路径可能拿到 reuse 版编译产物，builder warp 直接消失、dedup 结构无人构建。

**练习 2**：`cute.compile` 时传的指针地址全是 0，编译产物为什么还能在真实地址上工作？

**答案**：占位指针只提供**类型签名**（dtype、地址空间、对齐假设）——JIT 编译需要的是「参数的类型形状」而非具体值；运行期调用缓存产物时传入的 `make_ptr(..., tensor.data_ptr(), ...)` 才是实际地址，作为运行期参数绑定。这与 4.1 节「编译期定形、运行期定值」的分流一致。

**练习 3**：`smem_budget` 为什么要减去 1024 字节，而 `_smem_bytes` 又额外加 256 字节余量？

**答案**：两处都是对「不可精确预知的 smem 消费」的保守垫层：减 1024 是给 DSL/驱动在 opt-in 之外可能附加的静态共享内存（如内核参数区）留空间；加 256 是注释写明的「任何 cutlass 内部静态 smem」余量（[dispatch.py:128](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L128)）。宁可少一级流水，也不能让 `cudaFuncSetAttribute` 因超预算失败——启动失败比浅流水昂贵得多。

---

## 5. 综合实践

把本讲全部内容串成一个**两段式任务**：第一段（无 GPU 可做）生成 PTX 助手对照表，第二段（需 GPU，待本地验证）用最小 DSL 内核验证一个助手的语义。

### 5.1 第一段：helper ↔ CUDA C++/PTX 对照表生成器

写一个 `ptx_cheatsheet.py`（放任意目录，只读仓库），用 `ast` 静态解析 `_common.py`，把每个 `@dsl_user_op` 函数里的内联汇编模板、约束串、`has_side_effects` 提取成 Markdown 表，并拼上人工维护的「等价 CUDA C++ 写法」列：

```python
# 示例代码：ptx_cheatsheet.py —— 静态提取 _common.py 的 inline-asm 助手清单
import ast
from pathlib import Path

EQUIV = {  # 人工维护的语义对照（CUDA C++ / libcu++ 侧最接近的写法）
    "clock64":              "clock64()",
    "device_trap":          "__trap()",
    "atom_add_release_gpu": "cuda::atomic_ref<int, thread_scope_device>::fetch_add(release)",
    "atom_add_relaxed_gpu_s32": "cuda::atomic_ref<int, thread_scope_device>::fetch_add(relaxed)",
    "atom_min_relaxed_gpu_s32": "atomicMin()（天然 relaxed）或 asm 直发",
    "atom_or_relaxed_gpu_s32":  "atomicOr()（天然 relaxed）",
    "atom_or_relaxed_gpu_b32":  "atomicOr()（unsigned 视角）",
    "popc_b32":             "__popc()",
    "ctz_b32":              "__ffs() - 1（输入非零前提）",
    "ld_acquire_gpu_s32":   "cuda::atomic_ref<int, thread_scope_device>::load(acquire)",
    "red_add_release_sys":  "asm red.release.sys（CUDA C++ 无直接内建）",
    "ld_acquire_sys_s32":   "cuda::atomic_ref<int, thread_scope_system>::load(acquire)",
    "cp_async_bulk_g2s":    "cute::SM90_BULK_COPY_G2S::copy",
    "cp_async_bulk_s2g":    "cute::SM90_BULK_COPY_S2G::copy",
}

def extract(path="moonep/_common.py"):
    out = []
    for node in ast.parse(Path(path).read_text()).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "inline_asm"):
                asm  = sub.args[2].value       # 第 3 个位置参数：汇编模板
                cons = sub.args[3].value       # 第 4 个位置参数：约束串
                hse  = any(k.arg == "has_side_effects" and k.value.value
                           for k in sub.keywords)
                out.append((node.name, node.lineno, asm, cons, hse))
                break
    return out

if __name__ == "__main__":
    print("| 助手 | 行 | PTX 模板 | 约束 | 副作用 | CUDA C++ 等价 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, ln, asm, cons, hse in extract():
        print(f"| `{name}` | L{ln} | `{asm}` | `{cons}` | {'是' if hse else '否'}"
              f" | {EQUIV.get(name, '—')} |")
```

**预期结果**（基于对 `_common.py` 的静态阅读，共 14 行，按定义顺序）：`clock64`、`device_trap`、`atom_add_release_gpu`、`atom_add_relaxed_gpu_s32`、`atom_min_relaxed_gpu_s32`、`atom_or_relaxed_gpu_s32`、`atom_or_relaxed_gpu_b32`、`popc_b32`、`ctz_b32`、`ld_acquire_gpu_s32`、`red_add_release_sys`、`ld_acquire_sys_s32`、`cp_async_bulk_g2s`、`cp_async_bulk_s2g`。其中只有 `popc_b32` 与 `ctz_b32` 两行「副作用 = 否」。本段在任意有 Python 3 的环境都可验证（无需 GPU、无需安装 cutlass——ast 解析不执行被解析代码）。

### 5.2 第二段（需 GPU，待本地验证）：最小内核验证 `atom_add_relaxed_gpu_s32`

在装好 MoonEP 依赖（`nvidia-cutlass-dsl`，见 u1-l2）的机器上，写一个最小 `@cute.kernel`：一个 CTA、N 个线程，每线程对同一 gmem 计数器 `atom_add_relaxed_gpu_s32(ptr, 1)` 各调一次，宿主侧读回并断言等于 N：

- 骨架参考 [dispatch.py:147-264](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L147-L264) 的三层结构：`@cute.jit` 入口 + `@cute.kernel` 内核 + `cute.compile`（可仿照 `_get_compiled` 用 `lru_cache` 包一层）；
- 计数器用 `torch.zeros(1, dtype=torch.int32, device="cuda")`，内核里经 `make_ptr(Int32, t.data_ptr(), cute.AddressSpace.gmem, assumed_align=16)` 传入，`.iterator.toint().ir_value()` 后喂给助手；
- 观察点：① 计数器终值恰为线程数；② 把入参从 1 改成 2，终值线性翻倍——验证「原子累加、无丢失」；
- 本段在无 GPU 环境下**不可运行**，标注待本地验证；若想先做纯逻辑对拍，可对照 5.1 表中 `fetch_add(relaxed)` 语义写一个 CPU 侧 `threading` 模拟（结果应同样是无丢失求和）。

## 6. 本讲小结

- **编译-启动模型**：`@cute.kernel` 定义设备代码，`@cute.jit` 入口做指针包装与 `launch`（grid/block/smem/cooperative 在构造期定死），`cute.compile` 用哑指针完成按形特化；`const_expr` 把形状烧成编译期常量，`rank`/偏移/指针留在运行期。
- **共享 PTX 助手**：`_common.py` 用 `@dsl_user_op + llvm.inline_asm` 封装 14 个算子；约束字母（`r`/`l`）直接反映 32 位 smem / 64 位 gmem 地址空间；`has_side_effects` 区分可 CSE 的纯位运算与不可重排的原子/加载。
- **两个 TMA lowering 限制**：descriptor box 256 元素上限会把一行 H=7168 拆成 28 条 UTMA；普通 `CopyBulkG2SOp` 的 `mapa.shared::cluster` 与 `PipelineTmaAsync` 的 `shared::cta` mbar 不匹配会挂死。绕开方式是直发朴素 `cp.async.bulk`：G2S 走 mbarrier 事务计数，S2G 走 bulk_group 编组等待，且**只由 lane 0 单线程发射**。
- **同步原语**：`grid_sync`/`cross_warp_sync` 是「release 原子 + acquire 自旋 + 哨兵位翻转」的自复位拼装，全部带 clock64 看门狗；PDL 用「前驱 `pdl_trigger_dependents`（先 fence 后放行）/ 后继 `use_pdl=True` + `pdl_wait_predecessor`」把相邻内核的启动延迟藏进前驱尾部。
- **编译缓存**：`_get_compiled` 的 14 元组 lru_cache 键覆盖内核体内**每一个** `const_expr`，键不完备等于配置间串用 cubin；流水线深度按 `smem_budget = optin − 1024` 从 16 级向下自适应（H=7168 在 227 KB 档设备上落到 14 级）。

## 7. 下一步学习建议

本讲只搭好了「语言与工具」的脚手架，尚未深入任何一个内核的数据通路。下一讲 **u4-l2「dispatch 内核：warp 特化与 TMA 流水线」** 将把本讲的全部词汇投入到实战：warp 0/1 的生产-消费协议、`PipelineTmaAsync` 的 stage 状态机、负数 dst 的执行路径、退出屏障的发布范围。阅读顺序建议：

1. 带着 u4-l2 的讲义重读 [moonep/dispatch.py:360-448](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L360-L448)（生产者/消费者 warp），体会「管线是 DSL 的、搬运是裸 PTX 的」这句话在代码里的样子；
2. 对照 CUTLASS C++ 的 `SM90_BULK_COPY_G2S/S2G`（`cute/arch/copy_sm90*.hpp`，若本地有 cutlass 源码）验证 `_common.py` 文档声称的语义对齐；
3. 想加深 PTX 内存序的理解，可读 CUDA C++ Programming Guide 的 Memory Consistency Model 章节，把 `.relaxed/.release/.acquire` × `.gpu/.sys` 的矩阵与本讲 4.2.2 的分组表互相对照。
