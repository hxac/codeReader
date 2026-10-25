# 常量泛型类型层：typed_ops 与 gpu_buffers!

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `GpuTensor<DIM>`、`GpuWeight<OUT, IN>`、`NormWeight<DIM>` 三种类型各自的内存布局，以及它们如何用 Rust **常量泛型（const generics）** 把形状检查从运行时挪到编译期。
2. 独立读懂 `typed_ops` 模块里的任何一个算子包装，理解 `gemm_into` 家族的分工（纯 typed、DeviceMatrix/HiddenStates 桥接、graphsafe 变体）。
3. 说清 `gemm_into` 与 `gemm_graphsafe_into` 行为差异的确切来源——不是数学不同，而是底层选用了**两个不同的 cuBLAS handle**。
4. 会用 `gpu_buffers!` 宏声明一整套随 batch 伸缩的 scratch 缓冲结构，并能准确说出 `set_batch_size` 的语义：**只改逻辑长度字段，不触碰显存分配**。

本讲是 u2-l1（`DeviceContext` 与张量地基）的直接续篇：u2-l1 讲了非类型化的 `DeviceVec` / `DeviceMatrix` / `HiddenStates`，本讲讲架在它们之上的「第二层」——编译期形状安全层。

## 2. 前置知识

### 2.1 常量泛型（const generics）

Rust 允许把一个**编译期常量**当作泛型参数：`GpuTensor<128>` 与 `GpuTensor<256>` 是**两个不同的类型**。这带来一个关键性质：

- 若函数签名要求 `&GpuTensor<128>`，你传 `&GpuTensor<64>`，**编译直接失败**（类型不匹配），程序根本跑不起来；
- 若维度只是运行时字段（如 `HiddenStates::hidden_dim`），错配只能在运行时被 `ensure!` 抓到——而且前提是有人写了检查。

PegaInfer 的模型维度（hidden_dim、head_dim、专家数……）在编译模型 crate 时就全部已知（它们是 config 里的常量），而 batch/seq_len 每一步都在变。所以类型层的设计是：**「模型维度进类型，batch 维度留运行时」**。

两个入门要点：

- 维度可以是任意常量表达式，宏里用 `{ expr }` 包起来（如 `{ KIMI_K2_ROUTED_EXPERTS }`）。为了支持这类写法，crate 顶层打开了 nightly 的 `generic_const_exprs` feature（见 [src/lib.rs:L1-L2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L1-L2)），仓库工具链锁定 nightly（`rust-toolchain.toml` 的 `nightly-2026-07-10`）。
- 常量泛型不产生任何运行时开销：`GpuTensor<128>` 编译后就是一个普通结构体，维度数字变成了指令里的立即数。

### 2.2 术语速查

| 术语 | 含义 |
|------|------|
| bf16 | 16 位脑浮点，LLM 推理的主力 dtype，本讲所有张量的元素类型 |
| GEMM | 通用矩阵乘 \( Y = W X \)，底层走 cuBLAS |
| 列主序视角 | 承接 u2-l1：token 连续存放的 `[DIM × seq_len]` 内存，在 cuBLAS 眼里就是 `[DIM, seq_len]` 的列主序矩阵，零转置直通 GEMM |
| scratch 缓冲 | 前向计算中的中间激活内存，每步复用、不重复分配 |
| CUDA Graph | 把一串 kernel launch 录成图反复回放；要求回放期间**指针稳定**、捕获期间**不发生显存分配**（细节在 u4-l3 展开，本讲只用这个结论） |

### 2.3 承接 u2-l1 的关键结论

- `DeviceContext` 持有 CUDA 上下文、唯一计算流，并在初始化末尾调用 `ffi::cublas_init()` 初始化 cuBLAS（[src/tensor.rs:L388-L390](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L388-L390)）——本讲 4.3 会追进这个调用。
- `HiddenStates` 的注释说明缓冲「按最大尺寸分配、每步重写 `seq_len`」（[src/tensor.rs:L575-L580](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L575-L580)）——本讲 4.4 的 `set_batch_size` 正是同一纪律的类型化版本。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs) | 类型层全部住在文件末尾的 `── Typed tensor layer ──` 段：`GpuTensor`、`GpuWeight`、`NormWeight`、`GpuRawSlice`、`GpuRawSliceI32` |
| [src/typed_ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs) | 建立在类型层之上的算子包装：GEMM 家族、RMSNorm、add、embedding、bf16↔f32 转换等 |
| [src/gpu_buffers.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs) | 仅 63 行的声明式宏：一把声明出「带 `new` / `set_batch_size` 的缓冲结构体」 |
| [src/ffi/shared.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs) | `gemm_cuda` / `gemm_graphsafe_cuda` 的 `extern "C"` 声明（完整 FFI 契约是 u2-l3 的主题） |
| [csrc/shared/linear.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu) | 上述两个 GEMM 入口的 CUDA 实现，以及两个 cuBLAS handle 的创建处 |
| [src/ops/linear.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs) | `ensure_tuned_policy` 数值策略守卫；非类型化 GEMM 家族（u3-l2 精读） |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs) | 模块导出与 nightly feature 开关 |
| [../pegainfer-kimi-k2/src/typed_scratch.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs) | （跨 crate 的真实消费者）`gpu_buffers!` 与手写带检查 setter 的实际用法 |

## 4. 核心概念与源码讲解

### 4.1 类型层地基：GpuTensor、GpuWeight 与 NormWeight

#### 4.1.1 概念说明

tensor.rs 文件末尾有一段设计说明（[src/tensor.rs:L639-L646](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L639-L646)），翻译过来就是三条设计决策：

1. `GpuTensor<DIM>`：把隐藏维写进类型；`seq_len`（batch 轴）留在运行时，因为它每步都在变。
2. 权重矩阵带两个维度：`GpuWeight<OUT, IN>`。
3. 这一层是**增量式**的：旧的 `HiddenStates` / `DeviceMatrix` 原样保留，模型 crate 一次迁移一个算子。

为什么值得多建一层？直觉上有三笔收益：

- **错配提前爆炸**：把 `[4096, 2560]` 的权重乘到 `[2048]` 的激活上，旧代码里要等 kernel launch 之后才可能发现（甚至无声算出错的结果）；新类型层里这是一条编译错误。
- **`ensure!` 样板消失**：非类型化路径里每个算子都要手写 3~5 条维度检查；typed 路径编译期已保证维度，只剩 `seq_len` 相等这一条运行时断言。
- **语义防呆**：`GpuWeight` 与 `GpuTensor` 是不同类型，你没法把激活误当权重传进 GEMM——尽管两者底层都是 `CudaSlice<bf16>`。

#### 4.1.2 核心流程

类型层的数据流是「一次运行时检查的收口，之后全是编译期保证」：

```text
host 数据 ( &[bf16] )
   │  DeviceMatrix::from_host / DeviceVec::from_host
   ▼
DeviceMatrix [rows, cols] / DeviceVec          ← 非类型化（维度是运行时字段）
   │  GpuWeight::from_device_matrix             ← 唯一转换口：运行时检查
   │  NormWeight::from_device_vec                 rows==OUT && cols==IN 等
   │  GpuTensor::from_device_matrix_rows          检查失败 → anyhow 错误
   ▼
GpuWeight<OUT,IN> / NormWeight<DIM> / GpuTensor<DIM>   ← 类型化
   │
   ▼
typed_ops::gemm_into 等（本讲 4.2）→ FFI → CUDA
```

内存布局（与 u2-l1 的 `HiddenStates` 完全一致，只是维度从字段变成了类型参数）：

- `GpuTensor<DIM>`：`[DIM * seq_len]` 连续 bf16，token `i` 起始于偏移 `i * DIM`；cuBLAS 视角下即 `[DIM, seq_len]` 列主序矩阵。
- `GpuWeight<OUT, IN>`：`[OUT, IN]` 行主序——第 `i` 行是第 `i` 个输出通道的整条输入权重。
- `NormWeight<DIM>`：长度 `DIM` 的 bf16 向量，RMSNorm 的逐元素缩放权重。

#### 4.1.3 源码精读

**GpuTensor**——[src/tensor.rs:L648-L678](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L648-L678) 定义了「编译期维度 + 运行时 batch」的结构体，以及两个构造口：`zeros` 按最大 batch 分配清零缓冲；`from_device_matrix_rows` 把行主序矩阵按「一行 = 一个 token」收编为 typed 张量：

```rust
pub struct GpuTensor<const DIM: usize> {
    pub data: CudaSlice<bf16>,
    pub seq_len: usize,
}

impl<const DIM: usize> GpuTensor<DIM> {
    pub fn zeros(ctx: &DeviceContext, seq_len: usize) -> Result<Self> { /* alloc_zeros(DIM * seq_len) */ }

    pub fn from_device_matrix_rows(m: DeviceMatrix) -> Result<Self> {
        anyhow::ensure!(m.cols == DIM, /* ... */);
        Ok(Self { data: m.data, seq_len: m.rows })
    }
}
```

注意 `from_device_matrix_rows` 为何成立：行主序 `[rows, cols]` 矩阵的第 `i` 行恰好长 `cols == DIM`，与 `GpuTensor` 的「token 连续、每 token `DIM` 个元素」约定逐字节重合，所以只需搬走所有权，零拷贝。

**GpuWeight**——[src/tensor.rs:L680-L697](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L680-L697)：

```rust
pub struct GpuWeight<const OUT: usize, const IN: usize> {
    pub(crate) data: CudaSlice<bf16>,
}
```

这里有一个刻意的 API 细节：`data` 是 `pub(crate)` 而 `GpuTensor::data` 是 `pub`。权重一旦上卡就是只读常量，`pub(crate)` 把外部 crate 的构造路径**强制收口**到 `from_device_matrix`——它做一次 `rows == OUT && cols == IN` 的运行时校验后吃掉 `DeviceMatrix`。于是「权重维度」在转换那一刻就被钉死进类型，之后任何用错都在编译期报错。

**NormWeight**——[src/tensor.rs:L699-L714](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L699-L714)：单维度 `DIM`，从 `DeviceVec` 转换时检查 `v.len == DIM`。

**配套的原始缓冲类型**——[src/tensor.rs:L716-L730](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L716-L730) 的 `GpuRawSlice<ELEMS>`（f32）与 [L733-L746](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L733-L746) 的 `GpuRawSliceI32<ELEMS>`：每个 batch 条目固定 `ELEMS` 个元素，用于 router logits、topk 索引这类「不是 bf16 激活」的辅助缓冲，是 4.4 宏的三类字段之二、之三。

**模块导出与 nightly 开关**——[src/lib.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L1-L12)：`tensor`、`typed_ops`、`gpu_buffers` 都是常驻模块（无 feature 门控）；文件顶部的 `#![feature(generic_const_exprs)]` 说明本 crate 需要 nightly 工具链，`rust-toolchain.toml` 已锁定 `nightly-2026-07-10`。

**真实消费者示例**——kimi-k2 把模型维度常量直接喂给类型参数（[../pegainfer-kimi-k2/src/runner/worker.rs:L591](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L591)）：

```rust
fused_qkv_a_proj: GpuWeight<KIMI_K2_MLA_QKV_A_OUT, KIMI_K2_HIDDEN>,
```

权重维度来自模型 config 常量——这正是「模型维度编译期已知」的活例子。

#### 4.1.4 代码实践

**实践 A：亲手构造一次类型化权重，观察两道防线**

1. **实践目标**：验证 `GpuWeight` 只能经 `from_device_matrix` 构造；观察转换口的运行时检查与字段私有性带来的编译期检查。
2. **操作步骤**（在你自己的工作副本中新建 `examples/typed_layer_practice.rs`；examples 目录由 cargo 自动发现，无需改 Cargo.toml。以下为**示例代码**）：

   ```rust
   use half::bf16;
   use pegainfer_kernels::tensor::{DeviceContext, DeviceMatrix, GpuWeight};

   fn main() -> anyhow::Result<()> {
       let ctx = DeviceContext::new()?;

       // 正确形状：[64, 128] → GpuWeight<64, 128>
       let host: Vec<bf16> = (0..64 * 128).map(|i| bf16::from_f32(i as f32 * 0.01)).collect();
       let dm = DeviceMatrix::from_host(&ctx, &host, 64, 128)?;
       let _w = GpuWeight::<64, 128>::from_device_matrix(dm)?;

       // 错误形状：[32, 128] 喂给 GpuWeight<64, 128> —— 运行时防线
       let bad: Vec<bf16> = vec![bf16::from_f32(0.0); 32 * 128];
       let dm_bad = DeviceMatrix::from_host(&ctx, &bad, 32, 128)?;
       let err = GpuWeight::<64, 128>::from_device_matrix(dm_bad).unwrap_err();
       println!("runtime guard: {err}");

       // 字段私有性：在 kernels crate 外直接构造结构体字面量 —— 编译期防线
       // 取消下行注释将无法通过编译：
       // let _leak = pegainfer_kernels::tensor::GpuWeight::<64, 128> { data: todo!() };
       Ok(())
   }
   ```

   运行：`cargo run --release -p pegainfer-kernels --example typed_layer_practice`（需要 GPU、nvcc 与仓库锁定的 nightly 工具链）。
3. **需要观察的现象**：`unwrap_err` 打印的错误信息应包含 `GpuWeight<64, 128>::from_device_matrix shape mismatch: got [32, 128]` 字样（错误文案见 [src/tensor.rs:L687-L691](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L687-L691)）；取消注释后编译器应报字段不可见。
4. **预期结果**：运行时防线给出清晰 anyhow 错误；编译期防线报 `field 'data' of struct 'GpuWeight<_, _>' is private` 一类的 E0603 错误。本环境无 GPU，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`GpuTensor<128>` 的背衬内存里，token 5 的第一个元素在哪个偏移？cuBLAS 把这块内存看成什么形状？

答案：偏移 `5 × 128 = 640`。cuBLAS 视角是 `[128, seq_len]` 的列主序矩阵（`DIM` 是行数、token 是列），与 `HiddenStates` 的约定一致。

**练习 2**：为什么 `GpuWeight` 的 `data` 用 `pub(crate)`，而 `GpuTensor` 的 `data` 是 `pub`？

答案：权重上卡后语义上是只读常量，`pub(crate)` 强制外部经 `from_device_matrix` 的形状校验口构造，杜绝绕过检查直接拼装；激活张量则需要被模型 crate 直接读写（拷贝、切片、喂给其他 ops），所以保持开放。

**练习 3**：`GpuTensor::from_device_matrix_rows` 为什么可以零拷贝收编一个 `DeviceMatrix`？

答案：行主序 `[rows, cols]` 矩阵第 `i` 行长 `cols == DIM`、连续存放，与 `GpuTensor<DIM>`「token `i` 在偏移 `i * DIM`」的布局逐字节重合，`rows` 直接变成 `seq_len`，只需转移 `CudaSlice` 所有权。

### 4.2 gemm_into 家族：typed_ops 的算子面

#### 4.2.1 概念说明

[src/typed_ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs) 的模块文档说得很直白：权重参数是类型化的（`GpuWeight`、`NormWeight`），因此张量与权重的维度匹配靠 const generics 检查，而不是运行时矩阵元数据。

整个模块按「参与方的类型化程度」分三族：

| 族 | 函数 | 输入 → 输出 |
|----|------|------------|
| 纯 typed | `gemm_into` / `gemm_graphsafe_into`（[L21-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L21-L75)） | `GpuWeight<OUT,IN>` × `GpuTensor<IN>` → `GpuTensor<OUT>` |
| 桥接族 | `gemm_runtime_out_into`（[L221-L228](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L221-L228)）、`gemm_dm_typed_to_hs` 系列（[L297-L418](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L297-L418)） | 权重或输出有一侧仍是 `DeviceMatrix` / `HiddenStates`（运行时维度） |
| 非 GEMM | `rms_norm_into`（[L80-L108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L80-L108)）、`fused_add_rms_norm_round_into`、`add_into`、`embedding_vocab_shard_into`、`bf16_to_f32_into` / `f32_to_bf16_into` | 同 DIM 的 typed 张量之间 |

桥接族的存在印证了 4.1 的「增量迁移」设计：迁移没走完的环节用 `DeviceMatrix` / `HiddenStates` 过渡，两边各做一次运行时对齐检查。

#### 4.2.2 核心流程

以 `gemm_into` 为例，数学上是

\[
Y = W X, \qquad
W \in \mathbb{R}^{OUT \times IN},\;
X \in \mathbb{R}^{IN \times bs},\;
Y \in \mathbb{R}^{OUT \times bs}
\]

其中 `OUT`、`IN` 来自类型参数，`bs = x.seq_len` 是运行时字段。包装流程：

```text
gemm_into::<OUT, IN>(ctx, w, x, y)
  1. ensure!(y.seq_len == x.seq_len)        ← 全模块唯一的运行时形状断言
  2. w/x/y 各借 device_ptr（绑定 ctx.stream 的 guard）
  3. launch_gemm(m=OUT, n=x.seq_len, k=IN, graphsafe = (x.seq_len == 1))
       3.1 ensure_tuned_policy(m, n, k)      ← 数值策略守卫
       3.2 graphsafe ? ffi::gemm_graphsafe_cuda : ffi::gemm_cuda
  4. status != 0 → anyhow 错误（带 m/n/k 上下文）
```

第 3 步的 `graphsafe` 布尔是本讲 4.3 的主角。`m`、`k` 直接用常量 `OUT`、`IN` 传给 FFI——编译期数字变成了 launch 参数的立即数。

#### 4.2.3 源码精读

**gemm_into**——[src/typed_ops.rs:L21-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L21-L47)：先断言两个 `seq_len` 相等，再取三根指针，最后把 `OUT`、`x.seq_len`、`IN` 作为 `m`、`n`、`k` 与 `x.seq_len == 1` 一起传进内部 `launch_gemm`。注意签名里没有任何维度比较代码——类型系统已经替你比过了。

**launch_gemm（FFI 分流的唯一出口）**——[src/typed_ops.rs:L465-L506](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L465-L506)：先过 `ensure_tuned_policy`，再按 `graphsafe` 标志二选一调 FFI，失败时用 `ffi_status_detail` 把状态码翻译成带 `m/n/k` 的错误。所有 GEMM 包装（包括桥接族）都汇到这一个私有函数，是理解 4.3 的枢纽：

```rust
let status = if graphsafe {
    ffi::gemm_graphsafe_cuda(w_ptr, x_ptr, y_ptr, m as i32, n as i32, k as i32,
                             crate::tensor::active_cu_stream(ctx))
} else {
    ffi::gemm_cuda(/* 同参数 */);
};
if status != 0 { anyhow::bail!("GEMM failed: {}, m={m}, n={n}, k={k}", ffi_status_detail(status)); }
```

流参数用的是 `active_cu_stream(ctx)`——承接 u2-l1 的 thread-local 覆盖机制，而非裸的 `ctx.stream`。

**ensure_tuned_policy 守卫**——[src/ops/linear.rs:L766-L774](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L766-L774)（经 [src/ops.rs:L193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L193) 再导出）：typed GEMM 只允许 `Tuned` 数值策略；qwen3 的 batch-invariant `Pin` / `PerToken` 模式不在 typed 路径的覆盖范围内，遇到会直接报错而不是悄悄换路径。

**rms_norm_into 的对照**——[src/typed_ops.rs:L80-L108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L80-L108)：同样是「一条 `seq_len` 断言 + 指针 + FFI」，但它调用 [src/ffi/shared.rs:L31-L39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L31-L39) 声明的 `rms_norm_batched_cuda`——该声明**返回 `()`**，所以 wrapper 无状态可查；对比 `fused_add_rms_norm_round_into`（[L114-L147](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L114-L147)）调用的 FFI 返回状态码并用 `.result()?` 转成 `anyhow::Error`。FFI 错误契约的完整图谱留给 u2-l3。

**FFI 声明**——[src/ffi/shared.rs:L307-L325](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L307-L325)：`gemm_cuda` 与 `gemm_graphsafe_cuda` 签名逐字相同（`W/X/Y` 三指针 + `M/N/K` + stream，返回 `i32` 状态）——这正暗示两者差异不在接口而在实现内部。

#### 4.2.4 代码实践

**实践 B：跑通一次 typed GEMM，再故意传错维度看编译错误**（本讲规格指定的主实践）

1. **实践目标**：亲手完成 `GpuWeight<64,128> × GpuTensor<128> → GpuTensor<64>` 的一次 GEMM，并与 CPU 参考值对拍；然后把 `GpuTensor<64>` 传给期望 `GpuTensor<128>` 的位置，确认形状错误被编译器拦截。
2. **操作步骤**：在你的工作副本新建 `examples/typed_gemm_practice.rs`（**示例代码**）：

   ```rust
   use half::bf16;
   use pegainfer_kernels::tensor::{DeviceContext, DeviceMatrix, GpuTensor, GpuWeight};
   use pegainfer_kernels::typed_ops;

   fn main() -> anyhow::Result<()> {
       let ctx = DeviceContext::new()?;
       const OUT: usize = 64;
       const IN: usize = 128;
       const BS: usize = 3;

       // 权重 [64,128]，激活 [3,128]（一行 = 一个 token）
       let w_host: Vec<bf16> = (0..OUT * IN).map(|i| bf16::from_f32((i % 17) as f32 * 0.1 - 0.8)).collect();
       let x_host: Vec<bf16> = (0..BS * IN).map(|i| bf16::from_f32((i % 13) as f32 * 0.1 - 0.6)).collect();
       let w = GpuWeight::<OUT, IN>::from_device_matrix(DeviceMatrix::from_host(&ctx, &w_host, OUT, IN)?)?;
       let x = GpuTensor::<IN>::from_device_matrix_rows(DeviceMatrix::from_host(&ctx, &x_host, BS, IN)?)?;
       let mut y = GpuTensor::<OUT>::zeros(&ctx, BS)?;

       typed_ops::gemm_into(&ctx, &w, &x, &mut y)?;
       ctx.sync()?;

       // D2H 回读并对拍 CPU f32 参考
       let y_host = ctx.stream.clone_dtoh(&y.data)?;
       let w32: Vec<f32> = w_host.iter().map(|v| v.to_f32()).collect();
       let x32: Vec<f32> = x_host.iter().map(|v| v.to_f32()).collect();
       for row in 0..OUT {
           let mut expect = 0.0f32;
           for (k, xv) in x32.chunks(IN).nth(0).unwrap().iter().enumerate() {
               expect += w32[row * IN + k] * xv;   // 只对拍 token 0
           }
           let got = y_host[row].to_f32();
           assert!((got - expect).abs() < 0.5, "row {row}: got {got}, expect {expect}");
       }
       println!("typed GEMM parity ok (token 0, bf16 tolerance)");

       // —— 编译期防线演示：解注释后 cargo build 直接失败 ——
       // let x_wrong = GpuTensor::<64>::zeros(&ctx, BS)?;
       // typed_ops::gemm_into(&ctx, &w, &x_wrong, &mut y)?;
       Ok(())
   }
   ```

   运行：`cargo run --release -p pegainfer-kernels --example typed_gemm_practice`。
3. **需要观察的现象**：a) 程序打印 parity 通过（bf16 输入下 f32 累加与 GPU 结果差异在容差内）；b) 解注释最后两行后，`cargo build --release -p pegainfer-kernels --example typed_gemm_practice` 报**类型不匹配**错误——`gemm_into` 从 `&w` 推断出 `IN = 128`，而实参是 `&GpuTensor<64>`。
4. **预期结果**：编译错误形如 `error[E0308]: mismatched types … expected '&GpuTensor<128>', found '&GpuTensor<64>'`（具体措辞随推断路径可能略有出入，**待本地验证**）。这正是 4.1 声称的收益：错误在 `cargo build` 阶段爆炸，而不是在 GPU 上。

#### 4.2.5 小练习与答案

**练习 1**：`gemm_into` 传给 `launch_gemm` 的 `m/n/k` 分别是什么？为什么只有 `n` 来自运行时？

答案：`m = OUT`、`n = x.seq_len`、`k = IN`。GEMM \( Y = W X \) 中只有 batch 轴每步变化；`OUT`、`IN` 是类型参数携带的编译期常量。

**练习 2**：桥接族（如 `gemm_dm_typed_to_hs_graphsafe`，[src/typed_ops.rs:L297-L330](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L297-L330)）为什么比纯 typed 版多出 `ensure!` 检查？

答案：它的一侧是运行时维度的 `DeviceMatrix` / `HiddenStates`，编译期无法保证对齐，只能在运行时检查 `w.cols == IN`、`y.hidden_dim == w.rows` 之类；纯 typed 版这些约束已由类型系统承担。

**练习 3**：`ensure_tuned_policy` 在什么条件下会让 typed GEMM 失败？

答案：进程的 `NUMERIC_POLICY` 被设为 `Pin` 或 `PerToken` 时（目前只有 qwen3 的 batch-invariant 模式会设），typed GEMM 不在 pin 的覆盖范围内，守卫直接报错，而不是静默换一条未验证的路径。

### 4.3 gemm_into 与 gemm_graphsafe_into：行为差异的来源

#### 4.3.1 概念说明

两个函数的**数学完全相同**、签名完全相同，唯一差别在 [src/typed_ops.rs:L44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L44) 与 [src/typed_ops.rs:L72](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L72) 各传了一个不同的 `graphsafe` 布尔：前者传 `x.seq_len == 1`，后者恒传 `true`。这个布尔最终决定 FFI 层二选一：`gemm_cuda` 还是 `gemm_graphsafe_cuda`。

而这两个 CUDA 入口的差异，用 linear.cu 自己的注释就能说清：

- `gemm_cuda`（[csrc/shared/linear.cu:L423-L425](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L423-L425) 的注释）："Uses the prefill handle — only called from prefill path, never under CUDA Graphs."——用 **prefill handle**，只走 prefill 路径，绝不进 CUDA Graph。
- `gemm_graphsafe_cuda`（[L451-L452](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L451-L452) 的注释）："same math as gemm_cuda but uses the workspace-free handle. Safe for CUDA Graph capture and decode path."——同样的数学，用 **workspace-free handle**，可安全用于 CUDA Graph 捕获与 decode 路径。

背景（承接 CLAUDE.md 的架构事实）：**decode 路径会被录进 CUDA Graph 反复回放**。Graph 捕获期间任何隐式的显存分配/资源协商都会让捕获失败或回放出错，所以 decode 用的是一条被单独驯化过的 handle；prefill 一次一个长 prompt、不捕获，可以用更贪心的默认 handle。

于是 Rust 侧的选择逻辑是：`gemm_into` 把「seq_len == 1」当作「这是 decode」的信号——单 token 的 GEMM 自动路由到 graph-safe 入口；`gemm_graphsafe_into` 则供模型 crate 在显式的 decode/捕获路径上静态调用，无条件走 graph-safe。

#### 4.3.2 核心流程

```text
                     gemm_into                     gemm_graphsafe_into
                          │                               │
          graphsafe = (x.seq_len == 1)          graphsafe = true
                          │                               │
                          └───────────┬───────────────────┘
                                      ▼
                               launch_gemm
                     （typed_ops.rs:L465-L506 唯一分流点）
                          │                    │
                 graphsafe = false      graphsafe = true
                          ▼                    ▼
                    ffi::gemm_cuda    ffi::gemm_graphsafe_cuda
                          │                    │
                  g_cublas_prefill_handle   g_cublas_handle
                  （linear.cu:428）          （linear.cu:455）
                  prefill 路径专用            decode / CUDA Graph 捕获
```

两个 handle 的出生点在 `cublas_init`（[csrc/shared/linear.cu:L343-L364](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L343-L364)）：**每个设备创建两个独立 handle**，分别登记进 `g_cublas_handles_by_device` 与 `g_cublas_prefill_handles_by_device`。调用链的上游正是 u2-l1 见过的 `DeviceContext::new_with_device` 末尾那次 `ffi::cublas_init()`（[src/tensor.rs:L388-L390](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L388-L390)）。

说明一句以免过度解读：代码里两个 handle 的创建语句完全相同，也没有显式的 workspace 配置调用——「workspace-free」是注释对该 handle 工程角色的描述（专供 decode/捕获路径使用）。handle 内部 workspace 机制的深究超出本讲范围，CUDA Graph 的完整约束在 u4-l3 展开。

#### 4.3.3 源码精读

**gemm_cuda（prefill 侧）**——[csrc/shared/linear.cu:L423-L449](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L423-L449)：检查 `g_cublas_prefill_handle` 非空，把当前 stream 绑到该 handle，然后一次 `cublasGemmEx`（`CUBLAS_OP_T, CUBLAS_OP_N`，bf16 输入、f32 计算）完成 \( Y = W^T_{K×M 视角} X \)。行主序 `W [M,K]` 以 `lda = K` 读作列主序 `[K,M]` 再转置，正是 u2-l1「同一段内存零转置直通 GEMM」约定的 cuBLAS 落点。

**gemm_graphsafe_cuda**——[csrc/shared/linear.cu:L451-L476](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L451-L476)：与上文逐行对照，唯一不同的标识符是 `g_cublas_handle`（workspace-free handle）。**函数体其余部分逐字符相同**——这就是「差异只在一个 handle」的最直接证据。

**Rust 侧两个包装**——[src/typed_ops.rs:L21-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L21-L47)（`gemm_into`，graphsafe 实参 `x.seq_len == 1`）与 [L49-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L49-L75)（`gemm_graphsafe_into`，恒 `true`）。同样的模式在桥接族重复出现：`gemm_runtime_out_impl` 传的是 `graphsafe || x.seq_len == 1`（[src/typed_ops.rs:L537](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L537)）——「单 token 即走 graph-safe」是全模块的一致纪律。

**DSL 侧的静态选择**——[src/forward_pass.rs:L165-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L165-L171)：`typed_pipeline!` 宏的 `gemm` 语句按编译期的 `prefill` / `graphsafe` 模式分别展开成 `gemm_into` 或 `gemm_graphsafe_into`。即模型作者在写前向声明时就锁定了走哪条 handle 路径（宏本身是 u2-l4 的主题）。

#### 4.3.4 代码实践

**实践 C：源码阅读型——写出两份 CUDA 实现的差异清单并追踪 handle 出生点**

1. **实践目标**：亲眼确认「两个 FFI 入口的差异只有一个标识符」，并把 handle 的创建链追到 u2-l1 的 `DeviceContext`。
2. **操作步骤**：
   1. 打开 [csrc/shared/linear.cu:L426-L476](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L426-L476)，逐行对照 `gemm_cuda` 与 `gemm_graphsafe_cuda`，记录所有不同的标识符。
   2. 阅读函数上方的两段注释（L423-L425、L451-L452），抄下各自对使用场景的限定。
   3. 追踪创建链：`DeviceContext::new_with_device`（[src/tensor.rs:L361-L397](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L361-L397)）→ `ffi::cublas_init()`（L388-L390）→ `cublas_init`（[csrc/shared/linear.cu:L343-L364](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L343-L364)），确认「每设备两 handle」。
   4. 思考并回答：如果 `gemm_into` 不做 `seq_len == 1` 特判、恒走 prefill handle，decode 捕获会发生什么？
3. **需要观察的现象**：差异清单里只应有 handle 变量名一处不同；`cublas_init` 里两个 `cublasCreate` 分支结构对称。
4. **预期结果**：第 4 步的推论——decode 的 GEMM 会经由 prefill handle launch，而该 handle 按注释「never under CUDA Graphs」，捕获路径等于违反了工程契约（具体故障模式取决于 handle 内部行为，不在此断言）。此实践为纯阅读，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：`gemm_into` 在 `x.seq_len == 5` 时会调哪个 FFI 入口？`seq_len == 1` 时呢？

答案：`seq_len == 5`（prefill 场景）→ `ffi::gemm_cuda`（prefill handle）；`seq_len == 1`（decode 场景）→ `ffi::gemm_graphsafe_cuda`（graph-safe handle）。

**练习 2**：`typed_pipeline!` 的 `prefill` 与 `graphsafe` 两种 gemm 模式分别映射到哪两个函数？这个选择发生在什么时候？

答案：分别映射到 `gemm_into` 与 `gemm_graphsafe_into`（[src/forward_pass.rs:L165-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L165-L171)）。选择发生在宏展开时（编译期），与 `gemm_into` 内部按 `seq_len` 的运行时自适应是两层互补机制。

**练习 3**：两个 cuBLAS handle 是什么时候、被谁创建的？

答案：`DeviceContext::new_with_device` 末尾调用 `ffi::cublas_init()`，进入 linear.cu 的 `cublas_init`，它按当前设备在两个 map 里各登记一个 handle（`g_cublas_handles_by_device` 与 `g_cublas_prefill_handles_by_device`），并把 thread-local 的 `g_cublas_handle` / `g_cublas_prefill_handle` 指向它们。

### 4.4 gpu_buffers! 宏与 set_batch_size 语义

#### 4.4.1 概念说明

一个模型每步前向要用十几块 scratch 缓冲（hidden、normed、gate_up、router logits……）。手写它们意味着：为每个字段写一遍 `GpuTensor::<D>::zeros(ctx, bs)?` 的 `new`，再写一遍「每步只改逻辑长度」的 `set_batch_size`。[src/gpu_buffers.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs)（全文件 63 行）把这堆样板压成一份声明：

```rust
pegainfer_kernels::gpu_buffers! {
    pub struct RouterScratch {
        pub router_logits:      GpuRawSlice<{ ROUTED_EXPERTS }>,
        pub router_topk_idx:    GpuRawSliceI32<{ TOPK }>,
    }
}
```

宏支持三类字段（对应 4.1 见过的三个类型）：

| 字段种类 | 展开类型 | `set_batch_size` 改写的字段 |
|----------|----------|------------------------------|
| `GpuTensor<{D}>` | `tensor::GpuTensor<D>`（bf16） | `seq_len` |
| `GpuRawSlice<{E}>` | `tensor::GpuRawSlice<E>`（f32） | `batch_size` |
| `GpuRawSliceI32<{E}>` | `tensor::GpuRawSliceI32<E>`（i32） | `batch_size` |

而 `set_batch_size` 的语义必须一句话记住：**只改逻辑长度字段，不重新分配、不做任何边界检查**。这是 CUDA Graph 指针稳定纪律（预读 u4-l3）的直接产物：

- 缓冲在 `new(ctx, batch_size)` 时按**最大** batch 一次性分配；
- 每步 decode 的实际 batch 变小时，只把 `seq_len` / `batch_size` 字段改小，kernel 只计算前 `bs` 行，**显存地址从头到尾不动**；
- 这与 u2-l1 里 `HiddenStates::checked_extent` 的文档注释（[src/tensor.rs:L575-L580](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L575-L580)："buffers allocate at a max and rewrite `seq_len` to the active size per step"）是同一条纪律的非类型化表述。

#### 4.4.2 核心流程

宏主规则（[src/gpu_buffers.rs:L5-L41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs#L5-L41)）做三件事：生成 struct、生成 `new`、生成 `set_batch_size`；两类内部规则 `@alloc`（L43-L51）与 `@set_bs`（L53-L61）按字段种类分派。

对上面两字段的 `RouterScratch` 手工展开后是（**展开示意**，依据宏规则手写，非 `cargo expand` 输出）：

```rust
pub struct RouterScratch {
    pub router_logits:   pegainfer_kernels::tensor::GpuRawSlice<{ ROUTED_EXPERTS }>,
    pub router_topk_idx: pegainfer_kernels::tensor::GpuRawSliceI32<{ TOPK }>,
}

impl RouterScratch {
    pub fn new(ctx: &pegainfer_kernels::tensor::DeviceContext,
               batch_size: usize) -> anyhow::Result<Self> {
        Ok(Self {
            router_logits:   GpuRawSlice::<{ ROUTED_EXPERTS }>::zeros(ctx, batch_size)?,
            router_topk_idx: GpuRawSliceI32::<{ TOPK }>::zeros(ctx, batch_size)?,
        })
    }

    pub fn set_batch_size(&mut self, bs: usize) {
        self.router_logits.batch_size = bs;    // @set_bs 的 GpuRawSlice 分支
        self.router_topk_idx.batch_size = bs;  // @set_bs 的 GpuRawSliceI32 分支
    }
}
```

生命周期视图：

```text
new(ctx, max_bs)          每个 decode step
  分配 D×max_bs bf16 ────► set_batch_size(active_bs)
  地址从此固定              只改 seq_len/batch_size 字段（纯字段赋值）
  │                         │
  └────── CUDA Graph 捕获时录下的指针不变 ──────┘
```

#### 4.4.3 源码精读

**宏主体**——[src/gpu_buffers.rs:L5-L41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs#L5-L41)：结构体元属性、字段可见性（`$vis`、`$field_vis`）都原样透传；字段类型统一展开成 `$crate::tensor::$kind<{ $dim }>`，所以 `$kind` 只能是三类已知名字。`new` 里每个字段调一次 `@alloc`；`set_batch_size` 里每个字段调一次 `@set_bs`。

**@alloc 分派**——[src/gpu_buffers.rs:L43-L51](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs#L43-L51)：三种类型各自调 `zeros(ctx, batch_size)`，即 4.1 读过的按最大尺寸一次性分配。

**@set_bs 分派**——[src/gpu_buffers.rs:L53-L61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs#L53-L61)：注意三个分支都只有**一条字段赋值语句**——`self.$field.seq_len = $bs;` 或 `self.$field.batch_size = $bs;`。没有 `ensure!`，没有 `alloc_zeros`，没有同步。宏把「不越界」的责任完全交给调用方。

**真实消费者：kimi-k2 的 RouterScratch**——[../pegainfer-kimi-k2/src/typed_scratch.rs:L115-L121](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs#L115-L121) 用宏声明了三个字段的 router scratch（logits 是 f32 的 `GpuRawSlice`，topk 权重是 f32、topk 索引是 i32）——routed-expert 数与 top-k 都是编译期常量，正好进类型参数。

**对照：手写的带检查 setter**——同文件 [L238-L250](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs#L238-L250) 的 `set_gpu_tensor_seq_len` 做了宏**没有**做的事：`ensure!(seq_len > 0 && seq_len * DIM <= tensor.data.len())` 之后才允许改 `seq_len`。这是同一个 crate 生态里两种取舍并存的样本：宏走极致轻量（热路径每步都调，不能有检查开销与分配），手写 setter 用在需要防御的位置。

**风险画像**（把上面的契约说透）：若容量 8 的缓冲被 `set_batch_size(16)`，宏本身不会报错；紧接着的 `gemm_into` 只检查 `x.seq_len == y.seq_len`，也拦不住——kernel 将按 16 行写一块只装得下 8 行的显存，属于静默越界。正确用法是：`new` 时按服务上限分配，此后 `set_batch_size` 的实参永不超上限。

#### 4.4.4 代码实践

**实践 D：声明一个两字段缓冲结构，证明 set_batch_size 不碰显存**（本讲规格指定的实践第三部分）

1. **实践目标**：用 `gpu_buffers!` 生成一个含两个字段（不同种类）的结构，验证 `set_batch_size` 前后背衬分配的字节数完全不变、只有逻辑长度字段变化。
2. **操作步骤**：在工作副本新建 `examples/gpu_buffers_practice.rs`（**示例代码**）：

   ```rust
   use pegainfer_kernels::gpu_buffers;
   use pegainfer_kernels::tensor::DeviceContext;

   gpu_buffers! {
       pub struct PracticeBuffers {
           pub hidden: GpuTensor<128>,     // bf16 激活，每 token 128 个元素
           pub scores: GpuRawSlice<4>,     // f32 辅助缓冲，每 token 4 个元素
       }
   }

   fn main() -> anyhow::Result<()> {
       let ctx = DeviceContext::new()?;
       let mut bufs = PracticeBuffers::new(&ctx, 8)?;      // 按上限 8 分配

       let (h_len, h_seq) = (bufs.hidden.data.len(), bufs.hidden.seq_len);
       let (s_len, s_bs) = (bufs.scores.data.len(), bufs.scores.batch_size);
       println!("before: hidden={h_len} elems (seq_len={h_seq}), scores={s_len} elems (bs={s_bs})");

       bufs.set_batch_size(3);                              // 缩到 3

       println!("after:  hidden={} elems (seq_len={}), scores={} elems (bs={})",
                bufs.hidden.data.len(), bufs.hidden.seq_len,
                bufs.scores.data.len(), bufs.scores.batch_size);

       assert_eq!(bufs.hidden.seq_len, 3);
       assert_eq!(bufs.scores.batch_size, 3);
       assert_eq!(bufs.hidden.data.len(), h_len);           // 分配未变
       assert_eq!(bufs.scores.data.len(), s_len);           // 分配未变
       println!("set_batch_size 只改逻辑长度，不重新分配显存 ✓");
       Ok(())
   }
   ```

   运行：`cargo run --release -p pegainfer-kernels --example gpu_buffers_practice`。
3. **需要观察的现象**：`before` 行打印 `hidden=1024 elems (seq_len=8), scores=32 elems (bs=8)`；`after` 行的元素总数仍是 1024 与 32，只有括号里的逻辑长度变成 3；最后打印确认断言全部通过。
4. **预期结果**：如上。`128 × 8 = 1024` 个 bf16、`4 × 8 = 32` 个 f32 的背衬分配从头到尾不动——这正是 CUDA Graph 回放所要求的指针稳定性。本环境无 GPU，**待本地验证**。
5. 附加一问（不要求运行）：把 `bufs.set_batch_size(3)` 改成 `bufs.set_batch_size(16)` 再跑，断言会通过吗？——会通过（字段赋值无检查），但这已经埋下越界隐患；回头对照 4.4.3 的风险画像加深印象。

#### 4.4.5 小练习与答案

**练习 1**：`gpu_buffers!` 的字段类型可以用任意 `tensor` 模块里的类型吗？

答案：不能。`$kind:ident` 只在 `@alloc` / `@set_bs` 规则里与 `GpuTensor` / `GpuRawSlice` / `GpuRawSliceI32` 三个字面量匹配，写别的名字会在宏匹配阶段报错。

**练习 2**：为什么 `set_batch_size` 不顺手做一次越界检查（比如 kimi-k2 手写 setter 的那条 `ensure!`）？

答案：两个原因。其一，性能纪律：它位于每步 decode 的热路径，宏的生成物就是纯字段赋值；其二，容量上限在 `new` 时已确定且模型 crate 自己掌握，检查责任显式交给调用方。需要防御的场景（如 kimi-k2 的 `set_moe_seq_len`）由模型 crate 在宏之外加带检查的手写 setter。

**练习 3**：`set_batch_size(3)` 之后，`data.len()` 为什么不是 `3 × DIM`？

答案：`data.len()` 是背衬 `CudaSlice` 的**物理容量**（按 `new` 时的上限分配，从不变化）；`seq_len` / `batch_size` 只是记录「本次前向实际用前几行」的**逻辑长度**。kernel 按逻辑长度发射行数，多余行空闲。

## 5. 综合实践

**迷你类型化前向：RMSNorm → GEMM → graph-safe GEMM，全程 batch 自适应**

把本讲四个模块串成一个可运行的最小闭环（在 GPU 机器上）：

1. **任务描述**：用 `gpu_buffers!` 声明 `hidden: GpuTensor<128>` 与 `proj: GpuTensor<64>` 两个字段的缓冲，容量按 `MAX_BS = 8` 分配；准备 `NormWeight<128>`、`GpuWeight<64,128>`、`GpuWeight<128,128>`；对 `bs = 5` 执行 `rms_norm_into` → `gemm_into`（此时 `seq_len > 1`，走 prefill 路径）→ 用 `gemm_graphsafe_into` 模拟单 token 的 decode 投影；再 `set_batch_size(1)` 重复一次 decode 步骤，确认缓冲地址未变而计算行数变为 1。
2. **操作步骤**：
   1. 仿照实践 B/D 在 `examples/` 下新建文件，组合 `typed_ops::rms_norm_into`（[src/typed_ops.rs:L80-L108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L80-L108)）、`gemm_into`、`gemm_graphsafe_into` 三个调用；
   2. 为 `rms_norm_into` 写 CPU 参考：\( out_i = \dfrac{x_i}{\sqrt{\frac{1}{D}\sum_j x_j^2 + \epsilon}} \cdot w_i \)（f32 累加，bf16 输入，容差放宽到相对 5%）；
   3. 在每次 `set_batch_size` 前后打印 `bufs.hidden.data.len()`，确认不变；
   4. 运行 `cargo run --release -p pegainfer-kernels --example <你的文件名>`。
3. **需要观察的现象**：三段计算的 GPU 结果都在容差内命中 CPU 参考；两次 decode 调用之间缓冲元素数保持 1024 不变；`bs=5` 的 GEMM 若在调用前 `log` 打点（或在 4.3 实践 C 的理解基础上推理）应经由 prefill handle，`bs=1` 的调用经由 graph-safe handle。
4. **预期结果**：全部断言通过。若你手头没有 GPU，可以把第 3 步降级为「源码推演」：对照 [src/typed_ops.rs:L44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L44) 写下每个调用将走的 handle 分支。运行结果**待本地验证**。

## 6. 本讲小结

- **类型层三分法**：`GpuTensor<DIM>`（激活）、`GpuWeight<OUT,IN>`（权重，`pub(crate)` data 强制走检查过的转换口）、`NormWeight<DIM>`（归一化权重）；模型维度进类型参数，batch 轴留运行时字段。
- **typed_ops 是薄包装**：每个算子只剩一条 `seq_len` 相等断言 + 指针借用 + FFI；所有 GEMM 汇入 `launch_gemm` 这一个分流点。
- **gemm_into 与 gemm_graphsafe_into 数学相同**：差异只在 `launch_gemm` 的 `graphsafe` 布尔，它决定用 prefill handle 还是 workspace-free/graph-safe handle——CUDA 侧两个函数体的差异只有一个标识符。
- **「seq_len == 1 即 decode」是全模块纪律**：`gemm_into` 与桥接族都据此自动路由到 graph-safe 入口，因为 decode 路径会被 CUDA Graph 捕获。
- **gpu_buffers! = 声明式样板消除器**：三类字段、`new` 按上限分配一次、`set_batch_size` 只改逻辑长度字段。
- **set_batch_size 无检查、无分配**：指针稳定是 CUDA Graph 的前提，容量契约由调用方持有；kimi-k2 在需要防御处用带 `ensure!` 的手写 setter 补位。

## 7. 下一步学习建议

- **u2-l3（FFI 声明与跨语言错误契约）**：本讲已埋下钩子——为什么有的 FFI 返回 `()`（`rms_norm_batched_cuda`）、有的返回 `i32` 状态、有的返回 `CUresult` 并用 `.result()?` 收尾；下一讲把这条错误通道从 CUDA 错误码追到 `anyhow::Error`。
- **u2-l4（typed_pipeline! 前向声明 DSL）**：本讲 4.3 已经看到宏按 `prefill`/`graphsafe` 模式静态选择 `gemm_into` / `gemm_graphsafe_into`；下一讲系统学习这套 DSL 的语法与逃逸口。
- **提前预读的源码**：[../pegainfer-kimi-k2/src/typed_scratch.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs)——目前 `gpu_buffers!` 与 typed scratch 最重要的真实消费者，值得通读一遍看类型层在完整模型线里如何落地。
- **远期衔接**：u4-l3（CUDA Graph 安全与流管理）会解释本讲反复引用的「指针稳定」约束的完整图景；u3-l2（线性层与 cuBLAS）会展开 prefill handle 之外更完整的 GEMM 调优面（`gemm_lt` 家族与 algo store）。
