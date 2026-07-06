# mir-lower 深潜：ops 转换与算术（含 FMA 收缩）

## 1. 本讲目标

本讲是 u4-l4（MIR Lowering 鸟瞰）的「向下钻取」篇，承接 u6-l2（mir-importer 翻译机）。

u4-l4 已经告诉你：`mir-lower` 把 `dialect-mir` / `dialect-nvvm` 的 op 统一降级成 LLVM dialect op，遍历交给 pliron 的 `DialectConversion` 框架，crate 自己只写「每个 op 怎么换」。本讲要回答的是——**「怎么换」这件事到底落在哪几行代码上**。

学完后你应该能够：

- 说清一个 `mir.add` / `mir.div` 是如何变成 `llvm.add` / `llvm.sdiv` 的，尤其是**符号性（signedness）丢失后如何在 lowering 层恢复**。
- 解释 cuda-oxide 的 **FMA 收缩契约**：为什么只挂 `contract` 这一个 fast-math 标志、它由谁开关、在 `.ll` 里能看到什么差异。
- 画出 `mir.call` 的漏斗：从「rust bit/saturating/bigint/float-math 占位调用」到「LLVM intrinsic 调用 / libdevice `__nv_*` 调用 / 普通函数调用」的分派路径。
- 对照本轮 #327/#328/#329 新增的三条 mma（f16/tf32/s8），看懂 **wmma intrinsic 的 convergent inline-PTX 模式**，并能照葫芦画瓢追踪 s8 `m16n8k32` 的 lowering 链路。

## 2. 前置知识

本讲默认你已经读过 u4-l4 与 u6-l2。下面几个概念会反复出现，先用一段话把它们对齐：

- **dialect-mir / dialect-nvvm / LLVM dialect**：cuda-oxide 在 Pliron IR 里维护的几层「方言」（命名空间隔离的 op/type 集合）。`dialect-mir`（前缀 `mir.`）贴近 Rust 语义，由 mir-importer 从 rustc MIR 一对一翻译而来；`dialect-nvvm`（前缀 `nvvm.`）贴近 PTX/NVVM 指令；`LLVM dialect`（前缀 `llvm.`）是 pliron-llvm 提供的、几乎与 LLVM IR 一一对应的最后一层。`mir-lower` 的职责就是把前两层收敛到第三层。

- **op interface 与 op_cast 派发**：每个待 lowering 的 op 实现一个名为 `MirToLlvmConversion` 的 op 接口（trait），driver 通过 `op_cast` 做一次虚表（vtable）查找，O(1) 地拿到对应的转换器。你写新 op 的 lowering 时，本质就是「写一个 `convert_*` 函数 + 在 `interface_impls.rs` 里给 op 实现接口」。

- **fast-math 标志**：LLVM 浮点 op 上的一组「允许更激进优化」的许可位，包括 `nnan`（假设无 NaN）、`ninf`、`nsz`、`reassoc`（可重结合）、`arcp`、`contract`（可把乘加收缩成融合乘加）。**许可位越多，结果越快但越偏离 IEEE-754 严格语义。**

- **libdevice**：NVIDIA 以 LLVM bitcode 形式分发的设备端数学库，导出 `__nv_sinf` / `__nv_expf` 等 `__nv_*` 符号。当内核用到 `sin`/`exp` 等浮点数学函数时，lowering 会把它变成对 `__nv_*` 的调用，由后续 libNVVM/nvJitLink 阶段链接（见 u4-l5）。

- **符号性（signedness）丢失**：LLVM 的整数类型是 **signless**（无符号/有符号之分不在类型里），有符号还是无符号由指令本身决定（`sdiv` vs `udiv`、`ashr` vs `lshr`、`icmp slt` vs `ult`）。但 Rust 的 `i32` / `u32` 是有区别的，这个区别在「类型转换」阶段会被抹掉，lowering 必须从别处把它捞回来。

## 3. 本讲源码地图

本讲全部围绕 `crates/mir-lower/` 这一个 crate，集中在 `src/convert/` 下的几个文件：

| 文件 | 作用 |
|------|------|
| [`crates/mir-lower/src/lib.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs) | lowering 的入口与 driver：`LoweringOptions`、`MirToLlvmConversionDriver`、`lower_mir_to_llvm_with_options` |
| [`crates/mir-lower/src/context.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs) | 跨 op 的 CUDA 状态类型（共享/设备全局去重、动态共享内存对齐），以及把 `LoweringOptions` 挂进 pliron Context 的存取函数 |
| [`crates/mir-lower/src/convert/ops/arithmetic.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs) | **算术/位运算/比较 op** 的 lowering，FMA 收缩的核心 `add_fastmath_flags` 就在这里 |
| [`crates/mir-lower/src/convert/ops/call.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs) | `mir.call` 的 lowering：rust intrinsic 占位符分派、libdevice 映射、ABI 参数展平与地址空间桥接 |
| [`crates/mir-lower/src/convert/ops/memory.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/memory.rs) | `mir.load/store/alloca/ref/ptr_offset/memcpy/shared_alloc/...` 的 lowering |
| [`crates/mir-lower/src/convert/intrinsics/common.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/common.rs) | 所有 GPU intrinsic 共用的工具：`call_intrinsic`、`inline_asm_convergent`、地址空间转换等 |
| [`crates/mir-lower/src/convert/intrinsics/atomic.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs) | 原子 op：作用域/序映射、`atomicrmw` 栅栏拆分、打包原子加 |
| [`crates/mir-lower/src/convert/intrinsics/wmma.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs) | warp 级 `mma.sync` 与 `movmatrix`：本轮新增 f16/tf32/s8 三条 mma 的 lowering |
| [`crates/mir-lower/src/convert/interface_impls.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs) | 把每个 op 类型接到对应 `convert_*` 函数的 `#[op_interface_impl]` 表 |

## 4. 核心概念与源码讲解

### 4.1 派发骨架：driver、op_cast 与需要跨 op 状态的「特例」

#### 4.1.1 概念说明

`mir-lower` 的整体形状是 pliron 的 `DialectConversion`：框架负责遍历 IR、保证 def-before-use 顺序、做类型转换、修补块参数；crate 只需要提供一个实现了 `DialectConversion` 的 driver，告诉框架「哪些 op 能转、怎么转」。

driver 的核心逻辑非常短——见 [`crates/mir-lower/src/lib.rs:245-305`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L245-L305) 的 `rewrite`。它分两类：

1. **需要跨函数状态的 op**（少数几个）：`MirFuncOp`、`MirSharedAllocOp`、`MirGlobalAllocOp`、`MirExternSharedOp` 被特判，直接调用带额外状态参数的 `convert_*_dc` 版本。
2. **其余所有 op**：走通用 `op_cast` 派发，O(1) 虚表查到该 op 实现的 `MirToLlvmConversion`，调用其 `convert`。

那几个被特判的 op 之所以不走通用路径，是因为它们需要 driver 持有的三张跨函数状态表——`shared_globals`（共享内存全局去重）、`device_globals`（设备 `static` 去重）、`dynamic_smem_alignments`（每函数动态共享内存对齐）。这三张表定义在 [`crates/mir-lower/src/context.rs:51-72`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs#L51-L72)：

```rust
pub type SharedGlobalsMap = FxHashMap<String, pliron::identifier::Identifier>;
pub type DeviceGlobalsMap = FxHashMap<String, pliron::identifier::Identifier>;
pub type DynamicSmemAlignmentMap = FxHashMap<String, (pliron::identifier::Identifier, u64)>;
```

> 通用 `op_cast` 路径拿不到 driver 字段，所以凡是要读写这三张表的 op，都得从 `rewrite` 里手动分流。这是「框架给出钩子，crate 在钩子里塞状态」的典型折中。

#### 4.1.2 核心流程

把整个 lowering 想成一条流水线：

```text
lower_mir_to_llvm_with_options(ctx, module, options)
  │  1. set_lowering_options(ctx, options)          ← 策略挂进 Context（不是全局环境变量）
  │  2. propagate_kernel_dynamic_shared_alignments  ← 预处理：算每函数动态共享内存最大对齐
  │  3. MirToLlvmConversionDriver { 共享/设备/对齐 三张空表 }
  └─► apply_dialect_conversion(ctx, driver, module) ← 框架遍历，逐 op 调 driver.rewrite
        │
        ├─ 特判 op（Func/SharedAlloc/GlobalAlloc/ExternShared）→ 带 _dc 的转换器
        └─ 其余 op → op_cast::<dyn MirToLlvmConversion> → converter.convert(...)
```

关键代码点：

- [`crates/mir-lower/src/lib.rs:330-349`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L330-L349)：`lower_mir_to_llvm_with_options` 的三步编排。
- [`crates/mir-lower/src/convert/mod.rs:8-12`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/mod.rs#L8-L12)：模块顶部文档说明「每个 op 实现 `MirToLlvmConversion`，经 `op_cast` O(1) 派发」。

> 注意：`LoweringOptions` 是通过 pliron Context 的 `aux_data` 在整个 lowering 期间传递的，而不是用进程级环境变量。这意味着 lowering 内的任何 op 转换器都能用 `lowering_options(ctx)` 读到策略，且不会互相干扰。见 [`crates/mir-lower/src/context.rs:25-49`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/context.rs#L25-L49)。

### 4.2 算术 op 的 lowering：符号性恢复是核心难点

#### 4.2.1 概念说明

`dialect-mir` 的算术 op 与 Rust 的 `BinOp` 一一对应：`mir.add / sub / mul / div / rem / shl / shr / bitand / bitor / bitxor / neg / not / checked_add / ...` 以及比较 `mir.cmp`。它们的 lowering 看起来「平淡无奇」（`mir.add` → `llvm.add`），但有一个贯穿全模块的难点：**符号性**。

`i32` 和 `u32` 在类型转换后都变成 signless 的 `i32`，于是一切依赖符号性的指令都得另找信息源：

| 操作 | 有符号 → LLVM | 无符号 → LLVM |
|------|---------------|---------------|
| `div` | `llvm.sdiv` | `llvm.udiv` |
| `rem` | `llvm.srem` | `llvm.urem` |
| `shr` | `llvm.ashr`（算术右移） | `llvm.lshr`（逻辑右移） |
| `cmp lt/le/gt/ge` | `icmp slt/...` | `icmp ult/...` |
| `checked_add` | `llvm.sadd.with.overflow` | `llvm.uadd.with.overflow` |

#### 4.2.2 核心流程

符号性的恢复靠一个 `OperandsInfo` 参数——它记录了「每个值在类型转换之前是什么类型」。`is_signed_int_op` 先尝试从这条历史里查 `IntegerType` 的 `signedness`：

```text
is_signed_int_op(ctx, op, operands_info):
  lookup_most_recent_of_type::<IntegerType>(operand 0)
    ├─ 命中 → 看 signedness 是否 == Signed
    ├─ 是 MirPtrType → 指针按无符号处理
    └─ 查不到（类型从未被转换过，今天是 signless i1 布尔）→ 退回 live 类型的 signedness
```

这套「先查历史、查不到再退回当前类型」的设计，是为了应付 `||`/`&&` 短路求值产生的 phi——它从来没有可转换的 MIR 类型，只能用 live 类型兜底。详见 [`crates/mir-lower/src/convert/ops/arithmetic.rs:81-114`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L81-L114) 的注释。

#### 4.2.3 源码精读

以「最简单」的 `convert_add` 为例，看一个 lowering 函数的标准三段式（取操作数 → 建新 op → 替换旧 op）：

[crates/mir-lower/src/convert/ops/arithmetic.rs:145-165](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L145-L165) —— `convert_add`：先判断是不是浮点（`is_float_type`），是则建 `llvm.fadd` 并挂 fast-math 标志，否则建带溢出标志的 `llvm.add`，最后 `insert_operation` + `replace_operation` 把旧 `mir.add` 换掉。

依赖符号性的 `convert_div` 多一步选择：

[crates/mir-lower/src/convert/ops/arithmetic.rs:223-244](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L223-L244) —— 浮点走 `llvm.fdiv`；整数则用 `is_signed_int_op` 在 `sdiv` / `udiv` 之间二选一。

移位 `convert_shr/shl` 还要额外处理 Rust 的「不检查移位量」语义——LLVM 在移位量 ≥ 位宽时是 poison，所以必须先把移位量对齐到同位宽、再 `& (width-1)` 屏蔽，见 [`crates/mir-lower/src/convert/ops/arithmetic.rs:425-506`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L425-L506) 的 `convert_shift` / `mask_shift_amount`。

比较 `convert_cmp` 有一处容易被忽略的正确性细节：浮点谓词刻意对齐 `rustc_codegen_ssa` 的 `bin_op_to_fcmp_predicate`——`Eq → oeq`、`Lt → olt`、`Ne → une`（无序，含 NaN 时为真），保证 `a != b` 恰好等于 `!(a == b)`。见 [crates/mir-lower/src/convert/ops/arithmetic.rs:657-688](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L657-L688)。

#### 4.2.4 代码实践

这是一个**纯源码阅读型实践**，不需要 GPU：

1. **实践目标**：理解 `is_signed_int_op` 如何「穿越类型转换」找回符号性。
2. **操作步骤**：在 `arithmetic.rs` 中打开 `convert_div`（L223）与 `is_signed_int_op`（L81）。想象一段 Rust：`let z = a / b;`，其中 `a: i32, b: u32`（先假设能编译）。mir-importer 会产出两个 `mir.div`，操作数的 MIR 类型分别带 `Signed` / `Unsigned`。
3. **需要观察的现象**：在 DialectConversion 之后，两个操作数的 live 类型都已经是 signless `i32`；`is_signed_int_op` 通过 `operands_info.lookup_most_recent_of_type::<IntegerType>` 找回转换前的 signedness，从而分别发出 `llvm.sdiv` 与 `llvm.udiv`。
4. **预期结果**：你能用自己的话说出「如果没有 `operands_info`，lowering 就无法区分 `sdiv` 与 `udiv`，整除语义会出错」。

#### 4.2.5 小练习与答案

**练习 1**：`mir.shl` 为什么不需要 `is_signed_int_op`，而 `mir.shr` 需要？

> 参考答案：左移 `shl` 不区分符号（高位直接丢弃），所以 `convert_shl` 不查 signedness；右移 `shr` 必须区分算术右移（`ashr`，高位补符号位）与逻辑右移（`lshr`，高位补 0），所以要查。见 `convert_shr`（L381-395）与 `convert_shl`（L401-416）。

**练习 2**：`mir.not` 在 LLVM 里没有直接对应指令，它是怎么实现的？

> 参考答案：转成「与全 1 常量异或」`llvm.xor operand, -1`。见 [`convert_not`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L608-L640)（L608-640）。

### 4.3 FMA 收缩策略：只挂 `contract` 这一个标志

#### 4.3.1 概念说明

**FMA 收缩（contraction）** 指编译器把「一次乘法接一次加/减」`a*b + c` 合并成一条**融合乘加** `fma(a, b, c)`。两者的差别在舍入次数：

- 不收缩：先算 \(a \cdot b\) 舍入一次，再加 \(c\) 舍入一次，共两次舍入。
- 收缩（FMA）：\(a \cdot b + c\) 在内部以更高精度计算，只舍入一次，得到 \(\mathrm{round}(a \cdot b + c)\)。

\[ \text{非 FMA: } \mathrm{round}(\mathrm{round}(a\cdot b) + c) \quad\neq\quad \text{FMA: } \mathrm{round}(a\cdot b + c) \]

两者结果在边界情况下会差 1 ULP。NVIDIA 的 nvcc 默认 `--fmad=true`（允许收缩），这是它开箱即用的唯一一项 fast-math 放宽。cuda-oxide 刻意**对齐**这个默认：在浮点 `add/sub/mul/div/rem` 上**只挂 `contract` 标志**，绝不挂 `reassoc/nnan/ninf/nsz/arcp`，这样无论收缩与否，结果都与对应参考实现逐位一致。

#### 4.3.2 核心流程

开关 `allow_fma_contraction` 在 [`LoweringOptions`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/lib.rs#L158-L173)（默认 `true`）里，经 `set_lowering_options` 挂进 Context，再由每个浮点 op 在建指令时读取并决定挂不挂 `contract`：

```text
建一个浮点二元 op（fadd/fsub/fmul/fdiv/frem）
  └─► add_fastmath_flags(ctx, op)
        flags = if lowering_options(ctx).allow_fma_contraction { CONTRACT } else { empty() }
        把 flags 写进 op 的 "llvm_fast_math_flags" 属性
```

`contract` 标志是「给后端的许可」：它本身不改变当前指令的语义，但告诉 NVPTX 后端「你可以把我前面那个 `fmul` 和我这个 `fadd` 融合成一条 `fma.rn.f32`」。

#### 4.3.3 源码精读

整个策略的核心就这一个函数：

[crates/mir-lower/src/convert/ops/arithmetic.rs:126-135](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L126-L135) —— `add_fastmath_flags`：根据 `allow_fma_contraction` 决定 `FastmathFlags::CONTRACT` 还是 `FastmathFlags::empty()`。函数上方 L116-125 的注释把「为什么只挂 contract、绝不挂其他」讲得很清楚——「我们刻意不设 reassoc/nnan/ninf/nsz/arcp，使结果在收缩/不收缩两种参考下都逐位可比」。

这套行为被三个单元测试**锁定**，是本讲最值得读的「契约文档」：

[crates/mir-lower/src/convert/ops/arithmetic.rs:827-951](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs#L827-L951) —— 三个测试：浮点 `mul` 只挂 `contract`、浮点 `add` 只挂 `contract`、`allow_fma_contraction=false` 时 `mul/add/sub` 链全部 `empty()`。第三个测试的注释点出一个非平凡结论：**仅靠后端命令行禁用 FMA 是不够的**，必须同时把每条指令上的 `contract` 标志也拿掉，否则任一侧仍带 `contract`，后端照样会融合。

> 补充：`core::intrinsics::f*_fast`（显式声明「快」语义的浮点运算）走的是另一条路。它本来就该用全套 `fast` 标志，但当编译期禁用 FMA 时，要从 `fast` 里**单独抠掉 `contract`**。见 `call.rs` 的 [`fast_float_intrinsic_flags`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L1052-L1060)（L1052-1060）及其测试 L1482-1496。也就是说「禁用 FMA」是一个编译期全局策略，对普通浮点运算和 `f*_fast` 都生效，只是两者的「剩余标志」不同。

#### 4.3.4 代码实践（本讲主实践的第一部分）

1. **实践目标**：亲眼看到 FMA 收缩契约在 `.ll` 中造成的差异。
2. **操作步骤**：
   - 复制一个最简示例（如 `vecadd`），把核函数改成包含一次乘加：
     ```rust
     #[kernel]
     fn fma_demo(out: &mut [f32], a: &[f32], b: &[f32], c: &[f32]) {
         let i = thread::index_1d();
         // 这里同时出现 fmul 和 fadd，是 FMA 收缩的目标
         out[i] = a[i] * b[i] + c[i];
     }
     ```
     （示例代码，需自行放进一个 `#[cuda_module]` 并用 `unsafe` 启动。）
   - 用 `cargo oxide pipeline <name>` 生成中间产物，找到设备端 `.ll` 文件。
   - 在两种环境下各跑一次：
     - 默认（FMA on）：`cargo oxide pipeline <name>`
     - 禁用 FMA：`CUDA_OXIDE_NO_FMA=1 cargo oxide pipeline <name>`
   - 用 `diff` 对比两份 `.ll`。
3. **需要观察的现象**：
   - FMA on：`fmul` 与 `fadd` 指令上带有 fast-math 标记（在 LLVM IR 文本里通常表现为 `contract` 修饰）；下游 `llc` 很可能把它们融合成一条 `fma.rn.f32`（出现在 `.ptx` 里）。
   - FMA off：`fmul`/`fadd` 不带 `contract`，保持两条独立指令，`.ptx` 里是 `mul.rn.f32` + `add.rn.f32`。
4. **预期结果**：`.ll` 层的 `contract` 标志差异是确定可见的；`.ptx` 层是否真正融合成 `fma.rn.f32` 取决于 `llc` 的模式识别，**待本地验证**。即便 `llc` 没融合，IR 层的标志差异也已由上节三个单元测试锁定，是契约的真相源。
5. 关于 `CUDA_OXIDE_NO_FMA` 如何一路传到这里的链路，见 u6-l1（`CUDA_OXIDE_NO_FMA` → `PipelineConfig.allow_fma_contraction` → `backend_options.no_fma`，取反后进入 `LoweringOptions::allow_fma_contraction`）。

#### 4.3.5 小练习与答案

**练习**：为什么「只关掉后端 `llc` 的 FMA 命令行开关」不够，还必须在 IR 层把 `contract` 标志也去掉？

> 参考答案：`contract` 是「前端给后端的许可」。只要 `fmul` 或 `fadd` 任一侧仍带 `contract`，`llc` 就有权把它们融合，命令行开关挡不住。所以禁用 FMA 必须在生成 IR 时就不挂 `contract`——这正是 `add_fastmath_flags` 读 `allow_fma_contraction` 的目的，也是单元测试 `no_fma_contraction_omits_contract_flags_from_mul_add_sub_chains` 锁定的不变量。

### 4.4 call → intrinsics 的对接：从占位调用到 libdevice / LLVM intrinsic

#### 4.4.1 概念说明

mir-importer 在翻译 `TerminatorKind::Call` 时，对一部分「rustc 没有 MIR 体的特殊调用」（如 `core::intrinsics::ctlz`、`f32::sin`、`u32::rotate_left`）会保留成**占位 `mir.call`**——callee 名是一个约定好的占位符（如 `CALLEE_SIN_F32`）。`mir-lower` 的 `call.rs::convert` 必须把这些占位调用识别出来，翻译成对应的 LLVM intrinsic 调用或 libdevice 调用；至于「普通设备函数调用」则按 CUDA ABI 展平参数后发出 `llvm.call`。

#### 4.4.2 核心流程

`convert` 是一个**漏斗**，逐层拦截：

```text
convert(mir.call):
  1. RustBitIntrinsic::from_placeholder_callee     → rotate_left/ctpop/ctlz/cttz/bswap/bitreverse
  2. RustSaturatingIntrinsic                        → sadd_sat/ssub_sat/uadd_sat/usub_sat
  3. RustBigIntIntrinsic::CarryingMulAdd            → 双宽乘累加，拆 (low, high)
  4. RustFloatMathIntrinsic                         → sin/cos/exp/.../fma/fabs/...
       ├─ f*_fast 子族 → lower_fast_binop（LLVM 浮点 binop + fast 标志）
       └─ 其余        → libdevice __nv_* 调用
  5. 都不是 → 普通函数调用：ABI 参数展平 + addrspace 桥接 + llvm.call
```

每拦截到一类，就 `return` 走专门路径；都拦不到才落到通用的「普通调用」处理。

#### 4.4.3 源码精读

漏斗入口在 [`crates/mir-lower/src/convert/ops/call.rs:485-650`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L485-L650)：四个 `if let Some(intrinsic) = ... from_placeholder_callee(...)` 串成一条拦截链，最后落到 callee 解析与参数展平。

**浮点数学 → libdevice 映射**是其中最大的一张表。`RustFloatMathIntrinsic` 枚举列出了 50+ 个 rustc 浮点 intrinsic，[`libdevice_name`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L328-L417)（L328-417）把它们映射到 `__nv_*`：

```rust
Self::SinF32  => Ok("__nv_sinf"),
Self::SinF64  => Ok("__nv_sin"),
Self::FmaF32 | Self::FmuladdF32 => Ok("__nv_fmaf"),
// ...
```

`convert_rust_float_math_intrinsic`（[L989-1039](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L989-L1039)）先把 `f*_fast` 子族分流到 `lower_fast_binop`，剩下的用 `helpers::ensure_intrinsic_declared` 在模块里声明 `__nv_*`，再发一条直接调用。注意：这些 `__nv_*` 在当前模块里只有声明、没有定义，真正的实现在 libdevice bitcode 里，由后续 libNVVM/nvJitLink 阶段链接（u4-l5）。

> `f*_fast` 子族是特例：它们**不**调 libdevice，而是直接降级成 `llvm.fadd/fsub/fmul/fdiv/frem` 并挂 `fast` fast-math 标志（见 `lower_fast_binop` L1062-1099），因为这正是 Rust `f*_fast` intrinsic 承诺的语义。

**普通调用的 ABI 处理**有三块值得读的代码：

- `flatten_arguments`（[L1197-1315](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L1197-L1315)）：slice 展平成 `(ptr, len)`、struct 按**内存顺序**展平成各字段、ZST 跳过。
- `coerce_arg_to_param_ty`（[L1334-1373](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L1334-L1373)）：当实参指针地址空间与被调用者声明不一致时，插一条 `llvm.addrspacecast` 桥接（双向：generic↔shared、shared↔global）。这就是「调用一个形参为 `ptr addrspace(3)` 的 block_reduce，但实参来自 `DynamicSharedArray::get` 的 `ptr addrspace(3)`」能成立的根因。
- `find_callee_arg_types`（[L1402-1456](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L1402-L1456)）：到父模块里找被调用者的声明（可能已是 `llvm::FuncOp`，可能还是 `MirFuncOp`），把它的 LLVM 级形参类型取出来，作为 `coerce_arg_to_param_ty` 的目标。

**device-extern 符号剥前缀**是一个易错点：`#[device] extern "C"` 的宏会把 `foo` 改名成 `cuda_oxide_device_extern_<hash>_foo` 供 collector 识别，但外部 LTOIR 导出的是原名 `foo`。`resolve_device_extern_symbol`（[L1471-1475](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/call.rs#L1471-L1475)）在 lowering 时剥掉前缀，让发出的 `llvm.call @foo` 能被 nvJitLink 解析到外部符号。这个「前缀字符串 + 剥名函数」的真相源在 `reserved-oxide-symbols` crate。

#### 4.4.4 代码实践

1. **实践目标**：理解「占位调用 → libdevice」的翻译时机。
2. **操作步骤**：在仓库里找一个用到 `f32::sin` 之类数学函数的示例，例如 `libdevice_math` 或 `libm_math`（位于 `crates/rustc-codegen-cuda/examples/`）。用 `cargo oxide pipeline libm_math` 生成 `.ll`，搜索 `__nv_`。
3. **需要观察的现象**：`.ll` 里会出现一条 `declare ... @__nv_sinf(...)` 的声明和对应的 `call ... @__nv_sinf(...)`，但**没有** `__nv_sinf` 的定义体。
4. **预期结果**：你能在 `.ll` 里定位到「声明在这里、定义在外部 libdevice」的证据，对应 u4-l5 讲的 NVVM/LTOIR 链路。如果本机没有可用的工具链 dump，这一步标注「待本地验证」，改为直接阅读 `libdevice_name` 表与 `convert_rust_float_math_intrinsic` 验证映射。

#### 4.4.5 小练习与答案

**练习**：`core::intrinsics::fadd_fast` 和普通 `a + b`（`a,b: f32`）都会变成 `llvm.fadd`，它们的 fast-math 标志有什么不同？

> 参考答案：普通 `+` 经 `add_fastmath_flags` 只挂 `contract`（或禁用 FMA 时为空）；`fadd_fast` 经 `fast_float_intrinsic_flags` 挂全套 `FAST`（nnan/ninf/nsz/reassoc/contract/arcp），仅在禁用 FMA 时从中抠掉 `contract`。前者保持 IEEE-754 逐位语义，后者承诺「快」语义、允许更激进优化。

### 4.5 atomic / wmma intrinsic lowering（含本轮新增 mma）

#### 4.5.1 概念说明

GPU 专用 intrinsic 走两条 lowering 策略（见 [`intrinsics/mod.rs` 顶部文档](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/mod.rs#L1-L71)）：

1. **LLVM intrinsic 调用**：有直接 NVVM intrinsic 对应的（如线程号 `llvm.nvvm.read.ptx.sreg.tid.x`），用 `call_intrinsic` 声明并调用。LLVM 可优化、调试信息更好。
2. **内联 PTX 汇编**：没有现成 intrinsic 或需要精确控制 PTX 编码的（如 mma、wgmma、tcgen05），用 `llvm.inlineasm`。其中 **warp 同步类**必须带 `convergent` 属性，防止 LLVM 把它提到发散控制流之外；**每线程独立类**（如 cp.async、原子加）只带 `sideeffect`。

这两条路共用 `common.rs` 里的几个工具，是本节的「基础设施」。

#### 4.5.2 核心流程

原子 op 是少数「lowering 到标准 LLVM IR 指令而非 intrinsic/inlineasm」的 op：

```text
NvvmAtomicRmwOp
  ├─ map_scope   : Device/Block/System → "device"/"block"/默认(.sys)
  ├─ map_ordering: Relaxed→Monotonic, Acquire→Acquire, ...
  └─ [可选前置 fence] + atomicrmw Monotonic + [可选后置 fence]   ← 栅栏拆分变通
```

wmma 的 mma op 则走「convergent inline PTX」的统一范式：

```text
NvvmMma...Op（10 个寄存器操作数：C[0..4], A[0..4], B[0..2]）
  ├─ 建 LLVM struct 结果类型 {f32,f32,f32,f32}（或 {i32}×4）
  ├─ inline_asm_convergent(模板, 约束串)   ← 一条 mma.sync.aligned... 指令
  └─ 用 ExtractValue 把 struct 拆回 4 个 SSA 结果，替换原 op 的 4 个结果
```

#### 4.5.3 源码精读

**共用工具**（[crates/mir-lower/src/convert/intrinsics/common.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/common.rs)）：

- [`call_intrinsic`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/common.rs#L128-L146)（L128-146）：`ensure_intrinsic_declared` + 建 `llvm.call`，是策略 1 的标准动作。
- [`inline_asm_convergent`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/common.rs#L149-L167)（L149-167）：建带 `convergent` 的 `llvm.inlineasm`。
- [`inline_asm_sideeffect`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/common.rs#L175-L193)（L175-193）：同上但只 `sideeffect`。
- [`cast_to_shared_addrspace`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/common.rs#L79-L99)（L79-99）：必要时插 `addrspacecast` 到 addrspace(3)。

**原子 op**（[crates/mir-lower/src/convert/intrinsics/atomic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs)）：

- 作用域/序映射 [`map_scope`/`map_ordering`/`map_rmw_kind`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs#L73-L105)（L73-105）。注意 `Relaxed` 映射成 LLVM 的 `Monotonic`（这是 LLVM 的习惯叫法）。
- **`atomicrmw` 栅栏拆分变通**：[`convert_atomic_rmw`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs#L177-L233)（L177-233）。因为 LLVM 的 NVPTX 后端在 `atomicrmw` 上会**静默丢弃** ordering（要等 LLVM 23 的 PR #176015 修），cuda-oxide 暂时把 `atomicrmw` 本身固定成 `Monotonic`，用前后 `fence` 补回 Acquire/Release/SeqCst 语义。文件顶部 L25-36 的表把这个变通的所有组合列得很清楚。

  ```text
  AcqRel: fence release + atomicrmw monotonic + fence acquire
  SeqCst: fence seq_cst  + atomicrmw monotonic + fence seq_cst
  ```

- **打包原子加**（f16x2/bf16x2）：[`convert_packed_atom_add`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs#L288-L319)（L288-319）发出 `atom.global.add.noftz.<type>` 内联 PTX，用 `sideeffect`（每线程独立，非 warp 同步）。

**wmma / mma op**（[crates/mir-lower/src/convert/intrinsics/wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs)）：所有 mma 共用一套结构——校验操作数个数（通常 10 个：C[0..4], A[0..4], B[0..2]）、建结果 struct、`inline_asm_convergent` 一条 `mma.sync.aligned...`、`ExtractValue` 拆回结果。本轮 #327/#328/#329 新增的三条与既有的 bf16/f64 同族：

- f16 `m16n8k16`：[`convert_mma_m16n8k16_f32_f16`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs#L113-L156)（L113-156），模板 `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32`，约束 `=f,=f,=f,=f,f,f,f,f,r,r,r,r,r,r`。
- tf32 `m16n8k8`：[`convert_mma_m16n8k8_f32_tf32`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs#L163-L206)（L163-206），模板 `...m16n8k8.row.col.f32.tf32.tf32.f32`，A/B 操作数以打包 `i32`（`r` 约束）传入。
- **s8 `m16n8k32`**：[`convert_mma_m16n8k32_s32_s8`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs#L213-L256)（L213-256）。它是三条里最「整型」的：结果与所有操作数都用 `r`（整数寄存器）约束，结果是 `{i32, i32, i32, i32}`，模板 `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32`。

> 三条 mma 的约束串差异折射出「操作数寄存器类型」：f16/tf32 的累加器 C/D 是 `f32`（用 `f` 约束），A/B 是打包的 `i32`（用 `r`）；s8 全程 `i32`（全 `r`）。这与你手算 fragment 的 lane→element 映射是一致的（详见 u5-l6）。

op → converter 的接线在 [`interface_impls.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs)：每条 mma 的 `#[op_interface_impl]` 把 dialect-nvvm 的 op 类型（如 `MmaM16N8K32S32S8Op`）连到对应 `convert_mma_*` 函数。s8 这条见 [`interface_impls.rs:2779-2794`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/interface_impls.rs#L2779-L2794)。

#### 4.5.4 代码实践（本讲主实践的第二部分）

1. **实践目标**：把一条 s8 `m16n8k32` mma 从「设备 API 调用」一直追到「内联 PTX 模板」。
2. **操作步骤**（纯源码追踪，跨四个 crate）：
   - **设备层**：在 `crates/cuda-device/src/wmma.rs` 找到 s8 mma 的 `unsafe fn` 桩（`#[inline(never)]` + `unreachable!()`，靠名字被识别）。
   - **importer 层**：在 `crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs` 看它如何把这个调用翻成 `dialect-nvvm` 的 `MmaM16N8K32S32S8Op`（u6-l2 讲过 fragment 拆装三段范式）。
   - **派发层**：在 `crates/mir-lower/src/convert/interface_impls.rs:2779-2794` 看 `MmaM16N8K32S32S8Op` 如何实现 `MirToLlvmConversion` 并指向 `convert_mma_m16n8k32_s32_s8`。
   - **lowering 层**：在 `crates/mir-lower/src/convert/intrinsics/wmma.rs:213-256` 看它发出 `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32` 的 convergent inline PTX。
3. **需要观察的现象**：四层改动缺一不可；op 的 10 个操作数按 `C[0..4], A[0..4], B[0..2]` 顺序在模板里被重新排成 `{$0..$3}`(D)、`{$8..$11}`(A)、`{$12,$13}`(B)、`{$4..$7}`(C 的输入)——注意 PTX 的 D/C 与 SSA 操作数顺序不一致，靠约束串里的位置重排实现。
4. **预期结果**：你能画出一条从 Rust 调用到 PTX 文本的完整链路，并指出「真正生成 PTX 的就只有 `wmma.rs` 这一个函数」。这也是 u6-l4「新增 intrinsic 全栈模板」要复用的模式。
5. 若想看实际 PTX：用一个含 s8 mma 的示例（参考 `crates/rustc-codegen-cuda/examples/` 下与 mma 相关的示例，注意需要 sm_80+ 与 `llc-21+`），`cargo oxide pipeline <name>` 后在 `.ptx` 中搜索 `mma.sync.aligned.m16n8k32`。是否本机可运行**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `mma.sync` 的 inlineasm 必须用 `convergent`，而 `atom.global.add` 用 `sideeffect`？

> 参考答案：`mma.sync` 是 **warp 同步**指令——32 个 lane 必须锁步执行同一条 mma，若 LLVM 把它提到发散控制流之外或在某 lane 上被省略，warp 协作就会破坏（甚至死锁）。`convergent` 正是禁止这类移动。`atom.global.add` 是**每线程独立**的内存操作，没有 warp 协作约束，只需 `sideeffect` 保证它不被当作纯计算删除。

**练习 2**：`atomicrmw` 的栅栏拆分变通里，为什么 `atomicrmw` 本身固定成 `Monotonic`，而不是直接用请求的 ordering？

> 参考答案：LLVM 的 NVPTX 后端（在 LLVM 23 之前）会静默丢弃 `atomicrmw` 上的 ordering，直接用反而拿不到 Acquire/Release/SeqCst 语义。变通做法是让 `atomicrmw` 只做「原子读改写」（`Monotonic` 足矣），由前后 `fence` 携带真正的 ordering 与 syncscope 来补语义。

## 5. 综合实践

把本讲两条主线（FMA 收缩 + intrinsic lowering）串成一个对照任务：

**任务**：在一个核函数里同时写一段「浮点乘加」和一次「warp 归约」，用 `cargo oxide pipeline` 把它们各自的 lowering 产物都拿出来，对照本讲讲过的两条策略。

1. 写一个核函数：先用 `a[i]*b[i] + c[i]` 做一次乘加（触发 FMA 收缩判断），再用 `shuffle_xor` 做一个 warp 内求和（触发 intrinsic lowering）。
2. `cargo oxide pipeline <name>` 生成 `.ll`：
   - 在乘加部分，定位 `fmul`/`fadd`（或被 `llc` 融合后的 `fma`）；记录它们是否带 `contract`。
   - 在归约部分，定位代表 shuffle 的 `llvm.inlineasm`（convergent）或 `llvm.nvvm.shfl.*` intrinsic 调用。
3. 再跑一次 `CUDA_OXIDE_NO_FMA=1 cargo oxide pipeline <name>`，确认只有乘加部分的 fast-math 标志发生变化，shuffle 部分完全不变。
4. 用一句话总结：**FMA 收缩是「IR 指令属性」级的策略（只改 fast-math 标志），intrinsic lowering 是「整条指令替换」级的策略（换成 inlineasm/intrinsic call）**——两者层次不同，互不影响。

> 本任务无需 GPU 即可完成 `.ll` 层的对照；`.ptx` 层是否真正融合 FMA、shuffle 是否被后端识别，待本地验证。

## 6. 本讲小结

- `mir-lower` 的派发骨架是 pliron `DialectConversion`：driver 在 `rewrite` 里把「需要跨函数状态」的少数 op（Func/SharedAlloc/GlobalAlloc/ExternShared）特判分流，其余 op 走 `op_cast` O(1) 虚表派发到各自的 `MirToLlvmConversion`。
- 算术 op 的核心难点是**符号性恢复**：LLVM 整数 signless，`div/rem/shr/cmp/checked_*` 必须用 `operands_info` 查回转换前的 MIR 类型才能在 `sdiv/udiv`、`ashr/lshr`、`slt/ult` 之间正确选择。
- **FMA 收缩契约**：浮点 `add/sub/mul/div/rem` 经 `add_fastmath_flags` **只挂 `contract`** 一个 fast-math 标志，由 `LoweringOptions::allow_fma_contraction`（默认 true，受 `CUDA_OXIDE_NO_FMA` 控制）开关；绝不挂其他标志以保持逐位可比。`f*_fast` 子族另挂全套 `fast`，仅在禁用 FMA 时抠掉 `contract`。
- `mir.call` 是一个**漏斗**：依次拦截 rust bit/saturating/bigint/float-math 占位调用，分别落到 LLVM intrinsic、`__nv_*` libdevice 调用或 `f*_fast` 的 LLVM 浮点 binop；其余才是普通调用，经 `flatten_arguments`（slice/struct 展平）+ `coerce_arg_to_param_ty`（addrspace 桥接）发出 `llvm.call`，并对 device-extern 剥前缀。
- GPU intrinsic 走两条路：`call_intrinsic`（LLVM/NVVM intrinsic 调用）或 `inline_asm_convergent`/`inline_asm_sideeffect`（内联 PTX）。原子是少数直接 lower 成标准 LLVM 原子指令的 op，且因 NVPTX 后端 bug 用 `atomicrmw Monotonic + 前后 fence` 的栅栏拆分变通。
- 本轮 #327/#328/#329 新增的 f16 `m16n8k16`、tf32 `m16n8k8`、s8 `m16n8k32` 三条 mma 与既有 bf16/f64 同族，统一走「建 struct 结果 → convergent inline PTX → ExtractValue 拆回」范式，差别只在约束串（`f` vs `r`）与模板里的 dtype/m/n/k。

## 7. 下一步学习建议

- **横向补全 ops lowering**：本讲聚焦算术/call/memory/intrinsic，还有 `convert/ops/` 下的 `cast.rs`、`aggregate.rs`、`control_flow.rs`、`constants.rs` 没展开。建议按本讲的「三段式 + 符号性/地址空间陷阱」的读法，自己各挑一个 op 走一遍。
- **纵向追函数 lowering**：`lowering.rs::convert_func` 是本讲多次提到但没深读的「最复杂转换器」——负责签名展平、kernel 属性传播、入口块参数重建、动态共享内存对齐合并。它是 u4-l4 提到的「最复杂的转换器」，建议作为下一篇深潜的对象。
- **照模板加一条 intrinsic**：以本讲的 s8 mma 链路为模板，结合 u6-l4 的「新增 intrinsic 全栈清单」，挑一个 PTX 已有但 cuda-oxide 未封装的指令（如某 reduce/scan op），按「设备层 → dialect op → importer 翻译 → importer 分派 → lowering」四层落地，把本讲的知识变成一次可提交的改动。
- **回到运行时**：FMA 契约与 libdevice 调用最终都要在 cubin 阶段生效，建议结合 u4-l5 复习「libNVVM/nvJitLink 如何消费这里发出的 `__nv_*` 与 FMA 策略」，把编译期与运行时串成一条完整链路。
