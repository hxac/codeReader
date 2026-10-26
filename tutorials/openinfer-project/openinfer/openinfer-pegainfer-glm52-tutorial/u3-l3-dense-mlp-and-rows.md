# u3-l3 dense MLP 与 Rows 类型化行缓冲

> 承接：u3-l1 讲过 FP8 块量化原语（e4m3 权重 + 128×128 块 f32 scale、小 m 的 weight-only GEMV 与大 m 的激活量化 groupwise GEMM、`FP8_GEMV_MAX_ROWS = 8` 分界、gate|up 打包惯例）。本讲直接消费这些结论，不再重推 FP8 数学；u2-l1/u2-l2 的几何常量与 checkpoint 张量契约、u2-l3 的「生产加载走 from_device 零拷贝包装」也视为已知。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 GLM5.2 前 3 层 dense MLP 的形状、fp8 布局与字节账，并解释它为什么能与 MoE 共享专家共用同一条 `fp8_mlp_into` 代码路径。
2. 逐步读懂 decode 路径「一次 GEMV + SwiGLU + 一次 GEMV」的执行过程，包括 mma partials 融合变体与 #812 宽桶（wide bucket）变体。
3. 掌握 `Rows<const C>` 的「构造点不变量」设计：宽度进类型、行数进值、长度恒等式由构造保证，约 30 处重复 `ensure!` 因此消失。
4. 会把 dense MLP 与共享专家逐字段对比，并解释 `Glm52MlpScratch` 按形状隔离实例的防呆逻辑。

## 2. 前置知识

用通俗语言补齐本讲要用的概念：

- **投影（projection）**：就是一个线性层 \( y = Wx \)。GLM5.2 里所有 MLP 投影的权重都以 fp8 存储（见下）。
- **SwiGLU**：现代 LLM MLP 的标准形状。设 \( x \) 为输入 hidden 状态：

  \[
  y = W_{down}\big(\mathrm{silu}(W_{gate}\,x) \odot W_{up}\,x\big),\qquad
  \mathrm{silu}(z) = z\cdot\sigma(z) = \frac{z}{1+e^{-z}}
  \]

  其中 \( \odot \) 是逐元素乘。直觉：gate 分支做「软门控」，up 分支提供内容，乘起来再投影回 hidden 维。
- **fp8 块量化（回顾 u3-l1）**：一个 `[n, k]` 投影的权重存成 e4m3 字节（每元素 1 字节），另存一张 f32 反量化 scale 表，粒度是 128×128 的块：`[n/128, k/128]`。本项目里它就是 checkpoint 的 `weight_scale_inv`。字节数公式（所有真实形状都恰好整除；单位均为字节）：

  \[
  B_{\text{fp8}}(n,k) = n \times k,\qquad
  B_{\text{scale}}(n,k) = \frac{n}{128}\times\frac{k}{128}\times 4
  \]

- **GEMV 与 GEMM**：GEMV 是「一行激活 × 矩阵」的矩阵-向量乘；GEMM 是多行激活的一般矩阵乘。u3-l1 的结论：行数 ≤ 8 走 weight-only GEMV（激活保持 bf16，块 scale 在 kernel 里边乘边反量化），行数 > 8 走激活量化 + groupwise fp8 GEMM。
- **decode 桶（bucket）**：u1-l3 建立的概念——decode 步的行数只能取一组离散值（桶），scratch 按「最大桶行数」一次性预分配，这是 CUDA Graph 指针稳定的前提（u6-l3 深入）。
- **Rust const 泛型**：泛型参数可以是编译期整数。`Rows<6144>` 和 `Rows<154880>` 是**两个不同的类型**——这是本讲第二主角的全部魔法来源。
- **构造点不变量（invariant at construction）**：一条性质如果在「创建对象的那一行」就成立，并且之后无人能绕过，那么所有下游代码都可以免检查地相信它。本讲会看到它以两种方式落地（类型携带宽度、scratch 携带形状）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的段落 |
|---|---|---|
| `src/dense.rs`（127 行） | dense MLP 权重包 + 两条前向入口，主角一 | 全文件 |
| `src/rows.rs`（54 行） | `Rows<const C>` 类型化行缓冲，主角二 | 全文件 |
| `src/fp8.rs`（750 行） | 共享 FP8 投影原语（u3-l1 已总览） | `ProjWeight`、`pack_proj_pair`、`fp8_linear_into`、`Glm52MlpScratch`、`fp8_mlp_into` |
| `src/config.rs` | 几何常量与 config.json 探针（u2-l1） | dense 层数、两个 intermediate、`ensure_mlp_layer_types` |
| `src/layer.rs` | decoder 层组合（u6-l1 深入） | `Glm52LayerMlp` 枚举、dense 分支接线 |
| `src/scratch.rs` | 步 scratch 装配 | 两个 `Glm52MlpScratch` 实例、`Rows` 字段 |
| `src/moe_decode.rs` | MoE 侧共享权重与路由器（u5-l1 深入） | `Glm52MoeSharedExpert`、专家形状常量 |

## 4. 核心概念与源码讲解

### 4.1 Glm52DenseMlpWeights：权重包与两条前向

#### 4.1.1 概念说明

GLM5.2 的 78 层里，**前 3 层**（`first_k_dense_replace = 3`）的 MLP 半场不是 MoE，而是一个普通的 fp8 SwiGLU MLP；第 3..78 层是 sparse（MoE）。这个「前 3 dense、后 75 sparse」的事实由 config.json 的 `mlp_layer_types` 数组钉死，probe 逐项核对（u2-l1 的 fail-fast 哲学）。

dense MLP 的中间维是 12288——恰好是 hidden（6144）的 2 倍；而 MoE 专家（含共享专家）的中间维是 2048。dense 层与共享专家在**结构上完全同构**，只是更宽。dense.rs 的模块文档自己就把定位说透了：

> The dense layers replace the MoE block with a plain fp8 SwiGLU MLP `down(silu(gate(x)) * up(x))` -- the same shape as the MoE shared expert, only the intermediate is wider (12288 vs 2048). It reuses the shared `fp8_mlp` helper, so this module is just the weight bundle + a thin forward.

为什么 dense 层值得单独学？因为它是 `fp8_mlp_into` 的**最纯消费者**：没有路由、没有 collective、没有量化协议桥接，只有一个权重包和一层薄薄的转发。读懂它，就等于读懂了 `fp8_mlp_into` 这条被 dense 与共享专家共用的主干道。

#### 4.1.2 核心流程

**构建期（每个 rank 各一份，非专家栈全量复制——u2-l2）：**

1. 生产加载路径把已驻留 GPU 的 gate/up/down 三个 `ProjWeight` 交给 `from_device`；
2. `from_device` 用形状闭包逐个校验：gate `[12288, 6144]`、up `[12288, 6144]`、down `[6144, 12288]`；
3. `pack_proj_pair` 把 gate|up 在设备端拼接成**一个** `[24576, 6144]` 投影（权重与 scale 都拼），down 原样保留。

**执行期（decode，行数由 scratch 携带）：**

```text
输入 [rows, 6144]（post-attention-layernorm 之后的 hidden）
  ├─ rows ≤ 8（GEMV 家族）:
  │    ① GEMV: y1 = x·gate_upᵀ      → gate_up 缓冲 [rows, 24576]（前 12288 是 gate，后 12288 是 up）
  │    ② SwiGLU: s = silu(gate)⊙up   → silu_out 缓冲 [rows, 12288]
  │         （rows 4/8 的 mma 路线会把 ① 的 f32 部分和与 ② 融合成一个 kernel）
  │    ③ GEMV: out = s·downᵀ         → out [rows, 6144]
  └─ rows > 8（#812 宽桶）:
       ①' 激活量化 + groupwise fp8 GEMM（gate_up）
       ②' 独立 SwiGLU kernel
       ③' 激活量化 + groupwise fp8 GEMM（down）
```

**执行期（prefill）：** `glm52_dense_mlp_prefill_into` 把宽桶路径的三步显式展开，行数 `rows` 由调用者传入。

**形状与字节账**（每层，按 §2 公式手算）：

| 张量 | 形状 | fp8 字节 | scale 形状 | scale 字节 |
|---|---|---|---|---|
| gate | [12288, 6144] | 72 MiB | [96, 48] | 18 KiB |
| up | [12288, 6144] | 72 MiB | [96, 48] | 18 KiB |
| gate_up（打包后） | [24576, 6144] | 144 MiB | [192, 48] | 36 KiB |
| down | [6144, 12288] | 72 MiB | [48, 96] | 18 KiB |

每层合计约 **216 MiB + 54 KiB**，3 个 dense 层约 **648 MiB**。注意 gate 与 up 打包后**不增加任何字节**——拼接是零成本的，这是打包惯例的直接动机：一次 GEMV 同时产出 gate 和 up 两段，SwiGLU 直接消费 `[gate | up]` 布局，没有任何 concat 拷贝。

#### 4.1.3 源码精读

**常量从哪来**：dense 层数与中间维钉死在 config 常量里，且被 config.json 探针逐字段核对（`first_k_dense_replace`、`intermediate_size`）：

- [src/config.rs:14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L14) — `GLM52_DENSE_LAYERS = 3`：前 3 层是 dense。
- [src/config.rs:41-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L41-L42) — `GLM52_DENSE_INTERMEDIATE = 12_288`、`GLM52_EXPERT_INTERMEDIATE = 2048`：dense 与专家两个中间维并存。
- [src/config.rs:185-204](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L185-L204) — `ensure_mlp_layer_types`：要求 config.json 的 `mlp_layer_types` 长度等于 78，且第 `idx` 项在 `idx < 3` 时必须为 `"dense"`、否则 `"sparse"`。层类型的「前 3 dense」不是引擎的运行期选择，而是 checkpoint 契约。

**权重包**（整个 dense.rs 的全部状态）：

- [src/dense.rs:27-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L27-L33) — 结构体只有两个字段：

  ```rust
  pub(crate) struct Glm52DenseMlpWeights {
      gate_up: ProjWeight, // fp8 [2*INTERMEDIATE, HIDDEN] (gate | up)
      down: ProjWeight,    // fp8 [HIDDEN, INTERMEDIATE]
  }
  ```

  注释一句话预告了 decode 形状：「one GEMV + SwiGLU + one GEMV at decode」。

**ProjWeight 是什么**（u3-l1 的主角，这里只取契约）：

- [src/fp8.rs:191-197](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L191-L197) — `ProjWeight { weight: CudaSlice<u8>, scale: CudaSlice<u8>, n, k }`：weight 是 fp8 e4m3 的 `[n,k]` 字节，scale 是 f32 的 `[n/128, k/128]`（即 `weight_scale_inv`）按原始 u8 持有。
- [src/fp8.rs:227-250](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L227-L250) — 生产路径 `from_device` 零拷贝包装已驻留缓冲，并用两条 `ensure!` 校验 `weight.len() == n*k` 与 `scale.len() == ceil(n/128)*ceil(k/128)*4`。文档注释原话：「Same validation as `upload`, so a packaging drift crashes here, not in the kernel」——包装漂移在包装点崩，绝不带进 kernel。

**gate|up 打包**：

- [src/fp8.rs:350-383](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L350-L383) — `pack_proj_pair(a, b)` 产出 `[a.n + b.n, a.k]`。两个前提：`a.k == b.k`，以及 **`a.n` 必须是 128 的倍数**。后者是正确性关键：块 scale 以 128 行为一格，gate 的行数（12288 或 2048）恰是 128 的整数倍，拼接后的 scale 表 `[a.n/128 + b.n/128, k/128]` 与 gate|up 边界严格对齐——没有任何一个 128×128 块同时覆盖 gate 尾部和 up 头部。权重与 scale 都用 `memcpy_dtod` 设备内拼接，最后经 `from_device` 收口（再次吃掉那两条校验）。
- [src/dense.rs:67-91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L67-L91) — `from_device`（「the production loader path」）用闭包三次校验形状后 `pack_proj_pair(ctx, gate, up)`。对照 [src/dense.rs:38-65](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L38-L65) 的 `from_host`：多一步 H2D 上传，校验完全相同——测试与生产走同一套形状契约。

**decode 前向（薄到只剩一次转发）**：

- [src/dense.rs:93-112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L93-L112) — `glm52_dense_mlp_forward_into(ctx, weights, normed_hidden, mlp, out)` 的函数体只有一行：把 `gate_up`、`down`、输入、scratch、输出交给 `fp8_mlp_into`。注意文档强调 `mlp` 「must be sized for the dense intermediate (12288)」——行数事实不在参数里，而在 scratch 的 `rows` 字段里。
- [src/fp8.rs:510-576](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L510-L576) — `fp8_mlp_into` 主干：
  - [src/fp8.rs:523-532](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L523-L532) 唯一的运行期校验是「跨对象 join」：`s.intermediate == gate_up.n / 2`。注释说明权重包内部一致性已在构造点钉死，这里只查这次调用新引入的配对关系。
  - [src/fp8.rs:535-539](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L535-L539) 宽桶分支（#812）：`s.wide` 存在时（scratch 构造时 `rows > 8`）两腿都走 `fp8_linear_large_m_into`（大 m 的激活量化 + groupwise GEMM，u3-l1），SwiGLU 用独立 kernel。
  - [src/fp8.rs:544-575](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L544-L575) GEMV 家族：`glm52_fp8_weight_only_gemv_partials_launch` 一次写出 gate_up 缓冲（或停在 f32 部分和）；`ksplit == 0`（寄存器 tile 路线，rows 1/2）时用独立 `glm52_silu_and_mul_bf16_launch`；`ksplit != 0`（tensor-core mma 路线，rows 4/8）时用 `glm52_gemv_reduce_silu_mul_launch` 把定序 reduce 与 SwiGLU 融合成一个 launch（bit-identical）。最后 [src/fp8.rs:568-575](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L568-L575) 的 `fp8_linear_into` 完成 down 腿。
- [src/fp8.rs:385-409](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L385-L409) — `fp8_linear_into` 的文档注释交代了行数分流与数值契约：rows 1/2 保持 bit-stable 的 CUDA-core 路径；rows 4/8 走 tensor-core mma（逐桶确定、但与 rows=1 不逐位一致——跨桶 FP 漂移是被接受的全步契约，细节在 `glm52_moe_gemv.cu`）。
- [src/fp8.rs:464-508](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L464-L508) — `Glm52MlpScratch`：按「精确 intermediate」持有 `gate_up [rows, 2*I]`、`silu_out [rows, I]`、每实例独占的 `gemv_partial`（防流重叠共享指针），以及 `rows > FP8_GEMV_MAX_ROWS` 时才创建的 `wide: Glm52Fp8GemmScratch`（k 取 `max(HIDDEN, intermediate)`，一份覆盖两腿）。

**prefill 变体**：

- [src/dense.rs:114-127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L114-L127) — `glm52_dense_mlp_prefill_into` 三步显式展开：`fp8_linear_large_m_into(gate_up)` → `glm52_silu_and_mul_bf16_launch` → `fp8_linear_large_m_into(down)`。与宽桶分支同构——大行数下 GEMV 让位给 GEMM 是普遍规律。

**谁来调用它**：

- [src/layer.rs:61-66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L61-L66) — `Glm52LayerMlp` 枚举三分支 `Dense(Box<Glm52DenseMlpWeights>) | MoeEp8(..) | MoeTp(..)`；Box 化是因为 78 项 vec 持有全体层权重，枚举本体保持小。
- [src/layer.rs:409-416](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L409-L416) — dense 分支的标准接线（注意 [src/layer.rs:372](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L372) 的注释说明这段是单层 oracle 辅助函数，生产路径由 step body 直接驱动两个半场，u6-l2 精读）：

  ```rust
  Glm52LayerMlp::Dense(dense) => glm52_dense_mlp_forward_into(
      ctx,
      dense,
      s.layer.normed2.data(),
      &mut s.dense_mlp,
      s.layer.mlp_out.data_mut(),
  )?,
  ```

#### 4.1.4 代码实践：手算一个 dense 层的显存账

1. **实践目标**：不看答案，从 §2 的字节公式推出一个 dense 层的 fp8 与 scale 字节数，并核对形状常量来源。
2. **操作步骤**：
   1. 打开 [src/fp8.rs:237-247](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L237-L247)，抄下两条 `ensure!` 的公式；
   2. 打开 [src/dense.rs:83-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L83-L89)，记下三个期望形状；
   3. 手算 §4.1.2 的表格（gate_up 打包后的形状、fp8 字节、scale 形状与字节）；
   4. 用 `grep -n "INTERMEDIATE" src/config.rs` 核对 12288 与 2048 两个常量名，确认自己算的是 dense 那个。
3. **需要观察的现象**：打包前后 gate+up 的总字节数不变（144 MiB = 72 + 72），scale 从两张 [96,48] 变成一张 [192,48]。
4. **预期结果**：每层 216 MiB + 54 KiB，三层约 648 MiB。全程无需 GPU。
5. 有 GPU 与权重时可加分验证：启动引擎观察加载报告与显存探测（u2-l3 的加载链路）——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`pack_proj_pair` 为什么要求第一个投影的 `n` 是 128 的倍数？如果 gate 的 n 是 12280 会怎样？

**答案**：块 scale 的粒度是 128 行。gate 的 12288 恰是 96 个整块，拼接后的 scale 表 `[192, 48]` 里 gate 与 up 各占整数块行，反量化正确。若 n=12280，gate 的尾部与 up 的头部会落进同一个 128×128 块，一块只能有一个 scale，两边都反量化错——所以 [src/fp8.rs:361-365](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L361-L365) 直接 `ensure!` 拒绝。

**练习 2**：`from_host` 为什么标 `#[cfg(test)]`，生产路径走哪条？

**答案**：生产加载是 u2-l3 的 mmap → H2D 驻留 → `ProjWeight::from_device` 零拷贝包装，所以 [src/dense.rs:67-91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L67-L91) 的 `from_device` 才是生产入口；`from_host` 只服务 oracle/单元测试里的小张量上传，不进发布二进制。

**练习 3**：decode 行数为 1 与为 8 时，`fp8_mlp_into` 的 kernel 序列有何不同？

**答案**：两者都走 GEMV 家族，但 rows=1/2 走 bit-stable 寄存器 tile 路线，GEMV 直接写 bf16 的 gate_up 缓冲（`ksplit == 0`），SwiGLU 用独立 kernel，共 3 个 launch；rows=4/8 走 tensor-core mma，GEMV 停在 f32 部分和，随后 `glm52_gemv_reduce_silu_mul_launch` 把 reduce 与 SwiGLU 融合，down 腿 GEMV 复用部分和缓冲，仍是 3 个 launch 但数值路径不同（[src/fp8.rs:540-567](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L540-L567)）。

### 4.2 Rows<const C>：把缓冲宽度变成类型事实

#### 4.2.1 概念说明

整步 scratch 里到处是「行主序 `[tokens, C]`」的 bf16 设备缓冲：hidden 是 `[T, 6144]`、logits 是 `[T, 154880]`……以前它们用**无类型**的设备 vec 承载，于是每个消费者都在自己的入口写一遍 `ensure!(buf.len() == tokens * C)`——rows.rs 的文档注释给出了数量级：「~30 `ensure!`s all re-checking what the allocation already established」。这类检查有三个问题：重复、只查长度不查语义（长度对了宽度错了照样通过）、且把本该由类型系统回答的问题推到运行期。

`Rows<const C>` 的解法是把不变量搬到**构造点**：

- 宽度 `C` 是 const 泛型参数——进**类型**；
- 行数 `tokens` 是值字段——随**值**走；
- 恒等式 \(\text{data.len} = \text{tokens} \times C\) 由 `zeros` 这唯一的分配入口保证。

于是接收方「从值读行数、从类型读宽度」，两者都不再运行期校验；把 `[T, HIDDEN]` 缓冲传到 `[T, VOCAB]` 位置从「运行期长度碰巧相等、语义悄悄错」变成**编译错误**。

一个诚实的边界：`data()`/`data_mut()` 是类型事实的「出口」——kernels crate 的 launch 接口收的是无类型切片，宽度与行数事实到此为止（rows.rs 的访问器注释原话：「the width/row facts stop here」）。另一条边界写在模块注释末尾：宽度编译期化依赖「TP1 是唯一支持布局」这一事实（整个 crate 的形状都钉死在 config.rs 常量上）；将来若出现分片布局，const 参数退化为「维度标签」、每 rank 宽度进 rank 配置——构造点不变量与类型化签名原样幸存。

#### 4.2.2 核心流程

不变量的生命周期：

```text
Rows::<C>::zeros(ctx, tokens)          ← 唯一分配入口
    ├─ ensure!(tokens > 0)             ← 构造点拒绝空缓冲
    └─ alloc_zeros::<bf16>(tokens * C) ← 长度恒等式在此成立
          │
          ▼  按值传递（类型携带 C，值携带 tokens）
消费者：tokens() 读行数，签名上的 Rows<C> 读宽度 —— 零运行期校验
          │
          ▼  kernel 边界
data() / data_mut() 交出 &CudaSlice<bf16> —— 类型事实到此为止
```

新旧对比：

| | 旧世界（无类型 vec） | 新世界（`Rows<C>`） |
|---|---|---|
| 宽度错误 | 运行期长度检查可能碰巧通过 | **编译错误** |
| 行数 | 每个消费者自行推导/校验 | 值字段，一次构造 |
| 入口检查 | ~30 处 `ensure!` | 1 处（`tokens > 0`） |
| kernel 边界 | 原样传裸缓冲 | `data()` 显式「降级」 |

#### 4.2.3 源码精读

- [src/rows.rs:1-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L1-L17) — 模块文档，完整交代动机（~30 处 ensure）、方案（宽度进类型、行数进值、构造保证）与 TP 备注。这段注释本身就是一份小型设计文档，值得逐句读。
- [src/rows.rs:25-39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L25-L39) — 结构与唯一构造入口：

  ```rust
  /// `[tokens, C]` row-major bf16 device buffer; `data.len() == tokens * C` by
  /// construction.
  pub(crate) struct Rows<const C: usize> {
      data: CudaSlice<bf16>,
      tokens: usize,
  }

  impl<const C: usize> Rows<C> {
      pub(crate) fn zeros(ctx: &DeviceContext, tokens: usize) -> Result<Self> {
          ensure!(tokens > 0, "Rows<{C}> needs a positive row count");
          Ok(Self {
              data: ctx.stream.alloc_zeros::<bf16>(tokens * C)?,
              tokens,
          })
      }
  ```

- [src/rows.rs:41-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L41-L53) — `tokens()` 与 `data()/data_mut()`；后者的文档注释明确这是 kernel-launch 边界。
- **使用面**：`Rows` 已渗透到整个执行层。本讲实测 `grep -rn "Rows<" src/ --include="*.rs" | grep -v "^src/rows.rs" | head -35` 命中 35 行，覆盖 indexer、mtp、prefill_tp、bookend、scratch 等（完整计数请自行去掉 `head`）。几个代表点：
  - [src/scratch.rs:64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L64)、[src/scratch.rs:71-72](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L71-L72) — 步 scratch 顶层的 `hidden: Rows<GLM52_HIDDEN>`、`final_normed: Rows<GLM52_HIDDEN>`、`logits: Rows<GLM52_VOCAB>`：同一份类型在两种宽度下使用。
  - [src/bookend.rs:65-67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L65-L67) — bookend 的 `normed: &Rows<GLM52_HIDDEN>` 进、`out: &mut Rows<GLM52_VOCAB>` 出：宽度混接在这里就是编译错误，这正是 u3-l2 讲过的 lm_head 尾巴。
  - [src/layer.rs:413-415](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L413-L415) — 4.1 里 dense 分支的 `normed2.data()` / `mlp_out.data_mut()` 正是 `Rows` 风格访问器在层接线处的消费点：两大主角在本讲交汇。
- **来龙去脉**：`git log --follow -- src/rows.rs` 的最早提交是 `0c2bb64a refactor(glm52): typed Rows<C> buffers — invariants at construction, not per-call ensure (#575)`（其后只有重命名与 rustfmt 触碰）——提交信息本身就是本模块的设计宣言。

#### 4.2.4 代码实践：复现 Rows 的引入与迁移面

1. **实践目标**：用 git 考古亲手验证「~30 处 ensure 被 #575 删除」与 `Rows` 的渗透范围。
2. **操作步骤**（全部只读命令，无需 GPU）：
   1. `git log --oneline --follow -- src/rows.rs` — 应看到引入提交 `0c2bb64a`（#575）；
   2. `git show 0c2bb64a --stat` — 观察这次重构波及了哪些文件（预期是 scratch/bookend/mtp 等执行层文件）；
   3. `git show 0c2bb64a | grep -c '^-.*ensure'` — 数一数被删掉的 `ensure` 行数，与模块注释里的「~30」对照；
   4. `grep -rn "Rows<" src/ --include="*.rs" | grep -v "^src/rows.rs" | wc -l` — 复现当前使用计数（本讲实测 `head -35` 即满）。
3. **需要观察的现象**：引入提交的 diff 里，删除集中在各消费者入口的长度校验；新增集中在类型签名替换。
4. **预期结果**：`0c2bb64a` 存在且改动面横跨多文件；删除的 ensure 行数与「~30」同量级（精确数字**待本地验证**）。
5. **类型实验（示例代码，非项目代码）**：在任意临时 crate 里验证宽度是类型事实——

   ```rust
   // 示例代码：把 Rows<6144> 传给期望 Rows<154880> 的函数
   fn takes_vocab(_x: &Rows<154880>) {}
   let hidden: Rows<6144> = Rows::zeros(&ctx, 4)?;
   takes_vocab(&hidden); // 预期编译失败（类型不匹配），而非运行期长度检查
   ```

   预期得到编译期类型不匹配错误（Rust 典型报错码 E0308），证明错宽传递已不可能溜进运行期。

#### 4.2.5 小练习与答案

**练习 1**：为什么行数 `tokens` 存在值里而不是也做成类型参数？

**答案**：行数是运行期事实（decode 桶 1/2/4/8、prefill chunk 长度都随步变化），类型参数会强迫按行数单态化出无数版本；宽度（6144、154880 等）来自 config 常量、编译期已知且数量有限，才适合进类型。

**练习 2**：`data()` 为什么返回裸 `&CudaSlice<bf16>` 而不是带形状的包装？

**答案**：kernels crate 的 launch 接口收无类型设备切片（rows.rs:45-47 注释：「the kernels crate takes untyped slices; the width/row facts stop here」）。`Rows` 的职责在调用边界结束，越界之后的正确性由调用点自己保证——这是一个有意识的、写进注释的边界。

**练习 3**：`zeros` 为什么连 `tokens == 0` 都要拒绝？

**答案**：零行缓冲会让「行数为 0」合法流入下游 launch 与 tokens() 消费者，产生难以追查的空跑或断言；在构造点直接拒绝，失败信息离错误源最近（对照 u2-l4 的原则：宁可启动失败，不可带病运行）。

### 4.3 同一个 fp8_mlp、两种 intermediate：与共享专家对比

#### 4.3.1 概念说明

MoE 层的输出 = 路由专家加权和 + **共享专家**输出。共享专家是一个全体 token 无条件全量参与、不经过路由的 fp8 SwiGLU MLP——也就是说，它与 dense MLP 是**同一种东西**，只是 intermediate 从 12288 缩到 2048。moe_decode.rs 的文档注释与 dense.rs 遥相呼应：

> The single shared expert (a plain fp8 SwiGLU MLP at intermediate 2048). gate|up are packed into one `[2*INTERMEDIATE, HIDDEN]` projection at build so the decode path is one GEMV + SwiGLU + one GEMV.

于是这个 crate 里存在一条**被两种消费者共用**的 MLP 主干道：`fp8_mlp_into`。fp8.rs 的注释直接点名这两个用户：「`gate_up`/`down` internal consistency is pinned where the weight bundles are built (`Glm52DenseMlpWeights` / `Glm52MoeSharedExpert` check exact shapes)」。代码路径复用、数值形状隔离——隔离靠的是 scratch 按形状各持一份。

#### 4.3.2 核心流程

逐字段对比（dense vs 共享专家 vs 单枚路由专家）：

| 项 | dense MLP | 共享专家 | 单枚路由专家 |
|---|---|---|---|
| 出现层数 | 3（层 0-2） | 75（每个 MoE 层一个） | 每 rank 持一片（u2-l2 区间切分） |
| intermediate | 12288 | 2048 | 2048 |
| gate_up（打包后） | [24576, 6144] = 144 MiB | [4096, 6144] = 24 MiB | 打包进专家 bank（W13） |
| down | [6144, 12288] = 72 MiB | [6144, 2048] = 12 MiB | [6144, 2048] = 12 MiB |
| 每份权重 | ≈ 216 MiB + 54 KiB | ≈ 36 MiB + 9 KiB | ≈ 36 MiB + 9 KiB |
| decode 前向 | `fp8_mlp_into` | `fp8_mlp_into` | DeepGEMM 路径（u5-l2） |

两个直接推论：一个 dense 层的 MLP 权重 ≈ **6 个**专家；dense 与共享专家的差别只剩「宽度常量 + 错误信息前缀」。

防交叉的三道防线（全部 fail-fast）：

1. **权重包构造点**：两者的构造函数各自用 shape 闭包钉死精确形状（dense.rs:74-89 与 moe_decode.rs:92-103 几乎逐行对称）；
2. **pack_proj_pair**：128 倍数检查（fp8.rs:361-365）；
3. **运行期 join**：`fp8_mlp_into` 开头唯一一条校验 `s.intermediate == gate_up.n / 2`（fp8.rs:527-532）——把 12288 的 scratch 配给 2048 的权重包（或反之）在这里当场崩。

scratch 的隔离不是「以防万一」的防御，而是**必须**：[src/fp8.rs:464-478](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L464-L478) 的注释原话——dense 与共享专家「each gets its own instance so a cross-wiring crashes here」；同时每实例独占 `gemv_partial`，保证 ctx/aux 流重叠永远看不到共享指针。

#### 4.3.3 源码精读

- [src/moe_decode.rs:77-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L77-L83) — `Glm52MoeSharedExpert` 定义。与 [src/dense.rs:30-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L30-L33) 并排读：字段名、类型、注释格式完全一致，只有常量语义不同（2048 vs 12288）：

  ```rust
  pub(crate) struct Glm52MoeSharedExpert {
      gate_up: ProjWeight, // fp8 [2*INTERMEDIATE, HIDDEN] (gate | up)
      down: ProjWeight,    // fp8 [HIDDEN, INTERMEDIATE]
  }
  ```

- [src/moe_decode.rs:85-108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L85-L108) — `new()` 与 dense 的 `from_device` 同构：同款 shape 闭包、同样以 `pack_proj_pair(ctx, gate, up)` 收口。两个构造点刻意各自存在（错误信息分别带 `dense MLP` / `MoE shared` 前缀，指向各自上下文）。
- [src/moe_decode.rs:34-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L34-L41) — 专家形状常量：`W13_N = 2*INTERMEDIATE = 4096`（gate|up）、`W13_K = HIDDEN = 6144`、`W2_N = 6144`、`W2_K = 2048`；注释里的 scale 维度（`W13_SCALE_ROWS = 32`、`W2_SCALE_COLS = 16`）反推出量化分组 `QUANT_GROUP = 128`，与 fp8.rs 的 `FP8_BLOCK` 同值——同一个块量化契约贯穿两处。
- [src/moe_decode.rs:123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L123) — `pub(crate) const GLM52_SHARED_EXPERT_INTERMEDIATE: usize = INTERMEDIATE;`（即 2048）：共享专家的中间维有自己的具名常量，scratch 侧从这取名。
- [src/scratch.rs:125-126](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L125-L126) — 两行并排就是本模块的结论：

  ```rust
  dense_mlp: Glm52MlpScratch::new(ctx, GLM52_DENSE_INTERMEDIATE, tokens)?,
  shared_mlp: Glm52MlpScratch::new(ctx, GLM52_SHARED_EXPERT_INTERMEDIATE, tokens)?,
  ```

  同一个 `Glm52MlpScratch` 类型、两个形状实例；行数 `tokens` 相同（同一步的行）。
- [src/config.rs:42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L42)、[src/config.rs:45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L45) — `GLM52_EXPERT_INTERMEDIATE = 2048` 与 `GLM52_SHARED_EXPERTS = 1`：一个共享专家、专家中间维 2048，均由 config.json 探针核对。
- [src/layer.rs:64-66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L64-L66) — TP 形态下共享专家的另一条存在方式：折叠进 rank 的切片 bank（`Glm52MoeTpRank.slices`，共享专家固定在 bank index 256）。也就是说共享专家有两种驻留形态（EP：独立权重包；TP：折叠进 bank），dense 层则永远是独立权重包——u5-l1/u5-l4 展开。

#### 4.3.4 代码实践：并排 diff 两个权重包

1. **实践目标**：亲手确认「同构不同宽」，并把防交叉的三道防线标注到行号。
2. **操作步骤**：
   1. 并排阅读 [src/dense.rs:30-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L30-L33) 与 [src/moe_decode.rs:77-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L77-L83)，再并排 [src/dense.rs:74-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L74-L89) 与 [src/moe_decode.rs:92-107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L92-L107)，列出相同点与不同点；
   2. 把 12288 换成 2048 重算 §4.1 的字节账（gate_up 24 MiB + 6 KiB，down 12 MiB + 3 KiB）；
   3. 在源码里给三道防线各贴一个行号书签：两个构造点的 shape 闭包、`pack_proj_pair` 的 128 倍数检查、`fp8_mlp_into` 的 join 校验；
   4. 计算「一个 dense 层的 MLP 权重相当于几个共享专家」（216 ÷ 36 = 6）。
3. **需要观察的现象**：两段构造代码除常量与错误前缀外无差别；每份共享专家权重 ≈ 36 MiB + 9 KiB。
4. **预期结果**：得到一张三行防线表（构造点 / 打包点 / join 点，各带行号与失败信息）。
5. 有 GPU 时可运行 `cargo test --release -p pegainfer-glm52 --features glm52 --lib` 观察相关门是否全绿——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：既然两个构造点几乎相同，为什么源码保留两份而不是抽一个公共构造函数？

**答案**：两处各约 20 行、错误信息携带各自上下文（`GLM5.2 dense MLP ...` vs `GLM5.2 MoE shared ...`），失败时能直接指认是哪一侧的包装漂移；抽象掉上下文的公共函数会让失败信息变差、可读性收益有限。是否值得抽象可以自行评判——当前源码的选择是「复制式对称」。

**练习 2**：为什么不能让 dense 与共享专家共用一个按 12288 分配的大 scratch，让共享专家「只用前 2048」？

**答案**：`fp8_mlp_into` 以 `gate_up.n / 2` 推 intermediate 并要求与 `s.intermediate` 严格相等（fp8.rs:527-532）——「只用前一段」的约定无法通过这条校验；放宽校验则会把配对错误重新变成静默错误。此外每实例独占 `gemv_partial` 是流重叠下的指针安全要求（fp8.rs:472-474 注释）。

**练习 3**：dense 层为什么不把 gate|up/down 折叠进专家 bank？

**答案**：dense 层没有 bank——它的权重由 `Glm52LayerMlp::Dense` 分支独立持有（layer.rs:62），不参与任何专家路由或 collective；折叠进 bank 是 TP 形态下「共享专家与路由专家同池」的布局技巧（layer.rs:64-66），与 dense 层无关。

## 5. 综合实践

**任务：为「第 1 层（dense、无 indexer）decode 单步、行数 rows=8」产出三张小表，把本讲全部内容串起来。**

1. **缓冲清单表**：从 [src/layer.rs:409-416](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L409-L416) 的接线出发，列出该步 MLP 半场涉及的每个缓冲：名字、形状、所在类型（`ProjWeight` / `Glm52MlpScratch` 字段 / 层缓冲），并标注哪些长度事实来自「构造点不变量」。预期包含：gate_up `[24576, 6144]` 权重、down `[6144, 12288]` 权重、scratch 的 `gate_up [8, 24576]`、`silu_out [8, 12288]`、`gemv_partial`（f32，行数 8，宽度是 kernel 常量）与输出 `[8, 6144]`。
2. **kernel 序列表**：对照 [src/fp8.rs:544-575](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L544-L575)，写出 rows=8 的三个 launch（mma partials GEMV → 融合 reduce+SwiGLU → down GEMV），再写出 rows=1 的版本（tile GEMV → 独立 SwiGLU → down GEMV），标注两版的数值契约差异来源。
3. **字节账对比表**：该步 dense 半场读取的权重字节（整份 gate_up + down ≈ 216 MiB）对比同一行数下共享专家半场（≈ 36 MiB），并回答：为什么权重读取量与行数无关？（权重按行广播式重读——weight-stationary，fp8.rs:387 的「the weight is still read once」描述的是行间复用，步与步之间权重常驻。）

**检查方式**：逐项对照本讲引用的行号核销；三张表全部无需 GPU 即可完成。完成后，你等于提前画好了 u6-l2「整步前向」里 dense 层那一段的底图。

## 6. 本讲小结

- GLM5.2 前 3 层的 MLP 半场是 fp8 SwiGLU dense MLP（intermediate 12288 = 2×hidden），层类型由 config.json 的 `mlp_layer_types` 钉死为「前 3 dense、后 75 sparse」。
- dense MLP 与 MoE 共享专家（2048）结构同构，共用同一条 `fp8_mlp_into` 主干；dense.rs 因此只是「权重包 + 薄前向」。
- gate|up 在构建期经 `pack_proj_pair` 零成本拼成 `[24576, 6144]`（要求首投影 n 是 128 倍数以对齐块 scale），decode 于是就是「一次 GEMV + SwiGLU + 一次 GEMV」，无 concat 拷贝。
- 行数分流：≤8 走 weight-only GEMV（1/2 bit-stable、4/8 mma+融合 reduce），>8 走 #812 宽桶的激活量化 groupwise GEMM；prefill 显式走大 m 路径。
- `Glm52MlpScratch` 按精确 intermediate 隔离实例（scratch.rs:125-126 两行并排），配对错误在 `fp8_mlp_into` 的唯一 join 校验崩——代码路径复用、数值形状隔离。
- `Rows<const C>`（#575）把 `[T, C]` 缓冲的宽度提升为 const 泛型、行数随值走，`len = tokens × C` 由构造保证：约 30 处重复 `ensure!` 归零，错宽传递从运行期隐患变成编译错误；类型事实在 `data()` 的 kernel 边界处有意识地终止。

## 7. 下一步学习建议

- **本单元收尾后进入 u4（注意力子系统）**：u4-l1 的 MLA front 投影链与本讲的 `ProjWeight`/fp8 投影调度同源；u4-l2 的 KV 页 64 对齐与 `pack_proj_pair` 的 128 块对齐是同一种「对齐即协议」思想，学完本讲再看会非常顺。
- **想看共享专家的另一半**：u5-l1（路由器与共享专家）讲 noaux_tc 路由下共享专家如何与路由专家合成层输出；u5-l4 讲 TP 形态下共享专家折叠进 bank 256 的细节——本讲 4.3 是它的前菜。
- **想看 dense 前向在生产中由谁驱动**：u6-l1（层组合）与 u6-l2（整步前向 run_step_body）——layer.rs:372 注释里「Production drives the two halves directly」指的就是那里。
- **延伸阅读**：`docs/models/glm52/fp8-blockwise-gemm-lab.md`（u3-l1 引用的 fp8 GEMM 实验记录）与 `docs/models/glm52/serving-status.md`（能力矩阵）；想给 GEMV 做基准可预习 u10-l3 的 kernel_lab。
