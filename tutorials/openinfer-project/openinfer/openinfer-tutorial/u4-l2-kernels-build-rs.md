# kernels 构建系统：nvcc、SM 探测、Triton/TileLang AOT

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立追踪 `pegainfer-kernels/build.rs` 从「发现 CUDA 工具链」到「产出 `libkernels_cuda.a` 并交给 rustc 链接」的完整流水线。
2. 解释 SM 目标的三级探测策略（`PEGAINFER_CUDA_SM` 环境变量 → `nvidia-smi` 查询 → panic），以及 `PEGAINFER_CUDA_SM` 为何是 CI 与无 GPU 构建环境的「逃生舱」。
3. 掌握 cargo feature 如何裁剪 `csrc/` 编译单元：每个模型线一个目录过滤函数（`is_glm52_source`、`is_k3_source` 等），feature 不开则整个目录不进编译列表。
4. 说清楚 qwen35 的 Triton AOT 与 k3 的 TileLang AOT 是「构建期跑 Python、运行期零 Python」的关键机制，以及 k3 的「生成/预生成/桩」三级回退。

本讲是单元 4（共享运行时）的第二讲。上一讲（u4-l1）我们看了 kernels crate 的张量与设备类型——那些是**运行时**代码；本讲往下挖一层，看这些类型背后的 CUDA 内核是**怎么被编译出来的**。回顾 u1-l2 的结论：PegaInfer 默认构建（qwen3 feature）全程无 Python，正是因为所有 GPU 内核都在构建期用 nvcc/Triton AOT 预编译成静态库，运行期只做 `dlopen` 级别的调用，没有任何 JIT。

## 2. 前置知识

### 2.1 什么是 cargo build script（build.rs）

Rust 的 cargo 允许每个包带一个 `build.rs`（构建脚本），它在**编译该包的 Rust 代码之前**运行，典型用途是编译 C/C++ 依赖、生成代码、设置链接参数。build.rs 与普通 Rust 程序的通信靠两条通道：

- **环境变量进**：cargo 注入 `OUT_DIR`（构建产物目录）、`CARGO_MANIFEST_DIR`（包根目录）、`NUM_JOBS` 等。
- **stdout 指令出**：build.rs 向 stdout 打印 `cargo:` 前缀的指令，cargo 解析后生效。本讲会反复见到这几条：

| 指令 | 作用 |
|------|------|
| `cargo:rustc-link-lib=static=kernels_cuda` | 让 rustc 链接名为 `libkernels_cuda.a` 的静态库 |
| `cargo:rustc-link-search=native=<目录>` | 告诉链接器去哪个目录找库 |
| `cargo:rerun-if-changed=<路径>` | 该路径变化时才重新运行 build.rs（不发这条则任何变化都重跑） |
| `cargo:rerun-if-env-changed=<变量>` | 该环境变量变化时重新运行 build.rs |
| `cargo:warning=<文本>` | 构建时向用户打印一条警告（也被本项目用作信息输出通道） |

注意 `cfg!(feature = "...")` 在 build.rs 里读到的是**本包**（pegainfer-kernels）的 feature 集合，这正是 feature 门控编译的入口。

### 2.2 nvcc、SM 与 gencode

- **nvcc** 是 NVIDIA 的 CUDA 编译器，把 `.cu` 文件（C++ + 设备端扩展）编译成 `.o` 目标文件。
- **SM**（Streaming Multiprocessor）编号即 GPU 的「计算能力」（compute capability），如 RTX 4090 是 `sm_89`、H100 是 `sm_90`、B200 是 `sm_100`。为某个 SM 编译的 SASS 机器码只能在同代或兼容代上跑。
- **`-gencode arch=compute_X,code=sm_X`** 告诉 nvcc：以 PTX 虚拟架构 `compute_X` 为源，生成 `sm_X` 的 SASS。还可以额外生成 `code=compute_X`（把 PTX 嵌入），供驱动在未来架构上即时翻译。
- 后缀字母有讲究：`sm_90a`/`sm_100f` 这类「加速/家族」变体解锁架构专属指令（如 Hopper 的 TMA、Blackwell 的 tcgen05），但二进制不能跨代运行。本讲会看到 build.rs 如何针对特定内核选择这些变体。

### 2.3 AOT 与 JIT

- **JIT（即时编译）**：运行期才把中间表示（Triton Python、TileLang Python、PTX）编译成机器码——需要运行环境里有编译器（通常就是 Python + GPU 工具链）。vLLM/PyTorch 生态大量使用这种方式。
- **AOT（ahead-of-time，提前编译）**：构建期就把内核编译成 cubin/机器码，产物直接链接进二进制。PegaInfer 对 Triton/TileLang 内核采取 AOT：构建主机上跑一次 Python 生成 C 启动代码与 cubin，之后运行期只需要 CUDA Driver API 加载——这就是「构建期可选 Python、运行期零 Python」的实现基础。

### 2.4 一句话回顾 workspace 里的位置

`pegainfer-kernels` 是唯一带重量级 build.rs 的 crate（见 u1-l3 的职责地图）：它拥有 `csrc/*.cu` 内核源码、CUDA FFI 声明，以及对 FlashInfer/DeepEP/DeepGEMM/FlashMLA 等第三方子模块的编译编排。`pegainfer-build` 则是一个小的纯 Rust 辅助 crate，只负责「找到 CUDA 工具链在哪」这一件事。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pegainfer-kernels/build.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs) | 本讲主角：2568 行构建脚本，编排 submodule 自愈、工具链发现、SM 探测、csrc 过滤、并行 nvcc、静态库归档与 Triton/TileLang AOT |
| [pegainfer-build/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs) | `CudaToolkit::discover`：覆盖经典/conda/HPC SDK 三种安装布局的工具链定位 |
| [pegainfer-kernels/tools/triton/gen_triton_aot.py](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/gen_triton_aot.py) | Triton AOT 生成器：离线指定 target、调用 `triton.tools.compile`、回传 `FUNC_NAME=`/`C_PATH=` 契约 |
| [pegainfer-kernels/tools/triton/README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/README.md) | Triton AOT 的引导文档：`.venv` 引导、`PEGAINFER_TRITON_PYTHON`、Windows 说明 |
| [pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py) | 被 AOT 的 Triton 内核源（Qwen3.5 Gated Delta Rule chunkwise prefill 七个内核） |
| [pegainfer-kernels/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/Cargo.toml) | kernels crate 的 feature 定义表（`qwen35`/`glm52`/`k3`/`moe` 等） |
| [docs/subsystems/kernels/build-rs-submodule-init.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/kernels/build-rs-submodule-init.md) | submodule 自动初始化机制的设计与验证记录 |

另外会顺带引用 `pegainfer-k3/kernels/generate.py`（TileLang 生成器入口，被 build.rs 调用）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **工具链发现**：pegainfer-build 与 submodule 自愈（main 的第 1～2 步）。
2. **SM 目标探测与 gencode 决策**：`PEGAINFER_CUDA_SM` 三级策略与架构探测。
3. **feature 门控的 csrc 收集、nvcc 并行池与静态库归档**：构建的主体。
4. **Triton AOT（qwen35）与 TileLang AOT（k3）**：构建期 Python 的两套 AOT 通道。

### 4.1 工具链发现：pegainfer-build 与 submodule 自愈

#### 4.1.1 概念说明

编译 CUDA 代码前要先回答两个问题：

1. **nvcc 在哪？** CUDA Toolkit 的安装位置因机器而异：可能是 `/usr/local/cuda`（经典布局）、conda 环境里的 `targets/aarch64-linux/include`（conda 布局，头文件不在顶层 `include/`），或 NVIDIA HPC SDK 的 `.../cuda/<ver>` + `.../math_libs/<ver>` 兄弟树。
2. **第三方内核头文件在吗？** FlashInfer、DeepEP、DeepGEMM、FlashMLA 都是 git submodule，新 clone 默认是空目录——如果构建脚本不做处理，用户会在编译中途撞上一堆「找不到头文件」的困惑报错。

`pegainfer-build::CudaToolkit` 解决问题 1，`ensure_git_submodules_initialized` 解决问题 2。

#### 4.1.2 核心流程

main 的开头三步：

```text
main()
 ├─ ensure_git_submodules_initialized(workspace_root)   # 有未初始化 submodule？自动 git submodule update --init --recursive
 ├─ toolkit = CudaToolkit::discover()                   # CUDA_HOME → CUDA_PATH → /usr/local/cuda
 │    ├─ nvcc = {root}/bin/nvcc（不存在则退回 PATH 上的裸 nvcc）
 │    └─ include_dirs / lib_dirs（经典 + targets/<arch> + HPC math_libs，去重）
 └─ cuda_include = toolkit.header_dir("cuda.h")          # 找真正含 cuda.h 的目录（conda 布局关键）
```

submodule 自愈的判断逻辑很克制：只有当 `git submodule status --recursive` 的输出中**至少一行以 `-` 开头**（`-` 前缀 = 未初始化）时才执行网络操作 `git submodule update --init --recursive`，避免每次构建都做无谓的 git 网络请求。

#### 4.1.3 源码精读

先看 build.rs 里 submodule 自愈的入口调用：

[pegainfer-kernels/build.rs:L203-L258](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L203-L258) —— `ensure_git_submodules_initialized`：先确认这是 git 检出（`.git` 与 `.gitmodules` 都存在），跑 `git submodule status --recursive`，若任一行以 `-` 开头则打印警告并执行 `git submodule update --init --recursive`；git 不可用或更新失败时 panic 并给出手动命令提示。

[pegainfer-kernels/build.rs:L1887-L1895](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1887-L1895) —— main 的前三行：submodule 自愈 → `CudaToolkit::discover()` → 用 `header_dir("cuda.h")` 定位真正的 CUDA 头文件目录（找不到就退回 `{root}/include`）。`out_dir` 取自 cargo 注入的 `OUT_DIR`。

再看工具链发现本体：

[pegainfer-build/src/lib.rs:L67-L89](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs#L67-L89) —— `CudaToolkit` 结构与 `discover()`：环境变量优先级为 `CUDA_HOME` → `CUDA_PATH` → 默认 `/usr/local/cuda`；同时声明 `rerun-if-env-changed`，改这两个变量会触发重建。

[pegainfer-build/src/lib.rs:L91-L121](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs#L91-L121) —— `from_root()`：`nvcc` 取 `{root}/bin/nvcc`，不是文件就退回 PATH 上的裸 `nvcc`（conda 布局里工具链没有自己的 nvcc）；include/lib 目录依次收集 `{root}/include`、`{root}/lib64`、`{root}/lib`、`targets/<arch>/{include,lib}`（`target_dirs()` 对 aarch64 同时尝试 `aarch64-linux` 与 `sbsa-linux` 两种名字），HPC SDK 布局再追加 `../math_libs/<ver>/lib*` 兄弟树。所有目录经 `existing_deduped` 过滤（只留真实存在且未重复的）。

[pegainfer-build/src/lib.rs:L125-L142](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs#L125-L142) —— `header_dir(header)`：在候选 include 目录里找**真正包含指定头文件**的那个——conda 布局顶层 `include/` 存在却没有 CUDA 头文件，这条 API 就是为此设计；`link_search()` 把 lib 目录逐个输出为 `cargo:rustc-link-search`。

[pegainfer-build/src/lib.rs:L161-L269](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs#L161-L269) —— 这个 crate 自带一组临时目录单测，用 `TempTree` 模拟四种布局并断言发现结果（`classic_layout`、`conda_layout`、`hpc_sdk_layout_adds_math_libs_sibling`、`symlinked_dirs_dedupe`）。这是「构建逻辑也可测试」的好范例——不需要装 CUDA 就能验证路径逻辑。

#### 4.1.4 代码实践

**实践：跑 pegainfer-build 的布局单测（无需 GPU/CUDA）**

1. **实践目标**：验证工具链发现逻辑的三种布局分支，建立「build 逻辑也能写单测」的直觉。
2. **操作步骤**：

   ```bash
   cargo test --release -p pegainfer-build --lib
   ```

3. **需要观察的现象**：5 个测试全部通过，其中包括 `conda_layout`（断言 `header_dir("cuda.h")` 落在 `targets/<arch>/include` 而非顶层 `include`）与 `hpc_sdk_layout_adds_math_libs_sibling`。
4. **预期结果**：`test result: ok. 7 passed`（数量以当前版本为准）。
5. 若你所在环境连 cargo 都不可用，则改为精读 `from_root` 的目录收集顺序，画出三种布局各自的目录树。本教程编写环境未执行该命令，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `header_dir` 不能直接返回 `{root}/include`？
**答案**：conda 布局的 CUDA 包顶层 `include/` 目录存在但不含 CUDA 头文件，真正的头文件在 `targets/<arch>/include`；直接返回会导致 nvcc 的 `-I` 指向空目录、编译报「找不到 cuda.h」。

**练习 2**：`ensure_git_submodules_initialized` 为什么不无条件执行 `git submodule update`？
**答案**：build script 在每次相关变化后都会重跑，无条件 update 会带来无谓的网络访问与延迟；它只在 `submodule status` 输出里出现 `-` 前缀（未初始化）行时才执行，已初始化的常规构建零额外开销。

### 4.2 SM 目标探测与 gencode 决策

#### 4.2.1 概念说明

nvcc 需要知道「为哪代 GPU 编译」。编多了（为所有架构）浪费编译时间；编错了（本机是 H100 却只编了 sm_89）运行时直接崩。PegaInfer 的策略是「**为构建机上实际存在的 GPU 精确编译**」，探测优先级：

1. `PEGAINFER_CUDA_SM`（或旧名 `CUDA_SM`）环境变量——人工指定，例如 `120` 或 `120,80` 多目标；
2. `nvidia-smi --query-gpu=compute_cap` 查询本机所有 GPU 的计算能力，去重；
3. 都失败 → `panic!("GPU detection failed")`。

这个设计把「构建机 = 目标机」作为默认假设（推理引擎的常见情形），同时用环境变量覆盖 CI 容器（里面往往没有 GPU、但有 nvcc）的场景——这正是 CLAUDE.md 把 `PEGAINFER_CUDA_SM` 列为关键环境变量的原因。

#### 4.2.2 核心流程

```text
detect_sm_targets()
 ├─ PEGAINFER_CUDA_SM / CUDA_SM 环境变量
 │    └─ 逗号分隔 → parse_sm_token 逐个解析（"120"、"sm_120"、"12.0"、"120f" 均可）
 │        ├─ 有合法 token → 直接返回（不再查 nvidia-smi）
 │        └─ 全部非法 → 警告并继续下探
 ├─ nvidia-smi 查询 compute_cap → 解析 + BTreeSet 去重排序
 └─ 失败 → panic

normalize_nvcc_sms()                    # 对探测结果按 nvcc 能力修正
 └─ nvcc --list-gpu-arch                # 问 nvcc 支持哪些 compute_ 架构
      └─ 例如 "120"/"120f" 优先升到 "120f"（家族目标）；nvcc 不支持则退回裸数字并警告

nvcc_arch_args()                        # 生成 gencode 参数
 ├─ 每个 SM 一条 -gencode arch=compute_X,code=sm_X
 └─ 为最大 SM 追加一条 code=compute_X（嵌 PTX，供未来架构 JIT 兜底）
```

单目标编译时，总 nvcc 时间近似为

\[ T_{\text{total}} \approx \left\lceil \frac{N_{\text{cu}}}{J} \right\rceil \cdot \bar{t}_{\text{cu}} \]

其中 \( N_{\text{cu}} \) 是编译单元数（本仓库当前 69 个 `.cu`），\( J \) 是并行 job 数，\( \bar{t}_{\text{cu}} \) 是单个文件的平均编译耗时。多一个 SM 目标近似让 \( \bar{t}_{\text{cu}} \) 翻倍——这就是「精确探测而非全架构编译」的价值。

#### 4.2.3 源码精读

[pegainfer-kernels/build.rs:L417-L447](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L417-L447) —— `detect_sm_targets()`：三级策略本体。注意环境变量解析失败只是打 `cargo:warning` 并继续下探，而两级探测全部失败时 `panic!("GPU detection failed")`——构建期硬失败，绝不带着空目标列表往下走。

[pegainfer-kernels/build.rs:L314-L347](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L314-L347) —— `parse_sm_token()`：宽容解析各种写法——`"8.9"` → `"89"`，`"9"` → `"90"`（个位数补零），`"sm_120a"` / `"compute_100f"` 剥前缀，`"120f"` 保留字母后缀（加速/家族变体）。非法输入返回 `None` 由调用方跳过。

[pegainfer-kernels/build.rs:L392-L415](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L392-L415) —— `sm_targets_from_nvidia_smi()`：调 `nvidia-smi --query-gpu=compute_cap --format=csv,noheader`，逐行解析后用 `BTreeSet` 去重（多卡同构机器只得到一个目标）。

[pegainfer-kernels/build.rs:L349-L390](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L349-L390) —— `nvcc_supported_arches()`（跑 `nvcc --list-gpu-arch` 收集支持的 `compute_*` 集合）与 `normalize_nvcc_sm()`/`normalize_nvcc_sms()`：探测出的 `120`/`120f` 在 nvcc 支持时统一升到 `120f`（家族目标，覆盖整条 sm_12x 线），nvcc 太老不支持 `compute_120f` 时退回裸 `120` 并警告。

[pegainfer-kernels/build.rs:L449-L465](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L449-L465) —— `nvcc_arch_args()`：每个目标一条 `-gencode arch=compute_X,code=sm_X`，再为数值最大的目标追加 `code=compute_X` 的 PTX 兜底。

[pegainfer-kernels/build.rs:L467-L510](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L467-L510) —— `nvcc_accepts_gencode()`：一个精巧的「能力探针」——向临时目录写一个只含空内核的 `.cu`，用待验证的 gencode 组合试编译一次，成功与否决定是否采用。后面 glm52/k3 的 `sm_100f`、k3 TileLang 的 `sm_103a` 决策都复用这个探针，避免「build.rs 以为 nvcc 支持而实际报错」的晚失败。

[pegainfer-kernels/build.rs:L1911-L1926](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1911-L1926) —— 探测结果通过两条 `cargo:warning` 打印出来（`Detected CUDA SM targets: ...` 与 `Compiling CUDA kernels for nvcc targets: ...`），构建日志里可以直接核对生效目标。

#### 4.2.4 代码实践

**实践：观察 SM 探测的决策输出**

1. **实践目标**：亲眼看环境变量覆盖与自动探测两条路径的差异。
2. **操作步骤**：
   - 路径 A（有 GPU 机器）：`PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-kernels`，在输出中找到 `Detected CUDA SM targets` 一行；
   - 路径 B（同机）：`PEGAINFER_CUDA_SM=120 PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-kernels`，对比该行是否变为 `sm_120`（并注意若 nvcc 支持 `compute_120f` 会升为 `120f`）。
3. **需要观察的现象**：路径 A 显示真实 GPU 的计算能力；路径 B 显示被 pin 的目标；同时 `Compiling CUDA kernels for nvcc targets` 反映 normalize 之后的最终 gencode 集合。
4. **预期结果**：两行的目标列表按上述规则变化。注意改动会触发全部内核重编（`rerun-if-env-changed=PEGAINFER_CUDA_SM`），耗时较长。
5. 本教程编写环境无 GPU 与 CUDA 工具链，无法实际执行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：CI 容器里有 nvcc 但没有 GPU（`nvidia-smi` 不可用），构建会发生什么？怎么救？
**答案**：`detect_sm_targets` 两级探测都失败，直接 `panic!("GPU detection failed")`。救法是在 CI 环境里设置 `PEGAINFER_CUDA_SM`（如 `120` 或 `120,80`），env 路径优先于 nvidia-smi 且不会触发 panic。

**练习 2**：为什么 `nvcc_arch_args` 要为最大目标额外生成一条 `code=compute_X`（PTX）？
**答案**：SASS 只能在同代 GPU 上运行；嵌入 PTX 后，若二进制被拿到更新的架构上，驱动还能以 PTX 即时翻译兜底，避免「换了张卡就 `no kernel image`」。

### 4.3 feature 门控的 csrc 收集、nvcc 并行池与静态库归档

#### 4.3.1 概念说明

这是 build.rs 的主体。设计要点有三：

1. **feature = 目录级裁剪**。`csrc/` 下按模型线分目录（`glm52/`、`k3/`、`kimi_k2/`、`deepseek_v2_lite/`、`gemma4/`、`deepep/`、`marlin/`、`qwen35/`、`shared/`），每个受控目录配一个 `is_<line>_source` 谓词函数；feature 未启用时该目录下的 `.cu` 直接不进编译列表——不是「编译成空桩」，而是**根本不编译**。这保证默认（qwen3）构建只编共享内核，构建时间与 toolkit 版本门槛（如 gemma4 的 NVFP4 内在函数需要 CUDA ≥ 12.8）都最低。
2. **每个编译单元可以有专属编译参数**。通用内核吃通用 `arch_args`；glm52/k3 的 DeepGEMM、FlashMLA、FlashKDA 等内核则换成专属 gencode（`sm_100f` 家族目标或 `90a/103a` 加速变体）、专属 `-I` 与 `-D`。当目标架构不满足时**降级为 NOT_SUPPORTED 桩**（内核存在但调用返回「不支持」），保证链接完整。
3. **并行 nvcc 池 + 优先级调度**。69 个 `.cu` 逐个串行编译会非常慢；build.rs 用一个 `Mutex<VecDeque>` 任务队列加 N 个 OS 线程（N 默认取 CPU 并行度）抢任务编译，且把解码关键路径内核（`paged_attention` 等）排到队头优先编译。

最终所有 `.o` 用 `ar rcs` 归档成 `libkernels_cuda.a`，再通过 `cargo:rustc-link-lib=static=kernels_cuda` 交给 rustc。

#### 4.3.2 核心流程

```text
main() 主体
 ├─ 读 feature 开关：deepseek-v2-lite / moe / gemma4 / glm52 / kimi-k2 / k3 / qwen35
 ├─ （glm52）生成 TRTLLM FMHA cubin 头 + 可选 CuTe DSL fp8 GEMM 导出
 ├─ moe/glm52/k3 → require_*_submodules：断言第三方子模块文件存在
 ├─ collect_files_recursively(csrc/) → 逐文件过滤：
 │     is_deepseek_v2_lite_source / is_deepep_source / is_gemma4_source /
 │     is_marlin_source / is_glm52_source / is_kimi_k2_source / is_k3_source
 │     + 排除 replaced_cuda_files（activation/embedding/fused_attention 已退役）
 │     + 只留 .cu 扩展名
 ├─ 逐文件构造 NvccTask：通用 args + 按 stem 的专属 gencode/-I/-D 分支
 ├─ （k3）追加 TileLang 生成的 nvcc 任务
 ├─ 按 nvcc_task_priority 排序（paged_attention 最优先）
 ├─ Mutex<VecDeque> + thread::scope 起 min(jobs, tasks) 个 worker 并行 nvcc
 ├─ obj_files.sort() → ar rcs libkernels_cuda.a
 ├─ （可选 PEGAINFER_KERNEL_LAB）链接 libglm52_kernel_lab.so
 └─ 输出链接指令：kernels_cuda/cudart/cublas/cublasLt/cuda/nvrtc（+nccl, moe 时）
```

#### 4.3.3 源码精读

[pegainfer-kernels/Cargo.toml:L23-L38](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/Cargo.toml#L23-L38) —— feature 定义表：`default = []`（kernels 自身默认无模型 feature）；注释明确写着 `qwen35` 是「唯一需要构建期 Python + Triton 的 feature」；`glm52 = ["moe"]`、`kimi-k2 = ["moe"]` 体现依赖传递（用 DeepEP 基座就得带上 moe 子模块）。

[pegainfer-kernels/build.rs:L1899-L1910](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1899-L1910) —— main 里用 `cfg!(feature = "...")` 一次性读出七个开关；`glm52_enabled` 时先做 cubin 头生成与可选 CuTe DSL 导出（后者由 `PEGAINFER_CUTEDSL_PYTHON` 门控，未设置则编空表桩、运行期走 CUTLASS 路线）。

[pegainfer-kernels/build.rs:L690-L759](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L690-L759) —— 一组同构的目录谓词：`is_deepseek_v2_lite_source`、`is_kimi_k2_source`、`is_deepep_source`、`is_marlin_source`、`is_gemma4_source`、`is_glm52_source`、`is_k3_source`。实现都是 `path.strip_prefix(csrc_dir)` 后看相对路径的任一组件是否等于目录名。注意 `is_marlin_source` 的注释：Marlin 是「共享四比特权重基座」，跟着任一消费者（gemma4/kimi-k2）启用，而非独立 feature。

[pegainfer-kernels/build.rs:L1943-L1986](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1943-L1986) —— csrc 收集与过滤主循环：先递归收集全部文件，再对每个文件跑一遍上面七个谓词（feature 关闭即丢弃），排除 `replaced_cuda_files`（`activation.cu`、`embedding.cu`、`fused_attention.cu` 三个旧翻译单元已退役，见 L1928-L1929 与 L1997-L2004 的警告输出），最后只保留 `.cu` 扩展名。

> **勘误式说明**：学习手册的大纲里提到 build.rs 有一个 `is_qwen35_source` 过滤函数——**当前 HEAD 里并不存在**。`csrc/qwen35/` 下的三个文件（`conv1d.cu`、`gated_delta_rule.cu`、`prefill_attention_hd256.cu`）是原生 CUDA 内核，**无条件参与编译**；`qwen35` feature 门控的是另一件事——构建期 Triton AOT 阶段（见 4.4 节）。以源码为准，这正是本手册强调「引用前先核实」的原因。

[pegainfer-kernels/build.rs:L762-L810](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L762-L810) —— `require_submodule_file` 及三个 feature 各自的子模块断言：缺文件时 panic 信息直接给出该跑的 `git submodule update --init --recursive <路径>` 命令。`moe` 需要 DeepEP/DeepGEMM/FlashMLA；`glm52` 需要 FlashMLA 内嵌 CUTLASS；`k3` 只需要 DeepGEMM 及其内嵌 CUTLASS/fmt（无 DeepEP/NCCL 依赖）。

[pegainfer-kernels/build.rs:L2016-L2064](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2016-L2064) —— `NvccTask` 构造循环的开头与按 stem 的 gencode 分支：`glm52_fp8_gemm` 尝试 `glm52_fp8_gemm_arch_args`（sm_100 系升 `100a`），`glm52_deepgemm_mqa`/`glm52_deepgemm_grouped_sm100` 要求 `sm_100f` 否则编 NOT_SUPPORTED 桩；`k3_deepgemm_fp8_fp4_grouped_sm100` 与 `k3_mega_moe_*` 同理。每个分支失败时都退回通用 `arch_args` 并打警告——构建永远能完成，只是特定内核变成「不可用」状态。

[pegainfer-kernels/build.rs:L2167-L2252](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2167-L2252) —— include 路径分支：包含 FlashInfer 头的内核（`paged_attention`、`flashinfer_norm`、`flashinfer_sampling` 等）拿到 FlashInfer 的 include/csrc/cutlass/spdlog/**自带 CCCL** 一整套 `-I`（注释解释了为什么 vendored CCCL 必须以 `-I` 传入而 CTK 目录用 `-isystem`：要覆盖工具链里较旧的 CCCL 拷贝）；`kimi_*`/`marlin_nvfp4` 走类似分支；DeepEP shim 镜像上游 JIT 的编译旗标（C++20、`-Xptxas --register-usage-level=10` 等）并接上 `PEGAINFER_NCCL_ROOT` 的 NCCL 头。

[pegainfer-kernels/build.rs:L1021-L1056](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1021-L1056) —— `flashinfer_includes()`：`PEGAINFER_FLASHINFER_INCLUDE` 环境变量优先（校验 `flashinfer/sampling.cuh` 存在），否则按候选顺序探测 submodule 目录与 `.venv` 里 pip 安装的 flashinfer data 目录。

[pegainfer-kernels/build.rs:L282-L312](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L282-L312) —— 并行度与优先级：`nvcc_job_count()` 依次尝试 `PEGAINFER_NVCC_JOBS` → `NUM_JOBS`（cargo 注入）→ `thread::available_parallelism()`；`nvcc_task_priority()` 把 `paged_attention`（解码主内核）排 0 级、`paged_attention_hd512`/`flashinfer_sampling` 排 1 级，其余 10 级——同优先级内 `cu_files.sort()` 已保证字典序稳定。

[pegainfer-kernels/build.rs:L2434-L2478](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2434-L2478) —— 并行池本体：任务装进 `Mutex<VecDeque>`，`thread::scope` 起 `min(nvcc_jobs, 任务数)` 个 worker，每个 worker 循环 `pop_front` 抢任务跑 nvcc（每次编译套 `time_phase` 计时），队列空即退出；主线程 join 后逐个断言 `status.success()`，任何失败直接 panic 点名出错的 `.cu` 文件。

[pegainfer-kernels/build.rs:L2484-L2500](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2484-L2500) —— 归档：所有 `.o` 排序后 `ar rcs` 成 `OUT_DIR/libkernels_cuda.a`（先删旧档保证干净）。

[pegainfer-kernels/build.rs:L2519-L2542](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2519-L2542) —— 链接指令输出：`OUT_DIR` 进链接搜索路径，静态链接 `kernels_cuda`，再链 `cudart`/`cublas`/`cublasLt`/`cuda`（DeepGEMM 运行时需要 driver API）/`nvrtc`，`moe` 时额外链 NCCL（`link_deepep_nccl` 用 OUT_DIR 里的符号链接解决 wheel 只带 `libnccl.so.2` 没有未版本化名字的问题），非 Windows 再链 `stdc++`。

[pegainfer-kernels/build.rs:L260-L280](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L260-L280) —— `build_timing_enabled()`/`time_phase()`：`PEGAINFER_BUILD_TIMING` 打开后，每个阶段（每个 nvcc 任务、ar、Triton 生成等）的耗时以 `cargo:warning=build-timing <label> <秒>` 打印——这是本讲实践任务的观察工具。

#### 4.3.4 代码实践

**实践：对比 feature 开关裁剪了多少编译单元**

1. **实践目标**：用真实构建日志验证「feature = 目录级裁剪」。
2. **操作步骤**：
   ```bash
   # 默认构建（无模型 feature），打开计时与单线程便于数任务
   PEGAINFER_BUILD_TIMING=1 PEGAINFER_NVCC_JOBS=1 \
     cargo build --release -p pegainfer-kernels 2>&1 | grep -c 'build-timing nvcc'

   # 开 k3 后再数一次（需要 DeepGEMM 子模块；无 TileLang Python 也不怕，会走桩回退）
   PEGAINFER_BUILD_TIMING=1 PEGAINFER_NVCC_JOBS=1 \
     cargo build --release -p pegainfer-kernels --features k3 2>&1 | grep -c 'build-timing nvcc'
   ```
3. **需要观察的现象**：两次计数之差 = k3 专属目录 `csrc/k3/` 的 `.cu` 数 + TileLang 生成（或桩）单元数；同时日志里能看到 `nvcc csrc/.../paged_attention.cu` 排在最先编译。
4. **预期结果**：第二次计数明显多于第一次；若环境缺 TileLang Python，还会出现 `cargo:warning=K3 TileLang generation unavailable`，随后生成桩单元。需要 CUDA 工具链；本教程编写环境不可执行，**待本地验证**。
5. 无 GPU 环境的替代方案（纯阅读）：数一数 `csrc/` 下各模型目录的 `.cu` 文件数，与 `is_*_source` 谓词一一对应，制成「feature → 目录 → 文件数」表。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `is_marlin_source` 没有对应的独立 feature？
**答案**：Marlin 是被多条模型线（gemma4、kimi-k2）共享的四比特权重基座；build.rs 在 `!gemma4_enabled && !kimi_k2_enabled` 时才剔除它——「跟着消费者走」，避免为一个共享基座单设 feature。

**练习 2**：`paged_attention` 被排到编译优先级 0 有什么意义？
**答案**：多 worker 并行时任务队列是共享的，优先级排序让最影响增量构建反馈的解码主内核最先编完；对全量构建总时长影响不大，但在部分文件变化触发的重建里能更早暴露 `paged_attention` 自身的编译错误。

**练习 3**：若把本机 `PEGAINFER_CUDA_SM` 误设成显卡不支持的值（如 90 设在游戏卡上），构建会失败吗？运行会发生什么？
**答案**：构建大概率成功——nvcc 只管编译，`sm_90` 是合法目标。失败发生在运行期：内核镜像与本机 SM 不匹配，加载/启动内核时报 `no kernel image for device` 一类错误。这提示 SM 探测的「构建机 = 目标机」假设被人工打破时要自己负责。

### 4.4 Triton AOT（qwen35）与 TileLang AOT（k3）

#### 4.4.1 概念说明

Triton 和 TileLang 都是用 Python 描述 GPU 内核的 DSL，常规用法是运行期 JIT。PegaInfer 把它们改成**构建期 AOT**：构建主机上跑一次 Python，把内核编译成 cubin 并生成一个 C 启动函数（内部 `cuModuleLoadData` 加载 cubin、`cuLaunchKernel` 启动），C 文件再由 `cc` crate 编译并链接进静态库。运行期的 Rust 代码只通过 FFI 调这个 C 函数——不需要 Python、不需要 Triton 运行时。

两套通道的形态不同：

- **Triton（qwen35，强制）**：`--features qwen35` 时**必须有** Python + Triton（`find_triton_python` 找不到直接 panic），因为 Qwen3.5 的 Gated Delta Rule chunkwise prefill 七个内核全部靠它生成。它还解决了一个微妙的离线问题：Triton 默认要查询活动 GPU 决定 target，而构建机可能无 GPU——生成器用 `OfflineCudaDriver` 骗过 Triton，直接喂进 `--target cuda:<sm>:32`。
- **TileLang（k3，三级回退）**：K3 的解码小内核由 `pegainfer-k3/kernels/generate.py` 用 TileLang 生成。它不强制构建机装 Python，而是三级回退——① 有 TileLang Python 就生成；② 否则用 `PEGAINFER_K3_TILELANG_PREGEN` 指向的预生成目录；③ 都没有就编译一个「全部返回 `cudaErrorNotSupported`」的桩，保持 feature 可链接、调用时快速失败而非静默算错。此外 glm52 还有一条可选的 CuTe DSL AOT（`PEGAINFER_CUTEDSL_PYTHON` 门控，未设置编空表桩），思路同 k3 的「可选增强、缺则降级」。

#### 4.4.2 核心流程

Triton AOT 链路（`--features qwen35` 时）：

```text
main() → qwen35_enabled?
 └─ compile_triton_aot_kernels(cuda_include, out_dir, sm_targets)
     ├─ find_triton_python()            # PEGAINFER_TRITON_PYTHON → .venv/bin/python → python3 → python
     ├─ triton_target(sm_targets)       # "cuda:<最大sm>:32"（多目标时取最大并警告）
     ├─ 对 7 个 TritonKernelSpec 逐个：
     │    generate_triton_artifacts()
     │      ├─ python gen_triton_aot.py --kernel-path ... --signature ... --target ...
     │      │     └─ Triton 离线编译 → <out>/triton_aot/<name>/{.cubin,.c,...}
     │      │     └─ stdout 契约：FUNC_NAME=... / C_PATH=...
     │      ├─ patch_triton_aot_per_device_handles()   # 改写 C：单 CUmodule → 16 槽按设备表
     │      └─ write_wrapper()            # 生成稳定的 extern "C" 包装（含 CUstream 参数）
     └─ cc 编译全部生成 C → libtriton_kernels_aot.a；rustc-link-lib=cuda
```

TileLang AOT 链路（`--features k3` 时，三级回退）：

```text
main() → cfg!(feature = "k3") → k3_tilelang_nvcc_tasks()
 ├─ ① k3_tilelang_pregen_artifacts()         # env 指向预生成目录（manifest.txt 契约）
 ├─ ② generate_k3_tilelang_artifacts()       # 找到 TileLang Python 才跑 generate.py
 ├─ ③ 都失败 → write_k3_tilelang_stub()      # 全量 cudaErrorNotSupported 桩
 └─ 产物 .cu（或桩）→ 变成普通 NvccTask 并入并行池
```

#### 4.4.3 源码精读

[pegainfer-kernels/build.rs:L2511-L2517](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2511-L2517) —— qwen35 门控的总开关：`if qwen35_enabled { compile_triton_aot_kernels(...) } else { 警告"需构建期 Python + Triton" }`。注意这发生在 nvcc 归档**之后**——Triton 产物是独立的一块，不进 `libkernels_cuda.a`。

[pegainfer-kernels/build.rs:L977-L1011](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L977-L1011) —— `find_triton_python()`：`PEGAINFER_TRITON_PYTHON` 显式指定（设了但为空是错误）；否则探测 workspace 的 `.venv/bin/python` → `python3` → `python`，每个候选实跑 `python -c "import triton"` 验证，全部失败时把每个候选的报错拼进 panic 信息并指向 `tools/triton/README.md`。

[pegainfer-kernels/build.rs:L1109-L1123](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1109-L1123) —— `triton_target()`：取 SM 列表的**最大值**拼成 `cuda:{max_sm}:32`；多目标时警告「每个内核只出一个 cubin，用了最高目标，可用 PEGAINFER_CUDA_SM pin 单目标」。

[pegainfer-kernels/build.rs:L1299-L1310](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1299-L1310) —— `TritonKernelSpec` 的一个完整样例（GDR chunk prepare）：内核源文件路径、内核名、**签名字符串**（`*bf16,*fp32,...,i32`——指针/标量/constexpr 逐个列出，`128,128` 结尾的是 constexpr 块大小）、grid 表达式、`num_warps`/`num_stages`。同一文件里 L1299-L1461 共定义了 7 个这样的 spec，全部来自 `gated_delta_rule_chunkwise_kernels.py`。

[pegainfer-kernels/build.rs:L1137-L1200](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1137-L1200) —— `generate_triton_artifacts()`：以子进程跑 `gen_triton_aot.py`，全部参数显式传入；成功后从 stdout 逐行解析 `FUNC_NAME=` 与 `C_PATH=` 两个契约字段（生成器端对应打印见 [gen_triton_aot.py:L114-L115](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/gen_triton_aot.py#L114-L115)）——build.rs 与 Python 之间没有 JSON、没有文件锁，就靠这两行 stdout。

[pegainfer-kernels/tools/triton/gen_triton_aot.py:L18-L39](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/gen_triton_aot.py#L18-L39) —— **离线编译的关键**：`OfflineCudaDriver` 实现了 Triton 需要的最小驱动接口——`get_current_target()` 直接返回构造时喂入的 `GPUTarget`，`map_python_to_cpp_type` 复用官方 `ty_to_cpp`；`activate_target_driver` 把 `cuda:120:32` 这样的字符串拆开并 `triton.runtime.driver.set_active` 注入。于是 Triton 在**没有 GPU 的构建机**上也能完成 AOT 编译。

[pegainfer-kernels/tools/triton/gen_triton_aot.py:L42-L92](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/gen_triton_aot.py#L42-L92) —— `compile_kernel_compat()`：新版 Triton 直接调 `triton.tools.compile` 的 `CompileArgs` API；旧版退回 `runpy` 冒充命令行跑 `triton.tools.compile` 模块，再从输出目录里挑最新的 `.c` 文件并用正则抠出启动函数名。一个生成器兼容两代 Triton API。

[pegainfer-kernels/build.rs:L1202-L1270](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1202-L1270) —— `patch_triton_aot_per_device_handles()`：Triton 生成的 C 启动代码默认只有**一个**全局 `CUmodule`/`CUfunction`（首次调用懒加载）——在多 GPU 进程里这会让 1 号卡复用 0 号卡的模块句柄。这段补丁用一串文本替换把单变量改写成 `[PEGAINFER_TRITON_DEVICE_TABLE_SIZE]`（16 槽）按 `cuCtxGetDevice` 的结果索引的表，并把 `cuModuleLoadData`/`cuLaunchKernel` 等调用点全部改为带 `[dev]` 下标。生成代码也要「改得动」，这是 AOT 路线的一个现实代价。

[pegainfer-kernels/build.rs:L1463-L1490](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1463-L1490) —— `compile_triton_aot_kernels` 的收尾：所有生成的 `.c` 交给 `cc::Build`（`-std=c11`、include CUDA 头目录、`cuda(false)` 走主机编译器）编成 `triton_kernels_aot` 库，然后 `cargo:rustc-link-lib=cuda`（C 启动代码用 Driver API）；同时为内核 `.py`、生成器与 `PEGAINFER_TRITON_PYTHON` 声明 `rerun-if-changed/-env-changed`。

[pegainfer-kernels/build.rs:L1492-L1521](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1492-L1521) —— k3 TileLang 区块的注释块：写明三级回退策略（generate 优先 → pre-generated 目录 → 桩），并强调桩返回 `cudaErrorNotSupported` 是「响亮失败而非静默算错」的刻意选择；区块自称 self-contained，除 main 里一个 `cfg!(feature = "k3")` 外全部封闭在这对标记之间。

[pegainfer-kernels/build.rs:L1522-L1569](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1522-L1569) —— `K3_TILELANG_LAUNCHERS`：生成器必须吐出的 11 个 `extern "C"` 入口签名表，桩层要逐字匹配这份表否则链接失败——一张表同时是生成器的输出契约与桩的模拟对象。

[pegainfer-kernels/build.rs:L1800-L1881](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L1800-L1881) —— `k3_tilelang_nvcc_tasks()`：先试 pregen、再试生成；生成路径要求 nvcc 能组装出 TileLang 降低目标所用的加速变体 gencode（`k3_tilelang_gencode` 复用 `nvcc_accepts_gencode` 探针，不行则退桩）；产出的 `.cu`（或桩）转成普通 `NvccTask` 并入第 4.3 节的并行池——AOT 产物最终与手写内核走同一条 nvcc 归档流水线。

[pegainfer-kernels/build.rs:L2407-L2432](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L2407-L2432) —— main 里的接入点：`cfg!(feature = "k3")` 时把 TileLang 任务追加进 `nvcc_tasks`，并为 vendored FlashKDA 头文件补 `rerun-if-changed`（这些头经 `#include` 进入 `csrc/k3/k3_flash_kda.cu`，csrc 目录级跟踪看不见它们）。

[pegainfer-kernels/tools/triton/README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/tools/triton/README.md) —— 面向用户的引导：`uv venv .venv && uv pip install triton` 一条命令引导仓库级 Python；明确「默认构建（仅 Qwen3）从不跑 Triton、不需要 Python」；Windows 用 `triton-windows` 社区轮子。

#### 4.4.4 代码实践

**实践：写通「`--features qwen35` 从开关到 Triton AOT 产物落地」的因果链**

这是本讲义规格中指定的实践任务。原任务表述为「定位 `is_qwen35_source` 与 `generate_triton_artifacts` 的调用条件」——经核实当前 HEAD **不存在** `is_qwen35_source`（见 4.3.3 的勘误说明），因此按下述与源码一致的版本执行：

1. **实践目标**：把 feature 开关到产物落地的每一步因果跳转写成一段有据可查的文字说明。
2. **操作步骤**：
   1. 读 [Cargo.toml:L23-L38](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/Cargo.toml#L23-L38)：确认 `qwen35 = []`（kernels 侧无额外依赖）。
   2. 在 build.rs 定位两个调用点：`qwen35_enabled` 的赋值（L1906）与 `if qwen35_enabled { compile_triton_aot_kernels(...) }`（L2511-L2512）。
   3. 顺调用链记录：`compile_triton_aot_kernels`（L1281）→ `find_triton_python`（L1288）→ `triton_target`（L1289）→ 七个 `TritonKernelSpec`（L1299-L1461）→ 每个_spec 调 `generate_triton_artifacts`（L1311 等七处）→ 子进程 `gen_triton_aot.py` → stdout 的 `FUNC_NAME=`/`C_PATH=` → `patch_triton_aot_per_device_handles`（L1198）→ `cc` 归档 `triton_kernels_aot`（L1472-L1474）→ `rustc-link-lib=cuda`（L1476）。
   4. 写出你的因果链说明（示例骨架）：「`--features qwen35` → cargo 把 feature 传给 kernels 包的 build.rs → `cfg!(feature = "qwen35")` 为真 → 归档完 `libkernels_cuda.a` 后调用 `compile_triton_aot_kernels` → 找到一个能 `import triton` 的 Python → 按 SM 最大值定 target → 逐 spec 调 Python 生成器产出 cubin+C 启动代码 → 补丁改多 GPU 句柄表 → cc 编译进静态库 → 链接 cuda driver 库。任何一步失败（无 Python、生成器报错）都在构建期 panic，而非运行期。」
   5. 有环境的话执行 `PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-kernels --features qwen35`，观察 `build-timing triton-gen <kernel>` 七条与 `build-timing cc triton_kernels_aot` 一条的耗时分布。
3. **需要观察的现象**：七个 GDR 内核各有一条 triton-gen 计时；多 SM 目标时出现「using highest detected target」警告；产物落在 `target/release/build/pegainfer-kernels-*/out/triton_aot/` 下。
4. **预期结果**：你的因果链文字能逐行对应到上面列出的源码位置；计时输出能区分 Python 生成阶段与 cc 编译阶段。
5. 本教程编写环境无 CUDA/Triton，步骤 5 **待本地验证**；步骤 1-4 是纯阅读任务，现在即可完成。

#### 4.4.5 小练习与答案

**练习 1**：Triton AOT 为什么需要 `OfflineCudaDriver`？
**答案**：Triton 编译时默认向活动 GPU 驱动查询目标架构；构建机常常无 GPU（CI 容器）。`OfflineCudaDriver` 实现最小驱动接口并把 `--target` 传入的 `GPUTarget` 直接返回，让 Triton 在离线环境照常完成编译。

**练习 2**：k3 的三级回退里，为什么「桩返回 `cudaErrorNotSupported`」优于「跳过编译」？
**答案**：跳过编译会让 `k3` feature 链接失败（Rust 侧 FFI 符号缺失），或迫使 build.rs 条件性地改 Rust 源。桩保住了 11 个 `extern "C"` 符号（与 `K3_TILELANG_LAUNCHERS` 表逐字对齐），链接永远完整；运行期一旦真调用就拿到明确的「不支持」错误码，快速失败、不会静默算出错误结果。

**练习 3**：`patch_triton_aot_per_device_handles` 解决什么问题？如果不打这个补丁，什么场景会出错？
**答案**：Triton 生成的 C 启动代码用单个全局 `CUmodule`/`CUfunction` 懒加载。多 GPU（多 context）进程里，第二个设备会复用第一个设备加载的模块/函数句柄，`cuLaunchKernel` 在错误的 context 上执行而失败或行为异常。补丁改成按 `cuCtxGetDevice` 索引的 16 槽设备表，每个设备各自加载。kimi-k2/glm52 这类 8 卡 EP 模型线若复用此机制，没有补丁就会在 rank>0 上出错。

## 5. 综合实践

**任务：为一次「qwen3 默认构建」写出完整的构建剧本，并标注每个阶段的源码落点。**

把本讲四个模块串起来。假设你在 一台有单张 GPU、装有 CUDA Toolkit 的机器 上 clone 了仓库，执行：

```bash
cargo build --release -p pegainfer-kernels
```

请完成：

1. **时序表**：按真实执行顺序列出 build.rs 的阶段（submodule 自愈 → 工具链发现 → SM 探测与 normalize → feature 读取与 csrc 过滤 → NvccTask 构造 → 并行 nvcc → ar 归档 → 链接指令输出），每行给出对应的源码行号区间。
2. **裁剪说明**：默认 feature 下（`default = []`），指出哪些 `csrc/` 目录被剔除、哪些保留，并说明 `csrc/qwen35/` 为何保留（提示：它是原生 CUDA，不是 Triton）。
3. **计时验证**（可选，需环境）：用 `PEGAINFER_BUILD_TIMING=1` 重新构建，把输出里每条 `build-timing` 按你时序表的阶段归类，找出最耗时的三个 nvcc 编译单元，与 `nvcc_task_priority` 的排序对照，验证优先级是否生效。
4. **诊断演练**（纸面）：构建日志出现 `GPU detection failed`、`Could not find a Python interpreter with Triton`、`The moe feature requires the DeepEP ... submodule` 三条报错时，各自给出处置命令（答案分别涉及 `PEGAINFER_CUDA_SM`、`uv venv .venv && uv pip install triton`（或 `PEGAINFER_TRITON_PYTHON`）、`git submodule update --init --recursive third_party/DeepEP`——注意第三个场景你实际上启用了 `moe` 类 feature）。

**验收标准**：时序表的每个阶段都能点开对应源码链接对上；裁剪说明与 `is_*_source` 谓词逐一吻合；诊断演练的命令来自本讲引用的 panic 文本。

## 6. 本讲小结

- `pegainfer-kernels/build.rs` 是全仓唯一的重型构建脚本：submodule 自愈（仅在有 `-` 前缀状态行时联网）→ `pegainfer_build::CudaToolkit::discover`（CUDA_HOME → CUDA_PATH → /usr/local/cuda，覆盖经典/conda/HPC SDK 三布局）→ SM 三级探测（`PEGAINFER_CUDA_SM` → nvidia-smi → panic）→ feature 门控的目录级 csrc 裁剪 → 并行 nvcc 池（`Mutex<VecDeque>` + N worker，`paged_attention` 优先）→ `ar rcs libkernels_cuda.a` → 链接指令。
- **feature = 目录谓词**：`is_glm52_source`/`is_k3_source` 等七个谓词在 feature 关闭时把整个模型目录挡在编译列表外；`csrc/qwen35/` 是例外——它是原生 CUDA，无条件编译，`qwen35` feature 只门控 Triton AOT 阶段（大纲中提到的 `is_qwen35_source` 在当前 HEAD 不存在，以源码为准）。
- 架构决策靠**能力探针**：`nvcc_accepts_gencode` 用临时空内核试编译判断 nvcc 是否支持 `100f`/`90a` 等变体；不满足时 glm52/k3 的专属内核降级为 NOT_SUPPORTED 桩，构建永远可完成。
- **Triton AOT（qwen35）**是构建期 Python 的强制通道：`OfflineCudaDriver` 让无 GPU 构建机也能定 target；`FUNC_NAME=`/`C_PATH=` 两行 stdout 是 Rust↔Python 的全部契约；生成后的 C 代码还要打「按设备句柄表」补丁才能安全用于多 GPU。
- **TileLang AOT（k3）走三级回退**：能生成就生成、有预生成目录就用、都没有就编 `cudaErrorNotSupported` 桩保链接；glm52 的 CuTe DSL 是同思路的可选增强。AOT 是「运行期零 Python」的根基——Python 只在构建期出现。
- `PEGAINFER_BUILD_TIMING=1` + `time_phase` 给每个阶段（含每个 nvcc 任务）打点，是观察这一切的第一工具。

## 7. 下一步学习建议

- **下一讲（u4-l3）**：内核编译产物如何被 Rust 侧使用——`kernels::ffi` 的 extern 声明、`kernels::ops` 安全包装、`core::ops` 门面三层调用约定。你会看到本讲的 `libkernels_cuda.a` 里的符号如何在 Rust 里获得类型安全的签名。
- **延伸阅读**：`docs/subsystems/kernels/pegainfer-kernels-boundary.md`（kernels crate 的边界契约）；`pegainfer-kernels/tools/triton/README.md` 的 TVM FFI 一节（生成 cubin 的另一条消费路径）。
- **动手方向**：给一个 `csrc/shared/` 里的小内核（如 `norm.cu`）加一行 `#warning` 或改一个参数，配合 `cargo:rerun-if-changed` 观察增量构建只重编该翻译单元——验证本讲的裁剪与归档机制。
