# MIR Lowering 鸟瞰：dialect → LLVM IR → PTX/NVVM

## 1. 本讲目标

在前一讲（u4-l3）里，我们已经看清 cuda-oxide 在 Pliron IR 里维护的两层方言：高层的 `dialect-mir`（贴近 Rust 语义）和低层的 `dialect-nvvm`（贴近 PTX/NVVM 指令）。本讲要回答的是：**这两层方言的 op，最终是怎么变成能在 GPU 上跑的 LLVM IR、乃至 PTX 或 NVVM IR 的？**

学完本讲，你应当能够：

- 画出 `mir-lower` 这个 crate 的整体 lowering 流程，并能说出它依赖的 `DialectConversion` 框架替我们做了哪些事。
- 区分两类转换入口：`convert::ops::*`（普通语义 op）与 `convert::intrinsics::*`（GPU intrinsic），并理解它们各自如何挂在同一个 `MirToLlvmConversion` op 接口上。
- 解释最终产物为什么会分叉成 `.ptx` 与 NVVM `.ll` 两种，触发条件是什么。
- 说出本轮（#314）后，lowering 不再由 `mir-importer` 自己执行，而是被独立后端 `cuda-oxide-codegen` 统一编排，以及新增的 `LoweredVerification` 阶段起什么作用。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个关键概念。

**Lowering（降级）是什么？** 编译器里，IR（中间表示）通常分层。越高层越贴近源语言语义、越方便人读；越低层越贴近机器指令、越方便后端生成代码。「降级」就是把一个高层的 op 改写成一组低层 op 的过程，理想情况下语义不变，只是换了一种表达粒度。本讲里，高层是 `dialect-mir`/`dialect-nvvm`，低层是 LLVM dialect（再往下导出成 LLVM IR 文本）。

**什么是 DialectConversion 框架？** Pliron（cuda-oxide 用的类 MLIR IR 框架）提供了一个通用的「方言转换」骨架。你只要告诉它「哪些 op 可转换」「类型怎么转换」「每个 op 怎么改写（rewrite）」，它就会自动遍历 IR、维护 def-before-use 顺序、处理块参数（block argument）的打补丁，避免你手写一堆容易出错的遍历循环。`mir-lower` 的核心就是把这套骨架实例化。

**op 接口（op interface）是什么？** 在 MLIR/Pliron 里，一个「接口」是一组可以挂到任意 op 上的方法契约。`mir-lower` 定义了一个本地接口 `MirToLlvmConversion`，每个 `dialect-mir`/`dialect-nvvm` 的 op 都实现它，声明「我该怎么降级成 LLVM dialect」。这样调度时不需要写一长串 `if op == MirAddOp ... else if op == MirSubOp ...`，而是用一次 `op_cast` 虚表查找（O(1)）直接拿到对应的转换器。

**LLVM dialect vs LLVM IR 文本？** LLVM dialect 是 Pliron 里对 LLVM 指令的建模（比如 `llvm.add`）；LLVM IR 文本（`.ll` 文件）是 LLVM 自己的文本格式（比如 `%r = add i32 %a, %b`）。`mir-lower` 只负责把 `mir.*`/`nvvm.*` op 降级到 `llvm.*` op；把 `llvm.*` op 打印成 `.ll` 文本是下游 `llvm-export` 的事。

**PTX 与 NVVM IR 的差别？** PTX 是 NVIDIA 自己的并行线程汇编指令集，可以直接被 CUDA 驱动加载运行。NVVM IR 是一种 LLVM IR 子集格式，专门给 NVIDIA 的 libNVVM/nvJitLink 编译器吃。当一个内核用到了浮点数学库函数（sin/cos/exp 等，会 lowering 成 `__nv_*` libdevice 调用），cuda-oxide 就不直接出 PTX，而是出 NVVM IR `.ll`，交给宿主侧的 libNVVM/nvJitLink 去做最后的链接与编译。这部分细节留到 u4-l5 展开，本讲只关注「在哪里分叉」。

**本轮 #314 带来的变化：** 在 #314 之前，lowering 流水线（mem2reg、循环展开、lowering、导出、llc 出 PTX）散落在 `mir-importer` 里。#314 把这一整段「翻译之后」的后段流水线抽出来，迁入一个全新的、不依赖 rustc 的 crate `cuda-oxide-codegen`。现在 `mir-importer` 只负责「Stable MIR → dialect-mir」的翻译，随后调用 `cuda-oxide-codegen` 的单一编排函数完成后段。这意味着 lowering 这一段在全工具链里只有一份实现，rustc 前端和实验性前端都复用它。

## 3. 本讲源码地图

本讲主要围绕 `mir-lower` crate，并向上承接它的编排者 `cuda-oxide-codegen`。涉及的文件与作用：

| 文件 | 作用 |
|------|------|
| [crates/mir-lower/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs) | lowering crate 的入口与文档。定义 `LoweringOptions`、`MirToLlvmConversionDriver`（实现 `DialectConversion`），以及对外入口 `lower_mir_to_llvm` / `lower_mir_to_llvm_with_options`。 |
| [crates/mir-lower/src/context.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs) | 转换期间需要的 CUDA 专用状态类型：共享内存全局去重表、设备全局表、动态共享内存对齐表，以及把 `LoweringOptions` 存进 pliron context 的工具。 |
| [crates/mir-lower/src/lowering.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs) | 函数级 lowering 的核心：`convert_func` 把 `MirFuncOp` 降级成 `llvm.func`，并在 lowering 前做动态共享内存对齐传播、ABI 对齐盖章、入口块聚合参数重建。 |
| [crates/mir-lower/src/convert/mod.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/mod.rs) | 转换子模块的索引：`ops`（按语义类别）与 `intrinsics`（GPU intrinsic）两条路径。 |
| [crates/mir-lower/src/conversion_interface.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/conversion_interface.rs) | 定义 op 接口 `MirToLlvmConversion`，所有可降级的 op 都实现它。 |
| [crates/mir-lower/src/convert/interface_impls.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs) | 把每个具体 op（如 `MirAddOp`）的接口实现挂到对应的 `convert::*` 函数上。 |
| [crates/mir-lower/src/convert/ops/arithmetic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs) | 算术 op 的转换器，例如 `convert_add`。也包含 FMA 收缩（fast-math `contract` 标志）的实现。 |
| [crates/cuda-oxide-codegen/src/pipeline.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs) | #314 后的「后段编排器」`compile_translated_module`：按顺序调 prepare → externs → lower → legalize → verify → export → llc，并决定 PTX/NVVM IR 分叉。 |
| [crates/cuda-oxide-codegen/src/lower.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lower.rs) | 编排器对 `mir-lower` 的薄封装 `lower_to_llvm`，把错误格式化成 `PipelineError::Lowering`。 |
| [crates/cuda-oxide-codegen/src/verify.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/verify.rs) | 通用 `verify_operation`：递归校验一个 op 及其嵌套 op，定位最内层失败点。 |
| [crates/cuda-oxide-codegen/src/error.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/error.rs) | `PipelineError` 枚举，含本轮新增的 `LoweredVerification` 变体。 |

## 4. 核心概念与源码讲解

### 4.1 mir-lower 流程鸟瞰

#### 4.1.1 概念说明

`mir-lower` 这个 crate 的职责非常单一：**把已经翻译好的 `dialect-mir`（以及混在其中的 `dialect-nvvm`）op，批量改写成 LLVM dialect op。** 它不做优化、不做循环展开、不导出文件——这些都是流水线里其他工位的事。

它放在整条流水线的位置，crate 顶部文档画得很清楚：

```text
Rust Source Code
       │
       ▼
┌──────────────┐
│   rustc      │  (extracts Stable MIR)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ mir-importer │  (Stable MIR → dialect-mir, mem2reg, annotated unroll)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  mir-lower   │  ◄── THIS CRATE (dialect-mir → LLVM dialect)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ llvm-export  │  (exports to LLVM IR)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│     llc      │  (LLVM IR → PTX)
└──────────────┘
```

> 见 [crates/mir-lower/src/lib.rs:26-53](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L26-L53) —— 注意这张图里 mir-importer 旁边写着的 mem2reg/annotated unroll，在 #314 之后其实也搬进了 `cuda-oxide-codegen` 的 `prepare_mir_module`，但相对位置（在 mir-lower 之前）不变。

这里要建立一个关键直觉：**mir-lower 的工作单元是「op」，不是「整个模块」。** 它把每个 `mir.add`、`mir.load`、`nvvm.shfl...` 这样的 op，逐个换成对应的 `llvm.add`、`llvm.load`、`llvm.inlineasm`/intrinsic 调用。框架替你做遍历，你只管写「这一个 op 怎么换」。

#### 4.1.2 核心流程

整个 lowering 的执行流程可以概括为：

1. **入口**：调用方（#314 后是 `cuda-oxide-codegen`）拿到一个装满 `dialect-mir` op 的模块，调用 `lower_mir_to_llvm_with_options(ctx, module, options)`。
2. **存选项**：把 `LoweringOptions`（目前唯一字段是 `allow_fma_contraction`）写进 pliron context，这样每个 op 转换器都能读到，不必去读进程级环境变量。
3. **动态共享内存对齐传播**：在真正转换前，先在整个 MIR 调用图上传播 `dynamic_shared_alignment` 标记，让被多个 kernel 共享的 helper 拿到最强对齐要求。
4. **构造驱动器**：新建 `MirToLlvmConversionDriver`，它持有几张 CUDA 专用状态表（共享内存全局去重、设备全局、动态共享内存对齐）。
5. **`apply_dialect_conversion`**：pliron 框架拿着这个驱动器遍历模块里每一个 op，对每个「可转换」的 op 调驱动器的 `rewrite` 方法。
6. **rewrite 派发**：`rewrite` 先用 OpId 特判几个需要 pass 级状态的 op（函数、共享/全局分配），其余走 `op_cast` 虚表派发到具体 op 的 `convert`。
7. **逐 op 替换**：每个 `convert_*` 用 `rewriter` 创建新的 LLVM dialect op，再用 `replace_operation` 把旧 op 的所有使用点改指向新 op。

#### 4.1.3 源码精读

对外入口与编排逻辑在 [crates/mir-lower/src/lib.rs:330-349](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L330-L349)：

```rust
pub fn lower_mir_to_llvm_with_options(
    ctx: &mut Context,
    module_op: Ptr<Operation>,
    options: LoweringOptions,
) -> Result<()> {
    context::set_lowering_options(ctx, options);
    // 动态共享内存操作可能位于设备 helper 里。趁完整的 MIR 调用图还在，
    // 算出每个 kernel→helper 的对齐需求；函数转换会逐个删掉调用图。
    lowering::propagate_kernel_dynamic_shared_alignments(ctx, module_op);
    let mut conversion = MirToLlvmConversionDriver {
        shared_globals: FxHashMap::default(),
        device_globals: FxHashMap::default(),
        dynamic_smem_alignments: FxHashMap::default(),
    };
    apply_dialect_conversion(ctx, &mut conversion, module_op)?;
    Ok(())
}
```

注意三件事：第一，`LoweringOptions` 通过 `set_lowering_options` 存进 context（见 [crates/mir-lower/src/context.rs:25-37](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs#L25-L37)），转换器只拿到 context，因此这是一种把策略显式传下去、又不污染每个 converter 签名的设计。第二，对齐传播（见 4.2）必须在 `apply_dialect_conversion` 之前跑，因为函数转换会一边进行一边把 MIR 调用图拆掉。第三，`apply_dialect_conversion` 现在返回一个 `IRStatus`（Changed/Unchanged），lowering 只关心成败，故丢弃它（注释见 [lib.rs:345-347](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L345-L347)）。

`MirToLlvmConversionDriver` 实现的 `DialectConversion` 三件套，决定了框架「会不会转换、怎么转」：

- `can_convert_op`（[lib.rs:219-228](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L219-L228)）：凡方言名是 `mir` 或 `nvvm` 的 op 都要转换；此外有一种特殊情形——`sccp` 常量折叠可能产出带符号/无符号整数类型的 `builtin.constant`，必须把它归一化成 LLVM 约定的 signless 整数，否则模块里会出现「signless op 被有符号常量喂入」的类型不匹配。判定函数 `is_signed_builtin_constant` 见 [lib.rs:207-216](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L207-L216)。
- `can_convert_type` / `convert_type`（[lib.rs:230-243](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L230-L243)）：处理类型转换，核心是把有符号/无符号整数归一为 signless。
- `rewrite`（[lib.rs:245-305](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L245-L305)）：真正「这一个 op 怎么改写」的派发逻辑，详见 4.3。

#### 4.1.4 代码实践

**实践目标**：用最低成本，看到「pliron 框架替我们遍历、逐 op 改写」这件事真的在发生。

**操作步骤**：

1. 打开 [crates/mir-lower/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs)，定位到 `lower_mir_to_llvm_with_options`。
2. 注意它把几乎所有脏活都交给了 `apply_dialect_conversion`——`mir-lower` 自己**没有**任何 `for op in module.op_iter()` 这样的显式遍历。这就是框架的价值。
3. 在 [crates/mir-lower/src/lib.rs:192-196](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L192-L196) 看 `is_mir_or_nvvm_op`，确认「可转换」的判定就是看方言名是不是 `mir` 或 `nvvm`。

**需要观察的现象**：`mir-lower` crate 里搜不到「主动遍历所有 op 并 dispatch」的代码；遍历发生在 pliron 的 `apply_dialect_conversion` 内部，对每个 op 调用我们提供的 `can_convert_op` 与 `rewrite`。

**预期结果**：你会理解——`mir-lower` 写的是「每个 op 怎么换」的说明书，而「谁来读这本说明书、按什么顺序读」是 pliron 框架的职责。这种分工是后面 4.3 里 op 接口派发能成立的前提。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lower_mir_to_llvm_with_options` 要把 `LoweringOptions` 存进 context，而不是作为参数一路传递到每个 `convert_*`？

> **参考答案**：因为每个 op 的 `convert` 方法签名由 pliron 的 op 接口固定（见 4.3 的 `MirToLlvmConversion::convert`），它只接收 `ctx`、`rewriter`、`operands_info`，没有「额外选项」的位置。把策略存进 context，是「在受约束的签名里把策略显式传下去」的唯一干净办法，也避免了 converter 去读进程级环境变量（那样会让单元测试无法控制行为）。

**练习 2**：`apply_dialect_conversion` 返回的 `IRStatus` 为什么被 lowering 丢弃？

> **参考答案**：`IRStatus` 只表示「这次转换有没有改 IR」（Changed/Unchanged），lowering 不需要区分——只要没出错就当成功。它对那些需要「如果没改就跳过后续 pass」的 pass 管理器有意义，但 `mir-lower` 是一次性整模块转换，只关心 `Result<()>`。

---

### 4.2 context 与 lowering：状态、选项与函数级转换

#### 4.2.1 概念说明

`DialectConversion` 框架虽然管遍历，但有几个 op 的转换需要「跨 op、跨函数」的全局状态，单靠 `rewrite` 里的局部信息不够。`mir-lower` 把这些 CUDA 专用状态集中放在 `MirToLlvmConversionDriver` 里，并在 [crates/mir-lower/src/context.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs) 定义了它们的类型：

- **`SharedGlobalsMap`**：共享内存（address space 3）分配键 → LLVM 全局符号名。多个 op 引用同一块共享内存时，要保证它们指向同一个全局。
- **`DeviceGlobalsMap`**：普通设备 `static`/`static mut`（address space 1）键 → LLVM 全局符号名。
- **`DynamicSmemAlignmentMap`**：函数名 → `(符号名, 最大对齐)`。记录每个拥有动态共享内存访问的函数，以及它最终要的对齐。

这一节还要讲函数级 lowering 的「门面」`convert_func`：它是整个 crate 里最复杂的转换器，因为函数降级不只是「换指令」，还要重建入口块、传播 kernel 属性、合并动态共享内存对齐。

#### 4.2.2 核心流程

`convert_func` 在框架遇到 `MirFuncOp` 时被 `rewrite` 特判调用（见 [lib.rs:256-265](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L256-L265)）。它做的事按顺序是：

1. **跨边界枚举校验**：kernel 参数是 host 写、device 读的「跨 ABI 数据」，若参数里含 niche 编码枚举（如 `Option<&T>`，host 用 null 标记 None、不存 tag，而 device 会加显式 tag），两边会读出不同字节——直接在这里报错拒绝。
2. **类型签名转换**：把 MIR 函数类型转成 LLVM 函数类型（参数会被展平，slice 变成 `(ptr, len)`）。
3. **传播 kernel 属性**：把 `gpu_kernel`、cluster 维度、`maxntid`/`minctasm`（即 `#[launch_bounds]`）、`alwaysinline` 等属性从 MIR func 搬到 LLVM func。
4. **预扫动态共享内存对齐**：在 `inline_region` 把块搬走之前，遍历所有 MIR 块，算出 `MirExternSharedOp` 的最大对齐，与启动契约标记取最大值。
5. **ABI 对齐盖章**：在 MIR 类型还在时，把 `repr(align(N))` 等真实对齐盖到 load/store/alloca/ref 上（LLVM struct 类型不携带 over-alignment 信息）。
6. **入口块 prologue**：在 LLVM 入口块里用 `insertvalue` 把展平的参数重新拼回 slice/struct，再 `br` 跳到原 MIR 入口块。
7. **`inline_region`**：把整个 MIR region 搬进 LLVM 函数。
8. **替换**：用 `replace_operation` 把旧 `MirFuncOp` 换成新 `llvm.func`。

入口块 prologue 的形态，源码注释里画了示意：

```text
LLVM entry block (flattened args: ptr, len, field0, field1, ...):
  %undef_slice = llvm.mlir.undef : {ptr, i64}
  %with_ptr    = llvm.insertvalue %ptr into %undef_slice[0]
  %slice       = llvm.insertvalue %len into %with_ptr[1]
  llvm.br ^mir_entry(%slice, %field0, %field1, ...)
```

> 见 [crates/mir-lower/src/lowering.rs:22-28](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L22-L28)。其意义是：LLVM 调用约定把 slice 展平成两个独立参数，但 MIR 入口块期望收到一个完整的 slice 值，所以要在入口处先「拼回来」。

#### 4.2.3 源码精读

驱动器持有的三张状态表定义在 [crates/mir-lower/src/context.rs:51-72](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs#L51-L72)：

```rust
pub type SharedGlobalsMap = FxHashMap<String, pliron::identifier::Identifier>;
pub type DeviceGlobalsMap = FxHashMap<String, pliron::identifier::Identifier>;
/// 记录每个被降级函数的动态共享内存对齐：函数名 → (符号名, 最大对齐)。
pub type DynamicSmemAlignmentMap = FxHashMap<String, (pliron::identifier::Identifier, u64)>;
```

`DynamicSmemAlignmentMap` 上的注释点出一个关键设计：每个拥有动态共享内存访问的函数都会拿到一个符号；转换前，pass 会把函数体请求的对齐与「每个能到达它的启动契约标记」合并，确保被多个 kernel 共享的 helper 用上最强要求。

合并发生在 lowering 之前的对齐传播 pass——[crates/mir-lower/src/lowering.rs:74-112](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L74-L112)：

```rust
pub(crate) fn propagate_kernel_dynamic_shared_alignments(ctx, module_op) {
    // 1. 收集模块里所有 MirFuncOp
    // 2. 构建它们的调用图
    // 3. 找出所有带 dynamic_shared_alignment 标记的函数作为传播根
    // 4. 沿调用图把每个根的对齐向下传，取最大值
}
```

它带有一个单元测试，明确锁定了「传递性、环安全、共享 helper 取最大值」三条契约——见 [lowering.rs:820-852](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L820-L852)：例如 `kernel_256` 要求 256，`shared` 这个被多个 kernel 调用的 helper 最终拿到 256；而从未被任何带标记函数到达的 `unreached_helper` 不出现在结果里。

`convert_func` 主体在 [crates/mir-lower/src/lowering.rs:208-331](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L208-L331)，其中在 `inline_region` 之前先算对齐（[lowering.rs:283-305](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L283-L305)）：

```rust
let body_max_align = compute_max_dynamic_smem_alignment(ctx, &mir_blocks);
let contract_min_align = dynamic_shared_alignment_attr(ctx, op);
let max_align = match (body_max_align, contract_min_align) {
    (Some(body), Some(contract)) => Some(body.max(contract)),
    (body, contract) => body.or(contract),
};
```

这就是 u2-l3 讲过的「启动契约动态共享内存对齐合并」在 lowering 层的落点：函数体里 `DynamicSharedArray<T,ALIGN>` 的 `ALIGN`（body_max_align）与 `#[launch_contract(dynamic_shared_alignment=N)]` 注入的标记（contract_min_align）取最大值。

#### 4.2.4 代码实践

**实践目标**：理解「为什么对齐传播必须在 lowering 之前、在调用图还在的时候跑」。

**操作步骤**：

1. 读 [crates/mir-lower/src/lowering.rs:74-112](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L74-L112) 的 `propagate_kernel_dynamic_shared_alignments`，注意它需要遍历调用图。
2. 读 [lowering.rs:317](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L317) 的 `rewriter.inline_region(...)`——这一步会把 MIR 函数体搬进 LLVM 函数，搬完之后 MIR 调用图就被破坏了。
3. 回到 [lib.rs:339](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L339) 确认：对齐传播调用排在 `apply_dialect_conversion` **之前**。

**需要观察的现象**：对齐传播是「模块级、一次性」的前置 pass；而 `convert_func` 是「逐函数、被框架驱动」的转换。前者依赖完整调用图，后者会逐个拆掉调用图，所以两者不能颠倒顺序。

**预期结果**（待本地验证）：若你尝试把 `propagate_kernel_dynamic_shared_alignments` 挪到 `apply_dialect_conversion` 之后调用，会看到它读不到完整调用图（很多 `MirCallOp` 已被 lowering 替换掉），传播结果会缺失 helper 节点。

#### 4.2.5 小练习与答案

**练习 1**：`convert_func` 为什么要拒绝 kernel 参数里的 niche 编码枚举（如 `Option<&T>`）？

> **参考答案**：因为 kernel 参数是 host 写、device 读的跨边界数据，两边对同一组字节的解读必须一致。niche 枚举在 host 端用「不可能的 payload 值」（如 null 表示 `None`）来编码 variant，根本不存 tag；而 cuda-oxide 的设备端建模会给枚举加显式 tag。两边读到的字节会不同，内核会读出错值，所以必须在边界拒绝。给枚举加 `#[repr(u32)]` 这类显式判别式即可绕过。

**练习 2**：`stamp_memory_op_alignment` 为什么必须在 `inline_region` 之前、且在「MIR 类型还在」时跑？

> **参考答案**：因为 `repr(align(N))` 这类真实 ABI 对齐信息记录在 `MirStructType`/`MirEnumType`/`MirUnionType` 上，一旦类型转换把 MIR 类型换成 LLVM 类型，LLVM struct 不携带 over-alignment，信息就丢了。所以要在搬运与类型替换之前，把对齐「盖章」到 load/store/alloca/ref op 上（见 [lowering.rs:752-795](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lowering.rs#L752-L795)）。

---

### 4.3 ops 与 intrinsics：两类转换入口，同一个接口

#### 4.3.1 概念说明

`mir-lower` 要降级的 op 来自两个方言、却走同一个派发机制。这正是 u4-l3 讲过的「两个方言在 IR 中长期共存，最后由 mir-lower 收敛到 LLVM IR」的落地。

转换器代码按两条路径组织（见 [crates/mir-lower/src/convert/mod.rs:6-33](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/mod.rs#L6-L33)）：

- **`convert::ops::*`**：按语义类别分的「普通」转换器——`arithmetic`、`memory`、`control_flow`、`constants`、`cast`、`aggregate`、`call`。这些对应 `dialect-mir` 里贴近 Rust 语义的 op，通常一对一映射到 LLVM dialect（如 `mir.add` → `llvm.add`）。
- **`convert::intrinsics::*`**：GPU intrinsic 转换器——`basic`（线程/块索引、屏障）、`warp`、`mbarrier`、`tma`、`wgmma`、`tcgen05`、`wmma`、`atomic`、`cp_async`、`cluster` 等。这些对应 `dialect-nvvm` 的 op，降级策略有两种：调 LLVM/NVVM intrinsic，或内联 PTX 汇编（`llvm.inlineasm`）。

这两条路径靠的不是两套派发，而是**同一个 op 接口** `MirToLlvmConversion`。每个 op（无论 `mir.*` 还是 `nvvm.*`）都实现它，`#[op_interface_impl]` 宏把实现注册到该 op 的虚表上。驱动器在 `rewrite` 里用 `op_cast::<dyn MirToLlvmConversion>` 一次虚表查找就能拿到正确的 converter，无需 if-else 链。

#### 4.3.2 核心流程

以「一行 Rust 加法 `a + b`（f32）」为例，跟踪它的 lowering 路径：

1. mir-importer 把 `a + b` 翻译成一个 `mir.add` op（`MirAddOp`），两个操作数是 f32。
2. 框架遍历到这个 op，`can_convert_op` 看到方言名是 `mir`，返回 true。
3. `rewrite` 走通用派发：`op_cast::<dyn MirToLlvmConversion>` 命中 `MirAddOp` 的接口实现。
4. 该实现（[interface_impls.rs:95-105](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs#L95-L105)）调用 `convert::ops::arithmetic::convert_add`。
5. `convert_add` 看到操作数是浮点，创建 `llvm.fadd`，给它挂上 fast-math `contract` 标志（FMA 收缩许可），再用 `replace_operation` 把 `mir.add` 替换掉。

GPU intrinsic 路径结构相同，只是 converter 落在 `convert::intrinsics::*` 下，产出 LLVM intrinsic 调用或 `llvm.inlineasm`。lib.rs 顶部文档把这两种策略写得很明白（[lib.rs:112-122](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L112-L122)）：有直接 NVVM intrinsic 等价物的用 intrinsic 调用（如线程 ID 用 `llvm_nvvm_read_ptx_sreg_tid_x`）；复杂或需要更精细控制的用内联 PTX（如 tcgen05、wgmma），并带 `convergent` 属性表达 warp 同步语义。

#### 4.3.3 源码精读

op 接口定义在 [crates/mir-lower/src/conversion_interface.rs:32-49](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/conversion_interface.rs#L32-L49)：

```rust
#[op_interface]
pub trait MirToLlvmConversion {
    /// 把这个 op 降级到 LLVM dialect。
    fn convert(
        &self,
        ctx: &mut Context,
        rewriter: &mut DialectConversionRewriter,
        operands_info: &OperandsInfo,
    ) -> Result<()>;

    /// 校验钩子（空实现——底层 op 自身的 verifier 已足够）。
    fn verify(_op: &dyn Op, _ctx: &Context) -> Result<()>
    where Self: Sized { Ok(()) }
}
```

注意它特意定义在 `mir-lower` 而非 `dialect-mir`：因为 `#[op_interface_impl]` 块要为外部类型（`MirAddOp` 等属于 `dialect-mir` crate）实现这个 trait，必须把 trait 放在本地 crate 才不违反 Rust 的孤儿规则（注释见 [conversion_interface.rs:6-12](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/conversion_interface.rs#L6-L12)）。

`MirAddOp` 的实现短小直接——[interface_impls.rs:95-105](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs#L95-L105)：

```rust
#[op_interface_impl]
impl MirToLlvmConversion for MirAddOp {
    fn convert(&self, ctx, rewriter, operands_info) -> Result<()> {
        super::ops::arithmetic::convert_add(ctx, rewriter, self.get_operation(), operands_info)
    }
}
```

实际工作在 `convert_add`——[crates/mir-lower/src/convert/ops/arithmetic.rs:145-165](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L145-L165)：

```rust
pub(crate) fn convert_add(ctx, rewriter, op, _operands_info) -> Result<()> {
    let (lhs, rhs) = get_binary_operands(op, ctx)?;
    let llvm_op = if is_float_type(ctx, lhs) {
        let fadd = llvm::FAddOp::new(ctx, lhs, rhs);
        add_fastmath_flags(ctx, fadd.get_operation());   // 挂 contract 标志
        fadd.get_operation()
    } else {
        let flags = IntegerOverflowFlagsAttr::default();
        llvm::AddOp::new_with_overflow_flag(ctx, lhs, rhs, flags).get_operation()
    };
    rewriter.insert_operation(ctx, llvm_op);
    rewriter.replace_operation(ctx, op, llvm_op);
    Ok(())
}
```

这里有个贯穿全工具链的浮点语义关键点：`add_fastmath_flags` 只挂 `contract` 一个 fast-math 标志（见 [arithmetic.rs:126-135](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L126-L135)），它对应 `allow_fma_contraction`：

```rust
fn add_fastmath_flags(ctx, op) {
    let flags = if crate::context::lowering_options(ctx).allow_fma_contraction {
        FastmathFlags::CONTRACT
    } else {
        FastmathFlags::empty()
    };
    ...
}
```

注释解释了为什么只挂 `contract`：它允许 NVPTX 后端把「一个 fmul 喂给 fadd/fsub」融合成单条 `fma.rn.f32`，匹配 nvcc 默认的 `--fmad=true`；而绝不挂 reassoc/nnan/ninf/nsz/arcp，这样结果在「收缩/不收缩」两种参考下都可逐位比较。这条契约由单元测试锁定——[arithmetic.rs:893-951](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L893-L951)：开启 FMA 时 fmul/fadd/fsub 各自带 `CONTRACT`；关闭时三者都是空标志。

最后看驱动器的通用派发（[lib.rs:295-305](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L295-L305)）：在特判完需要 pass 级状态的几个 op 后，剩下全部走虚表派发，若某个 op 没实现接口就报「Unsupported MIR/NVVM op for lowering」。

#### 4.3.4 代码实践

**实践目标**：把「一行 `a + b`（f32）从 dialect op 到 LLVM IR 指令」的 convert 调用路径完整写出来。

**操作步骤**：

1. 从 [interface_impls.rs:95-105](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs#L95-L105) 出发：`MirAddOp::convert` → `convert::ops::arithmetic::convert_add`。
2. 在 [arithmetic.rs:145-165](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L145-L165) 跟进：f32 走 `llvm::FAddOp::new` 分支，再 `add_fastmath_flags` 挂 `CONTRACT`。
3. 在 [arithmetic.rs:126-135](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L126-L135) 跟进：读 `lowering_options(ctx).allow_fma_contraction`，决定挂不挂标志。
4. 串成调用链（这就是本讲的实践任务答案）：

   ```
   pliron apply_dialect_conversion
     → MirToLlvmConversionDriver::rewrite  (lib.rs:245)
       → op_cast::<dyn MirToLlvmConversion>(MirAddOp)  (lib.rs:297)
         → MirAddOp::convert  (interface_impls.rs:97)
           → convert::ops::arithmetic::convert_add  (arithmetic.rs:145)
             → llvm::FAddOp::new + add_fastmath_flags  (arithmetic.rs:154-155)
             → rewriter.replace_operation(mir.add → llvm.fadd)
   ```

5. 后续 `llvm.fadd` 由 `llvm-export` 打印成 `.ll` 文本（`%r = fadd contract float %a, %b`），再由 `llc` 编成 PTX。

**需要观察的现象**：在源码层面，你能看到 `mir-lower` 只产出 `llvm.fadd` 这个 dialect op；它既不直接打印文本，也不调 llc。文本化与 PTX 化都在下游。

**预期结果**：调用链上每一跳都有对应源码行号可指，没有任何「黑盒」环节。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把所有 op 的转换写成一个巨大的 `match opid`，而要用 op 接口 + `op_cast` 派发？

> **参考答案**：两个原因。一是可扩展性：新增一个 op 时，只要在 `dialect-mir`/`dialect-nvvm` 加 op 类型、在 `interface_impls.rs` 加一个 `impl MirToLlvmConversion`、在某个 `convert::*` 子模块加一个 `convert_*` 函数即可，无需改动驱动器的 `match`（O(1) 虚表查找也比对全表 OpId 快）。二是分发归属：每个 op 的转换逻辑挂在 op 自身的虚表上，符合「谁的数据谁负责」。

**练习 2**：浮点 `mir.add` 降级出的 `llvm.fadd` 只挂 `contract` 标志，绝不挂 `nnan`/`ninf` 等，这是为什么？

> **参考答案**：为了在「允许 FMA 收缩」与「禁止 FMA 收缩」两种模式下，结果都能与一个明确的参考（收缩或不收缩的逐位参考）一致。`contract` 只许可「乘后加」融合成 fma，不放松 NaN/Inf/舍入行为；而 `nnan`/`ninf`/`nsz`/`reassoc` 会改变浮点结果的可观察性。所以只挂 `contract` 是「最小但足够触发 NVPTX fma 融合」的许可。

---

### 4.4 PTX vs NVVM IR 分叉

#### 4.4.1 概念说明

`mir-lower` 把所有 op 降级到 LLVM dialect 之后，产物并不是只有「PTX」一种。cuda-oxide 在编排层会做一次关键分叉：

- **默认路径（PTX）**：LLVM dialect → `llvm-export` 导出成普通 `.ll` → `llc` 编成 PTX 文本 → 嵌进 `.oxart` 制品。
- **NVVM IR 路径（`.ll`）**：LLVM dialect → 先过 `nvvm-transforms::legalize_for_nvvm` 调整成 NVVM 方言 → 导出成 NVVM IR `.ll` → **跳过 llc** → 交给宿主侧 libNVVM/nvJitLink 编译。这条路径还会附带 `.options`/`.target` 边车记录 FMA 策略与目标架构。

分叉的触发条件在编排器里写得很集中，本节只看判定逻辑；libdevice 与两阶段链接的细节留到 u4-l5。

#### 4.4.2 核心流程

编排函数 `compile_translated_module` 在 lowering 之后做这一串决策（顺序见 4.5 的总图）：

1. **lowering 之后，检查模块是否用到 libdevice**：`module_uses_libdevice(ctx, module)` 检测有没有 `__nv_*` 调用。
2. **决定要不要出 NVVM IR**：`should_emit_nvvm_ir(policy, needs_libdevice)`。对 rustc 前端（`ExternalLinkAllowed`）路径，只要「显式请求 NVVM IR」或「检测到 libdevice」就出 NVVM IR；对独立 PTX 前端（`SelfContainedPtx`）永远不出 NVVM IR。
3. **若出 NVVM IR**：解析 NVVM 目标架构、选 Legacy 还是 Modern 方言、跑 `nvvm_transforms::legalize_for_nvvm`，再导出 `.ll`，直接返回 `ModuleArtifactKind::NvvmIr`（跳过 llc）。
4. **否则出 PTX**：导出普通 `.ll`，调 `llc` 生成 `.ptx`，返回 `ModuleArtifactKind::Ptx`。

#### 4.4.3 源码精读

分叉判定的核心是 `should_emit_nvvm_ir`——[crates/cuda-oxide-codegen/src/pipeline.rs:377-382](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L377-L382)：

```rust
fn should_emit_nvvm_ir(policy: OutputPolicy, needs_libdevice: bool) -> bool {
    match policy {
        OutputPolicy::SelfContainedPtx => false,
        OutputPolicy::ExternalLinkAllowed { request_nvvm_ir } => request_nvvm_ir || needs_libdevice,
    }
}
```

两条不变量由测试锁定（[pipeline.rs:413-430](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L413-L430)）：独立 PTX 前端「即使检测到 libdevice 也绝不出 NVVM IR」（因为独立前端没有后续 libNVVM 链接能力）；rustc 前端则在「自动检测到 libdevice」或「显式请求」时切换。

切换点在 `compile_translated_module` 内，lowering 完成后立即判断——[pipeline.rs:213-226](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L213-L226)：

```rust
let needs_libdevice = module_uses_libdevice(ctx, module);
let emit_nvvm_ir = should_emit_nvvm_ir(request.output_policy, needs_libdevice);
...
if needs_libdevice && !requested_nvvm_ir && emit_nvvm_ir && request.trace.verbose {
    request.trace.emit("\n=== Detected CUDA libdevice (`__nv_*`) calls; \
                        auto-emitting NVVM IR (skip llc) ===");
}
```

若出 NVVM IR，则做 NVVM 方言 legalize 与目标解析（[pipeline.rs:244-275](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L244-L275)），随后在导出阶段（[pipeline.rs:321-339](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L321-L339)）直接返回 `ModuleArtifactKind::NvvmIr`，并打 trace「Skipping llc; consumer owns libNVVM/nvJitLink build」。否则走 `llc` 出 PTX（[pipeline.rs:341-362](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L341-L362)）。

注意：这个分叉发生在 `mir-lower` 跑完之后。也就是说，**`mir-lower` 本身对最终出 PTX 还是 NVVM IR 一无所知**——它只产出 LLVM dialect，分叉是编排器 `cuda-oxide-codegen` 的职责。这是 #314 重构带来的清晰边界。

#### 4.4.4 代码实践

**实践目标**：在一个真实示例上看到「出 PTX」与「出 NVVM IR」两种产物的区别。

**操作步骤**：

1. 选一个不用浮点数学库的示例（如 `vecadd`），用 `cargo oxide pipeline vecadd`（详见 u1-l3 的 pipeline 子命令）跑一遍，在输出目录找到 `vecadd.ptx`（文本汇编）。
2. 再选一个用到 libdevice 数学函数的示例（仓库内有 `libm_math` 或 `mathdx_ffi_test` 等；具体示例名以本机 `crates/rustc-codegen-cuda/examples/` 目录为准），用 `cargo oxide pipeline <示例名>` 跑一遍，观察输出目录里出现的是 `.ll`（NVVM IR）而非 `.ptx`，且没有调用 `llc` 的 trace。
3. 对照 [pipeline.rs:213-226](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L213-L226) 的 trace 文本，确认第二种情况下日志里出现了「Detected CUDA libdevice ... auto-emitting NVVM IR (skip llc)」。

**需要观察的现象**：普通算术示例产出 `.ptx` 文本（可直接阅读 PTX 指令）；用 libdevice 的示例产出 NVVM IR `.ll`，其中数学函数是 `__nv_*` 的 `declare`，留给宿主侧 libNVVM/nvJitLink 去解析链接。

**预期结果**（待本地验证，依赖示例是否在当前 HEAD 存在）：两条路径的产物格式不同，且切换由 `module_uses_libdevice` 自动触发，无需用户手工指定。

#### 4.4.5 小练习与答案

**练习 1**：为什么独立 PTX 前端（`SelfContainedPtx`）即使检测到 libdevice 也坚持出 PTX？

> **参考答案**：因为独立前端（`cuda-oxide-codegen` 的实验性 API，详见 u6-l6）没有挂接宿主侧 libNVVM/nvJitLink 链接管线的能力，它承诺产出「自包含、可直接加载的 PTX」。NVVM IR 路径需要后续链接步骤才能变成可运行 cubin，这与独立前端的契约冲突，所以测试 `standalone_never_silently_switches_to_a_linkable_artifact`（[pipeline.rs:413-417](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L413-L417)）明确禁止这种「静默切换」。

**练习 2**：分叉发生在 `mir-lower` 内部还是外部？为什么这样设计？

> **参考答案**：发生在 `mir-lower` 外部——由编排器 `cuda-oxide-codegen` 的 `compile_translated_module` 决定。因为 `mir-lower` 的职责只是「dialect op → LLVM dialect op」，对「最终文本化成 PTX 还是 NVVM IR」不应有认知。把分叉放在编排器，让 lowering 这一段在全工具链只此一份、与产物格式解耦，这是 #314 抽出独立后端的核心收益之一。

---

### 4.5 LoweredVerification 阶段与 cuda-oxide-codegen 编排

#### 4.5.1 概念说明

本节回答两个问题：（1）lowering 之后的「校验」在哪一步、用什么错误类型报告？（2）#314 之后，整条后段流水线（含 lowering）是怎么被编排的？

`mir-lower` 把每个 op 换成 LLVM dialect op 后，可能引入结构问题——比如某个 op 的操作数/结果类型对不上、缺了必要的属性。pliron 的每个 op 都自带一个 `verify` 方法做结构校验。编排器在 lowering 之后、导出之前，会对整个模块跑一次完整校验。这次校验如果失败，被报告成一种专门的错误类型 `LoweredVerification`，以便和「翻译期校验失败」「mem2reg/unroll 校验失败」区分开。

#### 4.5.2 核心流程

`cuda-oxide-codegen` 的 `compile_translated_module` 是后段唯一的编排者，完整顺序如下（对应源码行号见下）：

```text
dialect-mir 模块
  │
  ├─(1) prepare_mir_module           verify + mem2reg + annotated unroll
  │       （变量调试模式跳过 mem2reg/unroll）
  │
  ├─(2) add_device_extern_declarations   给 extern "C" 设备函数提前声明
  │
  ├─(3) lower_to_llvm                 ★ mir-lower 在此被调用（allow_fma_contraction）
  │
  ├─(4) module_uses_libdevice + should_emit_nvvm_ir   决定 PTX / NVVM IR 分叉
  │
  ├─(5) [若 NVVM IR] nvvm_transforms::legalize_for_nvvm
  │
  ├─(6) verify_operation("llvm module")   ★ LoweredVerification 在此触发
  │
  ├─(7) export_llvm_ir              导出 .ll
  │
  └─(8a)[NVVM IR] 直接返回 NvvmIr（跳过 llc）
     (8b)[PTX]      generate_ptx（llc）→ 返回 Ptx
```

注意步骤 (1) 的 prepare 阶段在 #314 之后也搬进了 `cuda-oxide-codegen`——它先 verify，再 `mem2reg`，再 verify，再 `unroll_annotated_loops`，再 verify（每步之间都校验，便于定位是哪个 pass 引入了问题）。

#### 4.5.3 源码精读

mir-lower 的调用点是 `lower_to_llvm`——[crates/cuda-oxide-codegen/src/lower.rs:33-51](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lower.rs#L33-L51)，它就是 `mir_lower::lower_mir_to_llvm_with_options` 的薄封装，把 `allow_fma_contraction`（来自 `BackendOptions.no_fma` 取反）传进去，错误格式化成 `PipelineError::Lowering`：

```rust
pub fn lower_to_llvm(ctx, module_op_ptr, allow_fma_contraction) -> Result<(), PipelineError> {
    mir_lower::register(ctx);
    match mir_lower::lower_mir_to_llvm_with_options(
        ctx, module_op_ptr,
        mir_lower::LoweringOptions { allow_fma_contraction },
    ) {
        Ok(()) => Ok(()),
        Err(e) => Err(PipelineError::Lowering(e.disp(ctx).to_string())),
    }
}
```

它在编排里被调用的那一行——[pipeline.rs:211](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L211)：`lower_to_llvm(ctx, module, !request.backend.no_fma)?;`。

lowering 之后的结构校验，用通用 `verify_operation`——[crates/cuda-oxide-codegen/src/verify.rs:19-42](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/verify.rs#L19-L42)。它会递归找最内层失败 op，给出更好的报错位置。编排器在 lowering 后调用它，并通过 `as_lowered_verification` 把错误映射成 `LoweredVerification`——[pipeline.rs:286](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L286) 与映射函数 [pipeline.rs:384-391](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L384-L391)：

```rust
verify_operation(ctx, module, "llvm module").map_err(as_lowered_verification)?;
...
fn as_lowered_verification(error: PipelineError) -> PipelineError {
    match error {
        PipelineError::Verification { message, operation, .. }
            => PipelineError::LoweredVerification { message, operation },
        other => other,
    }
}
```

`LoweredVerification` 是 `PipelineError` 里的一个独立变体——[crates/cuda-oxide-codegen/src/error.rs:22-26](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/error.rs#L22-L26)：

```rust
/// The lowered LLVM-dialect module failed structural verification.
LoweredVerification {
    message: String,
    operation: Option<String>,
},
```

它的 Display（[error.rs:55-62](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/error.rs#L55-L62)）明确标注「Verification failed for lowered LLVM module」，与翻译期/prepare 期的 `Verification` 区分开。这条区分由测试 `final_verification_is_not_reported_as_mir_preparation` 锁定（[pipeline.rs:432-444](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L432-L444)）。

最后，整个后段的入口 `compile_translated_module` 在 [pipeline.rs:152-375](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L152-L375)，prepare 阶段在 [pipeline.rs:169-192](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L169-L192)（调 `prepare_mir_module`，其内部见 [prep.rs:25-53](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/prep.rs#L25-L53)）。`mir-importer` 侧只在翻译完每个函数后调一次 `compile_translated_module`，其余后段细节全由 `cuda-oxide-codegen` 承担——见 [crates/mir-importer/src/pipeline.rs:322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L322)。

#### 4.5.4 代码实践

**实践目标**：把「lowering 现由 cuda-oxide-codegen 编排」这句话在源码上落实，并确认 `LoweredVerification` 与普通 `Verification` 是两条不同错误路径。

**操作步骤**：

1. 在 [crates/mir-importer/src/pipeline.rs:322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L322) 确认：`mir-importer` 的 `run_pipeline` 在翻译完所有函数后，**只调一次** `compile_translated_module`，后者来自 `cuda_oxide_codegen::__private`。
2. 跟进到 [cuda-oxide-codegen/src/pipeline.rs:152](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L152) 的 `compile_translated_module`，按行号找到：prepare(181) → externs(203) → lower(211) → libdevice 判定(213) → nvvm legalize(273) → 校验(286) → 导出(305) → PTX/NVVM 分叉(321/344)。
3. 在 [lower.rs:33-51](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lower.rs#L33-L51) 确认：`lower_to_llvm` 把 mir-lower 的错误包成 `PipelineError::Lowering`；而 lowering 之后那次结构校验的错误，经 `as_lowered_verification` 包成 `PipelineError::LoweredVerification`。
4. 在 [error.rs:13-35](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/error.rs#L13-L35) 通览 `PipelineError` 全部变体，理解每个阶段（Translation / Verification / Lowering / LoweredVerification / Export / PtxGeneration）各对应流水线哪一段。

**需要观察的现象**：`PipelineError` 的变体名几乎一对一映射到后段流水线的工位；`LoweredVerification` 专门留给「lowering 之后、导出之前」那次整模块校验。

**预期结果**：你能指着源码说清——「lowering 失败」与「lowering 后校验失败」是两种不同的错误，分别走 `Lowering` 与 `LoweredVerification` 变体，便于排障时立刻定位问题出在哪个 pass。

#### 4.5.5 小练习与答案

**练习 1**：为什么要把「lowering 后的校验失败」单独定义为 `LoweredVerification`，而不是复用 `Verification`？

> **参考答案**：为了错误诊断的精度。`Verification` 还被用于翻译期校验、mem2reg 校验、unroll 校验（见 prep.rs 里 `name: "module post-mem2reg"` 等）。如果 lowering 后的失败也混进 `Verification`，用户看到报错时分不清是「翻译错了」「优化 pass 错了」还是「lowering 错了」。`LoweredVerification` 明确告诉用户：模块在 lowering 之前是合法的，是 lowering 这一步（或它产出的 LLVM dialect op）引入了结构问题。

**练习 2**：`prepare_mir_module` 为什么在 `mem2reg` 与 `unroll` 之间和之后各跑一次 `verify_operation`？

> **参考答案**：为了把「哪个 pass 引入了非法 IR」定位到具体 pass。mem2reg 跑完先校验，能立刻发现是 mem2reg 出错；unroll 跑完再校验，能定位到 unroll。若只在最后校验一次，失败时无法区分责任 pass，调试成本高。这是一种「每个破坏性 pass 之后立刻校验」的防御性编排。

---

## 5. 综合实践

把本讲的知识串起来：**手动追踪一行 `c = a + b`（`a`、`b`、`c` 为 `f32`）从 Rust 源码到 PTX 指令，经过后段流水线的每一站，并标出每站对应的源码位置与产物变化。**

具体步骤：

1. **翻译（mir-importer，u4-l2）**：`a + b` 在 stable MIR 里是一个 `BinOp::Add`。mir-importer 的 terminator/rvalue 翻译把它变成一个 `mir.add` op。产物：dialect-mir 里的 `mir.add`。

2. **prepare（cuda-oxide-codegen/prep.rs）**：[prep.rs:25-53](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/prep.rs#L25-L53) 先 verify，再 mem2reg（把栈槽提升为 SSA，本例若 `a`/`b` 来自参数则基本无影响），再 unroll（本例无循环，无影响）。产物：仍是 `mir.add`，但底层值已是 SSA。

3. **lower（本讲核心）**：
   - 框架遍历到 `mir.add`，`can_convert_op` 看到 `mir` 方言返回 true。
   - `rewrite` 走 `op_cast` 命中 `MirAddOp` 的接口实现（[interface_impls.rs:95-105](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs#L95-L105)）。
   - `convert_add`（[arithmetic.rs:145-165](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L145-L165)）因 f32 走 `FAddOp`，挂 `CONTRACT` 标志。
   - `replace_operation` 把 `mir.add` 换成 `llvm.fadd`。产物：LLVM dialect 的 `llvm.fadd contract`。

4. **lowered verification（本讲 4.5）**：[pipeline.rs:286](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L286) 对整模块校验，失败映射成 `LoweredVerification`。产物：通过校验的 LLVM dialect 模块。

5. **分叉判定（本讲 4.4）**：本例不含 libdevice，`should_emit_nvvm_ir` 返回 false，走 PTX 路径。

6. **导出 + llc（下游 llvm-export / llc）**：`llvm-export` 把 `llvm.fadd` 打印成 `%r = fadd contract float %a, %b`；`llc` 把它编成 PTX（在 NVPTX 后端，配合相邻的 fmul + `contract` 可能融合成 `fma.rn.f32`）。产物：`.ptx` 文本。

7. **收尾**：自己用一句话回答实践任务的后半问——「该 lowering 现由 `cuda-oxide-codegen` 编排」体现在哪里？答：`mir-importer::run_pipeline` 翻译完所有函数后只调一次 `cuda_oxide_codegen::compile_translated_module`（[mir-importer/src/pipeline.rs:322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L322)），后者在 [pipeline.rs:211](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L211) 调 `lower_to_llvm`，进而调 `mir_lower::lower_mir_to_llvm_with_options`。lowering 的策略（FMA）、顺序（prepare 之后、校验之前）、错误归类（`Lowering`/`LoweredVerification`）全部由编排器决定，`mir-lower` 本身只是「执行降级」的纯函数式工位。

完成后，建议你再用 `cargo oxide pipeline vecadd` 实际跑一次（详见 u1-l3），对照输出目录的 `.ll` 文件，找到其中 `fadd contract` 那一行，反向印证你追踪的路径。

## 6. 本讲小结

- **职责单一**：`mir-lower` 只把 `dialect-mir`/`dialect-nvvm` 的 op 降级成 LLVM dialect op，不做优化、不导出文件、不决定产物格式；遍历交给 pliron 的 `DialectConversion` 框架。
- **同一接口，两类转换**：普通语义 op（`convert::ops::*`）与 GPU intrinsic（`convert::intrinsics::*`）都挂在同一个 op 接口 `MirToLlvmConversion` 上，靠 `op_cast` 虚表 O(1) 派发，新增 op 不必改动驱动器。
- **状态与函数转换**：跨 op 的 CUDA 专用状态（共享/设备全局去重、动态共享内存对齐）集中在 `MirToLlvmConversionDriver` 与 `context.rs`；`convert_func` 是最复杂的转换器，负责函数签名展平、kernel 属性传播、入口块聚合参数重建与对齐合并。
- **产物分叉在编排器**：PTX vs NVVM IR 的分叉由 `should_emit_nvvm_ir`（libdevice 检测 / 显式请求）决定，发生在 `mir-lower` 之外，lowering 对最终格式一无所知。
- **#314 重构**：lowering 及其后段（prepare/externs/legalize/verify/export/llc）整体迁入 rustc 无关的 `cuda-oxide-codegen`，`mir-importer` 翻译完只调一次 `compile_translated_module`，全工具链后段只有一份实现。
- **错误分级**：`PipelineError` 的变体一对一映射流水线工位；本轮新增 `LoweredVerification` 专门承接「lowering 之后那次整模块校验」的失败，与翻译期/prepare 期的 `Verification` 区分。

## 7. 下一步学习建议

- **向下深潜 lowering 细节**：本讲是鸟瞰，u6-l3（mir-lower 深潜：ops 转换与算术/含 FMA 收缩）会逐文件读 `convert/ops/arithmetic.rs`、`call.rs`、`memory.rs` 与 `convert/intrinsics/*`，讲清每个 converter 的内部实现与扩展模式。
- **看 NVVM IR 路径的下游**：本讲只讲了「在哪里分叉」。u4-l5（从 NVVM IR 到 cubin：libNVVM + nvJitLink + libdevice）会展开分叉之后的故事——libdevice、两阶段链接、FMA 策略如何随 `.options` 边车一路传到最终 cubin。
- **看独立后端**：u6-l6（独立后端 cuda-oxide-codegen）会从「experimental 公共 API」视角讲这个 crate 怎么脱离 rustc 单独组装 dialect-mir 并出 PTX，本讲的 `compile_translated_module` 在那里有另一条 `for_standalone_ptx` 入口。
- **看新 intrinsic 怎么落进 lowering**：u6-l4（端到端新增一个 intrinsic）给出「cuda-device API → dialect-nvvm op → mir-importer 翻译 → mir-lower 的 `convert::intrinsics` lowering」的全栈改动清单，本讲的 `interface_impls.rs` + `convert/intrinsics/*` 正是其中一环。
- **建议阅读的源码**：先把 [crates/mir-lower/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs) 顶部文档（含架构图与 GPU intrinsic lowering 策略）通读一遍，再按本讲的源码地图逐文件深入。
