# u2-l3 · ops.rs：共享 GPU 算子封装总览

## 1. 本讲目标

读完本讲，你应该能够：

1. 说清 `ops.rs` 的门面定位：它把 `pegainfer-kernels` 的底层算子整理成模型 crate 可以直接 `use` 的稳定路径，模型 crate 从不直接碰 kernels 的 ops。
2. 数出这个门面的全部公开名字（当前 HEAD 共 75 个），并按「归一化/激活、GEMM/GEMV、注意力、gather/extract、逐元素/缩放、embedding/采样」等类别归类。
3. 解释 `kernel-call-trace` feature 的「cfg 换出口」机制：同一个名字（如 `rms_norm_batch_into`）在开关该 feature 时分别从 kernels 直出或从 `traced.rs` 包装导出，调用方代码一行不改。
4. 读懂 `call_spec.rs` 的「调用规格」体系（`KernelCall` + `TensorSpec` + 语义轴标签），理解为什么追踪记录的是形状规格而不是数据。

## 2. 前置知识

本讲建立在 u2-l1（tensor 门面）和 u2-l2（FFI 边界）之上，先复习三个词：

- **门面与再出口（re-export）**：一个模块自己不定义东西，只用 `pub use 别处::名字;` 把别处的名字转手暴露。tensor.rs 和 ffi.rs 各是一行门面；ops.rs 是「大宗门面 + 少量本地包装」。
- **`_into` 后缀**：项目惯例，表示「结果写入调用者预分配的输出缓冲」，函数内部不做设备内存分配。这是 decode 路径能被 CUDA Graph 捕获的前提（图回放要求地址不变，见 u4-l1）。
- **feature 门控**：Cargo 的编译期开关。core 在 `Cargo.toml` 里只声明了一个非默认 feature——`kernel-call-trace`（[Cargo.toml:34-36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/Cargo.toml#L34-L36)），它不引入任何依赖，纯粹是一个「换出口」开关。

再补两个本讲新词：

- **cfg 换出口**：同名符号在不同 feature 组合下从不同模块导出。Rust 里 `#[cfg(not(feature = "x"))] pub use a::f;` + `#[cfg(feature = "x")] pub use b::f;` 是标准写法，两个 `use` 永远只有一个被编译。
- **KernelCall / TensorSpec**：一次 kernel 调用的「规格快照」——算子名、标签、每个输入/输出的形状（含语义轴标签与 dtype）、若干属性。记录规格而非数据，既零拷贝开销又可序列化成报告。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|------|------|------|
| [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L1-L115) | 115 | 本讲主角：算子门面。声明 5 个子模块，再出口 75 个公开名字 |
| [src/ops/traced.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L1-L220) | 220 | 10 个「记录后转发」包装器，仅 `kernel-call-trace` 开启时编译 |
| [src/ops/call_spec.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L1-L325) | 325 | 每个被追踪算子的 `KernelCall` 规格构造器 + 语义轴辅助函数 |
| [src/ops/call_trace.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_trace.rs#L1-L126) | 126 | 追踪收集器本体：thread-local + 全局双通道，仅在 feature 开启时编译 |
| [src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L1-L146) | 146 | 三个分页注意力入口（下一讲 u2-l4 精读，本讲只看它在门面中的位置） |
| [src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tensor.rs#L1-L1) | 1 | tensor 门面（u2-l1 已讲），ops 的参数类型全部来自它 |

类型真身在 kernels 侧（本讲引用签名时给出行号，深入留待需要时再读）：
[pegainfer-kernels/src/ops/norm.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L550-L558)、[pegainfer-kernels/src/ops/elementwise.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/elementwise.rs#L803-L811)、[pegainfer-kernels/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L309-L315)。

## 4. 核心概念与源码讲解

### 4.1 算子再出口门面

#### 4.1.1 概念说明

`ops.rs` 的头注释一句话说清了它是什么：**Shared GPU operation wrappers and kernel-crate re-exports**（共享 GPU 算子封装与 kernel crate 再出口）。它解决的问题是：kernels 侧的算子数量多、分组细（norm.rs、elementwise.rs、gemm 系列等分文件存放），而模型 crate 需要的是一条稳定的扁平路径 `pegainfer_core::ops::某个算子`。core 站在中间做整理：

- 大部分名字**原样再出口**，一行一个，不加任何逻辑；
- 少数名字（attention 三个入口）在 core 侧**薄包装**——把 core 自己的 `KvLayout` 转成 kernels 要的 `kernel_layout()`，这一层是 core 真正「拥有」的胶水（u2-l4 详讲）。

这样形成的分层契约是：**模型 crate 只 use core 的 ops，从不直接 use kernels 的 ops**；kernels 想改内部文件组织，只要 ops.rs 的名字清单不变，所有模型 crate 都不用改。

#### 4.1.2 核心流程

ops.rs 的骨架可以画成：

```text
src/ops.rs（门面，115 行）
├─ 模块声明（3-9 行）
│   ├─ mod attention;            私有：3 个分页注意力薄包装
│   ├─ pub mod call_spec;        公开：KernelCall 规格构造器
│   ├─ pub mod call_trace;       公开（仅 kernel-call-trace）：收集器
│   ├─ mod paged_plan;           私有：PrefillPagedPlan/SplitKvCsr
│   └─ mod traced;               私有（仅 kernel-call-trace）：10 个包装器
├─ 本地出口（11-16 行）：attention 3 个函数 + paged_plan 3 个名字
├─ kernels 大宗再出口（17-95 行）：69 条语句
│   └─ 其中 10 条带 #[cfg(not(feature = "kernel-call-trace"))]
└─ traced 出口（96-115 行）：10 条，全部 #[cfg(feature = "kernel-call-trace")]
```

一个关键的不变量：**无论 feature 开关如何，`pegainfer_core::ops` 对外的名字集合完全相同**（当前 75 个名字）。feature 只决定其中 10 个名字从哪条路径导出。

#### 4.1.3 源码精读

**模块声明**——注意 `attention` 和 `paged_plan` 是私有模块，外界只能通过 ops.rs 顶层的再出口访问它们：

- [src/ops.rs:3-9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L3-L9)：声明 5 个子模块，其中 `call_spec` 无条件公开、`call_trace` 仅在 feature 下编译并公开、`traced` 仅在 feature 下编译且保持私有。

**本地出口**——core 自己包装的部分只有 6 个名字：

- [src/ops.rs:11-16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L11-L16)：转出 attention 的三个注意力入口与 paged_plan 的 `PrefillPagedPlan`、`SplitKvCsr`、`build_split_kv_csr`。

**大宗再出口**——69 条 `pub use pegainfer_kernels::ops::...`，值得注意开头几条转的不是函数而是类型与常量：

- [src/ops.rs:17-21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L17-L21)：再出口 `GEMM_LT_MAX_N`（cublasLt 的 N 上限常量）、`GatedNormActivation`、`LoraDecodeGroupedProjection`、`SUPPORTED_GQA_GROUP_SIZES`、`SuppressIds`——门面不只转函数，也转供调用方做能力探测的常量与类型。

**换出口样本**——以 `rms_norm_batch_into` 为例，两条互斥的出口：

- [src/ops.rs:74-75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L74-L75)：默认（feature 关）时直接从 kernels 再出口，零额外开销。
- [src/ops.rs:112-113](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L112-L113)：feature 开时改从 `traced` 模块导出同名函数。

**kernels 侧真身签名**（用来对照 traced.rs 是否忠实）：

- [pegainfer-kernels/src/ops/norm.rs:551-558](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L551-L558)：`rms_norm_batch_into(ctx, x: &HiddenStates, weight: &DeviceVec, eps: f32, out: &mut HiddenStates)`，返回单元类型，注释明确「Batched RMSNorm into pre-allocated output buffer (zero allocation)」。
- [pegainfer-kernels/src/ops/elementwise.rs:803-811](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/elementwise.rs#L803-L811)：`silu_mul_fused_batch_into(ctx, gate_up: &HiddenStates, out: &mut HiddenStates) -> Result<()>`，从 `[2I, B]` 的交织 gate+up 缓冲直接产出 `[I, B]`，免反交织。

**真实消费点**（模型 crate 视角）：

- [pegainfer-qwen3/src/unified_forward.rs:500-506](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L500-L506)：Qwen3 前向第一步就是 `ops::rms_norm_batch_into(&self.ctx, hidden, &layer.input_layernorm, self.config.rms_norm_eps, &mut bufs.normed);`——注意它拿的是 `&mut bufs.normed` 预分配缓冲，不是让算子自己分配。

**75 个名字的分类清单**（按当前 HEAD 逐条统计，69 条 kernels 再出口 + 6 条本地）：

| 类别 | 数量 | 代表名字 |
|------|------|----------|
| 归一化/激活 | 18（含 `GatedNormActivation` 类型） | `rms_norm` 家族 10 个变体、`fused_add_rms_norm_*`、`silu_mul_*`、`gelu_tanh_mul_batch_into`、`softcap_bf16_in_place` |
| 归一化+RoPE 融合 | 3 | `dflash_qk_norm_rope_into`、`eagle3_rope_into`、`qk_norm_rope_batch_decode_into` |
| GEMM/GEMV/linear/LoRA | 17（含 `GEMM_LT_MAX_N`、`LoraDecodeGroupedProjection`） | `gemm*` 8 个、`gemv`、`linear`、`gemm_lt_tune`、`lora_*` 3 个 |
| 注意力 | 7（本地 3 + kernels 4，含 `SUPPORTED_GQA_GROUP_SIZES`） | `paged_attention_batch_decode_into`（本地）、`single_prefill_nhd_causal_into` |
| gather/extract/copy | 9 | `extract_vec*` 4 个、`gather_hidden_tokens_into`、`copy_hidden_rows_into`、`write_vec_into` |
| 逐元素/缩放/精度转换 | 10 | `add_batch*`、`scaled_add_*` 4 个、`scale_bf16_in_place`、`bf16_hidden_to_f32_into` |
| embedding/采样/输出 | 7（含 `SuppressIds` 类型） | `embedding_batch`、`embedding_decode_into`、`argmax*`、`suppress_logits_bf16_in_place` |
| paged plan 数据结构 | 3 | `PrefillPagedPlan`、`SplitKvCsr`、`build_split_kv_csr` |
| 步进元数据 | 1 | `advance_decode_metadata` |

（18+3+17+7+9+10+7+3+1 = 75。核对你自己数出的结果时，注意 cfg 属性行不是 `pub use` 行。）

#### 4.1.4 代码实践

**实践目标**：亲手数出门面清单，并写出一个模型 crate 视角的最小调用用例。

**操作步骤**：

1. 在 `pegainfer-core` 目录下运行三条统计命令：

   ```bash
   grep -c 'pub use pegainfer_kernels::ops::' src/ops.rs   # 预期 69
   grep -c 'cfg(not(feature = "kernel-call-trace"))' src/ops.rs  # 预期 10
   grep -c 'pub use traced::' src/ops.rs                   # 预期 10
   ```

2. 写出最小调用用例。以下为**示例代码**（非项目原有代码），签名严格对照上面引用的 norm.rs / elementwise.rs 真身与 tensor 门面类型：

   ```rust
   // 示例代码：模型 crate 视角的最小调用
   use pegainfer_core::ops::{rms_norm_batch_into, silu_mul_fused_batch_into};
   use pegainfer_core::tensor::{DeviceContext, DeviceVec, HiddenStates};

   let ctx = DeviceContext::new()?;              // 拿到 ctx.ctx 与 ctx.stream
   let hidden = 2560_usize;                      // 例：Qwen3-4B 的 hidden_size
   let inter = 9728_usize;                       // 例：intermediate_size
   let batch = 4_usize;                          // 本步 token 数

   // RMSNorm 权重：长度 = hidden 的 bf16 向量，从宿主一次性搬上 GPU
   let weight = DeviceVec::from_host(&ctx, &norm_weight_bf16)?; // &[bf16]

   // 输入与输出全部预分配（zeros 内部走 ctx.stream.alloc_zeros）
   let mut x = HiddenStates::zeros(&ctx, hidden, batch)?;
   let mut norm = HiddenStates::zeros(&ctx, hidden, batch)?;

   // ① 归一化：x[H,B] --weight,eps--> norm[H,B]（无返回值，失败会 panic 于内部 expect）
   rms_norm_batch_into(&ctx, &x, &weight, 1e-6, &mut norm);

   // ② 激活：gate_up[2I,B] --> act[I,B]，一步融合 SiLU(gate)*up
   let mut gate_up = HiddenStates::zeros(&ctx, 2 * inter, batch)?;
   let mut act = HiddenStates::zeros(&ctx, inter, batch)?;
   silu_mul_fused_batch_into(&ctx, &gate_up, &mut act)?;
   ```

   类型构造依据：`HiddenStates::zeros`（[pegainfer-kernels/src/tensor.rs:597](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L597-L606)）、`DeviceVec::from_host`（[tensor.rs:415](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L415-L424)）。

**需要观察的现象**：三条 grep 的计数与讲义表格是否一致；示例代码里两个算子的参数顺序（`weight` 在 `x` 之后、`eps` 在中间）与真实调用点 unified_forward.rs:500-506 是否一致。

**预期结果**：计数为 69 / 10 / 10；示例代码能在带 GPU 的机器上编译（把 `norm_weight_bf16` 换成任意 `vec![half::bf16::ONE; hidden]`）。**运行验证待本地验证**（本环境无 GPU，仅完成源码对照）。

#### 4.1.5 小练习与答案

**练习 1**：`GEMM_LT_MAX_N` 和 `SUPPORTED_GQA_GROUP_SIZES` 是函数吗？模型 crate 为什么需要它们出现在 ops 门面上？

**答案**：不是，是常量（`GEMM_LT_MAX_N` 是 cublasLt 路径的 N 维上限，`SUPPORTED_GQA_GROUP_SIZES` 是注意力 kernel 支持的 GQA 分组尺寸集合）。模型 crate 需要在**启动期**做能力探测——比如判断某组 head 数能否走该 kernel、某个 GEMM 形状是否超限——这类决策属于模型 crate 的装配逻辑，所以常量也必须从门面出。

**练习 2**：`rms_norm_batch_into` 与 `silu_mul_fused_batch_into` 的返回类型为什么一个是 `()` 一个是 `Result<()>`？

**答案**：以 kernels 真身为准：norm.rs:552-558 返回单元类型，elementwise.rs:805-809 返回 `Result<()>`。这不是随意的：RMSNorm 的形状校验在更外层完成、kernel 启动参数总能构造成功；而 fused SiLU-mul 走 cublasLt/复杂启动路径，可能因形状或驱动原因启动失败，需要把错误交还调用方。签名差异由 kernels 决定，traced.rs 包装器必须逐字照抄（见 4.4）。

### 4.2 cfg 换出口：kernel-call-trace

#### 4.2.1 概念说明

`kernel-call-trace` 是 core 唯一的非默认 feature，作用是回答一个问题：**「一次前向到底启动了哪些 kernel、什么形状？」** 而且要求做到——**调用方（模型 crate）一行代码都不改**。

实现手段就是 cfg 换出口：10 个高频算子的名字，默认从 kernels 直出（快）；开 feature 后改从 traced.rs 导出（先记录规格、再转发给 kernels 同名函数）。因为 traced.rs 的函数签名与 kernels 逐字一致，`use pegainfer_core::ops::gemm_into;` 的代码在两种编译下都成立。

feature 还有一层**转发链**：模型 crate 用自己的 feature 名转发 core 的开关，例如 [pegainfer-qwen3/Cargo.toml:35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/Cargo.toml#L35-L44) 声明 `kernel-call-trace = ["pegainfer-core/kernel-call-trace"]`，且它的 `kernel-report` feature 依赖 `kernel-call-trace`——所以 `cargo run --features kernel-report` 一条命令就同时点亮了 core 的换出口和 qwen3 的报告工具链。

#### 4.2.2 核心流程

被换出口的 10 个名字（左边是 feature 关时的 kernels 直出行，右边是 feature 开时的 traced 出口行）：

| 名字 | kernels 直出 | traced 出口 |
|------|--------------|-------------|
| `embedding_batch` | ops.rs:35 | ops.rs:97 |
| `fused_add_rms_norm_batch_into` | ops.rs:44 | ops.rs:99 |
| `gemm_into` | ops.rs:52 | ops.rs:101 |
| `gemm_into_checked` | ops.rs:54 | ops.rs:103 |
| `gemm_rows_into` | ops.rs:58 | ops.rs:105 |
| `gemm_rows_into_checked` | ops.rs:60 | ops.rs:107 |
| `gemm_token_range_into_checked` | ops.rs:62 | ops.rs:109 |
| `qk_norm_rope_batch_decode_into` | ops.rs:69 | ops.rs:111 |
| `rms_norm_batch_into` | ops.rs:75 | ops.rs:113 |
| `silu_mul_fused_batch_into` | ops.rs:89 | ops.rs:115 |

选中这 10 个是有意图的：embedding、GEMM 族、norm 族、激活、qk-norm+RoPE——恰好覆盖 dense Transformer 前向里**每层都跑一遍**的骨架算子，追踪它们就等于拿到了前向的算子组成（per-op、by-call-site 报告的数据源，见 [docs/subsystems/kernels/kernel-op-reports.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/subsystems/kernels/kernel-op-reports.md#L6) 的 TL;DR：qwen3 的 `qwen3_model_report` 正是「禁 CUDA Graph → 追踪 BatchDecodeDag → 把 traced TensorSpecs 与微基准结果 join 出报告」）。

#### 4.2.3 源码精读

- [src/ops.rs:34-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L34-L35) 与 [src/ops.rs:96-97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L96-L97)：`embedding_batch` 的两条互斥出口——`#[cfg(not(...))] pub use pegainfer_kernels::ops::embedding_batch;` 对 `#[cfg(...)] pub use traced::embedding_batch;`。两条永远只有一条参与编译，不存在重定义。
- [src/ops.rs:88-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L88-L89) 与 [src/ops.rs:114-115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L114-L115)：`silu_mul_fused_batch_into` 的同款配对，本讲实践的主角之二。
- [Cargo.toml:34-36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/Cargo.toml#L34-L36)：`default = []`、`kernel-call-trace = []`——空依赖列表说明这个 feature 不拉任何新 crate，纯粹是编译期代码选择，这也是「零成本换出口」的底气。

#### 4.2.4 代码实践

**实践目标**：画出 `rms_norm_batch_into` 在两种编译下的完整时序，纠正一个想当然的顺序。

**操作步骤**：

1. 读 traced.rs 的 [38-55 行](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L38-L55)：先 `is_enabled()` 判断，再 `record_call(...)`，最后才调 `pegainfer_kernels::ops::rms_norm_batch_into(...)`。
2. 对照默认编译（kernels 直出，无任何中间层）。
3. 画出两条时序（推荐用文字版，如下）。

**需要观察的现象**：注意记录发生在 kernel 启动**之前**——规格是宿主侧元数据，必须在启动前快照；同时两个算子内部都**没有**流同步，它们只是把 kernel 排进 `ctx.stream`，同步点由调用方在 API 边界控制。所以常见直觉「先启动再记录」和「算子内同步」都是错的：

```text
默认编译（feature 关）：
  调用方: rms_norm_batch_into(ctx, x, w, eps, out)
    └─> kernels: 校验形状 → 在 ctx.stream 上排入 kernel → 立即返回（异步）

追踪编译（feature 开）：
  调用方: rms_norm_batch_into(ctx, x, w, eps, out)      ← 同一行代码
    └─> traced: is_enabled()? ──否──> 直接转发 kernels（同上）
                      │是
                      ├─ current_label("rms_norm_batch") 取标签
                      ├─ record_call(rms_norm_batch_call(label, x.hidden_dim, x.seq_len, eps))
                      │    └─ 规格入 thread-local（或全局）Vec<KernelCall>
                      └─> 转发 kernels → 排入 kernel → 返回
  （流同步发生在调用方边界，不在算子里）
```

**预期结果**：一张能明确标出「检查 → 记录 → 启动」三段、且两条路径共享同一签名的时序图。本实践为源码阅读型，无需 GPU，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么换出口选在 core 的 ops.rs 做，而不是让模型 crate 自己包一层 `traced_gemm_into`？

**答案**：三点。(1) 调用方零改动：换出口发生在 `use` 解析处，模型 crate 的源文件与两种编译完全无关；(2) 单一事实源：签名一致性的维护点集中在 traced.rs 一处，若各模型 crate 自己包，签名漂移会在 N 处发生；(3) 报告可比性：所有模型 crate 共享同一套 call_spec 规格定义，追踪出的报告才能跨模型 join 微基准数据。

**练习 2**：如果给一个**返回 `Result<()>`** 的算子（如 `gemm_into_checked`）写 traced 包装，返回路径要注意什么？

**答案**：先记录、再转发、把 kernels 的 `Result` 原样 `?`/return 回去，**不能吞掉或提前返回**——否则失败路径的调用也会丢失记录，或更糟：记录了但没真正启动。对照 traced.rs 的 [100-116 行](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L100-L116)（`gemm_into_checked`：记录后直接 `pegainfer_kernels::ops::gemm_into_checked(...)` 尾调用返回其 Result）。

### 4.3 call_spec 调用规格

#### 4.3.1 概念说明

追踪要回答「跑了什么形状」，最朴素的办法是记指针和字节数——但指针跨进程无意义，字节数不表达语义。core 的选择是记**规格**：用 `TensorSpec` 描述每个张量的 dtype、布局和每个维度的「语义轴」（AxisTag）。

语义轴是这套体系的灵魂：同样是 `2560`，作为 `Hidden`（隐藏维）和 `Vocab`（词表维）在前向里的意义完全不同。给轴打上 `Hidden`/`QDim`/`Inter2`/`PageSlot` 这类标签后，报告工具才能按「隐藏维上的形状分布」「分页槽位规模」这类问题聚合统计。`KernelCall` 则把一次调用装订成：算子名 + 标签 + 输入规格表 + 输出规格表 + 属性表。

#### 4.3.2 核心流程

```text
traced.rs 包装器                 call_spec.rs                     kernels tensor.rs
────────────────                ────────────────                 ─────────────────
rms_norm_batch_into(ctx,x,w,eps,out)
  │ 读取 x.hidden_dim / x.seq_len
  ├─> rms_norm_batch_call::<Hidden>(label, dim, batch, eps)
  │      │ KernelCall::new("rms_norm_batch", label)
  │      │   .input("x",  hidden_batch::<Hidden>(dim, batch))   ← TensorSpec: Bf16 × HiddenStatesLayout × [Hidden(dim), Batch(batch)]
  │      │   .input("weight", vector::<Hidden,Bf16>(dim))
  │      │   .output("out", hidden_batch::<Hidden>(dim, batch))
  │      │   .attr("eps", eps.to_string())
  │      └─ 返回 KernelCall
  └─> call_trace::record_call(call)  ──进入 Vec<KernelCall>──
```

维度语义的对照表（AxisTag → 含义 → 典型持有者）摘录：

| AxisTag | 语义 | 典型出现 |
|---------|------|----------|
| `Hidden` | 隐藏维 | 各 norm/gemm 的主维度 |
| `Batch` / `BatchPlusOne` | 本步 token 数 / CSR 前缀和长度 | 几乎所有 `[D, B]` 张量的第二轴 |
| `QDim` / `KvDim` | 全部 Q 头展平宽 / 全部 KV 头展平宽 | 注意力 q/k/v |
| `Inter2` / `Intermediate` | gate+up 交织宽 `2I` / 激活输出宽 `I` | silu_mul_fused |
| `OutTotal` | qkv 融合投影总宽 `Q+2KV` | split_qkv |
| `Page` / `Layer` / `Kv` / `PosInPage` / `KvHead` / `HeadDim` | paged KV 六维 | kv_buffer 规格 |
| `PageSlot` / `Tile` | 分页槽位 / split-KV 分块 | split-KV 元数据 |
| `Vocab` / `Token` | 词表 / token id | embedding |
| `Seq` / `RopeDim` | RoPE 表序列长 / 每头旋转维 | cos/sin cache |

#### 4.3.3 源码精读

- [src/ops/call_spec.rs:79-90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L79-L90)：`rms_norm_batch_call` 构造器——泛型参数 `A: AxisTag` 由 traced.rs 具体化为 `Hidden`（见 traced.rs:47），把「哪个语义轴」的决定权留在规格层。
- [src/ops/call_spec.rs:234-242](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L234-L242)：`silu_mul_fused_batch_call`——输入 `gate_up` 标 `Inter2` 轴（宽 `2*inter`），输出标 `Intermediate`（宽 `inter`），一对轴名直接讲清了融合语义。
- [src/ops/call_spec.rs:265-270](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L265-L270)：私有辅助 `hidden_batch`——所有 `[D, B]` 张量统一标 `Bf16 + HiddenStatesLayout`（隐藏维在前、batch 在后的布局），这就是全引擎 `HiddenStates` 的标准形状语法。
- [src/ops/call_spec.rs:36-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L36-L50)：`PagedDecodePath` 枚举区分非分区与 split-KV 两条 decode 路径，`label()` 把它编成报告可读的字符串。
- [src/ops/call_spec.rs:173-232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L173-L232)：最长的构造器 `paged_decode_attention_call`——按 `PagedDecodePath` 分支追加不同的输入集合（split-KV 多出 `split_tmp_v/s` 等缓冲），并复刻 [call_spec.rs:308-317](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_spec.rs#L308-L317) 的六维 paged-KV 规格（与 u3-l2 将讲的 `PagedKvPageFirst` 布局一一对应）。
- 真身结构：[pegainfer-kernels/src/tensor.rs:309-315](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L309-L315) 定义 `KernelCall { op, label, inputs: Vec<TensorArg>, outputs, attrs: Vec<AttrSpec> }`，`new/input/output/attr` 是链式构造器。**注意 `KernelCall` 与 `TensorSpec` 本体定义在 kernels 的 tensor 模块**——core 的 call_spec 只是用它们拼规格。

#### 4.3.4 代码实践

**实践目标**：为一个新的假想算子写一份 call_spec，检验你是否掌握了「语义轴」的用法。

**操作步骤**：

1. 通读 call_spec.rs 的公有构造器清单（embedding_batch / rms_norm_batch / fused_add_rms_norm_batch / gemm_rows / suppress_logits / gemm / qk_norm_rope_batch_decode / paged_decode_attention / silu_mul_fused_batch / split_qkv / all_reduce_hidden）。
2. 假设要给 `argmax_batch_bf16_into`（ops.rs:27 已再出口，但暂无 traced 包装）补一个 `argmax_batch_call`，写出构造器。

**需要观察的现象**：你会遇到一个真实的设计约束——`argmax` 的输入是 `[rows, batch]` 的 logits（bf16），输出是 `[batch]` 的 i32 token id。输入可复用 `hidden_batch::<A>`，但输出没有现成辅助函数（`meta_i32::<Batch>` 正合适，见 call_spec.rs:319-321）。

**预期结果**（参考写法，示例代码）：

```rust
// 示例代码：为 argmax_batch_bf16_into 补 call_spec
pub fn argmax_batch_call<A: AxisTag>(
    label: impl Into<String>,
    rows: usize,   // logits 宽度（通常是 vocab）
    batch: usize,
) -> KernelCall {
    KernelCall::new("argmax_batch", label)
        .input("logits", hidden_batch::<A>(rows, batch))
        .output("token_ids", meta_i32::<Batch>(batch))
}
```

若要让它真正生效，还需在 traced.rs 加包装 + ops.rs 加换出口——本实践只到规格层。无 GPU 也能完成（纯阅读 + 写规格）；编译验证可另开临时 crate 引 core 的 `call_spec` 模块（它是 `pub mod`，ops.rs:4），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`hidden_batch::<Inter2>(2 * inter, batch)` 与 `hidden_batch::<Intermediate>(inter, batch)` 如果都只记数字，报告还能区分 gate_up 和 out 吗？

**答案**：能区分宽度差异，但**失去语义**。宽度相同形状不同的张量（例如 `[2I, B]` 的 gate_up 和某个恰好等宽的 `[D, B]` 权重）会混在一起；语义轴让 by-op/by-call-site 报告能按「这是激活前的交织缓冲」归类，这正是报告要 join 微基准的前提。

**练习 2**：`paged_decode_attention_call` 为什么不把 split-KV 的十来个额外输入无条件全部记上，而要走 `match spec.path` 分支？

**答案**：因为两条路径的**输入集合本来就不同**（NonPartition 只有 `kv_tile_indices/kv_chunk_size`，SplitKv 有 `split_request_indices` 等六七个缓冲，见 call_spec.rs:195-229）。规格要忠实反映实际启动的 kernel 签名，记不存在的输入会让报告与微基准 join 时错位；`variant` 属性（call_spec.rs:193）再给报告一个显式的路径标签。

### 4.4 traced 包装器

#### 4.4.1 概念说明

traced.rs 是换出口的「另一侧」：10 个函数，每个都是同一个三段式模板——**检查 `is_enabled()` → 记录规格 → 尾调用 kernels 同名函数**。它是整个 core 里「装饰器模式」的标准样本：包装不改语义、不改签名、不加同步，唯一的副作用是可能往追踪缓冲里 push 一条 `KernelCall`。

它同时承担一个容易被忽略的责任：**签名保真**。包装器的参数列表必须与 kernels 真身逐字一致（参数名、类型、顺序、返回类型），否则换出口会改变公共 API，调用方就得随 feature 改代码——那套机制就白设计了。

#### 4.4.2 核心流程

每个包装器遵循：

```text
fn op(参数与 kernels 完全一致) -> 返回类型与 kernels 一致 {
    if call_trace::is_enabled() {                       // ① 快速路径：未启用时仅一次判断
        let label = call_trace::current_label("op名");  // ② 取标签（栈空则用默认名）
        call_trace::record_call(<对应>_call(label, 从参数里摘出的形状));
    }                                                    // ③ 规格快照入缓冲
    pegainfer_kernels::ops::op(同样的参数逐个转发)       // ④ 真正启动
}
```

两个细节值得注意：

- **unchecked 变体委托 checked 变体**：`gemm_rows_into`（traced.rs:57-67）不是转发 kernels 的 unchecked 版，而是调用本模块的 `gemm_rows_into_checked` 再 `.expect("GEMM row-range launch failed")`——这样 unchecked 路径的调用同样被记录，且错误信息由这一处统一。
- **记录用的形状摘取有讲究**：`qk_norm_rope_batch_decode_into` 从 `cos_cache.len / head_dim` 反推 `rope_seq`（traced.rs:155），`gemm_token_range_into_checked` 记 `out.seq_len` 而非 `x.seq_len`（traced.rs:131，token-range 语义下输出行数才是本次 GEMM 的真实规模）。

收集侧的双通道：thread-local `TRACE` 优先，全局 `GLOBAL_TRACE`（`OnceLock<Mutex<...>>`）兜底——兜底通道专门捕获**子线程**里发出的 kernel 调用（多线程调度器场景），call_trace.rs:110-125 的内嵌测试验证的正是这一点。

#### 4.4.3 源码精读

- [src/ops/traced.rs:20-36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L20-L36)：`embedding_batch`——最标准的三段式样本；注意它返回 `Result<()>` 并把 kernels 的结果直接作为尾表达式返回。
- [src/ops/traced.rs:38-55](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L38-L55)：`rms_norm_batch_into`——返回单元类型的样本：无 `Result`，直接转发。
- [src/ops/traced.rs:57-67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L57-L67)：`gemm_rows_into` 委托自家 `_checked` 再 `expect`，保证 unchecked 入口也有记录。
- [src/ops/traced.rs:206-220](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/traced.rs#L206-L220)：`silu_mul_fused_batch_into`——注意记录时传的是 `out.hidden_dim`（= `I`，与 call_spec 的 `Intermediate` 轴对应）和 `gate_up.seq_len`（batch）。
- [src/ops/call_trace.rs:9-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_trace.rs#L9-L13)：三个 thread-local——`TRACE`（收集缓冲）、`LABEL_STACK`（标签栈）、`DECODE_KV_LEN`（decode 长度标注）。
- [src/ops/call_trace.rs:21-52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_trace.rs#L21-L52)：`collect_result`——安装收集器、跑闭包、合并 thread-local 与全局两路记录并返回 `(T, Vec<KernelCall>)`；两处 `assert!` 拒绝嵌套收集（嵌套会让记录归属不清，干脆禁止）。
- [src/ops/call_trace.rs:62-91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_trace.rs#L62-L91)：`record_call` 先试 thread-local、未命中转全局；`current_label` 取栈顶否则退回默认算子名。**以当前 HEAD 为准：全仓库检索不到向 `LABEL_STACK` push 的代码，栈恒为空，`current_label` 实际总是返回默认算子名**——标签栈是预留能力，标注「待确认」其未来消费方。
- [src/ops/call_trace.rs:93-104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_trace.rs#L93-L104)：`with_decode_kv_len`/`decode_kv_len`——thread-local 标注当前 decode 的 KV 长度；**当前仓库内除定义外没有调用方**（grep 仅命中定义处），同样标注「待确认」。

#### 4.4.4 代码实践

**实践目标**：跑通 core 自带的一个追踪单测，再给「被换出口的 10 个名字」与 traced.rs 的 10 个函数做一次一一配对审计。

**操作步骤**：

1. 运行（需开启 feature 才能编译 call_trace 模块；该测试本身不碰 GPU）：

   ```bash
   cargo test --release -p pegainfer-core --features kernel-call-trace --lib -- call_trace
   ```

2. 读测试 [src/ops/call_trace.rs:110-125](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/call_trace.rs#L110-L125)：`collect_result` 包住一个 `std::thread::spawn`，子线程里 `record_call` 一条假调用，断言全局通道把它收了回来、op/label 字段无损。
3. 配对审计：把 ops.rs:96-115 的 10 个 `pub use traced::...` 与 traced.rs 的 10 个 `pub fn` 逐个连线，再与 kernels 真身签名抽查两对（建议 `gemm_into` 与 `silu_mul_fused_batch_into`）核对参数顺序与返回类型。

**需要观察的现象**：feature 开启前后 `cargo build -p pegainfer-core` 都应成功（换出口两侧互斥且都自洽）；测试输出应显示 `collect_result_captures_calls_from_child_thread` 通过。

**预期结果**：测试通过；10 对名字一一对应、无遗漏无多余。GPU 运行部分待本地验证（子线程收集测试不需要 GPU，可直接跑）。

#### 4.4.5 小练习与答案

**练习 1**：`collect_result` 为什么干脆 `assert!` 禁止嵌套，而不是让内层收集器忽略？

**答案**：记录的归属语义决定用途——报告要把 `Vec<KernelCall>` 与某一次具体前向（或某一段被测代码）对应。嵌套时一条记录属于哪层无法回答，静默忽略会产生「少记了却说测完了」的假报告；显式 panic 把歧义在开发期暴露。这与 EP free-running 纪律同源：宁可启动失败，不要静默错账。

**练习 2**：如果 kernels 给 `rms_norm_batch_into` 新增一个参数（比如 `weight_scale: f32`），改动清单要动哪些文件？

**答案**：至少四处。(1) kernels 真身；(2) traced.rs 的包装器签名与转发；(3) call_spec.rs 的 `rms_norm_batch_call`（新参数进 `attr` 或改变输入规格）；(4) 所有模型 crate 调用点。ops.rs 本身不用改（名字没变），这正是门面+换出口设计的收益边界：**它保护的是名字与路径，不是签名**——签名仍是 kernels 与 traced.rs 必须同步维护的契约。

## 5. 综合实践

把本讲四件事串起来：**「数门面 → 配对换出口 → 写用例 → 读比例」**。

1. **数门面**：用 4.1.4 的三条 grep 命令统计 ops.rs（预期 69/10/10），再手工把 75 个名字填进 4.1.3 的分类表，核对自己归类的总数是否等于 75。
2. **配对换出口**：把 ops.rs:34-95 中 10 条 `cfg(not)` 行与 ops.rs:96-115 的 10 条 `traced` 行抄成两列对照表（可参照 4.2.2 的表格核对）。
3. **写用例**：完成 4.1.4 的示例代码；有 GPU 的环境（待本地验证）把它放进一个临时 crate 跑通：`x` 填非零值、`weight` 全 1、`eps` 取 1e-6，用 `clone_dtoh` 读回 `norm`，手算 RMSNorm 期望值逐元素对拍；再把 `gate_up` 上半填 1.0、下半填 2.0，验证 `act = SiLU(1.0) * 2.0`（SiLU(1.0) = 1/(1+e⁻¹) ≈ 0.7311，故期望 ≈ 1.4621）。
4. **读比例**：统计归一化/激活类占比（18/75 = 24%；算上 norm+RoPE 融合 3 个则 21/75 = 28%），写一段 150 字左右的观察：dense Transformer 每层前向都是「norm → GEMM → qk-norm+RoPE → attention → add+norm → GEMM → 激活 → GEMM」的固定循环，norm/激活种类多（单算、fused_add、dual、gated、round、offset、batch 各变体）且几乎全部 `_into` 化、融合化，说明引擎把优化重心放在**减少 kernel 启动次数与显存往返**（访存 bound）上，而不是单个算子的算力——这是后续读 u4-l1 CUDA Graph 捕获与模型 crate executor 的伏笔。
5. **收尾自查**：回到练习 4.2-1 的问题，用一句话向自己复述「为什么模型 crate 永远不需要知道 kernel-call-trace 的存在」。

## 6. 本讲小结

- `ops.rs` 是 115 行的算子门面：5 个子模块声明 + 6 个本地出口 + 69 条 kernels 再出口，公开名字集合固定为 75 个，模型 crate 只 use 这一条路径。
- `kernel-call-trace` 是 core 唯一的非默认 feature，通过 **cfg 换出口**把 10 个高频算子（embedding、GEMM 族、norm 族、激活、qk-norm+RoPE）从 kernels 直出切换为 traced.rs 包装，调用方零改动；qwen3/kimi-k2 用自己的 feature 名转发这个开关。
- traced 包装器是严格的三段式模板：`is_enabled()` 检查 → `record_call(规格)` → 尾调用 kernels 同名函数；记录在启动之前、内部无流同步；unchecked 变体委托自家 checked 变体以统一记录与错误信息。
- `call_spec.rs` 用 `KernelCall` + `TensorSpec` + 语义轴（Hidden/Inter2/PageSlot…）记录「规格」而非数据，使追踪结果可序列化、可跨进程与微基准 join——这是 qwen3 `qwen3_model_report` 等报告工具的数据契约。
- 归一化/激活类占再出口约四分之一且全部融合化、`_into` 化，折射出 dense 前向的访存瓶颈取向与零分配纪律（与 CUDA Graph 的指针稳定性要求呼应）。
- 两个「预留未用」点如实标注：`LABEL_STACK` 无写入方（标签恒为默认算子名）、`with_decode_kv_len` 在仓库内无调用方——待确认其后续消费计划。

## 7. 下一步学习建议

- **下一讲 u2-l4** 精读 ops 门面里 core 真正「自己写」的部分：`ops/attention.rs` 三个分页注意力入口与 `ops/paged_plan.rs` 的 `PrefillPagedPlan`/`SplitKvCsr`，并与 `kv_pool.rs` 的 `KvDesc` 对拍「core 描述状态、kernels 消费状态」的契约。
- 若想立刻看到追踪的产出形态，读 [docs/subsystems/kernels/kernel-op-reports.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/subsystems/kernels/kernel-op-reports.md) 的 TL;DR 与 `pegainfer-qwen3/src/bin/qwen3_model_report.rs`（报告二进制如何 `collect_result` 后 join 微基准）。
- kernels 侧算子实现（norm.rs / elementwise.rs / gemm 家族）属于 `pegainfer-kernels` 学习线，本手册不展开；需要时可从本讲给出的签名行号顺藤摸瓜。
