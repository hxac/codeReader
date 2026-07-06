# MLIR 方言层：dialect-mir 与 dialect-nvvm

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 cuda-oxide 为什么在编译流水线里要分 **两层方言（dialect）**，而不是直接把 Rust 翻译成 LLVM IR。
2. 区分 `dialect-mir`（高层、贴近 Rust 语义）与 `dialect-nvvm`（低层、贴近 PTX/NVVM 指令）各自的职责。
3. 在每一层里挑出典型 op（`mir.add`/`mir.store`/`mir.cond_branch` vs `nvvm.read_ptx_sreg_tid_x`/`nvvm.atomic`/`nvvm.mma_*`），判断它属于 Rust 语义级还是直接对应某条 PTX 指令。
4. 理解「方言切换」在整条流水线中的位置：哪些 op 从入口到出口都待在 `dialect-mir`，哪些 op 在翻译期就被直接写成 `dialect-nvvm`，最后两者都由 `mir-lower` 收敛到 LLVM IR。
5. 看懂本轮（#327/#328/#329）在 `dialect-nvvm/ops/wmma.rs` 里新增的三条 mma.sync op（f16 / tf32 / s8）是怎么挂在两层方言体系里的。

---

## 2. 前置知识

本讲默认你已经读过：

- **u1-l1 / u4-l1**：知道 cuda-oxide 是一个被 rustc `dlopen` 的自定义代码生成后端，设备端走 `Rust MIR → dialect-mir → … → LLVM IR → PTX` 这条流水线。
- **u4-l2**：知道 `mir-importer` 负责把 rustc 的 stable MIR 翻译成基于 **Pliron** 的 IR，后段（mem2reg、lowering、导出、llc）委托给 `cuda-oxide-codegen` 编排。

这里再补三个本讲要用到的概念：

- **IR（Intermediate Representation，中间表示）**：编译器在「源码」和「目标机器码」之间用的中间数据结构。把翻译拆成多个 IR 层级，每一层只关心一类问题，是 LLVM、MLIR 这类现代编译器的通用做法。
- **方言（dialect）**：borrowed 自 MLIR 的概念。一个「方言」就是一组命名空间隔离的 op（操作）和 type（类型），名字带前缀，比如 `mir.add`、`nvvm.read_ptx_sreg_tid_x`。多个方言可以共存于同一段 IR，互不冲突。Pliron 是 cuda-oxide 使用的类 MLIR 的 Rust 原生多方言 IR 框架。
- **op（operation，操作）**：IR 里的最小语义单元，可以理解为「一条带输入操作数（operand）和输出结果（result）的指令」。例如 `mir.add` 吃两个操作数、产一个结果；`nvvm.read_ptx_sreg_tid_x` 吃零个操作数、产一个 `i32`。

一个直观比喻：如果 LLVM IR 是「全世界通用的世界语」，那么方言就是「专业术语词典」。`dialect-mir` 是「Rust 程序员词典」（结构体、枚举、引用、判别式……），`dialect-nvvm` 是「GPU 指令词典」（线程号、warp shuffle、张量核 mma……）。翻译流程先用 Rust 词典把语义写下来，再逐层换成 GPU 词典，最后统一成 LLVM IR。

---

## 3. 本讲源码地图

本讲聚焦「方言定义」本身，主要读两个 crate 的「门面 + ops 目录索引」，外加一处「方言切换」证据：

| 文件 | 作用 |
| --- | --- |
| `crates/dialect-mir/src/lib.rs` | `dialect-mir` 的入口：注册方言名 `"mir"`，并把 ops/types/attributes 注册到 Pliron 上下文。 |
| `crates/dialect-mir/src/ops/mod.rs` | `dialect-mir` 全部 op 的总目录与注册表，含一张「模块 ↔ op 数量」对照表与校验策略说明。 |
| `crates/dialect-mir/src/ops/arithmetic.rs` | 高层算术 op 样例（`mir.add`/`mir.sub`/`mir.mul` …），体现「类型一致性校验」。 |
| `crates/dialect-nvvm/src/lib.rs` | `dialect-nvvm` 的入口：注册方言名 `"nvvm"`，文档点明它映射到 LLVM 的 NVPTX 后端 intrinsic。 |
| `crates/dialect-nvvm/src/ops/mod.rs` | `dialect-nvvm` 全部 op 的总目录与注册表，含「按 GPU 架构分级」表与「最小结构校验」策略说明。 |
| `crates/dialect-nvvm/src/ops/wmma.rs` | warp 级矩阵乘 op 集合，**本轮新增的 f16/tf32/s8 三条 mma.sync 就在这里**。 |
| `crates/dialect-nvvm/src/ops/thread.rs`、`atomic.rs` | `dialect-nvvm` 典型 op 样例：线程索引读取、原子操作，体现「直接对应 PTX 寄存器/指令」。 |
| `crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs` | 「方言切换」证据：mir-importer 在翻译 mma intrinsic 时**直接生成 `dialect-nvvm` 的 mma op**。 |
| `crates/mir-lower/src/convert/intrinsics/wmma.rs` | 收敛点：把 `dialect-nvvm` 的 mma op 翻译成 LLVM 内联汇编（即 PTX）。 |

> 提示：本讲只读「方言定义层」和「切换证据」，不展开 mir-lower 的 lowering 细节——那是 u4-l4 / u6-l3 的主题。

---

## 4. 核心概念与源码讲解

### 4.1 为什么要分两层方言：dialect-mir 与 dialect-nvvm

#### 4.1.1 概念说明

cuda-oxide 的目标是「把纯 Rust 编译成 GPU 代码」。这件事的难点在于：Rust 的语义（结构体字段、枚举判别式、引用、生命周期）和 GPU 的语义（线程、warp、共享内存、张量核）几乎是两个世界。

如果把它们一锅炖，直接从 Rust MIR 翻译到 LLVM IR，会遇到两个麻烦：

1. **Rust 特有概念无处安放**。比如「取枚举的判别式」「按字段下标访问结构体」这类操作，LLVM IR 里没有对应指令，需要一套自己的 op 来承载，否则翻译器要把这些细节到处散落写死。
2. **GPU 特有概念也无处安放**。比如「读当前线程号 `%tid.x`」「做一次 warp shuffle」「发起一次 mma.sync 张量核乘法」，这些是 NVPTX 后端才懂的东西，也不该和普通整数加法混在一起。

cuda-oxide 的解法 borrowed 自 MLIR：**用两个方言分而治之**。

- `dialect-mir`（名字前缀 `mir.`）：**高层方言**，贴近 Rust 语义。承载算术、内存、控制流、结构体/元组/枚举、函数等 Rust 程序里就有的概念。
- `dialect-nvvm`（名字前缀 `nvvm.`）：**低层方言**，贴近 PTX/NVVM 指令。承载线程索引、warp、原子、集群、TMA、张量核（mma/wgmma/tcgen05）等 GPU 才有的概念。

两层方言共存于同一段 Pliron IR 里，最后统一由 `mir-lower` 收敛到 LLVM IR。这样每个翻译阶段只面对一个世界的概念，复杂度被切开。

#### 4.1.2 核心流程

两层方言在流水线里的关系（承接 u4-l2）：

```text
rustc MIR
   │  mir-importer 翻译
   ▼
┌─────────────────────────────────────────────────────┐
│  Pliron IR（同时容纳两个方言）                       │
│                                                     │
│   普通代码：mir.func / mir.add / mir.load / …       │  ← dialect-mir
│   GPU intrinsic：nvvm.read_ptx_sreg_tid_x /         │  ← dialect-nvvm
│                  nvvm.mma_m16n8k16_f32_f16 / …      │
└─────────────────────────────────────────────────────┘
   │  cuda-oxide-codegen 编排：mem2reg / unroll / lower
   ▼
mir-lower 把两个方言的 op 都翻译成 LLVM IR（普通算术走 convert/ops/，
GPU intrinsic 走 convert/intrinsics/）
   │
   ▼
LLVM IR → llc/NVPTX → PTX
```

关键点：**方言切换不是一个「整段 IR 从 mir 变成 nvvm」的瞬间事件**，而是「按 op 粒度，在需要 GPU 语义的地方就地用 nvvm op 替换/补充」。一段 kernel 函数的 IR 里，`mir.add` 和 `nvvm.read_ptx_sreg_tid_x` 完全可以肩并肩存在。

#### 4.1.3 源码精读

两个方言的入口几乎是镜像的。先看 `dialect-mir` 的门面：

[crates/dialect-mir/src/lib.rs:L6-L25](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-mir/src/lib.rs#L6-L25) —— 中文说明：定义 `MIR_DIALECT_NAME = "mir"`，`register(ctx)` 把方言名、ops、types、attributes 依次注册进 Pliron 上下文。这是「Rust 词典」的扉页。

再看 `dialect-nvvm` 的门面，它的文档注释把定位说得非常直白：

[crates/dialect-nvvm/src/lib.rs:L6-L22](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/lib.rs#L6-L22) —— 中文说明：注释写明「This dialect maps to LLVM's NVPTX backend intrinsics」（本方言映射到 LLVM 的 NVPTX 后端 intrinsic），方言名 `"nvvm"`。这是「GPU 词典」的扉页。

注意两者注册子项的差异：`dialect-mir` 多注册了 `types` 和 `attributes`（因为它要建模 Rust 的指针、元组、枚举等类型），而 `dialect-nvvm` 只注册 `ops`（它复用 Pliron 内置的基础类型如 `i32`/`f32`，不需要自己的类型系统）。

#### 4.1.4 代码实践

1. **实践目标**：建立「两层方言共存」的直觉。
2. **操作步骤**：
   - 打开 `crates/dialect-mir/src/lib.rs` 与 `crates/dialect-nvvm/src/lib.rs`，对比两个 `register(ctx)` 函数注册了哪些子模块。
   - 在仓库根目录执行 `grep -rn "\"mir\\." crates/mir-lower/src/convert/ops/ | head` 与 `grep -rn "\"nvvm\\." crates/mir-lower/src/convert/ | head`，你会看到 `mir.*` 出现在 `convert/ops/`，而 `nvvm.*` 出现在 `convert/intrinsics/`——这正是两条并行的收敛路径。
3. **需要观察的现象**：两套 op 名前缀（`mir.` 与 `nvvm.`）在 `mir-lower` 里被分别处理，互不干扰。
4. **预期结果**：能在 `convert/ops/` 找到 `mir.add` 的 lowering，在 `convert/intrinsics/` 找到 `nvvm.mma_*` 的 lowering。
5. 本实践为源码阅读型，**待本地验证** grep 命令在你环境中的实际命中行。

#### 4.1.5 小练习与答案

**练习 1**：如果 cuda-oxide 不分两层方言，把 `nvvm.read_ptx_sreg_tid_x` 也叫成 `mir.read_tid_x`，会出什么问题？

**参考答案**：从工程上看，命名空间不再隔离，op 定义会混在一起难以维护；更重要的是两类 op 的**校验策略完全不同**（见 4.4），混在一个方言里要么过度校验、要么校验不足，失去「按语义层级精准校验」的好处。

**练习 2**：`dialect-nvvm` 的入口为什么不像 `dialect-mir` 那样注册自己的 `types` 模块？

**参考答案**：因为 `dialect-nvvm` 的 op 只搬运 GPU 寄存器级的标量（`i32`/`f32`/`f64` 等），这些类型 Pliron 内置就有；而 `dialect-mir` 要建模 Rust 的元组、枚举、带地址空间的指针等 Rust 特有类型，所以需要自己的 types。

---

### 4.2 dialect-mir ops 概览：Rust 语义的 IR 化

#### 4.2.1 概念说明

`dialect-mir` 是 mir-importer 翻译 rustc MIR 的**直接产物**。它把 Rust 的中间表示「几乎一对一」地搬进 Pliron IR，保留了 Rust 的类型语义。它的 op 可以粗略分成几类，每一类对应 MIR 里的一个概念族：

| 类别 | 典型 op | 对应的 Rust/MIR 概念 |
| --- | --- | --- |
| 函数 | `mir.func` | 函数定义 |
| 控制流 | `mir.return`、`mir.cond_branch`、`mir.goto` | MIR 的终止符（terminator） |
| 内存 | `mir.alloca`、`mir.load`、`mir.store` | 栈槽、读写（每个 local 一个栈槽） |
| 常量 | `mir.const_int`、`mir.const_float` | 整型/浮点字面量 |
| 算术 | `mir.add`、`mir.sub`、`mir.mul`、位运算、移位 | 二元运算 |
| 比较 | `mir.*_eq`、`mir.*_lt` 等 | 关系与相等比较 |
| 聚合 | `mir.extract_field`、`mir.insert_field` | 结构体/元组的字段访问 |
| 枚举 | `mir.get_discriminant`、`mir.set_discriminant` | 枚举判别式读写 |
| 类型转换 | `mir.cast` | IntToInt、FloatToFloat 等 |
| 调用 | `mir.call` | 函数调用 |

这些 op 的共同点是：**它们都在说「Rust 程序想干什么」，而不是「GPU 怎么干」**。一个 `mir.add` 既可能是两个 `i32` 相加，也可能是两个 `f32` 相加——具体是什么，由操作数的类型决定，op 本身不关心。

#### 4.2.2 核心流程

mir-importer 把一个 MIR 基本块翻译成一串 `dialect-mir` op，规则很朴素（承接 u4-l2 的「alloca + load/store」模型）：

```text
MIR local 变量  ──►  mir.alloca <ty>        （开栈槽）
写入 local      ──►  mir.store <val>, <ptr> （写栈槽）
读取 local      ──►  mir.load <ptr>         （读栈槽）
二元运算        ──►  mir.add/mir.sub ... <a>, <b>
结构体字段      ──►  mir.extract_field <agg>, <idx>
块结束          ──►  mir.cond_branch / mir.goto / mir.return（终止符）
```

跨基本块的数据流通过栈槽传递（写后存、读前取），SSA 提升这件难事留给后段的 mem2reg。这正是 `dialect-mir` 贴近 MIR 的体现。

#### 4.2.3 源码精读

op 总目录在 `dialect-mir/src/ops/mod.rs` 顶部给出了一张官方对照表：

[crates/dialect-mir/src/ops/mod.rs:L15-L31](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-mir/src/ops/mod.rs#L15-L31) —— 中文说明：列出 12 个 op 子模块、各自职责与 op 数量（如 `function` 1 个、`control_flow` 5 个、`memory` 6 个、`arithmetic` 13 个等）。这是 `dialect-mir` 全貌的一览表。

来看一个最朴素的高层 op——整数或浮点加法 `mir.add`：

[crates/dialect-mir/src/ops/arithmetic.rs:L40-L66](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-mir/src/ops/arithmetic.rs#L40-L66) —— 中文说明：`#[pliron_op(name = "mir.add", ...)]` 派生出 `MirAddOp`，声明它有 2 个操作数、1 个结果；`Verify` 实现检查「两个操作数类型相同」且「结果类型与操作数一致」。注意它**不区分整数加还是浮点加**——这是 Rust 语义级 op 的典型特征，具体指令由 lowering 时按类型决定。

最后所有 op 由一个聚合 `register` 注入上下文：

[crates/dialect-mir/src/ops/mod.rs:L135-L148](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-mir/src/ops/mod.rs#L135-L148) —— 中文说明：`register(ctx)` 依次调用每个子模块的 `register`，把全部 `dialect-mir` op 注册进 Pliron，使其可被解析、校验、打印。

#### 4.2.4 代码实践

1. **实践目标**：感受「一个 Rust op 如何承载类型语义」。
2. **操作步骤**：
   - 打开 `crates/dialect-mir/src/ops/arithmetic.rs`，挑 `mir.add`、`mir.sub`、`mir.mul` 三个 op，对比它们的 `Verify` 实现。
   - 再看 `crates/dialect-mir/src/ops/control_flow.rs` 里 `mir.return` 的 `Verify`（它会校验操作数类型必须匹配所在 `mir.func` 的返回类型）。
3. **需要观察的现象**：算术 op 只校验「类型一致」，控制流 op 还会校验「与父函数签名匹配」——校验深度按语义重要性递增。
4. **预期结果**：你能用一句话概括 `dialect-mir` op 的校验哲学：「保证类型一致 + Rust 不变量成立」。
5. 本实践为源码阅读型，无需运行 GPU。

#### 4.2.5 小练习与答案

**练习**：`mir.add` 既是整数加又是浮点加，lowering 时怎么知道该发 `add i32` 还是 `fadd float`？

**参考答案**：看操作数的类型。`dialect-mir` op 把「是整型还是浮点」编码在操作数的 Pliron 类型里（`IntegerType` vs `FP32Type`），lowering 阶段（u6-l3）根据类型分派到不同的 LLVM 指令。这正是「高层 op 携带类型、低层按类型具化」的分工。

---

### 4.3 dialect-nvvm ops 概览：PTX 指令的 IR 化（含本轮 mma 扩充）

#### 4.3.1 概念说明

`dialect-nvvm` 是 GPU 指令那一侧的方言。它的每一个 op 几乎都**直接对应一条 PTX 指令或一个 NVVM intrinsic**，比如：

| op | 对应的 PTX / NVVM |
| --- | --- |
| `nvvm.read_ptx_sreg_tid_x` | `%tid.x`（块内线程号 X） |
| `nvvm.barrier0` | `bar.sync 0`（块级屏障） |
| `nvvm.threadfence` | `membar.gl`（设备级内存栅栏） |
| `nvvm.atomic_*` | `atom.*` / LLVM `atomicrmw`、`cmpxchg` |
| `nvvm.shfl_*` | `shfl.sync.*`（warp shuffle） |
| `nvvm.mma_m16n8k16_f32_f16` | `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32` |
| `nvvm.mma_m16n8k8_f32_tf32` | `mma.sync.aligned.m16n8k8.row.col.f32.tf32.tf32.f32` |
| `nvvm.mma_m16n8k32_s32_s8` | `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32` |

这些 op 的命名就透着「机器味」——名字里直接编码了指令的形状（m/n/k）、累加器类型、输入类型，因为这些信息在 PTX 指令里就是写死的助记符的一部分。

**本轮（#327/#328/#329）新增的三条 mma op** 就落在 `dialect-nvvm/ops/wmma.rs`：

- `nvvm.mma_m16n8k16_f32_f16`（f16 输入，#327）
- `nvvm.mma_m16n8k8_f32_tf32`（tf32 输入，#328）
- `nvvm.mma_m16n8k32_s32_s8`（s8 输入，#329）

它们与上轮已有的 `nvvm.mma_m16n8k16_f32_bf16`（bf16，#321）、`nvvm.mma_m8n8k4_f64`（f64，#323）以及 `nvvm.movmatrix_trans_b16`（#310）组成同一族 op。

#### 4.3.2 核心流程

一个 `dialect-nvvm` op 从「Rust 调用」到「PTX 指令」要走两步：

```text
设备端 Rust: mma_m16n8k32_s32_s8(acc, a, b)
        │
        │  mir-importer 的 intrinsic 翻译器识别这个名字，
        │  直接生成 dialect-nvvm op（不经 dialect-mir）
        ▼
nvvm.mma_m16n8k32_s32_s8  <10 个 i32 操作数>  → 4 个 i32 结果
        │
        │  mir-lower 的 intrinsics/wmma.rs 把它翻成
        │  LLVM 内联汇编（即 PTX 字符串）
        ▼
LLVM inline asm: mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32 ...
```

注意第一步：mma 这类 op 是 **mir-importer 在翻译 intrinsic 调用时直接产出的 `dialect-nvvm` op**，并没有先变成 `dialect-mir` op 再转换。这是「方言切换」最典型的发生方式——遇到 GPU 专用语义，直接用低层方言写。

#### 4.3.3 源码精读

op 总目录同样给了一张按 **GPU 架构分级** 的官方表：

[crates/dialect-nvvm/src/ops/mod.rs:L16-L30](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/mod.rs#L16-L30) —— 中文说明：列出每个 op 子模块、它需要的 GPU 架构与 op 数量。可见 `thread`/基本 `warp` 适用于所有 GPU，`cluster`/`mbarrier`/`tma`/`wgmma` 需要 Hopper（sm_90+），`tcgen05` 需要 Blackwell（sm_100+）。这是 `dialect-nvvm` 与硬件强绑定的直接体现。

来看一个最直观的低层 op——读块内线程号 X：

[crates/dialect-nvvm/src/ops/thread.rs:L11-L32](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/thread.rs#L11-L32) —— 中文说明：文档表把 `ReadPtxSregTidXOp` 直接对应到 PTX 寄存器 `%tid.x`，并画出 Grid→Block→Thread 的线程层级。op 的含义就是「读一个 32 位整数 = 当前线程在块内的 X 坐标」。

现在看本轮新增的三条 mma op 之一——s8 输入的 m16n8k32：

[crates/dialect-nvvm/src/ops/wmma.rs:L338-L354](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L338-L354) —— 中文说明：`#[pliron_op(name = "nvvm.mma_m16n8k32_s32_s8", interfaces = [NOpdsInterface<10>, NResultsInterface<4>])]` 定义 `MmaM16N8K32S32S8Op`。它吃 10 个 `i32` 操作数（4 个累加器 C + 4 个 A 片段 + 2 个 B 片段，每个 `i32` 打包 4 个 s8），产出 4 个 `i32` 结果（D 累加器）。文档注释清楚写明每个寄存器打包了几个 s8 值。

另外两条本轮新增的 op 定义结构完全对称：

- [crates/dialect-nvvm/src/ops/wmma.rs:L164-L180](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L164-L180) —— `nvvm.mma_m16n8k16_f32_f16`（f16 输入，本轮 #327 新增）：4 个 f32 累加器 + 4 个 i32（每个打包 2 个 f16）A + 2 个 i32 B，产出 4 个 f32。
- [crates/dialect-nvvm/src/ops/wmma.rs:L251-L267](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L251-L267) —— `nvvm.mma_m16n8k8_f32_tf32`（tf32 输入，本轮 #328 新增）：4 个 f32 累加器 + 4 个 i32（每个持有 1 个 tf32）A + 2 个 i32 B，产出 4 个 f32。

对比一下「本轮之前」就存在的 bf16 版本，可见这一族 op 的定义是高度模板化的：

[crates/dialect-nvvm/src/ops/wmma.rs:L74-L90](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L74-L90) —— 中文说明：`nvvm.mma_m16n8k16_f32_bf16`（bf16 输入，上轮 #321）的结构与 f16 版几乎完全一致，只是输入语义从 f16 换成 bf16。这种「改 dtype 即新增 op」的模式，正是 mma intrinsic 扩展的典型打法（详见 u6-l4 模板）。

最后，整族 op 由 wmma 模块的 `register` 统一注册：

[crates/dialect-nvvm/src/ops/wmma.rs:L486-L493](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L486-L493) —— 中文说明：`register(ctx)` 把 `MovmatrixTransB16Op`、5 个 mma op（bf16/f16/tf32/s8/f64）全部注册。本轮新增的 f16/tf32/s8 三条就是在这里被加入 `dialect-nvvm` 的 op 注册表。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「方言切换」发生在 mir-importer，而非 mir-lower。
2. **操作步骤**：
   - 打开 `crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs`，定位函数 `emit_mma_m16n8k32_s32_s8`。
   - 看它如何 `build` 出一个 `MmaM16N8K32S32S8Op` 并插入 IR——也就是说，mir-importer 直接产出了 `dialect-nvvm` op。
3. **需要观察的现象**：mma intrinsic 调用在翻译期就被「就地」写成 `nvvm.mma_*` op，跳过了 `dialect-mir` 这一层。
4. **预期结果**：你能画出 `mma_m16n8k32_s32_s8 调用 → mir-importer emit → nvvm.mma_m16n8k32_s32_s8 op → mir-lower → LLVM inline asm` 这条链路。
5. 本实践为源码阅读型，**待本地验证** `emit_mma_m16n8k32_s32_s8` 的具体行号（可用 `grep -n "emit_mma_m16n8k32_s32_s8" crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs` 定位）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `nvvm.mma_*` 的操作数用的是打包后的 `i32`（每个打包多个 f16/s8），而不是 `f16`/`i8` 类型？

**参考答案**：因为 PTX 的 `mma.sync` 指令本身就要求操作数按「每个 lane 持有的寄存器」组织，而一个 32 位寄存器里恰好打包了 2 个 f16 或 4 个 s8。`dialect-nvvm` op 忠实反映硬件寄存器布局，所以用 `i32` 表达「一个打包寄存器」。这是低层方言「贴近硬件」的体现。

**练习 2**：`nvvm.mma_m16n8k16_f32_f16` 与 `nvvm.mma_m16n8k16_f32_bf16` 的 op 定义几乎一样，为什么不合并成一个带 dtype 属性的 op？

**参考答案**：可以合并，但 cuda-oxide 选择「一个指令一个 op」，让 op 名字本身编码 dtype。好处是校验、lowering、调试时一眼能看出语义，且与 PTX 助记符一一对应；代价是 op 数量多（这正是 u6-l4「新增 intrinsic 模板」要解决的工程化问题）。

---

### 4.4 两层方言的职责分工与校验策略对比

#### 4.4.1 概念说明

两层方言不只是「op 名字前缀不同」，它们的**校验策略（verification strategy）也刻意不同**。这是 cuda-oxide 方言设计里最值得品味的工程取舍。

- `dialect-mir`：**类型一致性校验（type consistency verification）**，做全套类型检查。因为它直接承接 rustc MIR，是「类型安全」到「IR」的桥梁，校验可以发现 mir-importer 的翻译 bug。
- `dialect-nvvm`：**最小结构校验（minimal structural verification）**，默认只校验「操作数个数对不对、结果个数对不对」，**故意不做**类型检查。因为这些 op 是编译器机器生成的、LLVM 在下游会再做一次校验，重复校验收益不大却要写大量样板代码。

换句话说：**越靠近 Rust 的方言校验越严，越靠近硬件的方言校验越松、把校验责任下放给 LLVM**。

#### 4.4.2 核心流程

校验在流水线里发生在「IR 构造之后、lowering 之前」，由 Pliron 的 `Verify` trait 驱动：

```text
mir-importer 构造 op
   │
   ▼
dialect-mir op: Verify 做全套类型一致性检查
dialect-nvvm op: Verify 只做操作数/结果计数（少数 op 额外查类型）
   │
   ▼  校验通过才进入后段
mem2reg / unroll / lower …
```

这种分工可以用一个概率式直观理解。设「用户代码里的类型错误」为事件 \(E_u\)，「codegen 自己引入的参数个数错误」为事件 \(E_n\)。由于 rustc 已经拦截了 \(E_u\)，到 `dialect-nvvm` 时 \(P(E_u)\approx 0\)，唯一还值得查的是 \(P(E_n)\)，于是只校验操作数个数：

\[
\text{校验收益} \;\propto\; P(\text{该错误在本地会发生}) \times \text{拦截后的调试成本节省}
\]

对 `dialect-mir`，\(P(E_u)\) 因翻译 bug 而非 0，且类型错误若漏到后面极难定位，故收益高、值得全套校验；对 `dialect-nvvm`，类型几乎不可能错（机器生成），收益低，故只保留计数校验。

#### 4.4.3 源码精读

先看 `dialect-mir` 自己声明的校验策略——它有一整段「按类别给校验深度打分」的说明：

[crates/dialect-mir/src/ops/mod.rs:L33-L81](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-mir/src/ops/mod.rs#L33-L81) —— 中文说明：文档明确「MIR 方言使用类型一致性校验」，按类别列出每个子模块校验到什么程度（Function/Control Flow「Full」、Arithmetic/Comparison「Good」、Storage「Basic」），并解释「为什么校验：rustc 源码已类型安全、校验是为了抓住 importer 翻译错误、为 lowering 打基础」。

再看 `dialect-nvvm` 截然相反的策略声明：

[crates/dialect-nvvm/src/ops/mod.rs:L40-L89](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/mod.rs#L40-L89) —— 中文说明：文档明确「NVVM 方言故意只做最小结构校验（仅操作数/结果计数）」，列出四条理由（op 是机器生成的、LLVM 下游会校验、参数个数才是常见错误、避免 1500+ 行样板），并写明「什么不校验：操作数类型、指针地址空间、描述符类型」——这些全部下放给 LLVM lowering 时校验。

把两条放一起对比：

| 维度 | dialect-mir | dialect-nvvm |
| --- | --- | --- |
| 贴近 | Rust 语义 | PTX/NVVM 指令 |
| op 来源 | mir-importer 从 rustc MIR 翻译 | mir-importer 的 intrinsic 翻译器直接生成（mma 等） |
| 校验深度 | 全套类型一致性 | 最小结构（计数为主） |
| 类型系统 | 自带（指针/元组/枚举等） | 复用 Pliron 内置（i32/f32…） |
| 与硬件 | 无关 | 强相关（按 sm_xx 分级） |
| lowering 入口 | `mir-lower/convert/ops/` | `mir-lower/convert/intrinsics/` |

#### 4.4.4 代码实践

1. **实践目标**：用真实 `Verify` 实现印证「一严一松」。
2. **操作步骤**：
   - 打开 `crates/dialect-nvvm/src/ops/thread.rs`，看 `ReadPtxSregTidXOp` 的 `Verify`——它除了校验 0 操作数/1 结果，还额外查「结果是 `i32`」。这是文档里说的「thread indexing ops 是最简单最常用，所以值得多查一项」。
   - 对比 `crates/dialect-nvvm/src/ops/wmma.rs` 里 `MmaM16N8K16F32Bf16Op` 的 `Verify`——它查了每个操作数和结果的类型（f32/i32）。这其实是「最小校验」基线之上的**额外**类型检查，因为 mma 的寄存器布局太容易写错。
3. **需要观察的现象**：即便同为 `dialect-nvvm`，不同 op 的校验深度也不一样——简单/高频 op 与「布局复杂、易错」的 op 会多查一点，其余只查计数。
4. **预期结果**：能复述「最小校验是默认，按需加查」的设计原则。
5. 本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习**：假如某个 `dialect-nvvm` op 的 `Verify` 完全留空（什么都不查），会有什么后果？

**参考答案**：最坏情况是「op 被错误地构造（比如操作数个数错），但校验没拦住」，错误会一直漏到 LLVM lowering 阶段才暴露，错误信息会显得「来自 LLVM、不知所云」。这正是文档强调「操作数个数是最常见的 codegen bug，必须查」的原因——保留计数校验是为了把最容易犯的错尽早拦截。

---

### 4.5 方言切换时机：op 在哪里从 mir 变成 nvvm

#### 4.5.1 概念说明

「方言切换」这个词容易让人误以为是「整段 IR 从 `mir.*` 集体换成 `nvvm.*`」。实际上切换是**按 op 粒度、在两个时机**发生的：

1. **翻译期（mir-importer 内）**：遇到 GPU 专用 intrinsic（读线程号、原子、warp、mma 等），mir-importer 的 intrinsic 翻译器**直接生成 `dialect-nvvm` op**，根本不经过 `dialect-mir`。这是最主要的切换时机。
2. **lowering 期（mir-lower 内）**：剩下的 `dialect-mir` op（普通算术、内存、控制流）被翻译成 LLVM IR；同时已存在的 `dialect-nvvm` op 也被翻译成 LLVM IR（往往是对应的 LLVM intrinsic 或内联汇编）。严格说这一步不是「mir → nvvm」切换，而是「两个方言都 → LLVM IR」的**收敛**。

所以更准确的说法是：**`dialect-mir` 与 `dialect-nvvm` 在 IR 里长期共存，最后一起被 mir-lower 翻译掉**。所谓「方言切换」主要指 mir-importer 决定「这个语义要不要直接用 nvvm op 表达」。

#### 4.5.2 核心流程

把两类 op 的生命周期画在一起：

```text
                 ┌─ 普通 Rust 代码（a+b、数组访问、循环…）
                 │      └─► mir-importer ─► dialect-mir op（mir.add / mir.load / mir.cond_branch …）
rustc MIR ───────┤
                 │      ┌─►（直接）dialect-nvvm op
                 └─ GPU intrinsic 调用（thread::index / mma / atomic …）
                        └─► mir-importer 的 intrinsic 翻译器识别名字后 emit

   dialect-mir op  ─┐
                    ├─►  mir-lower（convert/ops + convert/intrinsics） ─► LLVM IR ─► PTX
   dialect-nvvm op ─┘
```

判断「某个语义走哪层」的经验法则：**如果这条指令在普通 CPU 程序里也有意义（加法、比较、函数调用），用 `dialect-mir`；如果它只在 GPU 上有意义（线程号、warp shuffle、张量核），用 `dialect-nvvm`**。

#### 4.5.3 源码精读

mir-lower 的 `convert` 目录就是「两条并行收敛路径」的物证——它把 ops/ 与 intrinsics/ 分开：

`crates/mir-lower/src/convert/ops/` 下有 `arithmetic.rs`、`control_flow.rs`、`memory.rs`、`aggregate.rs`、`cast.rs`、`call.rs`、`constants.rs`——与 `dialect-mir/ops/` 的子模块一一对应，专门 lowering 高层 op。

`crates/mir-lower/src/convert/intrinsics/` 下则有 `wmma.rs`、`atomic.rs`、`warp.rs`、`tma.rs`、`mbarrier.rs`、`cluster.rs`、`tcgen05.rs`、`cp_async.rs`……——与 `dialect-nvvm/ops/` 的子模块对应，专门 lowering 低层 op。

来看 mma op 的「切换证据 + 收敛证据」一对：

- **切换证据（mir-importer 直接生成 nvvm op）**：[crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs:L573-L597](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L573-L597) —— 中文说明：函数 `emit_mma_m16n8k32_s32_s8` 的文档说「Emit `mma_m16n8k32_s32_s8` as a register-producing dialect operation」，并检查「expects 3 arguments (acc, a, b)」。它在 mir-importer 里就被翻译成一个产出寄存器的 `dialect-nvvm` op，而不是 `dialect-mir` op。
- **收敛证据（mir-lower 把 nvvm op 翻成 PTX 内联汇编）**：[crates/mir-lower/src/convert/intrinsics/wmma.rs:L11-L59](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs#L11-L59) —— 中文说明：注释「Warp-level matrix intrinsic lowering (`movmatrix`, `mma.sync`)」，`convert_movmatrix_trans_b16` 把 `nvvm.movmatrix_trans_b16` 翻成内联汇编字符串 `"movmatrix.sync.aligned.m8n8.trans.b16 $0, $1;"`——这就是 nvvm op 落到 PTX 的最终一步。

#### 4.5.4 代码实践

1. **实践目标**：跟踪一条完整调用链，亲见「翻译期切换 + lowering 期收敛」。
2. **操作步骤**：
   - 在 `crates/dialect-nvvm/src/ops/wmma.rs` 找到 `MmaM16N8K16F32F16Op` 的定义（op 名 `nvvm.mma_m16n8k16_f32_f16`）。
   - 在 `crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs` 找 `emit_mma_m16n8k16_f32_f16`，确认它 `build` 出这个 op。
   - 在 `crates/mir-lower/src/convert/intrinsics/wmma.rs` 找对应的 convert 函数（把该 op 翻成 `mma.sync.aligned.m16n8k16...` 内联汇编）。
3. **需要观察的现象**：三个文件、三处代码，分别对应「op 定义」「翻译期生成」「lowering 期收敛」。
4. **预期结果**：能画出 `设备端 mma 调用 → mir-importer emit nvvm.mma_* → mir-lower convert → LLVM inline asm (PTX)` 的三段链路。
5. 本实践为源码阅读型，**待本地验证** 各函数精确行号（用 grep 定位）。

#### 4.5.5 小练习与答案

**练习 1**：一个普通的 `for` 循环（里面只有整数加法）会被翻译成哪些方言的 op？会发生「方言切换」到 nvvm 吗？

**参考答案**：不会切到 nvvm。循环变成 `dialect-mir` 的控制流 op（`mir.cond_branch`/`mir.goto`）+ 算术 op（`mir.add`）+ 内存 op（`mir.load`/`mir.store`）。这些全是 Rust 语义级的，全程待在 `dialect-mir`，最后由 mir-lower 的 `convert/ops/` 直接 lower 成 LLVM IR。只有当循环体里调用了 GPU 专用 intrinsic（如读线程号）时，那一个 op 才会是 `dialect-nvvm`。

**练习 2**：如果新增一个「只在 GPU 上才有意义」的 op（比如读某个性能计数器），应该放进哪个方言？为什么？

**参考答案**：放进 `dialect-nvvm`。因为它对应一条 PTX 指令、只在 GPU 有意义，属于「GPU 词典」；同时它的 `Verify` 只需做最小结构校验，类型校验下放给 LLVM，与该方言的策略一致。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「op 归类与链路追踪」小任务。

**任务**：从 `dialect-mir/ops/` 与 `dialect-nvvm/ops/` 各挑 3 个 op，完成下表，并解释「为什么 cuda-oxide 要分两层方言」。

| op 名 | 所属方言 | 对应的 Rust 语义 or PTX 指令 | 何时进入 IR | lowering 入口 |
| --- | --- | --- | --- | --- |
| `mir.add` | dialect-mir | Rust 整数/浮点加法 | mir-importer 翻译 MIR | `convert/ops/arithmetic.rs` |
| `mir.extract_field` | dialect-mir | 结构体/元组字段访问 | mir-importer 翻译 MIR | `convert/ops/aggregate.rs` |
| `mir.cond_branch` | dialect-mir | MIR `Switch`/`Goto` 终止符 | mir-importer 翻译 MIR | `convert/ops/control_flow.rs` |
| `nvvm.read_ptx_sreg_tid_x` | dialect-nvvm | PTX `%tid.x` | mir-importer intrinsic 翻译 | `convert/intrinsics/` |
| `nvvm.atomic_rmw` | dialect-nvvm | PTX `atom.*` / LLVM `atomicrmw` | mir-importer intrinsic 翻译 | `convert/intrinsics/atomic.rs` |
| `nvvm.mma_m16n8k8_f32_tf32` | dialect-nvvm | `mma.sync.aligned.m16n8k8...` | mir-importer `emit_mma_*` | `convert/intrinsics/wmma.rs` |

**进阶**：再用 `git log --oneline -- crates/dialect-nvvm/src/ops/wmma.rs` 列出本轮对该文件的修改，指出 #327/#328/#329 分别新增了哪个 op（f16 m16n8k16、tf32 m16n8k8、s8 m16n8k32），并对照 4.3.3 的源码链接确认它们都已注册到 `dialect-nvvm`。

**预期产出**：一段话总结「高层 op 承载 Rust 语义并做全套类型校验、低层 op 承载 PTX 语义只做最小校验，两者在 IR 中共存、最后由 mir-lower 一起收敛到 LLVM IR」——这就是分两层方言的全部理由。

> 本综合实践为源码阅读型，表格已给出参考答案；若要在本机验证 git 提交，运行 `git log --oneline -- crates/dialect-nvvm/src/ops/wmma.rs` 即可看到 #327/#328/#329 的提交。

---

## 6. 本讲小结

- cuda-oxide 在 Pliron IR 里维护**两层方言**：`dialect-mir`（高层、Rust 语义）与 `dialect-nvvm`（低层、PTX/NVVM 指令），用名字前缀 `mir.` / `nvvm.` 隔离命名空间。
- `dialect-mir` 由 mir-importer 从 rustc MIR 一对一翻译而来，覆盖函数/控制流/内存/算术/比较/聚合/枚举/转换/调用，做**全套类型一致性校验**以抓住翻译 bug。
- `dialect-nvvm` 贴近硬件，按 GPU 架构分级（所有 GPU / Hopper+ / Blackwell+），op 几乎一一对应 PTX 指令或 NVVM intrinsic，做**最小结构校验**（计数为主），类型校验下放给 LLVM。
- 「方言切换」是按 op 粒度发生的：GPU 专用 intrinsic 在 mir-importer 翻译期就被**直接写成 `dialect-nvvm` op**（如 mma），普通 Rust 代码则全程待在 `dialect-mir`；最后两者由 mir-lower 的 `convert/ops/` 与 `convert/intrinsics/` 分别收敛到 LLVM IR。
- 本轮 #327/#328/#329 在 `dialect-nvvm/ops/wmma.rs` 新增了三条 mma.sync op：`nvvm.mma_m16n8k16_f32_f16`（f16）、`nvvm.mma_m16n8k8_f32_tf32`（tf32）、`nvvm.mma_m16n8k32_s32_s8`（s8），与既有的 bf16/f64 版本同属一族。
- 分层的本质收益是「复杂度切开」：每一层只面对一个世界的概念，校验深度按语义重要性精准调节。

---

## 7. 下一步学习建议

- **u4-l4（MIR Lowering 鸟瞰）**：本讲到「mir-lower 把两个方言都收敛到 LLVM IR」就止步了。下一讲会进入 mir-lower 内部，看 `convert/ops/` 与 `convert/intrinsics/` 具体怎么把 op 翻成 LLVM 指令，以及 `.ptx` 与 NVVM `.ll` 两种产物的分叉条件。
- **u6-l2（mir-importer 深潜）**：想彻底搞懂「翻译期方言切换」的读者，可以深潜 mir-importer 的 terminator/intrinsics 分派机制，看一个 intrinsic 调用如何按类别（atomic/wmma/warp/tma/…）进到对应翻译模块。
- **u6-l3（mir-lower 深潜）**：聚焦 `convert/ops/arithmetic.rs` 等，看 FMA 收缩策略如何在本层落地。
- **u6-l4（新增 intrinsic 模板）**：如果看完 4.3 你已经想自己加一条 mma op，这一讲给出「设备 API → dialect-nvvm op → mir-importer 翻译 → mir-lower lowering」的全栈改动清单。
- **延伸阅读**：直接对照 `crates/dialect-mir/src/ops/mod.rs` 与 `crates/dialect-nvvm/src/ops/mod.rs` 顶部的官方对照表，是巩固本讲最快的方式。
