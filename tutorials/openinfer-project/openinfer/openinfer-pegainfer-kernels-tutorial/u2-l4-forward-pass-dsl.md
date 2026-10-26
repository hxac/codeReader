# typed_pipeline! 前向声明 DSL

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `typed_pipeline!` 要消灭的样板是什么：一条前向链里反复出现的「拿引用 → 调 `typed_ops` → `?` 传播错误」三段式。
2. 默写宏的 4 种 header 形态与 11 种语句，并把每条语句对应到它展开出的那个 `typed_ops` 函数调用。
3. 解释 `prefill` / `graphsafe` 两档 GEMM 模式如何被 header 与语句级覆盖（`gemm[...]`）两级控制，以及为什么 `tensor` 分配被硬性绑定在 `gemm = prefill` 上。
4. 掌握 `try` / `call` 两个逃逸口的语义差别（`?` 传播 vs 普通语句），并判断什么时候应当放弃 DSL、直接手写 `typed_ops` 链。
5. 会用 `cargo expand`（或纯手工展开）验证「宏展开 == 手写 typed_ops 链」这一等价关系。

## 2. 前置知识

### 2.1 声明宏（macro_rules!）最小必备

本讲会读一段真实的 `macro_rules!`，只需要以下四点：

- **fragment 说明符**：`$x:expr` 匹配一个表达式，`$name:ident` 匹配一个标识符，`$dim:path` 匹配一条路径（如常量 `KIMI_K2_MLA_QKV_A_OUT`），`$($rest:tt)*` 匹配「剩下的全部 token 树」。
- **递归展开（tt muncher）**：宏的一条规则把队首语句翻译成 Rust 代码，再把剩余 token 重新喂给自己，直到队列空。本讲的 `@steps` 就是这么写的。
- **内部规则**：以 `(@steps ...)`、`(@gemm ...)` 开头的臂不是给用户写的，是宏自己递归调用的「内部入口」，用户只会写 header + 语句。
- **hygiene**：宏定义里的路径要用 `$crate::` 前缀（展开到「定义处 crate」而不是调用处的某个本地模块），`swap` 语句用 `::std::mem::swap` 绝对路径，都是同一原因。

### 2.2 承接前几讲的结论

| 来自 | 结论 | 在本讲的用途 |
|---|---|---|
| u2-l1 | `GpuTensor` 维度是编译期常量、token 按列主序连续存放；预分配缓冲保指针稳定 | DSL 的 `tensor` 语句分配的就是 `GpuTensor` |
| u2-l2 §4.1 | `GpuWeight<OUT, IN>`、`NormWeight<DIM>` 用 const generics 锁形状 | DSL 语句里 `$dim:path`、`$w:expr` 的类型来源 |
| u2-l2 §4.2–4.3 | `gemm_into` 家族签名；graphsafe 变体走 workspace-free cuBLAS 句柄 | DSL 的 `gemm = prefill/graphsafe` 两档最终落到这里 |
| u2-l3 | wrapper 统一返回 `anyhow::Result` | DSL 每条语句展开都带 `?` |

术语速查：

- **DSL（领域特定语言）**：在 Rust 语法内部用宏造出的一套「小语言」，只覆盖前向链这一种场景。
- **逃逸口（escape hatch）**：DSL 故意留的口子，让你随时退回普通 Rust 调用（本讲是 `try` 与 `call`）。
- **header**：`typed_pipeline! { ... }` 花括号里的第一行，声明上下文、eps、长度与 GEMM 模式。
- **graphsafe**：可在 CUDA Graph 捕获里安全回放的执行形态（无工作区分配、无指针漂移），细节见 u2-l2 §4.3 与 u4-l3。

## 3. 本讲源码地图

| 文件 | 规模 | 在本讲中的角色 |
|---|---|---|
| [src/forward_pass.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs) | 172 行 | 全部内容就是 `typed_pipeline!` 一个宏（文档 + 规则） |
| [src/typed_ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs) | 540 行 | DSL 的展开目标：每条语句对应这里的一个函数 |
| [src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs) | —（类型定义散布全文） | `GpuTensor` / `GpuWeight` / `NormWeight` 与 `GpuTensor::zeros` |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L4-L12) | 常驻模块表 | `pub mod forward_pass;`（L5）与 `pub mod typed_ops;`（L12）把两者挂到 crate 根 |

说明：宏带 `#[macro_export]`，所以它导出在 crate 根上，模型 crate 用 `pegainfer_kernels::typed_pipeline!` 即可调用。本 crate 内部（tests/examples）目前没有它的调用点——真实调用发生在各模型 crate（文档示例即取材 kimi-k2 线，见 4.1.3），那些目录在本 crate 之外。

## 4. 核心概念与源码讲解

### 4.1 宏语法：header 四形态与语句目录

#### 4.1.1 概念说明

一个解码/预填（prefill）step 的前向链，手写出来是这样一遍遍重复的：

```rust
// 手写形态（节选示意）
typed_ops::rms_norm_into(ctx, &hidden, &attn.input_norm, eps, &mut normed)?;
typed_ops::gemm_into(ctx, &attn.fused_qkv_a_proj, &normed, &mut qkv_a)?;
kimi_mla_split_qkv_a(ctx, &qkv_a, &mut q_a, &mut compressed_kv, &mut k_rope)?;
```

每个算子都在重复「ctx、引用、`?`」。`typed_pipeline!` 把这类**已存在于 typed_ops 的标准调用**折叠成一行声明，而把模型专属 kernel（如上面的 `kimi_mla_split_qkv_a`）留在普通 Rust 调用形态。它的职责边界写在文件头：

> The macro expands common `typed_ops` calls while keeping model-specific kernels as normal Rust calls through `try ...;` or `call ...;` escape hatches. It is intentionally limited to tensor allocation, typed op dispatch, error propagation, and buffer swaps.

即：只做「张量分配、typed op 分发、错误传播、缓冲交换」四件事，其余一概逃逸。

#### 4.1.2 核心流程

宏的执行模型分两个阶段：

```text
typed_pipeline! { header; s1; s2; ...; sn; }
   │
   ├─ 阶段一：header 归一化（4 个公开臂）
   │     解析 ctx / eps / seq_len / gemm 四个键
   │     → 统一转成内部形式 @steps(ctx, eps, 模式, seq_len标记; 语句...)
   │     seq_len 标记：(@some 表达式) 或 (@none)
   │
   ├─ 阶段二：@steps 递归消化语句（tt muncher）
   │     每条语句：
   │       ① 匹配一条语句臂
   │       ② 展开成一行 Rust（typed_ops 调用 / zeros / mem::swap / 逃逸表达式）
   │       ③ 把剩余语句重新喂给 @steps
   │     gemm 语句额外转发到 @gemm 终结臂，按模式选 gemm_into / gemm_graphsafe_into
   │
   └─ 语句队列为空 → 基例展开为 {}，递归终止
```

header 的四种合法形态与去向：

| header 写法 | seq_len 标记 | GEMM 模式 | 结果 |
|---|---|---|---|
| `ctx, eps, seq_len, gemm = prefill;` | `(@some ...)` | prefill | 允许 `tensor` 分配，`gemm` 走 `gemm_into` |
| `ctx, eps, seq_len, gemm = graphsafe;` | `(@some ...)` | graphsafe | header 合法，但一写 `tensor` 即编译错误 |
| `ctx, eps, seq_len;`（漏 gemm） | — | — | 直接 `compile_error!` |
| `ctx, eps, gemm = <mode>;` 或只写 `ctx, eps;` | `(@none)` | 指定模式 / 默认 graphsafe | 不允许任何 `tensor` 语句 |

三个编译期门禁（都是刻意设计，不是限制不足）：

- `seq_len = ...` 必须搭配 `gemm = prefill`；
- graphsafe 模式下 `tensor` / `tensor_at` 一律拒绝；
- 没有 `seq_len` 的 header 里出现 `tensor` 一律拒绝。

三者合起来表达一条纪律：**解码/graph 回放路径零分配**——分配只发生在 prefill 链里。

#### 4.1.3 源码精读

文件头的文档与完整示例（注意示例 fence 标了 `ignore`，是示意风格，取材 kimi-k2 线）：

- [src/forward_pass.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L1-L6)：声明宏的职责边界——只做张量分配、typed op 分发、错误传播、缓冲交换，模型专属 kernel 经 `try ...;` / `call ...;` 保持普通 Rust 调用。
- [src/forward_pass.rs:L10-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L10-L18)：官方示例：header 声明 `ctx/eps/seq_len/gemm = prefill`，`tensor qkv_a: ...` 分配，`rms_norm` → `gemm` → `try kimi_mla_split_qkv_a(...)` 逃逸。读它就能看出 DSL 的完整长相。
- [src/forward_pass.rs:L20-L22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L20-L22)：三条默认规则——`gemm` 省略时默认 `graphsafe`；`tensor` 分配要求 `gemm = prefill`（让解码 graph 路径零分配）；单条语句可用 `gemm[prefill](...)` / `gemm[graphsafe](...)` 覆盖 header 模式。

四个公开 header 臂（[L24-L52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L24-L52)），以全量臂为例：

```rust
#[macro_export]
macro_rules! typed_pipeline {
    (
        ctx = $ctx:expr, eps = $eps:expr, seq_len = $seq_len:expr, gemm = $gemm_mode:ident;
        $($rest:tt)*
    ) => {
        $crate::typed_pipeline!(@steps $ctx, $eps, $gemm_mode, (@some $seq_len); $($rest)*)
    };
```

- [L26-L31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L26-L31)：全量 header。`$gemm_mode:ident` 接收任意标识符，模式合法性由后面的 `@gemm` 终结臂把关。
- [L33-L38](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L33-L38)：写了 `seq_len` 却没写 `gemm = prefill` → `compile_error!("typed_pipeline `seq_len = ...` requires `gemm = prefill`")`。
- [L40-L45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L40-L45)：`ctx/eps/gemm` 三键形态，seq_len 标记为 `(@none)`。
- [L47-L52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L47-L52)：最简两键形态，模式直接填默认值 `graphsafe`。
- [L54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L54)：`(@steps ...; ) => {};`——空语句队列的基例，递归在这里终止。

seq_len 的「有/无」被编码成 `(@some $seq_len:expr)` / `(@none)` 两个 token 组：`$seq_len:tt` 能整体接住这两种标记继续下传，这是 macro_rules 里表达 Option 的惯用法。

#### 4.1.4 代码实践：编译期门禁探针

1. **实践目标**：用三个「故意写错」的 pipeline，验证 4.1.2 表格里三个门禁确实各自报出专属错误。
2. **操作步骤**：在本 crate 的 `tests/` 下新建 `dsl_gate_probe.rs`（本目录 tests 为自动发现，无需登记 Cargo.toml；示例代码）：

```rust
// 示例代码：三个应当编译失败的探针
use pegainfer_kernels::typed_pipeline;

fn probe_missing_gemm(ctx: &pegainfer_kernels::tensor::DeviceContext, n: usize) -> anyhow::Result<()> {
    typed_pipeline! {
        ctx = ctx, eps = 1e-5, seq_len = n;      // 漏掉 gemm = prefill
        tensor t: 64;
    }
    let _ = t; Ok(())
}

fn probe_graphsafe_alloc(ctx: &pegainfer_kernels::tensor::DeviceContext, n: usize) -> anyhow::Result<()> {
    typed_pipeline! {
        ctx = ctx, eps = 1e-5, seq_len = n, gemm = graphsafe;  // graphsafe 下分配
        tensor t: 64;
    }
    let _ = t; Ok(())
}

fn probe_no_seqlen(ctx: &pegainfer_kernels::tensor::DeviceContext) -> anyhow::Result<()> {
    typed_pipeline! {
        ctx = ctx, eps = 1e-5;                   // 无 seq_len 却要分配
        tensor t: 64;
    }
    let _ = t; Ok(())
}
```

3. **需要观察的现象**：`PEGAINFER_CUDA_SM=120 cargo check --release -p pegainfer-kernels --tests`（无 GPU 环境用 SM 覆盖；build.rs 仍需 nvcc）应当编译失败，且三条错误分别指向 [L37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L37)、[L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L60)、[L90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L90) 的三条 `compile_error!` 文案。
4. **预期结果**：错误文案逐一命中——「`seq_len = ...` requires `gemm = prefill`」「tensor allocation requires `gemm = prefill`」「tensor statements require `seq_len = ...`」。**待本地验证**（本讲未在本机执行该命令）。
5. 观察完删除该探针文件，避免污染测试目录。

#### 4.1.5 小练习与答案

**练习 1**：header 写 `ctx, eps, seq_len, gemm = graphsafe;` 合法吗？随后写 `tensor t: 64;` 呢？
答案：header 本身合法（[L26-L31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L26-L31) 的 `$gemm_mode:ident` 不挑值），但 `tensor` 语句会命中 [L56-L61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L56-L61) 的 graphsafe+分配守卫臂而编译失败。

**练习 2**：为什么不把模式合法值直接写成字面量 `prefill` / `graphsafe` 两个臂？
答案：用 `$gemm_mode:ident` 透传，同一套 `@steps` 机器可以同时服务 header 模式与语句级覆盖（`gemm[$stmt_mode]`，[L101-L107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L101-L107)），模式对不对由 `@gemm` 终结臂最终把关。

**练习 3**：`(@none)` 标记的 pipeline 里为什么连 `tensor` 都不让写？
答案：`tensor` 语句的展开需要调用 `GpuTensor::zeros(ctx, seq_len)`，`(@none)` 意味着 header 没提供 seq_len，无从分配——[L86-L91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L86-L91) 直接报「tensor statements require `seq_len = ...` in the header」。

### 4.2 与 typed_ops 的对应关系：一条语句 = 一次函数调用

#### 4.2.1 概念说明

`typed_pipeline!` **不实现任何新算子**。它的每条语句都原样展开为一个已存在的 `typed_ops` 函数调用（或 `GpuTensor::zeros` / `std::mem::swap` 这类标准动作）。这意味着：

- 语义完全透明：想知道一条 DSL 语句干了什么，查下表即可，不必猜；
- 等价可验证：宏链与手写链可以用 `cargo expand` 逐行对照（4.3.4 实践）；
- 性能零损耗：宏在编译期消失，运行时就是那几个函数调用。

#### 4.2.2 核心流程

语句目录与展开对照表（行号指向宏规则所在的臂）：

| DSL 语句 | 展开（伪形式） | 落点 | 规则行号 |
|---|---|---|---|
| `tensor n: DIM;` | `let mut n = GpuTensor::<{DIM}>::zeros(ctx, seq_len)?;` | [tensor.rs:L657-L658](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L657-L658) | [L70-L76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L70-L76) |
| `tensor_at n: DIM, len;` | 同上，但用语句自带 `len` | 同上 | [L78-L84](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L78-L84) |
| `gemm(x => y, w);` | `gemm_into` 或 `gemm_graphsafe_into`（按 header 模式） | [typed_ops.rs:L22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L22) / [L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L50) | [L93-L99](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L93-L99) + [L165-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L165-L171) |
| `gemm[mode](x => y, w);` | 同上，按语句级 `mode` 覆盖 | 同上 | [L101-L107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L101-L107) |
| `rms_norm(x => y, w);` | `rms_norm_into(ctx, x, &w, eps, y)?` | [typed_ops.rs:L80](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L80) | [L109-L115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L109-L115) |
| `add(a, b => y);` | `add_into(ctx, a, b, y)?` | [typed_ops.rs:L152](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L152) | [L117-L123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L117-L123) |
| `bf16_to_f32(x => y);` | `bf16_to_f32_into(ctx, x, y)?` | [typed_ops.rs:L243](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L243) | [L125-L131](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L125-L131) |
| `f32_to_bf16(x => y);` | `f32_to_bf16_into(ctx, x, y)?` | [typed_ops.rs:L269](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L269) | [L133-L139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L133-L139) |
| `swap(a, b);` | `::std::mem::swap(a, b);`（无 `?`） | std | [L141-L147](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L141-L147) |
| `try EXPR;` | `EXPR?;` | 任意返回 `Result` 的调用 | [L149-L155](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L149-L155) |
| `call EXPR;` | `EXPR;` | 任意普通调用 | [L157-L163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L157-L163) |

引用的传递规则（读 DSL 类型是否匹配的关键）：

- `x` / `y` / `a` / `b` 等张量表达式**原样透传**：调用处要给出 `&buf`、`&mut buf` 这类引用形态（或绑定本身是引用变量）。文件头 L10-L18 的示例是 `ignore` 的示意风格，真实调用处以引用表达式为准（可在模型 crate 里搜 `typed_pipeline!` 对照，**待确认**具体写法惯例）。
- `w` 由宏自动加 `&`：调用处写权重值或引用都可以（`&&T` 会被解引用强制折回 `&T`）。
- 两个类型陷阱：`bf16_to_f32` 的目标是普通 `CudaSlice<f32>` 而不是 `GpuTensor`；`f32_to_bf16` 的输入同理（见 4.2.3）。

#### 4.2.3 源码精读

**分配语句**。[L70-L76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L70-L76)：

```rust
(@steps $ctx:expr, $eps:expr, $gemm_mode:ident, (@some $seq_len:expr);
    tensor $name:ident : $dim:path;
    $($rest:tt)*
) => {
    let mut $name = $crate::tensor::GpuTensor::<{ $dim }>::zeros($ctx, $seq_len)?;
    $crate::typed_pipeline!(@steps $ctx, $eps, $gemm_mode, (@some $seq_len); $($rest)*)
};
```

`$dim:path` 允许写 `KIMI_K2_MLA_QKV_A_OUT` 这类常量路径，`<{ $dim }>` 把它放进 const-generic 块。`GpuTensor::zeros` 定义在 [src/tensor.rs:L657-L658](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L657-L658)（`pub fn zeros(ctx: &DeviceContext, seq_len: usize) -> Result<Self>`），结构体本体在 [L652](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L652)。`tensor_at`（[L78-L84](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L78-L84)）只是把长度换成语句自带的第二个表达式，header 的默认长度继续下传。

**GEMM 两级分派**。语句臂只做转发（[L93-L99](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L93-L99)）：header 模式给 `@gemm $gemm_mode`；带方括号的语句（[L101-L107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L101-L107)）改传 `$stmt_mode`，这就是「单条语句覆盖 header」的实现。终结臂只有两条（[L165-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L165-L171)）：

```rust
(@gemm graphsafe, $ctx:expr, $x:expr, $y:expr, $w:expr) => {
    $crate::typed_ops::gemm_graphsafe_into($ctx, &$w, $x, $y)?
};
(@gemm prefill, $ctx:expr, $x:expr, $y:expr, $w:expr) => {
    $crate::typed_ops::gemm_into($ctx, &$w, $x, $y)?
};
```

两个函数签名一致（承接 u2-l2 §4.3）：`gemm_into`（[typed_ops.rs:L22-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L22-L47)）与 `gemm_graphsafe_into`（[L50-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L50-L75)），差别只在传给 `launch_gemm` 的 `graphsafe` 布尔值：前者传 `x.seq_len == 1`（[L44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L44)——单 token 形状视同解码），后者恒为 `true`。`launch_gemm`（[L465-L506](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L465-L506)）据此选择 `ffi::gemm_graphsafe_cuda`（workspace-free cuBLAS 句柄，见 [L49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L49) 的文档注释）或 `ffi::gemm_cuda`，并在启动前先做 `ensure_tuned_policy(m, n, k)` 检查（[L475](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L475)）。三个形状参数是 `m=OUT, n=x.seq_len, k=IN`（[L41-L43](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L41-L43)），即 `Y = W @ X`、`X:[IN,bs]`、`Y:[OUT,bs]`。

**逐元素与转换语句**。`rms_norm` 臂（[L109-L115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L109-L115)）展开为 `rms_norm_into(ctx, x, &w, eps, y)?`——注意 eps 是 header 注入的，所以**同一 pipeline 内所有 rms_norm 共享一个 eps**；函数签名在 [typed_ops.rs:L80-L86](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L80-L86)（`(ctx, x: &GpuTensor<DIM>, w: &NormWeight<DIM>, eps: f32, out: &mut GpuTensor<DIM>)`，`NormWeight` 本体在 [tensor.rs:L700](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L700)）。`add` 臂（[L117-L123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L117-L123)）落到 [add_into](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L152-L157)（三者同 DIM、seq_len 相等，[L158-L164](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L158-L164) 有运行时 ensure）。两个 dtype 转换臂落到的函数签名值得抄一遍（[typed_ops.rs:L243-L247](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L243-L247) 与 [L269-L273](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L269-L273)）：`bf16_to_f32_into(ctx, x: &GpuTensor<DIM>, out: &mut CudaSlice<f32>)`、`f32_to_bf16_into(ctx, x: &CudaSlice<f32>, out: &mut GpuTensor<DIM>)`——f32 侧是裸切片（文档注释注明用途是确定性 all-reduce，[L242](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L242)）。`swap` 臂（[L141-L147](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L141-L147)）展开为 `::std::mem::swap(a, b);`，不带 `?`：交换的是两个既有 `GpuTensor` 绑定的内容，零分配、不碰 CUDA，正是解码路径复用预分配缓冲的手段（承接 u2-l1 的指针稳定结论）。

#### 4.2.4 代码实践：手工展开对照（无需 GPU）

1. **实践目标**：不依赖任何工具，验证 4.2.2 表格的每一行。
2. **操作步骤**：把 [L10-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L10-L18) 的示例链逐条翻译成手写 Rust：`tensor qkv_a: ...` 查分配行、`rms_norm(...)` 查 rms_norm 行、`gemm(...)` 先按 header 模式（此处 prefill）选定 `gemm_into`、`try ...` 补 `?`。
3. **需要观察的现象**：翻译结果应当是 4 句普通 Rust 语句，其中没有任何一个函数是 typed_ops 之外的新名字。
4. **预期结果**：手写链与 4.1.1 的手写形态一致；若某条语句在表里找不到落点，说明你写出了 DSL 不支持的语法（会在编译期无臂可配而报错）。

#### 4.2.5 小练习与答案

**练习 1**：`rms_norm(a => b, w);` 展开后 eps 从哪来？多个 rms_norm 语句可以用不同 eps 吗？
答案：来自 header 的 `$eps`（[L26-L52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L26-L52) 一路透传，[L113](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L113) 使用）；同一 pipeline 内共享，不能逐语句覆盖（要换 eps 就换一个 pipeline 或用逃逸口直调）。

**练习 2**：`gemm[graphsafe](x => y, w);` 出现在 `gemm = prefill` 的链里，展开成什么？
答案：语句级 `mode` 覆盖 header（[L101-L107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L101-L107) 转发 `$stmt_mode`），终结臂选 `gemm_graphsafe_into`（[L165-L167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L165-L167)）。

**练习 3**：`gemm_into` 在 prefill 模式下什么时候实际走 graphsafe 路径？
答案：`x.seq_len == 1` 时（[typed_ops.rs:L44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L44) 传布尔，[launch_gemm L477-L497](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs#L477-L497) 分派到 `ffi::gemm_graphsafe_cuda`）——单 token 形状按解码对待，天然是 graph 安全的。

### 4.3 逃逸口：try / call 与「故意不扩展」的边界

#### 4.3.1 概念说明

DSL 的价值来自它的「小」。如果把模型专属 kernel（MLA 拆分、MoE 路由、注意力……）都做成宏语句，宏会无限膨胀，而它们各自的参数形态千差万别，声明式语法反而更难读。所以设计者把边界画在「typed_ops 已有的标准调用」，其余全部退回普通 Rust：

- `try EXPR;`——`EXPR` 是任何返回 `Result` 的调用，展开为 `EXPR?;`，错误随链传播；
- `call EXPR;`——任何普通调用/副作用，展开为 `EXPR;`，返回值被丢弃。

两者合起来覆盖了「模型专属 kernel + 日志/断言/宿主侧小动作」两类需求，而且逃逸表达式里可以直接引用宏内新建的 `tensor` 绑定（所有标识符都是调用处的 token，无 hygiene 隔离问题）。

什么时候应当**整体放弃** DSL、回到手写 typed_ops 链：

- 出现条件分支、循环、提前返回等控制流（宏语句是线性的）；
- 中间量依赖运行时 shape 计算（DSL 只认 const-generic 维度）；
- 某步需要把张量传给 typed_ops 里没有对应函数的 op——虽然可以全程 `try` 串，但那样 DSL 只剩噪音，不如手写。

判断标准一句话：**当一条链里逃逸语句占比过高，DSL 就失去了压缩样板的意义**。

#### 4.3.2 核心流程

```text
写新语句前先问：
  是 tensor 分配且在 prefill 链里？ → tensor / tensor_at
  是 W @ X 线性投影？              → gemm（必要时 [mode] 覆盖）
  是 rms_norm / add / dtype 转换？  → 对应语句
  是两个既有缓冲互换？              → swap
  返回 Result 的其它调用？          → try EXPR;
  普通调用/副作用？                 → call EXPR;
  都不是（控制流、shape 计算）？    → 放弃 DSL，手写 typed_ops 链
```

#### 4.3.3 源码精读

逃逸口的两条臂是全文件最短的规则（[L149-L163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L149-L163)）：

```rust
(@steps $ctx:expr, $eps:expr, $gemm_mode:ident, $seq_len:tt;
    try $call:expr;
    $($rest:tt)*
) => {
    $call?;
    $crate::typed_pipeline!(@steps $ctx, $eps, $gemm_mode, $seq_len; $($rest)*)
};

(@steps $ctx:expr, $eps:expr, $gemm_mode:ident, $seq_len:tt;
    call $call:expr;
    $($rest:tt)*
) => {
    $call;
    $crate::typed_pipeline!(@steps $ctx, $eps, $gemm_mode, $seq_len; $($rest)*)
};
```

- `try` 臂（[L149-L155](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L149-L155)）：展开成 `$call?;`，错误并入外层函数的 `Result`（u2-l3 的 anyhow 链）。
- `call` 臂（[L157-L163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L157-L163)）：展开成 `$call;`。注意 `call` 一个返回 `Result` 的函数不会传播错误，只会得到 `unused_must_use` 警告——要传播就用 `try`。
- 两臂的 `$call:expr` 都是调用处 token，可以引用链上先前 `tensor` 语句创建的绑定（如文档示例里 `try kimi_mla_split_qkv_a(ctx, &qkv_a, ...)` 直接用了上一行分配的 `qkv_a`，[L16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L16)）。
- 兜底安全网：写出任何不匹配的语句形态时，`@steps` 没有臂可配，编译器直接在宏调用处报「no rules expected the token」，不存在静默忽略。

#### 4.3.4 代码实践：三步链 + cargo expand 对照（本讲指定实践）

1. **实践目标**：写一段 `RMSNorm → GEMM → call 逃逸` 的三步链，用 `cargo expand` 拿到展开产物，与手写版本逐行对照，量化 DSL 省掉的样板。

2. **操作步骤**：

   a. 在 `tests/` 下新建 `dsl_expand_probe.rs`（示例代码；tests 自动发现，无需改 Cargo.toml）：

```rust
// 示例代码：三步链探针
use anyhow::Result;
use pegainfer_kernels::tensor::{DeviceContext, GpuTensor, GpuWeight, NormWeight};
use pegainfer_kernels::typed_pipeline;

fn note_step(seq_len: usize) {
    println!("escape hatch: produced {seq_len} rows");
}

fn probe_step(
    ctx: &DeviceContext,
    hidden: &GpuTensor<128>,
    norm_w: &NormWeight<128>,
    proj: &GpuWeight<64, 128>,
    seq_len: usize,
) -> Result<GpuTensor<64>> {
    typed_pipeline! {
        ctx = ctx, eps = 1e-5, seq_len = seq_len, gemm = prefill;
        tensor normed: 128;
        tensor y: 64;
        rms_norm(hidden => &mut normed, norm_w);
        gemm(&normed => &mut y, proj);
        call note_step(seq_len);
    }
    Ok(y)
}
```

   b. 展开宏：`cargo expand -p pegainfer-kernels --test dsl_expand_probe`（需先 `cargo install cargo-expand`，且依赖 nightly 工具链的 `-Zunpretty`；**待本地验证**环境是否具备）。
   c. 若 cargo-expand 不可用：退回 4.2.4 的手工展开，按表逐条翻译，效果等同。

3. **需要观察的现象**：展开产物中 `typed_pipeline!` 消失，取而代之的应当依次是——`let mut normed = pegainfer_kernels::tensor::GpuTensor::<128>::zeros(ctx, seq_len)?;`、`let mut y = ...zeros...?;`、`pegainfer_kernels::typed_ops::rms_norm_into(ctx, hidden, &norm_w, 1e-5, &mut normed)?;`、`pegainfer_kernels::typed_ops::gemm_into(ctx, &proj, &normed, &mut y)?;`、`note_step(seq_len);`（`$crate::` 前缀以绝对路径出现，`&$w` 自动加引用）。**待本地验证**（本讲未在本机执行）。

4. **预期结果**：宏链 6 行语句对应展开 5 行普通 Rust；手写同样链需要每次写全 `pegainfer_kernels::typed_ops::…`（或 use）+ 4 个实参 + `?`。统计两种写法的字符数/行数差，写进你的笔记——这就是 DSL 的全部收益：**纯样板压缩，零语义增量**。

5. **运行时验证（可选，需 GPU）**：给 `probe_step` 补一个测试：随机权重 + CPU 参考实现对拍（rms_norm 参考可参考 u3-l6 的做法）。无 GPU 时跳过，此步**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一行说清 `try` 与 `call` 的差别。
答案：`try EXPR;` 展开为 `EXPR?;`（错误沿链传播，[L153](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L153)）；`call EXPR;` 展开为 `EXPR;`（普通执行、返回值丢弃，[L161](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L161)）。

**练习 2**：为什么复用缓冲用 `swap` 而不是赋值/新增拷贝语句？
答案：`::std::mem::swap`（[L145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L145)）在宿主侧交换两个既有 `GpuTensor` 的内容，零设备操作、零分配，保住预分配缓冲的指针稳定（承接 u2-l1 / u4-l3）；DSL 因此不需要任何 CUDA 拷贝语句。

**练习 3**：一条链里 80% 是 `try` 包裹的模型专属 kernel，该继续用 DSL 吗？
答案：不该。逃逸语句没有压缩效果，还额外套了一层宏语法；直接手写 typed_ops 链（两者展开等价，见 4.2），可读性更好。

## 5. 综合实践

把本讲三块内容串成一个「展开审计」小任务：

1. **写**：以 [L10-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L10-L18) 官方示例为蓝本，改写成引用形态可编译的版本（参照 4.3.4 的探针），要求覆盖：1 条 `tensor`、1 条 `rms_norm`、1 条 `gemm`、1 条 `gemm[graphsafe]` 覆盖、1 条 `try`、1 条 `swap`。
2. **审**：用 `cargo expand`（或手工按 4.2.2 表格）展开，逐行标注每行来自哪条语句、落到 typed_ops 哪个函数哪个行号。
3. **验**：故意做两个破坏实验——(a) header 去掉 `gemm = prefill`；(b) `gemm` 写成 `gemm[quantized]`——分别观察 `compile_error!`（[L37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/forward_pass.rs#L37)）与「无臂可配」两类报错的差异。
4. **量**：统计等价手写链与 DSL 链的行数/字符数，写一句结论：这条链用 DSL 值不值。
5. 全程产物只是你自己的笔记与临时测试文件，**不要提交**到仓库；所有需要编译/运行的结果均**待本地验证**。

## 6. 本讲小结

- `typed_pipeline!` 是纯语法糖：header 归一化（4 种形态）+ `@steps` 递归消化语句 + `@gemm` 终结分派，每条语句恰好展开为一个 `typed_ops` 调用（或 zeros / mem::swap / 逃逸表达式）。
- 11 种语句里，`tensor`/`tensor_at` 管分配、`gemm`（含 `[mode]` 覆盖）管线性投影、`rms_norm`/`add`/`bf16_to_f32`/`f32_to_bf16` 管逐元素、`swap` 管缓冲复用、`try`/`call` 是逃逸口。
- 分配被三重门禁锁在 `gemm = prefill` + `seq_len` 之下，落点是「解码/graph 回放路径零分配」这条引擎纪律。
- `gemm` 模式两级可控：header 定基调、`gemm[...]` 单条覆盖；prefill 模式下单 token 形状自动走 graphsafe 路径（`x.seq_len == 1`）。
- 逃逸口的语义就一行：`try` 是 `?`、`call` 是普通语句；逃逸占比过高的链应当整体放弃 DSL。
- 验证等价性的两条路：`cargo expand` 逐行对照，或按 4.2.2 对照表手工展开——两者都不需要 GPU。

## 7. 下一步学习建议

- **u4-l3（CUDA Graph 安全与流管理）**：本讲反复出现的 graphsafe/prefill 之分，其下游原因（workspace-free 句柄、指针稳定、`set_batch_size` 重放）在那里展开。
- **u3-l1（ops.rs 分发与算子包装解剖）**：从 DSL 再往下沉一层，看 typed_ops 之下的 ops wrapper 与 FFI 入口如何组织。
- **模型 crate 实地观察**：到本 crate 之外的模型 crate（kimi-k2 线的 `pegainfer-kimi-k2`）里搜索 `typed_pipeline!` 的真实调用，对照本讲的语句表核对引用形态与逃逸用法（此目录在本 crate 工作区之外，**待确认**具体文件位置）。
- 若你还没读 u2-l2 的 `gpu_buffers!` 一节，建议补读：它与本讲的 `tensor` 语句是「预分配缓冲 vs 链内分配」的互补两半。
