# 消费方集成：kernels 与 cupti 如何使用 CudaToolkit

## 1. 本讲目标

前几讲我们一直在读 `pegainfer-build` 这个库本身：discover 的回落链、target_dirs 的架构目录、from_root 的三种布局、existing_deduped 的收敛、header_dir 与 link_search 的输出。本讲换到**消费方视角**，回答一个新问题：**一个真实的 build.rs 拿到 `CudaToolkit` 之后，到底怎么用它的各个出口？**

学完本讲你应该能够：

1. 在 `pegainfer-kernels/build.rs` 近三千行代码中定位**全部**工具包使用点，并逐个说明用途。
2. 说清 `header_dir("cuda.h")` 返回 `None` 时 kernels 的兜底路径（`toolkit.root.join("include")`），以及这个兜底在 conda 布局下为什么不可靠。
3. 对比两个消费方的使用深度：kernels 用 nvcc / header_dir / link_search / root 四个出口，而 cupti 不用 nvcc、却额外用 `root` 拼 `extras/CUPTI` 特例目录。
4. 独立画出「discover 输出 → 各消费点用途 → 最终 cargo: 指令（或编译器 argv）」的完整数据流图。

## 2. 前置知识

本讲假设你已完成 u1（build script 协议、lib.rs 导览）和 u2（探测机制精读）。在此之上补充四个新概念：

- **宿主编译器 vs nvcc**：nvcc 编译 `.cu` 时自己知道 CUDA 工具包（CTK）的头文件在哪；但宿主机的 `cc` / `c++` 编译一个 `#include <cuda_runtime.h>` 的 C 包装文件时**不会**自动找到 CUDA 头，必须显式传 `-I`。这是 `header_dir` 存在的根本原因——它要回答「哪一个目录里**真的**有这个头文件」。
- **`-I` 与 `-isystem` 的区别**：两者都添加头文件搜索目录，但 `-isystem` 把目录标记为「系统头」（抑制其中警告），且优先级低于 `-I`。kernels 正是利用这个优先级差，把 CTK 头目录装进 `-isystem`、把自带的较新 CCCL 装进 `-I`，让后者覆盖前者。
- **cc crate**：Rust 生态驱动宿主 C/C++ 编译器的标准 build-dependency；`cc::Build::include(path)` 等价于给编译器加 `-I path`。
- **CUPTI**（CUDA Profiling Tools Interface）：CUDA 的性能剖析接口。它的头和库在经典安装里**不在**主 `include/`、`lib64/` 下，而在 `extras/CUPTI/` 子树——这是本讲第二个模块的主角。

## 3. 本讲源码地图

| 文件 | 体量 | 本讲角色 |
|---|---|---|
| `pegainfer-kernels/build.rs` | 约 2930 行 | 构建总管：编译全部 CUDA 源、Triton AOT、TileLang、cutedsl，是 `CudaToolkit` 最重的消费方 |
| `pegainfer-cupti/build.rs` | 34 行 | 最小完整消费方：discover → include → compile → link 一条龙，外加 extras/CUPTI 特例 |
| `pegainfer-build/src/lib.rs` | 299 行 | 被消费的库本尊（u1-l3、u2 各讲已精读，本讲只引用关键行） |
| `pegainfer-kernels/csrc/glm52/glm52_fp8_dsl_gemm.c` | — | cutedsl shim 源文件，用来验证兜底风险（只看第 17 行的 `#include`） |

两个消费方在各自 `Cargo.toml` 的 `[build-dependencies]` 里以 workspace 依赖声明引用本库：[pegainfer-cupti/Cargo.toml:L7-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml#L7-L9)（`cc` + `pegainfer-build`）与 [pegainfer-kernels/Cargo.toml:L27-L29](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/Cargo.toml#L27-L29)（同样两条）。

> **行号校准记录（update）**：上一版讲义以 223206b7 为基准。当前 HEAD 8886aa47 期间，73d998ea（Qwen4 分组 RMSNorm）与 8b5b84a9（DFlash2）触碰了 kernels，但 `build.rs` 只有 4 行改动：往 CCCL `-I` 门控清单加了两个 stem（[L2500](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2500) 的 `dflash2_selector`、[L2504](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2504) 的 `qwen4_grouped_rmsnorm`）并改写了一句 Qwen4 警告文案（[L2739](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2739)）。这使 L2500 以下行号整体 +2：尾部工具包出口从旧版的 2884-2887 漂移到 2886-2889；而 L929、L2209-2213 等锚点不变。`pegainfer-build/src/lib.rs` 与 `pegainfer-cupti/build.rs` 两个文件无任何改动。

## 4. 核心概念与源码讲解

### 4.1 kernels 构建链路中的工具包消费点

#### 4.1.1 概念说明

`pegainfer-kernels` 是整个工作区的构建总管：`csrc/*.cu` 的 nvcc 编译、qwen35 的 Triton AOT、glm52 的 TileLang 与 cutedsl 代码生成、静态库打包、kernel-lab 共享库链接，全部从这里发起。它对 `CudaToolkit` 的需求最全面：

- 要一个**可执行的 nvcc 命令**（几十个并行编译任务都要用它）；
- 要一个**真正含 `cuda.h` 的头目录**，同时喂给 nvcc（`-isystem`）和宿主 cc（`-I`）；
- 要**库搜索路径**，在构建末尾变成 `cargo:rustc-link-search`。

值得注意的是：kernels **从不直接遍历 `toolkit.include_dirs`**——它只消费 `header_dir("cuda.h")` 的单点结果。原因在 u2-l4 讲过：`include_dirs` 里可能有「存在却缺 CUDA 头」的目录（conda 前缀的 `include/`），只有按具体头文件探测才可靠。

#### 4.1.2 核心流程

kernels 的 `main()` 在开头一次性完成探测，之后所有消费点都用这三样派生物：

```text
toolkit = CudaToolkit::discover()                    # L2209，全文件唯一探测点
nvcc         = toolkit.nvcc（转成字符串）             # L2210 → nvcc_command 包装 → 并行 worker 克隆
cuda_include = header_dir("cuda.h")                  # L2211-2212
               ?? toolkit.root.join("include")       # L2213，兜底（见 4.3.4）

cuda_include 的四条下游：
  ├─ nvcc 编译 .cu        ：-isystem {cuda_include}          # L2353-2354
  ├─ glm52 cutedsl shim   ：宿主 cc -I                      # L2229 → L129
  ├─ qwen35 Triton AOT    ：断言 + 宿主 cc -I                # L2875 → L1323-1327 / L1506
  └─ TileLang 生成体      ：nvcc -I                          # L2748/2757/2771 → L2178-2179

toolkit.root 的两条下游：
  ├─ kernel-lab .so 链接  ：-L {root}/lib64（仅 PEGAINFER_KERNEL_LAB）# L2871 → L944
  └─ Windows 库目录       ：cargo:rustc-link-search={root}/lib/x64    # L2883-2887

lib_dirs 的唯一下游：
  └─ toolkit.link_search()（非 Windows）                    # L2889
       → cargo:rustc-link-search=native=... 配合 L2891-2899 的 link-lib 清单
```

关键认知：`nvcc` 和 `cuda_include` 变成**子进程 argv**，`link_search` 变成 **cargo: 指令**——`CudaToolkit` 的出口一半喂编译器、一半喂 Cargo，两类输出不要混淆。

#### 4.1.3 源码精读

**① 唯一探测点。** [pegainfer-kernels/build.rs:L2209-L2213](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2209-L2213)：`main()` 开头三行完成全部探测——`discover()`、把 `toolkit.nvcc` 转成所有权字符串、用 `header_dir("cuda.h")` 定头目录并在 `None` 时回落 `root.join("include")`。注意这里探测只发生一次；整个近三千行的构建脚本没有任何第二次 `discover()`。

**② nvcc 命令的包装与分发。** nvcc 字符串不直接 `Command::new`，而是经 [pegainfer-kernels/build.rs:L354-L364](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L354-L364) 的 `nvcc_command` 包装：设置了 `PEGAINFER_NVCC_LAUNCHER` 时用启动器前缀，否则直接 `Command::new(nvcc)`——裸 `"nvcc"` 的 `$PATH` 回落（u2-l3 讲过）正是在这里被 `Command` 解析。并行编译时每个 worker 线程克隆一份字符串：[pegainfer-kernels/build.rs:L2799-L2814](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2799-L2814)，线程从共享任务队列弹出 `NvccTask`、用 `nvcc_command(&nvcc)` 起进程。

**③ nvcc 的 `-isystem`。** 每个 `.cu` 编译任务的公共前缀在 [pegainfer-kernels/build.rs:L2347-L2357](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2347-L2357)：`-isystem {cuda_include}` 加 CTK 头，`-I {csrc_dir}` 加项目源码头。为什么 CTK 用 `-isystem` 而不是 `-I`？[pegainfer-kernels/build.rs:L35-L39](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L35-L39) 的注释给了答案：自带的 vendored CCCL 必须以 `-I` 传入，才能压过 `-isystem` 里工具包自带的旧版 CCCL——FlashInfer v0.6+ 需要旧 CTK 缺失的 API。哪些源文件需要 CCCL 由 stem 门控清单决定：[pegainfer-kernels/build.rs:L2497-L2507](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2497-L2507)（本次 HEAD 新增的 `dflash2_selector`、`qwen4_grouped_rmsnorm` 就加在这里）。

**④ cuda_include 的宿主 cc 消费点。** 三处宿主编译都需要它：

- glm52 cutedsl：feature 开启时 [pegainfer-kernels/build.rs:L2227-L2230](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2227-L2230) 调 `build_glm52_cutedsl_fp8_dsl(&crate_root(), &out_dir, &cuda_include)`；函数内 [pegainfer-kernels/build.rs:L128-L132](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L128-L132) 用 `shim.include(cuda_include)` 编译 shim C 文件（其第 17 行 `#include <cuda_runtime.h>` 就是这个 `-I` 的服务对象）。
- qwen35 Triton AOT：[pegainfer-kernels/build.rs:L1321-L1327](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L1321-L1327) 先断言 `cuda_include/cuda.h` 存在（panic 文案带 `set CUDA_HOME` 提示），再由 [pegainfer-kernels/build.rs:L1503-L1508](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L1503-L1508) 的 `cc::Build` 编译生成的 C 包装。注释点明动机：宿主 cc 不像 nvcc 那样自己找得到 cuda.h。
- TileLang 生成体：三个调用点把 `&cuda_include` 传进 `tilelang_nvcc_tasks`，在 [pegainfer-kernels/build.rs:L2172-L2180](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2172-L2180) 变成 nvcc 的 `-I`。

**⑤ root 与 kernel-lab。** 设置了 `PEGAINFER_KERNEL_LAB` 时，[pegainfer-kernels/build.rs:L2870-L2872](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2870-L2872) 把整个 `toolkit` 传给 `link_kernel_lab_shared`，后者在 [pegainfer-kernels/build.rs:L941-L951](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L941-L951) 用 `toolkit.root.join("lib64")` 拼出 nvcc 链接器的 `-L` 参数并链接 `-lcudart -lcublas ...`。注意这是给**链接器进程**的参数，不是 cargo: 指令——同一个「库目录」概念在两种输出里各出现一次。

**⑥ 尾部收口。** [pegainfer-kernels/build.rs:L2882-L2899](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2882-L2899)：先为 `OUT_DIR`（静态库所在）发射 link-search；Windows 下绕过 `link_search()` 直接发射 `{root}/lib/x64`（Windows 工具包的库目录习惯与 Linux 不同），非 Windows 才调用 `toolkit.link_search()`；随后是一串 `cargo:rustc-link-lib`（`kernels_cuda` 静态库、`cudart`、`cublas`、`cublasLt`、`cuda`、`nvrtc`）。库名由消费方自己发射——`link_search` 只管目录（u2-l4 的契约）。L2895-2896 的注释还提醒：`libcuda.so` 在系统库目录而非工具包目录，靠链接器的默认搜索解析。

#### 4.1.4 代码实践

**实践目标**：不借助本讲义，独立复现「全部工具包使用点」清单，验证与 4.1.2 的数据流一致。

**操作步骤**：

1. 在仓库根目录执行 `grep -n "toolkit\|header_dir\|link_search" pegainfer-kernels/build.rs`；
2. 对每条命中分类：是真代码、注释还是 panic 文案里的英文单词（例如 L1325 的命中只是报错信息里的 "toolkit" 一词）；
3. 再执行 `grep -n "cuda_include" pegainfer-kernels/build.rs`，把 `cuda_include` 的每处出现挂到 4.1.2 数据流的对应分支上；
4. 用 `git log --oneline -3 -- pegainfer-kernels/build.rs` 确认本讲校准记录里提到的最近提交。

**需要观察的现象**：`toolkit` 的真实代码命中应恰好是 L929（函数参数）、L944（root/lib64）、L2209-2213（探测三连）、L2871（传参）、L2886（Windows 分支）、L2889（link_search）；`cuda_include` 的出现全部能追溯到 L2211 这一个定义点。

**预期结果**：你手绘的数据流图与 4.1.2 一致；多余的命中都被你正确识别为注释或文案。

**待本地验证**：本实践是纯源码阅读，无需 GPU、无需 CUDA，任何机器可做；grep 输出请在本地实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：kernels 为什么从不直接遍历 `toolkit.include_dirs` 给编译器加 `-I`？

<details><summary>参考答案</summary>

因为 `include_dirs` 只保证「目录存在」，不保证「含所需的头」——conda 前缀的 `include/` 存在却缺 CUDA 头（[pegainfer-build/src/lib.rs:L123-L124](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L123-L124) 的文档注释明说了这一点）。kernels 只需要「真正含 cuda.h 的那一个」目录，`header_dir` 正是为此设计的单点查询。
</details>

**练习 2**：`toolkit` 传进 `link_kernel_lab_shared` 后（L929），函数体里用了哪个出口？为什么不用 `link_search()`？

<details><summary>参考答案</summary>

只用了 `root`：在 L944 拼 `-L {root}/lib64` 传给 nvcc 链接器。不用 `link_search()` 是因为后者发射的是 `cargo:rustc-link-search` 指令（影响 rustc 最终链接 Rust crate），而 kernel-lab 是构建期由 nvcc 直接链接的独立 `.so`，需要的是链接器命令行参数——两者输出通道完全不同。
</details>

**练习 3**：`nvcc` 出口在 kernels 里经过了几层间接才真正变成进程？

<details><summary>参考答案</summary>

三层：L2210 把 `PathBuf` 转成字符串 → 各编译任务把它克隆进 worker 线程（L2803）→ `nvcc_command(&nvcc)` 包装（可选的 `PEGAINFER_NVCC_LAUNCHER` 前缀）后 `Command` 起进程。裸 `"nvcc"` 回落时，`$PATH` 解析发生在 `Command` 执行那一刻。
</details>

### 4.2 cupti 的 extras/CUPTI 目录特例

#### 4.2.1 概念说明

CUPTI 的头文件与库在经典安装里位于 `extras/CUPTI/include` 与 `extras/CUPTI/lib64`，**不在**主 `include/`、`lib64/` 里。回看 u2-l3：`from_root` 的候选目录只覆盖 `include`、`lib64/lib`、`targets/<arch>` 和 HPC SDK 的 `math_libs` 兄弟树（[pegainfer-build/src/lib.rs:L91-L121](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L91-L121)），完全不认识 `extras` 子树。

这是有意的 API 边界：`pegainfer-build` 保持「通用 CUDA 工具包探测」，具体组件的安装特例交给消费方用 `root` 自己拼。`pegainfer-cupti/build.rs` 就是这种分工的教科书示例——34 行走完 discover → include → compile → link 全流程，外加一个组件级特例。

#### 4.2.2 核心流程

```text
toolkit = CudaToolkit::discover()                        # L5
cupti_include = root/extras/CUPTI/include                # L7
cupti_lib64   = root/extras/CUPTI/lib64                  # L8

cc::Build（宿主编译 range_profiler.cpp）：
  .includes(&toolkit.include_dirs)                       # L16，主工具包头（直接用字段！）
  if cupti_include.is_dir() { .include(cupti_include) }  # L17-19，特例头，软门控
  .compile("pegainfer_cupti_range_profiler")             # L20

toolkit.link_search()                                    # L22，主库目录
if cupti_lib64.is_dir() { cargo:rustc-link-search=... }  # L23-25，特例库，软门控
cargo:rustc-link-lib = cuda / cupti / stdc++(非Windows)  # L27-31
cargo:rerun-if-changed=csrc/range_profiler.cpp           # L33
```

「软门控」是本模块的关键词：两个 `is_dir()` 判断让没有 `extras/CUPTI` 的安装（比如 conda 布局）不至于在构建期立刻炸掉，而是优雅降级——目录不存在就跳过这条额外路径。

#### 4.2.3 源码精读

**① discover 与特例路径的拼装。** [pegainfer-cupti/build.rs:L4-L8](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L4-L8)：一行 `discover()` 之后，立刻用**公开字段 `root`** 拼出 `extras/CUPTI/include` 与 `extras/CUPTI/lib64` 两个路径。注释直言：「CUPTI 在经典安装里位于主工具包目录之外」。

**② 编译期：include_dirs 的直接消费。** [pegainfer-cupti/build.rs:L10-L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L10-L20)：与 kernels 不同，cupti **直接遍历** `toolkit.include_dirs` 整个字段（`.includes()` 复数形式），因为它编译的 C++ 文件要同时用主工具包头和 CUPTI 头，多给几个候选目录无害；`is_dir()` 命中时再补 CUPTI 特例头目录，最后 `compile()`。

**③ 链接期：link_search 与特例库。** [pegainfer-cupti/build.rs:L22-L31](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L22-L31)：先 `toolkit.link_search()` 输出主库目录，再为 `extras/CUPTI/lib64` 补一条手写的 `cargo:rustc-link-search=native=...`（注意格式要自己写全，因为这条不经库的 `link_search`）；随后三条 `cargo:rustc-link-lib`——`cuda`、`cupti`，以及仅在非 Windows 上的 `stdc++`（CUPTI 头引用的 C++ 运行时符号在 Linux 上由 libstdc++ 提供，MSVC 工具链自带）。

**④ 缓存契约。** [pegainfer-cupti/build.rs:L33](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L33)：`rerun-if-changed` 盯住唯一源文件。注意 `CUDA_HOME`/`CUDA_PATH` 的 rerun-if 声明藏在库的 `discover()` 里（u2-l1、u3-l3 展开），消费方不必重复。

#### 4.2.4 代码实践

**实践目标**：验证「软门控」语义——把两个 `is_dir()` 分支的行为推演清楚，并体会它与 kernels 的 `assert!` 硬失败（L1323）在容错风格上的对比。

**操作步骤**：

1. 通读 `pegainfer-cupti/build.rs` 全文 34 行，为每条 `println!("cargo:...")` 与每个 `if ...is_dir()` 写一行中文注释（u1-l2 做过逐行注释，这次补上「为什么是软门控」）；
2. 对照 `pegainfer-build` 的布局测试想象三种根目录：经典布局（有 `extras/CUPTI`）、conda 布局（大概率没有）、空目录（u2 的 `unknown_layout` 场景）；
3. 对每种根目录，在纸上写出：`include_dirs` 是什么、两条软门控走不走、最终发射哪几条 cargo: 指令。

**需要观察的现象**：空目录场景下 `link_search()` 会先打一条 `no CUDA library dir found under ...` 告警（库内行为，[pegainfer-build/src/lib.rs:L132-L138](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L132-L138)），cupti 的两条软门控都跳过，随后 cc 编译 `range_profiler.cpp` 时因找不到 CUPTI 头而失败——软门控只是「不提前失败」，缺依赖终究会在真正用到的地方失败。

**预期结果**：三种场景的指令清单推演结果一致；能说清「软门控的失败推迟」与「Triton AOT 的 assert! + hint 提前失败」各自适合什么场合（前者依赖可能装在别处，后者依赖绝无替代）。

**待本地验证**：步骤 3 是纸面推演；若本地有 CUDA 环境，可用 `CUDA_HOME=/tmp/empty cargo build -p pegainfer-cupti` 实际观察告警与编译错误（无 GPU/CUDA 的机器上推演即可，结论不受影响）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `extras/CUPTI` 的探测放在消费方而不是加进 `from_root` 的候选目录？

<details><summary>参考答案</summary>

`from_root` 的候选是「CUDA 工具包本体」的通用布局（classic/conda/HPC SDK），对所有消费方成立；`extras/CUPTI` 是**特定组件**的安装位置，只有 CUPTI 消费方关心。放进库里会让每个消费方的 `include_dirs`/`lib_dirs` 都混入无关候选；放在消费方用 `root` 拼，则库保持通用、特例局部化——这正是 `root` 字段公开的设计意图。
</details>

**练习 2**：cupti 消费了 `CudaToolkit` 的哪几个出口？哪一个没用？

<details><summary>参考答案</summary>

用了 `root`（L7-8 拼 extras 路径）、`include_dirs`（L16 直接遍历）、`lib_dirs`（L22 经 `link_search()` 间接输出）。没用 `nvcc`——它只编译 C++ 包装，不碰 `.cu`，宿主 cc 足矣。
</details>

**练习 3**：如果某台机器 CUPTI 装在 `targets/<arch>` 布局下而不是 `extras`，这份 build.rs 会怎样？

<details><summary>参考答案</summary>

两个 `is_dir()` 都为假，软门控跳过——但若 CUPTI 头恰好在 `include_dirs` 的某个候选里（`targets/<arch>/include` 是 `from_root` 的候选之一，见 [pegainfer-build/src/lib.rs:L101-L104](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L101-L104)），L16 的 `.includes(&toolkit.include_dirs)` 仍能让 cc 找到头；库目录同理可能被 `link_search()` 覆盖。也就是说：主工具包布局由库兜住，组件特例目录只是「锦上添花」的补充路径。
</details>

### 4.3 CudaToolkit 四个出口（root/nvcc/include_dirs/lib_dirs）的用途映射

#### 4.3.1 概念说明

u1-l3 介绍过结构体的可见性设计：`root`、`nvcc`、`include_dirs` 公开，`lib_dirs` 私有、唯一出口是 `link_search()`。本模块把两个真实消费方的用法叠上去，得到这张完整的「出口 → 用途」映射表——它同时是二次开发时的选型指南：**新写一个 build.rs 时，每个需求该走哪个出口？**

- 要起编译器/链接器进程 → `nvcc`；
- 要按具体头文件定位目录 → `header_dir("某头文件")`（`include_dirs` 的推荐消费方式）；
- 要给宿主 cc 一批宽松的候选目录 → 直接 `include_dirs`；
- 要组件特例路径 → `root` 自己拼；
- 要 rustc 链接搜索 → `link_search()`，别自己碰库目录。

#### 4.3.2 核心流程

| 出口 | 可见性 | kernels 的消费点（HEAD 8886aa47） | cupti 的消费点 | 最终形态 |
|---|---|---|---|---|
| `root` | pub | 兜底 `root/include`（L2213）；kernel-lab `-L root/lib64`（L944）；Windows `root/lib/x64`（L2885-2887） | `extras/CUPTI/{include,lib64}`（L7-8） | 编译器 argv 或 cargo: 指令 |
| `nvcc` | pub | L2210 → `nvcc_command`（L354-364）→ 并行 worker（L2803） | 不用 | 子进程 argv |
| `include_dirs` | pub | 不直接用；经 `header_dir("cuda.h")` 单点消费（L2211-2212） | 直接遍历 `.includes()`（L16） | `-I` / `-isystem` |
| `lib_dirs` | 私有 | 仅 `link_search()`（L2889，非 Windows） | 仅 `link_search()`（L22） | `cargo:rustc-link-search=native=...` |

两个消费方恰好示范了 `include_dirs` 的两种消费姿势：kernels 是「精确制导」（只信 `header_dir` 的单点结果），cupti 是「面多加水」（候选全给，编译器自己挑）。前者服务于「头文件版本敏感」的场景，后者适合「有就行」的包装层。

#### 4.3.3 源码精读

**① 出口的定义处。** [pegainfer-build/src/lib.rs:L67-L73](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L67-L73)：结构体四个字段，`nvcc` 的文档注释写明回落语义（`{root}/bin/nvcc` 存在则用之，否则裸 `nvcc` 走 `$PATH`）。

**② header_dir 与 link_search 的契约。** [pegainfer-build/src/lib.rs:L123-L130](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L123-L130) 是 `header_dir`：按头文件逐目录探测，返回 `Option`——**None 是合法返回**，怎么处理是消费方的责任（kernels 的兜底见 4.3.4）。[pegainfer-build/src/lib.rs:L132-L142](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L132-L142) 是 `link_search`：空表软告警，非空逐目录发射 `native=` 指令。

**③ None 场景的库内锚点。** [pegainfer-build/src/lib.rs:L260-L269](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L260-L269) 的 `unknown_layout_yields_nothing` 测试：探测一个只有 `weird/place` 的根目录，断言 `header_dir("cuda.h")` 返回 `None`、两个目录列表全空——这正是触发 kernels 兜底的场景在库侧的样子。

#### 4.3.4 代码实践

**实践目标**：回答本讲的核心思考题——若 `header_dir("cuda.h")` 返回 `None`，glm52 的 cutedsl 构建会用哪个目录？这个兜底在 conda 布局下是否正确？

**操作步骤**：

1. 读 [pegainfer-kernels/build.rs:L2211-L2213](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2211-L2213)，写下 `cuda_include` 的兜底表达式；
2. 追踪 glm52 分支 [pegainfer-kernels/build.rs:L2227-L2230](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2227-L2230) 与 [pegainfer-kernels/build.rs:L128-L132](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L128-L132)，确认 `cuda_include` 最终成为 shim 的 `-I`；
3. 打开 `pegainfer-kernels/csrc/glm52/glm52_fp8_dsl_gemm.c` 第 17 行，确认 shim 依赖 `<cuda_runtime.h>`；
4. 对照 `header_dir` 的文档注释（`pegainfer-build/src/lib.rs:L123-L124`）与 conda 布局测试（`pegainfer-build/src/lib.rs:L208-L224`），推演 conda 前缀下兜底目录的内容。

**需要观察的现象 / 推演**：兜底表达式是 `toolkit.root.join("include")`——即**经典布局的 include 目录**。在 conda 布局下这个兜底不可靠：

- conda 前缀的 `include/` 通常**存在**但**不含** CUDA 头（`header_dir` 的文档注释原话："on conda `include/` exists but lacks the CUDA headers"）；
- `header_dir` 已经返回 `None`，说明连 `targets/<arch>/include` 里也没有 `cuda.h`——这本来就不是一个健康的 CUDA 安装；此时兜底指向的是一个「存在但没有所需头」的目录；
- 后果分两级：轻则 shim 的 `#include <cuda_runtime.h>` 在宿主 cc 深处报「找不到头文件」，错误信息毫无 `set CUDA_HOME` 之类的可操作提示（对比 Triton AOT 路径 [L1323-L1327](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L1321-L1327) 的 assert + hint 风格）；重则若编译器恰好在其他默认搜索路径找到**另一个版本**的 CUDA 头，会静默用错头，埋下 ABI 风险。

**预期结果**：能写出结论——兜底是对「经典布局但探测失灵」的乐观猜测；探测已经失败时再猜经典布局，在 conda 上大概率指向错误目录。改进方向可参考 Triton 路径：对必需头文件先 `assert!`（fail fast + 提示）再使用，而不是静默替换成一个未经验证的路径。

**待本地验证**：以上为源码推演结论；若本地有 glm52 feature 的构建环境，可构造 `CUDA_HOME=<空conda前缀>` 并开 `--features glm52` 观察 shim 编译失败的具体报错（无环境时推演即可，所有引用行号均已核对）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lib_dirs` 设计成私有字段，而 `include_dirs` 公开？

<details><summary>参考答案</summary>

库目录的输出有固定格式（`cargo:rustc-link-search=native=...`）和空表告警语义，封装在 `link_search()` 里可以保证任何消费方都不会绕过告警、不会拼错格式；而头目录的消费方式多样（`-I`、`-isystem`、cc 的 `.includes()`、按头文件单点查询），公开 `include_dirs` 给消费方留足自由度。风险不对称：链接目录错了往往只是链接失败，头目录用法则本就需要消费方按场景选择。
</details>

**练习 2**：新消费方需要 `cuda_version.h` 里的 `CUDA_VERSION` 宏，该用哪个出口？

<details><summary>参考答案</summary>

`header_dir("cuda_version.h")`——按具体头文件定位（`include_dirs` 的推荐姿势），拿到的一定是含该头的目录；直接遍历 `include_dirs` 加一堆 `-I` 也能工作，但会把可能缺头的目录也塞给编译器。
</details>

**练习 3**：观察 L2883-2890：为什么 Windows 分支不用 `link_search()`？

<details><summary>参考答案</summary>

Windows 版 CUDA 工具包的库目录习惯是 `lib/x64` 而非 `lib64`/`lib`，`from_root` 的候选目录（按 Linux 布局拼）在 Windows 上大概率全不存在、`link_search()` 只会打出「找不到库目录」的告警。所以 kernels 在 Windows 下绕过它，直接用 `root` 拼 `lib/x64` 发射一条手写指令——这也算一次「消费方用 root 处理布局特例」的现身说法（与 cupti 的 extras 特例同构）。
</details>

## 5. 综合实践

**任务**：绘制 kernels 的「discover 输出 → 各消费点用途 → 最终输出」数据流图，并完成 4.3.4 的兜底分析。这是贯穿本讲三个模块的收口练习。

**操作步骤**：

1. 执行 `grep -n "toolkit\|header_dir\|link_search" pegainfer-kernels/build.rs`，标出全部真实代码命中（剔除注释与文案）；
2. 对每个命中点，沿调用链追到「最终形态」：是变成子进程 argv（nvcc/cc 的参数），还是 cargo: 指令（`rustc-link-search` / `rustc-link-lib`）；
3. 画一张图（或表格），左侧是 `discover()` 返回的四个出口，右侧是最终输出，中间标注消费点行号与用途；
4. 在图上单独标红 L2213 的兜底边（`header_dir None → root/include`），写下 4.3.4 的结论；
5. 对照下面的参考答案自查。

**参考数据流图**（HEAD 8886aa47 行号）：

```text
CudaToolkit::discover() (L2209)
│
├─ nvcc ──────────► L2210 字符串化 ─► nvcc_command 包装 (L354-364)
│                    └─► 并行 worker 克隆 (L2803) ─► nvcc 子进程 argv
│                        ├─ .cu 编译（含 -isystem cuda_include, L2353-2354）
│                        ├─ TileLang 生成体编译（-I cuda_include, L2178-2179）
│                        └─ kernel-lab .so 链接（-L root/lib64, L944；门控 L2870-2872）
│
├─ include_dirs ──► header_dir("cuda.h") (L2211-2212) ─► cuda_include
│                    │  （None 时兜底 root/include, L2213 ← 标红：conda 下不可靠）
│                    ├─► nvcc -isystem (L2353-2354)          → nvcc argv
│                    ├─► glm52 cutedsl shim -I (L2229→L129)  → cc argv
│                    ├─► qwen35 Triton AOT：assert (L1323-1327) + cc -I (L1506)
│                    └─► TileLang nvcc -I (L2748/2757/2771)
│
├─ root ──────────► kernel-lab -L root/lib64 (L944)          → nvcc argv
│                   Windows root/lib/x64 (L2885-2887)        → cargo:rustc-link-search
│                   header_dir 兜底 (L2213)                  → 同 cuda_include
│
└─ lib_dirs（私有）► link_search() (L2889, 非Windows)         → cargo:rustc-link-search=native=...
                     配套库名由 kernels 自己发射 (L2891-2899) → cargo:rustc-link-lib=...
```

**兜底分析结论**（4.3.4 的答案摘要）：`header_dir("cuda.h")` 为 `None` 时，glm52 cutedsl 用 `toolkit.root.join("include")`；在 conda 布局下该目录存在但不含 CUDA 头，兜底大概率指向错误目录，把「探测失败」变成「cc 深处的含糊编译错误」（或更糟：静默用错别的版本的 cuda 头）。对比 Triton AOT 路径的 `assert!` + `set CUDA_HOME` 提示，这是可以改进的容错风格。

**待本地验证**：数据流图的行号来自本讲已核对的 grep 与阅读；请在本地重跑步骤 1 的 grep 复核。若本地有 GPU + CUDA 环境，可再跑一次 `cargo build --release -p pegainfer-kernels` 观察构建日志里的 `cargo:warning=Compiling CUDA kernels for nvcc targets: ...`（无环境时跳过，不影响结论）。

## 6. 本讲小结

- kernels 全文件只有**一个**探测点（L2209-2213）：`discover()` 之后立即派生出 `nvcc` 字符串与 `cuda_include`（`header_dir("cuda.h")` + `root/include` 兜底），其余近三千行都在消费这两个派生物和 `root`。
- `CudaToolkit` 的出口一半喂**编译器 argv**（nvcc、cuda_include、kernel-lab 的 `-L`），一半喂 **cargo: 指令**（`link_search` 的 `rustc-link-search`）；同一个「库目录」概念在两种通道里各出现一次，别混淆。
- kernels 不直接遍历 `include_dirs`，只信 `header_dir` 的单点结果（conda 的 `include/` 存在却缺头）；cupti 则直接 `.includes(&toolkit.include_dirs)` 给宿主 cc——两种姿势对应「版本敏感」与「有就行」两类场景。
- cupti 用公开的 `root` 拼 `extras/CUPTI/{include,lib64}` 组件特例目录，并用两个 `is_dir()` 软门控优雅降级；组件级布局特例放消费方、通用工具包布局放库，是这条 API 边界的分工原则。
- `header_dir` 返回 `None` 时的 `root.join("include")` 兜底在 conda 布局下不可靠：指向一个「存在但没有 CUDA 头」的目录，把清晰的探测失败变成含糊的编译错误；Triton AOT 路径的 `assert!` + hint 是更稳的范式。
- 本次 HEAD 更新（223206b7→8886aa47）只给 kernels 的 CCCL `-I` 门控清单加了两个 stem 并改写一句警告，L2500 以下行号整体 +2；工具包消费点中仅尾部 link_search 从 2884-2887 漂移到 2886-2889。

## 7. 下一步学习建议

- **下一讲 u3-l3（rerun-if 语义与构建缓存正确性）**：本讲反复出现 `rerun-if-env-changed`（藏在 `discover()` 里）与 `rerun-if-changed`（cupti L33、cutedsl L125-127），下一讲系统拆解 Cargo 指纹机制——「换了 CUDA 安装但环境变量没变」时构建缓存为什么可能过期、怎么规避。
- 继续阅读 `pegainfer-kernels/build.rs` 的 `nvcc_job_count`（L293 起）与 `nvcc_task_priority`（L304 起），看构建总管如何并行调度几十个 nvcc 任务——这是 `nvcc` 出口消费链的延伸。
- 对照阅读 `pegainfer-cupti/csrc/range_profiler.cpp`，看 34 行 build.rs 编译出的到底是什么，加深「build.rs 只是入口、产物在 csrc」的理解。
- 若你想动手：仿照 cupti 的软门控模式，为一个你熟悉的 CUDA 组件（如 NVTX，位于 `extras/NVTX` 或 `include`，随版本而异）写一个 10 行的探测片段，体会「库管通用布局、消费方管组件特例」的分工手感（待本地验证目录布局）。
