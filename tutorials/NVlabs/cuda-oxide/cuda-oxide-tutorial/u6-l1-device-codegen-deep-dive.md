# Device codegen 深潜：`#[device]` 属性与设备端 MIR 生成

## 1. 本讲目标

本讲是「编译器深潜」单元的第一讲，承接 [u4-l1 后端入口与 host/device 分流](./u4-l1-backend-entry-and-split.md)。u4-l1 已经交代了 `__rustc_codegen_backend` 入口、`CudaCodegenBackend` 包装标准 LLVM 后端、`codegen_crate` 的 host/device 叠加分流，并在「交给 `mir_importer::run_pipeline`」处止步。本讲从那一处往里走，读完整个 `device_codegen.rs`，并下沉到它依赖的 `collector.rs`，把下面四件事讲透：

1. **设备函数识别**：后端怎么在一个 crate 的全部代码里，认出哪些函数该编译上 GPU。
2. **设备端 MIR 生成**：rustc 内部的 `rustc_middle` MIR 怎么被桥接成 mir-importer 使用的 `rustc_public`（stable MIR），再交给 cuda-oxide 流水线。
3. **宿主/设备隔离**：单源编译里，host 代码与 device 代码是如何被分流的，以及 owner 过滤、错误硬提升、panic 捕获这三道「隔离闸门」。
4. **编译策略注入**：以 `allow_fma_contraction`（浮点乘加是否允许收缩）为例，追踪一个编译策略如何从环境变量一路下贯到设备端代码生成。

学完后，你应该能够：在源码里指认设备函数的识别点、说清楚两套 MIR 表达之间的桥接、解释为何 device codegen 失败必须硬致命、并完整画出 `CUDA_OXIDE_NO_FMA` 影响设备端代码生成的链路。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**两套 MIR，一个桥梁。** rustc 内部用的是 `rustc_middle::mir`（类型带 `'tcx` 生命周期，依赖 `TyCtxt<'tcx>`）；而 cuda-oxide 的翻译流水线（mir-importer）为了「跟随 rustc 升级更稳」，是建立在 stable MIR（本仓库里叫 `rustc_public`）之上的。两套 API 各有一套 `Instance` 类型，本讲的 `device_codegen.rs` 主要工作就是在这两者之间架桥。详见模块顶部文档对这张对照表的描述（[`crates/rustc-codegen-cuda/src/device_codegen.rs`:L14-L23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L14-L23)）。

**MIR 是「优化后」的。** 当 `codegen_crate` 被调用时，rustc 已经跑完了所有 MIR 优化 pass（`-C opt-level`、`-Z mir-enable-passes` 决定）。后端拿到的就是这份**优化后、已单态化**的 MIR——这正是它会同时喂给标准 LLVM 后端的那一份（[`crates/rustc-codegen-cuda/src/lib.rs`:L88-L119](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L88-L119)）。这也解释了为什么必须 `-Z mir-enable-passes=-JumpThreading`：那道 pass 会在到后端之前把屏障复制进不同分支，造成 GPU 死锁（详见 u4-l1 与 `lib.rs` 的图示）。

**reserved namespace 是命名契约。** cuda-oxide 用一簇带哈希魔数的前缀来「打标」：`#[kernel]` 把 `fn foo` 改名为 `cuda_oxide_kernel_246e25db_foo`，`#[device]` 改名为 `cuda_oxide_device_246e25db_foo`。哈希 `246e25db` 是 `sha256("cuda_oxide_ + rust")` 截断 8 位，作用是让一个没有哈希的裸 `cuda_oxide_kernel_` 子串**永远不可能误匹配**，也让 `DEVICE_PREFIX` 与 `DEVICE_EXTERN_PREFIX` 互相排斥而不必排序判断（[`crates/reserved-oxide-symbols/src/lib.rs`:L20-L42](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L20-L42)）。这条命名契约是宏、collector、codegen 后端、lowering/export、运行时加载器五方的唯一真相源。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`crates/rustc-codegen-cuda/src/lib.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs) | 后端主体：`codegen_crate` 的 host/device 分流决策、owner 过滤、panic 捕获、错误硬致命，以及把产物打成 `.oxart` 制品对象。 |
| [`crates/rustc-codegen-cuda/src/device_codegen.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs) | 本讲主角：`generate_device_code` 桥接 `rustc_middle` 与 `rustc_public`，把收集到的设备函数送进 mir-importer 流水线，并注入编译策略。 |
| [`crates/rustc-codegen-cuda/src/collector.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs) | 设备函数收集器：从 kernel 入口出发的调用图 BFS、`no_std` 强制、intrinsic 桩识别、堆分配守卫。`device_codegen.rs` 通过 `use crate::collector::{CollectedFunction, DeviceExternDecl}`（[L96](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L96)）消费它的产物。 |
| [`crates/reserved-oxide-symbols/src/lib.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs) | 保留命名空间常量与判定函数（`is_kernel_symbol` / `is_device_symbol`），是识别设备函数的最终依据。 |
| [`crates/mir-importer/src/pipeline.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs) | `run_pipeline` 编排者：接收 `PipelineConfig`（含 `allow_fma_contraction`），把它转成后段 `BackendOptions.no_fma`，是编译策略下贯的下一站。 |

> 说明：manifest 给定的关键源码是 `device_codegen.rs` 与 `lib.rs`，但「设备函数识别」与「宿主/设备隔离」的实际逻辑大量落在 `collector.rs`。本讲按真实调用链补充引用 `collector.rs` 与 `reserved-oxide-symbols`，所有行号均对应当前 HEAD `29396b7`。

## 4. 核心概念与源码讲解

### 4.1 设备函数识别：从命名空间到调用图闭包

#### 4.1.1 概念说明

后端要回答的第一个问题是：**这个 crate 里，哪些函数属于 device 代码？** cuda-oxide 的回答分两步——先靠「保留命名空间」认出入口，再靠「调用图 BFS」把入口可达的全部函数收进闭包。

入口有两类：
- **kernel 入口**（`#[kernel]`）：会被宏改名为 `cuda_oxide_kernel_246e25db_<name>`，最终在 PTX 里以 `.entry` 出现，可被宿主启动。
- **独立 device 函数**（`#[device]` 标在普通 `fn` 上）：改名为 `cuda_oxide_device_246e25db_<name>`，在 PTX 里以 `.func` 出现，只能被 device 代码调用。

其余被入口**可达**调用的函数（如 `core` 的迭代器、`Option`、用户写的未标注 helper）则通过调用图自动归入。这一切由 `collector.rs` 完成，`device_codegen.rs` 只消费其结果。

#### 4.1.2 核心流程

识别一条 kernel 入口的判定极其朴素——只是子串匹配：

```rust
pub fn is_kernel_function(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    is_kernel_symbol(&tcx.def_path_str(def_id))
}
```
（[`crates/rustc-codegen-cuda/src/collector.rs`:L357-L359](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L357-L359)）

而 `is_kernel_symbol` 来自 reserved-oxide-symbols，定义就是一行：

```rust
pub fn is_kernel_symbol(name: &str) -> bool { name.contains(KERNEL_PREFIX) }
```
（[`crates/reserved-oxide-symbols/src/lib.rs`:L272-L273](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L272-L273)）

收集的主入口 `collect_device_functions` 采用**两阶段根发现**：

```text
Phase 1: 扫描所有 CGU，找出全部 kernel 入口（is_kernel_function）
          - 跳过 kernel 内部的 closure（{closure#0} 不是入口）
          - 跳过未单态化的泛型实例（scale<T>，只收 scale::<f32>）
          - 每个入口计算 export_name，作为根加入 worklist

Phase 2: 仅当 Phase 1 一个 kernel 都没找到时才跑
          - 扫描独立 #[device] 函数（is_device_function），作为非 kernel 根加入
          - 这样「无 kernel 的纯 device 库」也能编译出 .func

BFS: 从 worklist 弹出函数 → 取优化后 MIR → 遍历每个基本块的 terminator
     - 若是 Call：代入 caller 的泛型实参解析 callee → 判 crate 归属
     - 该收的入队、该跳的跳、违禁的硬报错
```
（Phase 1/2 与 BFS 分别见 [`collector.rs`:L655-L763](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L655-L763) 与 [`collector.rs`:L865-L919](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L865-L919)）

判 crate 归属的规则在 `should_collect_from_crate`（[`collector.rs`:L1406-L1506](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1406-L1506)），核心是「`std` 禁止、`core`/`alloc`/`cuda_device` 允许、其它 no_std crate 跟随可达」：

| 来源 crate | 决策 | 说明 |
|------------|------|------|
| 本地 crate | `Collect` | 用户代码，照单全收 |
| `core` / `alloc` / `cuda_device` | `Collect`（但 `::fmt::`/`::panicking::`/`precondition_check` 跳过） | no_std 基础 |
| `std`（真正的 std，非 re-export） | `Forbidden`（硬报错框） | OS/IO/线程不能上 GPU |
| `libm` | `SkipIntentional` | 由 mir-importer 的 float-math 分派改写到 `__nv_*`，体不能收 |
| 其它外部 no_std crate | `Collect` | 跨 crate device helper |

#### 4.1.3 源码精读

`is_fully_monomorphized` 守住「只收具体实例」这条底线——泛型定义 `scale<T>` 会被跳过，只收 `scale::<f32>`（[`collector.rs`:L381-L398](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L381-L398)）：

```rust
pub fn is_fully_monomorphized<'tcx>(tcx: TyCtxt<'tcx>, instance: Instance<'tcx>) -> bool {
    if instance.args.has_non_region_param() { return false; }
    if generics.requires_monomorphization(tcx) && instance.args.is_empty() { return false; }
    true
}
```

调用边的处理是 `process_call_operand`，它做了三件关键的事（[`collector.rs`:L1003-L1271](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1003-L1271)）：

1. **泛型实参代入**：用 caller 的具体实参替换 callee 的形参，把 `scale<T>` 解析成 `scale::<f32>`（[`collector.rs`:L1046-L1050](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1046-L1050)）。
2. **去重键用 mangled 名**：`seen` 集合以 `tcx.symbol_name(resolved)` 为键，因此同一泛型的不同单态化互不混淆（[`collector.rs`:L1158-L1162](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1158-L1162)）。
3. **intrinsic 桩识别**：`is_unreachable_body` 判定「整个函数体只是一段 panic」的占位桩（如 `cuda_device::threadIdx_x` 在宿主侧的桩），跳过不收——它们由 mir-importer 按名字改写（[`collector.rs`:L1562-L1597](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1562-L1597)）。

最后，`std` 违禁与堆分配守卫都是**硬致命**：违禁 `std` 会 panic 出一个带边框的诊断框（[`collector.rs`:L1061-L1111](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1061-L1111)）；从 device 代码可达 `__rust_alloc` 会经 `report_heap_allocation` 报「GPU 无设备分配器」（[`collector.rs`:L1190-L1206](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1190-L1206) 调用 [`collector.rs`:L1604-L1637](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1604-L1637)）。这两处体现了 cuda-oxide 的一条工程原则（仓库 `.cursor/rules/compiler-gaps-are-bugs.mdc`）：编译期的缺口要尽早变成可读的错误，绝不静默放过、绝不把问题推迟成 PTX 里晦涩的「undefined symbol」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 collector 的两阶段根发现与调用图闭包。

**操作步骤**：

1. 进入任一带 kernel 的示例，例如 vecadd，开启 verbose 与 rustc MIR 转储：
   ```bash
   cd crates/rustc-codegen-cuda/examples/vecadd
   CUDA_OXIDE_VERBOSE=1 CUDA_OXIDE_SHOW_RUSTC_MIR=1 cargo oxide build
   ```
2. 在 stderr 里找 `[collector] Found kernel:` 行（Phase 1 入口），随后是 `[collector] Discovered callee:` 行（BFS 闭包）。
3. 挑一个示例里的 `#[kernel] fn`，在它内部调用一个**未标注 `#[device]`** 的本地 helper，并在 helper 里调用 `thread::index_1d()`，重新编译。

**需要观察的现象**：第 2 步应看到 kernel 名被剥成 `vecadd` 这样的裸名（`kernel_base_name`），以及一连串来自 `core` 的被收集函数（迭代器/`Option` 等）。第 3 步应触发 `check_unreachable_callee` 的诊断——它告诉你 `thread::index_*` 只在 `#[kernel]`/`#[device]` 内有效，并建议给 helper 加 `#[device]`（[`collector.rs`:L1660-L1776](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1660-L1776)）。

**预期结果**：能从日志里数出「根 → 直接 callee → 间接 callee」三层；第 3 步编译失败且错误指向你写的 helper，而不是 PTX 的 undefined symbol。若本机无 GPU，`cargo oxide build` 仍可完成编译期部分，因此本实践**不需要 GPU**。

> 待本地验证：具体日志行数与诊断措辞依工具链版本而定；若 `CUDA_OXIDE_SHOW_RUSTC_MIR` 未被识别，请以 `cargo oxide doctor` 确认环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `seen` 用 mangled 名而不是 `DefId` 去重？
**答**：`DefId` 只标识泛型定义，不区分单态化；`map::<f32, Closure1>` 与 `map::<f32, Closure2>` 共享同一个 `DefId` 却要生成两份 PTX。mangled 名带上了完整的类型实参，能唯一标识每个单态化实例（[`collector.rs`:L771-L774](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L771-L774)）。

**练习 2**：Phase 2（独立 device 函数根发现）为什么只在「没有 kernel」时才跑？
**答**：当存在 kernel 时，所有 device 函数都会经调用图被自动收入闭包；只有在一个「纯 device 库、暂无 kernel」的 crate 里，才需要把 `#[device]` 函数当根来启动 BFS（[`collector.rs`:L727-L732](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L727-L732) 的注释明确写了这点）。

---

### 4.2 设备端 MIR 生成：`rustc_middle` 到 `rustc_public` 的桥接

#### 4.2.1 概念说明

collector 给出的是一列 `CollectedFunction<'tcx>`（持有 `rustc_middle::ty::Instance<'tcx>`）；而 mir-importer 的 `run_pipeline` 要的是 stable MIR 的 `rustc_public::mir::mono::Instance`。`device_codegen.rs` 的全部存在意义就是这个桥接。模块文档把它概括为「**重用而非重写**」——mir-importer 已经过充分测试、跟随 rustc 升级更稳，于是宁可多一次类型转换，也不重写流水线（[`device_codegen.rs`:L84-L94](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L84-L94)）。

#### 4.2.2 核心流程

`generate_device_code` 的三步骨架：

```text
STEP 0（进闭包前预计算）
  - export_names / debug_scope_maps / inline_always_flags
  - device_externs 经 rustc_ty_to_device_extern_type 转成 mir-importer 类型
  原因：闭包不能捕获对本地 TyCtxt 数据的引用，凡是要带进 stable_mir
        上下文的东西，都得先拷成 owned 数据。

STEP 1  rustc_internal::run(tcx, || { ... })
        建立 Tables + CompilerCtxt，让 stable() 转换可用。

STEP 2  在闭包内，逐个 func：
          stable_instance = rustc_internal::stable(func.instance)
        把 rustc_middle::Instance 变成 rustc_public::Instance。

STEP 3  组装 PipelineConfig（含 allow_fma_contraction 等），调用
        mir_importer::run_pipeline(&stable_functions, &stable_device_externs, &cfg)
        产出 .ll / .ptx（或 NVVM IR / LTOIR / cubin）。
```
（三步对应 [`device_codegen.rs`:L451-L752](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L451-L752) 的整体，骨架图见模块文档 [`device_codegen.rs`:L27-L82](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L27-L82)）

结果回传时要拆两层 `Result`：外层是 `rustc_internal::run` 的成败，内层是 `run_pipeline` 的成败（[`device_codegen.rs`:L703-L751](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L703-L751)）。

#### 4.2.3 源码精读

STEP 0 的「预计算」里最值得读的是 `inline_always_flags`。`#[inline(always)]` 是个只活在 `rustc_middle::TyCtxt` 上的查询，stable_mir 没有暴露它；于是必须**在进入 stable_mir 之前**把它问出来，否则下游 lowering 只能靠优化器启发式决定是否内联 helper（[`device_codegen.rs`:L620-L633](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L620-L633)）：

```rust
let inline_always_flags: Vec<bool> = functions.iter().map(|func| {
    let def_id = func.instance.def_id();
    matches!(
        tcx.codegen_fn_attrs(def_id).inline,
        rustc_hir::attrs::InlineAttr::Always | rustc_hir::attrs::InlineAttr::Force { .. }
    )
}).collect();
```

STEP 1+2 的核心转换只有一行——`rustc_internal::stable(func.instance)`（[`device_codegen.rs`:L637-L657](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L637-L657)）。它把预计算的四个字段（instance、is_kernel、export_name、debug_source_scopes、is_inline_always）重新打包成 mir-importer 的 `CollectedFunction`。

device extern（`#[device] extern "C" { ... }` 声明的外部函数，用于链接外部 LTOIR）的签名校验也发生在 STEP 0：`rustc_ty_to_device_extern_type` 把 rustc 的 `Ty` 递归翻成 mir-importer 的 `DeviceExternType`，并**拒绝**不支持的 C ABI 类型（如 `bool`、`char`、按值传递的 `f16`、窄整数）——错误统一归到 `InvalidDeviceExternSignature`（[`device_codegen.rs`:L121-L221](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L121-L221)，校验入口 [`device_codegen.rs`:L500-L557](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L500-L557)）。注意它要求 `extern "C"` 且不可变参、不可 unwind。

#### 4.2.4 代码实践

**实践目标**：观察到「两套 Instance」的桥接确实发生，并理解 STEP 0 预计算的必要性。

**操作步骤**：

1. 阅读 [`device_codegen.rs`:L635-L699](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L635-L699)，对照模块顶部的 ASCII 图（[`device_codegen.rs`:L27-L82](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L27-L82)），在自己的话里写出 STEP 0/1/2/3 各自承担的工作。
2. 找到 `rustc_internal::stable(func.instance)` 这一行（约 L646），它是唯一的「类型穿越点」。回答：为什么这一行不能移到闭包外面？
3. 思考题（不需改码）：若把 `inline_always_flags` 的预计算删掉、改为在闭包内查询 `tcx`，编译会怎样？

**需要观察的现象/预期结果**：第 2 步应认识到 `stable()` 依赖 stable_mir 的 `Tables`/`CompilerCtxt`，而这些只有在 `rustc_internal::run` 的闭包内才建立；闭包外调用会缺少上下文。第 3 步应认识到 `tcx` 借用了 `'tcx` 生命周期，无法被 `'static` 闭包捕获——这正是「STEP 0 必须预计算 owned 数据」的根本原因（[`device_codegen.rs`:L487-L489](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L487-L489) 的注释点明了这点）。

#### 4.2.5 小练习与答案

**练习 1**：`generate_device_code` 为什么在最开头就检查 `functions.is_empty()` 并返回 `NoKernels`？
**答**：因为 collector 可能因 owner 过滤等原因返回空集；空集进入流水线只会无谓地创建空模块。提前返回 `DeviceCodegenError::NoKernels` 把语义讲清楚（[`device_codegen.rs`:L459-L461](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L459-L461)）。

**练习 2**：device extern 为什么必须是 `extern "C"` 且 `unwind: false`？
**答**：device extern 要被翻成 LLVM `declare`，供 nvJitLink 与外部 LTOIR 链接；CUDA 工具链不支持展开，且 C ABI 是与外部库对接的唯一稳定约定，所以变参、unwinding、Rust ABI 都被拒（[`device_codegen.rs`:L507-L519](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L507-L519)）。

---

### 4.3 宿主/设备隔离：叠加分流与三道闸门

#### 4.3.1 概念说明

u4-l1 已指出分流是「叠加（overlay）而非互斥」——host 代码**永远**走标准 LLVM 后端生成 x86_64，device 代码是**额外**触发的一条流水线。本讲深入这条叠加线上的三道「隔离闸门」：

1. **owner 过滤闸门**：决定哪些 crate 才有资格产生 device 制品。
2. **错误硬提升闸门**：device codegen 一旦失败，必须让整个 cargo 失败。
3. **panic 捕获闸门**：流水线里的 panic 不能伪装成 rustc 的 ICE。

#### 4.3.2 核心流程

`codegen_crate` 的分流决策（[`lib.rs`:L505-L530](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L505-L530)）：

```text
mono_partitions = tcx.collect_and_partition_mono_items()
kernel_count    = count_kernels_in_cgus(...)        // 0 表示无 kernel
device_fn_count = count_device_fns_in_cgus(...)     // 0 表示无 #[device] fn
contains_device_code = kernel_count>0 || device_fn_count>0
has_device_code = contains_device_code && allows_device_codegen_for(crate_name)  ← 闸门1
```

闸门 1 的 `allows_device_codegen_for` 受 `CUDA_OXIDE_DEVICE_CODEGEN_CRATE` 控制（[`lib.rs`:L414-L419](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L414-L419)）：未设该变量时所有 crate 都允许；设了就只允许列出的 owner crate 产生 device 制品。这解决了「一个 workspace 里多 crate 都含 `#[cuda_module]`，但只想让某个 crate 真正出 PTX」的需求。

无论分流结果如何，结尾都无条件调用 `self.llvm_backend.codegen_crate(tcx, crate_info)` 把全部 host 代码交给 LLVM（[`lib.rs`:L734](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L734)）——这就是「叠加」。

#### 4.3.3 源码精读

**闸门 1 的「软着陆」**：当 owner 过滤把某 crate 的 device 制品挡掉，但该 crate 用的是旧版 `#[cuda_module]` 宏（仍引用老的包级锚符号），后端会写一个**只含 legacy 锚、不含 `.oxart` bundle** 的弱对象，保证混版本 host 代码仍能链接（[`lib.rs`:L532-L554](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L532-L554)）。这是「向前兼容」的工程化体现。

**闸门 2 的硬致命**：device codegen 的 `Ok(Err(e))` 分支调用 `tcx.dcx().fatal(...)`，理由写在注释里——吞掉错误会让 host 二进制带着「过期或缺失的 PTX」静默错跑在 GPU 上，而 cargo-oxide 包装脚本仍会打印「✓ Build succeeded」（因为下面的 host LLVM 后端成功了）。所以必须把失败提成 rustc fatal，让 cargo 非零退出（[`lib.rs`:L715-L727](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L715-L727)）。

**闸门 3 的 panic 捕获**：流水线内部（典型是 pliron 的 IR 不变式检查）可能 panic。若放任它逃逸，会被 rustc 的 ICE hook 包装成「编译器意外 panic，请报 rustc bug」。但 bug 其实不在 rustc 而在 cuda-oxide。于是 `codegen_crate` 用 `catch_unwind` 捕获，并**短暂替换** rustc 的 panic hook 为自带 backtrace 捕获的自定义 hook（因为 panic hook 在 `catch_unwind` 之前触发，不换就会先打印 rustc 横幅），再把 panic 重发为「This is a bug in cuda-oxide, please file at .../issues」（[`lib.rs`:L607-L673](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L607-L673)，尤其 L625-L647 的 hook 替换与 backtrace 捕获）。

三道闸门共同保证：device 代码要么正确地变成内嵌制品，要么编译 loudly 失败——不存在「编译成功但 GPU 上错跑」的灰色地带。

#### 4.3.4 代码实践

**实践目标**：体会 owner 过滤与硬致命。

**操作步骤**：

1. 阅读 [`lib.rs`:L510-L523](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L510-L523)，确认 `has_device_code` 同时要求「有 device 代码」与「owner 允许」两个条件。
2. 阅读 [`lib.rs`:L715-L727](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L715-L727) 的硬致命注释，再用一句话解释「为何不能把 device codegen 错误降级为 warning」。
3. 选做：在一个双 crate 的设想场景里（一个 kernel 库 + 一个 host app），设想设置 `CUDA_OXIDE_DEVICE_CODEGEN_CRATE=kernel_lib`，预测哪个 crate 会出 PTX、哪个只会出 legacy 锚对象。

**需要观察的现象/预期结果**：第 2 步应指出「host LLVM 后端独立成功 → 包装脚本误报成功 → 用户拿到带错 PTX 的二进制 → 在 GPU 上静默错跑」这条链；第 3 步应预测只有 `kernel_lib` 出 `.oxart`，host app 走 legacy 锚弱对象。

> 待本地验证：第 3 步的双 crate 场景需要你自建测试工程；本仓库的 examples 都是单 crate standalone，可直接观察的是第 1、2 步。

#### 4.3.5 小练习与答案

**练习**：`catch_unwind` 之外，为什么还要 `take_hook`/`set_hook` 临时换 panic hook？
**答**：panic hook 在 unwind 被 catch **之前**触发；不换的话 rustc 的 ICE 横幅会先打到 stderr，随后才轮到我们的诊断。自定义 hook 还顺便捕获 backtrace（因为到 `catch_unwind` 接住时，想要的栈帧已经没了）。hook 是全局的，但此处 codegen 实际单线程，短暂换 hook 是安全的（[`lib.rs`:L613-L647](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L613-L647)）。

---

### 4.4 编译策略注入：`allow_fma_contraction` 的全程下贯

#### 4.4.1 概念说明

「编译策略」指那些**不改变源码语义、但改变生成代码**的开关。本节以浮点乘加收缩（FMA contraction）为例——它决定编译器是否可以把 `a*b + c` 收缩成一条硬件 `fma(a,b,c)`。收缩通常更快、更准（只舍入一次），但会改变逐位结果，因此必须可控。

记 \(\text{fma}(a,b,c)\) 为单次舍入的融合乘加。默认策略允许把普通表达式 \(a\cdot b + c\) 替换为 \(\text{fma}(a,b,c)\)；设 `CUDA_OXIDE_NO_FMA` 后则禁止这种替换（但源码里显式的 `f32::mul_add` 不受影响，那是用户主动要求的融合）。这一策略必须**从 codegen 后端一路传到最终 cubin**，否则中间任何一环自行决定都会破坏契约。

#### 4.4.2 核心流程

`allow_fma_contraction` 的全程（本讲覆盖前半段，后半段属 u6-l3 与 u4-l5）：

```text
[用户]  设 CUDA_OXIDE_NO_FMA=1   （或 cargo oxide --no-fmad）
   │
   ▼
[device_codegen.rs:675]  allow_fma_contraction = var_os("CUDA_OXIDE_NO_FMA").is_none()
   │
   ▼
[device_codegen.rs:692]  写入 PipelineConfig.allow_fma_contraction
   │
   ▼
[mir-importer/pipeline.rs:303]  backend_options.no_fma = !config.allow_fma_contraction
   │
   ▼ （后段，u6-l3 详述）
   mir-lower 的算术 converter 据 no_fma 决定浮点 op 是否挂 fast-math contract 标志
   │
   ▼ （产物回流）
[device_codegen.rs:739]  DeviceCodegenResult.allow_fma_contraction = compilation_result.allow_fma_contraction
   │
   ▼
[lib.rs:811]  write_device_artifact_object 用 with_fma_contraction(result.allow_fma_contraction)
              打进 .oxart 的 compile-options 位域（仅 NVVM IR / LTOIR 制品）
   │
   ▼ （运行时，u4-l5 详述）
   cuda-host 读回 compile-options，下发 libNVVM / nvJitLink 的 -fma=0/1
```

#### 4.4.3 源码精读

注入点是 stable_mir 闭包内的一行（[`device_codegen.rs`:L675-L679](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L675-L679)）：

```rust
let allow_fma_contraction = std::env::var_os("CUDA_OXIDE_NO_FMA").is_none();
if verbose && !allow_fma_contraction {
    eprintln!("[device_codegen] FMA contraction disabled");
}
```

注意它用的是 `var_os`（只要变量**存在**即视为禁用，不看值），因此 `CUDA_OXIDE_NO_FMA=0` 也会禁用——这是「存在即生效」的约定。随后它被写进 `PipelineConfig`（[`device_codegen.rs`:L682-L693](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L682-L693)），与 `target_arch`、`device_arch_hint`、`debug_kind`、`emit_nvvm_ir` 一起交给 `run_pipeline`。

下一站是 mir-importer 把它**取反**成 `no_fma`（语义翻转：上游「允许收缩」= 下游「不禁止 FMA」），写进 `BackendOptions`（[`crates/mir-importer/src/pipeline.rs`:L303](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L303)）：

```rust
backend_options.no_fma = !config.allow_fma_contraction;
```

`PipelineConfig.allow_fma_contraction` 字段的文档把它讲得很清楚：「是否允许普通的浮点乘/加或乘/减表达式收缩为融合运算；显式融合运算（如 `f32::mul_add`）不受影响」（[`crates/mir-importer/src/pipeline.rs`:L158-L162](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L158-L162)），默认值是 `true`（[L177](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L177)）。

产物回流时，`CompilationResult` 把策略原样带回（[pipeline.rs:340 与 L349](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L334-L351)），`device_codegen.rs` 把它放进 `DeviceCodegenResult.allow_fma_contraction`（字段定义 [`device_codegen.rs`:L246-L248](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L246-L248)，赋值 [`device_codegen.rs`:L739](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L739)）。

最后，`lib.rs` 的 `write_device_artifact_object` 在**只在 NVVM IR / LTOIR 制品**时，把这个策略写进 `.oxart` 的 compile-options 位域（[`lib.rs`:L805-L814](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L805-L814)）：

```rust
let compile_options = if matches!(artifact.kind,
    device_codegen::DeviceCodegenArtifactKind::NvvmIr
        | device_codegen::DeviceCodegenArtifactKind::Ltoir
) {
    oxide_artifacts::ArtifactCompileOptions::new()
        .with_fma_contraction(result.allow_fma_contraction)
} else {
    oxide_artifacts::ArtifactCompileOptions::new()   // PTX/cubin 不带边车
};
```

为什么只有 NVVM IR / LTOIR 才写？因为这两条路径还要在运行时经 libNVVM/nvJitLink 再次编译成机器码，那一步会重新决定 FMA，所以必须把策略随制品带下去；而 PTX/cubin 制品的机器码已经定型，无需再传（详见 u4-l5）。mir-importer 还会为 NVVM IR 额外写一个 `.options` sidecar 文件记录同一策略（[`pipeline.rs`:L326-L330 与 L398-L405](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L324-L330)）。

#### 4.4.4 代码实践（本讲指定的核心实践）

**实践目标**：在 `device_codegen.rs` 中找到设置 `allow_fma_contraction` 的位置，完整解释 `CUDA_OXIDE_NO_FMA` 如何一路影响设备端代码生成。

**操作步骤**：

1. **定位注入点**：在 [`device_codegen.rs`:L675](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L675) 找到 `let allow_fma_contraction = std::env::var_os("CUDA_OXIDE_NO_FMA").is_none();`。这是策略进入 cuda-oxide 的唯一入口。
2. **追踪下贯**：沿调用链读三处——
   - 写入 `PipelineConfig`（[`device_codegen.rs`:L692](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L692)）；
   - mir-importer 取反为 `backend_options.no_fma`（[`pipeline.rs`:L303](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L303)）；
   - 回流到 `DeviceCodegenResult`（[`device_codegen.rs`:L739](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L739)）并写进 `.oxart` compile-options（[`lib.rs`:L811](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L811)）。
3. **写一段对比内核**：在一个 kernel 里写 `let z = a*b + c;`（`a,b,c: f32`），分别在默认与 `CUDA_OXIDE_NO_FMA=1` 下编译，用 `cargo oxide pipeline <name>` 查看产出的 `.ll`。

**需要观察的现象**：verbose 模式下应看到 `[device_codegen] FMA contraction disabled`（[`device_codegen.rs`:L677-L679](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L677-L679)）。在 `.ll` 里，默认（FMA on）的浮点乘加 op 会带 `contract` fast-math 标志，允许 NVPTX 后端融合成 `fma.rn.f32`；设 `CUDA_OXIDE_NO_FMA=1` 后该标志消失，乘法和加法保持两条独立指令。显式的 `f32::mul_add(a,b,c)` 在两种设置下都应生成 `fma.rn.f32`（因为它本就是显式融合，不受策略影响）。

**预期结果**：你能用一句话说清——`CUDA_OXIDE_NO_FMA` 经 device_codegen 读入 → mir-importer 转成 `no_fma` → mir-lower 据此开关浮点 op 的 `contract` 标志 → 最终机器码遵守契约；NVVM/LTOIR 路径还会把策略随 `.oxart` 传给运行时的 libNVVM/nvJitLink。

> 待本地验证：`.ll` 里 fast-math 标志的具体拼写、以及是否能在某条简单表达式上稳定看到差异，取决于 mir-lower 的算术 converter（u6-l3 详述）与 NVPTX 后端的优化启发式；若对比不明显，可改用更长的乘加链或查阅 u6-l3 的算术 lowering 细节。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `allow_fma_contraction` 默认是 `true`？
**答**：FMA 收缩通常更快、更准（只舍入一次），是高性能 GPU 代码的默认期望；只有需要逐位可复现性时才用 `CUDA_OXIDE_NO_FMA` 关闭（[`pipeline.rs`:L165-L180](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L165-L180) 的 `Default` 实现）。

**练习 2**：为什么 `device_codegen.rs` 用 `var_os().is_none()`，而 `CUDA_OXIDE_VERBOSE` 用 `var().is_ok()`？两者语义有何不同？
**答**：`var_os` 只看变量是否存在（不校验值也不校验 UTF-8），契合「`CUDA_OXIDE_NO_FMA=任意值` 甚至 `=0` 都算禁用」的约定；`var().is_ok()` 则要求变量存在且是合法 UTF-8。选择哪种取决于该开关是否需要区分「未设」与「设为假值」（[`device_codegen.rs`:L675](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L675) 对照 [`lib.rs`:L399](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L399)）。

**练习 3**：为什么 PTX/cubin 制品不带 FMA compile-options 边车，而 NVVM IR / LTOIR 带？
**答**：PTX/cubin 的机器码在 codegen 期已定型，运行时不再二次编译，FMA 契约已在 mir-lower/llc 阶段落实；NVVM IR / LTOIR 还要在运行时经 libNVVM/nvJitLink 重新编成机器码，那一步会重新决策 FMA，必须把策略随制品带下去（[`lib.rs`:L805-L814](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L805-L814)，机制详见 u4-l5）。

## 5. 综合实践

把本讲四个模块串起来，做一次「**给 device codegen 加一行可观测日志，并追踪一次策略下贯**」的端到端阅读：

1. **识别**：在 `collector.rs` 的 `collect_device_functions`（[L655](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L655)）与 `should_collect_from_crate`（[L1406](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs#L1406)）处，挑一个示例（如 vecadd）画出「kernel 根 → core 被收 callee」的调用图闭包。
2. **桥接**：在 `device_codegen.rs` 的 `generate_device_code`（[L451](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L451)）里，标注 STEP 0（预计算）/STEP 1（进 stable_mir）/STEP 2（stable 转换）/STEP 3（run_pipeline）四个阶段的边界行号。
3. **隔离**：在 `lib.rs` 的 `codegen_crate`（[L505](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L505)）里，指出 owner 过滤闸门（[L518](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L518)）、硬致命闸门（[L725](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L725)）、panic 捕获闸门（[L637](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L637)）各自的位置，并解释三者为何缺一不可。
4. **策略**：设 `CUDA_OXIDE_NO_FMA=1` 与 `CUDA_OXIDE_VERBOSE=1` 编译同一示例，对照本讲 4.4.2 的流程图，确认日志里出现 `[device_codegen] FMA contraction disabled`，并在 `.options`/`.oxart` 里找到策略被记录的证据（若该示例走 NVVM IR 路径）。

完成后再回答一个问题：如果有人把 [`device_codegen.rs`:L675](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L675) 这一行删掉、改成硬编码 `let allow_fma_contraction = true;`，整条链路里哪些环节会失去用户控制？（答：mir-lower 的浮点 `contract` 标志永远打开、运行时 libNVVM/nvJitLink 永远收到 `-fma=1`，用户的逐位可复现性需求失效。）

## 6. 本讲小结

- **设备函数识别**靠保留命名空间（`cuda_oxide_kernel_246e25db_` / `cuda_oxide_device_246e25db_`）认出入口，再用调用图 BFS 收闭包；`std` 违禁与堆分配守卫都是硬致命，体现「编译期缺口不当 bug」原则。
- **设备端 MIR 生成**的核心是 `rustc_middle` → `rustc_public` 的桥接：因闭包不能捕获 `'tcx` 数据，凡进 stable_mir 上下文所需的字段（含 `inline_always`）都得在 `rustc_internal::run` 之前预计算成 owned 数据。
- **宿主/设备隔离**是叠加分流，三道闸门（owner 过滤、错误硬致命、panic 捕获）共同消灭「编译成功但 GPU 错跑」的灰色地带；其中硬致命是因为 host LLVM 后端独立成功会骗过包装脚本。
- **编译策略注入**以 `allow_fma_contraction` 为样板：`CUDA_OXIDE_NO_FMA` 在 `device_codegen.rs:675` 入口，经 `PipelineConfig` → `backend_options.no_fma`（取反）下贯到 mir-lower，再经 `DeviceCodegenResult` 回流写进 NVVM/LTOIR 制品的 compile-options 边车，最终约束运行时 libNVVM/nvJitLink 的 `-fma`。
- 跨讲义承接：识别用到的命名契约由 u2-l1 的宏与 u7-l4 的 reserved-symbols 维系；策略的后半段（mir-lower 算术 converter）属 u6-l3，运行时 FMA 路由属 u4-l5。

## 7. 下一步学习建议

- **u6-l2 mir-importer 深潜**：本讲到 `mir_importer::run_pipeline` 就把控制权交出去了，下一讲深入 terminator/intrinsics 的翻译分派机制，看 device MIR 是如何被一句句翻成 dialect op 的。
- **u6-l3 mir-lower 深潜**：本讲的 `allow_fma_contraction`/`no_fma` 在 mir-lower 的算术 converter 里真正落地为 fast-math `contract` 标志，下一讲去读那个判断逻辑。
- **u4-l5 NVVM IR → cubin（含 FMA 契约）**：想看清 `.oxart` 的 compile-options 边车如何被 cuda-host 读回、如何下发 libNVVM/nvJitLink，继续这一讲。
- **u7-l1 compile_fail 与安全契约**：本讲提到的「编译期硬报错」哲学，在 compile_fail 测试套件里有成体系的体现，可对照阅读。
- 继续阅读建议：通读 [`device_codegen.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs) 的模块级文档与 [`collector.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/collector.rs) 的两张 ASCII 图，它们是理解 device codegen 全貌最好的入口。
