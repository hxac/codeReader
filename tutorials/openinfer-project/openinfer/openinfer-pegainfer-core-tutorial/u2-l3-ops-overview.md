# ops.rs：共享 GPU 算子封装总览

> 前置：本讲承接 u2-l1（tensor 门面）与 u2-l2（ffi 边界）。你已经知道 pegainfer-core 里存在「core 拥有 API 门面、kernels 拥有实现」的分层纪律；本讲把这套纪律推进到第三张、也是最大的一张门面——`ops`。

## 1. 本讲目标

学完本讲，你应当能够：

1. **说清 `ops.rs` 的门面定位**：它把 `pegainfer_kernels::ops` 的 69 项底层能力、core 自己实现的 attention/paged_plan 两个子模块，以及 feature 门控下的 traced 包装器，合并成模型 crate 可以直接调用的稳定路径 `pegainfer_core::ops::*`。
2. **按类别归纳再出口清单**：归一化/激活、GEMM/GEMV、注意力、gather/extract 等各占多少，并从清单形态反推模型前向的算子形态。
3. **解释 `kernel-call-trace` feature 的「cfg 换出口」机制**：同一个符号（如 `rms_norm_batch_into`、`gemm_into`）在 feature 开、关时分别从 `traced.rs` 或 `pegainfer_kernels::ops` 导出，而调用方代码一行不改。
4. **理解 `call_spec.rs`（调用规格）、`traced.rs`（包装器）、`call_trace.rs`（记录器）** 三者如何组成一套算子级调用追踪设施。

## 2. 前置知识

- **算子（op）与 Transformer 前向**。一个 decoder 模型的一次前向大致是：embedding 查表 → N 层重复（RMSNorm → QKV 投影 → 注意力 → 残差加 → RMSNorm → gate/up 投影 → SiLU·mul 激活 → down 投影 → 残差加）→ 最终 norm → lm_head → argmax/采样。RMSNorm 的定义是

  \[ \mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{d}\sum_{i=1}^{d} x_i^2 + \varepsilon}} \odot w \]

  SiLU 是 \(\mathrm{SiLU}(x) = x\cdot\sigma(x)\)，FFN 里常用「gate/up 拼接投影 → SiLU(gate) ⊙ up」。`ops.rs` 的再出口清单基本就是这条流水线的算子字典——看懂清单就看清了前向。
- **Rust 再出口与模块可见性**。`pub use a::b;` 把别处定义的名字引入本模块命名空间；`mod x;`（私有）+ `pub use x::f;`（选择性公开）是「模块私有、成员公开」的惯用法（u1-l2 已见过这种组织）。
- **cargo feature 与 `#[cfg]`**。`Cargo.toml` 的 `[features] kernel-call-trace = []` 定义一个不引入任何额外依赖的纯开关；`#[cfg(feature = "kernel-call-trace")]` 使代码块只在开关打开时参与编译（u1-l3 讲过 core 唯一的 dev-dependency `tempfile` 也只挂在这个 feature 上）。
- **CUDA kernel launch 是异步的**。宿主侧函数把参数送上 stream 即返回，不等 GPU 算完（u2-l1 的流纪律）。因此本讲的追踪设施记录的是「调用」（名字 + 形状），不是「完成」——这与性能剖析器（profiler）不同，它几乎零成本。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `pegainfer-core/src/ops.rs` | 115 | 本讲主角：模块声明 + 三批再出口（原生 6 项、kernels 69 项、traced 10 项） |
| `pegainfer-core/src/ops/attention.rs` | 146 | 三个分页注意力封装入口（u2-l4 精读） |
| `pegainfer-core/src/ops/paged_plan.rs` | 246 | `PrefillPagedPlan` / `SplitKvCsr` 分页计划（u2-l4 精读） |
| `pegainfer-core/src/ops/call_spec.rs` | 325 | `KernelCall` 调用规格的构造器词汇表 |
| `pegainfer-core/src/ops/traced.rs` | 220 | 仅 `kernel-call-trace` 下编译：10 个带记录的包装器 |
| `pegainfer-core/src/ops/call_trace.rs` | 126 | 仅 `kernel-call-trace` 下编译：记录与收集基础设施 |
| `pegainfer-core/src/tensor.rs` | 1 | tensor 门面（u2-l1），traced.rs 的类型来源 |
| `pegainfer-core/src/lib.rs` | 15 | `pub mod ops;` 在 L10 |
| `pegainfer-core/Cargo.toml` | — | `kernel-call-trace = []` 在 L36 |

模块声明一览（本讲反复引用，先记住）：`mod attention;`、`pub mod call_spec;`、`mod paged_plan;` 是无条件编译；`pub mod call_trace;` 与 `mod traced;` 只在 feature 打开时存在。也就是说 `call_spec` 与 `call_trace` 是仅有的两个对外公开的子模块，其余三个子模块对模型 crate 不可见，只能通过 `ops.rs` 顶层再出口的名字使用。

## 4. 核心概念与源码讲解

### 4.1 算子再出口门面

#### 4.1.1 概念说明

pegainfer-core 有三张门面，纪律相同、形态不同：

| 门面 | 形态 | 真身 |
|---|---|---|
| `tensor.rs` | 一行 `pub use pegainfer_kernels::tensor::*;` | kernels 的 tensor 模块 |
| `ffi.rs` | 一行 `pub use pegainfer_kernels::ffi::*;` | kernels 的 ffi 模块 |
| `ops.rs` | **逐项点名的结构化门面** | kernels 的 ops 模块 + core 自己的两个原生子模块 + traced 包装器 |

ops.rs 为什么不照抄一行 `pub use pegainfer_kernels::ops::*;`？三个理由：

1. **公开面即契约**。逐项点名意味着 kernels 内部新增、改名、重构不会自动泄漏到模型 crate；模型 crate 依赖的是一个稳定的名字集合。
2. **给 cfg 换出口留位置**。同一个名字（如 `gemm_into`）需要根据 feature 从两个不同模块导出，通配 `*` 无法按名字挑选，必然与 traced 出口冲突。
3. **与本地实现合并命名空间**。`attention.rs` 和 `paged_plan.rs` 是 core 自己写的（分页注意力的 Rust 侧调度面），它们的入口要和 kernels 的算子并排摆在同一个 `ops::` 命名空间里，模型 crate 用起来才无差别。

它解决的问题是：模型 crate（qwen3、kimi-k2……）不直接依赖 `pegainfer-kernels` 的路径细节，只面对 `pegainfer_core::ops` 这一层稳定 API；底层换成哪个 kernel、是否带追踪，都由 core 的出口决定。

#### 4.1.2 核心流程

ops.rs 的三段式结构（伪代码）：

```text
ops.rs（115 行）
├── 模块声明区（L3-L9）
│   mod attention;                    私有 · core 原生
│   pub mod call_spec;                公开 · 调用规格词汇表
│   #[cfg] pub mod call_trace;        feature 下公开 · 记录器
│   mod paged_plan;                   私有 · core 原生
│   #[cfg] mod traced;                feature 下私有 · 包装器
├── 原生入口再出口（L11-L16，共 6 项）
│   attention 3 个注意力入口 + paged_plan 2 个类型 + 1 个构造函数
├── kernels 大宗再出口（L17-L95，共 69 项）
│   其中 10 项被 #[cfg(not(feature))] 摘出，改由 traced 提供
└── traced 再出口（L96-L115，共 10 对 cfg+use）
```

公开名字总量：6（原生）+ 69（kernels）+ 10（traced，与 kernels 侧 10 项同名互换）= **85 个名字**。69 项 kernels 再出口的构成为 64 个函数 + 5 个类型/常量。按用途归类（我自己的统计，供你 4.1.4 对拍）：

| 类别 | 数量（函数） | 代表符号 |
|---|---|---|
| 归一化 / 激活 | 16（+1 类型） | `rms_norm` 家族 9 个、`fused_add_rms_norm_*` 2 个、`silu_mul_*` 3 个、`gelu_tanh_mul_batch_into` |
| GEMM / GEMV / linear | 12（+1 常量） | `gemm`、`gemm_into(_checked)`、`gemm_rows_*`、`gemm_graphsafe_*`、`gemv`、`linear`、`gemm_lt_tune` |
| 注意力 / RoPE / 采样 | 10（+2 类型/常量） | `single_prefill_nhd_causal_into`、`qk_norm_rope_batch_decode_into`、`dflash_qk_norm_rope_into`、`eagle3_rope_into`、`argmax`、`suppress_logits_bf16_in_place` |
| gather / extract / copy | 9 | `extract_vec(_ref)(_into)`、`gather_hidden_tokens_into`、`copy_hidden_rows_into`、`write_vec_into` |
| 逐元素 / 缩放 / 累加 | 11 | `add_batch(_into)`、`scale_*_in_place`、`scaled_add_rows_*`、`bf16_hidden_to_f32_into` |
| embedding | 2 | `embedding_batch`、`embedding_decode_into` |
| LoRA | 3（+1 类型） | `lora_decode_fused_delta_*`、`pack_lora_b_rows_into` |
| 调度元数据 | 1 | `advance_decode_metadata` |

（另有原生 6 项：`prefill_attention_paged_into`、`paged_attention_batch_decode_into`、`paged_attention_batch_decode_split_kv_into`、`PrefillPagedPlan`、`SplitKvCsr`、`build_split_kv_csr`。）

#### 4.1.3 源码精读

**模块声明区**。[pegainfer-core/src/ops.rs:L1](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L1) 是整份文件唯一的文档注释，一句话定位本模块：共享 GPU 算子封装与 kernel crate 再出口。[pegainfer-core/src/ops.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L3-L9) 声明五个子模块：两个私有（`attention`、`paged_plan`）、一个公开（`call_spec`）、两个 feature 门控（`call_trace` 公开、`traced` 私有）。

```rust
mod attention;
pub mod call_spec;
#[cfg(feature = "kernel-call-trace")]
pub mod call_trace;
mod paged_plan;
#[cfg(feature = "kernel-call-trace")]
mod traced;
```

**原生入口再出口**。[pegainfer-core/src/ops.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L11-L16) 把 attention 的三个入口（对应 [attention.rs:L12](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L12) 的 `prefill_attention_paged_into`、[attention.rs:L53](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L53) 的 `paged_attention_batch_decode_into`、[attention.rs:L94](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L94) 的 split-KV 版本）和 paged_plan 的三个名字（`PrefillPagedPlan` 在 [paged_plan.rs:L71](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L71)、`SplitKvCsr` 在 [paged_plan.rs:L11](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L11)、`build_split_kv_csr` 在 [paged_plan.rs:L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L20)）抬进顶层命名空间。细节留给 u2-l4。

**kernels 大宗再出口**。[pegainfer-core/src/ops.rs:L17-L33](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L17-L33) 是清单开头：先出 5 个类型/常量（`GEMM_LT_MAX_N`、`GatedNormActivation`、`LoraDecodeGroupedProjection`、`SUPPORTED_GQA_GROUP_SIZES`、`SuppressIds`），再按字母序出函数（`accumulate_bf16_token_scaled_to_f32_into` … `eagle3_rope_into`）。[pegainfer-core/src/ops.rs:L63-L67](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L63-L67) 是 GEMM/linear/LoRA 段（`gemv`、`linear`、`lora_decode_fused_delta_*`、`pack_lora_b_rows_into`）；[pegainfer-core/src/ops.rs:L70-L79](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L70-L79) 是 rms_norm 家族段；[pegainfer-core/src/ops.rs:L86-L95](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L86-L95) 是激活与单序列注意力段（`silu_mul_batch`、`single_decode_nhd_into`、`softcap_bf16_in_place`、`suppress_logits_bf16_in_place`、`write_vec_into`）。整段 69 项中夹着 10 行 `#[cfg(not(feature = "kernel-call-trace"))]` 属性（L34、L43、L51、L53、L57、L59、L61、L68、L74、L88），被摘出的正是 4.2 要讲的换出口符号。

**命名规律是清单的「第二语言」**，读清单时建议按后缀解码：

- `_into`：结果写入调用方给的输出缓冲（destination-passing）。调用方预分配、复用缓冲，这是 CUDA Graph 捕获要求指针稳定的前提（u4-l1 详讲）。
- `_batch` / `_batch_into`：一次处理整段序列的行（prefill 形态）。
- `_rows` / `_token_range` / `_offset`：只处理行子集（分块 prefill、滑动窗口、页内偏移）。
- `graphsafe`：可安全进入 CUDA Graph 捕获区的变体。
- 融合前缀（`fused_add_`、`dual_`、`gated_`、`qk_norm_rope_`、`silu_mul_`）：多个小操作合并成一个 kernel，省一次显存往返——LLM 推理的算子小、带宽贵，融合是主要优化面。

#### 4.1.4 代码实践

**实践目标**：亲手数出三批再出口的数量并自行归类，与 4.1.2 的表格对拍。

**操作步骤**（纯文本统计，无需 GPU，已在本仓库验证过输出）：

```bash
# 在 pegainfer-core/ 目录下
grep -c 'pub use pegainfer_kernels::ops::' src/ops.rs   # → 69
grep -c 'pub use traced::'                  src/ops.rs   # → 10
grep -c 'pub use attention::\|pub use paged_plan::' src/ops.rs  # → 6
```

**需要观察的现象**：三个数字之和是 85；再数一数 `#[cfg(not(feature = "kernel-call-trace"))]` 的出现次数（应为 10），它与 `pub use traced::` 的条数相等——这就是「换出口」的静态证据。

**预期结果**：与 4.1.2 的统计一致。若你把 69 项按类别归类，归一化/激活类（16 个函数）在函数中占比最高（16/64 = 25%），其次 GEMM 家族（12/64 ≈ 19%）。

**这暗示了模型前向的什么形态**：归一化与激活是每层至少执行两三次的高频小算子，变体多（batch/offset/dual/gated/add 融合）说明工程重心在「减少访存次数」而非单算子算力；GEMM 家族自带 `graphsafe`/`checked`/`per_token`/`lt_tune` 变体，则对应 CUDA Graph 捕获（CLAUDE.md 提到的 decode 路径图捕获）与 cuBLASLt 算法调优（环境变量 `PEGAINFER_GEMM_LT_CACHE` 的落点）两条工程线。

#### 4.1.5 小练习与答案

1. **问**：为什么 ops.rs 用逐项 `pub use` 而不是 `pub use pegainfer_kernels::ops::*;`？
   **答**：三点——公开面即契约（kernels 内部变动不外泄）；cfg 换出口需要按名字从两个模块二选一，通配无法做到且会同名冲突；core 原生的 attention/paged_plan 入口要并入同一命名空间。
2. **问**：`mod attention;` 是私有模块，但它的入口被 `pub use` 出去，这样设计有什么好处？
   **答**：attention.rs 的内部类型、helper、错误路径全部不进入公开面，将来可以自由重构；模型 crate 只依赖三个稳定入口，耦合面最小。
3. **问**：`rms_norm_into`、`rms_norm_batch_offset_into`、`dual_rms_norm_add_batch_into` 三个名字分别针对什么场景？
   **答**：单段就地归一化（`into`，输出复用调用方缓冲）；带行偏移的批量归一化（页/分块场景只碰行子集）；双输入相加后再归一化的融合 kernel（残差流合并，省一次显存读写）。

### 4.2 cfg 换出口（kernel-call-trace feature）

#### 4.2.1 概念说明

「换出口」指：**同一个公开名字，在 feature 开、关时来自两个不同模块**。受影响的 10 个符号是：

`embedding_batch`、`fused_add_rms_norm_batch_into`、`gemm_into`、`gemm_into_checked`、`gemm_rows_into`、`gemm_rows_into_checked`、`gemm_token_range_into_checked`、`qk_norm_rope_batch_decode_into`、`rms_norm_batch_into`、`silu_mul_fused_batch_into`。

机制是两条互斥的 cfg：

- feature **关**（默认）：`#[cfg(not(feature = "kernel-call-trace"))] pub use pegainfer_kernels::ops::X;` —— 直连 kernels，零包装开销。
- feature **开**：`#[cfg(feature = "kernel-call-trace")] pub use traced::X;` —— 包装器先记录一次调用规格，再委托 kernels。

两条 `pub use` 在任何一种编译配置下只有一条存在，所以不会同名冲突；而**调用方始终写 `pegainfer_core::ops::X`，一行不用改**。这是「编译期装仪表，运行期读仪表」的设计：编译期由 feature 决定记录代码是否存在；运行期由 `call_trace::is_enabled()` 决定这次调用要不要真的记录（见 4.4）。

feature 本身在 [pegainfer-core/Cargo.toml:L36](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/Cargo.toml#L36) 定义：`kernel-call-trace = []`——一个空依赖列表的纯开关。

#### 4.2.2 核心流程

编译期矩阵：

```text
                        feature = off（默认）          feature = on
ops::rms_norm_batch_into
  的真实来源            pegainfer_kernels::ops         ops::traced（core 内）
  函数体                kernels 直出                   is_enabled 守卫 + record_call + 委托
  公开签名              (ctx, x, weight, eps, out)     完全相同（traced.rs 逐参数复刻）
  追踪能力              无                             有（且受运行期开关控制）
```

运行期在 feature=on 下再看一层：

```text
调用 ops::X(...) ──> traced::X
                      ├─ call_trace::is_enabled() == false → 直接委托 kernels（只花一次布尔检查）
                      └─ == true → current_label + record_call(规格) → 委托 kernels
```

#### 4.2.3 源码精读

以 `rms_norm_batch_into` 为例对照两条出口：feature 关时从 kernels 直出 [pegainfer-core/src/ops.rs:L74-L75](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L74-L75)；feature 开时改由 traced 提供 [pegainfer-core/src/ops.rs:L112-L113](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L112-L113)。这段代码声明「开着追踪时，这个名字从 traced 模块来」。

`silu_mul_fused_batch_into` 同理：kernels 侧出口在 [pegainfer-core/src/ops.rs:L88-L89](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L88-L89)，traced 侧出口在 [pegainfer-core/src/ops.rs:L114-L115](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L114-L115)（文件最后一行）。

GEMM 家族的换出口成组出现：[pegainfer-core/src/ops.rs:L51-L60](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L51-L60) 依次摘出 `gemm_into`、`gemm_into_checked`、`gemm_rows_into`、`gemm_rows_into_checked`；[pegainfer-core/src/ops.rs:L100-L109](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L100-L109) 逐个从 traced 放回。被换出的正是 decode 高频路径上的主力算子——追踪清单本身就是一份「热点算子清单」。

embedding 的对照更直观：[pegainfer-core/src/ops.rs:L34-L35](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L34-L35) 与 [pegainfer-core/src/ops.rs:L96-L97](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L96-L97) 一一对应。

最后，子模块本身的门控：[pegainfer-core/src/ops.rs:L5-L6](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L5-L6) 与 [pegainfer-core/src/ops.rs:L8-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L8-L9) 声明 `call_trace`（公开）与 `traced`（私有）只在 feature 下编译——feature 关掉时这两个文件根本不进编译单元。

#### 4.2.4 代码实践

**实践目标**：验证「换出口不改调用方代码」。

**操作步骤**：

1. 在 pegainfer-core/ 下运行 `cargo check -p pegainfer-core`（默认 feature），确认通过。
2. 再运行 `cargo check -p pegainfer-core --features kernel-call-trace`，确认同样通过（这会先编译 pegainfer-kernels 的 CUDA 部分，需要 CUDA 工具链；无工具链环境跳过实际运行，改为阅读源码完成下面第 3 步）。
3. 用 `grep -n 'rms_norm_batch_into\|silu_mul_fused_batch_into' src/ops.rs` 列出这四个出口行（应为 74/75、88/89、112/113、114/115），确认每对名字各有一条 `cfg(not)` 与一条 `cfg(feature)` 出口。

**需要观察的现象**：两次 check 都无「ambiguous re-export / 名字冲突」错误——因为任何配置下每个名字只有一条生效出口。

**预期结果**：grep 输出与 4.2.3 引用的行号一致；带 feature 的编译结果**待本地验证**（依赖 CUDA 工具链）。

#### 4.2.5 小练习与答案

1. **问**：如果把两条出口都写成无条件的 `pub use`，会发生什么？
   **答**：同名符号从两个模块各导入一次，编译期报名字冲突（duplicate/ambiguous re-export）。`cfg(not)` 与 `cfg(feature)` 的互斥是换出口能编译的前提。
2. **问**：feature 打开但从未调用 `collect_result`，行为与 feature 关闭有何差别？
   **答**：路径变为 traced 包装器，每次调用多一次 `is_enabled()` 线程局部检查（结果为 false），不记录任何东西，计算语义完全不变。即仪表常装、按需读数。
3. **问**：为什么 `gemm_into`（无返回值版本）在 traced.rs 里不自己记录，而是委托 `gemm_into_checked`？
   **答**：unchecked 只是 checked 的 `expect` 语法糖，记录点收敛在 checked 一处，避免同一次调用被记两次（见 4.4.3 的 [traced.rs:L91-L98](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L91-L98)）。

### 4.3 call_spec 调用规格

#### 4.3.1 概念说明

`call_spec.rs` 回答的问题是：**「一次 kernel 调用」如何被描述成一份可比对、可离线阅读的记录？**

它的核心类型 `KernelCall`（以及 `TensorSpec`、`AxisSpec`、`AxisTag` 等词汇）全部来自 `pegainfer_kernels::tensor`——又一次「core 拥有门面/组合逻辑、kernels 拥有类型真身」的单一事实源纪律（与 u2-l1、u2-l2 同构）。core 在这个文件里做的事情是提供一组便捷构造函数（`rms_norm_batch_call`、`gemm_call`、`silu_mul_fused_batch_call`……），把「某算子的形状参数」翻译成一份带轴语义的张量规格描述。

`call_spec` 是 `pub mod`（ops.rs L4），所以它不只服务 traced.rs——模型 crate 也能直接用它为自己的算子（不经过换出口的那些）构造记录，这是 `split_qkv_call`、`paged_decode_attention_call`、`all_reduce_hidden_call`、`suppress_logits_call` 在 core 内没有消费点的原因（core 内唯一消费方是 traced.rs 的 7 个 import；模型 crate 侧的消费点请读者自行 grep 确认）。

#### 4.3.2 核心流程

一份 `KernelCall` 的构造流程：

```text
标签 label（哪一层/哪个阶段，来自 call_trace::current_label 或调用方指定）
   │
   ▼
KernelCall::new("算子名", label)          ← 记录的身份
   │  .input("x",     TensorSpec)        ← 输入张量：dtype + 布局 + 命名轴
   │  .input("weight", TensorSpec)
   │  .output("out",  TensorSpec)
   └─ .attr("eps", "1e-5")               ← 标量属性（eps、num_heads、row_offset…）
   ▼
一份可 clone、可拼接进 Vec<KernelCall> 的调用记录
```

`TensorSpec` 的关键设计是**轴标签（AxisTag）**：不是记 `(rows, cols)` 两个裸数字，而是记 `hidden_batch::<Intermediate>(inter, batch)`——第一根轴叫 Intermediate、值是 inter；第二根轴叫 Batch。轴有了名字，离线比对两份 trace 时才不会把「hidden 维」误当「batch 维」。常见的轴标签有 `Hidden`、`Intermediate`、`Inter2`（gate·up 拼接后的 2 倍中间维）、`QDim`/`KvDim`、`Seq`、`Token`、`Page`、`Layer` 等。

#### 4.3.3 源码精读

**词汇来自 kernels**。[pegainfer-core/src/ops/call_spec.rs:L1-L33](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L1-L33) 是 33 行从 `pegainfer_kernels::tensor` 的逐项导入：`KernelCall` 在 [call_spec.rs:L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L15)，`TensorSpec`、`AxisSpec`、`AxisTag` 与全部轴标签紧随其后。

**精读样板：rms_norm_batch_call**。[pegainfer-core/src/ops/call_spec.rs:L79-L90](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L79-L90) 这 12 行就是一份完整的规格构造器：输入 `x` 与输出 `out` 都是「A 轴 × batch」的隐藏态（A 由调用处的泛型决定，traced.rs 用 `Hidden` 实例化），`weight` 是 A 轴的一维 bf16 向量，`eps` 作为标量属性附在尾部。

```rust
pub fn rms_norm_batch_call<A: AxisTag>(
    label: impl Into<String>,
    dim: usize,
    batch: usize,
    eps: f32,
) -> KernelCall {
    KernelCall::new("rms_norm_batch", label)
        .input("x", hidden_batch::<A>(dim, batch))
        .input("weight", vector::<A, Bf16>(dim))
        .output("out", hidden_batch::<A>(dim, batch))
        .attr("eps", eps.to_string())
}
```

**对照样板：silu_mul_fused_batch_call**。[pegainfer-core/src/ops/call_spec.rs:L234-L242](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L234-L242) 输入 `gate_up` 的第一轴是 `Inter2`（2×inter，gate 与 up 拼在一起），输出 `out` 的第一轴是 `Intermediate`（激活后减半）——融合激活的形状变化直接写在轴标签里。

```rust
pub fn silu_mul_fused_batch_call(
    label: impl Into<String>,
    inter: usize,
    batch: usize,
) -> KernelCall {
    KernelCall::new("silu_mul_fused_batch", label)
        .input("gate_up", hidden_batch::<Inter2>(2 * inter, batch))
        .output("out", hidden_batch::<Intermediate>(inter, batch))
}
```

**与 u2-l4 的接口**。[pegainfer-core/src/ops/call_spec.rs:L37-L41](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L37-L41) 定义 `PagedDecodePath` 枚举（`NonPartition` / `SplitKv { chunk_size, cap }`，配套的 `label()` 在 [call_spec.rs:L43-L49](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L43-L49) 把它渲染成 `"non_partition"`/`"split_kv"`）；[pegainfer-core/src/ops/call_spec.rs:L52-L65](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L52-L65) 的 `PagedDecodeCallSpec` 把 batch、页数、层数、页大小、头数、kv_len 等分页解码参数打包成值类型，供 `paged_decode_attention_call`（[call_spec.rs:L173](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L173)）使用。这两个类型与 u2-l4 要讲的 split-KV 路径一一对应。

**私有 spec 工具**。文件后段 [pegainfer-core/src/ops/call_spec.rs:L265-L325](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L265-L325) 是十个私有构造器：`hidden_batch`（L265）、`weight_matrix`（L272）、`vector`（L286）、`embed_table`（L290）、`token_ids`（L297）、`rope_cache`（L301）、`paged_kv`（L308）、`meta_i32`/`meta_u8`（L319/L323）等，把「bf16 隐藏态 × batch」「行主序权重矩阵」「u32 token id 向量」「page-first 布局的分页 KV 池」等常用形态收敛成一处——`paged_kv` 用 `PagedKvPageFirst` 布局与 Page/Layer/Kv/PosInPage/KvHead/HeadDim 六根轴描述 KV 池，正是 u3-l2 将精读的 page-first 几何在「记录侧」的镜像。

另有一处值得一看：[pegainfer-core/src/ops/call_spec.rs:L257-L263](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_spec.rs#L257-L263) 的 `all_reduce_hidden_call` 把 `tp_world_size=1`、`no_op=true` 记成属性——单卡时 all-reduce 退化为 no-op 也会留下一条记录，说明 trace 追求的是「完整还原调用形态」而不是「只记真实计算」。

#### 4.3.4 代码实践

**实践目标**：吃透规格构造器的模式，能独立起草新算子的规格。

**操作步骤**：为 ops.rs 再出口清单里的 `gelu_tanh_mul_batch_into`（[ops.rs:L47](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L47)，Gemma 系激活）起草一份规格构造器，仿照 `silu_mul_fused_batch_call` 的形状语义（gate_up → 激活输出减半）。写在你的笔记里即可，不修改项目源码：

```rust
// 示例代码（练习草稿，不进入项目）：仿 silu_mul_fused_batch_call 的模式。
pub fn gelu_tanh_mul_batch_call(
    label: impl Into<String>,
    inter: usize,
    batch: usize,
) -> KernelCall {
    KernelCall::new("gelu_tanh_mul_batch", label)
        .input("gate_up", hidden_batch::<Inter2>(2 * inter, batch))
        .output("out", hidden_batch::<Intermediate>(inter, batch))
}
```

**需要观察的现象 / 预期结果**：你的草稿与 silu 版本只差算子名字符串——如果发现需要引入新轴标签或新属性才能描述清楚，说明该算子的形状语义与 silu 不同，回头修改假设。此练习为源码阅读型，无需编译验证。

#### 4.3.5 小练习与答案

1. **问**：`KernelCall`、`TensorSpec`、`AxisTag` 定义在哪里？为什么 core 不自己定义一份？
   **答**：`pegainfer_kernels::tensor`（call_spec.rs L1-L33 逐项导入，`KernelCall` 在 L15）。单一事实源：kernels 侧如果改了规格结构，core 与模型 crate 的记录代码同步演进，不会出现两套漂移的词汇表——与 tensor/ffi 门面同一纪律。
2. **问**：`rms_norm_batch_call` 带泛型 `<A: AxisTag>`，`silu_mul_fused_batch_call` 不带，为什么？
   **答**：rms 的输入/输出是同一个「隐藏态」语义，但调用处可能是 Hidden（注意力前归一化）也可能是 Intermediate（FFN 前归一化），轴标签交给调用方（traced.rs L47 用 `::<Hidden>` 实例化）；silu 的输入固定是 Inter2、输出固定是 Intermediate，形状语义在规格内写死即可。
3. **问**：`.attr(...)` 与 `.input(...)` 记录的东西有何本质区别？
   **答**：input/output 记张量（dtype + 布局 + 命名轴 + 维度），attr 记标量元数据（eps、num_heads、row_offset）。前者描述内存形态，后者描述 kernel 行为参数；离线比对时二者缺一不可。

### 4.4 traced 包装器与 call_trace 记录器

#### 4.4.1 概念说明

`traced.rs` 是 10 个手工包装器，每个都遵循同一个三段式：

```text
pub fn X(与 kernels 完全相同的参数表) -> 与 kernels 相同的返回类型 {
    if call_trace::is_enabled() {                        // 1. 运行期守卫
        let label = call_trace::current_label("默认名");  // 2. 取上下文标签
        call_trace::record_call(某_x_call(label, 形状参数…)); // 3. 记录规格
    }
    pegainfer_kernels::ops::X(原样转发全部参数)           // 4. 委托真身
}
```

`call_trace.rs` 是被这些包装器调用的记录与收集设施：**线程局部缓冲优先、全局互斥缓冲兜底**的双通道设计，让「发起收集的线程」和「实际发射 kernel 调用的线程」可以不同——推理引擎的 executor 常在 worker 线程上跑前向。

#### 4.4.2 核心流程

一次带追踪的前向（时序图，对应本讲实践任务）：

```text
调用方（模型 crate）
   │ ops::rms_norm_batch_into(ctx, x, w, eps, &mut out)
   ▼
pegainfer_core::ops::rms_norm_batch_into        ← 同一名字，出口由 feature 决定
   │  feature=off ─────────────────────────────┐（直连 kernels，无记录）
   │  feature=on ─→ traced::rms_norm_batch_into
   │                 │ 1. call_trace::is_enabled()
   │                 │      └ false → 跳到 4
   │                 │ 2. current_label（LABEL_STACK 栈顶或默认名）
   │                 │ 3. record_call(rms_norm_batch_call::<Hidden>(
   │                 │        label, x.hidden_dim, x.seq_len, eps))
   │                 ▼ 4. 委托
   └──────────→ pegainfer_kernels::ops::rms_norm_batch_into
                    │ 参数校验、组装设备指针与维度
                    │ 异步启动 CUDA kernel（送上 ctx 的流，不等待完成）
                    ▼
              （后续算子继续上流；需要结果时由调用方按流纪律同步）
```

收集侧：`collect_result(f)` 先安装两个缓冲（线程局部 `TRACE` 与全局 `GLOBAL_TRACE`，都断言不许嵌套），跑完 `f` 后把两边记录拼接成 `Vec<KernelCall>` 随结果一起返回。`record_call` 先试线程局部，没装就推给全局。

#### 4.4.3 源码精读

**样板一：rms_norm_batch_into 包装器**。[pegainfer-core/src/ops/traced.rs:L38-L55](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L38-L55) 参数表与 kernels 真身逐参数一致（这也反过来向我们证实了 kernels 侧的公开签名），函数体就是标准三段式：

```rust
pub fn rms_norm_batch_into(
    ctx: &DeviceContext,
    x: &HiddenStates,
    weight: &DeviceVec,
    eps: f32,
    out: &mut HiddenStates,
) {
    if call_trace::is_enabled() {
        let label = call_trace::current_label("rms_norm_batch");
        call_trace::record_call(rms_norm_batch_call::<Hidden>(
            label,
            x.hidden_dim,
            x.seq_len,
            eps,
        ));
    }
    pegainfer_kernels::ops::rms_norm_batch_into(ctx, x, weight, eps, out);
}
```

**样板二：silu_mul_fused_batch_into 包装器**。[pegainfer-core/src/ops/traced.rs:L206-L220](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L206-L220)（文件最后一个函数）与样板一结构相同，差异有两点：返回 `anyhow::Result<()>`（kernel 侧有形状检查，可能失败）；记录的形状取自 `out.hidden_dim` 与 `gate_up.seq_len`——注意它**从现有张量的字段读形状**，包装器自己不做任何几何计算，规格与真实调用永远一致。

**委托链收敛记录点**。[pegainfer-core/src/ops/traced.rs:L91-L98](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L91-L98) 的 `gemm_into` 只有一行实质代码：委托 `gemm_into_checked` 并 `.expect("GEMM launch failed")`；记录发生在 [traced.rs:L100-L116](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L100-L116) 的 checked 版本里（`gemm_call::<OutDim, InDim>(label, weight.rows, weight.cols, x.seq_len)`）。`gemm_rows_into` → `gemm_rows_into_checked`（[traced.rs:L57-L89](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L57-L89)）是同样的配对。

**存储槽**。[pegainfer-core/src/ops/call_trace.rs:L9-L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L9-L15) 定义三样东西：`thread_local!` 里的 `TRACE`（`RefCell<Option<Vec<KernelCall>>>`）、`LABEL_STACK`（标签栈）与 `DECODE_KV_LEN`（当前 decode 步的 KV 长度上下文），以及全局兜底 `GLOBAL_TRACE: OnceLock<Mutex<Option<Vec<KernelCall>>>>`。`Option` 的有无就是「收集是否激活」。

**收集入口**。[pegainfer-core/src/ops/call_trace.rs:L21-L51](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L21-L51) 的 `collect_result`：先断言两个缓冲都未被占用（嵌套收集直接 panic，把误用变成显式失败），安装空缓冲 → 执行闭包 `f()` → 取回两边记录并拼接。**运行期开关**在 [call_trace.rs:L54-L60](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L54-L60)：`is_enabled()` 就是「线程局部有缓冲 或 全局有缓冲」。

**记录路由**。[pegainfer-core/src/ops/call_trace.rs:L62-L81](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L62-L81) 的 `record_call`：优先推入线程局部 `TRACE`（推入时 clone 一份留作全局备选判断），线程局部没激活就推全局缓冲——这就是跨线程收集的通路。

**标签与上下文**。[pegainfer-core/src/ops/call_trace.rs:L83-L91](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L83-L91) 的 `current_label` 取 `LABEL_STACK` 栈顶，没有则用算子默认名（如 `"rms_norm_batch"`）；[call_trace.rs:L93-L104](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L93-L104) 的 `with_decode_kv_len`/`decode_kv_len` 在线程局部保存「当前 decode 步 KV 长度」。core 内没有这两个上下文的使用者，消费方应在模型 crate（它们也 import 了 `pub mod call_trace`），**待读者检索确认**。

**内嵌测试**。[pegainfer-core/src/ops/call_trace.rs:L107-L126](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/call_trace.rs#L107-L126) 的测试模块里，`collect_result_captures_calls_from_child_thread` 在子线程里 `record_call(KernelCall::new("child_op", "child.label"))`，断言主线程 `collect_result` 收到恰好 1 条、`op` 与 `label` 字段正确——这正是「全局兜底通道」的行为证明，也说明 `KernelCall` 有公开的 `op`/`label` 字段。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：从模型 crate 视角写出两个换出口算子的最小调用用例，画出两条 cfg 出口的时序图，并核对 traced 委托链。

**操作步骤**：

1. **写最小调用用例**（示例代码，非项目原文件）。签名依据是 traced.rs 复刻的参数表（[traced.rs:L38-L55](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L38-L55)、[traced.rs:L206-L220](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L206-L220)）——由于换出口保证同名同签名，这段代码在 feature 开关两种构建下都能原样编译：

   ```rust
   // 示例代码（非项目原文件）：模型 crate 视角的最小调用形态。
   use pegainfer_core::ops::{rms_norm_batch_into, silu_mul_fused_batch_into};
   use pegainfer_core::tensor::{DeviceContext, DeviceVec, HiddenStates};

   fn one_ffn_step(
       ctx: &DeviceContext,
       normed: &HiddenStates,   // [seq, hidden]
       norm_w: &DeviceVec,      // [hidden]
       gate_up: &HiddenStates,  // [seq, 2*inter]，gate/up 已拼接
       out_buf: &mut HiddenStates,
       act_buf: &mut HiddenStates,
       eps: f32,
   ) -> anyhow::Result<()> {
       rms_norm_batch_into(ctx, normed, norm_w, eps, out_buf); // 无返回值：直接写入 out
       silu_mul_fused_batch_into(ctx, gate_up, act_buf)?;      // 返回 Result：形状检查可能失败
       Ok(())
   }
   ```

   注意两点：`rms_norm_batch_into` 无返回值（失败条件由调用方用形状纪律保证），`silu_mul_fused_batch_into` 返回 `anyhow::Result`。`HiddenStates` 的构造/分配方式在 kernels 侧定义（u2-l1 已知 core 只按字段使用 `hidden_dim`、`seq_len`），本例聚焦调用形态，**构造细节待确认**；能否真正上 GPU 运行**待本地验证**。
2. **画时序图**：对照 4.4.2 的文本时序图，分别画出 feature=off 与 feature=on 两条链路（参数准备 → 守卫/记录（仅 on）→ kernel 异步启动 → 调用方按流纪律继续），并标出 `record_call` 发生在 kernel 启动**之前**（记录的是「将要发生的调用」）。
3. **核对委托链**：阅读 [traced.rs:L91-L116](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/traced.rs#L91-L116)，确认 unchecked→checked 的委托让 GEMM 家族每个名字只记一次。
4. **统计占比**：完成 4.1.4 的 grep 统计，得出归一化/激活类占比（参考答案：16/64 = 25%），并写一句你的解读。

**需要观察的现象**（若在有 GPU/CUDA 工具链的环境实际开启 feature 运行）：`collect_result` 返回的 `Vec<KernelCall>` 里，每层 decode 前向应出现成对的 `rms_norm_batch` 与 `gemm` 记录，`silu_mul_fused_batch` 记录的 gate_up 第一轴是 2×inter。

**预期结果**：源码核对部分（步骤 2-4）可完全离线完成；GPU 部分待本地验证。

#### 4.4.5 小练习与答案

1. **问**：`record_call` 为什么先试线程局部 `TRACE`、失败再推全局 `GLOBAL_TRACE`？
   **答**：发起收集的线程希望先接住本线程的记录（快、无锁）；但 kernel 调用可能发生在别的线程（executor worker），线程局部没装缓冲时落到全局互斥缓冲，仍能被 `collect_result` 收走——内嵌测试专门验证了这条子线程通路。
2. **问**：`collect_result` 为什么用断言禁止嵌套？
   **答**：缓冲的「激活」语义靠 `Option<Vec>` 的占位表达，嵌套的第二层会把第一层的缓冲顶掉或混淆两条记录流；断言让这种误用在第一时间显式失败，而不是产出错误的 trace。
3. **问**：`current_label("rms_norm_batch")` 的默认名什么时候生效？
   **答**：`LABEL_STACK` 为空（没有任何调用方压入标签）时。也就是说追踪记录至少能落到「算子名」粒度，调用方压栈（例如标出层号）才能到「层内位置」粒度。

## 5. 综合实践

**任务：为一个 decoder 前向编制「算子账单」。**

1. **清单编制（离线，必做）**。假想一个 Qwen3 风格的 decoder 层前向，按执行顺序列出它会用到的 `pegainfer_core::ops::` 名字：`embedding_batch` → `rms_norm_batch_into` → `gemm_into`（QKV 投影）→ `qk_norm_rope_batch_decode_into` → 注意力入口（`paged_attention_batch_decode_into` 或 split-KV 版）→ `gemm_into`（输出投影）→ `scaled_add_batch_into`（残差）→ `fused_add_rms_norm_batch_into` → `gemm_into`（gate/up）→ `silu_mul_fused_batch_into` → `gemm_into`（down）→ `scaled_add_batch_into` →（最后一步采样）`argmax_batch_bf16_into`。为每个名字标注它在 [ops.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L11-L115) 中的行号。
2. **换出口比对**。在你的账单上标出哪些名字属于 10 个换出口符号（会被 traced 自动记录），哪些不在其中（如 `scaled_add_batch_into`、`argmax_batch_bf16_into`——它们开着 feature 也直连 kernels）。再对照 call_spec.rs 的构造器清单，指出哪些 `*_call`（如 `split_qkv_call`、`paged_decode_attention_call`、`all_reduce_hidden_call`）在 core 内没有 traced 消费点——推断：模型 crate 经 `pegainfer_core::ops::call_spec::` 自行记录这些调用（**待检索模型 crate 确认**，建议在 pegainfer-qwen3 内 `grep -rn 'call_spec'`）。
3. **运行验证（可选，需 GPU + CUDA 工具链）**。`cargo test --release -p pegainfer-core --lib --features kernel-call-trace` 跑通 call_trace.rs 的内嵌测试；进一步在模型 crate 侧用 `collect_result` 包一次前向并打印记录序列，与第 1 步的账单逐条比对。**待本地验证**。

## 6. 本讲小结

- `ops.rs` 是 core 的第三张门面、也是唯一「结构化」的一张：逐项点名再出口 69 项 kernels 算子 + 6 项 core 原生入口（attention/paged_plan）+ 10 项 traced 包装器，共 85 个名字挤在同一个 `pegainfer_core::ops` 命名空间。
- 清单形态映照前向形态：归一化/激活变体最多（16/64），命名后缀（`_into`/`_batch`/`_rows`/`graphsafe`/融合前缀）分别对应输出复用、整段批处理、行子集、CUDA Graph 兼容与访存融合。
- `kernel-call-trace` 是「编译期 cfg 换出口 + 运行期 `is_enabled` 守卫」的双层开关：10 个热点符号在 feature 开启时改由 traced.rs 提供同名同签名包装器，调用方代码零改动。
- `call_spec.rs` 用 kernels::tensor 的 `KernelCall`/`TensorSpec`/`AxisTag` 词汇构造带命名轴的调用规格；`paged_decode_attention_call` 与 `PagedDecodeCallSpec` 是通往 u2-l4 的接口。
- `call_trace.rs` 的双通道设计（线程局部优先、全局互斥兜底、断言禁嵌套）让跨线程的 kernel 调用也能被一次 `collect_result` 收齐。
- `attention.rs` 与 `paged_plan.rs` 是 core 在纯再出口之外**自己实现**的两个子模块——下一讲的主角。

## 7. 下一步学习建议

- **下一讲 u2-l4**：精读 `ops/attention.rs` 的三个注意力入口与 `ops/paged_plan.rs` 的 `PrefillPagedPlan`/`SplitKvCsr`/`build_split_kv_csr`，并与 `kv_pool.rs` 的 `KvDesc` 对拍，理解「core 描述状态、kernels 消费状态」的契约。本讲的 `PagedDecodePath::SplitKv` 与 `paged_kv` 规格构造器是那边的伏笔。
- **横向检索**：在 pegainfer-qwen3（默认 feature 的模型 crate）里 `grep -rn 'pegainfer_core::ops'`，验证 4.4 的账单与 call_spec 的模型侧消费点；这是 u4-l3（模型 crate 消费面）的预演。
- **向下钻取（可选）**：`KernelCall`/`TensorSpec`/`AxisTag` 的真身在 `pegainfer-kernels/src` 的 tensor 模块（本讲义引用范围之外）；想理解 `gemm_graphsafe_*` 为何存在，等 u4-l1 的 CUDA Graph 讲。
