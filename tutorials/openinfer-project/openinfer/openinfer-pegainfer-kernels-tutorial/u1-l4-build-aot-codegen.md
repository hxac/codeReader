# 构建管线 II：feature 门控的 AOT 生成与 cubin 嵌入

## 1. 本讲目标

上一讲（u1-l3）我们走完了 build.rs 的 nvcc 主干：SM 探测、`NvccTask` 并行编译、FlashInfer 头定位。但 `csrc/*.cu` 只是这个 crate 构建产物的一部分——还有相当一批 GPU 代码**不在仓库的 `.cu` 文件里**，而是在构建期由外部工具「生成」出来的。

学完本讲，你应该能够：

1. 说出本 crate 存在的**四条构建期代码生成通道**：Triton AOT（qwen35）、TileLang AOT（gemma4 / k3 的 family 表）、checked-in cubin 嵌入（glm52）、vendored CUDA 头的 AOT 实例化（DeepGEMM / FlashMLA）。
2. 逐字段解释 `TritonKernelSpec`，并追踪一个 spec 如何变成 `OUT_DIR` 下的 cubin + C 包装 + 可链接符号。
3. 复述 TileLang 的**三层梯队**（generate / pre-generated / stub）以及它为什么能让「没有 Python 的构建主机」依然编译通过。
4. 解释 `GLM52_TRTLLM_FMHA_CUBINS` 表如何把 7 个 checked-in 的 cubin 文件变成 C 数组头文件，再被 nvcc 编进静态库。
5. 建立一张 AOT 相关环境变量的速查矩阵。

## 2. 前置知识

- **AOT vs JIT**：AOT（Ahead-Of-Time，提前编译）指在构建期就把 kernel 编成 GPU 机器码（cubin）；JIT（Just-In-Time，运行时编译）指进程跑起来后才编译。PegaInfer 的哲学是**默认零 Python、零运行时 JIT**——所有 Python 工具链（Triton / TileLang）只允许出现在构建期，而且被 feature 门控，默认构建完全不碰。
- **cubin**：CUDA 的 GPU 机器码二进制格式（`cuModuleLoadData` 可以直接加载内存中的 cubin 字节）。一个 cubin 只服务于特定 SM 架构（如 SM100），换 GPU 通常要重编。
- **`OUT_DIR`**：Cargo 提供给 build script 的输出目录（如 `target/release/build/pegainfer-kernels-*/out/`）。所有生成物都写到这里，**不进仓库**（TileLang README 明确说 "Generated CUDA is a build artifact under Cargo `OUT_DIR`; it should not be checked into the repository"）。
- **`cargo:rerun-if-changed` / `rerun-if-env-changed`**：build.rs 的增量触发声明。上一讲讲过通用规则，本讲关注 AOT 各通道各自声明了哪些触发条件。
- **feature 门控**：build.rs 里用 `cfg!(feature = "...")` 读取 cargo feature（如 [build.rs:2874-L2880](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2874-L2880) 判断 `qwen35`），决定某条生成通道是否启动。feature 定义见 [Cargo.toml:L31-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L31-L50)——注意注释里写明：`qwen35` 是**唯一**需要构建期 Python + Triton 的 feature。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs) | 构建管线总控。本讲聚焦其中四段：`TritonKernelSpec` 与 Triton 生成（约 L12-L21、L1001-L1530）、TileLang family 区段（L1533-L2204）、glm52 cubin/CuTe DSL（L42-L197）、main 中的接线点（L2227-L2230、L2743-L2792、L2874-L2880） |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml) | feature 定义与 example 的 `required-features` 绑定 |
| [tools/triton/README.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/README.md) | Triton AOT 官方操作手册：venv 引导、环境变量、常见故障 |
| [tools/triton/gen_triton_aot.py](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/gen_triton_aot.py) | Triton AOT 生成器（build.rs 以子进程调用它） |
| [tools/tilelang/README.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/tilelang/README.md) | TileLang 生成器的目录约定说明 |
| [cubin/glm52/README.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/cubin/glm52/README.md) | 7 个 checked-in cubin 的来源、SHA 校验与维护规则 |
| [csrc/glm52/glm52_flashinfer_sparse.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_flashinfer_sparse.cu) | 嵌入 cubin 的消费方：`#include` 生成的 `.inc` 并实现 cubin 加载器 |

一句话总览四条通道的差异：

| 通道 | feature | 构建期需要 Python？ | 产物形态 | 产物去向 |
| --- | --- | --- | --- | --- |
| Triton AOT | `qwen35` | **是**（Triton） | cubin 字节内嵌的 C 源 + 手写 wrapper | host cc 编成 `triton_kernels_aot` 静态库 |
| TileLang AOT | `gemma4` / `k3` | 可选（三层梯队降级） | CUDA 源 + manifest | nvcc 编进 `libkernels_cuda.a` |
| checked-in cubin | `glm52` | 否 | 十六进制 C 数组头 `.inc` | nvcc 编进 `libkernels_cuda.a` |
| vendored CUDA 头 AOT 实例化 | `glm52` / `k3` / `moe` | 否 | 普通 `.cu` TU（include 第三方设备头） | nvcc 编进 `libkernels_cuda.a` |

## 4. 核心概念与源码讲解

### 4.1 TritonKernelSpec 与 AOT 产物：qwen35 的构建期 Triton 编译

#### 4.1.1 概念说明

Qwen3.5 是混合架构（Gated DeltaNet 线性注意力 + 全注意力），它的 **GDR chunkwise prefill**（分块预填充）kernel 用 Triton 写在 [tools/triton/gated_delta_rule_chunkwise_kernels.py](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py) 里。Rust 侧不能直接调 Python，所以构建期要把每个 Triton kernel 提前编译成 cubin，并生成一个 C 语言的启动器（launcher），最后像普通 C 代码一样链进 Rust 二进制。

「哪个 kernel、什么签名、什么 launch 参数」由一个纯数据的结构体描述——`TritonKernelSpec`。build.rs 为 GDR 的 7 个分块步骤各写了一个 spec。

#### 4.1.2 核心流程

```text
qwen35 feature 开启（build.rs L2874）
  └─ compile_triton_aot_kernels
       ├─ find_triton_python()           # 解释器探测链（fail-fast）
       ├─ triton_target(sm_targets)      # 取最大 SM → "cuda:<sm>:32"
       ├─ 对 7 个 TritonKernelSpec 逐个：
       │    ├─ generate_triton_artifacts  # 调 tools/triton/gen_triton_aot.py
       │    │     └─ 产物：OUT_DIR/triton_aot/<artifact_dir>/ 下的 cubin + .c
       │    ├─ patch_triton_aot_per_device_handles  # 把单例 CUmodule 改成按设备表
       │    └─ write_wrapper             # 手写稳定的 extern "C" wrapper
       └─ cc::Build 把全部 C 源编成静态库 triton_kernels_aot
            └─ cargo:rustc-link-lib=cuda（cuLaunchKernel 需要 driver API）
```

关键点：**cubin 字节不是独立文件，而是嵌在生成的 C 源里**——运行时首次调用时 `cuModuleLoadData` 从内存加载。这与 glm52 的 cubin 嵌入（4.3）是同一个套路，只是一个由 Triton 生成、一个是 checked-in。

#### 4.1.3 源码精读

**spec 结构体**——7 个字段就是「一份 Triton kernel 的完整编译处方」：

[build.rs:L12-L21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L12-L21)：定义 `TritonKernelSpec`，字段依次为 `artifact_dir`（产物子目录名）、`kernel_path`（Python 源，相对 crate 根）、`kernel_name`（要编译的 `@triton.jit` 函数名）、`signature`（参数类型串）、`grid`（网格表达式）、`out_name`（产物前缀）、`num_warps` / `num_stages`（Triton 编译旋钮）。

以第一个 spec 为例（GDR 分块预填充的 prepare 步骤）：

[build.rs:L1340-L1349](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1340-L1349)：声明 `gdr_prepare_spec`。注意 `signature` 里 `"*bf16,*bf16,...,i32,i32,i32,i32,128,128"`——`*bf16` 是 bf16 指针、`i32` 是标量、末尾的 `128,128` 是**常量参数**（Triton 的 constexpr 特化，编译期直接烧进 kernel）；`grid: "seq_len,num_value_heads,1"` 是会在生成时求值的 Python 表达式。build.rs:1459-1460 还有一条有教学价值的注释：chunk_state spec 里第 4 个参数 `g` 故意**不加** `:16` 对齐标记，因为它不是连续块指针加载，「`:16` 会是不真实的声明」——签名串里的每个修饰符都必须与真实访存模式一致。

**解释器探测链**：

[build.rs:L1017-L1051](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1017-L1051)：`find_triton_python` 的顺序是——① 若设了 `PEGAINFER_TRITON_PYTHON` 则**只用它**（设了但 import 失败立即报错并指向 README，不兜底）；② 否则依次探测 workspace 根的 `.venv/bin/python` → `python3` → `python`，探测方式就是跑一遍 `python -c "import triton"`（[build.rs:L1001-L1015](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1001-L1015)），全部失败则 panic 并汇总每个候选的错误。这是典型的 fail-fast：宁可构建失败，也不静默选一个错的解释器。

**目标架构**：

[build.rs:L1149-L1163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1149-L1163)：`triton_target` 取所有 SM 目标里的最大值拼成 `"cuda:<sm>:32"`（warp size 32）；若检测到多个 SM 目标会打 warning 提醒「每个 spec 只产一个 cubin，请用 `PEGAINFER_CUDA_SM` 钉死单目标」。生成器侧配套了一个 `OfflineCudaDriver`（[tools/triton/gen_triton_aot.py:L18-L39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/gen_triton_aot.py#L18-L39)），把 Triton 的「当前设备」探测替换成离线 `GPUTarget`——**生成过程不需要构建主机真有一块 GPU**。

**生成与回读**：

[build.rs:L1177-L1240](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1177-L1240)：`generate_triton_artifacts` 以命令行参数把 spec 全量传给 `gen_triton_aot.py`（`--kernel-path/--kernel-name/--signature/--grid/--out-name/--out-dir/--target/--num-warps/--num-stages`），断言退出码成功，然后从 stdout 里解析两行机器可读输出：`FUNC_NAME=`（生成的 C 启动函数名）和 `C_PATH=`（C 源路径）。**build script 与生成器之间的契约是文本协议**，这与 TileLang 的 `KEY=VALUE` manifest（4.2）如出一辙。

**多设备补丁**：

[build.rs:L1242-L1310](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1242-L1310)：`patch_triton_aot_per_device_handles` 对生成的 C 源做文本替换——Triton 默认生成**单个全局** `CUmodule` / `CUfunction`，8 卡 EP 时所有设备会共用一个句柄从而互踩；补丁把它们改成 16 槽的数组 `module[dev]`，并在每次 load / launch 前用 `cuCtxGetDevice` 取当前设备号。这是一个很现实的教训：**上游生成器的默认假设（单设备）必须在使用方手里修正**。

**手写 wrapper 与链接**：

[build.rs:L1352-L1358](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1352-L1358)：`write_wrapper` 为每个 kernel 生成一个 `*_wrapper.c`，把「参数是 `CUdeviceptr` + stream 在首位」的 Triton 启动器包装成签名稳定的 `gated_delta_rule_prefill_chunk_prepare_cuda(...)`——Rust FFI 声明面对的是这个稳定名字，而不是每次生成可能变化的 `FUNC_NAME`。

[build.rs:L1503-L1516](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1503-L1516)：所有生成源交给 host 端 `cc::Build`（`cuda(false)`、`-std=c11`）编成静态库 `triton_kernels_aot`，并追加 `cargo:rustc-link-lib=cuda`——启动器用的是 driver API（`cuLaunchKernel`），要链 `libcuda`。

**增量触发**：

[build.rs:L1520-L1529](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1520-L1529)：声明对 kernel Python 源、生成器脚本、`PEGAINFER_TRITON_PYTHON` 环境变量的 rerun。改 `num_warps` 这类 spec 字段在 build.rs 里，天然被 `rerun-if-changed=build.rs`（L2908）覆盖。

#### 4.1.4 代码实践：整理 qwen35 构建前置步骤清单

1. **实践目标**：把 tools/triton/README.md 的散落指示整理成一份可执行的引导清单，并标注每一步对应 build.rs 里的哪段代码。
2. **操作步骤**：
   - 阅读 [tools/triton/README.md:L18-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/README.md#L18-L50)，写出清单，大致应为：
     ```bash
     # ① 基础环境
     export CUDA_HOME=/usr/local/cuda
     export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
     # ② 一次性引导 repo 本地 Triton venv（build.rs 会优先探测 .venv/bin/python）
     uv venv .venv
     uv pip install -p .venv/bin/python triton
     # ③ 显式指认（可选；不设则走探测链）
     export PEGAINFER_TRITON_PYTHON=$PWD/.venv/bin/python
     # ④ 无 nvidia-smi 的构建机上钉死目标
     export PEGAINFER_CUDA_SM=120
     # ⑤ 构建
     PEGAINFER_BUILD_TIMING=1 cargo build --release --features qwen35
     ```
   - 对照源码给每一步「点名」：② 对应 [build.rs:L1032-L1038](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1032-L1038) 的 `.venv → python3 → python` 探测顺序；③ 对应 [build.rs:L1017-L1030](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1017-L1030) 的「设了就只用它」；④ 对应 `triton_target`（[build.rs:L1149-L1163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1149-L1163)）。
3. **需要观察的现象**：带 `PEGAINFER_BUILD_TIMING=1` 构建时，日志出现 7 个 `triton-gen <kernel_name>` 计时相位，随后是 `cc triton_kernels_aot`；产物目录 `target/release/build/pegainfer-kernels-*/out/triton_aot/` 下有 7 个子目录（[tools/triton/README.md:L71-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/README.md#L71-L75)）。
4. **预期结果**：清单能逐条映射到源码；故意 `export PEGAINFER_TRITON_PYTHON=/usr/bin/false` 再构建，应看到带 README 指引的 panic 报错（对应 [tools/triton/README.md:L99-L101](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/README.md#L99-L101) 列出的首类故障）。
5. 本环境无 GPU 与 Triton venv，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`signature` 里末尾的裸数字（如 `128,128`）和 `*bf16:16` 中的 `:16` 分别是什么意思？
**答案**：裸数字是 Triton 的 constexpr 参数，值在编译期烧进 kernel（生成的是针对该常量的特化 cubin）；`:16` 是指针的 16 字节对齐修饰，允许生成器使用向量化/block-ptr 加载。build.rs:1459-1460 的注释说明：不能给不满足对齐事实的指针加 `:16`，否则是「不真实的声明」。

**练习 2**：为什么 `patch_triton_aot_per_device_handles` 必须存在？不做补丁会发生什么？
**答案**：Triton 生成的 C 启动器用一个全局单例 `CUmodule/CUfunction`；多卡 EP 场景下每个设备各有自己的 CUDA context，共用一个句柄会串设备。补丁改成 `[设备号]` 数组（上限 16）并在 load/launch 前用 `cuCtxGetDevice` 定位当前设备（[build.rs:L1242-L1310](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1242-L1310)）。

**练习 3**：Rust 侧为什么通过 `gated_delta_rule_prefill_chunk_prepare_cuda` 而不是 `FUNC_NAME` 调 kernel？
**答案**：`FUNC_NAME` 由 Triton 生成、可能随版本变化；`write_wrapper` 生成的 wrapper 提供了签名与名称都稳定的 `extern "C"` 边界（[build.rs:L1352-L1358](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1352-L1358)），把生成器的变化隔离在构建期。

### 4.2 TileLang 生成器目录约定：family 表与三层梯队

#### 4.2.1 概念说明

TileLang 是另一个 Python DSL（生成 CUDA 源而非 cubin）。build.rs 用一段「自包含区段」（`// tilelang: BEGIN` 到 `END`，[build.rs:L1533-L1551](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1533-L1551)）承载它，核心抽象是 **`TileLangFamily`（family，家族）**：一条模型线的全部 TileLang kernel 就是表里的一行，梯队选择、架构处理、nvcc 旗标全部共享。

与 Triton 通道最大的不同是**容错策略**。Triton 找不到 Python 直接 panic（qwen35 明确要求构建期 Python）；TileLang 则设计了**三层梯队，第一个可用的生效**：

1. **generate**：用装了（指定版本）TileLang 的构建期 Python 现场生成——开发机和 GPU CI 的常规路径；
2. **pre-generated**：`PEGAINFER_<label>_TILELANG_PREGEN=<dir>` 指向一个先前用 `generate.py --vendor-includes` 产出的目录（自带 `manifest.txt`），供装不了 TileLang 的主机使用；
3. **stub**：什么都没有时，生成一组**对任何形状都返回 `cudaErrorNotSupported` 的启动器**——feature 仍然能链接、CI 仍然绿，调用时响亮地失败而不是静默算出错误数字。

> 诚实提示：[tools/tilelang/README.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/tilelang/README.md) 的表格提到 `glm52/generate.py`（GLM5.2 sparse MLA，sm_90a），但**当前代码树里 `tools/tilelang/` 下只有 README**——build.rs 中的 family 实为 GEMMA4、GEMMA4_W4A16、K3 三个，各自的 `generate.py` 位于模型 crate（`pegainfer-gemma4/kernels/`、`pegainfer-k3/kernels/`）。README 与代码存在出入，以 build.rs 的 family 表为准。

#### 4.2.2 核心流程

```text
main() 中 feature 接线（L2744-L2792）
  gemma4 → tilelang_nvcc_tasks(GEMMA4_TILELANG) + (GEMMA4_W4A16_TILELANG)
  k3     → tilelang_nvcc_tasks(K3_TILELANG)

tilelang_nvcc_tasks(family)
  ├─ 声明 rerun 环境变量：PEGAINFER_<label>_TILELANG_{PYTHON,PREGEN,JOBS} 与通用 PYTHON
  ├─ cargo::rustc-check-cfg=cfg(<label小写>_tilelang)
  ├─ artifacts = pregen 目录(若设) .or_else( 现场生成 )
  │     现场生成：找 Python → 定 arch(sm_≥90 加 'a') → 跑 generate.py
  │              → stdout 按 KEY=VALUE 解析成 TileLangArtifacts
  │     任何一步不可用 → 返回 None（打印 cargo:warning）
  ├─ 有产物：checked_launchers / checked_manifest_fields 两道断言
  │          → rustc-cfg=<cfg> + rustc-env 导出 ARCH/GEOMETRY/SMEM
  │          → nvcc 任务带「生成器声明过的」NVCC_FLAG 与精确 -gencode
  └─ 无产物：write_tilelang_stub → 返回 cudaErrorNotSupported 的 stub TU
```

#### 4.2.3 源码精读

**family 行**——一行描述一条模型线：

[build.rs:L1575-L1600](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1575-L1600)：`TileLangFamily` 的字段：`label`（出现在 warning、`PEGAINFER_<label>_TILELANG_*` 变量名、小写后的产物目录与 stub 文件名里——注释强调「a family is this row and nothing else」）、`generator`（相对 workspace 根）、`sources`（供 rerun 追踪的额外输入）、`launchers`（stub 层必须精确复刻的 `extern "C"` 入口清单）、`min_sm`、`required_manifest`。

[build.rs:L1602-L1610](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1602-L1610)：`GEMMA4_TILELANG`——生成器在 `pegainfer-gemma4/kernels/generate.py`，`min_sm: 90`，注释给出理由：Q/K 的加载会 lower 成 TMA，而 TMA 是 Hopper 起才有。[build.rs:L1675-L1682](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1675-L1682) 的 `K3_TILELANG` 则 `min_sm: 0`、`required_manifest` 为空（最宽松的家族）。

**解释器探测链**（与 Triton 同构但多一层 label 特异性）：

[build.rs:L1749-L1789](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1749-L1789)：先试 `PEGAINFER_<LABEL>_TILELANG_PYTHON`（家族专属），再试通用的 `PEGAINFER_TILELANG_PYTHON`，然后探测两个 venv 路径（`../.venv` 与 `.venv`）和 `python3` / `python`。

**架构推导**：

[build.rs:L1791-L1824](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1791-L1824)：`tilelang_arch` 从（和 nvcc 相同的）SM 列表取最大值，≥90 时追加 `a` 后缀（如 `sm_103a`），因为「生成不得依赖构建主机看得见 GPU」。`tilelang_gencode` 则给这些生成体配**精确单架构**的 `-gencode`——注释解释：TileLang 会把 TMA copy lower 成发射 `setmaxnreg` 的 warp-specialized kernel，ptxas 只为加速版（`a`）目标汇编它；nvcc 拼不动该架构时返回 `None`，调用方降级到 stub 层。

**manifest 契约**：

[build.rs:L1826-L1890](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1826-L1890)：生成器在 stdout 打印、并镜像写入 `manifest.txt` 的 `KEY=VALUE` 协议：`CU_PATH`（每个编译单元一行）、`TILELANG_TEMPLATE_PATH`、`CUTLASS_INCLUDE_DIR`、`ARCH`、`GEOMETRY`、`SMEM`、`NVCC_FLAG`（逐条）、`LAUNCHER=name|参数串`；无法 lower 时只打一行 `UNAVAILABLE=<原因>`。路径都相对 manifest 所在目录解析，所以一个 vendored 目录拷到哪里都能自圆其说。

**两道防错断言**：

[build.rs:L1904-L1927](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1904-L1927)：`checked_launchers` 要求生成器发射的入口点与 stub 层声明的**名称和参数完全一致**——注释点破动机：C 没有名字修饰，「参数漂移的 stub 照样能链接、然后被错误地调用」。[build.rs:L1932-L1958](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1932-L1958)：`checked_manifest_fields` 要求 `required_manifest` 列出的键必须存在且可解析（SMEM 是整数、GEOMETRY 是 4 个逗号分隔整数），否则构建失败——否则旧目录会「报告自己是真 kernel 却什么都不拒绝」（fail-open）。

**stub 层**：

[build.rs:L2063-L2084](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2063-L2084)：`write_tilelang_stub` 按家族的 launchers 清单逐个生成「返回 `cudaErrorNotSupported`」的函数体，写到 `OUT_DIR/<label小写>_tilelang_stub.cu`。

**选择与导出**：

[build.rs:L2086-L2112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2086-L2112)：`tilelang_nvcc_tasks` 先声明 rerun 环境变量与 `cargo::rustc-check-cfg`，然后 `pregen.or_else(generate)` 选梯队；注释解释为什么要导出 cfg：「哪个梯队跑了是构建期事实，stub 与真身同名链接，运行时想知道只能加载完权重再调 kernel 读错误码——代价太高」。[build.rs:L2131-L2165](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2131-L2165)：真身路径上 `cargo::rustc-cfg=<cfg>` 并把 `ARCH/GEOMETRY/SMEM` 经 `cargo::rustc-env` 交给 Rust 侧做运行时拒绝（设备不匹配 / checkpoint 几何不匹配时拒载）；nvcc 参数里逐条附加生成器声明过的 `NVCC_FLAG`——注释警句：「到达 nvcc 命令行的一个 pass 配置改变的是数字，不只是代码」。

**main 接线**：

[build.rs:L2743-L2792](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2743-L2792)：`gemma4` feature 拉起两个 family，`k3` feature 拉起 K3 family；feature 关闭时打 warning 提示。这些 TileLang 任务汇入上一讲讲过的统一 `nvcc_tasks` 队列与并行工作池。

#### 4.2.4 代码实践：给 K3 family 做一张「家族身份卡」

1. **实践目标**：不运行任何命令，仅凭源码推导 K3 family 的全部「身份要素」，验证「一行即一家族」的设计。
2. **操作步骤**：
   - 从 [build.rs:L1675-L1682](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1675-L1682) 的 `K3_TILELANG` 出发，填出下表：

     | 要素 | 推导结果 |
     | --- | --- |
     | label | `K3` |
     | 专属解释器变量 | `PEGAINFER_K3_TILELANG_PYTHON` |
     | pregen 目录变量 | `PEGAINFER_K3_TILELANG_PREGEN` |
     | 产物目录 | `OUT_DIR/tilelang/k3` |
     | stub 文件 | `OUT_DIR/k3_tilelang_stub.cu` |
     | Rust cfg | `k3_tilelang` |
     | rustc-env 导出 | `PEGAINFER_K3_TILELANG_ARCH` 等（该家族 `required_manifest` 为空，导出为空串） |
   - 逐条在 [build.rs:L2086-L2105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2086-L2105)、[build.rs:L2018-L2018](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2018-L2018)、[build.rs:L2063-L2084](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2063-L2084) 找到生成这些名字的 `format!` 模板。
3. **需要观察的现象**（可选的运行验证）：在有 CUDA 工具链但**没有** TileLang 的机器上 `cargo build --release -p pegainfer-kernels --features k3`，日志应出现 `cargo:warning=K3 TileLang generation unavailable: ...` 与 `NOT_SUPPORTED stubs` 字样，且构建仍然成功。
4. **预期结果**：表格每一格都能指到具体源码行；stub 降级构建验证**待本地验证**（本环境无 CUDA 工具链）。
5. 若无法确认运行结果，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 stub 层的 launcher 清单必须和生成器发射的**参数也**一致，而不只是函数名一致？
**答案**：C 符号没有名字修饰（mangling），只要名字对就能链接通过；若 stub 声明的参数与真身不同，链接不报错，但调用方按 stub 签名压栈、真身按自己的签名读参，属于「链接成功然后被错误调用」。`checked_launchers` 用 `same_parameters` 归一化空白后逐参比对（[build.rs:L1904-L1927](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1904-L1927)）。

**练习 2**：`min_sm: 90` 对 GEMMA4 family 意味着什么？在 sm_86 的机器上开 `gemma4` feature 构建会怎样？
**答案**：Q/K 加载会被 lower 成 TMA，TMA 需要 Hopper（SM90）及以上；sm_86 上 `generate_tilelang_artifacts` 检查 `target_sm < family.min_sm` 后打印 warning 并返回 `None`（[build.rs:L2003-L2009](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2003-L2009)），该家族落到 stub 层——构建通过、调用时报 `cudaErrorNotSupported`。

**练习 3**：pregen 梯队解决了什么问题？manifest 里的路径为什么相对 manifest 所在目录解析？
**答案**：给装不了（指定版本）TileLang 的构建主机一条离线路径：别处用 `generate.py --vendor-includes` 产出的目录自带 `manifest.txt`，指过来即可。相对解析让这个目录「拷到哪都自洽」（[build.rs:L1826-L1834](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1826-L1834) 注释），绝对路径条目则原样通过。

### 4.3 GLM52 cubin 嵌入：从 checked-in 二进制到可链接符号

#### 4.3.1 概念说明

glm52 线的稀疏 MLA decode 用的是 FlashInfer 的 **TRT-LLM 生成式（trtllm-gen）FMHA kernel 家族**。这些 kernel 上游只以**预编译 cubin** 形式发布（没有可自行编译的源码），所以本 crate 把 7 个 cubin 文件直接 checked-in 在 `cubin/glm52/` 下，构建期把它们转成一个 C 头文件（十六进制字节数组），编进静态库——最终 Rust 侧链接到的符号，是一个按 kernel 名 + SHA-256 提供字节的 C++ 加载器。

同一 feature 下还有两条相关的构建期通道，一并认识：

- **DeepGEMM / FlashMLA 的 AOT 实例化**：vendored 第三方**设备头**由本 crate 自己的 `.cu` 文件 include 并用 nvcc 提前实例化（`-DDG_NO_TORCH` 去 torch 化、无运行时 JIT）。
- **CuTe DSL 导出**（可选）：`PEGAINFER_CUTEDSL_PYTHON` 指向装了 `nvidia-cutlass-dsl` 的 Python，导出 fp8 GEMM 对象并链 `libcute_dsl_runtime.so`；不设则 shim 以空表 stub 编译。

#### 4.3.2 核心流程

```text
glm52 feature 开启（= ["moe"]，Cargo.toml L45）
  └─ main L2227-L2230：
       ├─ generate_glm52_trtllm_fmha_cubins
       │     对表里 7 个 (symbol, file_name)：
       │       rerun-if-changed <cubin 文件>
       │       读字节 → 按 16 字节/行写成 "static const unsigned char <symbol>[] = {...}"
       │                + "<symbol>Size" 常量
       │     全部写入 OUT_DIR/glm52_trtllm_fmha_cubins.inc
       └─ build_glm52_cutedsl_fp8_dsl（可选 CuTe DSL 通道）
  之后 nvcc 编 csrc/glm52/glm52_flashinfer_sparse.cu：
       -I OUT_DIR + -DTLLM_GEN_FMHA_CUBIN_PATH="embedded"
       → #include "glm52_trtllm_fmha_cubins.inc"
       → FlashInfer runner 的 getCubin() 从内存字节取 cubin 并校验 SHA-256
  最终 ar 进 libkernels_cuda.a，Rust 经 cargo:rustc-link-lib=static=kernels_cuda 链接
```

#### 4.3.3 源码精读

**七元组表**：

[build.rs:L42-L71](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L42-L71)：`GLM52_TRTLLM_FMHA_CUBINS` 是 7 个 `(符号名, 文件名)` 对——符号名是短名（`kGlm52FmhaSparseSeedQ8` 等，即将成为 C 数组名），文件名是 FlashInfer 的长命名 cubin。

**字节→头文件**：

[build.rs:L73-L111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L73-L111)：`generate_glm52_trtllm_fmha_cubins` 先给**每个 cubin 文件**声明 `cargo:rerun-if-changed`（L79-L87，换 cubin 必然触发重建），读字节后按每行 16 个 `0x..,` 写成数组并附 `Size` 常量（L88-L102），最后整体写出 `glm52_trtllm_fmha_cubins.inc`（L104-L110）。这段代码没有任何编译——它只是**资产格式转换**：二进制 → C 源。

**消费方**：

[csrc/glm52/glm52_flashinfer_sparse.cu:L15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_flashinfer_sparse.cu#L15)：`#include "glm52_trtllm_fmha_cubins.inc"`——能找到它是因为该 TU 的 nvcc 参数带 `-I OUT_DIR` 与 `-DTLLM_GEN_FMHA_CUBIN_PATH=\"embedded\"`（[build.rs:L2628-L2639](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2628-L2639)），后者把 FlashInfer runner 切到「嵌入式 cubin」模式而非读文件。

[glm52_flashinfer_sparse.cu:L30-L93](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_flashinfer_sparse.cu#L30-L93)：`getCubin` 按 kernel 名子串匹配到嵌入数组，**并比对 SHA-256**——不匹配直接抛异常；表外名字抛 "requested an unembedded cubin"。SHA 值来自 vendored 的 `trtllm_gen/flashInferMetaInfo.h`（见 [cubin/glm52/README.md:L10-L11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/cubin/glm52/README.md#L10-L11)）。这解释了 README 的告诫：**升级 FlashInfer submodule 必须连同 cubin 一起换**，因为 `KernelParams` 布局烙在 cubin 里。

**形状约束在两侧成对出现**：

[glm52_flashinfer_sparse.cu:L113-L150](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_flashinfer_sparse.cu#L113-L150)：入口硬校验 `heads == 16`、`tokens ∈ {1,2,4,8}`、`topk ∈ {256,2048}`、设备 `major==10 && minor∈{0,3}`——与 [cubin/glm52/README.md:L3-L7](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/cubin/glm52/README.md#L3-L7) 说的「batch={1,2,4,8}、topk={256,2048}、16 头、E4M3 Q/K/V、BF16 输出」完全对应：**7 个 cubin 恰好是这组形状的极小选择闭包**。

**DeepGEMM 通道的 nvcc 实例化**：

[build.rs:L2656-L2683](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2656-L2683)：`glm52_deepgemm_mqa` / `glm52_deepgemm_grouped_sm100` 两个 TU 从 vendored DeepGEMM 设备头 AOT 实例化，注释写明「torch-free via DG_NO_TORCH, no runtime JIT」，带 CUTLASS 与 fmt 头路径、C++20。FlashMLA 通道同理（[build.rs:L2585-L2612](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2585-L2612)）。

**CuTe DSL 通道（可选）**：

[build.rs:L124-L197](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L124-L197)：`build_glm52_cutedsl_fp8_dsl` 仅当设置了 `PEGAINFER_CUTEDSL_PYTHON` 才跑 `tools/cutedsl/export_glm52_fp8_dsl.py` 导出对象、给 shim 定义 `GLM52_CUTEDSL_AOT` 并链 `cute_dsl_runtime`；否则 shim 以空表 stub 编译、运行时走 CUTLASS 路线。这是「可选 AOT、必绿构建」的又一实例。

#### 4.3.4 代码实践：解码 cubin 文件名

1. **实践目标**：把 `GLM52_TRTLLM_FMHA_CUBINS` 表里 7 个文件名的命名片段还原成 kernel 形状参数，并与 `cubin/glm52/` 目录实物核对。
2. **操作步骤**：
   - 列出 [cubin/glm52/](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/cubin/glm52/README.md) 下 7 个 `.cubin`（文件名与表一一对应），完成下表（「依据」列给出源码出处；标「待确认」的片段其精确语义以 FlashInfer trtllm-gen 命名为准，u8-l3 深入）：

     | 片段 | 含义 | 依据 |
     | --- | --- | --- |
     | `fmha` | 融合多头注意力家族 | 命名 |
     | `Sm100f` | SM100 家族 cubin | README「SM100-family」；运行时校验 `major==10, minor∈{0,3}` |
     | `QkvE4m3` | Q/K/V 均为 FP8 E4M3 | `DATA_TYPE_E4M3 ×3`（L153-154） |
     | `OBfloat16` | 输出 BF16 | `DATA_TYPE_BF16`（L154） |
     | `HQk576` | Q/K 头维 576（512 latent + 64 旋转位置） | `kHeadDimQk = 576`（L21） |
     | `HV512` | V 头维 512 | `kHeadDimV = 512`（L22） |
     | `HVPerCta128` / `HVPerCta256` | 每 CTA 承担的 V 维切片（split-V 解码） | README「right-sizing V per CTA」 |
     | `PagedKvDense` | 分页 KV 池、稠密页 | `QkvLayout::PagedKv`（L171） |
     | `StaticTokenSparseP1` | 静态 token 稀疏（按 top-k 选页）；`P1` 具体含义**待确认** | `TrtllmGenSparseMlaType::StaticTokenSparse`（L195） |
     | `VarSeq` | 变长序列 | `seqLensKvPtr`（L160） |
     | `MultiCtasKv` | 多 CTA 切分 KV（全局计数器 + 归约） | `mMultiCtasKvMode`（L194）、workspace 计数器（L198） |
     | `Persistent`（vs `Static`） | persistent 调度形态（无 MultiCtas 的短序列档） | 与 `TileScheduler::Static`（L193）对照 |
     | `Q8` / `Q16` | 生成路径的 query tile 档位；精确语义**待确认** | `tokens ∈ {1,2,4,8}` 校验（L135） |
     | `Kv128` | KV 侧 tile 常量 128；单位**待确认** | 命名 |
     | `SwapsAbForGen` | 为 generation 交换 A/B 操作数布局的变体；细节**待确认** | `FmhaKernelType::Generation`（L192） |
   - 再对照符号名归档：`Seed*` 两个是**种子 kernel**——README（L11-13）说明 FlashInfer 在按 CTA 定 V 尺寸前要先加载它们，即使它们不出现在最终的 Nsight 符号里，也必须保留。
3. **需要观察的现象**：表中每一行都能在 `glm52_flashinfer_sparse.cu` 或 cubin README 中找到出处；7 个文件名与表条目严格一一对应（可用 `ls cubin/glm52/*.cubin` 与表列比对）。
4. **预期结果**：得到一张「文件名片段 → 形状参数 → 源码依据」的完整解码表；三个待确认片段如实标注，不臆断。
5. 本实践为源码阅读型，无需 GPU；SHA-256 比对行为（改一个字节重建即失败）**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 cubin 要 checked-in，而 Triton/TileLang 的产物不进仓库？
**答案**：Triton/TileLang 有可重放的生成器（Python 源在仓库里），产物放 `OUT_DIR` 即可复现；trtllm-gen FMHA 上游只发布预编译 cubin，没有源码可编译，只能作为二进制资产入库（[build.rs:L73-L111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L73-L111) 做的是格式转换而非生成）。

**练习 2**：`-DTLLM_GEN_FMHA_CUBIN_PATH=\"embedded\"` 去掉会发生什么？
**答案**：FlashInfer runner 的 cubin 加载器会退回按路径读文件的默认行为，而构建产物里并没有这些 cubin 文件，运行时找不到 kernel；该宏配合 `-I OUT_DIR` 让 `getCubin` 走内存字节分支（[build.rs:L2628-L2639](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2628-L2639)）。

**练习 3**：为什么升级 FlashInfer submodule 必须连 cubin 一起升？
**答案**：cubin 内烙着 vendored runner 的 `KernelParams` 内存布局；runner 头变了而 cubin 不换，加载器按新头解释旧布局，轻则 SHA-256 校验失败抛异常，重则按错位参数启动 kernel。README 用一句话概括："bump the submodule and the cubins together"（[cubin/glm52/README.md:L7-L8](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/cubin/glm52/README.md#L7-L8)）。

### 4.4 AOT 相关环境变量矩阵

#### 4.4.1 概念说明

四条通道各有自己的旋钮，全部以环境变量表达。它们共享两条纪律：**设了就 fail-fast**（显式指定的解释器坏了立刻报错，不静默换）与**全部声明 rerun-if-env-changed**（换旋钮必然触发重生成）。

#### 4.4.2 核心流程与源码精读

速查矩阵（出处均为 build.rs，行号为当前 HEAD）：

| 变量 | 通道 | 作用 | 探测顺序 / 失败行为 | rerun 声明 |
| --- | --- | --- | --- | --- |
| `PEGAINFER_TRITON_PYTHON` | Triton | 指定带 Triton 的 Python | 设了就只用它；import 失败即 panic（L1017-1030） | L1529 |
| （无变量） | Triton | 自动探测 `.venv/bin/python` → `python3` → `python` | 全失败才 panic，附全部诊断（L1032-1050） | — |
| `PEGAINFER_CUDA_SM` | Triton / TileLang / nvcc | 钉死 SM 目标；多目标时 Triton 取最大值并警告 | 上一讲已讲；Triton 侧见 L1149-1163 | L2909 |
| `PEGAINFER_TILELANG_PYTHON` | TileLang（通用） | 所有 family 共用的兜底解释器 | 在家族专属变量之后探测（L1750-1753） | L2099 |
| `PEGAINFER_<LABEL>_TILELANG_PYTHON` | TileLang（按家族） | 如 `PEGAINFER_K3_TILELANG_PYTHON` | 优先级最高；设了但空/坏即报错（L1754-1764） | L2095-2097 |
| `PEGAINFER_<LABEL>_TILELANG_PREGEN` | TileLang（按家族） | 指向离线生成目录（须含 `manifest.txt`） | 目录不可读即 panic（L1960-1976） | L2095-2097 |
| `PEGAINFER_<LABEL>_TILELANG_JOBS` | TileLang（按家族） | 生成并行度旋钮；本 crate 仅声明 rerun 跟踪，消费者在模型 crate 的生成器（**待确认**） | — | L2095-2097 |
| `PEGAINFER_GEMMA4_W4A16_SMS` | TileLang（gemma4 w4a16） | w4a16 家族的 SM 相关旋钮；消费者在 gemma4 侧（**待确认**） | — | L2753 |
| `PEGAINFER_CUTEDSL_PYTHON` | CuTe DSL（glm52） | 带 torch + nvidia-cutlass-dsl 的 Python；不设则空表 stub | 设置后导出失败即断言失败（L124-158） | L125 |
| `PEGAINFER_BUILD_TIMING` / `PEGAINFER_NVCC_JOBS` | 全部 | 相位计时 / nvcc 并行度（u1-l3 已讲；AOT 相位 `triton-gen`、`tilelang-gen`、`cc ...` 也被 `time_phase` 包裹） | — | L2912-2913 |

精读锚点：TileLang 的 rerun 声明循环在 [build.rs:L2095-L2099](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2095-L2099)（对 `PYTHON/PREGEN/JOBS` 三个旋钮统一发声明）；CuTe DSL 的环境变量读取在 [build.rs:L125-L133](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L125-L133)。

#### 4.4.3 代码实践：验证矩阵与源码一一对应

1. **实践目标**：证明矩阵里每个变量都在 build.rs 里有「读取点 + rerun 声明点」两个锚。
2. **操作步骤**：
   - 在 crate 根执行 `grep -n "TILELANG_" build.rs`、`grep -n "TRITON_PYTHON" build.rs`、`grep -n "CUTEDSL" build.rs`；
   - 把每个命中行归类为「读取」（`env::var`）或「声明」（`rerun-if-env-changed`），与上表对照；
   - 特别验证 `tilelang_nvcc_tasks` 里对 `check-cfg` 的无条件声明（[build.rs:L2100-L2105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2100-L2105)）——注释解释了为什么 cfg 必须无条件声明。
3. **需要观察的现象**：grep 命中数与矩阵行数对得上；`PEGAINFER_K3_TILELANG_JOBS` 只有 rerun 声明、没有本 crate 内的读取点（其消费者在 `pegainfer-k3/kernels/generate.py`，本 crate 视角标注待确认）。
4. **预期结果**：得到一张带行号的双锚表；任何「只声明不读取」或「只读取不声明」的变量都会暴露出来（后者是增量构建的隐患）。
5. 本实践纯 grep + 阅读，可在无 GPU 环境完成；已在本讲义撰写过程中用 grep 核对过主要锚点。

#### 4.4.4 小练习与答案

**练习 1**：Triton 与 TileLang 的解释器探测链在「显式变量失败」时的行为有何不同？为什么可以不同？
**答案**：Triton 显式变量失败**立即 panic**（构建必须停，因为 qwen35 的 kernel 只有这一条来路）；TileLang 的任何失败都只是 `cargo:warning` + 降级 stub（构建继续，调用时响亮失败）。差异源于产物必要性：qwen35 的 GDR prefill 是功能主体，而 TileLang 家族在设计上就允许「这一档构建先绿着」。

**练习 2**：为什么 `rerun-if-env-changed` 对 AOT 通道比对普通 nvcc 编译更关键？
**答案**：AOT 产物（cubin / 生成 CUDA）比源码编译多一层「环境输入」——解释器路径、pregen 目录、SM 覆盖都会改变产物内容而不改动任何源文件；不声明的话 cargo 增量构建会直接复用旧产物，把错误的目标架构或旧版本生成体带进二进制。

**练习 3**：`PEGAINFER_CUTEDSL_PYTHON` 不设时 glm52 还能构建吗？
**答案**：能。shim 以空表 stub 编译，运行时留在 CUTLASS 路线（[build.rs:L117-L123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L117-L123) 注释）——与 TileLang stub 梯队同构的「可选 AOT、必绿构建」策略。

## 5. 综合实践

**任务：为一台「无 GPU、无 Triton」的 CI 机器写一份 AOT 通道行为预测书。**

设想 CI 机器只有 CUDA 工具链（nvcc 可用），依次执行三个构建并**先写预测、后验证**：

1. `cargo build --release -p pegainfer-kernels`（默认 feature）
   - 预测：不跑任何 Python；日志出现 `Qwen3.5 Triton AOT kernels disabled ...`（[build.rs:L2876-L2879](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2876-L2879)）与 `Gemma 4 TileLang kernels disabled ...`、`K3 TileLang kernels disabled ...`（L2762-2791）。
2. `PEGAINFER_CUDA_SM=120 cargo build --release -p pegainfer-kernels --features gemma4`
   - 预测：`tilelang_arch` 推出 `sm_120`，gemma4 家族走到「找不到 TileLang Python」的 warning，两个 gemma4 家族都落 **stub 梯队**；`OUT_DIR` 出现 `gemma4_tilelang_stub.cu` 与 `gemma4_w4a16_tilelang_stub.cu`；构建成功。
3. `cargo build --release -p pegainfer-kernels --features qwen35`
   - 预测：`find_triton_python` 全链失败后 **panic**（不是 warning），错误信息以 "Could not find a Python interpreter with Triton installed" 开头并列出每个候选的诊断（[build.rs:L1047-L1050](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1047-L1050)）。
4. 最后把 4.3 的 cubin 解码表与 4.1 的 qwen35 引导清单附在预测书末尾，作为「这台机器要跑通 qwen35 / glm52 各缺什么」的行动清单。

**预期结果**：三个预测全部命中即说明你已掌握「feature → 通道 → 梯队」的完整判定逻辑；本环境无 CUDA 工具链，全部**待本地验证**。

## 6. 本讲小结

- 本 crate 有**四条**构建期生成通道：Triton AOT（qwen35，唯一强制构建期 Python）、TileLang AOT（gemma4/k3 的 family 表）、checked-in cubin 嵌入（glm52）、vendored CUDA 头的 nvcc AOT 实例化（DeepGEMM/FlashMLA，另有可选的 CuTe DSL 导出）。
- `TritonKernelSpec` 七字段是一份 kernel 编译处方；生成器以 `FUNC_NAME=`/`C_PATH=` 文本协议回传；生成的单例 `CUmodule` 被补丁成按设备表，cubin 字节内嵌在 C 源里由 `cuModuleLoadData` 运行时加载。
- TileLang 的**三层梯队**（generate / pregen / stub）让无 Python 主机也能构建成功，代价是调用时返回 `cudaErrorNotSupported`；`checked_launchers` 与 `checked_manifest_fields` 两道断言堵住「stub 与真身 ABI 漂移」和「manifest 缺键导致 fail-open」两个静默错误面。
- glm52 的 7 个 cubin 是「batch={1,2,4,8}、topk={256,2048}、16 头、E4M3/BF16」形状的极小选择闭包；构建期只是把二进制转成 C 数组头，SHA-256 与 `KernelParams` 布局约束把 cubin 与 vendored runner 绑成必须同步升级的一对。
- 所有 AOT 旋钮都是环境变量，遵循「显式设置即 fail-fast、全量声明 rerun-if-env-changed」两条纪律。

## 7. 下一步学习建议

- 下一讲 u1-l5（KERNELS.md：算子索引与编辑规则）将教你用 `KERNELS.md` 这张路由表把本讲的「构建期产物」与运行时的 op 调用对上号。
- 若想先深入 Triton 生成器本身，读 [tools/triton/gen_triton_aot.py](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/gen_triton_aot.py) 的 `OfflineCudaDriver` 与 `triton.tools.compile` 兼容分支（u5-l2 会完整走这条链）。
- TileLang 家族的 Rust 侧消费（`cfg(k3_tilelang)`、`PEGAINFER_*_TILELANG_ARCH` 环境变量如何变成运行时拒绝）在 u6（gemma4）与 u8-l4（k3）展开。
- cubin 消费侧的完整调用链（`getCubin` → `TllmGenFmhaRunner` → Rust `glm52_flashinfer_sparse_mla_*`）留到 u8-l3。
