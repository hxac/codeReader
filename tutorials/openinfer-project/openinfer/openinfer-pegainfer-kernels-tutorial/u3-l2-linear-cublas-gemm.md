# 线性层与 cuBLAS：GEMM、batch 不变性与 algo store

> 讲义 id：u3-l2 ｜ 层级：intermediate ｜ 前置：u3-l1（ops.rs 分发与算子包装解剖）、u2-l2（常量泛型类型层）

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 pegainfer-kernels 线性层算子族的「两代入口 + 一个统一分发点」结构，并独立跟踪任意一条 GEMM 调用链从 Rust wrapper 到 CUDA 入口的全过程。
2. 解释 graph-safe（免 workspace）cuBLAS 句柄与 prefill 句柄的分工，以及为什么 decode 路径与 CUDA Graph 捕获必须走前者。
3. 说出 `gemm_per_token` 作为「N=1 参考实现（oracle）」的用途：它把每一行都送进与 decode 完全相同的 cuBLAS N=1 边界，是逐行精度对拍的基准。
4. 读懂 `gemm_lt_tune` 的启动期调优流程，以及 `PEGAINFER_GEMM_LT_CACHE` 离线 algo store 的记录格式、键指纹与命中条件。
5. 理解 batch 不变性问题——cuBLAS 按 N 选择 tile/split-K，导致同一 token 的 bf16 输出随它所在批的大小漂移——以及 `NumericPolicy` 三态策略与三臂门禁测试如何锁住它。

## 2. 前置知识

**GEMM 与 BLAS 记号。** 线性层就是矩阵乘 \( Y[M,N] = W[M,K] \cdot X[K,N] \)：M 是输出通道数（权重行数），K 是输入通道数（权重列数），N 是这一步要算的 token 数（prefill 是整段 prompt 长度，decode 是当前批的活跃请求数）。cuBLAS 是列主序世界：本 crate 的权重 `DeviceMatrix` 是行主序 `[M, K]`，在 cuBLAS 眼里等价于一个 `[K, M]` 的列主序矩阵按 `OP_T`（转置）参与运算；而 `HiddenStates` 里「token i 连续存于偏移 i×hidden_dim」的布局（u2-l1 讲过），恰好就是 `[hidden_dim, seq_len]` 的列主序矩阵——所以整条链零转置、零拷贝直通 GEMM。

**两个 cuBLAS 家族。** `cublasGemmEx` 是经典入口：句柄内部按启发式选 kernel，调用方不指定算法。`cublasLtMatmul` 是高级入口：算法（`cublasLtMatmulAlgo_t`）可以显式选择、逐形状调优，但部分算法（尤其 split-K）需要一块额外的 **workspace** 显存放部分和，还要预先创建 matmul 描述符和三个矩阵布局描述符。

**句柄与线程。** cuBLAS 句柄绑定 CUDA 上下文。本 crate 为 TP 多 rank 多线程设计了 thread-local 句柄（见 4.2），每个 rank 线程各持一套，避免在进程级单例上竞争。

**CUDA Graph 约束（预告 u4-l3）。** CUDA Graph 把一段 launch 序列录下来反复回放。capture 期间不能有隐式同步或依赖运行时决策的行为；一个在 capture 前从未执行过的 kernel/算法组合直接进图是不安全的。本讲的 graph-safe 句柄、调优热身（warm）都服务于此。

**bf16 输入、f32 累加。** 所有共享 GEMM 都以 `CUDA_R_16BF` 存储输入输出，用 `CUBLAS_COMPUTE_32F` 累加。bf16 → f32 的转换是无损的，这决定了 4.5 的对拍可以用**位级（bitwise）相等**而不是容差。

**batch 不变性问题。** cuBLAS 为不同 N 选不同 tile 划分与 split-K 方案。split-K 的归约顺序不同，f32 部分和的舍入路径就不同——于是**同一个 token 的 bf16 输出会随它所在批的大小变化**。对多数服务这无所谓，但对「把 decode 逐 token 结果与批量 prefill 结果对拍」的精度门禁是灾难：对拍永远对不齐。本讲的一半篇幅都在讲这个问题的两套解法（Pin 与 PerToken）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/ops/linear.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs)（962 行） | 未类型化（运行时维度）线性层包装层 + `NumericPolicy` 策略机 + lt tune/pin 包装，是本讲主战场 |
| [src/typed_ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs) | 常量泛型 typed GEMM（`gemm_into` / `gemm_graphsafe_into` / 私有 `launch_gemm`），承接 u2-l2 |
| [src/ffi/shared.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs) | Rust 侧 `unsafe extern` 声明：`gemm_cuda`、`gemm_graphsafe_cuda`、`gemm_lt_*`、store 两函数 |
| [csrc/shared/linear.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu)（977 行） | CUDA 侧全部实现：双句柄管理、`LtGemmPlan`、调优计时、pin、per-token 循环 |
| [csrc/shared/lt_algo_store.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu)（155 行） | 离线 algo store 的记录格式与 `lookup`/`append`（纯 CPU，不懂 cublasLt） |
| [tests/batch_invariance_gemm.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs) | batch 不变性三臂门禁测试（需要 GPU，无卡自动跳过） |
| [tests/lt_algo_store.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/lt_algo_store.rs) | store 的 torn-write / last-wins 容错测试（纯 CPU 可跑） |
| [KERNELS.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md) | op 路由表：`shared.linear.gemm_per_token`、`qwen3_4b.linear.gemm*`、`kimi_k2.linear.dense_bf16` 等登记 |

## 4. 核心概念与源码讲解

### 4.1 线性层算子族全景：两代入口与一个统一分发点

#### 4.1.1 概念说明

承接 u3-l1 的「三代 wrapper 形态」：`src/ops/linear.rs` 是**运行时维度**一代（`DeviceMatrix` + `HiddenStates`，维度是运行时字段），`src/typed_ops.rs` 是**常量泛型**一代（`GpuWeight<OUT, IN>` + `GpuTensor<IN>`，维度在编译期锁定，承接 u2-l2）。两代入口最终都汇到 FFI 层的同一组 CUDA 符号上。

`linear.rs` 内部是一条清晰的委托链：便捷函数（分配输出）→ checked 函数（校验形状）→ with_policy 函数（补 `graphsafe` 标志）→ 私有 `launch_gemm`（唯一的策略分发点）→ FFI。

#### 4.1.2 核心流程

```text
gemm() / gemm_into()            # 便捷层：分配 out，或 .expect() 收口
  └─ gemm_into_checked()        # 形状校验层
      └─ gemm_into_with_policy() / gemm_ref_into_with_policy()
          # 三重 assert：weight.cols == x.hidden_dim
          #           out.hidden_dim == weight.rows
          #           out.seq_len == x.seq_len
          └─ launch_gemm(w, x, y, m=weight.rows, n=x.seq_len, k=weight.cols, graphsafe, ctx)
              ├─ NumericPolicy::Pin      → ffi::gemm_lt_pin_cuda
              ├─ NumericPolicy::PerToken → ffi::gemm_per_token_cuda
              └─ Tuned → (先试 lt，未调优则按 graphsafe 二选一)
                  ├─ ffi::gemm_graphsafe_cuda   # 免 workspace 句柄
                  └─ ffi::gemm_cuda             # prefill 句柄
```

typed 一代结构相同但没有策略分发：`gemm_into<>` / `gemm_graphsafe_into<>` → typed 私有 `launch_gemm` → 直接 `ffi::gemm_graphsafe_cuda` 或 `ffi::gemm_cuda`。

#### 4.1.3 源码精读

**（1）未类型化便捷层。** [src/ops/linear.rs:530-534](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L530-L534) 是最外层的 `gemm`：分配 `out` 后委托给 `gemm_into_checked`，返回新 `HiddenStates`。

**（2）校验与委托。** [src/ops/linear.rs:561-570](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L561-L570) 的 `gemm_into_checked` 文档写明默认策略：「单 token decode 走 graph-safe 句柄，prefill/批量投影走带 workspace 的句柄」，实现只是转手给 `gemm_into_with_policy`。

**（3）with_policy 层。** [src/ops/linear.rs:695-732](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L695-L732) 的 `gemm_ref_into_with_policy` 做三重 `assert_eq!`（702-716 行），取三根设备指针（守卫保活），然后在 722-731 行以 `m=weight.rows, n=x.seq_len, k=weight.cols` 调 `launch_gemm`——注意 M/N/K 的来源：**N 就是 token 数**。

**（4）typed 一代入口。** [src/typed_ops.rs:21-47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L21-L47) 的 `gemm_into<OUT, IN>`：文档注释一行写清形状约定 `Y = W @ X，X:[IN,bs]，Y:[OUT,bs]`；44 行的 `x.seq_len == 1` 是关键——**typed 路径只有在单 token（decode 步）时才启用 graph-safe 句柄**。[src/typed_ops.rs:49-75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L49-L75) 的 `gemm_graphsafe_into` 则无条件传 `true`（72 行），文档一句话：「总是使用免 workspace 的 cuBLAS 句柄」。

**（5）家族速览。** 其余成员按需取用：行子范围投影 [src/ops/linear.rs:415-426](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L415-L426)（`Y = W[row_offset..row_offset+M, :] @ X`，用于从融合 QKV 大矩阵里切 q/k/v 片段）、token 子范围 [src/ops/linear.rs:572-613](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L572-L613)、每头批量 [src/ops/linear.rs:20-30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L20-L30)（`gemm_strided_batched_bf16`，文档点名 MLA 吸收 `q_nope @ W_UK`、`latent @ W_UV` 这类批 = 头数的场景）、更早的 `gemv`/`linear`（502/522 行）与 `gemm_bf16_f32`（179 行）。它们在 [KERNELS.md:26-27](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L26-L27)（qwen3 的 qkv/o/mlp/lm_head）和 [KERNELS.md:62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L62)（kimi-k2 密集层）都有登记。

#### 4.1.4 代码实践

**实践目标**：亲手跟踪一条完整调用链，验证「两代入口 → 统一分发」的结构。

**操作步骤**：

1. 打开 [src/ops/linear.rs:618-634](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L618-L634)，读 `gemm_graphsafe_into_checked` / `gemm_graphsafe_ref_into_checked`——注意 615-617 行文档：「Kimi decode 对活跃批 1..=4 都用它，让 graph 就绪不绑定在 bs=1 这一个条件上」。
2. 沿 `gemm_ref_into_with_policy` → `launch_gemm` → FFI 符号一路跟到 [csrc/shared/linear.cu:453](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L453) 的 `gemm_graphsafe_cuda`。
3. 画一张调用图，标注每一层函数名与所在行号。

**需要观察的现象**：从 wrapper 到 CUDA 入口共几跳、每一跳谁负责形状校验、谁负责策略选择。

**预期结果**：一条 4-5 跳的链，策略决策只出现在 `launch_gemm` 一处。

#### 4.1.5 小练习与答案

**练习 1**：`gemm_rows_into_checked`（[src/ops/linear.rs:430-458](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L430-L458)）里，子矩阵指针 `w_sub` 是怎么算出来的？为什么不需要拷贝？

**答案**：444 行 `w_ptr + (row_offset * weight.cols * size_of::<bf16>())`——行主序权重按行连续，行子范围的起点就是基地址加行偏移×行宽×2 字节，cuBLAS 拿到指针后按给定的 M/K/ld 直接寻址，零拷贝。

**练习 2**：typed 的 `gemm_into`（[src/typed_ops.rs:22-47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L22-L47)）为什么 44 行写 `x.seq_len == 1` 而 `gemm_graphsafe_into` 写死 `true`？

**答案**：前者是「默认策略」入口：decode 单 token 才值得放弃 workspace 换 graph 安全与低延迟，prefill 批量仍走高性能句柄；后者是显式声明「这一步一定要进 graph」的入口，所以无条件 graph-safe。两者最终都汇入同一个 `launch_gemm` 的 `graphsafe: bool` 参数。

**练习 3**：`gemm_token_range_into_checked`（[src/ops/linear.rs:572-613](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L572-L613)）的 N 是多少？

**答案**：`out.seq_len`（610 行 `out.seq_len == 1` 作为 graphsafe 条件可证）：span 长度由输出缓冲的逻辑长度决定，`x.seq_len` 只用来在 590-596 行断言 span 不越界。

### 4.2 graph-safe 与 workspace 的取舍

#### 4.2.1 概念说明

csrc 侧维护**两套 thread-local cuBLAS 句柄**：`g_cublas_prefill_handle`（服务 prefill 批量投影，从不出现在 CUDA Graph 里）与 `g_cublas_handle`（免 workspace 的 decode/graph-safe 句柄）。之所以分两套，一是 decode 要被 CUDA Graph 捕获与回放，带 workspace 的算法路径会引入捕获不友好的行为；二是句柄绑定线程的 CUDA 上下文，TP 各 rank 线程必须各持一套，不能共享进程级单例（[csrc/shared/linear.cu:28-30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L28-L30) 注释）。

除句柄外，cublasLt 还有第三块资源：一个 32MB 的默认 workspace（调优与生产共享）和一个 128MB 的 pin 专属 workspace——后者按架构预留余量，且调优时的 workspace 偏好上限故意压到 64MB：cuBLASLt 看到更大的预算就挑更大 workspace 的算法，64MB 偏好强制选出小 workspace 算法，让 128MB 缓冲始终留有余量（[csrc/shared/linear.cu:62-67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L62-L67)）。

#### 4.2.2 核心流程

`launch_gemm`（Tuned 策略）的完整决策树：

```text
launch_gemm(w, x, y, m, n, k, graphsafe, ctx)              # linear.rs:887
  graphsafe = graphsafe 且 无 prefill stream override          # 905 行
  若 n <= GEMM_LT_MAX_N(=32) 且 无 stream override:
      status = ffi::gemm_lt_cuda(...)                       # 914-923 行：先试调优过的 cublasLt
  若 status == GEMM_LT_UNTUNED:                             # 927 行：本线程没调优过该形状
      graphsafe ? ffi::gemm_graphsafe_cuda(...)             # 免 workspace 句柄
                : ffi::gemm_cuda(...)                       # prefill 句柄
  status != 0 → anyhow::bail!("GEMM failed: ...")           # 950-958 行
```

typed 一代的 `launch_gemm`（[src/typed_ops.rs:465-506](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L465-L506)）没有 lt 尝试也没有策略分发，直接按 `graphsafe` 二选一，并在 475 行先调 `ensure_tuned_policy` 拒绝非 Tuned 策略——typed GEMM 不在批不变 pin 的覆盖范围内（[src/ops/linear.rs:766](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L766) 注释点名：只有 qwen3 会设 Pin/PerToken）。

#### 4.2.3 源码精读

**（1）双句柄声明。** [csrc/shared/linear.cu:28-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L28-L33)：`g_cublas_handle`、`g_cublas_prefill_handle`、按设备缓存的 `g_cublas_handles_by_device` 全部 thread-local，注释写明动机是「让每个 TP rank 线程把句柄绑到自己的 CUDA 上下文，不在进程级单例上竞争」。

**（2）prefill 句柄入口。** [csrc/shared/linear.cu:423-449](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L423-L449) 的 `gemm_cuda`：文档三行讲完分工——「W 行主序 [M,K]，X 列主序 [K,N]，Y 列主序 [M,N]；N=1 等价 GEMV，N>1 支持批量 prefill；**用 prefill 句柄——只在 prefill 路径调用，绝不在 CUDA Graph 下**」。437-444 行是一次标准的 `cublasGemmEx(OP_T, OP_N, M, N, K, …, CUDA_R_16BF, CUBLAS_COMPUTE_32F, CUBLAS_GEMM_DEFAULT_TENSOR_OP)`。

**（3）graph-safe 入口。** [csrc/shared/linear.cu:451-476](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L451-L476) 的 `gemm_graphsafe_cuda`：「与 gemm_cuda 同样的数学，但用免 workspace 句柄，CUDA Graph 捕获与 decode 路径安全」。调用形态与（2）逐参数相同，只换句柄。

**（4）lt 路径为什么 graph 安全。** [csrc/shared/linear.cu:565-592](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L565-L592) 的 `gemm_lt_cuda`：按 `{M,N,K}` 查本线程 plan 表（574 行），查不到返回 `GEMM_LT_UNTUNED`；注释给出 graph 安全的理由——**被调优选中的 kernel 在调优时已经真实执行过**，所以在 CUDA Graph 里回放它是安全的。为此 [csrc/shared/linear.cu:259-290](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L259-L290) 的 `lt_algo_warm` 会在算法「发布」前用零填充缓冲真实跑一遍并同步。

**（5）stream override 下的双重保险。** [src/ops/linear.rs:907-914](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L907-L914)：注释记录了一个真实教训——「gemm_lt 在 stream override 激活时禁用（SM 分区的并发模型里，两条 green-ctx 流并发跑 cublasLtMatmul 会触发 Xid 31）」，所以 914 行的条件同时要求 `n <= GEMM_LT_MAX_N` **且**无 stream override；905 行还把 graphsafe 与 `has_prefill_stream_override` 相与后再用。

**（6）workspace 懒初始化。** [csrc/shared/linear.cu:111-128](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L111-L128) 的 `ensure_lt_resources`：cublasLt 句柄与 32MB workspace 懒创建，且只在调优入口做（604-605 行注释：「Lt 资源在这里创建而不是 cublas_init，只有真正调优的线程——模型 executor 线程——才付这 32MB」）。128MB pin workspace 在 [csrc/shared/linear.cu:130-139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L130-L139) 单独懒分配、首次 Pin 使用时才落地。

#### 4.2.4 代码实践

**实践目标**：盘点所有 graphsafe 决策点，理解「谁在什么条件下选哪个句柄」。

**操作步骤**：

1. 在 crate 根目录执行 `grep -n "seq_len == 1" src/ops/linear.rs src/typed_ops.rs`——应当找到 [src/ops/linear.rs:455](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L455)（rows 家族）、[src/ops/linear.rs:610](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L610)（token range）、[src/typed_ops.rs:44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L44)（typed gemm_into）与 [src/typed_ops.rs:537](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L537)（runtime-out 的 `graphsafe || x.seq_len == 1`）。
2. 再执行 `grep -n "has_stream_override\|has_prefill_stream_override" src/ops/linear.rs`，找到运行期否决点。
3. 对照 [csrc/shared/linear.cu:425](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L425) 与 [csrc/shared/linear.cu:451-452](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L451-L452) 两条入口注释。

**需要观察的现象**：graphsafe 判定散布在多少个调用点、几处写死、几处按 seq_len 推导、几处运行期否决。

**预期结果**：三类判定各至少一处，全部汇聚到两个 FFI 入口。

#### 4.2.5 小练习与答案

**练习 1**：`gemm_lt_cuda` 查不到 plan 时为什么返回 `GEMM_LT_UNTUNED` 而不是报错？

**答案**：这是「尽力而为」的快路径：该形状没调优过就退回既有 `cublasGemmEx` 路径（[src/ops/linear.rs:927-949](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L927-L949)），保持捕获行为与不开调优时一致；若报错，可选优化就变成了必选依赖。

**练习 2**：`GEMM_LT_MAX_N` 为什么是 32？依据在哪？

**答案**：[src/ops/linear.rs:220-224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L220-L224) 注释：GemmEx 启发式在 decode 桶选得差（最差在 N∈[8,16]，完全跳过 split-K），32 覆盖这些桶；再宽的 GEMM 默认选择已经够好。

**练习 3**：Xid 31 的教训是什么？代码在哪防它？

**答案**：两条 green-ctx 流并发跑 cublasLtMatmul 触发 GPU 硬件错误。防线在 [src/ops/linear.rs:914](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L914)（有 stream override 不走 lt）；Pin 策略下更是直接 bail（见 4.5）。

### 4.3 gemm_per_token：N=1 参考路径

#### 4.3.1 概念说明

`gemm_per_token` 是**逐行 oracle**：把批量为 N 的 GEMM 拆成 N 次独立的 `M×1×K` GEMM，每次只算一个 token。因为每一行都通过与 decode 单 token 步完全相同的 cuBLAS N=1 边界，它的结果**按构造对 N 不变**（N=1 时不存在「随 N 变化的算法选择」）。文档（[src/ops/linear.rs:536-538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L536-L538)）写明用途：「适合窄批量形状的精度门禁——需要与串行 decode oracle 逐行对拍时」。代价是慢（N 次 launch），所以只做参考、不做生产路径；生产上的批不变性由 4.5 的 Pin 承担，PerToken 策略只在测试里启用。

#### 4.3.2 核心流程

```text
gemm_per_token(ctx, weight, x)                   # linear.rs:539
  out = HiddenStates::zeros(rows, seq_len)
  gemm_per_token_into_checked(...)               # linear.rs:636
    三重断言（cols==hidden_dim、out.hidden==rows、out.seq==x.seq）
    ffi::gemm_per_token_cuda(w, x, y, M, batch=N, K, stream)
      for row in 0..batch:                       # linear.cu:961
        cublasGemmEx(句柄=g_cublas_handle, OP_T, OP_N,
                     M, 1, K,                    # ← 每行都是 N=1
                     X + row*K, Y + row*M)
```

输入寻址 `X + row*K`、输出寻址 `Y + row*M`：列主序下第 row 个 token 的输入列起点是 row×K，输出列起点是 row×M。

#### 4.3.3 源码精读

**（1）Rust 侧。** [src/ops/linear.rs:536-547](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L536-L547) 是便捷入口；[src/ops/linear.rs:636-683](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L636-L683) 的 `gemm_per_token_into_checked` 在 663 行调 `ffi::gemm_per_token_cuda`（声明见 [src/ffi/shared.rs:933](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L933)），失败时 672-680 行以 `per-token GEMM failed: ..., m={}, batch={}, k={}` 上抛。

**（2）CUDA 侧。** [csrc/shared/linear.cu:946-975](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L946-L975)：校验句柄与维度后，961-969 行是一个 `for (int row = 0; row < batch; ++row)` 循环，每行一次 `cublasGemmEx(..., M, 1, K, ..., X + row*K, ..., Y + row*M, ...)`，全程用**免 workspace 的 graph-safe 句柄**——与 decode 同一句柄，这正是「同一边界」的另一半含义。

**（3）登记。** [KERNELS.md:11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L11) 的 `shared.linear.gemm_per_token` 行：「每一行都通过 N=1 decode GEMM 边界计算；在性能优化之前需要逐行 parity 时使用」。

#### 4.3.4 代码实践

**实践目标**：确认「per_token 结果与 token 在批中的位置无关」及其在门禁中的角色。

**操作步骤**：

1. 读 [tests/batch_invariance_gemm.rs:78-106](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L78-L106) 的 `col0`：三个 Arm 各自算 token 0 的输出列，`Arm::PerToken` 分支调 `gemm_per_token`。
2. 读 [tests/batch_invariance_gemm.rs:164-167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L164-L167)：per-token oracle 的 maxΔ 若非 0，测试报「harness bug, not a fix result」。

**需要观察的现象**：断言把 oracle 的零漂移定位为「装置自检」而非「产品指标」——oracle 是**测量基准的基准**。

**预期结果**：理解 oracle 在三臂门禁中的角色（完整运行步骤见 4.5.4）。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 per_token「按构造不变」而 baseline 不是？

**答案**：per_token 每行独立走 N=1 GEMM，cuBLAS 在 N=1 下没有按 N 分派算法的自由度，同一 token 无论排在批内第几位、批多大，都执行完全相同的计算路径；baseline 的批量 GEMM 中 cuBLAS 按 N 选 tile/split-K，同一 token 的舍入路径随批变化。

**练习 2**：per_token 为什么不能当生产路径？

**答案**：N 次 launch、无批内并行，吞吐远差于一次批量 GEMM；它的价值是确定性与可对拍性。生产的批不变性由 Pin（4.5）以单算法覆盖全 N 实现。

**练习 3**：`gemm_per_token_cuda` 用哪个句柄？为什么必须是它？

**答案**：`g_cublas_handle`（graph-safe 句柄，[csrc/shared/linear.cu:949](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L949)）。oracle 的定义就是「与 decode 相同的边界」，decode 用的正是这个句柄，换句柄就不再是对 decode 的忠实参考。

### 4.4 gemm_lt_tune 启动调优与 lt algo store 记录格式

#### 4.4.1 概念说明

小 N decode 投影（N≤32）上，cuBLAS 默认启发式与最优算法之间「有 4-6% 带宽可拿」（[csrc/shared/linear.cu:37-38](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L37-L38)）。`gemm_lt_tune_cuda` 在启动时对每个 `(M,N,K)` 形状把 cublasLt 的全部启发式候选拿来**逐个真实计时**，赢家缓存进本线程 plan 表。三个设计要点：

1. **plan 按线程存、赢家按设备共享**：一个线程测完全设备收养（[csrc/shared/linear.cu:52-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L52-L53) 注释），后来者不重复计时。
2. **调优即热身**：赢家发布前先真实执行一次（`lt_algo_warm`），保证之后在 CUDA Graph 里回放安全（4.2 已读）。
3. **store 是缓存、永远不是事实源**：进程内 plan 表随进程死亡；`PEGAINFER_GEMM_LT_CACHE` 指向的文件让下一个进程免于重复搜索，文件里任何记录失效都只是**安静退回在线搜索**——「改变的是启动时间，永远不是结果」（[csrc/shared/linear.cu:191-195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L191-L195) 注释）。

#### 4.4.2 核心流程

调优入口的完整时序：

```text
gemm_lt_tune_cuda(Ws[], num_ws, M, N, K, stream)          # linear.cu:599
  校验维度；ensure_lt_resources()（懒建 cublasLt 句柄 + 32MB workspace）
  本线程已有 {M,N,K} plan → 直接成功返回
  以 {device, M, N, K} 认领设备级调优槽（TunedClaim RAII，失败擦槽并唤醒等待者）
  ├─ store 命中尝试：lookup → lt_algo_usable(AlgoCheck+workspace≤32MB)
  │    → lt_algo_warm 真跑一遍 → 收养并发布
  │    （键匹配但跑不动 → stderr 打 "stored algo rejected ... retuning"）
  └─ 在线搜索：cublasLtMatmulHeuristic → results[16]
       分配 x[K,N]=0、y[M,N]、begin/end 事件
       for 候选 i in [0, returned):
         for j in [0, warmup=3 + iters):        # Ws[j % num_ws] 轮转权重防 L2
           cublasLtMatmul(..., results[i].algo, workspace, stream)
         取事件耗时，记 best
       plan.algo = results[best].algo
       发布（mutex 写设备级表 + cv 通知）→ 若 store 开启则 append → emplace 本线程 plan 表
```

**离线记录格式**（[csrc/shared/lt_algo_store.cu:1-12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L1-L12) 头注释）：

```text
v1 <key> <w0> <w1> ... <w7>\n
```

- `w0..w7` 是 `cublasLtMatmulAlgo_t` 的不透明内部字节按 8 个 64 位字展开，每字**恰好 16 个十六进制位**；同键多条记录时**最后一条胜出**。
- 键 `<key>` 由 [csrc/shared/linear.cu:209-246](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L209-L246) 生成：GPU 名称（空格换下划线）、SM 版本 major.minor、CUDA runtime 版本、driver 版本、设备属性、workspace 上限，再以字面量 `bf16 TN` 标记 dtype 与转置布局。注释（209-213 行）解释了为什么键要背这么多指纹：algo 不透明、官方不承诺跨版本存活，而进程内键用的设备序号在另一台机器上毫无意义——「ordinal 0 在别的机器上是别的硬件」。
- **撕裂容错**：普通文件的 write 可能写短（PIPE_BUF 原子性只属于管道），所以解析端对残缺尾行「恢复而非判死」（[csrc/shared/lt_algo_store.cu:6-8](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L6-L8)），追加端发现文件末字节不是 `\n` 就先补一个换行再写——**上一条撕裂记录既不能被解析成命中，也不能拖垮它后面那条**。

#### 4.4.3 源码精读

**（1）plan 与资源。** [csrc/shared/linear.cu:37-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L37-L50)：`LtGemmPlan{op,a,b,c,algo}`、thread-local 的 `g_lt_plans`（键 `{M,N,K}`）、`LT_WORKSPACE_SIZE = 32MB`。

**（2）认领与收养。** [csrc/shared/linear.cu:599-643](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L599-L643)：其他线程在 641 行 `g_lt_tuned_cv.wait` 等待；已 ready 的赢家被 630-639 行直接收养进本线程 plan 表。[csrc/shared/linear.cu:644-656](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L644-L656) 的 `TunedClaim` 析构函数保证调优失败也擦槽并唤醒等待者，不留死等。

**（3）store 命中路径。** [csrc/shared/linear.cu:658-697](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L658-L697)：666 行 `pegainfer_lt_store_lookup`；命中后先 `lt_algo_usable`（[csrc/shared/linear.cu:248-257](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L248-L257)——`cublasLtMatmulAlgoCheck` 加 `result.workspaceSize <= 32MB` 双条件）再 warm，都过才收养。键匹配但跑不动时 687-694 行向 stderr 打印「stored algo rejected … retuning」——注释解释了为什么唯独这种失败要出声：静默会让整个陈旧文件看起来像全 miss，出声才能提示「文件是在库/驱动变更之前写的」。

**（4）计时循环。** [csrc/shared/linear.cu:711-766](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L711-L766)：候选上限 16（701 行 `results[16]`）；每候选 warmup 3 次 + `lt_tune_iters(M,K)` 次（734-735 行）；746 行 `Ws[j % num_ws]` 在多个同形权重样本间轮转，避免连续迭代命中同一块 L2，让计时反映冷数据带宽。

**（5）发布与落盘。** [csrc/shared/linear.cu:785-803](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L785-L803)：无候选报 `CUBLAS_STATUS_NOT_SUPPORTED`；赢家写设备级表、`notify_all`、`claim.published = true`，796-800 行 `memcpy(words, &plan.algo.data, 8*8)` 后追加进 store，最后 emplace 本线程 plan 表。

**（6）store 读端。** [csrc/shared/lt_algo_store.cu:63-105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L63-L105) 的 `pegainfer_lt_store_lookup`：逐行读，前缀 `"v1 <key> "` 匹配后交 [25-57 行](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L25-L57) 的 `decode_words` 严格解析——每个十六进制位合法、总长恰好、第八个字之后不允许再有任何内容（55-56 行）；无换行符的尾行按「短写尾巴或超长行」排掉（84-89 行）；同键多条取最后一条。

**（7）store 写端。** [csrc/shared/lt_algo_store.cu:109-155](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L109-L155) 的 `pegainfer_lt_store_append`：109 行注释定调「尽力而为：丢一条记录只是让下次启动多搜一遍，所以失败都安静」；130 行以 `O_RDWR|O_CREAT|O_APPEND` 打开（128-129 行注释：O_RDWR 是为了用 `pread` 看最后一个字节决定是否补 `\n`，O_APPEND 仍决定写入落点）；134-150 行末字节非 `\n` 则先补换行，再单次 `write` 落盘。

**（8）开关与 Rust 包装。** [csrc/shared/linear.cu:201-205](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L201-L205) 的 `lt_store_path()` 静态缓存一次 `std::getenv("PEGAINFER_GEMM_LT_CACHE")`。Rust 侧 [src/ops/linear.rs:236-278](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L236-L278) 的 `gemm_lt_tune` 收一组 `(DeviceMatrix, row_offset)` 样本（249-250 行断言各样本同形且行段不越界，253 行按行偏移算指针），把指针数组递给 `ffi::gemm_lt_tune_cuda`（声明见 [src/ffi/shared.rs:394-401](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L394-L401)）。store 两个函数也在 FFI 层暴露（[src/ffi/shared.rs:403-404](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L403-L404) 注释：因为格式与恢复语义的测试要用）。

#### 4.4.4 代码实践

**实践目标**：亲手验证 store 的文件格式与容错语义（不需 GPU 参与 kernel 执行）。

**操作步骤**：

1. 运行测试：
   ```bash
   cargo test --release -p pegainfer-kernels --test lt_algo_store -- --nocapture
   ```
2. 读 [tests/lt_algo_store.rs:43-107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/lt_algo_store.rs#L43-L107)：它用 `RLIMIT_FSIZE` 让第二次 append **真实地**写短 9 个字节（先 `SIG_IGN` 掉 SIGXFSZ，否则内核直接杀进程，[tests/lt_algo_store.rs:1-6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/lt_algo_store.rs#L1-L6) 注释），然后断言：撕裂记录不以命中解析（76-80 行）、前面的完整记录幸存（81-85 行）、之后的 append 能恢复（87-94 行）、撕裂记录保持不可读（100-104 行）。
3. 读 [tests/lt_algo_store.rs:109-144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/lt_algo_store.rs#L109-L144)：同键两次 append 后一条胜出（117-125 行）；再手工拼出短字段、长字段、尾部多字段的伪记录，断言全被严格宽度拒绝（127-130 行起）。

**需要观察的现象**：两个测试全绿；注意 [tests/lt_algo_store.rs:26-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/lt_algo_store.rs#L26-L41) 用一把进程级互斥锁把两个测试串行——`RLIMIT_FSIZE` 与 SIGXFSZ 是进程级状态，cargo 并行跑测试时会误伤邻居。

**预期结果**：`2 passed`。本测试不执行任何 GPU kernel，但编译本 crate 仍需 CUDA 工具链（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：一条 store 记录为什么必须携带 GPU 名称、CUDA runtime/driver 版本这些指纹？哪一项指纹是**故意不区分**的？

**答案**：`cublasLtMatmulAlgo_t` 不透明，排名取决于硬件与库版本，官方不承诺跨版本存活——别的 GPU/别的 runtime 调出的算法字可能无法运行或不再最优，全对上才可能命中。故意不区分的是 **driver 构建号**：`cudaDriverGetVersion` 返回驱动支持的 CUDA 等级而非构建号，等级不变的驱动升级不被区分（[csrc/shared/linear.cu:223-224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L223-L224) 注释）；想强制重调优就删文件。

**练习 2**：计时循环为什么要 `Ws[j % num_ws]` 轮转权重？

**答案**：每轮都用同一权重指针会让权重驻留 L2，测出的是缓存命中带宽而非真实服务带宽；在多个同形权重样本间轮转，让每次访问接近冷数据（[csrc/shared/linear.cu:746](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L746)）。这也解释了 Rust 侧 `gemm_lt_tune` 为什么收一组样本而不是单个权重。

**练习 3**：append 为什么用 `O_RDWR|O_APPEND` 而不是纯 `O_APPEND`？

**答案**：写前要用 `pread` 读文件最后一个字节，判断上次是否写短、要不要先补 `\n`；写入位置仍由 `O_APPEND` 原子落尾保证（[csrc/shared/lt_algo_store.cu:128-130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L128-L130)）。

### 4.5 batch 不变性：NumericPolicy、Pin 路径与三臂门禁测试

#### 4.5.1 概念说明

**问题重述**。cuBLAS 对每个形状（含 N，即同批 token 数）独立选 tile 形状与 split-K 数量，bf16 输出因此逐位不同：同一个 token 在 N=1 与 N=32 的批里算出的位模式不一样。于是「同一句话的答案」取决于「当时同批还有谁」——回归测试没法比、A/B 实验没法做、复现 bug 无从谈起。

**解法不是更准，而是解耦**。让数值路径只由 (M,K)（权重形状）决定，与批组成 N 无关。`NumericPolicy` 用三档策略表达这个光谱（[src/ops/linear.rs:734-746](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L734-L746) 的文档注释写得很直白）：

| 策略 | 值 | 语义 | 代价 |
|------|---|------|------|
| `Tuned`（默认） | 0 | 生产路径：每个 (M,N,K) 独立调优出最快算法 | N 变则算法变，接受漂移 |
| `Pin` | 1 | 每个 (M,K) 一个 cublasLt 算法服务**所有** N | 放弃按 N 调优的性能；不可服务即 bail，无回退 |
| `PerToken` | 2 | 每个 token 独立走 N=1 边界 | 串行逐行，仅测试/对拍用（oracle） |

三个要点：

1. **进程级原子**：`NumericPolicy` 存在 `AtomicU8` 静态里（[src/ops/linear.rs:748-750](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L748-L750)），不是 thread-local——主线程在 worker 构造前设定，worker 线程读取。
2. **时序约束**：`set_numeric_policy` 必须在 executor 构造 / graph capture 之前调用，「捕获时活着的算法就是回放时用的算法」（[src/ops/linear.rs:752-756](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L752-L756)）。测试也正是经由这个入口驱动三臂。
3. **与 stream override 互斥**：SM 分区并发（`--decode-overlap`）下两条 green-ctx 流并发跑 `cublasLtMatmul`，而 cuBLASLt 有设备级全局状态，历史上直接触发 Xid 31（驱动级错误）。所以 lt 整条路在 stream override 下禁用；Pin 策略更是在 `launch_gemm_pin` 入口就 bail——**`--batch-invariant` 与 `--decode-overlap` 不兼容**。

#### 4.5.2 核心流程

`launch_gemm`（[src/ops/linear.rs:887-958](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L887-L958)）的完整决策树：

```text
launch_gemm(w, x, y, M, N, K, graphsafe, ctx):
  match numeric_policy():
    Pin      -> launch_gemm_pin(...)      # 一个算法服务所有 N；不可服务即 bail
    PerToken -> launch_gemm_pertoken(...) # 逐 token 走 N=1 边界
    Tuned    -> 继续                       # 生产路径，向下
  graphsafe &= !has_prefill_stream_override()   # split-prefill 不许碰 decode 句柄
  status = if N <= GEMM_LT_MAX_N(32) 且无 stream override:
             gemm_lt_cuda(...)                 # 调优表里有就用赢家算法
           else:
             GEMM_LT_UNTUNED                   # 哨兵：不走 lt，落入下方兜底
  if status == GEMM_LT_UNTUNED:
    status = graphsafe ? gemm_graphsafe_cuda(...) : gemm_cuda(...)
  status != 0 -> bail!("GEMM failed: ..., m, n, k, graphsafe")
```

Pin 路径自身的流程：

1. **启动时 warmup**：`gemm_lt_pin_warmup` 对每个 (M,K) 用固定代表形状 N=32（`PIN_REP_N`，[src/ops/linear.rs:796-803](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L796-L803)）调 `gemm_lt_pin_tune_cuda`——**只取启发式 top，不做任何计时**（计时是非确定性的：同形状两次计时可能选出不同算法，不变性又会被调用顺序污染）。CUDA 侧见 [csrc/shared/linear.cu:805-877](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L805-L877)，文档注释即「heuristic top, no timing → deterministic」。
2. **运行时**：`launch_gemm_pin` 查 per-(M,K) 的 pin plan（CUDA 侧按 {M,K} 键控，[csrc/shared/linear.cu:74-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L74-L77)），用同一个算法跑任意 N——B/C 描述符按实际 N 重建（[csrc/shared/linear.cu:885-932](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L885-L932)），所以输出数值与批组成无关。
3. **fail-loud**：没有懒调优、没有 per-token 回退；stream override、未 warmup、特殊调用方（DFlash drafter / LoRA）覆盖不到时直接报错退出（[src/ops/linear.rs:818-821](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L818-L821)、[src/ops/linear.rs:842-846](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L842-L846)）——宁可拒绝启动，也不静默退回漂移路径。

#### 4.5.3 源码精读

**（1）`NumericPolicy` 三态定义。** [src/ops/linear.rs:734-746](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L734-L746)：文档注释写明「进程级原子、非 thread-local——主线程在 worker 构造前设定，worker 上读取」；`Pin = 一个 (M,K) 一个 cublasLt 算法，`launch_gemm_pin` 无法服务某个 N 或处于 stream override 时直接 bail——没有 per-token 回退」；`PerToken = N=1 oracle`。

**（2）设置入口与静态计数器。** [src/ops/linear.rs:748-756](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L748-L756)：`NUMERIC_POLICY: AtomicU8`（初值 `Tuned`）、`PIN_SERVED` / `PER_TOKEN_SERVED` 两个 `AtomicU64` 服务计数；`set_numeric_policy` 的文档点出时序契约——「必须在 executor 构造 / graph capture 之前运行（捕获时活着的算法就是回放用的算法）」，并注明测试用它驱动三臂。

**（3）代表形状 `PIN_REP_N = 32`。** [src/ops/linear.rs:796-803](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L796-L803)：文档注释强调「一个算法服务**所有** N（不是首次调用的那个 N）」——代表形状固定为 32，保证选出的算法与「哪个 N 先来」这种调用顺序细节无关。

**（4）`launch_gemm_pin`。** [src/ops/linear.rs:805-854](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L805-L854)：入口先查 stream override，命中即报「`--batch-invariant` 与 `--decode-overlap` 不兼容」（[L818-821](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L818-L821)）；服务成功后 `PIN_SERVED` 自增（L839 附近）；对未被 pin warmup 覆盖的调用方（DFlash drafter、LoRA 路径）直接报错（[L842-846](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L842-L846)）。

**（5）`launch_gemm_pertoken`。** [src/ops/linear.rs:856-885](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L856-L885)：文档注释一锤定音「N=1-per-column oracle（按构造不变）」，直接转发 4.3 读过的 `ffi::gemm_per_token_cuda`。

**（6）`launch_gemm` 决策本体。** [src/ops/linear.rs:887-926](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L887-L926)：897-901 行按策略三分；903-905 行注释「split 并发 prefill 必须把哪怕 N=1 的投影留在专用句柄上，不许重绑 graph-safe decode 句柄」后收紧 `graphsafe` 标志；907-909 行注释说明「本线程从未调优过的形状报 `GEMM_LT_UNTUNED`，保持原有 cublasGemmEx 路径及其捕获行为不变」；911-913 行记录 Xid 31 教训（两条 green-ctx 流并发 `cublasLtMatmul` 冲突 cuBLASLt 设备级全局状态）；914 行的门槛是 `n <= GEMM_LT_MAX_N && !has_stream_override()`，不满足时返回哨兵 `GEMM_LT_UNTUNED`（L924-926）。

**（7）UNTUNED 兜底与 fail-loud。** [src/ops/linear.rs:927-958](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L927-L958)：哨兵触发时二选一——`graphsafe` 走免 workspace 句柄，否则走 prefill 句柄的 `gemm_cuda`；最终任何非零状态都带着完整形状参数 bail（L950-958），不吞错误。

**（8）三臂门禁测试。** [tests/batch_invariance_gemm.rs:1-6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L1-L6) 定义三臂：**Baseline**（Tuned 默认路径）应漂移、**Pin** 应零漂移、**PerToken** 是自检 oracle；所有比较都是 bf16→f32 无损转换后的**位级**比较。无 CUDA 设备时打印「gate inconclusive」直接返回（[L143-146](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L143-L146)）——门禁缺席不冒充通过。判定的四个断言在 [L156-L181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L156-L181)：

| 断言 | 含义 |
|------|------|
| oracle 若漂移 → 报 harness bug | oracle 按构造不变，它漂移说明测试装置坏了，后面结论全不可信 |
| Pin 零漂移 | 这正是被验证的不变量（修复本身） |
| `pin_ran` 覆盖全部 decode N | 确认 pin 路径真的在这些 N 上执行过，零漂移不是「没跑」 |
| baseline 至少漂移一次 | 阳性对照：若这个 GPU/形状组合连 baseline 都不漂移，说明门禁没踩到 bug 的触发条件，「Pin 零漂移」只是平凡成立 |

#### 4.5.4 代码实践

**实践目标**：跑通 batch 不变性门禁，理解每个断言拦的是什么。

**操作步骤**：

1. 用测试文件 [L8-9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L8-L9) 给出的命令原样运行：
   ```bash
   PEGAINFER_BI_REPS=8 cargo test --release -p pegainfer-kernels --test batch_invariance_gemm -- --nocapture --test-threads=1
   ```
2. 对照 [tests/batch_invariance_gemm.rs:21-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L21-L32)：三个形状是 `down_proj(2560,9728)`（漂移最强，K 大）、`o_proj(2560,2560)`、`qkv_wide(4096,2560)`；N 序列 `[1,2,4,8,16,24,32,48,64]` 覆盖 decode 常见批与 `GEMM_LT_MAX_N=32` 两侧。`PEGAINFER_BI_REPS` 控制重复轮数，抵抗单次计时/调度噪声（[L34-40](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L34-L40)）。

**需要观察的现象**：

- 三臂各自对 col 0（首个 token）的输出如何在 N 间比较：baseline 出现位级差异、pin 全等、per-token 全等；
- 若人为把 GPU 换成无 CUDA 环境，测试打印「skipping ... gate inconclusive」而不是 pass。

**预期结果**：`1 passed`；若任一断言失败，错误信息会指明是哪一臂违反（oracle 漂移 = harness bug；pin 漂移 = 回归）。需要真实 GPU（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：Pin 调优为什么不计时挑最快，而是取启发式 top？

**答案**：不变性优先于最后几个百分点的性能。计时是非确定性的——同一形状两次计时可能选出不同算法，那么「选了哪个算法」又变成了与调用顺序、机器状态相关的变量，不变性目标落空。启发式 top 是确定函数：相同输入永远给相同输出。CUDA 侧文档注释原话就是「heuristic top, no timing → deterministic」（[csrc/shared/linear.cu:805-877](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L805-L877)）。

**练习 2**：为什么门禁里 oracle 臂漂移被定性为「harness bug」，而 baseline 臂不漂移则整场作废？

**答案**：oracle（per-token N=1）的不变性是**构造保证**——每行独立走同一条边界，与批组成无关；它都漂移，只能是测试装置本身（数据生成、指针偏移、比较逻辑）出了问题，此时 pin/baseline 两臂的任何结论都不可信。而 baseline 不漂移是**阳性对照缺失**：说明这组 GPU/形状/批组合根本没触发原始 bug 的条件，「pin 零漂移」退化成平凡成立，证明不了修复有效（[tests/batch_invariance_gemm.rs:164-181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/batch_invariance_gemm.rs#L164-L181)）。

**练习 3**：`launch_gemm` 在 `n > GEMM_LT_MAX_N` 时返回 `GEMM_LT_UNTUNED` 哨兵而非直接调 `gemm_cuda`，为什么多绕这一步？

**答案**：让「是否走 lt」和「最终落到哪个句柄」两个决策解耦。哨兵统一进入 [L927-949](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L927-L949) 的兜底：`graphsafe` 与否决定免 workspace 句柄还是 prefill 句柄；且「本线程没调优过」与「形状超出 lt 适用范围」走同一条路，保持了「未调优形状的行为与引入 lt 之前完全一致（含 graph 捕获行为）」这一兼容性承诺（[L907-909](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L907-L909) 注释）。

## 5. 综合实践

**任务**：把本讲四个模块串成一条线——先证明「漂移存在且可测」，再证明「调优结果可以离线复用」，最后解释每条记录为什么长这样。

**步骤 1：跑两个门禁测试**（4.4.4 与 4.5.4 的命令）：

```bash
cargo test --release -p pegainfer-kernels --test lt_algo_store -- --nocapture
PEGAINFER_BI_REPS=8 cargo test --release -p pegainfer-kernels --test batch_invariance_gemm -- --nocapture --test-threads=1
```

**步骤 2：第一次启动引擎，让调优落盘**：

```bash
PEGAINFER_GEMM_LT_CACHE=/tmp/lt-store.txt cargo run --release -- --model-path models/Qwen3-4B
```

启动过程中每个首次遇到的 (M,N,K) 会现场计时调优，赢家写入 `/tmp/lt-store.txt`。服务起来后可以 Ctrl-C 退出，我们只关心启动段。

**步骤 3：观察文件内容**：

```bash
wc -l /tmp/lt-store.txt
head -n 2 /tmp/lt-store.txt
```

每行应是 `v1 <key> <8 个 16 进制字>` 的格式；`<key>` 里能看到 GPU 名称（空格换下划线）、算力版本、CUDA runtime 与 driver 版本、`bf16 TN`、workspace 上限。

**步骤 4：第二次启动，验证命中**：

```bash
PEGAINFER_GEMM_LT_CACHE=/tmp/lt-store.txt cargo run --release -- --model-path models/Qwen3-4B
```

**需要观察的现象**：

1. 第二次启动的 warm ready 时间明显缩短——仓库 CLAUDE.md 对该机制记录的参考量级：Qwen3-4B 在一张 sm_89 卡上 warm ready 约 1965 ms → 约 1312 ms（成对采样 5/5）。这是仓库文档口径，本机数字待本地验证。
2. stderr **不出现** `stored algo rejected`——出现即说明有记录键匹配但算法跑不动（库/环境变了），该形状回退重调优。
3. `/tmp/lt-store.txt` 行数不再增长——同键命中不追加；只有新形状才追加新记录。

**步骤 5：回答两个问题**（本综合实践的核心检验）：

- 每条记录为什么必须携带 GPU 型号、CUDA runtime/driver、cublasLt 版本这些指纹？
  `cublasLtMatmulAlgo_t` 是不透明结构，算法排名依赖硬件与库版本，官方不承诺跨版本存活——别的 GPU/别的库版本调出的 64 字节可能在目标机上无法运行或不再最优。键全对上才可能命中；对不上就是 miss + 重调优，**结果永远不受影响**，store 只改启动时间（「cache and never a source of truth」，[csrc/shared/linear.cu:191-199](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L191-L199)）。
- 哪一项指纹是故意不区分的？为什么？
  **driver 构建号**。`cudaDriverGetVersion` 返回驱动支持的 CUDA 等级而非构建号（[csrc/shared/linear.cu:223-224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L223-L224) 注释）——等级不变的驱动升级被视为同环境；怀疑驱动变化影响了算法存活时，删文件强制重调优即可。

**进阶（可选）**：把文件中某一条记录的最后一个十六进制字删掉再启动——该记录解析失败被忽略，对应形状重调优后追加新记录；同键旧新两条共存，读取端 last wins（[csrc/shared/lt_algo_store.cu:63-105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/lt_algo_store.cu#L63-L105)）。

**预期结果**：步骤 1 两个测试通过；步骤 2-4 出现上述三种现象。整套实践需要本机 GPU 与 `models/Qwen3-4B` 权重（待本地验证）。

## 6. 本讲小结

- **三层对齐**：线性层 GEMM 在仓库里是「Rust ops 包装（`src/ops/linear.rs` / `src/typed_ops.rs`）→ FFI 声明（`src/ffi/shared.rs`）→ CUDA 实现（`csrc/shared/linear.cu`）」三层按模型线对齐；权重行主序 W[M,K] 在 cuBLAS 眼中就是列主序 [K,M] 取转置，hidden states 的 [hidden, seq] 布局零转置直通。
- **路径分裂按上下文**：graph-safe 免 workspace 句柄服务 CUDA Graph 捕获与 N=1 投影；prefill 句柄 GemmEx 服务长批；N ≤ 32 且无 stream override 时走 cublasLt 调优算法，未调优形状的回退路径保持与引入 lt 之前完全一致（含捕获行为）。
- **lt 调优是设备级单次事件**：每 (M,N,K) 只计时一次，赢家经 mutex+condition variable 广播给所有线程各自收养；「调优即热身」保证随后 graph 回放安全；计时在多个同形权重间轮转防 L2 假快。
- **algo store 只改时间不改结果**：append-only 的 `v1 <key> <w0..w7>` 文本，last wins、撕裂尾行容忍、键带全版本指纹；它让第二次启动跳过计时，但任何不匹配都静默重调优。
- **batch 不变性 = 数值路径与批组成解耦**：`Tuned` 默认接受漂移；`Pin` 用每 (M,K) 一个确定性算法服务所有 N（不可服务即 fail-loud）；`PerToken` 是 N=1 oracle；三臂门禁用「baseline 必须漂移」做阳性对照，「pin 零漂移」才是有效证明。

## 7. 下一步学习建议

- **CUDA Graph 专题**：本讲反复出现的「graph-safe 句柄」「捕获时活着的算法就是回放算法」只是约束的入口；下一站应系统学习 graph capture 的指针稳定性、预分配缓冲与 decode 路径捕获（见手册 CUDA Graph 相关讲义与 `pegainfer-core` 执行器源码）。
- **paged KV 与注意力**：线性层之后，推理引擎的另一大状态容器是 KV cache——建议接着读手册的 paged-KV 讲义与 `pegainfer-kv-cache`。
- **源码阅读路线**：
  1. 在仓库全局搜索 `gemm_lt_tune(` 的调用点，看模型 crate（executor 线程）如何准备同形权重样本、在启动的哪个阶段触发调优；
  2. 读 [KERNELS.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md) 中 `shared.linear.*` 与各模型线的 linear 行，对照本讲的分层地图；
  3. 回到 [csrc/shared/linear.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu) 的 pin 段（L805-941），验证 4.5 的每个论断。
- **动手验证**：完成第 5 节综合实践，并把两台不同机器（或升级 CUDA 前后）各自生成的 store 文件 diff 一下——键指纹的差异会直接告诉你为什么跨环境命中是例外而不是常态。
