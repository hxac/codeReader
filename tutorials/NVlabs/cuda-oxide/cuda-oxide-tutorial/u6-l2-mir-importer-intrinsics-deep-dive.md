# mir-importer 深潜：terminator/intrinsics 翻译机

## 1. 本讲目标

本讲是编译器深潜单元（U6）的第二篇，承接 u4-l2（mir-importer 鸟瞰）与 u6-l1（device codegen 深潜）。在 u4-l2 中我们建立了 mir-importer 的「翻译留在 mir-importer、后段委托 cuda-oxide-codegen」这条边界；本讲要把放大镜对准这条边界上最热闹的一处——**终止符（terminator）翻译**，尤其是其中最复杂的一类：**对 `cuda_device` GPU intrinsic 调用的分派与翻译**。

学完本讲，你应当能够：

1. 说清楚一个 MIR `TerminatorKind::Call` 进入 `translate_call` 后，是按什么顺序、经过哪些「关卡」被识别为 intrinsic、闭包调用、还是普通函数调用的。
2. 画出 intrinsic 翻译的「分模块目录」结构，并能据此为一个新的 GPU 指令找到它该落的文件。
3. 复述 atomic intrinsic 两条前端（`cuda_device::atomic::*` 与 `core::sync::atomic::*`）如何共用同一套 NVVM op 发射逻辑，以及作用域（scope）/内存序（ordering）分别在哪里被确定。
4. 对照本轮（#327/#328/#329）新增的 f16 `m16n8k16`、tf32 `m16n8k8`、s8 `m16n8k32` 三条 `mma.sync` 翻译，说明「fragment 数组拆装成标量寄存器 → dialect-nvvm op → 重新装回数组」这套统一范式。
5. 独立追踪一条 `mma_m16n8k16_f32_f16` 调用从设备桩函数到最终 dialect-nvvm op 的完整调用链。

## 2. 前置知识

阅读本讲前，建议你先建立以下直觉（若已读过 u4-l2/u6-l1 可跳过）：

- **MIR 的基本块与终止符**：rustc 把函数体降级成 MIR 后，每个基本块由若干「语句（statement）」加一个「终止符（terminator）」组成。终止符负责控制流转移：`Return`、`Goto`、`SwitchInt`、`Assert`、`Drop`、`Unreachable`，以及本讲的主角 `Call`。语句改写局部变量，终止符决定下一步去哪个块。
- **Pliron / dialect-mir / dialect-nvvm**：cuda-oxide 用 Pliron（一个类 MLIR 的多方言 IR 框架）承载中间表示。`dialect-mir`（op 前缀 `mir.`）贴近 Rust 语义，由 mir-importer 从 rustc MIR 一对一翻译而来；`dialect-nvvm`（op 前缀 `nvvm.`）贴近 PTX/NVVM 指令。详见 u4-l3。
- **alloca + load/store 模型**：mir-importer 给每个非 ZST 的 MIR 局部分配一个栈槽（`mir.alloca`），写即 `mir.store`、读即 `mir.load`，跨块数据流走槽位而非块参数；SSA 提升留给后段的 mem2reg。这条约定决定了本讲里所有 intrinsic handler 的「写回结果」套路。
- **设备桩函数（stub）**：`cuda-device` 里所有 GPU intrinsic（`threadIdx_x`、`mma_*`、`atomic`…）在 Rust 层都是 `#[inline(never)]` + `unreachable!()` 的占位函数。它们在 CPU 上绝不可调用；真正生效是因为 mir-importer 在翻译期**拦截了这些调用**、改写成 dialect op，桩函数体永远不会被翻译进 PTX。这是 cuda-oxide「编译器识别桩」模式的核心。
- **fragment（片段）**：`mma.sync` 这类张量核指令由 32 个线程（一个 warp）协作完成一条矩阵乘，操作数不是单个标量，而是按硬件规定分布在线程寄存器里的「片段」。在 Rust 侧片段用定长数组表示（如 `[f32; 4]`、`[u32; 4]`），翻译时需先拆成标量寄存器喂给 op、再把结果寄存器装回数组。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为只读引用，不会修改）：

| 文件 | 作用 |
|------|------|
| `crates/mir-importer/src/translator/terminator/mod.rs` | 终止符翻译的总入口 `translate_terminator`、调用翻译 `translate_call`、intrinsic 总分派器 `try_dispatch_intrinsic` |
| `crates/mir-importer/src/translator/terminator/helpers.rs` | 共用辅助函数：`emit_goto`、`emit_store_result_and_goto`（写回结果 + 跳转）、`emit_function_call`、`emit_nvvm_intrinsic` |
| `crates/mir-importer/src/translator/terminator/intrinsics/mod.rs` | intrinsic 子模块的目录与文档表，声明 21 个按类别划分的子模块 |
| `crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs` | 原子操作翻译：`cuda_device::atomic::*` 与 `core::sync::atomic::*` 两条前端 |
| `crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs` | warp 级矩阵乘 `mma.sync` 翻译，含本轮新增的 f16/tf32/s8 |
| `crates/mir-importer/src/translator/body.rs` | 函数体翻译 `translate_body`，是 `translate_terminator` 的调用者，负责 alloca/属性/块创建 |
| `crates/dialect-nvvm/src/ops/wmma.rs` | dialect-nvvm 侧的 mma op 定义（操作数/结果计数与校验） |
| `crates/cuda-device/src/wmma.rs` | 设备侧 `mma_*` 桩函数，是 intrinsic 调用的 Rust 源头 |

## 4. 核心概念与源码讲解

### 4.1 terminator 翻译分派：从 `Call` 到 intrinsic handler

#### 4.1.1 概念说明

mir-importer 把一个 MIR 函数体翻译成一个 `mir.func` 操作，函数体里的每个基本块由 `block::translate_block` 处理，块内语句走 `rvalue`/`statement`，块的终止符则统一交给 `translate_terminator`。终止符种类有限，但其中 `Call` 是最复杂的：它既要区分「调用的是 GPU intrinsic」「调用的是闭包 trait 方法」「调用的是普通函数」，还要在 intrinsic 这一类里进一步分门别类。

整个分派可以理解成一个**漏斗**：顶层按 `TerminatorKind` 分流，`Call` 这一支再按「优先级」一道道过筛——先处理那些需要从函数泛型里抠常量参数的特殊 intrinsic（如 `prof_trigger`、`__unroll_config`、`DynamicSharedArray`、`core::sync::atomic`），再交给通用的 `try_dispatch_intrinsic` 大 `match`，最后剩下的才是普通 `mir.call`。

#### 4.1.2 核心流程

`translate_terminator` 的顶层分流是一个对 `term.kind` 的 `match`：

```text
TerminatorKind::Return    → translate_return
TerminatorKind::Goto      → translate_goto          （零操作数 mir.goto）
TerminatorKind::Assert    → translate_assert        （条件取反 + mir.assert）
TerminatorKind::Call      → translate_call          ← 本讲主角
TerminatorKind::SwitchInt → translate_switch        （cond_branch 链）
TerminatorKind::Drop      → translate_drop          （noop glue 判定，否则硬错误）
TerminatorKind::Unreachable → mir.unreachable
其他                       → 「not yet implemented」硬错误
```

而 `translate_call` 内部对一次 `Call` 的处理顺序大致是：

```text
1. extract_func_info(func)
   → 得到 (pattern_name, call_name, substs_str)
2. 跳过 precondition_check（死分支里的 UB 检查）
3. callable_trait_call_info 命中 Fn/FnMut/FnOnce？
   → receiver 是闭包?  translate_closure_call
   → receiver 是函数项? translate_function_item_call
4. 特殊常量泛型 intrinsic（需从 func 泛型里抠 const）：
   - prof_trigger          （抠 const N 当 event_id）
   - __unroll_config       （抠 const FACTOR，插 mir.unroll_hint）
   - DynamicSharedArray::* （抠 const ALIGN）
   - core/std::intrinsics::atomic_* （抠 const AtomicOrdering）
   - assert_inhabited      （按 layout 判 inhabited/uninhabited）
5. try_dispatch_intrinsic(name, ...)   ← 通用 intrinsic 大分派
6. target_usize.is_none()?  → 发散调用（panic/unwrap_failed）→ mir.unreachable
7. 未识别的 rustc intrinsic / libm → 在此硬报错（issue #137，避免后段才报「Symbol not found」）
8. 否则 → helpers::emit_function_call （普通 mir.call）
```

这个顺序至关重要：**步骤 4 里那几个「特殊常量泛型 intrinsic」之所以要在 `try_dispatch_intrinsic` 之前单独处理，是因为它们的语义参数（unroll 因子、对齐字节数、内存序、event id）编码在 callee 的 const generic 里，而不是在 `args` 里**，必须从 `func` 这个 `mir::Operand::Constant` 里解析出来。

#### 4.1.3 源码精读

`translate_terminator` 的顶层 `match`，对 `Call` 调用 `translate_call` 并透传全部上下文（块指针、上一条 op、值映射表、块映射表、合法化器）：

[crates/mir-importer/src/translator/terminator/mod.rs:108-161](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L108-L161) —— 入口签名与 `TerminatorKind::Call` 分支：把 `func`/`args`/`destination`/`target`/`unwind` 一并交给 `translate_call`。

`translate_call` 开头先用 `extract_func_info` 把 callee 解析成三个名字：`pattern_name`（FQDN，用于 intrinsic 模式匹配，如 `cuda_device::wmma::mma_m16n8k16_f32_f16`）、`call_name`（普通调用时用的符号名）、`substs_str`（泛型调试串，用于 `substs_contains` 判定）：

[crates/mir-importer/src/translator/terminator/mod.rs:890](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L890) —— `let (pattern_name, call_name, substs_str) = extract_func_info(func);`

随后是「特殊常量泛型 intrinsic」拦截点之一——core 原子。注意它**先于** `try_dispatch_intrinsic`，因为内存序是 const generic，需要从 `func` 里解析：

[crates/mir-importer/src/translator/terminator/mod.rs:1118-1135](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L1118-L1135) —— `is_core_atomic_intrinsic(name)` 命中则转交 `intrinsics::atomic::dispatch_core_intrinsic`，把 `func`（而非 `args`）一并传入以便抠泛型。

接下来才是通用 intrinsic 总分派。`try_dispatch_intrinsic` 返回 `Option<Ptr<Operation>>`：`Some` 表示命中并已发射 op，`None` 表示「不是 intrinsic，继续往下当普通调用处理」：

[crates/mir-importer/src/translator/terminator/mod.rs:1193-1210](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L1193-L1210) —— 调用 `try_dispatch_intrinsic`，命中即 `return`。

`try_dispatch_intrinsic` 的结构是「先一堆 `if let` 前置分类器，再一个超大 `match name`」：

[crates/mir-importer/src/translator/terminator/mod.rs:2136-2164](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L2136-L2164) —— 总分派器签名，以及最先处理的「内联 PTX」分类器 `InlinePtxCallKind::from_path(name)`。

这个大 `match` 的兜底分支是「不是 intrinsic」，返回 `Ok(None)`，把控制权还给 `translate_call` 走普通函数调用路径：

[crates/mir-importer/src/translator/terminator/mod.rs:4614-4616](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L4614-L4616) —— `_ => Ok(None)`，分派结束。

#### 4.1.4 代码实践

**实践目标**：在源码里走一遍 `translate_call` 的漏斗，定位「同一类调用会被哪一道筛子接住」。

**操作步骤**：

1. 打开 `crates/mir-importer/src/translator/terminator/mod.rs`，定位 `fn translate_call`（约 868 行）。
2. 顺着函数体往下，分别找到这几道筛子的代码位置：`precondition_check`（约 899 行）、`callable_trait_call_info`（约 942 行）、`prof_trigger`（约 981 行）、`__unroll_config`（约 1026 行）、`DynamicSharedArray`（约 1053 行）、`is_core_atomic_intrinsic`（约 1118 行）、`try_dispatch_intrinsic`（约 1193 行）。
3. 注意它们的相对顺序：凡是要从 `func` 泛型里抠 const 的，都在 `try_dispatch_intrinsic` **之前**。

**需要观察的现象**：步骤 2 中那几道「特殊常量泛型」筛子如果被挪到 `try_dispatch_intrinsic` 之后会发生什么？——`try_dispatch_intrinsic` 的 `match` 末尾兜底返回 `Ok(None)`，那么这些调用会落入普通 `mir.call`，最终因「符号未找到」在后段校验失败。

**预期结果**：你能用一句话说出「为什么 atomic/ordering/unroll 这类参数必须从 `func` 而非 `args` 里取」——因为它们在 Rust 层是 const generic，rustc 把它们编进 callee 的类型，而不是运行期实参。**待本地验证**：可在 `translate_call` 里临时给某条筛子加一行 `eprintln!("hit: {name}")`，编译一个用到该 intrinsic 的示例观察打印顺序。

#### 4.1.5 小练习与答案

**练习 1**：`translate_call` 里对 `core::intrinsics::assert_inhabited` 的处理为什么也要单独前置，而不是进 `try_dispatch_intrinsic` 的 `match`？

**参考答案**：因为它的行为依赖被单态化的类型 `T` 的 layout（`VariantsShape::Empty` 判定 inhabited/uninhabited），需要从 `func` 的泛型里取出 `T` 并查询 `layout()`，这只能在拿到泛型参数后做编译期决策（inhabited → unit noop；uninhabited → `mir.unreachable`），不是简单的「名字 → op」映射。

**练习 2**：如果一个用户函数恰好起名叫 `cuda_device::thread::threadIdx_x`，会被误当成 intrinsic 吗？

**参考答案**：`pattern_name` 来自 `CrateDef::name()` 的 FQDN；要落到这条路径，该函数必须真的定义在 `cuda_device` crate 的 `thread` 模块里且符号被收集器认可。普通用户无法在自己的 crate 里伪造出 `cuda_device::` 前缀的 FQDN，因此这条匹配是可信的。

### 4.2 intrinsics 分模块：按功能类别的目录组织

#### 4.2.1 概念说明

`try_dispatch_intrinsic` 那个超大 `match` 看似把所有 intrinsic 揉在一个函数里，但实际发射逻辑早已按**功能类别**拆进 `terminator/intrinsics/` 下的 21 个子模块。每个子模块导出一组 `emit_*` 函数，签名高度一致：吃下 `ctx`/`body`/`args`/`destination`/`target`/`block_ptr`/`prev_op`/`value_map`/`block_map`/`loc` 这套翻译上下文，产出「写回结果 + 跳转」后的终止符 op。

模块文档里给出了一张类别总表（`indexing`/`sync`/`cluster`/`warp`/`wgmma`/`tcgen05`/`tma`/`memory`/`debug` 等），是「给一个新的 GPU 指令找落点」的索引页。值得注意的是文档里的一句坦诚说明：**目前多数 `emit_*` 仍物理地留在 `terminator/mod.rs` 以保证编译稳定，子模块结构是为渐进迁移准备的**——也就是说目录划分是「目标态」，迁移是「进行时」。

#### 4.2.2 核心流程

一个 intrinsic handler 的标准形态（以 `emit_*` 命名）做四件事，与 `intrinsics/mod.rs` 文档总结完全对应：

```text
1. 校验 args 数量/类型（不符合 → input_err! 硬报错）
2. 用 rvalue::translate_operand 把 MIR 操作数翻译成 pliron IR Value
3. new 一个 dialect-nvvm（或 dialect-mir）op，插入到 prev_op 之后
4. 用 emit_store_result_and_goto 写回 destination 槽位 + 跳转 target
```

第 4 步是所有 handler 共用的「尾声」，封装在 `helpers::emit_store_result_and_goto`：它把结果 Value 存进 `destination.local` 的栈槽（ZST 无槽则跳过），再发一条零操作数 `mir.goto` 到成功目标块。

#### 4.2.3 源码精读

`intrinsics/mod.rs` 的目录表与子模块声明，是本模块的「地图」：

[crates/mir-importer/src/translator/terminator/intrinsics/mod.rs:6-35](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/mod.rs#L6-L35) —— 类别总表与「Architecture」说明：每个 intrinsic 模块导出的 `emit_*` 都遵循「翻译操作数 → 建 op → 存结果 → 发零操作数 `mir.goto`」四步。

[crates/mir-importer/src/translator/terminator/intrinsics/mod.rs:37-59](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/mod.rs#L37-L59) —— 21 个子模块声明（`asm`/`atomic`/`bf16x2`/…/`wmma`），构成 intrinsic 翻译的目录树。

共用「尾声」`emit_store_result_and_goto`：注意它如何兼容 ZST（`store_local` 返回 `None` 时直接用 `prev_op` 接 goto），以及 target 缺失时如何用传入的 `no_target_msg` 报错：

[crates/mir-importer/src/translator/terminator/helpers.rs:65-95](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/helpers.rs#L65-L95) —— `emit_store_result_and_goto`：先 `store_local` 写回，再 `emit_goto` 跳转，是几乎所有 intrinsic handler 的统一收尾。

`emit_goto` 本身：因为非入口块无参数（跨块数据流走 alloca 槽位），所以 `mir.goto` 是零操作数、只带一个后继块：

[crates/mir-importer/src/translator/terminator/helpers.rs:36-55](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/helpers.rs#L36-L55) —— `emit_goto`：构造零操作数 `MirGotoOp` 并插到 `prev_op` 之后。

#### 4.2.4 代码实践

**实践目标**：用 `intrinsics/mod.rs` 的类别表做一次「反向检索」，验证目录划分与代码现实一致。

**操作步骤**：

1. 打开 `crates/mir-importer/src/translator/terminator/intrinsics/mod.rs`，记下 21 个子模块名。
2. 用检索工具（如 `rg "pub fn emit_" crates/mir-importer/src/translator/terminator/intrinsics/`）统计每个子模块实际导出了多少个 `emit_*`。
3. 再到 `terminator/mod.rs` 里检索 `intrinsics::<sub>::emit_`，看哪些子模块的函数已经被 `try_dispatch_intrinsic` 调用、哪些还「只在 mod.rs 里」（对应文档说的「迁移进行时」）。

**需要观察的现象**：`wmma`、`atomic` 这类已经迁入子模块；而像 `index_1d` 这类简单 intrinsic 的发射逻辑可能仍以 `helpers::emit_nvvm_intrinsic` 的形式留在 mod.rs。

**预期结果**：你会得到一张「子模块 → 已迁移 emit 数 / 仍在 mod.rs 的调用数」的对照表，理解「目录是目标态、迁移是渐进」这句话的具体含义。**待本地验证**：检索命令的结果依赖当前 HEAD，行号可能随提交漂移。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `emit_store_result_and_goto` 要把「写回结果」和「跳转」合在一个 helper 里，而不是让每个 handler 自己写两行？

**参考答案**：因为这两步对所有「有返回值、有成功 target」的 intrinsic 完全一致，且必须保持顺序（先 store 再 goto，否则后继块 load 不到值）；统一封装既避免拷贝粘贴出错，也保证 ZST（无槽位）与 target 缺失（发散调用）两类边界被一致处理。

**练习 2**：`emit_nvvm_intrinsic`（helpers.rs）与 `intrinsics::wmma::emit_*` 在「建 op」这步有什么本质区别？

**参考答案**：前者是「零操作数、单结果」的简单特殊寄存器读取（如 `threadIdx.x`），只需传入 op 类型构造一个无操作数 op；后者要把数组片段拆成多个标量寄存器作为多操作数 op 的输入，并接收多个结果寄存器再装回数组，复杂度高一个量级。

### 4.3 atomic intrinsic 翻译：两条前端、统一 NVVM op

#### 4.3.1 概念说明

`intrinsics/atomic.rs` 是「分模块」思想的一个范本：它**同时服务两条前端**，却最终落到同一批 NVVM atomic op。

- **前端 A：`cuda_device::atomic::*`**。这是 cuda-oxide 自定义的原子类型，把**作用域（scope）编进类型名前缀**：`DeviceAtomic*` → `.gpu`、`BlockAtomic*` → `.cta`、`SystemAtomic*` → `.sys`；元素类型编进后缀（`U32`/`I64`/`F32`…）。内存序作为**运行期枚举参数**传入。
- **前端 B：`core::sync::atomic::*`**。标准库原子在 rustc 侧会被编译成 `std::intrinsics::atomic_*`（或 `no_std` 下 `core::intrinsics::atomic_*`），其内存序是 **const generic**（不是运行期参数），且作用域**固定为 system（`.sys`）**，以保证 host-device 一致性，与 CUDA C++ `cuda::atomic<T>` 默认行为对齐。

两条前端的关键差异在「参数从哪来」：A 从类型名解析作用域、从 `args` 取 ordering；B 从 callee 泛型解析 ordering、作用域硬编码 System。但二者最终都构造同一个 `NvvmAtomicRmwOp`/`NvvmAtomicLoadOp`/`NvvmAtomicStoreOp`/`NvvmAtomicCmpxchgOp`，共享下游整条 lowering 链。

#### 4.3.2 核心流程

前端 A 的解析（`cuda_device` 路径）：

```text
call path = "cuda_device::atomic::BlockAtomicI64::fetch_add"
  ↓ parse_atomic_path（按 :: 反向切分）
  method = "fetch_add"
  type_name = "BlockAtomicI64"
  ↓ parse_atomic_type_name
  scope = Block(.cta), bit_width=64, is_signed=true, is_float=false
  ↓ method_to_rmw_kind("fetch_add", info)
  AtomicRmwKind::Add
  ↓ emit_atomic_rmw（从 args[2] 取 ordering）
  NvvmAtomicRmwOp{ptr, val, Add, ordering, Block}
```

前端 B 的解析（`core::sync::atomic` 路径），在 `translate_call` 里被 `is_core_atomic_intrinsic` 提前接住，从 callee 泛型抠出 (类型, ordering)：

```text
call = std::intrinsics::atomic_xadd::<u32, u32, AtomicOrdering::Relaxed>(ptr, val)
  ↓ dispatch_core_intrinsic
  ↓ extract_core_intrinsic_generics(func)
  type_info = U32 (从第 1 个泛型类型), scope = System (硬编码)
  ordering  = Relaxed (从第 3 个泛型 const，注意 std 的判别值表与 cuda_device 不同！)
  ↓ emit_core_atomic_rmw（注意：args 只有 [ptr, val]，没有 ordering 实参）
  NvvmAtomicRmwOp{ptr, val, Add, Relaxed, System}
```

两条路径在「构造 `NvvmAtomicRmwOp`」这一步合流。注意浮点 `fetch_sub` 的特殊处理：PTX 没有浮点原子减，于是翻译成「对相反数做 `FAdd`」，在发射前插一个 `MirNegOp`。

#### 4.3.3 源码精读

`AtomicTypeInfo` 是「类型名解析结果」的载体，把作用域、位宽、是否有符号/是否浮点打包：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:94-122](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L94-L122) —— `AtomicTypeInfo` 与 `element_type`：注意 f16 用 dialect-mir 自有的 `mir.fp16`，f32/f64 复用 pliron 内建浮点。

`parse_atomic_type_name` 把类型名拆成「作用域前缀 + 基类型后缀」，长前缀优先匹配（避免 `BlockAtomic` 被 `DeviceAtomic` 截断）：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:128-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L128-L157) —— `parse_atomic_type_name`：`BlockAtomic*`/`SystemAtomic*`/`DeviceAtomic*` → scope；`U32`/`I32`/`F32`… → 位宽与符号性。

`is_atomic_path` 是 `try_dispatch_intrinsic` 末尾那条 `path if is_atomic_path(path)` 守卫的判定函数；`dispatch` 是前端 A 的总入口，按 `method` 分流到 load/store/RMW/cmpxchg：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:263-350](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L263-L350) —— `dispatch`：解析 (type_info, method)，对 `fetch_*`/`swap` 经 `method_to_rmw_kind` 映射到 `AtomicRmwKind` 后调 `emit_atomic_rmw`。

`emit_atomic_rmw` 是前端 A 的 RMW 发射器，注意 `negate_value`（浮点 fetch_sub → 取负 + FAdd）这个变通：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:517-618](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L517-L618) —— `emit_atomic_rmw`：从 `args[2]` 取 ordering、`args[0]` 取 ptr、`args[1]` 取 val，按需取负，构造 `NvvmAtomicRmwOp::build(...)`，最后 `emit_store_result_and_goto`。

前端 B 的「内存序判别值表」与前端 A **不同**——这是极易踩的坑，源码用一张表显式对照（std 的 Release=1/Acquire=2，与 cuda_device 反过来）：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:739-759](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L739-L759) —— `intrinsic_ordering_from_discriminant`：std 与 cuda_device 的判别值在 Acquire/Release 上互换，必须分别映射。

`dispatch_core_intrinsic` 是前端 B 的总入口，从 `func` 泛型解析 (type_info, ordering) 后按 op 名分流，作用域恒为 `System`：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:891-978](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L891-L978) —— `dispatch_core_intrinsic`：`load`/`store`/`cxchg`/`cxchgweak`/RMW 分流；RMW 由 `intrinsic_op_to_rmw_kind` 映射（`xadd`→Add/FAdd、`umin`→UMin…）。

另外，文件还承载了「打包原子加」`atom_add_f16x2`/`atom_add_bf16x2` 这两个**独立函数**（不是原子类型的方法），用泛型 helper `emit_packed_atom_add::<O>` 复用发射逻辑：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:1357-1435](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L1357-L1435) —— `emit_packed_atom_add<O: Op>`：泛型 over op 类型，分别被 `emit_atom_add_f16x2`（`NvvmAtomAddF16x2Op`）与 `emit_atom_add_bf16x2`（`NvvmAtomAddBf16x2Op`）复用。

#### 4.3.4 代码实践

**实践目标**：亲手验证「两条前端、同一个 NVVM op」的合流，并理解 ordering 判别值差异。

**操作步骤**：

1. 在 `atomic.rs` 里读 `dispatch`（前端 A，约 263 行）与 `dispatch_core_intrinsic`（前端 B，约 891 行）。
2. 对比两者最终调用的发射函数：前端 A 的 `emit_atomic_rmw` 与前端 B 的 `emit_core_atomic_rmw`，确认它们都构造 `NvvmAtomicRmwOp::build(...)`，差别仅在「ordering 从哪来」「scope 是什么」。
3. 读 `intrinsic_ordering_from_discriminant`（约 750 行）与 `extract_ordering`（约 236 行），对照两张判别值表，找出 Acquire/Release 在哪条路径上被「互换」。
4. 阅读文末的 `tests` 模块（`packed_atomic_paths_match_only_the_public_stubs`），看负向用例如何拒绝近似路径。

**需要观察的现象**：如果有人把 std 路径的 ordering 误用 cuda_device 的判别值表，会发生什么？——`Relaxed` 仍对（都是 0），但 `Acquire`/`Release` 会被互换，导致内存序语义错乱（典型表现：本该 acquire 的读变成 release 语义的读）。

**预期结果**：你能口头复述「为什么 std 路径必须用独立的判别值表」——因为 `std::intrinsics::AtomicOrdering` 与 `cuda_device::atomic::AtomicOrdering` 是两个不同的 `#[repr(u8)]` 枚举，rustc 给它们分配的判别值恰好在 1/2 上相反。**待本地验证**：可写两个最小 kernel 分别用 `core::sync::atomic` 与 `cuda_device::atomic`，用 `cargo oxide pipeline` 看 IR 里 NVVM op 的 ordering/scope 属性差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cuda_device` 路径的 `fetch_sub` 对浮点类型要插一个 `MirNegOp`，而整型不需要？

**参考答案**：PTX 有整型 `atom.sub`，可直接对应 `AtomicRmwKind::Sub`；但 PTX 没有浮点 `atom.sub`，只有浮点 `atom.add`。于是浮点 `fetch_sub(x)` 被改写成 `fetch_add(-x)`：先对操作数取负（`MirNegOp`），再用 `AtomicRmwKind::FAdd` 发射，让 LLVM 复用原生浮点加原子指令。

**练习 2**：前端 B 为什么把作用域硬编码为 `System`，而不是像前端 A 那样从类型名解析？

**参考答案**：`core::sync::atomic` 的标准库语义面向 host-device 一致性，必须用最宽的 `.sys` 作用域才能保证「CPU 与 GPU、或不同 GPU 上下文之间」的可见性；这与 CUDA C++ `cuda::atomic<T>` 的默认一致。作用域信息不在 std 原子的类型里，所以无法解析、只能硬编码。

### 4.4 wmma intrinsic 翻译：fragment 拆装与新增 mma.sync（f16/tf32/s8）

#### 4.4.1 概念说明

`intrinsics/wmma.rs` 处理 warp 级矩阵乘 `mma.sync`（以及 `movmatrix`）。本模块是本轮（#327/#328/#329）改动的落点之一：在既有的 bf16 `m16n8k16`、f64 `m8n8k4` 基础上，**新增了 f16 `m16n8k16`、tf32 `m16n8k8`、s8 `m16n8k32` 三条 dtype**。

理解本模块的关键是 **fragment（片段）的「数组 ↔ 标量寄存器」转换**。硬件 `mma.sync` 指令不吃数组，它吃散落在 32 个线程寄存器里的若干 32 位标量；而 Rust 侧为了类型安全，把每个片段表达成定长数组（如 C 累加器 `[f32; 4]`、A 片段 `[u32; 4]`、B 片段 `[u32; 2]`）。因此翻译范式是固定的三段：

```text
1. extract_array_registers：把每个片段数组用 mir.extract_field 拆成 N 个标量 SSA 值
2. new 一个 dialect-nvvm mma op：吃进拆出来的全部标量寄存器，产出 M 个结果寄存器
3. mir.construct_array：把结果寄存器重新装回数组，作为整个调用的返回值
```

这套范式对 f16/bf16/tf32/s8/f64 五种 dtype **完全一致**，差异仅在「数组元素类型」「数组长度」「目标 op 类型」三处。这也是为什么本轮新增三条 dtype 时，每条都能照同一个模板加一个 `emit_mma_*` 函数。

#### 4.4.2 核心流程

以 `mma_m16n8k16_f32_f16(c: [f32;4], a: [u32;4], b: [u32;2]) -> [f32;4]` 为例，翻译流程：

```text
args[0]=c, args[1]=a, args[2]=b （各一个数组 Value）
  ↓ translate_operand
c_array, a_array, b_array
  ↓ extract_array_registers（按片段校验长度与元素类型）
c_regs: 4×f32, a_regs: 4×u32, b_regs: 2×u32
  ↓ operands = c_regs ++ a_regs ++ b_regs  （共 10 个操作数）
  ↓ new MmaM16N8K16F32F16Op(results=[f32;4], operands=10)
mma_op → 4 个 f32 结果寄存器
  ↓ MirConstructArrayOp([f32;4], d_regs)
d_array
  ↓ emit_store_result_and_goto（写回 destination + 跳转）
```

校验发生在 `extract_array_registers`：它用 `MirArrayType` 的 `size()` 与 `element_type()` 同时校验「长度对、元素类型对」。诊断信息刻意只报「expected N scalar registers」，**不点名别的 dtype**（测试 `mma_fragments_are_extracted_as_constant_index_ssa_values` 专门断言诊断里不含 `bf16/tf32/f16/s8`），避免误导用户以为片段可跨 dtype 复用。

#### 4.4.3 源码精读

模块顶部统一 import 五个 mma op 与 `movmatrix` op，对应 cuda-device 侧的五个桩：

[crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs:17-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L17-L20) —— `use dialect_nvvm::ops::{MmaM8N8K4F64Op, MmaM16N8K8F32Tf32Op, MmaM16N8K16F32Bf16Op, MmaM16N8K16F32F16Op, MmaM16N8K32S32S8Op, MovmatrixTransB16Op}`；本轮新增的三条（f16/tf32/s8）与既有 bf16/f64 同族。

`extract_array_registers` 是全模块的「拆装核心」：循环 `expected_len` 次，每次 `mir.extract_field` 取一个常量下标元素，并做严格的类型/长度校验：

[crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs:105-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L105-L157) —— `extract_array_registers`：校验 `MirArrayType` 的 size 与 element_type，逐元素 `MirExtractFieldOp` + `FieldIndexAttr` 拆成标量寄存器，注释说明这会 lowering 成 LLVM `extractvalue`、不引入临时栈槽。

`emit_mma_m16n8k16_f32_f16`（本轮新增的 f16 入口）是范式的标准实例：依次拆 C(4×f32)/A(4×u32)/B(2×u32)，拼成 10 操作数，建 `MmaM16N8K16F32F32Op`（4 个 f32 结果），再用 `MirConstructArrayOp` 装回 `[f32;4]`：

[crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs:305-433](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L305-L433) —— `emit_mma_m16n8k16_f32_f16`：三段式（拆 C/A/B → 建 mma op → 装回数组）。注意操作数顺序 `c_registers ++ a_registers ++ b_registers` 必须与 dialect op 的 `Verify` 期望一致。

`emit_mma_m16n8k8_f32_tf32`（本轮新增的 tf32 入口）与 f16 版几乎逐行相同，差别仅在数组元素类型仍为 u32（TF32 也打包进 u32 寄存器），但 op 类型换成 `MmaM16N8K8F32Tf32Op`：

[crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs:443-571](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L443-L571) —— `emit_mma_m16n8k8_f32_tf32`：tf32 m16n8k8，结构与 f16 同族，结果仍是 `[f32;4]`。

`emit_mma_m16n8k32_s32_s8`（本轮新增的 s8 入口）：与前两者形状相同，但**累加器与结果类型从 f32 改为 i32**（`Signed`），op 类型为 `MmaM16N8K32S32S8Op`，对应 PTX 的整型 `mma.sync.aligned.m16n8k32`：

[crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs:581-709](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L581-L709) —— `emit_mma_m16n8k32_s32_s8`：C/A 拆 i32/u32、B 拆 u32，建 `MmaM16N8K32S32S8Op`，装回 `[i32;4]`。

`try_dispatch_intrinsic` 中的四条 wmma 分派臂（连同既有的 bf16、f64）按 `pattern_name` 精确字符串匹配，命中即调对应 `emit_*`：

[crates/mir-importer/src/translator/terminator/mod.rs:3696-3763](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L3696-L3763) —— wmma 分派臂：`mma_m16n8k16_f32_bf16`/`mma_m16n8k16_f32_f16`/`mma_m16n8k8_f32_tf32`/`mma_m16n8k32_s32_s8`/`mma_m8n8k4_f64` 五条，各转交 `intrinsics::wmma::emit_*`。

dialect-nvvm 侧的 op 定义与校验：`MmaM16N8K16F32F16Op` 用 `#[pliron_op(... interfaces = [NOpdsInterface<10>, NResultsInterface<4>])]` 声明 10 操作数 / 4 结果，`Verify` 逐个核对前 4 个 f32、后 6 个 i32：

[crates/dialect-nvvm/src/ops/wmma.rs:180-249](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L180-L249) —— `MmaM16N8K16F32F16Op`：操作数/结果计数与类型校验，是 importer 发射的 op 的「结构契约」对端。

最后是这一切的 Rust 源头——cuda-device 侧的桩函数。它们都是 `#[inline(never)]` + `unreachable!()`，仅靠 mir-importer 识别调用、改写为 op 来「生效」：

[crates/cuda-device/src/wmma.rs:271-276](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L271-L276) —— `mma_m16n8k16_f32_f16`：`#[inline(never)]` + `unreachable!(...)` 桩，是 f16 mma 调用链的 Rust 起点。

#### 4.4.4 代码实践

**实践目标**：追踪一条 `mma_m16n8k16_f32_f16` 调用从设备桩函数到最终 dialect-nvvm op 的完整调用链，画出调用图。

**操作步骤**：

1. **起点**：在 kernel 里写一行 `let d = cuda_device::wmma::mma_m16n8k16_f32_f16(c, a, b);`。rustc 把它编译成一条 MIR `TerminatorKind::Call`，其 `func` 指向 `cuda_device::wmma::mma_m16n8k16_f32_f16` 这个 `FnDef`。
2. **入翻译**：mir-importer 的 `translate_body`（[body.rs:673](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L673)）逐块翻译，遇到该终止符调 `block::translate_block` → `translate_terminator`（[mod.rs:108](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L108)）。
3. **顶层分流**：`match term.kind` 命中 `TerminatorKind::Call` → `translate_call`（[mod.rs:868](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L868)）。
4. **抠名字**：`extract_func_info` 得到 `pattern_name = "cuda_device::wmma::mma_m16n8k16_f32_f16"`（[mod.rs:890](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L890)）。
5. **过前置筛子**：`precondition_check` 不命中、`callable_trait_call_info` 不命中（不是 Fn/FnMut/FnOnce）、`prof_trigger`/`__unroll_config`/`DynamicSharedArray`/`is_core_atomic_intrinsic`/`assert_inhabited` 均不命中（这些是按名字精确匹配的）。
6. **进总分派**：`try_dispatch_intrinsic`（[mod.rs:2136](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L2136)）。前置 `if let` 分类器（asm/typed_swap/bitops/saturating/bigint/float_math）都不命中，进入大 `match name`。
7. **命中 wmma 臂**：`"cuda_device::wmma::mma_m16n8k16_f32_f16"` 分支（[mod.rs:3710-3723](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L3710-L3723)）→ 调 `intrinsics::wmma::emit_mma_m16n8k16_f32_f16`。
8. **拆 fragment**：`emit_mma_m16n8k16_f32_f16`（[wmma.rs:305](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L305)）三次 `extract_array_registers` 拆出 4×f32 + 4×u32 + 2×u32。
9. **建 op**：`Operation::new(ctx, MmaM16N8K16F32F16Op::get_concrete_op_info(), [f32;4], 10_operands, ...)`（[wmma.rs:394-403](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L394-L403)），产 4 个 f32 结果寄存器。
10. **装回数组**：`MirConstructArrayOp` 把 4 个结果装回 `[f32;4]`（[wmma.rs:408-419](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L408-L419)）。
11. **收尾**：`emit_store_result_and_goto` 写回 destination 槽位并跳转 target。

**需要观察的现象**：步骤 6 里若把 `try_dispatch_intrinsic` 的 wmma 臂注释掉，会发生什么？——`match` 兜底返回 `Ok(None)`，于是这条调用落到 `translate_call` 末尾的「未识别 rustc intrinsic」检查之外（mma 不是 `core::intrinsics::`），最终被当成普通 `mir.call`，因「符号 `cuda_device::wmma::...` 找不到定义」（桩函数体被收集器跳过）在后段校验失败。

**调用链图示**：

```text
kernel: mma_m16n8k16_f32_f16(c,a,b)
  │ (rustc → MIR Call)
  ▼
translate_body ── translate_block ──► translate_terminator  [mod.rs:108]
                                            │ (match Call)
                                            ▼
                                       translate_call  [mod.rs:868]
                                            │ extract_func_info → pattern_name
                                            │ (前置筛子均不命中)
                                            ▼
                                  try_dispatch_intrinsic  [mod.rs:2136]
                                            │ match name
                                            ▼
        "cuda_device::wmma::mma_m16n8k16_f32_f16"  [mod.rs:3710]
                                            │
                                            ▼
       intrinsics::wmma::emit_mma_m16n8k16_f32_f16  [wmma.rs:305]
        ├── extract_array_registers ×3  (C/A/B → 标量寄存器)
        ├── new MmaM16N8K16F32F16Op       (dialect-nvvm op, 10 opds, 4 res)
        ├── MirConstructArrayOp           (4 res → [f32;4])
        └── emit_store_result_and_goto    (写回 + 跳转)
                                            │
                                            ▼
                       后段（mem2reg → mir-lower → PTX）
```

**预期结果**：最终生成的 dialect-nvvm op 是 `nvvm.mma_m16n8k16_f32_f16`（由 [dialect-nvvm/ops/wmma.rs:180](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L180) 的 `MmaM16N8K16F32F16Op` 定义），它在 mir-lower 阶段会降级成 `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32` 内联 PTX（lowering 细节见 u6-l3）。**待本地验证**：用一个最小 mma kernel 跑 `cargo oxide pipeline <name>`，在产出的 dialect-mir 中间文件里找到 `nvvm.mma_m16n8k16_f32_f16` 这一行，确认其 10 个操作数与 4 个结果。

#### 4.4.5 小练习与答案

**练习 1**：本轮新增的 f16/tf32/s8 三个 `emit_mma_*` 函数体几乎一模一样，为什么没有抽成一个泛型函数（像 atomic 模块的 `emit_packed_atom_add<O>` 那样）？

**参考答案**：差异点恰好「嵌在类型字面量里」——C/A/B 的元素类型（f32 vs i32）、数组长度（虽多为 4/4/2 但语义不同）、目标 op 类型、结果数组元素类型都随 dtype 变。要抽成泛型需要把这些都参数化（泛型类型参数 + 常量泛型 + op 类型参数），代价超过直接复制；而 atomic 的 `emit_packed_atom_add<O>` 之所以能复用，是因为 f16x2/bf16x2 两条路径的元素类型（u32）、长度、操作数布局完全相同，只有 op 类型不同，单一个泛型参数 `<O: Op>` 就够了。

**练习 2**：`extract_array_registers` 的诊断信息为什么刻意不点名 `bf16`/`tf32`/`f16`/`s8`？对应的那条测试（`mma_fragments_are_extracted_as_constant_index_ssa_values`）在守护什么不变量？

**参考答案**：因为这些片段类型不可跨 dtype 复用——一个 f16 mma 的 A 片段不能拿去喂 s8 mma。诊断若点名别的 dtype（如「应为 bf16 的片段」），会暗示「换一种 mma 就能修好」，反而误导。测试用 `assert!(!message.contains("bf16") && ...)` 守护「诊断只说『expected N scalar registers』」，强制用户从「当前这条 mma 的片段布局」找原因，而不是去猜别的 dtype。

**练习 3**：为什么 mma 的结果要先装回数组（`MirConstructArrayOp`），再由 `emit_store_result_and_goto` 写回 destination，而不是直接把 4 个结果寄存器写回？

**参考答案**：因为 MIR 层这次调用的「结果」是一个 `[f32;4]` 单值（存在 destination 局部的栈槽里），不是 4 个独立局部。mir-importer 的 alloca + load/store 模型按 MIR 局部组织存储，`store_local` 一次写一个 Value；所以必须先用 `construct_array` 把 4 个寄存器重组成单个数组 Value，再当一次 store 的值。下游 mem2reg/lowering 会把这套数组构造/析构优化掉。

## 5. 综合实践

把本讲四块知识串起来，完成一个「模拟新增 intrinsic」的源码阅读任务（不写代码，只画清单）：

假设要新增一条 `mma.sync.aligned.m16n8k8.f32.f16.f16.f32`（即 m16n8k8、f16 输入、f32 累加，区别于现有的 m16n8k16）。请对照本讲学到的「四层落地」与「分派漏斗」，写出你需要改动的源码点：

1. **设备层**：在 `crates/cuda-device/src/wmma.rs` 仿照 [mma_m16n8k16_f32_f16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L271-L276) 加一个 `#[inline(never)] unsafe fn mma_m16n8k8_f32_f16(c:[f32;4], a:[u32;2], b:[u32;1]) -> [f32;4]` 桩（注意 m16n8k8 的 A/B 片段更短）。
2. **dialect op 层**：在 `crates/dialect-nvvm/src/ops/wmma.rs` 仿照 [MmaM16N8K16F32F16Op](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L180-L249) 加一个 op（注意按 m16n8k8 调整 `NOpdsInterface`/`NResultsInterface` 的操作数与结果计数），并在 `register`（[wmma.rs:488-492](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs#L488-L492)）里登记。
3. **importer 翻译层**：在 `crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs` 仿照 [emit_mma_m16n8k16_f32_f16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L305-L433) 加一个 `emit_mma_m16n8k8_f32_f16`（调整 extract 的长度：A 拆 2、B 拆 1）。
4. **importer 分派层**：在 `crates/mir-importer/src/translator/terminator/mod.rs` 的 wmma 臂区（[mod.rs:3696-3763](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L3696-L3763)）加一条 `"cuda_device::wmma::mma_m16n8k8_f32_f16"` 分支。
5. **lowering 层**（下一讲 u6-l3 内容）：在 `crates/mir-lower/src/convert/intrinsics/wmma.rs` 加这条 op 到 PTX 内联汇编的降级规则。

完成清单后，对照 [u6-l4 端到端新增 intrinsic 模板](#)（下一讲）核对四层改动是否齐全——这正是 cuda-oxide「新增 intrinsic 全栈模板」的 mir-importer 这一环。本任务只做源码阅读与清单绘制，不实际改码。

## 6. 本讲小结

- mir-importer 的终止符翻译入口是 `translate_terminator`，对 `Call` 一类走 `translate_call`，它是一个**漏斗**：先跳过死分支、处理闭包/函数项，再处理需从 callee 泛型抠 const 的「特殊常量泛型 intrinsic」（prof_trigger/unroll/DynamicSharedArray/core atomic/assert_inhabited），最后才交 `try_dispatch_intrinsic`，剩下的是普通 `mir.call`。
- `try_dispatch_intrinsic` 是 intrinsic 总分派器：结构为「若干 `if let` 前置分类器 + 一个超大 `match name`」，发射逻辑按功能类别拆进 `terminator/intrinsics/` 下 21 个子模块（目录是目标态、迁移是渐进），所有 handler 用 `emit_store_result_and_goto` 统一收尾。
- atomic 模块同时服务两条前端：`cuda_device::atomic::*`（作用域编进类型名、ordering 是运行期参数）与 `core::sync::atomic::*`（ordering 是 const generic、作用域恒为 System），二者最终合流到同一批 `NvvmAtomicRmwOp`/`Load`/`Store`/`Cmpxchg`；关键是 std 与 cuda_device 的 ordering 判别值表在 Acquire/Release 上**互换**，必须分别映射。
- wmma 模块用统一的「`extract_array_registers` 拆片段 → 建 mma op → `MirConstructArrayOp` 装回数组」三段范式处理全部 dtype；本轮（#327/#328/#329）新增 f16 `m16n8k16`、tf32 `m16n8k8`、s8 `m16n8k32` 三条，与既有 bf16/f64 同族，差异仅在元素类型/数组长度/op 类型。
- 设备侧所有 GPU intrinsic 都是 `#[inline(never)]` + `unreachable!()` 的桩，靠 mir-importer 在翻译期「按名字识别调用、改写为 dialect op」生效，桩函数体永远不进 PTX。

## 7. 下一步学习建议

- **下一讲 u6-l3（mir-lower 深潜）**：本讲止步于「生成 `nvvm.mma_m16n8k16_f32_f16` op」；这个 op 如何降级成 `mma.sync.aligned...` 内联 PTX、如何在 `arithmetic.rs` 里按 `allow_fma_contraction` 决定浮点收缩，是 u6-l3 的主题。建议带着「一个 dialect-nvvm op 怎么变成 LLVM inline-asm」的问题去读。
- **u6-l4（端到端新增 intrinsic 模板）**：把本讲综合实践里的「四层落地清单」正式化，以本轮 #327/#328/#329 为范例，讲解「设备 API → dialect op → importer 翻译 → lowerer 转换」缺一不可的协同。
- **延伸阅读**：`crates/mir-importer/src/translator/terminator/intrinsics/` 下尚未深潜的子模块——`wgmma.rs`（Hopper 异步 warp-group MMA）、`tcgen05.rs`（Blackwell 张量核）、`tma.rs`（TMA）——它们与 wmma 同构但更复杂，可作为「对照阅读」练习，验证你是否真正掌握了本讲的分派与拆装范式。
- **回归验证**：读完后建议回到 u4-l2 的「两段编排」视角，确认本讲的 intrinsic 翻译发生在 mir-importer 的「前段（逐函数 translate_body）」之内，翻译产物（dialect-mir + dialect-nvvm 混存）随后整段交给 cuda-oxide-codegen 编排后段。
