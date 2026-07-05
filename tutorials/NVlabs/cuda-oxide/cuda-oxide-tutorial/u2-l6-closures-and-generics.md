# 闭包与泛型上 GPU

## 1. 本讲目标

本讲承接 u2-l1（`#[kernel]` 与 `#[cuda_module]` 宏）。在那里我们学到：宏会为每个**非泛型**内核预生成一个 `CudaFunction` 字段并在 `load()` 时一次性加载。本讲要回答两个更进一步的问题：

1. 当内核是**泛型**的（如 `fn scale<T>(...)`），每个具体类型（`f32`、`i32`）会怎样变成各自的 PTX 入口？
2. 当内核接收一个**闭包**（`F: Fn(T) -> T`），闭包捕获的环境是怎么「穿」过 host/device 边界跑到 GPU 上去的？

学完后你应当能够：

- 说清 Rust **单态化（monomorphization）** 在 cuda-oxide 里产生了哪些 device 入口；
- 解释闭包捕获为什么能被当作一个普通的「按值」内核参数传入 GPU；
- 理解 `type_id_u128` 如何在 host 与 backend 两端算出**同一个**稳定 PTX 名 `<base>_TID_<hex32>`，让泛型核能被运行时查找到。

## 2. 前置知识

### 2.1 Rust 的单态化（Monomorphization）

Rust 的泛型在编译期会被「展开」：对源码里每一个实际用到的具体类型，编译器都生成一份独立的机器码。例如：

```rust
fn scale<T>(x: T) { /* ... */ }
scale::<f32>(1.0);
scale::<i32>(1);
```

rustc 会产出 `scale::<f32>` 和 `scale::<i32>` 两份完全独立的函数（各自一份 `Ty<'tcx>`，即 `Instance`）。这套机制叫做**单态化**。CUDA 侧的对应物是 C++ 的 `template<class T> __global__ void scale(...)`——每用一个类型就实例化出一份。

> 关键点：单态化发生在 **rustc 层**，cuda-oxide 不需要发明新的泛型系统，只要保证 backend 的「内核收集器」能把单态化后的每个实例都收集到即可。

### 2.2 闭包是什么

Rust 的闭包（`|x| x * factor`）本质上是一个**编译器生成的匿名结构体**，捕获的变量就是这个结构体的字段：

```rust
let factor = 2.5f32;
let f = move |x: f32| x * factor;
// 等价于一个匿名 struct：struct __Closure { factor: f32 }
// 实现 Fn(f32) -> f32，方法体是 self.factor * x
```

理解这一点很重要：**闭包值就是一个普通的结构体**，所以它能像任何 `Copy` 结构体一样被「按值」传给内核。`move` 闭包把捕获值搬进结构体；非 `move` 闭包则把 `&T` 引用作为字段（u2-l6 末尾会讲这依赖 HMM）。

### 2.3 前置讲义回顾

- **u2-l1**：`#[kernel]` 会改名为保留前缀 `cuda_oxide_kernel_246e25db_<原名>`；非泛型内核在 `load()` 时被预加载成 `CudaFunction`。
- **u1-l1**：backend 靠**保留命名空间 + 调用图可达性**识别 device 代码，无需 `#[cfg]`。
- **u2-l4**：宿主把 Rust 参数压成 `Vec<*mut c_void>`，标量压一条、切片拆成（指针, 长度）压两条——本讲要看的闭包就是「按值结构体」压一条。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [generic/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs) | 泛型单态化示例：`scale<T>` / `add<T>` / `closure_capture<T>`，分别用 `f32`/`i32` 启动（启动调用均包在 `unsafe` 块中） |
| [host_closure/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs) | 闭包示例：泛型 `map<T,F: Fn>`，覆盖 0~4 个捕获、`Fn`/`FnMut`/`FnOnce`、const 泛型（同步/异步 × 类型化/`cuda_launch!` 四路径全部 `unsafe`） |
| [cuda-host/src/type_id.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs) | `type_id_u128` 与 `__intern_generic_kernel_name`：稳定的 128 位类型哈希与 PTX 名内联 |
| [cuda-host/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs) | `GenericCudaKernel` trait 与 `<base>_TID_<hex32>` 命名契约的文档定义 |
| [cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs) | 宏展开：生成 `__<name>_CudaKernel` marker、`<name>_ptx_name` 助手、闭包的单条 byval 压栈 |
| [README.md](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md) | Quick Start 的泛型 `map` 示例（`unsafe` 原始启动 + 启动契约说明）与能力清单 |

> 说明：本讲引用的 `cuda-macros`/`launch.rs`/`cuda_module_api.rs` 行号基于 HEAD `29396b7`，这是为「讲清楚机制」服务的补充证据；讲义指定的核心源码是前两个示例与 `type_id.rs`。本轮（#318）把这两个示例的全部启动调用统一包进 `unsafe` 块（raw `LaunchConfig` 路径），详见 4.1.3 与 4.2.2。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 泛型核的单态化与多入口收集
2. 闭包捕获的标量化（按值穿过内核边界）
3. `type_id_u128` 与稳定的 PTX 命名 `<base>_TID_<hex32>`
4. host_closure / generic 示例精读

---

### 4.1 泛型核的单态化

#### 4.1.1 概念说明

一个泛型 `#[kernel]` 函数在源码里只有一份定义，但运行时 PTX 里要为**每一个实际用到的具体类型**各放一份入口。这件事不是 cuda-oxide 自己做的，而是直接借用 rustc 的单态化（见 2.1）。cuda-oxide 要解决的是「**让单态化真的发生，并且被 backend 收集到**」。

难点在于：device 代码与 host 代码同在一个 crate 里编译。如果 host 从不调用内核的某个类型实例，rustc 就不会为它生成代码，backend 自然也收集不到。所以宏必须生成一段「**强制单态化**」的胶水代码。

#### 4.1.2 核心流程

1. `#[kernel]` 检测到内核带泛型参数（`has_codegen_generics` 为真），就**不**给它加 `#[no_mangle]`（非泛型内核才加），而是用 rustc 的 mangled 符号名，并给被前缀化后的函数加 `#[inline(never)]`——这样每个单态化实例都会作为独立符号出现在 codegen unit 里，供 backend 收集器发现。
2. 宏额外生成一个 `pub fn <name>_ptx_name<T,...>()` 助手，函数体里用**易失指针读写**（`write_volatile` / `read_volatile`）取内核函数项的地址——这一步在编译期不可消除，从而「**钉住**」该具体 `Instance`，强制 rustc 为它生成代码。
3. host 调用 `module.scale::<f32>(...)` 时，类型化的启动方法会引用到 `scale::<f32>`，单态化触发，backend 收集器把 `scale::<f32>` 与 `scale::<i32>` 收为两个 device 入口。
4. 与非泛型内核不同（u2-l1：在 `load()` 时预加载成 `CudaFunction` 字段），**泛型内核在 `load()` 时不预加载**，而是在**启动时**按算出的 PTX 名用 `module.load_function(name)` 现取，并缓存在 `__generic_functions` 表里。

一句话：**单态化由 rustc 完成，cuda-oxide 只负责「触发它」并「按名找到结果」。**

#### 4.1.3 源码精读

`generic` 示例定义了三个泛型内核，`scale<T>` 是最简单的一个：

[generic/src/main.rs:42-49](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L42-L49) —— 泛型 `scale` 内核，`T: Copy + Mul<Output = T>`，逐元素 `out = input * factor`。

host 侧分别用 `f32` 和 `i32` 启动它。关键就在这两个类型化的方法调用：

[generic/src/main.rs:121-130](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L121-L130) —— `module.scale::<f32>(...)`：turbofish 里的 `f32` 就是单态化的扳机；注意 #318 起这次调用整体包在 `unsafe { ... }` 块里（上方 `SAFETY:` 注释由调用方自证形状/资源匹配），因为 raw `LaunchConfig` 是未经证明的原始数据。

[generic/src/main.rs:151-161](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L151-L161) —— `module.scale::<i32>(...)`：再一次（同样在 `unsafe` 块中），但这次实例化出的是**另一个**入口。

示例顶部的注释把因果链写得很直白：

[generic/src/main.rs:105-110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L105-L110) —— 「typed method call with type parameter forces monomorphization」，并指出展开后的代码会引用 `cuda_oxide_kernel_scale::<f32>`，从而被 backend 收集器看见。

「强制单态化」胶水的真身在宏里：每个泛型内核都会生成一个 `<name>_ptx_name<T,...>()` 助手，函数体里用易失指针读写「钉住」内核函数项的地址（编译期不可消除），再调 `GenericCudaKernel::ptx_name()` 算名：

[cuda-macros/src/lib.rs:4029-4037](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L4029-L4037) —— `let #kernel_ptr = #kernel_name #kernel_turbofish as *const ();` 后用 `write_volatile`/`read_volatile` 取该具体 `Instance` 的地址，强制 rustc 为它生成代码；这正是 4.1.2 步骤 2 描述的机制（外层函数 `generate_generic_cuda_kernel_impl` 见 [cuda-macros/src/lib.rs:3974-4039](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L3974-L4039)）。

#### 4.1.4 代码实践

**目标**：亲眼看到「一个泛型核 → 两个 PTX 入口」。

**步骤**：

1. 在 `crates/rustc-codegen-cuda/examples/generic` 目录下，确认 `scale<T>` 内核存在。
2. 运行 `cargo oxide pipeline generic`（**无需 GPU**，只需 nightly + `llc-21` + clang）。
3. 打开流水线产出的 `.ll` / `.ptx` 中间产物（`pipeline` 会打印路径）。

**需要观察的现象**：在 PTX 中应能找到与 `scale` 相关、但分别对应 `f32` 与 `i32` 的两个入口符号（PTX 名形如 `scale_TID_<hex32>`，详见 4.3）。

**预期结果**：两个不同的 `_TID_` 后缀，证明单态化产生了两个 device 入口。若只看到一个，说明只有一个类型实例被触发——检查 host 是否两个 turbofish 都写到了。

> 待本地验证：实际 PTX 文本路径与符号名需在本机 `pipeline` 后确认；本讲不假设你已运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `generic` 示例里 `scale::<i32>` 那段调用整个删掉，重新 `pipeline`，PTX 里 `scale` 的入口数量会怎样变化？

**答案**：`i32` 实例不再被任何 host 代码引用，rustc 不会单态化它，backend 收集器也就收不到——PTX 里只会剩下 `scale::<f32>` 一个入口。这正是宏必须生成「强制单态化」胶水的根本原因。

**练习 2**：`scale<T>` 的约束是 `T: Copy + Mul<Output = T>`。为什么必须有 `Copy`？

**答案**：`factor` 是按值传入内核、再被每个线程读取的；`Copy` 保证它可被重复读而不发生 move。同时（见 4.3）类型化启动方法的泛型边界直接照搬内核声明的约束，`Copy` 是 backend 对「按值穿过边界的泛型」的要求。

---

### 4.2 闭包捕获的标量化

#### 4.2.1 概念说明

第 2.2 节说过，闭包值就是一个匿名的捕获结构体。所谓「**标量化**」（README 原文：captured, scalarized, and passed as kernel parameters），在这里的含义是：**把整个闭包结构体当成一个普通的「按值」内核参数**穿过 host/device 边界，设备侧再把同样的比特模式读回成类型 `F` 并调用 `f(...)`。

注意它**不是**「把每个捕获拆成独立的 kernel 参数」。这一点 cuda-oxide 经历过一次 ABI 修复：早期的 `cuda_launch!` 宏会把闭包的每个捕获**分别**压栈，而类型化 API 把闭包**整体**保留，两边与 backend 的 `.param` 声明对不上。修复后的统一设计是：**host 与 device 两侧都把闭包当作单条 byval `.param`**。host_closure 示例正是这次修复的回归测试。

#### 4.2.2 核心流程

类型化路径（`module.map::<f32, _>(..., move |x| x * factor, ...)`）：

1. 宏在启动方法里把闭包字面量绑定到一个临时变量 `__closure`。
2. 调用一个「实例化助手」`instantiate_map(&__closure)`——它只取 `&F`，目的是**用闭包的匿名类型绑定到泛型参数 `F`**从而触发单态化，同时算出 PTX 名（见 4.3）。注意它**不** move 闭包，调用方稍后还要把闭包值本身压栈。
3. 通过 `push_kernel_scalar(&mut args, &mut __closure)` 把**整个闭包结构体**作为一条 byval 参数压进参数包。
4. backend 为该内核在入口处声明**一个** byval `.param`，类型即 `F`，与 host 的「压一条」严格对齐。
5. 设备侧读到 `F` 后，在每个线程里 `f(input[idx])`——闭包是 `Copy` 的，所以每线程用自己的寄存器副本。

> 零捕获闭包（如 `|x| x * 2.0`）是 ZST（零大小类型），`push_kernel_scalar` 会把它从 host 参数包里**丢弃**，backend 也相应丢弃它的 `.param` 声明——两端依然对齐。

#### 4.2.3 源码精读

`host_closure` 的核心内核 `map`：

[host_closure/src/main.rs:70-77](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L70-L77) —— `map<T: Copy, F: Fn(T) -> T + Copy>`：闭包 `f` 是第一个参数，随后逐元素 `*out = f(input[i])`。

闭包作为「单条 byval」穿过边界的实现，在宏里就一行：

[cuda-macros/src/lib.rs:5452-5469](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L5452-L5469) —— `CudaLaunchArg::Closure` 分支调 `push_kernel_scalar(&mut #args_ident, &mut #closure_ident)`：把整个闭包结构体作为单条 byval 标量压栈，注释明确说明 backend 也只发一个 byval `.param`，move 闭包按值压、非 move 闭包压含 host 引用的结构体（HMM 解引用），ZST 闭包经 `push_kernel_scalar` 内部的 `size_of == 0` 检查丢弃。这条「不再做 per-capture 拆分」的设计意图也写在提取函数的文档里：[cuda-macros/src/lib.rs:5077-5081](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L5077-L5081)。

`cuda_launch!` 路径里闭包的实例化与压栈顺序：

[cuda-macros/src/lib.rs:5546-5568](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L5546-L5568) —— 先 `let mut #closure_ident = <闭包字面量>`，再 `#instantiate(&#closure_ident)` 算 PTX 名并触发单态化，最后（在参数压栈循环里）把 `__closure` 整体压一条；上方注释（5546-5551）再次点明「helper 取 `&F` 是为了保留 `__closure` 所有权、紧随其后压一条 byval，与 device 侧单条 `.param` 对齐」。

示例文件顶部的模块文档把「单条 byval、两侧不展平」这条契约与历史 ABI 修复讲得很清楚：

[host_closure/src/main.rs:6-31](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L6-L31) —— 列出本示例覆盖的点：泛型 `Fn` 核、0~4 个捕获、turbofish 推断 `F`、闭包作为单条 byval 结构体、`FnMut`/`FnOnce`、`#[repr(C)]` 与默认布局的捕获结构体。

#### 4.2.4 代码实践

**目标**：体会「闭包捕获数任意，都走同一条 byval」。

**步骤**：

1. 打开 `host_closure/src/main.rs`，定位 `map` 内核（70-77 行）与 `main` 里的 Test 1（单捕获，297-313 行）、Test 4（三捕获，356-373 行）、Test 5（四捕获，378-396 行）。
2. 注意：四个测试用的是**同一个** `map::<f32, _>` 内核，只是闭包字面量不同。
3. 阅读宏展开（可用 `cargo expand`，若可用）：确认每个 case 里闭包都只产生一次 `push_kernel_scalar`。

**需要观察的现象**：无论捕获是 1 个还是 4 个，host 侧压栈代码结构一致——区别只在闭包结构体的大小，而不在压栈次数。

**预期结果**：理解「捕获数变化不改变 ABI 形状，只改变单条 byval 参数的字节数」。这一点与 4.1 的单态化配合：不同捕获数的闭包是**不同的匿名类型 `F`**，因此会单态化成**不同的 PTX 入口**（见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：`map<T, F>` 里 `F` 有 `+ Copy` 约束。为什么闭包类型也要求 `Copy`？

**答案**：内核会被成百上千个线程并发执行，每个线程都要调用 `f`。`Copy` 保证每个线程能拿到自己的一份闭包副本，无需同步、无需 move；这也让 `FnOnce` 内核能「每个线程都 call_once 一次」（在一份新拷贝上）。

**练习 2**：Test 9（[host_closure/src/main.rs:479-496](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L479-L496)）用的是**非 move** 闭包 `|x| x * scale + bias`。它捕获的是什么？为什么需要 HMM？

**答案**：非 move 闭包捕获的是 `&f32` 引用，指向 host 栈帧里的 `scale`/`bias`。闭包结构体被按值传到 GPU 后，GPU 还要解引用这些 host 指针——这依赖 Heterogeneous Memory Management（HMM，sm_75+ Linux + 驱动开启）。没有 HMM 的机器会得到 `CUDA_ERROR_ILLEGAL_ADDRESS`。

---

### 4.3 type_id_u128 与稳定的 PTX 命名

#### 4.3.1 概念说明

单态化产生了多个 device 入口，运行时怎么按名字找到「我这次要的那个」？cuda-oxide 的方案是：给每个单态化实例一个**确定性的名字** `<base>_TID_<hex32>`，其中 `<hex32>` 是该实例类型的 128 位稳定哈希渲染成的 32 位十六进制串。这个名字必须满足：

1. **host 与 backend 算出同一个值**——host 启动时按名 `cuModuleGetFunction`，backend 生成 PTX 时按同名导出，对不上就找不到。
2. **稳定**：同一种类型在每次编译、每次运行都得到同一个哈希。
3. **对生命周期不敏感**：`map::<f32>` 与借用不同生命周期的闭包不应产生多余的 PTX 变体。
4. **定长**：无论泛型 arity 多大，后缀始终是 32 个十六进制字符。

`type_id_u128` 就是这套方案的 host 侧计算函数。

#### 4.3.2 核心流程

两端如何对齐：

```
                  rustc 稳定类型哈希（region-erasing）
   host 侧                              backend 侧
   ─────────                            ──────────
   type_id_u128_of_val(                 tcx.type_id_hash(Instance::ty)
     &kernel_entry::<T,N>)              .as_u128()
   )                                    （对同一个 kernel_entry 实例）
        │                                     │
        └────────── 同一个 128 位 u128 ────────┘
                        │
                        ▼
   format!("{base}_TID_{hash:032x}")   →  "scale_TID_<32hex>"
                        │
                        ▼
   __intern_generic_kernel_name(base, hash)  ← 进程级内联缓存，避免每次启动泄漏字符串
```

- host 用 `core::intrinsics::type_id::<T>()`（nightly 内部 intrinsic）拿到 128 位值；它和 backend 的 `tcx.type_id_hash` 走**同一条** `erase_and_anonymize_regions` + 稳定哈希流水线，所以两者必然相等。
- 因为哈希函数会**擦除生命周期区域**，所以「借用不同生命周期的闭包」哈希到同一个值——不会因为生命周期造出多余 PTX 变体，满足要求 3。
- `<hex32>` 用 `{hash:032x}` 格式化，定长 32 字符，满足要求 4。

#### 4.3.3 源码精读

`type_id_u128` 的实现非常短，但每个细节都有用意：

[cuda-host/src/type_id.rs:55-59](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L55-L59) —— `type_id_u128<T: ?Sized>()`：用 `core::intrinsics::type_id` 在编译期常量求值，再 `transmute` 成 `u128`。

注意边界是 `T: ?Sized` 而**不是** `T: 'static`。这是有意为之——文件顶部说明了为什么不能用稳定的 `core::any::TypeId::of`：

[cuda-host/src/type_id.rs:8-16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L8-L16) —— 稳定 API 会强制 `T: 'static`，从而拒绝合法的「非 `'static` 借用闭包」（如捕获栈帧 `&[f32]` 的启动器）；而 `core::intrinsics::type_id` 的边界是 `T: ?Sized`，且产生与 `tcx.type_id_hash` 完全相同的 128 位值。

`type_id_u128_of_val` 保留**函数项类型**而非函数指针：

[cuda-host/src/type_id.rs:68-71](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L68-L71) —— 对函数项（如 `&kernel::<T, 4>`）取哈希；函数项类型携带「定义身份 + 所有泛型实参」，所以一次哈希就覆盖了完整特化，无需为 const 值另造一套编码。

PTX 名的内联（dedup）缓存：

[cuda-host/src/type_id.rs:79-94](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L79-L94) —— `__intern_generic_kernel_name(base, hash)`：进程级 `HashMap<(base, hash), &'static str>`，同一特化只 `Box::leak` 一次格式化字符串，重复启动复用同一个 `&'static str`。

宏侧如何调用它来生成 `ptx_name()`：

[cuda-macros/src/lib.rs:4018-4023](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L4018-L4023) —— 在 `GenericCudaKernel::ptx_name()` 实现里：`type_id_u128_of_val(&kernel_entry::<T,N>)` 算哈希，再 `__intern_generic_kernel_name(base, hash)` 得到 PTX 名（外层生成器 `generate_generic_cuda_kernel_impl` 见 [cuda-macros/src/lib.rs:3974-4039](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L3974-L4039)）。

命名契约的「真源」文档在 `launch.rs`：

[cuda-host/src/launch.rs:86-102](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs#L86-L102) —— `GenericCudaKernel` trait 的 *PTX Naming Scheme* 段明确写出「`<base>_TID_<hex32>`，host 用 `type_id_u128_of_val`、backend 哈希 `Instance::ty`，二者都用 rustc 的擦区域稳定哈希；因此生命周期不会制造伪 PTX 变体，名字定长与泛型 arity 无关」，并解释为何不附加 `'static` 约束。

并有测试把这条契约钉死：

[cuda-host/tests/cuda_module_api.rs:413-444](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/tests/cuda_module_api.rs#L413-L444) —— `ptx_name_for_closure_generic_matches_tid_scheme` 配合 `is_lowercase_hex_32`/`split_tid_name` 校验 `ptx_name()` 形如 `<base>_TID_<32 个小写十六进制>`（同文件 446-506 行还有按闭包类型、const 特化、混合特化分离 PTX 名的一组测试）；`type_id.rs` 内另有 `distinct_types_hash_distinctly`、`static_borrow_collides_with_free_borrow`（擦生命周期）等单测（[type_id.rs:100-189](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L100-L189)）。

#### 4.3.4 代码实践

**目标**：验证「同类型哈希稳定、不同类型哈希不同、生命周期不影响」。

**步骤**：阅读 `type_id.rs` 末尾的单元测试（96-189 行），这是「源码阅读型实践」，无需 GPU。

**需要观察的现象**：关注三条断言：

1. `distinct_types_hash_distinctly`：`type_id_u128::<f32>() != type_id_u128::<i32>()`（[type_id.rs:100-104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L100-L104)）。
2. `static_borrow_collides_with_free_borrow`：`type_id_u128::<&'static i32>() == type_id_u128::<&'a i32>()`（自由生命周期与 `'static` 哈希相同，[type_id.rs:113-123](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L113-L123)）。
3. `generic_kernel_names_are_allocated_once_per_specialization`：`(base, hash)` 只内联一次，名字形如 `tile_TID_0000...0004`（[type_id.rs:178-189](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs#L178-L189)）。

**预期结果**：理解为什么「`scale::<f32>` 与 `scale::<i32>` 必然得到不同 PTX 名，而闭包借用不同生命周期不会泛滥出大量 PTX 变体」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `type_id_u128` 用 `core::intrinsics::type_id` 而不是稳定的 `TypeId::of::<T>()`？

**答案**：稳定 API 要求 `T: 'static`，会把所有「捕获非 `'static` 借用」的闭包挡在类型化启动路径之外。内部 intrinsic 的边界是 `T: ?Sized`，没有 `'static` 要求，且产生**完全相同**的 128 位值（两者走同一条擦区域稳定哈希流水线）。「跨 `stream.synchronize()` 保活借用」本就是调用方责任，不应由类型系统强行收紧。

**练习 2**：`<hex32>` 为什么用 32 个十六进制字符？

**答案**：128 位哈希 = 16 字节 = 恰好 32 个十六进制字符（`{hash:032x}`）。定长意味着 PTX 名长度与泛型 arity（类型参数 + const 参数的个数）无关，便于 backend 与 host 用统一的字符串匹配/解析逻辑。

---

### 4.4 host_closure / generic 示例精读

#### 4.4.1 概念说明

最后把三个机制（单态化、闭包标量化、`type_id_u128` 命名）在两个示例里串起来看。`generic` 侧重「泛型类型单态化」，`host_closure` 侧重「闭包类型的单态化 + 三种闭包 trait」，二者其实是同一套机制的两种触发方式。

#### 4.4.2 核心流程（串起来）

以 `module.map::<f32, _>(..., move |x| x * factor, ...)` 为例，端到端发生的事：

1. **单态化**：`::<f32>` 与闭包的匿名类型 `F` 一起，触发 rustc 生成 `map::<f32, F_某闭包>` 的 device 代码。
2. **命名**：宏生成的 `map_ptx_name::<f32, F>()` 用 `type_id_u128_of_val(&kernel_entry::<f32, F>)` 算出 `map_TID_<hex32>`。
3. **查找**：host 用该名字 `module.load_function("map_TID_...")`（首次加载后缓存在 `__generic_functions`）。
4. **压栈**：闭包被 `push_kernel_scalar` 作为单条 byval 压入参数包，与 backend 的单条 byval `.param` 对齐。
5. **执行**：设备侧把这条 `.param` 读回成 `F`，每个线程在自己那份 `Copy` 副本上调 `f(input[idx])`。

#### 4.4.3 源码精读

`generic` 里的 `closure_capture<T>` 演示了**内核体内**定义的捕获闭包（区别于 host 传入的闭包）：

[generic/src/main.rs:74-85](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L74-L85) —— 内核体内 `let transform = move |x: T| (x + bias) * scale;` 再 `apply_unary(transform, ...)`。注释（72-73 行）点出一个 importer 细节：rustc 在闭包的 `[kind, sig, tupled_upvars]` 后缀前会前置父泛型实参，importer 必须从后缀读 upvar 元组，而非用硬编码下标。

`host_closure` 同时覆盖 `Fn` / `FnMut` / `FnOnce` 三条设备侧派发路径：

[host_closure/src/main.rs:111-139](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L111-L139) —— `map_mut`（`FnMut`，走 `<F as FnMut>::call_mut`）与 `map_once`（`FnOnce`，走 `call_once`，靠 `Copy` 让每线程都能 call_once 一份新拷贝）。

const 泛型特化：闭包 + 显式 const 实参，必须能在低层启动宏里正确转发：

[host_closure/src/main.rs:82-92](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L82-L92) 与 [host_closure/src/main.rs:580-607](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L580-L607) —— `map_with_const::<F, const OFFSET: u32>` 分别以 `::<_, 4>`（`cuda_launch!`，581-589）与 `::<_, 8>`（`cuda_launch_async!`，596-604）启动（两条都在 `unsafe` 块中），断言 `result[i] == input[i]*factor + OFFSET`。

README 的 Quick Start 给了最短的「泛型 + 闭包」样例：

[README.md:79-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L79-L86) —— 一句话总结：泛型 `map` 接受任意 `Fn(T)->T`，`#[cuda_module]` 把设备制品嵌入二进制并生成类型化 `module.map::<f32,_>(...)`，闭包被「captured, scalarized, and passed as kernel parameters」；同段点明 `LaunchConfig` 是原始数据故启动需 `unsafe`，而 `#[launch_contract]` 内核改走受检 `PreparedLaunch`（具体的 `map` 内核与 `unsafe` 启动样例见 [README.md:38-72](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L38-L72)）。

能力清单把这两项列为正式特性：

[README.md:311-312](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L311-L312) —— 「Generic functions with monomorphization」「Closures with captures (move and non-move via HMM)」。

#### 4.4.4 代码实践

见第 5 节「综合实践」——在那里你会动手写一个泛型 `map<T,F>` 并确认 `f32`/`i32` 产生两个不同 PTX 入口。

#### 4.4.5 小练习与答案

**练习 1**：`generic` 与 `host_closure` 都用 `map/scale`，但前者闭包在**内核体内**定义，后者闭包从 **host 传入**。两者的单态化扳机分别是什么？

**答案**：`generic` 的 `closure_capture` 闭包随 `T` 一起被 `module.closure_capture::<f32>(...)` 的类型化调用单态化；`host_closure` 的闭包类型 `F` 由 host 的闭包字面量在 turbofish `::<f32, _>` 处推断绑定（`_` 即闭包的匿名类型），并由宏生成的实例化助手 `instantiate_map(&__closure)` 钉住。

**练习 2**：为什么 `map`、`map_mut`、`map_once` 是**三个不同的 PTX 入口家族**，即便闭包体一模一样？

**答案**：它们的 `Fn`/`FnMut`/`FnOnce` trait bound 不同，导致设备侧派发路径（`Fn::call` / `FnMut::call_mut` / `FnOnce::call_once`）不同，单态化出的函数项类型不同，`type_id_u128` 哈希不同，PTX 名（`map_TID_*` / `map_mut_TID_*` / `map_once_TID_*`）也不同。

---

## 5. 综合实践

**任务**：基于 `host_closure` 示例，写一个泛型 `map<T, F>` 核函数，分别用 `f32` 和 `i32` 启动，确认生成了两个不同 PTX 入口。

**操作步骤**：

1. 复制 `host_closure` 示例为新示例（参考 u1-l3 的 `cargo oxide new`，或手动在 `crates/rustc-codegen-cuda/examples/` 下建一个带 `[workspace]` 的独立 crate，照搬 `host_closure/Cargo.toml` 结构）。
2. 在 `#[cuda_module] mod kernels` 里写一个泛型核（可几乎照搬 [host_closure/src/main.rs:70-77](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L70-L77) 的 `map`）：

   ```rust
   // 示例代码：基于 host_closure 的 map 改写
   #[kernel]
   pub fn map<T: Copy, F: Fn(T) -> T + Copy>(f: F, input: &[T], mut out: DisjointSlice<T>) {
       let idx = thread::index_1d();
       let i = idx.get();
       if let Some(o) = out.get_mut(idx) {
           *o = f(input[i]);
       }
   }
   ```

3. 在 `main` 里分别用 `f32` 和 `i32` 启动（参照 [generic/src/main.rs:121-161](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L121-L161) 的两段 turbofish 调用，两段都在 `unsafe` 块中）：

   ```rust
   // 示例代码
   module.map::<f32, _>(&stream, cfg, move |x: f32| x * 2.0, &in_f, &mut out_f)?;
   module.map::<i32, _>(&stream, cfg, move |x: i32| x * 3,  &in_i, &mut out_i)?;
   ```

4. 用 `cargo oxide pipeline <你的示例名>` 看中间产物（**无需 GPU**）。

**需要观察与确认的现象**：

- 在生成的 PTX / `.ll` 中找到**两个** `map` 相关入口，PTX 名形如 `map_TID_<32hex>`，且两者的 `<32hex>` 后缀**不同**——这正是 `f32` 与 `i32` 单态化 + `type_id_u128` 命名的直接证据。
- 如果机器有 GPU 且架构达标，`cargo oxide run <你的示例名>` 应分别输出 `f32` 与 `i32` 的正确结果。

**预期结果**：两个不同的 `map_TID_*` 入口；运行结果 `out_f[i] == in_f[i]*2.0`、`out_i[i] == in_i[i]*3`。

> 待本地验证：PTX 文本中的确切符号、`pipeline` 产物路径，需在本机运行后确认；本讲不假设你已运行。

**延伸（可选）**：再加一个 `map::<f32, _>` 但用**不同捕获数**的闭包（如 `|x| x*2.0` 与 `move |x| x*a + b`），观察是否又多出一个 `map_TID_*` 入口——验证「闭包类型不同 → 单态化 → 不同 PTX 名」（结合 4.2.5 练习 1）。

## 6. 本讲小结

- **泛型单态化是 rustc 干的**：cuda-oxide 只用宏生成「强制单态化」胶水（`#[inline(never)]` + 易失指针取地址），让每个用到的类型实例都被 backend 收集器发现；泛型核在 `load()` 时不预加载，而是启动时按名现取并缓存。
- **闭包值就是匿名结构体**：cuda-oxide 把整个闭包结构体当作**单条 byval `.param`** 穿过内核边界（`push_kernel_scalar`），host 与 backend 两侧都不展平捕获；零捕获闭包是 ZST，两端一致地丢弃。
- **`type_id_u128` 是 host/backend 的共同命名真源**：用 `core::intrinsics::type_id`（`T: ?Sized`，非 `'static`，便于借用闭包）算出与 backend `tcx.type_id_hash` 完全相同的 128 位稳定哈希，格式化为定长的 `<base>_TID_<hex32>`。
- **生命周期被擦除**：稳定哈希擦除 region，所以借用不同生命周期的闭包不会泛滥出伪 PTX 变体；而不同类型 / 不同 const / 不同闭包类型必然产生不同入口。
- **Fn / FnMut / FnOnce 走不同设备侧派发**，因此也是不同的 PTX 入口家族；`Copy` 约束让每线程都有自己的闭包副本。
- **两类闭包**：`move` 闭包按值传值；非 `move` 闭包捕获 host 引用，依赖 HMM 才能在 GPU 解引用。

## 7. 下一步学习建议

- **u3-l1 / u3-l3（宿主运行时与异步执行）**：本讲的类型化启动方法 `module.map::<f32,_>(...)` 是同步路径；异步版 `map_async` 返回惰性 `DeviceOperation`，可结合 `and_then`/`zip` 构建依赖图——去 `host_closure` 的 `run_launch_matrix!` 宏里对照看四种启动路径（同步/异步 × 类型化/`cuda_launch!`）。
- **u4（编译流水线总览）**：本讲反复提到的「backend 收集器」「importer 读 upvar 元组」「`Instance::ty` 哈希」都发生在 `mir-importer`/`rustc-codegen-cuda` 里；想彻底搞清单态化实例如何从 MIR 流到 PTX，进入 U4。
- **继续阅读源码**：
  - [cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs) 的 `generate_generic_cuda_kernel_impl`（3974-4039 行，含强制单态化的 `<name>_ptx_name` 助手 4029-4037）与闭包压栈（`CudaLaunchArg::Closure` 分支 5452-5469 行、`cuda_launch!` 展开里 `__closure` 实例化 5546-5568 行）；
  - [cuda-host/src/type_id.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/type_id.rs) 的全部测试，体会「擦区域稳定哈希」的契约；
  - `const_generic`、`cross_crate_kernel` 示例，看 const 泛型与跨 crate 内核如何复用同一套 `_TID_` 命名。
