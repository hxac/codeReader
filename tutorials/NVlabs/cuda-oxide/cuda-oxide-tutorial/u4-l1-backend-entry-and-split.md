# 后端入口与 host/device 分流

## 1. 本讲目标

本讲是「编译流水线总览」单元（U4）的第一讲。我们已经在前面的讲义里反复提到一句话：「cuda-oxide 是一个把纯 Rust 的 `#[kernel]` 函数编译成 CUDA PTX 的自定义 rustc 后端」。但这句话背后有一个最关键的工程问题一直没有正面回答：

> rustc 是怎么把控制权交给 cuda-oxide 的？又是怎么决定哪些代码走 GPU、哪些代码走 CPU 的？

读完本讲，你应当能够：

1. 说清楚 `__rustc_codegen_backend` 这个入口符号的发现与加载机制，理解 cuda-oxide 后端本质上是一个被 rustc `dlopen` 的动态库插件。
2. 说明 `CudaCodegenBackend` 如何用「包装而非重写」的策略委托给标准的 `rustc_codegen_llvm`，并指出哪些 trait 方法被转发、哪个方法被改写。
3. 追踪 `codegen_crate` 里的 host/device 分流决策：核函数计数、owner 过滤、设备流水线的条件触发，以及「宿主代码永远走 LLVM」这一条不变量。
4. 进入 `device_codegen` 模块，理解它如何用 `stable_mir` 把 rustc 内部 MIR 桥接到 cuda-oxide 的 mir-importer 流水线。

本讲只看两个文件：`crates/rustc-codegen-cuda/src/lib.rs` 与 `crates/rustc-codegen-cuda/src/device_codegen.rs`。后续 U4 的讲义会分别深入 importer、方言层、lowering 等子阶段。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫四个概念。

### 2.1 什么是「代码生成后端（codegen backend）」

rustc 的编译过程是一条流水线：源码 → HIR → MIR →（代码生成）→ 机器码。最后这一段「代码生成」在 rustc 里被设计成**可插拔**的：rustc 定义了一个 trait `CodegenBackend`，任何实现了这个 trait 的东西都可以接管最后一段。

- rustc 自带的默认后端是 `rustc_codegen_llvm`，它把 MIR 翻译成 LLVM IR，再由 LLVM 生成 x86_64/ARM 等机器码。
- cuda-oxide 做的，就是**再写一个实现了 `CodegenBackend` 的后端**，在代码生成阶段「截胡」一部分代码，把它编成 GPU 的 PTX，而不是 CPU 机器码。

> 关键直觉：cuda-oxide 不是另一个编译器，它是 rustc 的一个**插件**，挂在 rustc 流水线的最末端。

### 2.2 后端是怎么被 rustc 加载的

rustc 通过 `-Z codegen-backend=路径` 指向一个 `.so`（Linux）/`.dylib`（macOS）动态库。rustc 会：

1. `dlopen` 这个动态库；
2. 用 `dlsym` 在里面找一个**名字固定为 `__rustc_codegen_backend`** 且带 `#[no_mangle]`（名字不被 Rust 编译器混淆）的函数；
3. 调用它，拿到一个 `Box<dyn CodegenBackend>`。

所以 cuda-oxide 后端 `librustc_codegen_cuda.so` 里必须导出这么一个固定名字的函数。这就是本讲 4.1 节的主角。

### 2.3 host 代码与 device 代码

CUDA 编程模型里，一段程序天然分成两半：

- **host 代码**：在 CPU 上跑，负责分配显存、启动内核、搬运数据、打印结果。对 cuda-oxide 来说，这些就该被编成普通的 x86_64 机器码。
- **device 代码**：在 GPU 上跑，即「内核（kernel）」以及内核调用的那些辅助函数。这些需要被编成 PTX。

cuda-oxide 的卖点叫**单源编译（single-source）**：host 和 device 代码写在同一个 `.rs` 文件里，不需要像传统 CUDA C++ 那样把 `__global__` 函数和 host 函数分到不同翻译单元，也不需要写 `#[cfg(cuda_device)]` 之类的条件编译。后端会自己判断。

### 2.4 「包装」与「重写」

一个朴素的想法是：自己从头实现一个完整的 codegen 后端。但这意味着要重新实现 LLVM 后端里成千上万行代码。

cuda-oxide 选了更聪明的路：**包装（wrap）标准的 LLVM 后端**。它内部持有一个 `rustc_codegen_llvm` 后端的实例，绝大多数事情都直接转交给这个「被包装的」后端，只在「需要处理 device 代码」的那一个方法里插入自己的逻辑。这一点会在 4.2 节展开。

> 类比：就像给一个快递公司（LLVM 后端）套了一层代理（cuda-oxide），普通包裹原封不动转交，只有贴了「GPU 专用」标签的包裹才拆出来单独处理。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `crates/rustc-codegen-cuda/src/lib.rs` | 后端 crate 的入口与核心。定义 `__rustc_codegen_backend`、`CudaCodegenBackend` 包装结构体，以及实现 `CodegenBackend` trait 的 `codegen_crate` 分流逻辑。 |
| `crates/rustc-codegen-cuda/src/device_codegen.rs` | 设备代码生成入口。把 rustc 内部 MIR 桥接到 `mir_importer` 流水线，产出 `.ll`/`.ptx`。 |
| `crates/rustc-codegen-cuda/src/collector.rs` | （辅助引用）从 CGU 里发现 kernel 入口并做调用图可达性收集。本讲只引用它的两个计数函数。 |
| `crates/reserved-oxide-symbols/` | （辅助引用）宏与后端之间唯一的命名契约真源，定义 `cuda_oxide_kernel_246e25db_*` 等前缀。 |

提醒（来自 u1-l2）：`rustc-codegen-cuda` 这个 crate **不在 workspace 内**，因为它是一个被 rustc `dlopen` 的 dylib 插件，靠 `#![feature(rustc_private)]` 访问 rustc 内部 crate。

---

## 4. 核心概念与源码讲解

### 4.1 `__rustc_codegen_backend` 入口

#### 4.1.1 概念说明

这是整个 cuda-oxide 后端的「正门」。它是一个**约定俗成的固定名字**：rustc 在加载任何 codegen 后端动态库时，都只会去找这个叫 `__rustc_codegen_backend` 的符号。找到就调用，拿回一个 `Box<dyn CodegenBackend>`；找不到，整个后端就不可用。

这个函数本身极其简单——它不编译任何东西，只负责「组装」出一个后端对象并返回。但它身上有两个细节值得深究：

- `#[unsafe(no_mangle)]`：告诉 Rust 编译器「不要把这个函数名改成 `_ZN3...__rustc_codegen_backend...` 这样的混淆名」。必须原样保留 `__rustc_codegen_backend`，否则 rustc 的 `dlsym` 找不到。
- 它在内部调用 `rustc_codegen_llvm::LlvmCodegenBackend::new()` 创建被包装的 LLVM 后端——这一步是「包装策略」的起点。

#### 4.1.2 核心流程

源码文件顶部的文档注释把这个加载序列画得很清楚：

```text
rustc -Z codegen-backend=librustc_codegen_cuda.so ...
      │
      ├──▶ dlopen("librustc_codegen_cuda.so")
      ├──▶ dlsym("__rustc_codegen_backend")
      └──▶ __rustc_codegen_backend()
              │
              ├──▶ CudaCodegenConfig::from_env()   读 CUDA_OXIDE_* 环境变量
              ├──▶ rustc_codegen_llvm::LlvmCodegenBackend::new()   创建被包装后端
              └──▶ Return Box<CudaCodegenBackend>
```

注意一个设计上的克制：**入口函数里不打日志**。因为 rustc 在编译依赖树时，会为**每一个** crate 都加载一次后端、调用一次入口函数，如果在这里 `eprintln!`，输出会被依赖 crate 的噪音淹没。真正的日志推迟到 `codegen_crate` 里、确认「这个 crate 确实有设备代码」时才打。

#### 4.1.3 源码精读

入口函数定义在这里：

[crates/rustc-codegen-cuda/src/lib.rs:939-953](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L939-L953) —— 导出固定名字的 `__rustc_codegen_backend`，从环境变量读取配置，创建被包装的 LLVM 后端，组装并返回 `CudaCodegenBackend`。

关键三行：

```rust
#[unsafe(no_mangle)]
pub fn __rustc_codegen_backend() -> Box<dyn CodegenBackend> {
    let config = CudaCodegenConfig::from_env();
    let llvm_backend = rustc_codegen_llvm::LlvmCodegenBackend::new();
    Box::new(CudaCodegenBackend { config, llvm_backend })
}
```

配置从环境变量读取，定义在 [crates/rustc-codegen-cuda/src/lib.rs:386-412](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L386-L412)：`CUDA_OXIDE_VERBOSE`、`CUDA_OXIDE_DUMP_MIR`、`CUDA_OXIDE_PTX_DIR`、`CUDA_OXIDE_DEVICE_CODEGEN_CRATE` 等。这种「用环境变量传配置、而不是塞进 rustc 命令行参数」的做法，是为了绕开 rustc 参数解析的复杂性——后端是动态加载的，自己拿环境变量最省事。

> 这个加载机制印证了 u1-l1 / u1-l2 已建立的事实：`rustc-codegen-cuda` 编外于 workspace，正因为它是一个 dylib 插件，且通过 `extern crate rustc_codegen_llvm;`（[lib.rs:318](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L318)）直接依赖并复用标准 LLVM 后端。

#### 4.1.4 代码实践

**实践目标**：亲手确认入口符号确实以未混淆的名字存在于编译产物里。

**操作步骤**（源码阅读 + 可选本地验证）：

1. 在 [lib.rs:939-953](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L939-L953) 定位 `__rustc_codegen_backend`，确认它带 `#[unsafe(no_mangle)]`。
2. 如果本机已按 u1-l3 装好工具链，编译后端产物：
   ```bash
   cd crates/rustc-codegen-cuda
   cargo build
   ```
3. 用 `nm` 检查导出符号（待本地验证）：
   ```bash
   nm -D target/debug/librustc_codegen_cuda.so | grep __rustc_codegen_backend
   ```

**需要观察的现象 / 预期结果**：`nm -D` 的输出里应能直接看到字面量 `__rustc_codegen_backend`，而不是被混淆成 `_ZN...` 的长串。这印证了 `no_mangle` 的作用，也是 rustc `dlsym` 能找到它的根本原因。

> 若本机未配置工具链，则第 2、3 步为「待本地验证」，但第 1 步的源码阅读结论是确定的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `#[unsafe(no_mangle)]` 去掉，会发生什么？

**参考答案**：Rust 编译器会把函数名混淆成带 `_ZN` 前缀的长符号，动态库里就不再存在字面量 `__rustc_codegen_backend`。rustc 用 `dlsym("__rustc_codegen_backend")` 会找不到符号，后端加载失败。

**练习 2**：为什么入口函数里刻意不写任何 `eprintln!` 日志？

**参考答案**：rustc 编译一棵依赖树时，会为每个 crate 都加载并调用一次后端入口。在这里打日志会针对所有依赖 crate 重复触发，产生大量与用户代码无关的噪音。cuda-oxide 把日志推迟到 `codegen_crate`，并加了「仅当本 crate 真有设备代码时才打」的条件。

---

### 4.2 `CudaCodegenBackend` 包装

#### 4.2.1 概念说明

入口函数返回的 `CudaCodegenBackend` 才是真正实现 `CodegenBackend` trait 的对象。它的设计哲学是文档里反复强调的「**包装而非重写（wrap, don't reimplement）**」：

```rust
pub struct CudaCodegenBackend {
    config: CudaCodegenConfig,
    /// The underlying LLVM backend for host code generation
    llvm_backend: Box<dyn CodegenBackend>,
}
```

它内持一个 `llvm_backend` 字段——就是标准的 `rustc_codegen_llvm` 后端实例。trait 的绝大多数方法都**原封不动地转发**给这个字段，只有 `codegen_crate` 这一个方法被改写，用来插入「设备代码处理」逻辑。

这套做法的好处是：宿主代码继续享受成熟、久经考验的 LLVM 后端；cuda-oxide 只在「需要管 GPU」的那个点上插手，维护面积极小。

#### 4.2.2 核心流程

`CodegenBackend` trait 有若干方法。cuda-oxide 对它们的处理可以分成三类：

| 类别 | 方法 | cuda-oxide 的做法 |
|------|------|-------------------|
| 直接转发 | `init` / `target_cpu` / `target_config` / `provide` | 一行调用 `self.llvm_backend.xxx(...)` |
| 包装后转发 | `join_codegen` / `link` | 先注入自己的设备产物对象，再交给 LLVM |
| 完全改写 | `codegen_crate` | 自己实现分流逻辑（见 4.3） |
| 标识自身 | `name` / `print_version` | 返回 `"cuda"`，并打印「wrapping rustc_codegen_llvm」 |

`name()` 返回 `"cuda"` 这一点值得留意：它是 rustc 用来区分后端身份的字符串，也解释了为什么 `cargo oxide` 里到处是 `cuda` 字样。

#### 4.2.3 源码精读

包装结构体定义在 [crates/rustc-codegen-cuda/src/lib.rs:352-356](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L352-L356)。

trait 实现块的起点与几个转发方法在 [crates/rustc-codegen-cuda/src/lib.rs:444-476](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L444-L476)：

```rust
impl CodegenBackend for CudaCodegenBackend {
    fn name(&self) -> &'static str { "cuda" }
    fn init(&self, sess: &Session) {
        // ... 初始化被包装的 LLVM 后端
        self.llvm_backend.init(sess);
    }
    fn target_cpu(&self, sess: &Session) -> String {
        self.llvm_backend.target_cpu(sess)
    }
    fn provide(&self, providers: &mut rustc_middle::util::Providers) {
        self.llvm_backend.provide(providers);   // 直接转发
    }
    // ...
}
```

可以看到转发方法体都极短，就是把调用透传给 `self.llvm_backend`。

`print_version`（[lib.rs:457-463](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L457-L463)）则同时打印自己的版本和被包装 LLVM 后端的版本，明确告知用户「这是一个套壳」：

```rust
fn print_version(&self) {
    println!("rustc_codegen_cuda version {} (wrapping rustc_codegen_llvm)",
             env!("CARGO_PKG_VERSION"));
    self.llvm_backend.print_version();
}
```

> 真正「有内容」的方法是 `codegen_crate`（4.3 节）与 `join_codegen`（[lib.rs:744-768](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L744-L768)，把设备产物对象塞进 `CompiledModules`）。其余全是转发。

#### 4.2.4 代码实践

**实践目标**：动手把 `CodegenBackend` 的方法分成「转发 / 包装 / 改写」三类，体会包装策略的「小维护面积」。

**操作步骤**（源码阅读型）：

1. 打开 [crates/rustc-codegen-cuda/src/lib.rs:444-781](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L444-L781)，这是完整的 `impl CodegenBackend for CudaCodegenBackend` 块。
2. 逐个方法判断：方法体里有没有出现 `self.llvm_backend.` 之外的实质性逻辑？
   - 没有额外逻辑 → 纯转发（`init` / `target_cpu` / `target_config` / `provide` / `link`）。
   - 有额外逻辑但最终仍调用 LLVM → 包装（`join_codegen`：先 push 设备产物对象，再调 `llvm_backend.join_codegen`）。
   - 完全自己实现分流 → 改写（`codegen_crate`）。

**预期结果**：你会得到一张表，绝大多数方法是转发，只有 `codegen_crate` 与 `join_codegen` 真正承载 cuda-oxide 的逻辑。这正是「包装策略」把改动面收敛到极小的证据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `target_cpu` / `target_config` 这些方法可以直接转发给 LLVM 后端？

**参考答案**：这些方法描述的是「宿主目标」的 CPU 与目标配置信息（例如 x86_64 的 CPU 特性），而宿主代码确实由被包装的 LLVM 后端生成。cuda-oxide 关心的是 GPU 目标（`sm_80` 等），那是由设备流水线自己通过 `CUDA_OXIDE_TARGET` 处理的，与这些「宿主目标」查询方法无关，故直接转发即可。

**练习 2**：`join_codegen`（[lib.rs:744-768](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L744-L768)）为什么不能简单转发，而要先 `compiled_modules.modules.push(...)`？

**参考答案**：设备代码生成阶段产出了一个内嵌 `.oxart` 制品的 object 文件（4.3 节会讲）。这个 object 必须参与最终的链接，否则 PTX 不会进到二进制里。`join_codegen` 在把控制权交还 LLVM 之前，把这个设备产物 object 以 `CompiledModule` 的形式追加到模块列表，确保后续 `link` 阶段能链接它。

---

### 4.3 `codegen_crate` 分流

#### 4.3.1 概念说明

`codegen_crate` 是整个后端的「大脑」，也是本讲最重要的方法。rustc 每编译一个 crate，都会调用一次后端的 `codegen_crate(tcx, crate_info)`。

这里有一个极其重要、且容易误解的点，必须先讲清楚：

> **cuda-oxide 的 host/device「分流」并不是「这个函数给 host、那个函数给 device」的二选一。** 真实模型是「**叠加**」：宿主代码**永远**走标准 LLVM 后端生成 x86_64 机器码；此外，如果这个 crate 里检测到设备代码，就**额外**把设备可达函数抽出来编成 PTX。

也就是说，一个 `#[kernel]` 函数，它的 MIR 会被 LLVM 后端编成宿主 object 里的一个（在宿主侧永远不会被调用的）函数，**同时**会被 cuda-oxide 抽出来编成 PTX。两条路径是并行叠加的，不是互斥分支。

`codegen_crate` 真正在做的「决策」其实只有一个：**这个 crate 要不要额外触发设备流水线？** 这个决策由两个条件相与决定：

1. 这个 crate 里是否检测到设备代码（有 kernel 或有 `#[device]` 函数）；
2. 是否通过了 owner 过滤（`CUDA_OXIDE_DEVICE_CODEGEN_CRATE`）。

#### 4.3.2 核心流程

设备代码的「检测」依赖保留命名约定（u2-l1 已建立）：`#[kernel]` 宏把函数改名为 `cuda_oxide_kernel_246e25db_*`，`#[device]` 宏加上 `cuda_oxide_device_246e25db_*` 前缀。后端扫描各 CGU（codegen unit，rustc 单态化后的函数实例集合）里的函数名，用 `is_kernel_symbol` / `is_device_symbol` 判断。

`codegen_crate` 的整体流程（行号见 4.3.3）：

```text
codegen_crate(tcx, crate_info)
  │
  ├─ 1. tcx.collect_and_partition_mono_items()   拿到单态化后的 CGU
  ├─ 2. count_kernels_in_cgus(...)               数 kernel 个数
  ├─ 3. count_device_fns_in_cgus(...)            数 #[device] 函数个数
  ├─ 4. owner_selected = allows_device_codegen_for(crate_name)   owner 过滤
  ├─ 5. has_device_code = contains_device_code && owner_selected 决策
  │
  ├─ if has_device_code:                          ◄── 条件触发设备流水线
  │     ├─ collector::collect_device_functions()   调用图可达性收集
  │     └─ device_codegen::generate_device_code()  → PTX，并产出 .oxart object
  │
  └─ llvm_backend.codegen_crate(tcx, crate_info)  ◄── 永远执行：宿主代码全交给 LLVM
```

最后一步 `llvm_backend.codegen_crate(...)` 是**无条件**的，这就是「宿主代码永远走 LLVM」这条不变量的代码体现。

至于「设备可达函数集合」如何确定，它本质是调用图上的一个不动点（最小不动点 lfp）：从 kernel 入口集合 \(K\) 出发，不断并入每个已收集函数的合规被调用者，直到不再增长：

\[
D = \mathrm{lfp}\!\left(\, S \;\mapsto\; S \;\cup\; \bigcup_{f \in S} \mathrm{callees}_{\,\mathrm{ok}}(f) \,\right)(K)
\]

其中 \(\mathrm{callees}_{\,\mathrm{ok}}(f)\) 表示 \(f\) 的、且通过了 `should_collect` 过滤（来自 local crate / `cuda_device` / `core` 等，排除 `std`）的被调用者。这个不动点的展开就是 collector 的 worklist 算法，4.4 节会用到它的产物 `Vec<CollectedFunction>`。

#### 4.3.3 源码精读

`codegen_crate` 的签名与文档在 [crates/rustc-codegen-cuda/src/lib.rs:478-505](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L478-L505)，方法体在 [crates/rustc-codegen-cuda/src/lib.rs:505-742](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L505-L742)。

**第一步：检测与决策**（[lib.rs:510-522](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L510-L522)）：

```rust
let mono_partitions = tcx.collect_and_partition_mono_items(());
let kernel_count = collector::count_kernels_in_cgus(tcx, mono_partitions.codegen_units);
let device_fn_count = collector::count_device_fns_in_cgus(tcx, mono_partitions.codegen_units);
let crate_name = tcx.crate_name(rustc_hir::def_id::LOCAL_CRATE);
let owner_selected = self.config.allows_device_codegen_for(crate_name.as_str());
let contains_device_code = kernel_count > 0 || device_fn_count > 0;
let has_device_code = should_codegen_device_crate(
    &self.config, crate_name.as_str(), contains_device_code);
```

两个计数函数定义在 collector：[collector.rs:310-323](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/collector.rs#L310-L323)（`count_kernels_in_cgus`）与 [collector.rs:329-342](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/collector.rs#L329-L342)（`count_device_fns_in_cgus`），它们遍历每个 CGU 的 mono item，用 `is_kernel_function` / `is_device_function`（底层是 `reserved_oxide_symbols` 里的前缀谓词，见 [collector.rs:160-163](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/collector.rs#L160-L163)）判断。

owner 过滤的决策函数在 [lib.rs:436-442](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L436-L442)：

```rust
fn should_codegen_device_crate(config, crate_name, contains_device_code) -> bool {
    contains_device_code && config.allows_device_codegen_for(crate_name)
}
```

它把「有设备代码」和「通过 owner 过滤」两个条件相与。当未设置 `CUDA_OXIDE_DEVICE_CODEGEN_CRATE` 时，`allows_device_codegen_for` 对所有 crate 都返回 `true`（见 [lib.rs:414-418](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L414-L418)），此时决策退化为「只要本 crate 有设备代码就触发」。

**第二步：条件触发设备流水线**（[lib.rs:568-730](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L568-L730)）：当 `has_device_code` 为真时，先调用 `collector::collect_device_functions(...)` 做调用图可达性收集，再调用 `device_codegen::generate_device_code(...)`。注意这里把整个设备流水线包在 `catch_unwind` 里（[lib.rs:625-647](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L625-L647)），目的是把 pipeline 内部的 panic（通常是 pliron 的 IR 不变式检查失败）拦截下来，重新报告成「这是 cuda-oxide 的 bug，请到我们的 issue tracker 反馈」，而不是让它逃逸成「rustc 意外崩溃，请报给 rustc」。

**第三步：宿主代码无条件交给 LLVM**（[lib.rs:732-734](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L732-L734)）：

```rust
// Step 3: Delegate ALL host codegen to LLVM backend
let host_result = self.llvm_backend.codegen_crate(tcx, crate_info);
```

这一行无论 `has_device_code` 真假都会执行——这就是「宿主代码永远走 LLVM」不变量的落点。最后把宿主结果和设备产物 object 一起打包进 `CudaOngoingCodegen`（[lib.rs:737-741](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L737-L741)），供 `join_codegen` 取用。

#### 4.3.4 代码实践

**实践目标**：在源码里精确定位「分流决策点」与「宿主委托点」，并解释为什么说这是「叠加」而非「互斥」。

**操作步骤**（源码阅读型 + 可选本地验证）：

1. 在 [lib.rs:505-742](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L505-L742) 找到三处关键位置：
   - 决策变量 `has_device_code` 的计算（[lib.rs:518-522](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L518-L522)）；
   - 条件触发设备流水线的 `if has_device_code { ... }`（[lib.rs:568](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L568)）；
   - 无条件委托宿主代码的 `self.llvm_backend.codegen_crate(...)`（[lib.rs:734](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L734)）。
2. 用 `CUDA_OXIDE_VERBOSE=1` 编译一个最小内核（如 vecadd），观察 stderr（待本地验证）：
   ```bash
   CUDA_OXIDE_VERBOSE=1 cargo oxide build vecadd
   ```
3. 体会叠加模型：注意 `#[kernel] fn vecadd` 既会被 LLVM 编进宿主 object，又会被抽出来编成 PTX。

**需要观察的现象 / 预期结果**：verbose 日志里应能看到 `[rustc_codegen_cuda] Compiling crate '...': ... kernel(s), ... device fn(s)`，紧接着 `Compiling device code via cuda-oxide...`。同时宿主 object 照常由 LLVM 生成。两条路径都发生了——这就是「叠加」。

> 若本机未配置工具链，则第 2 步为「待本地验证」，但第 1 步的源码定位结论是确定的。

#### 4.3.5 小练习与答案

**练习 1**：假设一个 crate 里没有任何 `#[kernel]` 也没有任何 `#[device]` 函数（比如一个纯宿主的依赖 crate），`codegen_crate` 会做什么？

**参考答案**：`count_kernels_in_cgus` 与 `count_device_fns_in_cgus` 都返回 0，`contains_device_code` 为假，`has_device_code` 为假，于是跳过整个设备流水线分支，只执行最后一步 `llvm_backend.codegen_crate(...)`。对依赖 crate 来说，cuda-oxide 后端的行为与标准 LLVM 后端**完全一致**——这正是包装策略的低开销体现。

**练习 2**：为什么设备 codegen 的错误（`Ok(Err(e))` 分支，[lib.rs:715-727](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L715-L727)）要被硬提升为 `tcx.dcx().fatal(...)`，而不是安静地返回？

**参考答案**：如果设备 codegen 失败被吞掉，宿主 LLVM 后端仍会成功生成一个二进制，`cargo oxide` 的包装脚本会误报「✓ Build succeeded」。但这个二进制里没有正确的 PTX，运行时会在 GPU 上静默跑错。把设备错误提升为 rustc fatal，能让 cargo 以非零退出码失败，阻止虚假的成功提示——「编译器缺口即 bug」。

**练习 3**：`CUDA_OXIDE_DEVICE_CODEGEN_CRATE` 这个 owner 过滤解决了什么问题？

**参考答案**：在多 crate 项目里，可能多个 crate 都含 `#[kernel]`，但你只想让特定 crate 触发设备 codegen（其余 crate 的 kernel 会在最终 bin crate 被单态化时再处理）。owner 过滤让你显式指定哪些 local crate 名是「设备代码所有者」，避免重复生成、也用于配合新版宏的 target-specific 锚符号机制（[lib.rs:532-554](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L532-L554) 那段 legacy 锚兜底逻辑正是为此而设）。

---

### 4.4 `device_codegen` 设备路径入口

#### 4.4.1 概念说明

当 4.3 节决定「这个 crate 要触发设备流水线」后，真正的设备代码生成入口是 `device_codegen::generate_device_code(...)`。这个模块要解决的核心问题，文档里叫**「桥接问题（The Bridge Problem）」**。

问题在于存在两套不同的 MIR 表示：

| API | 使用方 | 类型 |
|------|--------|------|
| `rustc_middle`（内部） | rustc 内部、本后端 | `rustc_middle::ty::Instance<'tcx>` |
| `rustc_public`（stable MIR） | mir-importer 流水线 | `rustc_public::mir::mono::Instance` |

cuda-oxide 的 mir-importer 流水线当初是用更稳定的 `rustc_public`（stable MIR）API 写的——因为 rustc 内部 API 变动频繁，stable MIR 变动小。但作为 codegen 后端，我们从 rustc 拿到的是 `rustc_middle` 的内部类型。`device_codegen` 这个模块就是这两套类型之间的**转换层**。

#### 4.4.2 核心流程

`generate_device_code` 内部三步走：

```text
输入: Vec<CollectedFunction<'tcx>>   （rustc_middle 类型）
  │
  ├─ STEP 1: 进入 stable_mir 上下文
  │     rustc_internal::run(tcx, || { ... })   建立 Tables / CompilerCtxt
  │
  ├─ STEP 2: 逐个转换 Instance
  │     rustc_internal::stable(func.instance)   rustc_middle → rustc_public
  │
  └─ STEP 3: 运行 cuda-oxide 流水线
        mir_importer::run_pipeline(&stable_functions, &stable_device_externs, &config)
          │
          ├─ Rust MIR → dialect-mir (alloca 形式)
          ├─ dialect-mir → dialect-mir (mem2reg → SSA)
          ├─ 标注式循环展开
          ├─ dialect-mir → LLVM dialect (mir-lower)
          ├─ LLVM dialect → 文本 LLVM IR (.ll)
          └─ LLVM IR → PTX (.ptx) via llc
```

`STEP 1` 必须用一个闭包 `rustc_internal::run(tcx, || {...})` 包起来，因为 `stable()` 转换依赖一套线程局部的 `Tables` 与 `CompilerCtxt`，只有进了这个上下文才能用。`STEP 2` 是真正「跨类型边界」的转换，每个函数一次，开销可忽略。`STEP 3` 之后的事（dialect-mir、lowering、PTX）属于后续讲义 u4-l2 ~ u4-l5 的范畴，本讲只到「把球传给 `mir_importer::run_pipeline`」为止。

设计动机（文档明确列出）：复用已成熟测试的 mir-importer、享受 stable MIR 的稳定性、用约 100 行桥接代码换「不必重写整个流水线」。

#### 4.4.3 源码精读

`generate_device_code` 的定义与完整文档在 [crates/rustc-codegen-cuda/src/device_codegen.rs:414-456](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L414-L456)，函数体在 [device_codegen.rs:451-752](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L451-L752)。

**进入 stable_mir 上下文并转换 Instance**（[device_codegen.rs:635-657](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L635-L657)）：

```rust
let result = rustc_internal::run(tcx, || {
    let stable_functions: Vec<mir_importer::CollectedFunction> = functions
        .iter().zip(export_names.iter()).zip(debug_scope_maps.iter())
        .zip(inline_always_flags.iter())
        .map(|(((func, (export_name, is_kernel)), debug_source_scopes), is_inline_always)| {
            // 关键桥接：rustc_middle::Instance → rustc_public::Instance
            let stable_instance = rustc_internal::stable(func.instance);
            mir_importer::CollectedFunction {
                instance: stable_instance,
                is_kernel: *is_kernel,
                export_name: export_name.clone(),
                debug_source_scopes: Some(debug_source_scopes.clone()),
                is_inline_always: *is_inline_always,
            }
        }).collect();
    // ...
});
```

`rustc_internal::stable(func.instance)` 这一行就是「桥接」的全部——把 `rustc_middle::ty::Instance<'tcx>` 变成 mir-importer 能吃的 `rustc_public::mir::mono::Instance`。

**运行流水线**（[device_codegen.rs:681-699](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L681-L699)）：组装好 `PipelineConfig`（含输出目录、目标架构、FMA 收缩策略等），然后：

```rust
mir_importer::run_pipeline(&stable_functions, &stable_device_externs, &pipeline_config)
```

这一行之后，控制权就交给了 mir-importer，正式进入 U4 后续讲义的领域。

值得注意的两个配置细节：

- 目标架构来自 `CUDA_OXIDE_TARGET`（硬覆盖，[device_codegen.rs:673](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L673)）与 `CUDA_OXIDE_DEVICE_ARCH`（建议性 hint，[device_codegen.rs:674](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L674)），呼应 u1-l5 讲过的架构优先级链。
- FMA 收缩策略由 `CUDA_OXIDE_NO_FMA` 决定（[device_codegen.rs:675](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L675)）：环境变量未设时 `allow_fma_contraction = true`，向下传给流水线，最终影响 LLVM IR 是否把乘加收缩成 fused 指令。这条线索会在 u4-l5 与 u6-l3 详细展开。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「Instance 跨类型边界」的转换，并定位「球被传给 mir-importer」的那一行。

**操作步骤**（源码阅读型）：

1. 在 [device_codegen.rs:451](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L451) 找到 `generate_device_code` 入口。
2. 往下找到 [device_codegen.rs:635](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L635) 的 `rustc_internal::run(tcx, || { ... })`，理解它建立 stable_mir 上下文。
3. 在闭包内定位 [device_codegen.rs:646](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L646) 的 `rustc_internal::stable(func.instance)`——这就是类型边界跨越点。
4. 再往下到 [device_codegen.rs:698](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L698) 的 `mir_importer::run_pipeline(...)`——从这里起，后续所有工作都不在 `rustc-codegen-cuda` 这个 crate 内了。

**预期结果**：你能画出一条从 `CollectedFunction<'tcx>`（rustc_middle 类型）到 `mir_importer::CollectedFunction`（stable_mir 类型）再到流水线入口的最短路径，并说清楚「类型转换」只发生在 `stable()` 这一处。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接在 mir-importer 里用 `rustc_middle` 的内部类型，从而省掉 `device_codegen` 这个桥接层？

**参考答案**：mir-importer 设计时选择了 stable MIR（`rustc_public`），因为它比 `rustc_middle` 内部 API 稳定得多——rustc 内部 API 每个 nightly 都可能变。用一个约 100 行的桥接层换来 mir-importer 的长期稳定性与可测试性，是值得的。代价仅是每个函数一次 `stable()` 转换，相对编译总耗时可忽略。

**练习 2**：`generate_device_code` 在调用 `run_pipeline` 之前，除了转换 Instance，还预先计算了什么「stable_mir 不暴露」的信息？（提示：看 [device_codegen.rs:624-633](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L624-L633)）

**参考答案**：它预先从 `rustc_middle::TyCtxt` 查询了每个函数的 `#[inline(always)]` 标志（`tcx.codegen_fn_attrs(def_id).inline`），因为该查询只在内部 `TyCtxt` 上有、stable_mir 没暴露。必须在进入 stable_mir 上下文之前算好，作为 `is_inline_always` 字段随函数一起传进去，确保内联提示不会完全依赖下游优化器的启发式。

---

## 5. 综合实践

本讲的实践任务是**画一张「一颗 crate 的代码生成分流图」**，把本讲四个最小模块串起来。

### 任务背景

考虑一个最小的单 crate 程序，里面有三类函数：

```rust
// 示例代码（仅用于说明，非项目原有）
fn helper(x: f32) -> f32 { x * 2.0 }      // 普通宿主函数

#[kernel]
fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) { ... }  // 设备入口

fn main() {
    // 分配显存、启动 vecadd、回收结果
}
```

### 你要做的

1. **定位分流点**：在 [lib.rs:505-742](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L505-L742) 的 `codegen_crate` 里，标出「决策变量 `has_device_code` 的计算」「条件触发设备流水线的 `if`」「无条件委托宿主的 `llvm_backend.codegen_crate`」三处，各写一行行号。

2. **画出分流图**：画一张图，画出 `helper`、`vecadd`、`main` 这三个函数（以及 vecadd 在设备端可能调用的、来自 `cuda_device`/`core` 的被调用者）分别经过哪条代码生成路径。要求图里能回答：
   - 谁会被 LLVM 编进宿主 object？（答：三者都会——这是「叠加」模型的关键。）
   - 谁会被 collector 收集、进入 cuda-oxide 流水线？（答：`vecadd` 及其可达的设备被调用者；`helper` 与 `main` 不会，除非它们从 kernel 可达。）
   - `vecadd` 的 MIR 在哪一行跨越了 `rustc_middle` → `rustc_public` 的类型边界？（答：[device_codegen.rs:646](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L646) 的 `rustc_internal::stable(...)`。）

3. **可选验证**（待本地验证）：用 `CUDA_OXIDE_VERBOSE=1 cargo oxide build vecadd` 观察 stderr，确认日志顺序与你的图一致：先看到 kernel/device-fn 计数，再看到 `Compiling device code via cuda-oxide...`，最后宿主 object 照常生成。

### 预期成果

一张标注了行号的分流图 + 三句话结论，分别说明「叠加而非互斥」「设备集合由调用图可达性决定」「类型边界跨越发生在 stable() 一处」。这张图就是后续 U4 讲义（importer、方言层、lowering）的「入口锚点」——它们都是图中「cuda-oxide 流水线」那一格的内部展开。

## 6. 本讲小结

- cuda-oxide 后端是一个被 rustc `dlopen` 的 dylib 插件，正门是固定名字 `__rustc_codegen_backend`（[lib.rs:939-953](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L939-L953)），靠 `#[unsafe(no_mangle)]` 保证 rustc 的 `dlsym` 能找到。
- `CudaCodegenBackend`（[lib.rs:352-356](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L352-L356)）用「包装而非重写」策略内持一个标准 LLVM 后端，绝大多数 trait 方法直接转发，只改写 `codegen_crate`。
- 分流发生在 `codegen_crate`（[lib.rs:505-742](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L505-L742)），且是**叠加**模型：宿主代码**永远**走 LLVM（[lib.rs:734](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L734)），设备代码**额外**触发 cuda-oxide 流水线。
- 是否触发设备流水线由「检测到 kernel/device 函数」与「owner 过滤（`CUDA_OXIDE_DEVICE_CODEGEN_CRATE`）」两个条件相与决定（[lib.rs:436-442](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/lib.rs#L436-L442)）。
- 设备路径入口 `device_codegen::generate_device_code`（[device_codegen.rs:451](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L451)）是 `rustc_middle` MIR 与 `rustc_public` stable MIR 之间的桥接层，核心转换是 `rustc_internal::stable(...)`，随后把球传给 `mir_importer::run_pipeline`。
- 设备 codegen 的错误被硬提升为 rustc fatal，pipeline 的 panic 被 `catch_unwind` 拦截重报为 cuda-oxide bug，二者共同保证「不会生成一个静默跑错的二进制」。

## 7. 下一步学习建议

本讲止步于「球被传给 `mir_importer::run_pipeline`」的那一刻。接下来：

- **u4-l2 MIR 导入器鸟瞰**：进入 `mir-importer`，看它如何把 stable MIR 的基本块、语句、终止符翻译成 `dialect-mir` IR，理解 `run_pipeline` 内部的分阶段处理。这是图中「cuda-oxide 流水线」第一格的内部展开。
- **u4-l3 MLIR 方言层**：认识 `dialect-mir` 与 `dialect-nvvm` 两层方言的分工，理解从 Rust 语义到 GPU 指令的过渡。
- **u4-l4 / u4-l5 Lowering 与到 cubin**：顺着 lowering 链一直追到 `.ll`/`.ptx` 与 NVVM IR/LTOIR/cubin 的分叉，并理解本讲埋下的 FMA 收缩策略（`CUDA_OXIDE_NO_FMA`）是如何一路传到最终机器码的。

如果你想先看「设备函数是怎么被一颗颗收集出来的」，可以暂离 U4，去读 `crates/rustc-codegen-cuda/src/collector.rs` 的 worklist 算法——它就是 4.3.2 里那个不动点 \(D\) 的实现。但回到主线时，请从 u4-l2 继续。
