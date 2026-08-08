# TVM-FFI 编译启动与 fake 张量

## 1. 本讲目标

本讲是 GEMM 主机侧系列的第三讲，聚焦「PyTorch 张量如何跨越 FFI（Foreign Function Interface）边界进入 CuTe-DSL 编译产物」这一关键环节。主角是 [quack/gemm_tvm_ffi_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py)，配套 [quack/gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py)。

学完后你应该能够：

- 理解 `make_fake_gemm_tensors` 如何用**符号维度（`cute.sym_int`）**刻画一整族 GEMM 形状，使一份 cubin 复用于任意 (M, N, K, batch)。
- 掌握 `compile_gemm_kernel` 如何把一个「按 SM 分发选出的内核类」实例化、配上 fake 张量，编译成可被反复调用的 TVM-FFI 函数。
- 理解 `launch_gemm` 与 `scalar_mode` / `scalar_arg` / `tensor_key` 三件套如何把**编译期结构**与**每次调用的数据指针 / 标量值**解耦，把命中路径压到微秒级。

## 2. 前置知识

阅读本讲前，建议你已经掌握 u4-l1（GEMM 编译与计划缓存的三层架构）与 u3-l3（`make_fake_tensor` / 符号张量 / `divisibility`）。下面用通俗语言补两个本讲会反复用到的概念。

**什么是 FFI 边界？** CuTe-DSL 用 Python 写内核，但编译产物是一个「机器码函数」（cubin），它由底层 C++/CUDA 运行时承载。Python 主机要调用它，必须经过一层「外语函数接口」——TVM-FFI。张量、标量、流（stream）都得以某种约定的形式从 Python 侧送过去。QuACK 用 `cute.compile(..., options="--enable-tvm-ffi")` 把内核包成一个接受真实张量的 Python 可调用对象 `compiled_fn`。

**什么是「编译期」与「运行期」之分？** 一个 GEMM 内核里有些东西必须**在编译时定死**：tile 形状、cluster 形状、数据类型（dtype）、主序（major）、epilogue 结构——它们决定了寄存器分配、循环展开、MMA 指令选择，会烘焙进 cubin。另一些东西则**每次调用都可能变**：具体 (M, N, K)、数据指针、alpha/beta 的数值。QuACK 的核心技巧是：用**符号整数** `cute.sym_int()` 告诉编译器「这一维是某个未知但合法的运行期值」，于是同一份 cubin 能服务整族形状。

> 关键直觉：**「形状进运行期，结构进编译期」**。本讲所有工具都在落实这条原则。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/gemm_tvm_ffi_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py) | 本讲主角。提供 fake 张量构造、编译、启动、以及 `scalar_mode` / `tensor_key` 等跨 FFI 边界的通用工具。 |
| [quack/gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py) | 用上述工具组装出 `_compile_gemm`（编译层）、`gemm`（公共入口 + 计划缓存）、`run_gemm_plan`（启动层）。 |
| [quack/compile_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py) | 更底层的 `make_fake_tensor` / `fake_batched` / `div_for_dtype` 叶子工具，被本讲主角复用（u3-l3 已讲）。 |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | 设备侧内核基类。其 `rotate_batch_last` / `permute_batch_last` 在 trace 期把「batch 在前」的张量重排成内核顺序——这是理解 fake 张量为何要「batch 在前」的关键对照。 |

## 4. 核心概念与源码讲解

### 4.1 make_fake_gemm_tensors：用符号张量刻画一整族 GEMM 形状

#### 4.1.1 概念说明

编译一个 GEMM 内核时，我们手头并没有「真实」的张量（那是运行期才有的数据）。但我们仍然要给编译器一组张量**样例**，让它推断出布局、dtype、rank、主序等结构信息——这就是 **fake 张量（fake tensor）**，也称「符号张量」或「无数据编译（tensor-free compilation）」。

`make_fake_gemm_tensors` 要解决的问题是：**用最少的、最通用的 fake 张量，描述一整族 GEMM 调用的形状结构**，使得编译产物对族内任意具体形状都成立。

它的设计原则有两条：

1. **动态维度（M、N、K、batch L）用符号**：这些是运行期数据相关的量，必须留在运行期。
2. **可整除性（divisibility）按需声明**：如果某一维在运行期一定可被某数整除（例如 bf16 的连续维总是 8 字节即 4 元素对齐），就告诉编译器，让它发射更宽的向量化加载。

#### 4.1.2 核心流程

`make_fake_gemm_tensors` 的整体流程可以概括为：

1. 由各操作数的 **major（主序）** 推出 `leading_dim`：主序维的 stride 静态为 1，其余维 stride 是符号。
2. 申请一组共享符号：`m, l = sym_int()`，再按 dtype 给 `n`、`k` 附上 divisibility。
3. 处理 **sub-byte（亚字节）dtype**（fp4/fp6）的特殊对齐与打包：这类操作数的连续维必须静态可整除，有时还需独立的符号（如打包 fp6 跨边界是裸字节，K 逻辑长度 ≠ 存储长度）。
4. 按调用形态（dense / varlen_m / varlen_k / swap_ab / b_kn / packed_cd）用 `fake_batched` 拼出 mA、mB、mD、mC，**全部 batch 在前 (l, x, y)**。
5. 返回 `(mA, mB, mD, mC, m, n, k, l)`——符号也一并交回，调用方（`_compile_gemm`）还要用 `l` 去构造 rowvec/colvec 等 epilogue 张量。

其中第 4 步「batch 在前」是个重要约定：调用者传进来的真实 torch 张量是 `(l, x, y)`，内核在 trace 期由 `GemmBase.rotate_batch_last` 把它**免费**重排成内核顺序 `(x, y, l)`，从而省掉每次调用都要做的 `.permute()` 主机视图（每个视图约 0.7µs 开销）。fake 张量必须与此保持一致，也按 batch 在前构造。

#### 4.1.3 源码精读

先看符号维度的申请。`m` 与 `l`（batch）是无约束符号；`n` 与 `k` 则按 dtype 附 divisibility：

[quack/gemm_tvm_ffi_utils.py:560-581](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L560-L581) —— 申请共享符号 `m, l`，并按 dtype 给 `n`、`k` 算 divisibility。说明：`cute.sym_int()` 表示「任意正整数」；`cute.sym_int(divisibility=d)` 表示「任意 d 的倍数」。`div_for_dtype(dt)` 返回 `128 // dt.width`，即「128 位（16 字节）对齐折算成元素数」。对 bf16（width=16）即 8，于是编译器可发射 128 位宽加载。

K 的 divisibility 有一段更细的处理，因为 sub-byte 操作数的对齐/打包规则更复杂：

[quack/gemm_tvm_ffi_utils.py:572-589](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L572-L589) —— 说明：当 A、B 任一是 fp4/fp6（width<8）且不是「双方皆 fp4」时，K 必须静态可被 128 整除（这是 TMA 解包 tensormap 的颗粒要求）；打包 fp6 跨边界是裸字节（torch 没有 fp6 dtype），其存储 K 是逻辑 K 的 3/4，无法在 arg spec 里表达「4/3 关系」，于是给它**独立的** `sym_int(divisibility=96)` 符号，逻辑一致性交给主机侧校验。

再看 `leading_dim` 的推导与最终的 `fake_batched` 调用（以最常见的 dense batched 分支为例）：

[quack/gemm_tvm_ffi_utils.py:556-559](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L556-L559) —— 由 major 推 leading_dim：m-major ⇒ leading=0，k-major ⇒ leading=1。leading_dim 那一维 stride 静态为 1（连续维）。

[quack/gemm_tvm_ffi_utils.py:631-640](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L631-L640) —— dense 分支用 `fake_batched` 拼出 mA/mB/mD/mC。注意 `fake_batched(dtype, x, y, l, leading_dim, divisibility)` 把 batch `l` 放在最前，构成 `(l, x, y)`，并把 leading_dim 自动 +1（因为 batch 维前置了）。

`fake_batched` 本身很薄，可对照 [quack/compile_utils.py:41-53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py#L41-L53) 理解：`l=None` 时退化为 2D 张量（用于 varlen 展平后的操作数），`leading_dim + 1` 正是「batch 维前置」的体现。

最后，trace 期免费重排的逻辑在设备侧基类：

[quack/gemm_base.py:141-160](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L141-L160) —— `rotate_batch_last` 说明：调用者以 `(l, x, y)` 跨边界传张量，内核在 trace 期把它重排成 `(x, y, l)`，用编译期布局改写替代每次调用的 `.permute()` 视图。fake 张量必须 batch 在前正是为了匹配这一点。

> 数学视角：divisibility 声明本质是给编译器一个前提 \( d \mid n \)（d 整除 n）。有了它，编译器可以把长度为 n 的连续区间切成 \( n/d \) 个 d 元素的块，每块发射一条对齐的宽加载；没有这个前提，编译器只能保守地逐元素或窄加载。

#### 4.1.4 代码实践

**实践目标**：亲手预测一个 bf16 dense GEMM 的各符号维 divisibility，并理解为何 sub-byte 操作数更严格。

**操作步骤**：

1. 打开 [quack/gemm_tvm_ffi_utils.py:560-581](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L560-L581)，假设 `a_dtype = b_dtype = d_dtype = Float16`（width=16），C 为 None。
2. 手算：`div_for_dtype(Float16) = 128 // 16 = 8`。
3. 推断各符号的 divisibility：
   - `m`、`l`：`sym_int()` ⇒ 无约束（任意正整数）。
   - `n`：`n_div = 1`（因为 d_dtype.width=16 不小于 8）⇒ `n = sym_int(divisibility=1)`，即任意。
   - `k`：A、B 都不是 sub-byte ⇒ `k_div = max(..., default=1) = 1` ⇒ `k = sym_int(divisibility=1)`，即任意。
4. 现在把 `a_dtype` 换成 fp4（width=4，非双方皆 fp4 的混合对），重算：`k_div = 128`，于是 `k` 必须是 128 的倍数。

**需要观察的现象**：bf16 dense GEMM 的 M/N/K/L 几乎全无整除约束（最宽松），而 fp4/fp6 操作数会强制 K 是 128 的倍数。

**预期结果**：步骤 3 的结论是「bf16 下所有动态维都几乎自由」，步骤 4 的结论是「fp4 下 K 被 128 整除」。这正是为何量化 GEMM 在测试里常取 `K ∈ {512, 1024, ...}` 这类 128 倍数。

**待本地验证**：若你装了 cutlass-dsl，可在 Python 里 `import cutlass.cute as cute; s = cute.sym_int(divisibility=128); print(s)` 确认符号对象的形态（不会触发编译）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `m`（M 维）和 `l`（batch 维）用的是无约束 `sym_int()`，而 `n`、`k` 有时要带 divisibility？

**参考答案**：因为 divisibility 的作用是**允许更宽的向量化加载**，而向量化加载发生在**连续维（leading_dim）**。M 维通常不是连续维（连续维由 major 决定，常是 K 或 N），所以对它声明 divisibility 没有发射宽加载的收益；而 N、K 常作为某操作数的连续维，声明 divisibility 才能让编译器放心发射 128 位加载。batch 维 `l` 是最外层、stride 最大，更无对齐收益。

**练习 2**：`packed_cd="n"` 是什么场景？为何它要一个**独立**的符号 `pd` 而不复用 `n`？

**参考答案**：`packed_cd` 用于 dgated 打包原生输出——D/C 以原始 16-bit dtype 跨边界，但其打包长度是 f32 视图的两倍。独立符号是因为「2n = 2*n」这种编译期关系无法在 arg spec 里表达，所以打包长度自己有一个符号，内核在 trace 期通过对半（`_recast_packed_cd`）推导出 f32 视图。

---

### 4.2 compile_gemm_kernel：把内核类编译成 TVM-FFI 函数

#### 4.2.1 概念说明

`make_fake_gemm_tensors` 造好了「形状样例」，但内核本体是一个 Python 类（如 `GemmDefaultSm90`），不能直接当函数调用。`compile_gemm_kernel` 的职责是：

1. **按 SM 选内核类并烘焙配置**：用 `functools.partial` 把 `pingpong`、`persistent`、`use_clc_persistence`、`sf_vec_size` 等「编译期开关」绑定到内核类上，得到一个半成品类。
2. **实例化内核对象**：传入 `tile_shape_mn`、`cluster_shape_mnk`、dtype、`gather_A`、`concat_layout` 等，构造出 `gemm_obj`。
3. **设置 trace 期重排标志**：`b_transposed` / `a_transposed` / `cd_transposed` / `cd_packed` 告诉 `rotate_batch_last` 要做哪些免费重排。
4. **调用 `cute.compile`**：把对象 + fake 张量 + fake stream 交给编译器，产出可反复调用的 TVM-FFI 函数。

它返回的不是数据结果，而是**编译产物本身**（一个绑定好 arg spec 的可调用对象）。

#### 4.2.2 核心流程

```
compile_gemm_kernel(GemmCls, ..., mA, mB, mD, mC, epi_args, scheduler_args, varlen_args, ...)
   │
   ├─ 1. 按 device_capacity[0]（SM 主版本）分流：
   │     • SM80 : partial(is_persistent, num_warps, arch)
   │     • SM90 : partial(pingpong, is_persistent, split_k_kwargs)
   │     • SM120: 同 SM90 + use_clc_persistence + blockscaled 的 sf_vec_size/mma_dtype
   │     • SM100/110: partial(use_clc_persistence, use_tma_gather, sf_vec_size, ...)
   │
   ├─ 2. gemm_obj = GemmCls(Float32, a_dtype, tile_shape_mn, cluster_shape_mnk,
   │                        gather_A=..., concat_layout=...)
   │
   ├─ 3. gemm_obj.b_transposed / a_transposed / cd_transposed / cd_packed = ...
   │     （trace 期 rotate_batch_last 据此免费重排）
   │
   ├─ 4. stream = make_fake_stream(use_tvm_ffi_env_stream=True)
   │
   └─ 5. return cute.compile(gemm_obj, mA, mB, mD, mC, epi_args,
                              scheduler_args, varlen_args, stream, mSFA, mSFB,
                              options="--enable-tvm-ffi")
```

注意第 5 步：`mSFA, mSFB` 总是被传入（blockscaled 的 scale factor 张量），非 blockscaled 时为 None。注释强调「统一的尾部签名」——所有 SM 的内核类都接受尾部 `(SFA, SFB)`，编译出的 arg spec 把完整 arity（含默认值）烘焙进去，所以启动时也总是传。

#### 4.2.3 源码精读

先看按 SM 分流的 partial 绑定。这是「编译期开关烘焙」的核心：

[quack/gemm_tvm_ffi_utils.py:711-747](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L711-L747) —— 说明：按 `device_capacity[0]` 用 `partial(GemmCls, ...)` 把编译期常量（`pingpong`、`is_persistent`、`use_clc_persistence`、`sf_vec_size`、`a/b_mma_dtype` 等）绑定到内核类。尤其注意 SM120 的 `use_clc_persistence` 同时门控于 `is_dynamic_persistent`、`persistent` 与**编译目标**架构（`get_compile_target_capacity()[0] >= 10`）——因为 CLC 指令是 sm_100+，H100 CI 代理（`QUACK_ARCH=120` 但为 sm_90a 编译）必须回退到静态调度，否则 NVVM 会拒绝内核。

再看实例化与重排标志：

[quack/gemm_tvm_ffi_utils.py:748-762](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L748-L762) —— 说明：构造 `gemm_obj`，并把 `b_transposed` 等四个标志设到对象上。这些标志随后被 `GemmBase.rotate_batch_last` 的 `const_expr` 分支读取，决定 trace 期对 mA/mB/mD/mC 做哪些免费布局重排（如 `b_kn` ⇒ B 以 `(l,k,n)` 跨边界、内核转置成 `(n,k,l)`，省掉调用方的 `.mT` 视图）。

最后是 `cute.compile`：

[quack/gemm_tvm_ffi_utils.py:765-783](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L765-L783) —— 说明：构造 fake stream（`use_tvm_ffi_env_stream=True` 表示运行期真实 stream 来自 TVM FFI 环境），然后 `cute.compile` 把内核对象与 fake 张量送入编译，`options="--enable-tvm-ffi"` 让产物成为一个接受真实张量的 Python 可调用对象。返回值即 `compiled_fn`，会被存进 `_GemmPlan`。

那么 `compile_gemm_kernel` 是被谁调用的？是 `_compile_gemm`——它先造好 fake 张量与 epilogue 参数，再转交过来：

[quack/gemm.py:101-117](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L101-L117) —— 说明：`_compile_gemm` 调 `make_fake_gemm_tensors` 得到 `mA, mB, mD, mC, m, n, k, l`，符号 `m, l` 随后被用来构造 rowvec/colvec 等 epilogue fake 张量。

[quack/gemm.py:194-222](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L194-L222) —— 说明：`_compile_gemm` 最终调 `compile_gemm_kernel`，把 `b_transposed=b_kn` 等映射传过去。注意整个 `_compile_gemm` 被 `@jit_cache` 装饰（见 [quack/gemm.py:48-49](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L48-L49)），编译结果落盘成 `.o`，这是更底一层缓存（u2-l6 / u8-l2）。

#### 4.2.4 代码实践

**实践目标**：理清「编译期开关」与「运行期数据」的边界，能列出每个 SM 分支把哪些参数烘焙进了 cubin。

**操作步骤**：

1. 阅读 [quack/gemm_tvm_ffi_utils.py:738-747](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L738-L747)（SM100/110 分支）。
2. 列出 `partial(GemmCls, ...)` 里烘焙的编译期量：`use_clc_persistence`、`use_tma_gather`、`sf_vec_size`、`a_mma_dtype`、`b_mma_dtype`、以及 `split_k_kwargs`（含 `split_k`、`split_k_mode`、`transform_a`）。
3. 对比 [quack/gemm_tvm_ffi_utils.py:748-755](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L748-L755) 的实例化参数：`Float32`（累加器 dtype）、`a_dtype`、`tile_shape_mn`、`cluster_shape_mnk`、`gather_A`、`concat_layout`。
4. 问自己：`(M, N, K)` 具体数值在哪一步进入？答案是——**它们不进入编译**，只以符号 `sym_int` 形式出现在 fake 张量里，运行期才从真实张量读出。

**需要观察的现象**：SM100 分支没有 `pingpong`（u4-l2 已讲：Blackwell 的 tcgen05 MMA 把累加器放进 TMEM，MMA↔epilogue 重叠由硬件原生提供，故无需软件 pingpong），但多了 `use_clc_persistence` 与 `use_tma_gather` 两个 Blackwell 专属调优旋钮。

**预期结果**：你能口头复述「SM100 把 use_clc/use_tma_gather/sf_vec_size 烘焙进 partial，把 tile/cluster/dtype 传进构造器，把形状留给符号」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `compile_gemm_kernel` 用 `functools.partial` 把配置绑定到**类**，而不是直接在实例上设属性？

**参考答案**：因为这些配置（pingpong、use_clc 等）是内核类**构造器**的参数，必须在实例化前就确定，partial 正是把它们提前绑定到构造器。而 `b_transposed` 等是实例属性（内核对象已有默认值 `False`，只是被覆盖），用于 trace 期的 `const_expr` 分支，所以直接 `gemm_obj.b_transposed = ...` 赋值即可。

**练习 2**：`cute.compile` 的最后两个参数 `*sf_args`（即 `mSFA, mSFB`）在非 blockscaled 时是什么？为什么仍然要传？

**参考答案**：非 blockscaled 时是 `(None, None)`。仍要传是因为内核类的签名**统一**地声明了尾部 `(SFA, SFB)`，编译出的 arg spec 把完整 arity（含默认）烘焙进去；启动时也按同样顺序传，保持跨 SM 的签名一致性（见 [launch_gemm](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L497-L505)）。

---

### 4.3 launch_gemm 与 scalar_mode：分层绑定与 per-call 启动

#### 4.3.1 概念说明

编译产物 `compiled_fn` 是一份定死的 cubin，但每次调用 GEMM 时，数据指针、alpha/beta 数值、stream 都在变。如何在不重新编译的前提下，把这些「per-call」的东西高效送进去？

QuACK 的答案是**三层绑定（three binding tiers）**设计——这是 [quack/gemm_tvm_ffi_utils.py:357-425](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L357-L425) 那段长注释的核心思想，也是整个主机侧架构的「总纲」。一句话：**关于一次 GEMM 调用的每一个事实，都在「能知道它的最早一层」处理掉**。

| 层 | 时机 | 处理什么 | 缓存载体 |
|----|------|----------|----------|
| **编译期** | trace → TVM-FFI 函数 | dtype、rank、major、tile/cluster、epilogue 结构、所有静态布局重排 | `@jit_cache` 的 `.o` 文件 |
| **计划期** | 每个 metadata 签名首次调用 | 校验断言、major/dtype 推导、config 选择、workspace/output 配方、静态参数模板、**选哪个 compiled_fn** | `_gemm_plan_cache` 字典（`_GemmPlan`） |
| **调用期** | 每次调用 | 数据指针、标量数值、stream | 无缓存，直接 FFI |

本模块的主角是**调用期**的两个工具：`tensor_key`（构造计划期缓存的键）与 `scalar_mode` / `scalar_arg`（处理 alpha/beta 等标量的「模式」），以及 `launch_gemm`（真正调用 `compiled_fn`）。

`scalar_mode` 解决一个微妙问题：alpha/beta 有**三种存在形式**，对应**三种结构不同的 epilogue**：

- **缺省（neutral，如 alpha=1.0）**：epilogue 里这个标量 op 可以整个**编译掉**。
- **主机常量（Python float）**：编译时这个 op 存在，但数值在每次启动时从主机传入。
- **设备指针（torch.Tensor）**：编译时这个 op 会从 gmem 读指针所指的值（用于数据相关的标量，如 per-tensor scale）。

因为三种形式选出三种**结构不同**的 epilogue，**模式**（而非数值）必须进入编译/计划键；而**数值**留在每次调用。

#### 4.3.2 核心流程

标量从用户输入到 FFI 的完整链路（以 alpha 为例）：

```
用户: alpha=1.0  ──scalar_mode──▶  mode=0 (absent)
用户: alpha=2.0  ──scalar_mode──▶  mode=1 (host const)
用户: alpha=Tensor──scalar_mode──▶  mode=2 (device ptr)

       │ mode 进入计划键（gemm() 的 key）与编译键（_compile_gemm 的 alpha_mode）
       ▼
编译期: fake_scalar(mode) ──▶ mode 0 ⇒ None（op 编译掉）
                             mode 1 ⇒ Float32(1.0) 样例
                             mode 2 ⇒ make_ptr(...) 样例（设备指针占位）
       │
       ▼ （产出的 compiled_fn 只接受与 mode 匹配的实参形态）
调用期: scalar_arg(alpha, mode) ──▶ mode 0 ⇒ None
                                  mode 1 ⇒ Float32(alpha)（真值）
                                  mode 2 ⇒ alpha.data_ptr()（真指针）
       │
       ▼
launch_gemm ──▶ plan.compiled_fn(A, B, D, C, epi_args, scheduler_args, varlen_args, SFA, SFB)
```

而 `tensor_key` 则负责「计划期」的缓存键：它抽取一个张量的 `(dtype, shape, stride)`，**唯独不含数据指针**——因为同一份计划要复用于「形状/布局相同、数据不同」的调用。命中计划缓存意味着「这是一次先前已校验过的调用的重放，只换了数据指针」。

#### 4.3.3 源码精读

先看三个最核心的小函数，它们各自不到十行：

[quack/gemm_tvm_ffi_utils.py:428-431](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L428-L431) —— `tensor_key(t)`：返回 `(dtype, shape, stride)`，不含 data_ptr。说明：这是计划缓存的最小元数据键；shape/stride 已经把 major 与校验断言都「吞」进去了。

[quack/gemm_tvm_ffi_utils.py:434-438](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L434-L438) —— `scalar_mode(scalar, neutral=1.0)`：返回 0/1/2。说明：`isinstance(scalar, torch.Tensor)` ⇒ 2（设备指针）；否则若 `scalar != neutral` ⇒ 1（主机常量）；否则 0（缺省/中性值）。中性值默认 1.0（乘法单位元），故 alpha/beta 默认 1.0 时为 0。

[quack/gemm_tvm_ffi_utils.py:441-449](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L441-L449) —— `scalar_arg(scalar, mode, dtype=Float32)`：返回 per-call 实参。说明：mode 0 ⇒ None；mode 1 ⇒ `dtype(scalar)`（真值）；mode 2 ⇒ `scalar.data_ptr()`（真指针）。注意 mode 2 用的是 `data_ptr()` 而非张量本身——FFI 边界只关心地址。

再看 `launch_gemm`：

[quack/gemm_tvm_ffi_utils.py:497-505](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L497-L505) —— `launch_gemm`：调用 `plan.compiled_fn(A, B, D, C, epi_args, scheduler_args, varlen_args, SFA, SFB)`。说明：这是真正的 FFI 调用，所有 per-call 张量在此刻送入。blockscaled 时先做一次 TMA 解包对齐校验（`_validate_tma_unpack_operands`），因为计划缓存不按指针 key，需在每次启动复核地址契约。

现在追踪 mode 如何贯穿「计划键 → 编译键 → 启动实参」三层。先看计划键：

[quack/gemm.py:444-445](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L444-L445) —— 公共入口 `gemm()` 把 `alpha`、`beta` 先转成 `alpha_mode`、`beta_mode`。

[quack/gemm.py:459-501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L459-L501) —— 计划缓存键 `key` 的构造。说明：它由每个张量的 `tensor_key(...)` 加上一堆标量开关组成；其中 `alpha_mode`、`beta_mode`、`sr_seed_mode`（[gemm.py:489-491](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L489-L491)）正是标量模式——它们进键，而 alpha/beta 的**数值不进键**。

mode 进入编译键的路径：

[quack/gemm.py:915-958](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L915-L958) —— `_build_gemm_plan` 把 `alpha_mode`、`beta_mode`（[gemm.py:932-933](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L932-L933)）作为参数传给 `_compile_gemm`。说明：mode 一路下传到编译层，决定 fake epilogue 的结构。

编译层如何把 mode 转成 fake 样例：

[quack/gemm.py:125-131](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L125-L131) —— 内嵌的 `fake_scalar(mode, dtype)`：mode 0 ⇒ None；mode 1 ⇒ `dtype(1.0)` 样例；mode 2 ⇒ `make_ptr(...)`（gmem 指针占位）。说明：这与 `scalar_arg` 完全对称——编译期的 fake 形态与运行期的真值形态必须严格匹配，否则 FFI 绑定报错。

最后看启动层如何用 `scalar_arg` 还原真值：

[quack/gemm.py:630-649](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L630-L649) —— `run_gemm_plan` 用 `plan.epi_static`（全中性时的静态模板）或现场构造 `EpilogueArguments`，其中 `alpha=scalar_arg(alpha, plan.alpha_mode)`。说明：真值此刻才注入；`plan.epi_static` 是当 alpha/beta/sr 全为 mode 0 且无 split-K/SFD 时的优化——直接复用一个无 per-call 值的静态 NamedTuple，连构造都省了。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：跟踪 `scalar_mode` 如何把 alpha/beta 的三种形式（缺省 / 标量 / 指针）编码进编译键，并亲手用纯 Python 复现这套编码。

**操作步骤**：

1. 阅读 [quack/gemm.py:444-445](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L444-L445) 与 [quack/gemm.py:489-491](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L489-L491)，确认 `alpha_mode`、`beta_mode` 同时进入「计划键」与「编译键」（经 `_build_gemm_plan` → `_compile_gemm`）。
2. 对照 [quack/gemm.py:125-131](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L125-L131) 的 `fake_scalar` 与 [quack/gemm_tvm_ffi_utils.py:441-449](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L441-L449) 的 `scalar_arg`，体会「编译期样例形态 ≡ 运行期真值形态」。
3. 运行下面的示例代码（**纯 CPU、无需 GPU / cutlass-dsl**，因为 `scalar_mode` 只做 `isinstance` 判断）：

```python
# 示例代码：复现 alpha/beta 的三态编码（不调用任何 GPU 内核）
import torch
from quack.gemm_tvm_ffi_utils import scalar_mode, scalar_arg
from cutlass import Float32

cases = [
    ("缺省 1.0",   1.0),
    ("主机常量 2.0", 2.0),
    ("设备指针",    torch.tensor([3.0], device="cpu")),
]
for name, val in cases:
    mode = scalar_mode(val)
    arg  = scalar_arg(val, mode, dtype=Float32)
    print(f"{name:12s} => mode={mode}, per-call arg={arg!r}")
```

**需要观察的现象**：三行分别得到 `mode=0/1/2`；对应 per-call arg 分别为 `None`、`Float32(2.0)`、一个整数（`data_ptr()` 返回的地址）。

**预期结果**：

```
缺省 1.0      => mode=0, per-call arg=None
主机常量 2.0   => mode=1, per-call arg=2.0
设备指针      => mode=2, per-call arg=<某个 int 地址>
```

> 说明：`Float32(2.0)` 的 repr 可能显示为 `2.0` 或带类型标记，视 cutlass 版本而定；mode 2 的具体地址每次运行不同。这三行的**模式**（0/1/2）是确定的。

**待本地验证**：若你的环境装了 cutlass-dsl，可直接 `python -c "..."` 运行；若只装了 PyTorch 而无 cutlass，`scalar_mode` 仍可独立运行（它只依赖 `torch.Tensor` 的 `isinstance`），但 `scalar_arg` 的 mode 1 会依赖 `Float32` 构造器——届时可把 `dtype=Float32` 换成 `dtype=float` 观察同样的 mode 编码逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tensor_key` 不包含数据指针，而 `scalar_mode` 不包含数值？

**参考答案**：两者动机相同——**把「结构」缓存、把「数据」留运行期**。`tensor_key` 不含指针，使「形状/布局相同但数据不同」的调用共享同一计划；`scalar_mode` 不含数值，使「同一标量形态但不同数值」（如两次都是 host 常量、值不同）共享同一编译产物。若把数值/指针放进键，缓存命中率会暴跌，每次都要重编译。

**练习 2**：设 `alpha` 默认 `1.0`，用户调用时没传 alpha。此时 `alpha_mode` 是多少？对应的 epilogue 会怎样？

**参考答案**：`scalar_mode(1.0)` = 0（absent）。`fake_scalar(0)` 返回 None，于是 epilogue 里的 alpha 标量 op 被**编译掉**（不生成任何代码）。这正是「中性值 ⇒ op 消失」的优化——既省指令又省一次 FFI 参数。

**练习 3**：`plan.epi_static` 在什么条件下非 None？它省掉了什么？

**参考答案**：当 `alpha_mode == beta_mode == sr_seed_mode == 0` 且无 rowvec/colvec、无（非 staged 的）split-K、无 SFD/SFDCol 时（见 [gemm.py:970-989](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L970-L989)），`epi_static` 是一个全 None 的静态 `EpilogueArguments`。它省掉了每次调用都构造一个 NamedTuple 的开销——热路径直接复用这个不可变模板。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画一张「从用户调用 `gemm(...)` 到 `launch_gemm` 触发 FFI」的完整数据流图，并用一段示例代码验证你对「三层绑定」的理解。

**要求**：

1. **画数据流图**（文字版即可），标注每一层处理的事实与缓存载体。参考答案骨架：

   ```
   用户 gemm(A,B,D,..., alpha=2.0, tile_M=128, ...)
     │
     ├─[计划期] key = (tensor_key(A), tensor_key(B), ..., alpha_mode=1, beta_mode=0, tile_M, ...)
     │           命中? → _GemmPlan ；未命中 → _build_gemm_plan
     │                                                │
     │                              ┌───────────────────┘
     │                              ▼
     │           [编译期] _compile_gemm(alpha_mode=1, ...)
     │                    ├ make_fake_gemm_tensors: m,n,k,l 全 sym_int
     │                    ├ epi_args.alpha = fake_scalar(1) = Float32(1.0)
     │                    └ compile_gemm_kernel → cute.compile → compiled_fn  (@jit_cache .o)
     │
     └─[调用期] run_gemm_plan
                ├ epi_args.alpha = scalar_arg(2.0, mode=1) = Float32(2.0)  ← 真值此刻注入
                └ launch_gemm → plan.compiled_fn(A,B,D,C, epi_args, ...)   ← FFI
   ```

2. **运行下面示例代码**（纯 CPU），观察「同一形状、不同 alpha 数值」时，计划缓存是否复用（即 `compiled_fn` 身份是否相同）。这验证了「数值不进键」：

```python
# 示例代码：验证计划缓存按模式而非数值复用（仅验证键构造逻辑，不启动内核）
from quack.gemm_tvm_ffi_utils import scalar_mode

# 两次调用：形状相同、alpha 都是 host 常量但数值不同
keyA = (("bf16", (128, 128), (128, 1)), 128, 128, scalar_mode(2.0))   # alpha=2.0
keyB = (("bf16", (128, 128), (128, 1)), 128, 128, scalar_mode(0.5))   # alpha=0.5
print("两键是否相等（期望 True，因为模式都是 1、数值不入键）:", keyA == keyB)

# 与缺省 alpha 对比
keyC = (("bf16", (128, 128), (128, 1)), 128, 128, scalar_mode(1.0))   # alpha=1.0 ⇒ mode 0
print("与缺省键是否相等（期望 False，因为模式 1≠0）:", keyA == keyC)
```

**预期结果**：第一行 `True`（同模式、数值不入键 ⇒ 复用计划与 cubin）；第二行 `False`（host 常量 vs 缺省 ⇒ 结构不同的 epilogue ⇒ 不同 cubin）。

**待本地验证**：上述只演示了键的相等性逻辑；要真正确认 `compiled_fn` 复用，需在带 GPU + cutlass-dsl 的环境里连续两次调用 `quack.gemm.gemm(...)`（相同形状、不同 alpha 数值），在两次之间插入断点比较返回的 `plan.compiled_fn` 是否同一对象。

**反思题**：如果 QuACK 把 alpha 的**数值**也放进编译键，会带来什么后果？

> 参考答案：每个不同的 alpha 数值都会触发一次完整编译（约数百 ms），缓存键空间爆炸，热路径不复存在。这正是「数值留运行期」设计的全部意义。

## 6. 本讲小结

- `make_fake_gemm_tensors` 用 `cute.sym_int()` 把 M/N/K/batch 表达为**运行期符号维度**，并用 `div_for_dtype`（128 位对齐折算）为连续维声明 divisibility，使一份 cubin 复用于整族形状；fake 张量统一「batch 在前 (l,x,y)」，由内核 `rotate_batch_last` 在 trace 期免费重排。
- `compile_gemm_kernel` 按 `device_capacity[0]` 用 `functools.partial` 把 pingpong / use_clc / sf_vec_size 等**编译期开关**烘焙进内核类，实例化后用 fake 张量调 `cute.compile(..., options="--enable-tvm-ffi")` 产出可反复调用的 `compiled_fn`。
- QuACK 的主机侧遵循**三层绑定**总纲：编译期定结构（落盘 `.o`）、计划期定路由与静态模板（`_GemmPlan` 字典）、调用期只换数据指针与标量值。
- `scalar_mode` 把 alpha/beta 的三种形式（缺省 0 / 主机常量 1 / 设备指针 2）编码进**编译键与计划键**，因为不同模式对应结构不同的 epilogue；而 `scalar_arg` 在调用期注入真值，与编译期的 `fake_scalar` 形态严格对称。
- `tensor_key` 抽取 `(dtype, shape, stride)` **不含数据指针**，使「同形状不同数据」的调用共享计划；`launch_gemm` 是真正的 FFI 调用点，blockscaled 时还会每次复核 TMA 解包对齐。

## 7. 下一步学习建议

- **向上一层（公共 API）**：阅读 u4-l3（公共 GEMM API 表面），看 `gemm_interface.py` 如何在 `gemm` / `gemm_act` / `gemm_gated` 之上再加一层 `_GemmIfacePlan` 计划缓存，并理解「计划按引用组合」——外层持有一个已解析的 `gemm` 计划，热路径只付一次 key。
- **向下一层（设备侧）**：进入 u5-l1（GemmBase 共享主循环），看 `rotate_batch_last` / `permute_batch_last` 如何在设备侧消费本讲构造的 batch-first 张量，以及 mainloop 与 epilogue 驱动如何编排。
- **深挖缓存**：本讲的 `compiled_fn` 由 `@jit_cache` 落盘成 `.o`，其内存/磁盘两级缓存、文件锁与源码指纹留待 u8-l2（`.o` JIT 缓存与异步编译池）深入；异步编译池 `--async-compile=N` 让冷编译与测试重叠。
- **配套概念**：`split_k` 如何通过 `_split_k_buffers` 在计划期分配 per-call 的 semaphore/workspace、并在 epilogue args 里以 `scalar_arg` 之外的张量形式送入，可结合 u8-l3（Split-K 归约）与 [quack/gemm.py:292-327](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L292-L327) 一并阅读。
