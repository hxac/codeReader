# 从 NVVM IR 到 cubin：libNVVM + nvJitLink + libdevice（含 FMA 契约）

## 1. 本讲目标

本讲承接 u4-l4（MIR Lowering 鸟瞰）与 u3-l2（模块加载与内嵌制品）。在 u4-l4 里我们看到，cuda-oxide 默认把设备代码 lowering 到 LLVM IR，再用 `llc` 编译出 `.ptx`。但当内核用到了浮点数学函数（`sin`/`cos`/`exp`/`pow`/`atan2`……）时，这条「`llc` → PTX」的路就走不通了——因为这些函数最终要调用 CUDA 的 `__nv_*` libdevice 符号，而 libdevice 是以 LLVM bitcode 形式分发的，必须交给 NVIDIA 自己的链接器去内联。

学完本讲，你应当能够：

- 说清 **NVVM IR / LTOIR / cubin** 三者的区别，以及为什么浮点数学内核要走这条「绕过 `llc`」的支线。
- 描述 cuda-oxide 宿主运行时如何用 **libNVVM** 把 NVVM IR（连同 libdevice）编译成 LTOIR，再用 **nvJitLink** 把 LTOIR 链接成可在 GPU 上运行的 cubin。
- 跟踪 **FMA 收缩策略**（fused multiply-add contraction）从 `CUDA_OXIDE_NO_FMA` / `--no-fmad` 一路传到 `.options` 边车、再传到 libNVVM 与 nvJitLink 的 `-fma=1/0` 选项，最终约束机器码的全链路。
- 理解 `.oxart` 制品里的 `compile-options` 位域（v1/v2 版本之分）如何把「这段设备代码必须以哪种浮点策略做最终 codegen」这一契约附着到产物上，保证未来某天在任意机器上加载它时都不会偷偷改变浮点语义。

## 2. 前置知识

在进入本讲前，最好已经建立以下直觉（u4-l4 与 u3-l2 已铺垫）：

- **三种设备产物**：cuda-oxide 的设备代码最终可以打包成四种 payload——PTX（人可读的虚拟指令集）、NVVM IR（基于 LLVM 的、带 `__nv_*` libdevice 引用的 IR）、LTOIR（链接时优化 IR，bitcode）、cubin（某具体 SM 架构的机器码）。本讲的主角是中间两种 IR 与最后一种机器码。
- **libdevice 是什么**：NVIDIA 把 `sin`/`cos`/`exp` 等 transcendental 函数实现成一份 LLVM bitcode（`libdevice.10.bc`），随 CUDA Toolkit 一起发布在 `<root>/nvvm/libdevice/` 下。GPU 上没有「标准库」，所以这些数学函数不能像 host 端那样链接一个 `.so`，而必须在 **链接期** 把对应函数体内联进你的内核。
- **FMA（fused multiply-add）收缩**：编译器看到 `a*b + c` 时，可以选择用一条硬件 `fma` 指令完成（只舍入一次，更快更准），也可以拆成一条 `mul` 加一条 `add`（舍入两次，但与「先乘后加」的朴素语义逐位一致）。这两种结果在最后一位（ULP）上可能不同，因此是否允许收缩是一个 **可复现性契约**，不能让最终机器码自己决定。
- **`.oxart` 制品与边车（sidecar）**：设备代码被打包进 `.oxart` 段嵌入可执行文件；除了 payload 本体，制品还会带若干「边车」小文件（`.target`、`.options`），记录它当初是为哪个架构、用哪种编译策略生成的。

如果你对「raw LaunchConfig 启动为何 unsafe」「`#[cuda_module]` 如何生成 loader」还不熟，建议先看 u2-l1 与 u3-l2。

## 3. 本讲源码地图

本讲的核心源码集中在宿主运行时与制品层，编译器层只涉及「策略如何注入」一个点。

| 文件 | 作用 |
| --- | --- |
| [`crates/cuda-host/src/ltoir.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs) | 本讲主战场。封装「NVVM IR + libdevice → libNVVM → LTOIR → nvJitLink → cubin/PTX」整条链路，以及运行时按 `.target`/`.options` 边车选产物、查活设备、选执行路由的逻辑。 |
| [`crates/oxide-artifacts/src/lib.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs) | `.oxart` wire 格式与 `ArtifactCompileOptions`（FMA 契约的位域表示）、v1/v2 版本协商、`.options` 边车文本编解码。 |
| [`crates/rustc-codegen-cuda/src/lib.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs) | codegen 后端。这里只看一处：`write_device_artifact_object` 如何把 `allow_fma_contraction` 写进 NvvmIr/Ltoir 制品的 compile-options。 |
| [`crates/rustc-codegen-cuda/src/device_codegen.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs) | 读取 `CUDA_OXIDE_NO_FMA` / `CUDA_OXIDE_EMIT_NVVM_IR` 环境变量，组装 `PipelineConfig` 下发给流水线。 |
| [`crates/cuda-host/src/embedded.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs) | 内嵌制品的运行时加载。对 NVVM IR / LTOIR payload，读出 `compile_options` 并交给 `ltoir` 模块做最终 cubin 编译。 |
| [`crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs) | 一个「手搓版」LTOIR 管线示例：用 C 工具分别包 libNVVM 与 nvJitLink，把 cuda-oxide 的 `.ll` 与外部 C++ 的 LTOIR 链成 cubin，直观展示两阶段链接。 |
| [`crates/rustc-codegen-cuda/examples/libdevice_math/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/libdevice_math/src/main.rs) | 走标准 `#[cuda_module]` API 调用 `f32::sin()`/`exp()` 等，自动触发 NVVM IR 路径，是本讲代码实践的主样例。 |

---

## 4. 核心概念与源码讲解

### 4.1 NVVM IR 与 libdevice：为什么数学内核要走支线

#### 4.1.1 概念说明

在标准 PTX 路径里（u4-l4 已讲），cuda-oxide 的设备流水线终点是：

```
dialect-mir → LLVM dialect → LLVM IR (.ll) → (llc) → PTX (.ptx)
```

这条终点由 codegen 后端顶部的 ASCII 架构图描述：

[`crates/rustc-codegen-cuda/src/lib.rs:78-80`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L78-L80) —— 标准路径里 `.ll` 经 `llc` 变成 `.ptx`。

但当内核里出现 `x.sin()`、`x.exp()`、`libm::sinf(v)` 这类调用时，mir-importer 的浮点数学派发会把它 lowering 成对 CUDA libdevice 符号的调用，例如 `__nv_sinf`、`__nv_expf`、`__nv_powf`。这些符号 **不在你的内核里**，也不在 PTX 指令集里——它们的实现是一份 LLVM bitcode（`libdevice.10.bc`）。`llc` 不会去内联外部 bitcode，于是直接产出 PTX 会得到一堆「未定义符号」。

解决办法是换一条终点：**产出 NVVM IR 而非 PTX，跳过 `llc`**，把「内联 libdevice + 出机器码」的活儿交给 NVIDIA 自己的两件套——libNVVM 与 nvJitLink。`ltoir.rs` 的模块级文档第一段就讲清了这条支线为何存在：

[`crates/cuda-host/src/ltoir.rs:6-18`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L6-L18) —— 当内核用到 Rust 浮点数学 intrinsic 时，cuda-oxide 把它们 lowering 成 `__nv_*` libdevice 调用、自动检测其存在、改出 NVVM IR（`.ll`）而非 `.ptx`、并跳过 `llc`；随后由应用完成「libNVVM 编译为 LTOIR → nvJitLink 链接成 cubin」两步。

几个术语对齐：

- **NVVM IR**：基于 LLVM IR 的文本格式（`.ll`），带 `nvvm.annotations`/`nvvmir.version` 等 NVVM 元数据。它仍是「虚拟架构」的、可移植的，引用了 `__nv_*` 但尚未内联。
- **LTOIR**（Link-Time Optimization IR）：libNVVM 把 NVVM IR 与 libdevice 一起优化、内联后产出的 LLVM bitcode。仍是虚拟架构，但符号已经解析完。
- **cubin**：nvJitLink 把 LTOIR 针对 **具体 SM 架构** 编译出的二进制机器码，可直接被 CUDA driver 加载执行。

> 注意：是否走 NVVM IR 路径在 cuda-oxide 里目前由 `--emit-nvvm-ir` / `CUDA_OXIDE_EMIT_NVVM_IR` 显式选择（见 4.4.2）。换言之，编译期决定产物形态，运行期再决定怎么把产物变成可加载的 cubin。

#### 4.1.2 核心流程

把「写一个用 `sin` 的内核」到「GPU 上跑出结果」串起来：

```text
Rust 源码 (#[kernel] 用了 x.sin())
   │ rustc + cuda-oxide 后端
   ▼
mir-importer: 把 x.sin() 派发为 call __nv_sinf   (参见 u6-l2 intrinsic 翻译)
   │ mir-lower lowering
   ▼
LLVM IR (NVVM IR 文本, .ll)   ← 引用 __nv_sinf, 未定义
   │ ✗ 跳过 llc
   ▼  (运行期, cuda-host::ltoir)
libNVVM:  add libdevice.10.bc + .ll → verify → compile -gen-lto
   ▼
LTOIR bitcode (.ltoir)        ← __nv_sinf 已被内联成具体指令序列
   │
   ▼
nvJitLink: add .ltoir, -arch=sm_XX -lto → cubin
   ▼
cubin (具体 SM 机器码)        ← cuModuleLoadData 即可执行
```

整条链路对调用方是透明的：用 `#[cuda_module]` 写出来的 `module.load(&ctx)` 不需要你区分走的是 PTX 还是 NVVM IR——`load_kernel_module` 会自己看 `.target` 边车决定。

#### 4.1.3 源码精读

`ltoir.rs` 的模块文档把整条链路凝练成三条职责，值得逐句读：

[`crates/cuda-host/src/ltoir.rs:11-18`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L11-L18)：三步走——① libNVVM 把 NVVM IR（已加 libdevice）编译成 LTOIR，使 `__nv_*` 被内联；② nvJitLink 把 LTOIR 链成 cubin（同架构）或 PTX（pre-Blackwell 模块在 Blackwell 上跑时的前向兼容桥）；③ 用 CUDA driver 加载该镜像。

文档还强调了一个工程取舍：整个过程只 `dlopen` `libnvvm.so` 与 `libnvJitLink.so`，**不依赖任何外部 C 工具、不需要 symlink `tools/` 目录**：

[`crates/cuda-host/src/ltoir.rs:27-29`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L27-L29)。这意味着只要机器装了 CUDA Toolkit，cuda-oxide 的运行时就够了——对比 4.1.4 里 `device_ffi_test` 那种「自己写 C 工具包 libNVVM」的老路，能体会到这套封装的价值。

libdevice 的发现由 `find_libdevice` 完成，搜索顺序在文档里写得很清楚：

[`crates/cuda-host/src/ltoir.rs:38-39`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L38-L39) 与 [`crates/cuda-host/src/ltoir.rs:1059-1062`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1059-L1062)：先 `CUDA_OXIDE_LIBDEVICE`，再在各 toolkit 根下的 `nvvm/libdevice/libdevice.10.bc` 找。

#### 4.1.4 代码实践

**实践目标**：用一个真正调用 libdevice 的内核，确认它产出的是 NVVM IR（`.ll`）而非 PTX（`.ptx`）。

**操作步骤**：

1. 进入 `libdevice_math` 示例目录，阅读它的两个 kernel：
   [`crates/rustc-codegen-cuda/examples/libdevice_math/src/main.rs:14-49`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/libdevice_math/src/main.rs#L14-L49)。注意 `swiglu_libdevice` 用了 `(-xi).exp()`，`math_functions` 用了 `xi.sin()`/`xi.exp()`/`(xi+1.25).sqrt()`/`xi.atan()`/`atan2_y[i].atan2(xi)`/`xi.acos()`/`xi.tan()`——这些都会变成 `__nv_*` 调用。
2. 用 `cargo oxide pipeline` 跑出中间产物（无需 GPU，只需编译期工具链）：

   ```bash
   cargo oxide pipeline libdevice_math --emit-nvvm-ir --arch sm_90
   ```

   `--emit-nvvm-ir` 会被 cargo-oxide 翻成 `CUDA_OXIDE_EMIT_NVVM_IR=1` 注入子进程（见 4.4.2）。

3. 到示例的产物目录（通常是示例自身目录或 `target/...`）下查找 `libdevice_math.*`。

**需要观察的现象**：

- 应当能看到 `libdevice_math.ll`（NVVM IR 文本）与一个 `libdevice_math.target` 边车。
- 用编辑器打开 `.ll`，搜索 `__nv_`，应能看到形如 `declare float @__nv_sinf(...)` 或对它的 `call`——这就是 libdevice 符号引用。
- **不应**出现 `libdevice_math.ptx`（或即便有也是上一次 PTX 构建的残留，且 `.target` 边车会让加载器优先选 NVVM IR）。

**预期结果**：`.ll` + `.target` 存在，`.ll` 内含 `__nv_*` 引用，证明此内核走了 NVVM IR 支线、跳过了 `llc`。

> 待本地验证：实际命令输出取决于本机是否安装 CUDA Toolkit 与 `llc`；若无 toolkit，`pipeline` 子命令本身会报缺库。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `math_functions` 里所有的 `.sin()/.exp()/...` 都删掉，只剩加减乘，`--emit-nvvm-ir` 还会成功吗？产物里还会有 `__nv_*` 吗？

> **答**：`--emit-nvvm-ir` 仍会成功（它只是强制走 NVVM IR 产物形态），但因为内核不再调用任何 transcendental 函数，`.ll` 里不会再出现 `__nv_*` 引用。这种情况下其实走 PTX 路径也完全可以——NVVM IR 路径的 **必要性** 来自 libdevice 符号，而非 NVVM IR 本身。

**练习 2**：NVVM IR、LTOIR、cubin 三者，哪个是「虚拟架构、符号未解析」的，哪个是「具体 SM、可直接执行」的？

> **答**：NVVM IR 与 LTOIR 都是虚拟架构（`compute_XX`），区别在于前者仍引用未定义的 `__nv_*`、后者已把 libdevice 内联解析完；cubin 是具体 SM（`sm_XX`）的机器码，可直接 `cuModuleLoadData` 执行。

---

### 4.2 libNVVM → LTOIR：把 libdevice 内联进去

#### 4.2.1 概念说明

libNVVM 是 NVIDIA 提供的「NVVM IR 编译器」动态库（`libnvvm.so`）。它的输入是 NVVM IR 文本（可多份模块），输出是 LLVM bitcode。它的核心能力有二：① 把 libdevice 的 bitcode 与你的 IR 一起做链接期优化，把 `__nv_sinf` 这种调用 **内联** 成针对目标架构的具体指令序列；② 做 NVVM 级别的合法化（legalization），保证产物能被后续 nvJitLink 接受。

cuda-oxide 用 `libnvvm-sys` crate 以 `dlopen` 方式加载它，并在 `ltoir.rs` 里把「构造 Program → 加模块 → verify → compile」封装成 Rust 函数。

#### 4.2.2 核心流程

`compile_nvvm_ir_to_ltoir_with` 是这一阶段的核心，伪代码如下：

```text
fn compile_nvvm_ir_to_ltoir_with(nvvm_ir, module_name, arch, libdevice, allow_fma):
    prog = Program::new()
    prog.add_module(libdevice, "libdevice.10.bc")   # 先加 libdevice
    prog.add_module(nvvm_ir, module_name)            # 再加内核模块
    arch_opt = "-arch=" + arch.compute()             # 虚拟架构 compute_XX
    prog.verify([arch_opt])                          # 校验 NVVM IR 合法
    options = ["-arch=compute_XX", "-gen-lto", "-fma=1 或 -fma=0"]
    return prog.compile(options)                     # 产出 LTOIR bitcode
```

两个关键点：

1. **libdevice 先加、内核后加**。注释说顺序严格讲无所谓（libNVVM 自己做符号解析），但与 NVCC 和 `device_ffi_test` 的 C 工具保持一致。
2. **`-gen-lto`** 让 libNVVM 产出 LTOIR（bitcode）而非 PTX；`-arch=compute_XX` 用虚拟架构；`-fma=1/0` 把 FMA 策略下发给 libNVVM（详见 4.4）。

#### 4.2.3 源码精读

编译主体：

[`crates/cuda-host/src/ltoir.rs:517-537`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L517-L537)：`compile_nvvm_ir_to_ltoir_with`。先 `Program::new`，`add_module(libdevice_bytes, "libdevice.10.bc")` 先加 libdevice，`add_module(nvvm_ir, module_name)` 再加内核；`verify` 用 `-arch=compute_XX`；`compile` 用 `nvvm_compile_options(...)` 产出的选项。注释解释了「libdevice 先加」是惯例而非必需。

选项构造：

[`crates/cuda-host/src/ltoir.rs:539-547`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L539-L547)：`nvvm_compile_options` 产出 `["-arch=compute_XX", "-gen-lto", "-fma=1" 或 "-fma=0"]`。这里第一次出现 FMA 策略向 libNVVM 的下发。

在编译前还有一道「前端校验」`validate_nvvm_frontend`：

[`crates/cuda-host/src/ltoir.rs:488-515`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L488-L515)：① 检查 libNVVM 接受的 IR 版本必须是 `2.0`（cuda-oxide 只发 2.0），否则 `UnsupportedNvvmIrVersion`；② 查 libNVVM 为该 target 报告的 LLVM 主版本号是否与 cuda-oxide 期望的方言（legacy LLVM 7 vs 现代 opaque-pointer）一致，不一致就 `DialectMismatch`。这是为了在运行期发现「toolkit 太老/太新导致方言错配」的硬错误。

整个第二阶段的对外入口有两个层次：

- 文件版 [`crates/cuda-host/src/ltoir.rs:240-300`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L240-L300)：`build_cubin_from_ll` 读 `.ll` 路径、读 `.options` 边车拿 FMA 策略、校验 `.target`、走缓存编译、把 `.ltoir` 与 `.cubin` 写回源文件旁。
- 内存版 [`crates/cuda-host/src/ltoir.rs:317-328`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L317-L328)：`build_cubin_from_nvvm_ir_with_options` 接收内存里的 NVVM IR 字节，对应内嵌 `.oxart` 的 NVVM IR payload 路径（不落地为文件）。

#### 4.2.4 代码实践

**实践目标**：动手把一份 NVVM IR 编译成 LTOIR 与 cubin，眼见为实。

**操作步骤**：

1. 在 4.1.4 得到的 `libdevice_math.ll` 基础上，写一个最小的 Rust 程序调用 cuda-host 的公开 API：

   ```rust
   use cuda_host::ltoir;
   // ll_path 指向 libdevice_math.ll, arch 是当初 --arch 指定的值
   let cubin_path = ltoir::build_cubin_from_ll(
       &ll_path,
       "sm_90",
   ).expect("build cubin");
   ```

   `build_cubin_from_ll` 是 [`crates/cuda-host/src/ltoir.rs:240-243`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L240-L243) 的公开入口。

2. 编译运行（需要 CUDA Toolkit 可被发现）。

**需要观察的现象**：

- 在 `ll_path` 同目录下新生成 `libdevice_math.ltoir`（bitcode，二进制）与 `libdevice_math.cubin`（ELF，以 `\x7fELF` 开头）。
- `libdevice_math.target` 边车被写入目标架构。
- 第二次运行同样的调用，日志（`CUDA_OXIDE_VERBOSE=1`）会显示 cubin cache hit（见 4.2.5 与 4.3.4 的缓存机制）。

**预期结果**：得到一个合法 cubin，且其字节以 `\x7fELF` 起始（ltoir.rs 的 `live_file_cache_hits_and_source_changes_miss` 测试就断言了这一点）。

> 待本地验证：`build_cubin_from_ll` 要求 `.ll` 同目录有匹配的 `.target`/`.options` 边车（由 cargo-oxide pipeline 生成），或显式传 arch 且无 `.target` 冲突；缺 toolkit 时 `LibNvvm::load()` 会失败。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `compile_nvvm_ir_to_ltoir_with` 要把 libdevice **先于** 内核模块加进 Program？如果反过来会怎样？

> **答**：注释明确说顺序严格讲不影响正确性（libNVVM 自做符号解析），先加 libdevice 只是沿用 NVCC/`device_ffi_test` 的惯例。反过来也能成功，但保持惯例有助于和 NVIDIA 工具链的输出可比对。

**练习 2**：`validate_nvvm_frontend` 在编译前检查 IR 版本必须是 `2.0`。如果用户机器上的 CUDA Toolkit 自带一个只认 NVVM IR 1.x 的老 libNVVM，会发生什么？

> **答**：返回 [`LtoirError::UnsupportedNvvmIrVersion`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L154-L156)（提示「installed libNVVM accepts ..., but cuda-oxide emits NVVM IR 2.0」），把「工具链版本不匹配」从后续的玄学链接错误提前到一条清晰报错。

---

### 4.3 nvJitLink → cubin：把虚拟架构落地成机器码

#### 4.3.1 概念说明

nvJitLink 是 NVIDIA 较新的链接器（取代了老的 `cuLink`），专为 LTOIR 链接设计。它的输入是一份或多份 LTOIR bitcode，加上一个目标架构 `-arch=sm_XX`，输出针对该架构的 cubin（或 `-ptx` 模式下输出前向兼容的 PTX）。

为什么有了 libNVVM 还要 nvJitLink？因为 libNVVM 只负责「IR → LTOIR」（虚拟架构、符号解析），**不负责**「针对具体 SM 出机器码」。这一步由 nvJitLink 完成，它还能合并多份 LTOIR（例如 cuda-oxide 内核的 LTOIR + 外部 C++ 库的 LTOIR，这正是 `device_ffi_test` 的玩法）。

#### 4.3.2 核心流程

```text
fn link_ltoir_to_cubin_with_tool_options(ltoir, module_name, arch, allow_fma):
    arch_opt = "-arch=" + arch.sm()                # 注意：用 sm_XX 具体架构
    options  = ["-arch=sm_XX", "-lto", "-fma=1 或 -fma=0"]
    linker   = Linker::new(options)
    linker.add(InputType::Ltoir, ltoir, module_name)
    return linker.finish()                          # cubin bytes
```

注意架构拼写的 **不对称**：

- libNVVM 用 **虚拟架构** `-arch=compute_XX`（[`ltoir.rs:533`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L533)）。
- nvJitLink 用 **具体架构** `-arch=sm_XX`（[`ltoir.rs:420`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L420)）。

这正对应了「LTOIR 仍是虚拟的、cubin 才是具体的」这条边界。

还有一条「PTX 桥」分支：当一份为 pre-Blackwell（legacy LLVM 7 方言）发出的 IR 被加载到 Blackwell（capability ≥ 100）GPU 上时，无法直接出 cubin，nvJitLink 会改出 **前向兼容的 PTX**，交给 CUDA driver 在加载时 JIT。`execution_route` 函数集中裁决走哪条路。

#### 4.3.3 源码精读

cubin 链接主体：

[`crates/cuda-host/src/ltoir.rs:413-425`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L413-L425)：`link_ltoir_to_cubin_with_tool_options`。`-arch=sm_XX`，选项来自 `nvjitlink_lto_options(..., false, allow_fma)`，`Linker::new` 建、`add(InputType::Ltoir, ...)` 加、`finish()` 出 cubin。

选项构造（含 PTX 桥）：

[`crates/cuda-host/src/ltoir.rs:451-462`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L451-L462)：`nvjitlink_lto_options`。基础是 `["-arch=sm_XX", "-lto"]`；若 `emit_ptx` 再加 `"-ptx"`；最后追 `"-fma=1"` 或 `"-fma=0"`。这里第二次出现 FMA 策略下发——这次给 nvJitLink。

PTX 桥链接：

[`crates/cuda-host/src/ltoir.rs:437-449`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L437-L449)：与 cubin 版几乎对称，差别仅在 `emit_ptx=true` 并调 `finish_ptx()`。

路由裁决：

[`crates/cuda-host/src/ltoir.rs:1236-1267`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1236-L1267)：`execution_route(emitted, execution)`。规则：① 同 capability → `Cubin`；② 带架构后缀（如 `sm_90a`）的不能转发到别的 GPU；③ 给更新的 GPU 编的产物不能在更老的 GPU 跑；④ legacy-LLVM 的 pre-Blackwell 产物在 Blackwell（≥100）上跑 → `PtxBridge`；⑤ 其余跨架构一律拒绝（`IncompatibleExecutionTarget`）。注意 `emitted`（产物记录的架构）与 `execution`（当前 GPU 实际架构）是两个独立输入，绝不混用。

`load_kernel_module` 把这一切串起来的「选产物」逻辑：

[`crates/cuda-host/src/ltoir.rs:910-996`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L910-L996)：按 `<name>.target` 是否存在判定 NVVM 输出是否为「当前产物」，再在 `.ll`/`.ltoir`/`.ptx`/`.cubin` 间选一个；选中 NVVM IR 后按 `execution_route` 决定走 cubin 还是 PTX 桥。

#### 4.3.4 代码实践

**实践目标**：观察「同一份 NVVM IR 在不同路由下产出不同形态」。

**操作步骤**：

1. 阅读路由测试，理解裁决规则：
   [`crates/cuda-host/src/ltoir.rs:1478-1509`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1478-L1509)。注意 `same_target_keeps_native_cubin_route` 与 `standard_legacy_target_bridges_forward_to_blackwell_as_ptx` 两个用例分别覆盖 Cubin 与 PtxBridge。
2. 在 4.2.4 的最小程序基础上，分别用 `"sm_90"` 与 `"compute_90"` 调 `build_cubin_from_ll`，对比返回的 cubin 字节是否一致。

**需要观察的现象**：

- `sm_90` 与 `compute_90` 两种拼写归一化后命中同一 cubin（缓存 key 把等价拼写归一，见 [`ltoir.rs:1628-1640`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1628-L1640) 的测试断言）。
- 第二次调用命中 `.oxide-artifacts/ltoir-cubin-cache/v1` 缓存，`CUDA_OXIDE_VERBOSE=1` 下打印 `cubin cache hit`。

**预期结果**：归一化的目标拼写共享缓存；首次构建产生 ELF cubin，再次构建命中缓存不重做。

> 待本地验证：缓存命中要求 libNVVM/libnvJitLink 能被 SHA-256 指纹识别（`load_for_cache`），无法指纹识别（如某些 toolkit 安装）时会 bypass 缓存每次重编，日志会提示。

#### 4.3.5 小练习与答案

**练习 1**：为什么 libNVVM 用 `compute_XX` 而 nvJitLink 用 `sm_XX`？

> **答**：libNVVM 的产物 LTOIR 仍是虚拟架构、可移植的，所以用 `compute_XX`；nvJitLink 负责「虚拟 → 具体」的最后一步，要出能在某 SM 上跑的机器码，所以用 `sm_XX`。这条边界也是 PTX 桥存在的前提：虚拟架构的产物可以保留为 PTX，让 driver 在加载期再决定具体架构。

**练习 2**：`execution_route` 为什么不允许把 `sm_90a`（带后缀）的产物转发到另一块 GPU？

> **答**：后缀（如 `a` 表示 architecture-specific）意味着产物已经用了某架构的专属特性，转发到「同名数值但不同后缀」或别的 GPU 都可能语义错乱。代码在 [`ltoir.rs:1250-1254`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1250-L1254) 直接拒绝，宁可报错也不静默跑。

---

### 4.4 FMA 收缩策略与 compile-options 边车

#### 4.4.1 概念说明

FMA 收缩（contraction）是贯穿全链路的浮点契约。允许收缩（`-fma=1`）时，`a*b+c` 可融合成一条 `fma` 指令（一次舍入）；禁止收缩（`-fma=0`）时强制拆成 `mul`+`add`（两次舍入）。两者在 ULP 级别可能不同，所以这是一个 **必须逐位可复现** 的属性。

cuda-oxide 的设计原则是：**FMA 策略由编译期一次性决定，并随制品一路携带到最终 codegen，任何中间环节都不得擅自改变它。** 这条原则落到三件事上：

1. 编译期：`CUDA_OXIDE_NO_FMA` / `--no-fmad` 决定 `allow_fma_contraction`，写进 `.oxart` 制品的 `compile_options`（仅 NvvmIr/Ltoir 制品）。
2. 制品层：`compile_options` 是一个 `u64` 位域，随 bundle 走；非默认策略会让 bundle 升级到 v2，老读取器会拒绝而非误读。
3. 运行期：加载 NVVM IR/LTOIR payload 时，从 bundle 读回 `fma_contraction_enabled()`，作为 `-fma=1/0` 同时下发给 libNVVM 与 nvJitLink。

#### 4.4.2 核心流程

```text
                        编译期 (rustc-codegen-cuda)
CUDA_OXIDE_NO_FMA ──► device_codegen.allow_fma_contraction   (默认 true)
   (或 cargo oxide --no-fmad)            │
                                         ▼
                            DeviceCodegenResult.allow_fma_contraction
                                         │
                                         ▼ (仅 NvvmIr/Ltoir 制品)
                  write_device_artifact_object: ArtifactCompileOptions
                                         │   .with_fma_contraction(allow_fma)
                                         ▼
                        .oxart bundle.compile_options  (u64 位域)
                        + .options 边车 (fma-contraction=on|off)
                        + .target 第二行 compile-options=v1 标记
                                         │
   ──────────────── 制品随可执行文件分发 ────────────────
                                         │
                                         ▼  运行期 (cuda-host)
                  bundle.compile_options.fma_contraction_enabled()
                                         │
                      ┌──────────────────┴──────────────────┐
                      ▼                                     ▼
            libNVVM: -fma=1/0                       nvJitLink: -fma=1/0
            (compile_nvvm_ir_to_ltoir)              (link_ltoir_to_cubin)
```

环境变量与 cargo-oxide 标志的对应关系（cargo-oxide 是它们的语法糖）：

- `--no-fmad` → `CUDA_OXIDE_NO_FMA=1`（[`crates/cargo-oxide/src/commands.rs:2991-3000`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L2991-L3000) 的 `apply_common_codegen_env`）。
- `--emit-nvvm-ir` → `CUDA_OXIDE_EMIT_NVVM_IR=1`（[`commands.rs:2942-2949`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L2942-L2949) 的 `apply_output_mode`）。

这两组环境变量由 codegen 后端读取：

[`crates/rustc-codegen-cuda/src/device_codegen.rs:660-693`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L660-L693)：`emit_nvvm_ir = CUDA_OXIDE_EMIT_NVVM_IR.is_ok()`；`allow_fma_contraction = CUDA_OXIDE_NO_FMA.is_none()`（默认允许收缩）；两者塞进 `PipelineConfig` 下发。

#### 4.4.3 源码精读

**① 编译期：把策略写进制品。**

[`crates/rustc-codegen-cuda/src/lib.rs:805-814`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L805-L814)：对 NvvmIr 与 Ltoir 制品，用 `ArtifactCompileOptions::new().with_fma_contraction(result.allow_fma_contraction)`；对 Ptx/Cubin 制品用默认（因为它们不再经历 FP-affecting codegen，策略无意义）。这是契约的「写入端」。

**② 制品层：compile-options 的位域表示与版本协商。**

[`crates/oxide-artifacts/src/lib.rs:25-35`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L25-L35)：常量定义。`OPTION_NO_FMA_CONTRACTION = 1<<0`；`COMPILE_OPTIONS_TARGET_MARKER = "compile-options=v1"`（写在 `.target` 第二行，让老的一行读取器拒绝而非误读）；`.options` 边车头 `cuda-oxide-compile-options-v1`。

[`crates/oxide-artifacts/src/lib.rs:42-97`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L42-L97)：`ArtifactCompileOptions(u64)`。`with_fma_contraction(bool)` 置/清第 0 位；`fma_contraction_enabled()` 读第 0 位（0 表允许收缩，符合「全 0 = 历史默认」）；`from_bits` 拒绝未知位（前向兼容）；`sidecar_text()`/`from_sidecar_text()` 编解码 `.options` 文本。

版本协商是这套设计最精巧的一处：

[`crates/oxide-artifacts/src/lib.rs:439-447`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L439-L447)：写 blob 时，若 `compile_options == 默认(全0)` 则写 v1（向后兼容老读取器）；只要携带了任何非默认策略（如禁用 FMA）就升级到 v2，把策略位写在 header `[24:32]`（[`:466`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L466) 的 `write_u64(&mut out, 24, ...)`)。读取端 [`:514-518`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L514-L518) 对 v1 强制返回默认策略——老制品里那 8 字节是保留位，新读取器绝不擅自赋予新含义。测试 [`lib.rs:988-997`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L988-L997) 固化了这条不变量。

**③ 运行期：从制品读回策略并下发。**

文件制品侧（`.options` 边车）：

[`crates/cuda-host/src/ltoir.rs:1140-1152`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1140-L1152)：`read_fma_contraction_option` 读 `<stem>.options`，用 `ArtifactCompileOptions::from_sidecar_text` 解析；文件不存在则视为默认（允许收缩）。`load_kernel_module` 的 NVVM IR 分支就是靠它拿到策略再调 `build_ptx_from_nvvm_ir_with_options(..., allow_fma_contraction)`（[`ltoir.rs:954-960`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L954-L960)）。

内嵌制品侧（bundle 位域）：

[`crates/cuda-host/src/embedded.rs:148-167`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L148-L167)：对 NVVM IR payload，`allow_fma_contraction = bundle.compile_options.fma_contraction_enabled()`，按路由调 `build_cubin_from_nvvm_ir_with_options` 或 `build_ptx_from_nvvm_ir_with_options`。LTOIR payload 对称处理（[`:169-188`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L169-L188)）。

策略最终落到 libNVVM 与 nvJitLink 的选项上（已在 4.2.3 与 4.3.3 见过 `-fma=1/0`）。FMA 策略还参与 **缓存 key**：

[`crates/cuda-host/src/ltoir.rs:803-810`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L803-L810) 与 [`:815-822`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L815-L822)：缓存 key 显式纳入 `nvvm-fma-policy` 与 `nvjitlink-fma-policy`，因为策略变了 cubin 字节就变。测试 [`:1654-1666`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1654-L1666) 固化「策略改变 → key 必变」。

#### 4.4.4 代码实践

**实践目标**：用 `--no-fmad` 让同一份内核产出两份 FMA 策略不同的 cubin，并验证策略确实随 `.options` 边车携带。

**操作步骤**：

1. 选 `libdevice_math`，分别在默认与禁 FMA 下跑 pipeline，把产物分目录存放：

   ```bash
   cargo oxide pipeline libdevice_math --emit-nvvm-ir --arch sm_90
   cp <产物目录>/libdevice_math.options /tmp/fma_on.options

   cargo oxide pipeline libdevice_math --emit-nvvm-ir --arch sm_90 --no-fmad
   cp <产物目录>/libdevice_math.options /tmp/fma_off.options
   ```

2. 对比 `/tmp/fma_on.options` 与 `/tmp/fma_off.options`：
   - on 版应为 `cuda-oxide-compile-options-v1\nfma-contraction=on\n`
   - off 版应为 `cuda-oxide-compile-options-v1\nfma-contraction=off\n`
   - 同时 `.target` 第二行应出现 `compile-options=v1` 标记（仅 off 版，因为 on 版是默认策略、写 v1 不带标记）。
3. 阅读 `nvidia_lto_options_set_fma_policy_explicitly` 测试确认选项形态：

   [`crates/cuda-host/src/ltoir.rs:1343-1360`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1343-L1360) 断言 `nvvm_compile_options(..., false) == ["-arch=compute_86","-gen-lto","-fma=0"]` 等。

**需要观察的现象**：

- 两份 `.options` 内容差异恰为 `on`/`off`。
- 禁 FMA 版的 `.target` 多出 `compile-options=v1` 第二行。
- 若把两份 NVVM IR 分别喂给 `build_cubin_from_ll`，得到的 cubin 字节不同（且缓存 key 不同）。

**预期结果**：FMA 策略以「`.options` 边车 + `.target` 标记」形式随制品落盘，运行期据此把 `-fma=0` 同时下发给 libNVVM 与 nvJitLink。

> 待本地验证：是否真产出不同 cubin 字节取决于内核里是否真有可收缩的 `mul+add`；`swiglu_libdevice`（`xi * sigmoid * yi` 含乘法链）是理想的对照样本。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PTX 与 cubin 制品 **不** 携带 `compile_options`，只有 NVVM IR 与 LTOIR 携带？

> **答**：PTX/cubin 是「最终产物」——它们之后不再经历任何会改变浮点语义的 codegen。而 NVVM IR/LTOIR 还要经 libNVVM/nvJitLink 做最终机器码生成，那一步的 FMA 选择会进入机器码，所以策略必须随制品携带到那一刻。见 [`lib.rs:805-814`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L805-L814)。

**练习 2**：假设你手改了一个 v1 制品，把它的 `.options` 删掉、却保留 `.target` 第二行的 `compile-options=v1` 标记，加载时会怎样？

> **答**：会报 `InvalidCompileOptions: required sidecar is missing`。因为标记的存在承诺了「有一份 `.options` 在」，读 `.target` 时会强制校验 `.options` 存在且合法（[`ltoir.rs:1162-1175`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1162-L1175)）。这正是「标记让老读取器拒绝而非误读」的设计意图。

**练习 3**：`device_ffi_test` 的 `compile_cuda_oxide_ltoir` 在判断是否需要重编时，把 `.options` 也算进源文件依赖（[`:412`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs#L412)）。为什么？

> **答**：因为 `.options` 改了（FMA 策略变了）就要重出 LTOIR，否则会用旧策略的 LTOIR 链出新策略的 cubin，破坏契约。这与 cuda-oxide 自带缓存把 FMA 纳入 key 是同一个道理，只是这里用 mtime 朴素实现。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「全链路追踪」小任务。

**任务**：以 `device_ffi_test` 为标本，画出「Rust 内核 + 外部 C++ LTOIR → 最终 cubin」的完整数据流，并标注 FMA 策略在每一站的载体。

**步骤**：

1. 阅读 `device_ffi_test` 的构建管线 [`crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs:478-485`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs#L478-L485)（`build_pipeline`），它把流程拆成 `build_tools` → `build_external_ltoir` → `compile_cuda_oxide_ltoir` → `link_ltoir` 四步。
2. 注意它与 cuda-oxide 自带 `ltoir` 模块的对应关系：
   - `compile_cuda_oxide_ltoir`（[`:397-428`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs#L397-L428)）调用 C 工具 `compile_ltoir`，后者就是 libNVVM 的薄封装，等价于 `ltoir::compile_nvvm_ir_to_ltoir_with`。它显式读 `device_ffi_test.options`（[`:399`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs#L399)）作为重编依据。
   - `link_ltoir`（[`:438-468`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs#L438-L468)）调用 C 工具 `link_ltoir`，等价于 `ltoir::link_ltoir_to_cubin_with_tool_options`，把 cuda-oxide 的 LTOIR 与外部 C++ 的 LTOIR（`external_device_funcs.ltoir`、`cccl_wrappers.ltoir`）合一。
3. 画一张表，列出 FMA 策略在以下各站的载体：

   | 阶段 | 载体 | 来源 |
   |------|------|------|
   | 编译期决策 | `CUDA_OXIDE_NO_FMA` / `--no-fmad` | cargo-oxide `apply_common_codegen_env` |
   | 写入制品 | `.options` 边车 + `.target` 标记 + bundle v2 位域 | `write_device_artifact_object` |
   | libNVVM 编译 | `-fma=1/0` 选项 | `nvvm_compile_options` |
   | nvJitLink 链接 | `-fma=1/0` 选项 | `nvjitlink_lto_options` |
   | cubin 缓存 key | `nvvm-fma-policy`/`nvjitlink-fma-policy` 字段 | `nvvm_ir_cubin_cache_key_with_options` |

4. **思考题**：`device_ffi_test` 里外部 C++ 的 LTOIR 是用 nvcc 的 `-dc -dlto` 编出来的（[`:359-391`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/device_ffi_test/src/main.rs#L359-L391) `build_external_ltoir`），它的 FMA 策略由 nvcc 决定，与 cuda-oxide 的 `.options` 是两套。当两边策略不一致时，nvJitLink 链接出的 cubin 会遵守哪一边？请结合 4.4 的「策略必须随每份 LTOIR 携带」思考，并写出你的判断与依据（提示：思考 nvJitLink 的 `-fma=` 是全局链接选项还是 per-input）。

> 待本地验证：第 4 题的最终结论需要在真机分别用 `--no-fmad` 与默认跑 `device_ffi_test`、对比 cubin 字节与数值结果才能完全确认。

## 6. 本讲小结

- 浮点数学内核（`sin`/`exp`/...）会被 lowering 成 `__nv_*` libdevice 调用，因此 cuda-oxide 改出 **NVVM IR（`.ll`）而非 PTX、跳过 `llc`**，把出机器码的活交给 libNVVM + nvJitLink。
- **libNVVM** 把 NVVM IR + libdevice.10.bc 编译成 **LTOIR**（`-gen-lto`，虚拟架构 `compute_XX`），在链接期内联 `__nv_*`；**nvJitLink** 再把 LTOIR 链成 **cubin**（`-lto`，具体架构 `sm_XX`）或前向兼容 PTX。
- `execution_route` 用「产物记录架构」与「GPU 实际架构」两个独立输入裁决走 cubin 还是 PTX 桥，跨架构只允许 pre-Blackwell → Blackwell 这一种前向兼容。
- **FMA 收缩策略** 是贯穿全链路的浮点契约：编译期由 `CUDA_OXIDE_NO_FMA`/`--no-fmad` 决定，写进 `.oxart` 的 `compile_options` 位域与 `.options` 边车，运行期读回后同时下发给 libNVVM 与 nvJitLink 的 `-fma=1/0`，并参与 cubin 缓存 key。
- `.oxart` 用 **v1/v2 版本协商** 兼顾向后兼容与策略保真：默认策略写 v1（老读取器无感），非默认策略升级 v2 并在 `.target` 写 `compile-options=v1` 标记，让老读取器拒绝而非误读。
- 这一切对 `#[cuda_module]` 的使用者完全透明——`load`/`load_kernel_module` 会自己看 `.target` 选产物、查活设备、选路由、读策略。

## 7. 下一步学习建议

- **想看清 `__nv_*` 是怎么从一条 `x.sin()` 调用变出来的**：去 u6-l2（mir-importer 深潜：terminator/intrinsics 翻译机），看 mir-importer 的浮点数学派发如何把 `f32::sin` 与 `libm::sinf` 都路由到同一条 `__nv_sinf` intrinsic 翻译路径。
- **想看清 `-fma` 在 lowering 层如何影响指令选择**：去 u6-l3（mir-lower 深潜：ops 转换与算术），看 `convert/ops/arithmetic.rs` 如何据 `allow_fma_contraction` 决定是否给浮点 op 挂 fast-math `contract` 标志。
- **想看「策略随制品分发」的另一面**：回看 u3-l2（模块加载与内嵌制品），本讲的 `compile_options` 边车正是 u3-l2 里 `.oxart` v2 头部 `[24:32]` 位域与锚符号机制的具体应用。
- **想动手扩一个自己的 intrinsic**：去 u6-l4（端到端新增一个 intrinsic），那里给出的「设备 API → dialect op → importer → lowerer」四层模板与本讲的 libdevice 路径互为补充。
- **运行时缓存的工程细节**：若你对 `.oxide-artifacts/ltoir-cubin-cache/v1` 的内容寻址、tool SHA-256 指纹、不可变条目发布感兴趣，可直接精读 [`crates/cuda-host/src/ltoir.rs:47-62`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L47-L62) 的「Native cubin cache」文档段与同 crate 的 `ltoir_cache.rs`。
