# 作用域原子与打包原子（f16x2 / bf16x2）

## 1. 本讲目标

本讲深入 cuda-oxide 的**设备端原子操作模型**。读完后你应当能够：

- 说清 GPU 原子操作的**作用域（scope）**与**内存序（ordering）**两个正交维度，并把它们对应到具体的 PTX 限定符；
- 区分**整型原子**与**浮点原子**各自支持的操作集合，理解为何浮点原子没有 CAS；
- 掌握**打包原子加法** `atom_add_f16x2` / `atom_add_bf16x2` 的 lane 布局、`.noftz` 语义与最低架构要求；
- 准确说出打包原子操作中** lane 级原子性**的边界，以及哪些「看起来合理」的混用是未定义行为（UB）。

本讲承接 u2-l3（共享内存与同步）中关于 `sync_threads` / `threadfence` 的可见性讨论，以及 u2-l5（设备内存与数据搬运）中关于 `DeviceBuffer` 与 `DisjointSlice` 的内存模型。本讲关注的是**多个线程（甚至多个 block、CPU 与 GPU）同时读写同一内存单元**时的正确性。

## 2. 前置知识

### 2.1 什么是原子操作

当多个线程同时对同一地址做「读—改—写」（Read-Modify-Write，简称 RMW）时，若不加保护，会出现丢失更新。例如两个线程各执行 `*counter += 1`，编译后会展开为「加载 → 加 1 → 存储」三步，两线程的三步可能交错，导致最终只加了 1 而非 2。

**原子操作**把「读—改—写」封装成一条不可分割的指令，保证全程没有其他线程插入。CUDA 在 PTX 层提供 `atom.*` 系列指令实现这一保证。

### 2.2 原子 ≠ 可见性 ≠ 顺序

这是本讲最容易混淆的三件事：

- **原子性（atomicity）**：单条 RMW 不会被打断。
- **可见性（visibility）**：一个线程的写何时对其他线程可见。
- **顺序性（ordering）**：本线程其它（非原子）读写与原子操作之间的相对顺序。

**内存序（memory ordering）**正是用来同时表达「可见性」与「顺序性」的工具。GPU 上还多出一个 CPU 上不存在的维度——**作用域（scope）**：一条原子操作保证对「哪些线程」可见。本讲的核心就是把这三个概念在 PTX 层面讲清楚。

### 2.3 cuda-oxide 的桩函数（stub）模式

在进入正题前，必须先理解 cuda-oxide 设备端 API 的统一实现策略。`cuda-device` 中几乎所有 GPU 原语（线程索引、共享内存、栅栏、本讲的原子）在 Rust 源码层都是 `unreachable!()` 占位桩——它们的存在只是为了让类型检查器和借用检查器在**开发期**正常工作；真正的逻辑由 cuda-oxide 编译器**按方法名识别**后，在 lowering 阶段替换成正确的 LLVM/PTX 指令。换言之，这些方法体**永远不会被执行**。这一点会在 4.2.3 反复看到。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/cuda-device/src/atomic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs) | 设备端原子操作的**全部类型定义**：作用域前缀、内存序枚举、整型/浮点两个声明宏、打包原子加法两个函数。是本讲的主战场。 |
| [crates/rustc-codegen-cuda/examples/atomics/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs) | 整型/浮点原子的 20 项端到端测试，覆盖三种作用域、五种内存序、CAS、位运算、min/max、以及 `core::sync::atomic` 路径。 |
| [crates/rustc-codegen-cuda/examples/atomic_f16/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomic_f16/src/main.rs) | f16 半精度浮点原子的直方图测试，演示 `fetch_add` / `fetch_sub` / `swap` / `load`+`store`。 |
| [crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs) | 打包 `atom_add_f16x2` / `atom_add_bf16x2` 的端到端示例，本讲「打包原子」与「lane 级 UB」两个模块的依据。 |

补充引用（用于说明桩函数如何被编译器识别并 lowering）：

| 文件 | 作用 |
|------|------|
| [crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs) | 把原子方法调用翻译成 NVVM 方言 op，从类型名解析作用域、从方法名解析 RMW 种类。 |
| [crates/mir-lower/src/convert/intrinsics/atomic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs) | 把 NVVM 原子 op 降级为标准 LLVM 原子指令，含作用域→`syncscope`、内存序→ordering 的映射，以及 `atomicrmw` 的栅栏拆分（fence splitting）变通。 |

## 4. 核心概念与源码讲解

### 4.1 原子作用域与内存序

#### 4.1.1 概念说明

GPU 原子操作有两个**正交**的维度：

1. **作用域（scope）**——回答「这条原子操作保证对**哪些线程**可见」。这是 GPU 特有的维度，因为一颗 GPU 上有数千个线程，跨 SM（流多处理器）的缓存一致性代价很高。让每条原子操作自己声明「我只需要对谁可见」，就能在不需要全局一致时省掉昂贵的跨 SM 同步。

2. **内存序（ordering）**——回答「这条原子操作如何约束**其它（含非原子）读写的顺序与可见性**」。这与 C++/Rust 标准库的内存序概念一致。

cuda-oxide 的设计哲学是：**把作用域编进类型名，把内存序作为运行期参数**。这样作用域在类型层就被钉死，不会因为传错参数而误用廉价作用域；内存序则可以在每次调用时按需选择。

#### 4.1.2 核心流程

cuda-oxide 用三个前缀编码三种作用域：

| 前缀 | PTX 作用域 | 哪些线程能观察到 |
|------|-----------|------------------|
| `DeviceAtomic*` | `.gpu` | 整颗 GPU 上的所有线程 |
| `BlockAtomic*`  | `.cta` | 仅同一线程块（thread block）内的线程 |
| `SystemAtomic*` | `.sys` | GPU **和** CPU（统一内存/HMM 场景） |

五种内存序及其代价：

| 变体 | PTX 限定符 | 代价 |
|------|-----------|------|
| `Relaxed` | `.relaxed` | 最便宜，只要原子性、不要排序 |
| `Acquire` | `.acquire` | 低（仅 load / fetch_* / CAS）|
| `Release` | `.release` | 低（仅 store / fetch_* / CAS）|
| `AcqRel`  | `.acq_rel` | 中（仅 fetch_* / CAS）|
| `SeqCst`  | `fence.sc` + 操作 | 最高，全局单一总序 |

调用一条 `DeviceAtomicU32::fetch_add` 时，从源码到 PTX 的数据流是：

```text
类型名 DeviceAtomicU32  ──►  作用域 = Device = .gpu   (类型层固定)
方法名 fetch_add        ──►  RMW 种类 = Add           (方法名固定)
参数 order = Relaxed    ──►  内存序 = .relaxed         (调用处选择)
                              │
                              ▼
              mir-importer 翻译成 NvvmAtomicRmwOp
                              │
                              ▼
              mir-lower 降级成 atomicrmw ... syncscope("device")
                              │
                              ▼
              llc 产出  atom.global.relaxed.add.u32 ...  (.gpu 作用域)
```

注意作用域是**编译期固定**的（编在类型名里），内存序是**调用期选择**的（参数）。下面看源码。

#### 4.1.3 源码精读

`atomic.rs` 顶部的模块文档用两张表把作用域与内存序的契约写死。先看作用域表——它定义了三个前缀对应的 PTX scope 与可见范围：

[crates/cuda-device/src/atomic.rs:L13-L20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L13-L20) —— 三种作用域前缀与 PTX scope 的对应表。

接着是内存序表：

[crates/cuda-device/src/atomic.rs:L22-L32](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L22-L32) —— 五种内存序变体、对应 PTX 限定符与相对代价。

内存序在源码中是一个 `#[repr(u8)]` 枚举，判别值固定为 0–4，编译器据此选择 PTX ordering：

[crates/cuda-device/src/atomic.rs:L100-L118](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L100-L118) —— `AtomicOrdering` 枚举定义，注释明确「判别值必须与 mir-importer 保持同步」。

注意两个命名上的巧思（避免与标准库撞名）：

[crates/cuda-device/src/atomic.rs:L56-L65](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L56-L65) —— 设备作用域类型命名为 `DeviceAtomic*`（而非 `Atomic*`），内存序枚举命名为 `AtomicOrdering`（而非 `Ordering`），从而可以与 `core::sync::atomic::{AtomicU32, Ordering}` 在同一 crate 共存。

这种「共存」在 `atomics` 示例里被直接用上——宿主侧的 host 代码导入 `core::sync::atomic::Ordering`，设备内核里同时用 `cuda_device::atomic::AtomicOrdering`，二者各走各的 lowering 路径：

[crates/rustc-codegen-cuda/examples/atomics/src/main.rs:L39-L44](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L39-L44) —— 同一文件同时导入标准库 `Ordering` 与设备端 `AtomicOrdering` 等类型，靠命名隔离避免冲突。

mir-importer 的文档把「类型名 → 作用域」的解析规则再确认了一遍：

[crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs:L17-L32](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs#L17-L32) —— 从 `BlockAtomicI64::fetch_add` 这样的调用路径里，靠前缀解析作用域、靠元素类型解析位宽与符号。

mir-lower 再把作用域映射成 LLVM 的 `syncscope` 字符串：

[crates/mir-lower/src/convert/intrinsics/atomic.rs:L40-L46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs#L40-L46) —— `Device`→`"device"`（即 `.gpu`）、`Block`→`"block"`（即 `.cta`）、`System`→默认（即 `.sys`）。

> 名词解释：**syncscope** 是 LLVM IR 里给原子操作标注可见范围的字符串，NVPTX 后端会把它翻译成 PTX 的 `.gpu` / `.cta` / `.sys`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「作用域编在类型名里、内存序作为参数」这一设计。

**操作步骤**：

1. 打开 [atomics/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs)。
2. 找到 Test 18（`atomic_block_scope_test`，约第 485 行）与 Test 1（`atomic_fetch_add_test`，约第 60 行）。
3. 对比两者：Test 1 把同一地址 reinterpret 成 `DeviceAtomicU32`，Test 18 reinterpret 成 `BlockAtomicU32`。两者的 `fetch_add(..., AtomicOrdering::Relaxed)` 调用在源码上只差一个类型名。
4. 把 Test 18 内核里的 `BlockAtomicU32` 改成 `DeviceAtomicU32`（其余不动），重新 `cargo oxide run atomics`（需要 GPU；若只想确认编译可改用 `cargo oxide build atomics`）。
5. 用 `cargo oxide pipeline atomics` 找到生成的 `.ptx` 文件，搜索 Test 18 与 Test 1 对应的 `atom.` 指令。

**需要观察的现象**：Test 18 的 PTX 里 `atom` 指令应带 `.cta` 作用域，Test 1 应带 `.gpu`（或不带作用域限定，因为 `.gpu` 是默认）。改类型名后，Test 18 的 PTX 应当从 `.cta` 变成 `.gpu`。

**预期结果**：作用域确实由类型名决定、与调用处的内存序参数无关。运行结果两者都应 `counter = 256`，因为单 block 启动下两种作用域都足够。

> 若本机无 GPU 或无 `cargo oxide` 环境，「待本地验证」PTX 文本中的作用域限定符差异；类型名与作用域的对应关系本身可直接从源码确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DeviceAtomicU32` 的命名不像标准库那样叫 `AtomicU32`？

**答案**：为了让设备端原子类型能和 `core::sync::atomic::AtomicU32` 在同一文件共存而不冲突；同时前缀 `Device`/`Block`/`System` 直接编码了作用域，使作用域成为类型层契约而非可传错的运行期参数。详见 [atomic.rs:L56-L60](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L56-L60)。

**练习 2**：一个跨多个线程块（CTA）累加的全局计数器，应该用 `DeviceAtomicU32` 还是 `BlockAtomicU32`？为什么？

**答案**：必须用 `DeviceAtomicU32`（`.gpu`）。`BlockAtomic*`（`.cta`）只保证同块线程可见，跨块访问同一地址是 UB（见 [atomic.rs:L483-L486](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L483-L486)）。`atomics` 示例的 Test 7（`atomic_multiblock_test`，4 块 × 64 线程）正是用 `DeviceAtomicU32` 跨 CTA 累加到 256。

---

### 4.2 整型与浮点原子类型

#### 4.2.1 概念说明

cuda-oxide 用两个声明宏批量生成原子类型：

- `define_integer_atomic!`：生成**整型**原子，支持全套操作——`load` / `store` / `fetch_add` / `fetch_sub` / `fetch_and` / `fetch_or` / `fetch_xor` / `fetch_min` / `fetch_max` / `swap` / `compare_exchange`。
- `define_float_atomic!`：生成**浮点**原子，**只**支持 `load` / `store` / `fetch_add` / `fetch_sub` / `swap`。

浮点原子少这么多操作，不是 cuda-oxide 偷懒，而是 **PTX 硬件本身**的限制：浮点没有 `atom.cas`（因此无法实现 CAS），位运算对浮点位模式无意义（因此没有 `fetch_and/or/xor`）。这一点在源码注释里写得很直白：

[crates/cuda-device/src/atomic.rs:L33-L41](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L33-L41) —— 整型支持 CAS 与全部 RMW；浮点仅支持 `fetch_add/sub` 与 `swap`，并明确「PTX 没有浮点的 `atom.cas`」。

每个原子类型内部都只是一个 `UnsafeCell<T>`，外层包一层 `#[repr(transparent)]`，使得它与裸 `T` 布局完全一致——这正是「host 与 device 共享同一份统一内存」的 ABI 基础：

[crates/cuda-device/src/atomic.rs:L67-L72](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L67-L72) —— host 与 device 的原子 ABI 相同，同一份分配可不经转换在两端使用。

#### 4.2.2 核心流程

整型原子类型的「读取—改—写」语义可以用一个不变量刻画。设地址 `p` 上有原子单元 `*p`，某线程执行 `fetch_add(val, Relaxed)`，返回旧值 `old`，则对所有作用域内的线程，存在一个**全局总序**，使得：

\[
\text{old} = \text{该线程在总序中紧前一刻的 } *p,\quad
*p_{\text{新}} = \text{old} + \text{val}
\]

`Relaxed` 只保证这个 RMW 的原子性，**不**约束其它访问的顺序。若需要把本线程先前的其它写入「发布」出去，就要用 `Release`/`AcqRel`；若需要建立一个跨所有线程的全局单一总序，就用 `SeqCst`。

整型 CAS（`compare_exchange`）在 cuda-oxide 里实现成两层：

```text
compare_exchange(current, new, success, failure)   // 公开 API，返回 Result<T,T>
        │  内联调用
        ▼
compare_exchange_raw(current, new, success, failure) // 私有桩，被编译器识别
        │  lowering
        ▼
LLVM cmpxchg（失败时用 failure 序，成功时用 success 序）
```

注意 `compare_exchange_raw` 是 `#[inline(never)]` 的桩，`compare_exchange` 则是 `#[inline(always)]` 的薄包装——后者只在源码层把返回值包装成 `Result`，真正的原子语义全部落在被编译器识别的 `_raw` 调用上。

浮点原子没有 CAS 这一层。它的 `fetch_sub` 也是一个小技巧：PTX 没有浮点 `sub` 原子，于是 cuda-oxide 把 `fetch_sub(x)` 实现为 `fetch_add(-x)`，复用 `atomicrmw fadd`，让后端继续用原生加法原子：

[crates/cuda-device/src/atomic.rs:L407-L415](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L407-L415) —— `fetch_sub` 注释明确：降级为「对相反数做 `atomicrmw fadd`」。

#### 4.2.3 源码精读

**桩函数模式**。先看整型宏生成的 `fetch_add`——注意方法体只有 `unreachable!()`，外加 `#[inline(never)]` 防止被内联掉、确保 mir-importer 能在 MIR 里看到这条 Call：

[crates/cuda-device/src/atomic.rs:L211-L217](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L211-L217) —— `fetch_add` 桩，方法体永不执行，仅供编译器按方法名识别。

模块底部的一段注释把这个策略讲透了：

[crates/cuda-device/src/atomic.rs:L74-L79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L74-L79) —— 「每个方法都是 cuda-oxide 编译器按名识别、替换为正确 LLVM 原子指令的桩；方法体永不执行，只服务于开发期的类型/借用检查」。

**类型布局与 `Sync`**。整型与浮点原子的类型体完全一样：`UnsafeCell<T>` + `#[repr(transparent)]` + `unsafe impl Sync`：

[crates/cuda-device/src/atomic.rs:L145-L153](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L145-L153) —— 整型原子的类型骨架。`UnsafeCell` 提供内部可变性，`unsafe impl Sync` 声明「跨线程共享安全」——其安全性靠「所有访问都走编译器生成的原子指令」保证。

**非拥有视图 `from_ptr`**。这是设备端最常用的访问模式：拿到一块 `&[T]` 切片（典型来自 `DisjointSlice` 或全局内存指针），把其中某个单元就地 reinterpret 成原子引用，从而用原子语义操作同一地址：

[crates/cuda-device/src/atomic.rs:L161-L188](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L161-L188) —— `from_ptr`：把裸指针重解释为原子引用，等价于 C++ 的 `cuda::atomic_ref<T, Scope>`。注释逐条列了对齐、有效期、不得与非原子访问混用等安全契约。

`atomics` 示例里反复出现的 `unsafe { &*(counter.as_ptr() as *const DeviceAtomicU32) }` 就是 `from_ptr` 的内联手写版——之所以写成内联，是因为 `from_ptr` 桩本身不能在源码层执行，但写法等价、且更直观地表达了「把 `&[u32]` 的首元素当原子用」：

[crates/rustc-codegen-cuda/examples/atomics/src/main.rs:L60-L73](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L60-L73) —— Test 1：把 `counter[0]` reinterpret 成 `DeviceAtomicU32`，256 个线程各 `fetch_add(1, Relaxed)`，结束后 `counter` 应等于 256、且各线程拿到的旧值两两不同。

**CAS 的两层结构**。私有 `compare_exchange_raw` 是被编译器识别的桩，公开 `compare_exchange` 是把它包成 `Result` 的薄包装：

[crates/cuda-device/src/atomic.rs:L276-L315](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L276-L315) —— `_raw` 返回旧值，公开方法据此判断 `Ok`/`Err`。注意成功/失败可分别指定内存序。

示例里的 CAS 测试：所有线程抢着把 0 换成自己的 tid+1，恰有一个赢家：

[crates/rustc-codegen-cuda/examples/atomics/src/main.rs:L107-L136](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L107-L136) —— Test 3：成功用 `AcqRel`、失败用 `Relaxed`，正是 `compare_exchange(current, new, success, failure)` 双内存序的典型用法。

**浮点原子**。`DeviceAtomicF16` 的文档点出了它的硬件映射与最低架构：

[crates/cuda-device/src/atomic.rs:L456-L462](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L456-L462) —— `DeviceAtomicF16` 的 `fetch_add/sub` 降级到硬件 `atom.add.noftz.f16`（sm_70+），更老的目标上 llc 会展开成 CAS 循环。

`atomic_f16` 示例用 f16 做直方图——每线程把 1.0f16 原子加进对应 bin：

[crates/rustc-codegen-cuda/examples/atomic_f16/src/main.rs:L21-L35](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomic_f16/src/main.rs#L21-L35) —— `device_hist_f16`：`fetch_add(1.0f16, Relaxed)`，并用 `DisjointSlice` 回收每个线程看到的旧值，宿主侧据此校验旧值集合的完备性。

**fence splitting 变通**。最后看一处容易被忽略、但影响所有非 `Relaxed` RMW 的实现细节。LLM 的 NVPTX 后端在 `atomicrmw` 上会**静默丢弃**内存序（待 LLVM 23 修复），cuda-oxide 在 lowering 层用「栅栏拆分」绕开：把一条带序 RMW 改写成「栅栏 + monotonic RMW + 栅栏」：

[crates/mir-lower/src/convert/intrinsics/atomic.rs:L25-L38](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs#L25-L38) —— 五种内存序各自的栅栏拆分模板；所有栅栏都带上与原子操作相同的 `syncscope`。

`atomics` 示例 Test 4/5（AcqRel / SeqCst）正是用来验证这条变通路径仍产生正确结果：

[crates/rustc-codegen-cuda/examples/atomics/src/main.rs:L143-L155](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L143-L155) —— Test 4：`fetch_add(1, AcqRel)` 触发「fence release + atomicrmw monotonic + fence acquire」。

#### 4.2.4 代码实践

**实践目标**：用整型原子实现一个无锁直方图，并体会「作用域选择」与「内存序选择」是两件独立的事。

**操作步骤**：

1. 复制 `atomics` 示例目录为基础：它已经搭好了 `#[cuda_module]`、`DeviceBuffer`、`LaunchConfig` 的脚手架。
2. 新增一个核函数 `histogram`：输入 `data: &[u32]`（每个元素是一个 0..NBINS 的 bin 编号），输出 `hist: &[u32]`（ reinterpret 成 `DeviceAtomicU32` 的数组）。每个线程读自己的 `data[gid]`，对 `hist[bin]` 做 `fetch_add(1, Relaxed)`。
3. 启动时 grid 多于 1 个 block（例如 4×64），验证直方图正确——这要求作用域必须是 `Device`（跨 CTA）。把 `hist` 的首地址传成 `&[u32]`，在内核内用 `unsafe { &*(hist.as_ptr().add(bin) as *const DeviceAtomicU32) }` 取原子引用。
4. （可选）把 `Relaxed` 改成 `SeqCst`，用 `cargo oxide pipeline` 观察生成的 PTX 多出两条 `fence.sc`。

**需要观察的现象**：直方图各 bin 的计数等于 `data` 中对应 bin 编号的出现次数；旧值集合与计数一致。多 block 启动下若误用 `BlockAtomicU32`，结果会错误（跨块丢失更新），但**未必**必然出错——这是 UB，可能时对时错，更隐蔽。

**预期结果**：用 `DeviceAtomicU32` + `Relaxed` 得到完全正确的直方图。这是「能用 `Relaxed` 就别用更强序」的典型场景——直方图只关心计数原子性，不依赖跨线程的其它读写顺序。

> 若无 GPU，至少用 `cargo oxide build histogram` 确认能通过编译；运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么浮点原子没有 `compare_exchange`？想用 CAS 语义更新一个 `f32` 该怎么办？

**答案**：PTX 没有 `atom.cas` 的浮点变体（见 [atomic.rs:L40-L41](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L40-L41)）。若确实需要 CAS 语义，可以把 `f32` 的位模式 reinterpret 成 `u32`（`f32::to_bits`），用 `DeviceAtomicU32::compare_exchange` 操作位，再把结果 `u32::from_bits` 回 `f32`——但要注意这种位级 CAS 对 NaN 的位模式未必符合浮点语义预期。

**练习 2**：`atomicrmw` 的栅栏拆分变通里，为什么所有栅栏都要带与原子操作相同的 `syncscope`？

**答案**：栅栏的可见范围必须与原子操作本身一致，否则拆分前后语义不等价。例如 block 作用域的 `AcqRel` RMW 拆成「release 栅栏 + monotonic RMW + acquire 栅栏」，若栅栏不带 `.cta`，就会变成默认（`.sys`/系统）作用域，强度与原意不符。见 [mir-lower/atomic.rs:L37-L38](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs#L37-L38) 与示例 Test 19 的注释 [atomics/src/main.rs:L497-L501](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L497-L501)。

---

### 4.3 打包 f16x2 / bf16x2 原子加法

#### 4.3.1 概念说明

深度学习里经常要对 f16 / bf16 梯度做原子累加（典型如反传播中的梯度归约）。PTX 提供了一条专门的打包指令，在**一条原子指令**里同时对两个相邻的 16 位浮点值做加法：

```ptx
atom.global.add.noftz.f16x2 %old, [%addr], %val;
```

`f16x2` 的含义是：把两个 f16 值打包进一个 32 位字（低 16 位 = 第 0 lane，高 16 位 = 第 1 lane），一次原子加法同时更新两个字。这在显存带宽和原子吞吐上都是 2 倍收益，对梯度累加场景意义重大。

cuda-oxide 把这条指令暴露成两个**独立函数**（而非原子类型的方法），原因是它操作的是裸 `u32` 字，绕过了「作用域编进类型」的那套体系：

[crates/cuda-device/src/atomic.rs:L567-L573](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L567-L573) —— 打包原子加法是独立函数，因为硬件直接操作承载两个 16 位 lane 的裸 `u32` 字，绕开作用域类型系统、直接用 `atom.global`。

#### 4.3.2 核心流程

`atom_add_f16x2` 的数学语义（设 `addr` 指向的字为 `W`，输入字为 `V`，均为两个打包 f16）：

\[
\text{lane}_0(W)_{\text{新}} = \text{lane}_0(W)_{\text{旧}} + \text{lane}_0(V),\qquad
\text{lane}_1(W)_{\text{新}} = \text{lane}_1(W)_{\text{旧}} + \text{lane}_1(V)
\]

返回值是两个字段上的**旧值**。这里有三个关键语义点（全部源自 PTX 指令本身）：

1. **两个 lane 各自原子**，但**两次 lane 操作的先后顺序未指定**。
2. 返回的两个旧值 lane **不必来自同一次 32 位快照**——即可能 lane0 取自时刻 A、lane1 取自时刻 B。
3. `.noftz` 表示**不**做 flush-to-zero：保留亚正规（subnormal）值，每 lane 按就近偶数舍入。

最低架构要求（两者不同，是本模块的硬约束）：

| 函数 | 最低架构 | PTX 版本 |
|------|---------|----------|
| `atom_add_f16x2`  | sm_70+（cuda-oxide 的 Volta 基线）| PTX 6.2 / sm_60+ 起 |
| `atom_add_bf16x2` | sm_90+（Hopper）| PTX ISA 7.8+ |

bf16x2 要求更高，是因为 cuda-oxide 只暴露**原生** PTX 指令，不像 CUDA C++ 那样在老 GPU 上软件模拟。

#### 4.3.3 源码精读

**打包常量**。`packed_atomic_add` 示例开头定义了两个打包「1.0」常量，是理解 lane 布局的最佳入口：

[crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:L16-L19](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L16-L19) —— `PACKED_F16_ONE = 0x3c00_3c00`：f16 的 `1.0` 位模式是 `0x3c00`，两个拼成 `0x3c00_3c00`。同理 bf16 的 `1.0` 是 `0x3f80`。

> 名词解释：**f16**（IEEE 半精度）的 `1.0` = 符号 0、指数 15（偏移后存 `01111`）、尾数 0，拼成 `0 01111 0000000000` = `0x3c00`。**bf16**（Brain Float）把 f32 的低 16 位尾数砍掉、保留高 8 位指数 + 1 位符号，故 `1.0` 的 bf16 位模式等于 f32 `1.0` 的高 16 位 = `0x3f80`。

**核函数**。两个打包原子加法在同一内核里依次调用，分别落在相邻的两个 `u32` 字上：

[crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:L25-L36](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L25-L36) —— `add_packed`：每个线程对 `base` 做 `atom_add_f16x2`、对 `base+1` 做 `atom_add_bf16x2`，各加一个打包的 `1.0`。32 个线程跑完，每个字内的两个 lane 都应累加到 32.0。

**sm_90 守卫**。因 bf16x2 需要 Hopper，宿主侧在启动前查算力、不够则跳过：

[crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:L52-L60](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L52-L60) —— `ctx.compute_capability()` 返回 `(major, minor)`，`major < 9` 即低于 sm_90，打印跳过原因后 `return`。

**桩函数签名与文档**。`atom_add_f16x2` 是 `unsafe fn`，接收 `*mut u32` 与 `u32`，返回 `u32`，方法体同样是 `unreachable!()`：

[crates/cuda-device/src/atomic.rs:L612-L617](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L612-L617) —— `atom_add_f16x2` 签名；返回值是承载两个旧 lane 的 `u32`。

它的文档精确刻画了 PTX 指令、`.noftz` 语义与最低架构：

[crates/cuda-device/src/atomic.rs:L590-L602](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L590-L602) —— PTX 模板、`.noftz` 保留亚正规值、每 lane 就近偶数舍入、sm_70+。

bf16x2 的对应文档则把架构要求抬到 sm_90，并说明「只暴露原生指令、不做软件模拟」的设计取舍：

[crates/cuda-device/src/atomic.rs:L642-L647](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L642-L647) —— bf16x2 需 sm_90+ / PTX 7.8+，CUDA C++ 会在老 GPU 上模拟，cuda-oxide 有意只暴露原生指令。

#### 4.3.4 代码实践

**实践目标**：运行 `packed_atomic_add` 示例，亲眼看到两个 16 位 lane 在一条 32 位字内独立累加。

**操作步骤**：

1. 确认本机 GPU 算力 ≥ sm_90（`nvidia-smi --query-gpu=compute_cap --format=csv`，或直接运行示例看它是否打印 skip）。
2. 在示例根目录运行 `cargo oxide run packed_atomic_add`。
3. 阅读宿主侧的 `unpack_f16x2` / `unpack_bf16x2`，理解它如何把结果 `u32` 拆回两个 `f32`：

[crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:L38-L50](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L38-L50) —— `unpack_*` 把 `u32` 的低/高 16 位分别还原成 f16/bf16 再转 f32。

4. 把 `THREADS` 常量从 32 改成 64，重新运行，观察两个 lane 的和都从 32.0 变成 64.0。

**需要观察的现象**：断言 `unpack_f16x2(result[0]) == (32.0, 32.0)` 与 `unpack_bf16x2(result[1]) == (32.0, 32.0)` 通过，打印 `PASS: packed lane sums reached 32 on sm_XX`。

**预期结果**：一条 `atom.global.add.noftz.f16x2` 指令同时把两个 lane 各加了 32 次，等价于 64 次单 lane 加法却只用 32 条指令、32 次原子事务。

> 本机算力 < sm_90 时示例会打印 skip 并退出；此时可把 `add_packed` 内核里 `atom_add_bf16x2` 那行注释掉、仅保留 f16x2 路径，在 sm_70+ 的卡上验证 f16x2 部分（bf16x2「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：`PACKED_F16_ONE = 0x3c00_3c00` 里，两个 `0x3c00` 各代表什么？为什么低位在前？

**答案**：每个 `0x3c00` 是 f16 `1.0` 的位模式。低位在前是小端打包约定：`u32` 的低 16 位是第 0 lane、高 16 位是第 1 lane，与 PTX `f16x2` 的 lane 布局一致。详见 [packed_atomic_add/src/main.rs:L17](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L17) 与 [atomic.rs:L581-L584](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L581-L584)。

**练习 2**：为什么 `atom_add_bf16x2` 需要 sm_90，而 `atom_add_f16x2` 只要 sm_70？

**答案**：f16x2 的 PTX 指令自 PTX 6.2 / sm_60+ 起就有硬件支持，cuda-oxide 基线是 sm_70（Volta）故满足；bf16x2 的原生 PTX 指令要到 PTX ISA 7.8+、即 Hopper（sm_90）才引入。cuda-oxide 有意只暴露原生指令、不做 CUDA C++ 那样的老 GPU 软件模拟（见 [atomic.rs:L643-L647](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L643-L647)），所以 bf16x2 的最低架构就是 sm_90。

---

### 4.4 lane 级原子性与 UB 约束

#### 4.4.1 概念说明

打包原子加法最反直觉的一点是：**「两个 lane 各自原子」≠「整个 32 位字原子」**。

这是一条极其重要的安全边界。`atom.global.add.noftz.f16x2` 保证的是：lane0 的读—改—写不会被另一条针对 lane0 的原子加法打断，lane1 同理；但它**不**保证「lane0 与 lane1 作为一次 32 位快照不可被打断」。换言之：

- 两个线程同时 `atom_add_f16x2` 同一地址，**不会**丢失 lane 级更新——两个 lane 各自正确累加。
- 但若一个线程 `atom_add_f16x2`、另一个线程对该地址做**整字 `u32` 原子操作**（如 `DeviceAtomicU32::fetch_add`），二者**不共享** lane 级原子性，结果未定义。

这背后的硬件事实是：lane 级原子性只存在于「同类打包指令」之间，整字原子操作在硬件上是另一条不同的指令，二者之间的交互没有任何一致性保证。

#### 4.4.2 核心流程

把「同一 32 位字的合法与非法并发访问」整理成一张决策表：

| 线程 A 的操作 | 线程 B 的操作 | 是否安全 |
|---------------|---------------|----------|
| `atom_add_f16x2` | `atom_add_f16x2` | ✅ 安全，两 lane 各自原子 |
| `atom_add_bf16x2` | `atom_add_bf16x2` | ✅ 安全（但需同类型，别混 f16x2 与 bf16x2）|
| `atom_add_f16x2` | `DeviceAtomicU32::fetch_add`（整字）| ❌ UB |
| `atom_add_f16x2` | 非原子 `*addr = ...` | ❌ UB（混用原子/非原子）|
| `atom_add_f16x2`（device 作用域）| host/CPU 上的原子访问 | ❌ 不可与系统作用域互操作 |

返回值的「非快照」特性也值得单独强调：`atom_add_f16x2` 返回的 `u32` 里，lane0 旧值与 lane1 旧值**可能取自不同时刻**。所以不能把返回值当成「加法前那一瞬间的 32 位快照」来用——例如不能用 `old & 0xFFFF` 与 `old >> 16` 做任何要求两者一致的推理。

#### 4.4.3 源码精读

这些 UB 约束不是隐含的——它们被逐条写进了 `atom_add_f16x2` 的 `# Safety` 文档：

[crates/cuda-device/src/atomic.rs:L603-L611](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L603-L611) —— 三条安全契约：地址须 4 字节对齐、4 字节可写；不得与整字 `u32` 原子或任何非原子访问竞争（不共享 lane 级原子性，是 UB）；并发 lane 原子的作用域必须互相包含，本操作是 device 作用域、与 host/system 访问不互为原子。

返回值的「非快照」语义则写在函数主文档里：

[crates/cuda-device/src/atomic.rs:L581-L588](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L581-L588) —— 返回的两个旧 lane 「不必来自一次连贯的 32 位快照」。

`bf16x2` 的安全契约与 f16x2 完全对称：

[crates/cuda-device/src/atomic.rs:L648-L656](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L648-L656) —— 同样三条：对齐、不得与整字/非原子混用、作用域须互相包含。

> 与 4.2 的整型/浮点原子对照：那里 `from_ptr` 的安全文档也强调「不得与非原子访问混用」（见 [atomic.rs:L175-L181](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L175-L181)），这是所有原子操作的通用约束；打包原子额外多出「不得与整字原子混用」这一条，因为它把原子性下放到了 lane 级。

#### 4.4.4 代码实践

**实践目标**：把「与整字 `u32` 原子混用为何是 UB」从规则变成可推理的因果链。

**操作步骤**：

1. 阅读 [atomic.rs:L603-L611](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L603-L611) 的三条安全契约。
2. 在纸上推演以下场景：地址 `A` 初始为 `0x0000_0000`（两个 f16 lane 都是 0.0）。
   - 线程 X 执行 `atom_add_f16x2(A, 0x3c00_0000)`（只给 lane0 加 1.0）。
   - 线程 Y 同时执行 `DeviceAtomicU32::fetch_add(A, 0x0000_3c00)`（整字加，等价于只给 lane1 的位模式加 `0x3c00`）。
3. 思考：两条指令在硬件上是不同的指令序列，它们各自「读—改—写」整个 32 位字。是否存在一种交错，使得线程 Y 的整字写覆盖了线程 X 刚写入的 lane0？是否存在一种交错使得 lane1 丢失更新？
4. （可选，需 GPU）写一个故意混用的核函数，用 `cargo oxide sanitize packed_atomic_add --tool racecheck` 看 Compute Sanitizer 是否报告竞争。

**需要观察的现象**：在步骤 3 的推演中，应能构造出「丢失更新」的交错——这正是 UB 的来源：硬件对这两条指令之间没有任何 lane 级一致性保证，它们各自整字读—改—写，互相覆盖。

**预期结果**：能用自己的话讲清——「打包原子只在**同类打包指令之间**提供 lane 级原子性；整字原子是另一条指令，二者不共享原子性域，所以混用会丢失更新，是 UB」。`racecheck` 在实际混用代码上「待本地验证」是否必然报告。

#### 4.4.5 小练习与答案

**练习 1**：返回值 `old: u32` 里，`old` 的低 16 位与高 16 位是否一定来自同一时刻的内存快照？为什么？

**答案**：不一定。文档明确「returned lanes ... need not come from one coherent 32-bit snapshot」（[atomic.rs:L583-L585](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L583-L585)）。两个 lane 的旧值可能取自不同时刻，因为两条 lane 操作的先后顺序未指定。因此不能把返回值当成连贯快照使用。

**练习 2**：我想用一个 `u32` 字同时存 f16 直方图的两个 bin，于是同一段代码里既调 `atom_add_f16x2`、又对该字调 `DeviceAtomicU32::fetch_add`，可以吗？

**答案**：不可以，是 UB。打包原子加法只在同类打包指令之间提供 lane 级原子性，整字 `u32` 原子是另一条指令，二者不共享原子性域，混用会丢失更新（见 [atomic.rs:L607-L609](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/atomic.rs#L607-L609)）。若两个 bin 都走打包路径，全程只用 `atom_add_f16x2`；若其中一个 bin 需要整字原子语义，应把它放到独立的 `u32` 字。

---

## 5. 综合实践

设计一个**双路径梯度累加**小程序，把本讲四个模块串起来。

**任务**：实现一个核函数，把一批「贡献」累加到一个长度为 `2*NBINS` 的 f16 缓冲区 `grad` 上（每相邻两个 f16 视为一个 f16x2 打包字，共 `NBINS` 个打包字）。要求：

1. 用 `atom_add_f16x2` 做打包累加（路径 A），体会「一条指令更新两个 lane」；
2. 另设一个独立的 `u32` 计数器，用 `DeviceAtomicU32::fetch_add(..., Relaxed)` 统计总贡献次数（路径 B）——注意它必须在**独立**的 `u32` 字上，不得与 f16x2 字混用；
3. 启动配置跨多个 block（如 4×64），论证为何 `grad` 路径不需要 `.cta` 而计数器路径也不能用 `BlockAtomicU32`；
4. 宿主侧用类似 `unpack_f16x2` 的方式拆解 `grad`，校验每个 lane 的累加和，并校验计数器等于总线程数。

**验收点**：

- 能解释路径 A 为何是 device 作用域（`.gpu`）、`Relaxed` 内存序就够；
- 能指出若把计数器放进某个 f16x2 字的高/低 lane 会立刻变成 UB；
- 能说出 `grad` 路径若误用 `BlockAtomicU32`（.cta）跨块累加是 UB，且「不一定每次都出错」正是 UB 的危险之处。

**提示**：脚手架可直接参考 [packed_atomic_add](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs)（打包部分）与 [atomics](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs) Test 1/7（整型计数器与跨块累加）。

> 本任务需要 sm_70+ 才能跑 f16x2 路径；若本机更低，「待本地验证」运行结果，但代码可先用 `cargo oxide build` 通过编译。

## 6. 本讲小结

- cuda-oxide 把**作用域编进类型名**（`DeviceAtomic*`/`BlockAtomic*`/`SystemAtomic*` → `.gpu`/`.cta`/`.sys`），把**内存序作为运行期参数**（`AtomicOrdering` 五种 → PTX ordering），二者正交。
- **整型原子**支持全套 RMW + CAS；**浮点原子**因 PTX 限制只有 `load/store/fetch_add/fetch_sub/swap`，没有 CAS 与位运算。所有方法都是 `unreachable!()` 桩，由编译器按方法名识别后 lowering。
- `compare_exchange` 是「`_raw` 桩 + `Result` 包装」两层；`fetch_sub` 对浮点实现为「对相反数 `atomicrmw fadd`」；非 `Relaxed` 的 RMW 走 **fence splitting** 变通（栅栏 + monotonic RMW + 栅栏），栅栏带相同 `syncscope`。
- **打包 `atom_add_f16x2` / `atom_add_bf16x2`** 是独立函数（不是方法），操作承载两个 16 位 lane 的 `u32` 字，直接用 `atom.global.add.noftz.*`；f16x2 需 sm_70+、bf16x2 需 sm_90+。
- 打包原子的原子性是** lane 级**而非整字级：与整字 `u32` 原子、非原子访问混用，或跨不互相包含的作用域并发，都是 **UB**；返回的两个旧 lane 不必来自同一快照。

## 7. 下一步学习建议

- **u5-l1 Warp 级编程**：`ballot`/`lanemask` 投票与 warp 归约，是「块内归约」的另一种（往往更高效）手段，可与本讲的 `BlockAtomicU32` 归约对比——前者靠 warp 锁步、后者靠显式原子。
- **u5-l3 异步屏障与异步拷贝**：当原子操作用于跨 block 协作时，`mbarrier` 与 `cp.async` 提供了比重原子更结构化的同步与搬运原语。
- **u6-l2 mir-importer 深潜 / u6-l3 mir-lower 深潜**：若想彻底搞清「桩函数如何被识别成 NVVM op、再降级为 LLVM 原子指令」，可深读 [mir-importer/.../atomic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/atomic.rs) 与 [mir-lower/.../atomic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/atomic.rs)，理解方法名→RMW 种类、类型名→作用域、内存序→栅栏拆分的完整映射。
- **u7-l3 Compute Sanitizer**：本讲反复出现的「混用是 UB」可用 `cargo oxide sanitize --tool racecheck` / `--tool memcheck` 在 GPU 上实证。
