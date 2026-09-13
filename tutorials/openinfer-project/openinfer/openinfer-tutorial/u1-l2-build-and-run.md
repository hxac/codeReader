# 构建与运行：从源码到 GPU 服务

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PegaInfer 从源码构建的三类前提：钉住的 Rust nightly 工具链、CUDA Toolkit（`nvcc` + cuBLAS）、NVIDIA 驱动。
2. 用 `cargo run --release -- --model-path models/Qwen3-4B` 完成一次「编译 → 启动 → curl 请求」的全流程（有 GPU 时），或在无 GPU 机器上用 `cargo check` 验证默认构建不依赖 Python。
3. 解释为什么必须 `--release`，以及 `CUDA_HOME`、`PEGAINFER_CUDA_SM`、`PEGAINFER_TRITON_PYTHON` 三个关键环境变量分别在构建的哪一步被谁消费。
4. 用 `install.sh` 安装预编译二进制，或用 `docker/dev.sh` 进入容器开发环境完成同样的构建。

## 2. 前置知识

本讲涉及几组基础概念，先用大白话过一遍：

- **cargo 与 workspace**：cargo 是 Rust 的构建工具。一个 *workspace* 把多个 crate（Rust 的编译单元，类似「子项目」）放在同一个 `Cargo.toml` 下统一构建、共享依赖版本。PegaInfer 有 20 个 workspace 成员，但默认只构建服务入口一条链路（见 4.1）。
- **rustup 与工具链钉住**：rustup 管理 Rust 编译器版本。仓库根目录的 `rust-toolchain.toml` 声明了「本仓库必须用哪个版本编译」，进入目录后 rustup 会自动下载并切换，无需手动 `rustup install`。
- **`--release` 与 debug 的区别**：debug 构建不做优化、插入大量运行时检查。对 CUDA 程序而言，debug 构建的 GPU 路径慢几个数量级，官方文档明确要求 GPU 构建始终用 `--release`，否则容易超时。
- **NVIDIA 驱动 vs CUDA Toolkit**：驱动是内核态软件，管 GPU 怎么跑；Toolkit 是开发包，含编译器 `nvcc` 和数学库 cuBLAS 等，管代码怎么变成 GPU 可执行文件。两者可以分开安装，版本需兼容。
- **SM / compute capability**：每代 NVIDIA GPU 有一个架构编号，例如 RTX 4090 是 `sm_89`，RTX 5090 是 `sm_120`。`nvcc` 需要在**编译期**知道目标架构，才能生成对应的机器码（`-gencode arch=compute_120,code=sm_120`）。这就是为什么构建脚本会去探测你的 GPU。
- **build.rs**：cargo 的「构建脚本」机制——在编译 Rust 代码**之前**运行的一段 Rust 程序，常用来编译 C/C++/CUDA。PegaInfer 用它把 `.cu` 内核编译成静态库再链接进来，这是「无 Python 运行时」的关键（内核是提前编译好的，不是运行时 JIT）。
- **Docker 开发环境**：把上面所有工具链版本（CUDA 13.2、nightly、Triton、TileLang、NCCL）固化成一个镜像，避免「在我机器上能编」的问题。

> 上一讲（u1-l1）已经建立了整体架构认知：七条模型线、仅 `qwen3` 默认开启、`pegainfer-server` 只做纯分发。本讲专注「怎么把它编出来、跑起来」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md) | Quickstart：预编译安装、源码构建命令、环境变量表、curl 示例 |
| [rust-toolchain.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/rust-toolchain.toml) | 钉住的 Rust nightly 版本与组件 |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml) | workspace 成员列表与 `default-members` |
| [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml) | 模型线 feature 表；默认 `qwen3` |
| [scripts/setup_dev.sh](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/scripts/setup_dev.sh) | 一键准备 Ubuntu GPU 主机的开发环境 |
| [install.sh](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/install.sh) | 预编译 Qwen3 二进制的下载安装器 |
| [docker/Dockerfile.dev](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/Dockerfile.dev) | 开发镜像：CUDA 13.2 + nightly + Triton/TileLang venv |
| [docker/dev.sh](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/dev.sh) | 容器包装脚本：build / shell / run 三个子命令 |
| [docker/README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/README.md) | 容器开发流程文档 |
| [pegainfer-build/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs) | `CudaToolkit::discover()`：CUDA 工具链发现（`CUDA_HOME` 在这里被消费） |
| [pegainfer-kernels/build.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs) | SM 目标探测、nvcc 编译、Triton AOT 入口（本讲只看关键函数，深入留给 u4-l2） |

## 4. 核心概念与源码讲解

### 4.1 cargo workspace 构建：`cargo run --release` 背后发生什么

#### 4.1.1 概念说明

PegaInfer 是一个 20 成员的 cargo workspace，但「构建 PegaInfer」默认不等于「构建全部 20 个 crate」。workspace 根配置把 `default-members` 设成了 `pegainfer-server` 一个，而 server 又通过 cargo feature 决定链接哪条模型线——默认只有 `qwen3`。这个设计带来两个直接后果：

1. `cargo run --release` 只编译 server → frontend → qwen3 → core/kernels 这条依赖链，其余六条模型线完全不参与编译。
2. 默认构建里没有任何 Python 依赖；只有当你启用 `qwen35`（Triton AOT）之类的 feature 时，构建期才需要 Python。

`--model-path` 是**运行期**参数（传给编译出的二进制，不是 cargo 的），两条 `--` 之后的参数原样透传给服务器程序。

#### 4.1.2 核心流程

在仓库根目录执行 `cargo run --release -- --model-path models/Qwen3-4B` 时：

```text
1. rustup 读取 rust-toolchain.toml → 安装/切换钉住的 nightly 工具链
2. cargo 读取根 Cargo.toml → default-members = ["pegainfer-server"]
   → 只解析 server 及其依赖闭包
3. server 默认 feature = qwen3 → 拉入 pegainfer-qwen3、pegainfer-frontend、
   pegainfer-core、pegainfer-kernels……
4. pegainfer-kernels 的 build.rs 先于 Rust 编译运行：
   发现 CUDA 工具链 → 探测 GPU SM 目标 → 用 nvcc 编译 csrc/*.cu → 归档静态库
5. 编译全部 Rust crate，链接出 target/release/pegainfer
6. cargo 直接运行该二进制，参数 --model-path models/Qwen3-4B
7. server 读取 checkpoint 的 config.json 识别模型家族 → 启动 Qwen3 引擎
   → 在 8000 端口提供 OpenAI 兼容 API
```

第 7 步的「识别模型家族」细节属于下一单元（u2-l1 的 main 分发流程），本讲不展开。

#### 4.1.3 源码精读

工具链钉住声明——任何人进入仓库目录，rustup 都会自动使用这个 nightly：

[rust-toolchain.toml:L1-L4](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/rust-toolchain.toml#L1-L4) —— 声明 `nightly-2026-07-10` 渠道及 `rustfmt`、`clippy` 两个组件。

workspace 只把 server 设为默认成员，同时列出全部 20 个成员：

[Cargo.toml:L1-L26](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L1-L26) —— `default-members = ["pegainfer-server"]` 加 members 列表；注意注释标出 `kvbm/kvbm-logical` 是从 dynamo fork 的 crate。

server 的 feature 表说明「默认构建为什么不需要 Python」：

[pegainfer-server/Cargo.toml:L33-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L33-L46) —— `default = ["qwen3"]`，注释写明「Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time」；其余六条线都是可选 feature，`qwen35` 一行的注释点明它会拉入 Triton AOT 内核（构建期需要 Python + Triton）。

README 给出的标准源码构建命令（注意 `--` 分隔 cargo 参数与程序参数）：

[README.md:L57-L68](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L57-L68) —— 「Use the Rust toolchain pinned in rust-toolchain.toml, a CUDA Toolkit with nvcc and cuBLAS, and a compatible NVIDIA driver. The default Qwen3 build needs no Python. Its driver floor is R545 / CUDA 12.3」，并给出 `export CUDA_HOME=/usr/local/cuda` 与 `cargo run --release -- --model-path models/Qwen3-4B`。

#### 4.1.4 代码实践

**实践目标**：验证「默认 qwen3 构建全程不需要 Python」，并观察构建产物与耗时。

**操作步骤**：

1. 克隆仓库后先初始化构建必需的子模块（否则 kernels 编译会找不到 FlashInfer 头文件，见 4.2）：

   ```bash
   git submodule update --init pegainfer-kernels/third_party/flashinfer
   git -C pegainfer-kernels/third_party/flashinfer submodule update --init 3rdparty/cccl
   ```

2. 在仓库根目录执行（无 GPU 机器必须显式指定 SM 目标，原因见 4.2；有 GPU 可省略前缀）：

   ```bash
   time env PEGAINFER_CUDA_SM=120 cargo check --release -p pegainfer-server
   ```

3. 构建成功后查看产物：

   ```bash
   ls -lh target/release/pegainfer   # 完整链接后约几十 MB，check 模式下不会生成
   ```

**需要观察的现象**：

- rustup 首次进入目录时自动下载 `nightly-2026-07-10` 工具链。
- 构建日志里出现 nvcc/`.cu` 编译输出（来自 `pegainfer-kernels` 的 build.rs），但**不出现**任何 `python`、`triton` 字样——这就是「默认构建无 Python」的直接证据。
- `time` 输出的 real 时间（首次构建含 CUDA 内核编译，通常以分钟计）。

**预期结果**：`cargo check`（或 `cargo build`）成功结束；若用 `cargo build --release -p pegainfer-server`，则 `target/release/pegainfer` 生成。二进制名是 `pegainfer`（由 server 的 `[[bin]]` 段定义）。

**注意**：即使无 GPU，`cargo check` 仍会运行 build.rs，因此机器上仍需安装 CUDA Toolkit（`nvcc`），并用 `PEGAINFER_CUDA_SM` 指定架构以跳过 GPU 探测；若连 Toolkit 都没有，此实践无法完成，请改做 4.2.4 的阅读型实践。具体耗时因机器而异——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cargo run --release -- --model-path ...` 中间要有 `--`？

答案：`--` 之前是 cargo 自己的参数（`--release`），之后的部分 cargo 不解析、原样传给编译出来的二进制。`--model-path` 是服务器程序的运行期参数；少了 `--`，cargo 会报「unrecognized subcommand/flag」。

**练习 2**：在默认构建里 `pegainfer-kimi-k2`（几百 MB 权重、Marlin INT4 内核的模型线）会被编译吗？

答案：不会。`pegainfer-kimi-k2` 在 server 的 `Cargo.toml` 中是 `optional = true` 依赖，只有 `--features kimi-k2` 才通过 `dep:pegainfer-kimi-k2` 拉入；且 workspace `default-members` 只有 `pegainfer-server`，默认闭包 = server + frontend + qwen3 + 共享 crate。

**练习 3**：README 说默认构建的「driver floor is R545 / CUDA 12.3」，预编译二进制却要求 driver 580+，为什么门槛不同？

答案：源码默认构建针对 Qwen3 一条线，驱动下限由其用到的 CUDA 运行时特性决定（R545 / CUDA 12.3）；预编译发布包捆绑了 CUDA 13 运行时（asset 名 `...cu130`），CUDA 13 要求驱动 580+，且其他模型线的新内核可能要求更高——所以两条通道的驱动门槛不同。

### 4.2 CUDA 工具链：CUDA_HOME 发现与 SM 目标探测

#### 4.2.1 概念说明

构建期有两个必须回答的问题：**CUDA Toolkit 装在哪**（去哪找 `nvcc`、头文件、cuBLAS 库），以及**为哪个 GPU 架构编译**（生成哪个 SM 的机器码）。PegaInfer 把这两件事分别放在两个 crate：

- `pegainfer-build` 提供 `CudaToolkit::discover()`：从 `CUDA_HOME`（或 `CUDA_PATH`）环境变量找到工具链根目录，兼容经典安装、conda、NVIDIA HPC SDK 三种目录布局。
- `pegainfer-kernels/build.rs` 的 `detect_sm_targets()`：决定 `-gencode` 目标。优先级是 `PEGAINFER_CUDA_SM`/`CUDA_SM` 环境变量 → `nvidia-smi` 实时探测 → 都失败则直接 panic。

`PEGAINFER_CUDA_SM` 的两个典型用途：构建机器上没有 `nvidia-smi`（比如某些容器/交叉编译场景），或者想一台构建机产出覆盖多代 GPU 的二进制（如 `120,80`）。

#### 4.2.2 核心流程

`detect_sm_targets()` 的决策树：

```text
读取 PEGAINFER_CUDA_SM（或 CUDA_SM）
 ├─ 有且含合法 token（如 "120"、"120,80"）→ 直接使用这些 SM 目标
 ├─ 有但全是非法 token → 打 warning，继续往下
 └─ 未设置 → 调 nvidia-smi 查询 compute_cap
      ├─ 查到 ≥1 个 → 使用探测结果
      └─ 查不到 → 打 warning 并 panic（构建失败）
```

`CudaToolkit::discover()` 的查找顺序：

```text
CUDA_HOME 环境变量 → CUDA_PATH 环境变量 → 默认 /usr/local/cuda
然后在根目录下找 bin/nvcc、include/、lib64|lib/、targets/<arch>-linux/...
（HPC SDK 布局额外找 math_libs 兄弟目录）
```

#### 4.2.3 源码精读

工具链发现逻辑，`CUDA_HOME` 的真正消费点：

[pegainfer-build/src/lib.rs:L65-L97](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs#L65-L97) —— `CudaToolkit::discover()` 先注册 `cargo:rerun-if-env-changed=CUDA_HOME/CUDA_PATH`（环境变量变了就重跑构建脚本），然后取 `CUDA_HOME` → `CUDA_PATH` → 兜底 `/usr/local/cuda`；若根目录下没有 `bin/nvcc` 文件则退回裸 `nvcc`（从 `$PATH` 找）。

SM 目标探测的三级优先：

[pegainfer-kernels/build.rs:L417-L447](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L417-L447) —— `detect_sm_targets()` 先解析 `PEGAINFER_CUDA_SM`/`CUDA_SM`（逗号分隔多目标），再退回 `sm_targets_from_nvidia_smi()`，两者皆失败时打出「Set PEGAINFER_CUDA_SM/CUDA_SM environment variable to override」的警告并 `panic!`。

探测结果如何变成 nvcc 参数：

[pegainfer-kernels/build.rs:L449-L455](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L449-L455) —— 每个目标生成一对参数 `-gencode arch=compute_<sm>,code=sm_<sm>`，即同时产出该架构的 PTX 与机器码。

一键开发脚本对 CUDA 的「检测不安装」策略：

[scripts/setup_dev.sh:L44-L60](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/scripts/setup_dev.sh#L44-L60) —— `detect_cuda()` 依次尝试 `CUDA_HOME`、`/usr/local/cuda`、`/usr/local/cuda-13*`、`/usr/local/cuda-12*`，再退回 `$PATH` 上的 `nvcc`；找不到就报错退出，且报错信息明确说「本脚本不安装 CUDA，请自己装好 ≥12.2 再来」。

同一段脚本还负责 FlashInfer 子模块初始化（这是新手最常见的构建失败原因）：

[scripts/setup_dev.sh:L79-L102](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/scripts/setup_dev.sh#L79-L102) —— 新 clone 不带子模块；build.rs 需要 FlashInfer 头文件，且把 FlashInfer 自带的 cccl 放在 Toolkit 自带 CCCL 之前，以保证 CUDA ≥12.2 上都有足够新的 `libcudacxx`。缺了它，CUDA <13 会报 `cuda/cmath: No such file`。

#### 4.2.4 代码实践

**实践目标**：搞清自己机器的 GPU 架构，并体验 `PEGAINFER_CUDA_SM` 对构建的影响。

**操作步骤**：

1. 查询本机 GPU 的 compute capability（即 SM 编号）：

   ```bash
   nvidia-smi --query-gpu=name,compute_cap --format=csv
   ```

2. 正常构建一次并观察 build.rs 打出的探测日志；再加上计时与并行度环境变量重跑内核编译阶段：

   ```bash
   PEGAINFER_BUILD_TIMING=1 PEGAINFER_NVCC_JOBS=8 cargo build --release -p pegainfer-kernels
   ```

3. （可选，无 GPU 机器）人为覆盖目标架构，验证探测可被绕过：

   ```bash
   PEGAINFER_CUDA_SM=120 cargo build --release -p pegainfer-kernels
   ```

**需要观察的现象**：

- 第 1 步输出中 `compute_cap` 列的值（如 `12.0` → `sm_120`）。
- 第 2 步 `PEGAINFER_BUILD_TIMING=1` 会让构建日志输出各阶段（nvcc 等）耗时；`PEGAINFER_NVCC_JOBS` 控制 nvcc 并行数。
- 第 3 步构建日志不再尝试 `nvidia-smi`，直接按 `sm_120` 生成 gencode。

**预期结果**：三次构建均成功；无 GPU 且未设 `PEGAINFER_CUDA_SM` 时，构建在 build.rs 处失败并打印「Failed to detect GPU SMs via nvidia-smi」。各阶段具体耗时数字**待本地验证**。

若没有 GPU/Toolkit，可改做**源码阅读型实践**：对照上面三个代码链接，把「环境变量 → 工具链根目录 → nvcc 路径」和「环境变量 → nvidia-smi → panic」两条链路各自画成流程图，并标出每一步失败时的错误消息出处行号。

#### 4.2.5 小练习与答案

**练习 1**：`CUDA_HOME` 指向的目录存在但里面没有 `bin/nvcc`，构建会发生什么？

答案：`CudaToolkit::from_root` 发现 `root.join("bin/nvcc")` 不是文件后，退回 `PathBuf::from("nvcc")`，即用裸命令名交给系统 `$PATH` 解析；如果 `$PATH` 里也没有 nvcc，后续编译命令会执行失败。（注意与「CUDA_HOME 指向不存在的目录」区分——后者只打一条 `cargo:warning` 仍继续用该 root。）

**练习 2**：CI 构建机上装了 CUDA Toolkit 但没插 GPU，应设什么环境变量？

答案：`PEGAINFER_CUDA_SM=<目标架构>`（可逗号分隔多个，如 `120,80`）。否则 `detect_sm_targets()` 在 `nvidia-smi` 探测失败后直接 panic。

**练习 3**：`PEGAINFER_TRITON_PYTHON` 在默认 qwen3 构建中会被读取吗？

答案：不会。Triton AOT 只在启用 `qwen35` feature 时进入构建流程；其查找顺序见 [pegainfer-kernels/build.rs:L977-L1010](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/build.rs#L977-L1010)：`PEGAINFER_TRITON_PYTHON` → workspace 根的 `.venv/bin/python` → `python3` → `python`，每个候选都要通过 `python -c "import triton"` 探测。默认构建根本不走这段代码。

### 4.3 预编译二进制：install.sh 安装通道

#### 4.3.1 概念说明

不想从源码编译时，官方提供 Qwen3-only 的预编译发布包（捆绑 CUDA 13 与 cuBLAS 运行时）。`install.sh` 是它的安装器：做环境自检、下载、校验、版本化管理安装。它适合「只想跑 Qwen3 推理服务」的用户；要开发或跑其他模型线，仍需源码构建（4.1/4.2）或 Docker（4.4）。

两条通道的硬性要求对比：

| 要求 | 预编译（install.sh） | 源码构建（默认 qwen3） |
| --- | --- | --- |
| 操作系统 | Linux x86_64 | Linux（Windows 见 README 折叠段） |
| GPU 架构 | compute capability 8.x–12.x | 由探测/覆盖决定 |
| 驱动 | 580+（CUDA 13 运行时） | R545 / CUDA 12.3 起 |
| 工具链 | 不需要（Rust/CUDA 均不需要） | Rust nightly（钉住）+ CUDA Toolkit |
| Python | 不需要 | 默认构建不需要 |
| glibc / OpenSSL | 2.35+ / 3 | 视发行版 |

#### 4.3.2 核心流程

```text
install.sh
 ├─ 1. 依赖自检：curl / sha256sum / tar / nvidia-smi 必须存在
 ├─ 2. 平台自检：Linux + x86_64
 ├─ 3. 驱动自检：driver 主版本 ≥ 580
 ├─ 4. GPU 自检：至少一块 compute_cap 8/9/10/11/12 的卡
 ├─ 5. 下载 release 的 tar.gz 与 SHA256SUMS，逐字节校验哈希
 ├─ 6. 解包，运行 bin/pegainfer --version 验证可执行且版本合法
 ├─ 7. 安装到 ~/.local/share/pegainfer/versions/v<版本>/
 │     建立 current 符号链接 + ~/.local/bin/pegainfer 入口
 └─ 8. 若 ~/.local/bin 不在 PATH，打印 export 提示
```

#### 4.3.3 源码精读

环境与硬件门槛检查：

[install.sh:L16-L34](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/install.sh#L16-L34) —— 依次校验 Linux、x86_64、`nvidia-smi` 存在、驱动主版本 ≥580（`((driver_major >= 580))`），再遍历所有 GPU 的 `compute_cap`，主版本号落在 8–12 才放行。

下载与校验和验证：

[install.sh:L53-L63](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/install.sh#L53-L63) —— 用 curl 下载 `pegainfer-qwen3-linux-x86_64-cu130.tar.gz` 与 `SHA256SUMS`，从总校验文件中筛出本资产条目后 `sha256sum --check`。

版本化安装与符号链接布局：

[install.sh:L75-L91](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/install.sh#L75-L91) —— 装入 `$install_root/versions/v<版本>`（默认 `~/.local/share/pegainfer`），原子地把 `current` 链接切到新版本（`ln -sfn` + `mv -Tf` 两步避免半更新状态），最后在 `~/.local/bin/pegainfer` 建入口链接；多版本并存、回滚只需切 `current`。

README 的预编译 Quickstart 与版本选择说明：

[README.md:L35-L55](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L35-L55) —— `curl -fsSL .../install.sh | bash` 一行安装；`PEGAINFER_VERSION` 可指定精确版本；装好后 `pegainfer --model-path models/Qwen3-4B` 直接启动。

#### 4.3.4 代码实践

**实践目标**：走查安装器的防御逻辑，理解每个 `die` 分支保护什么。

**操作步骤**（阅读型，任意机器可做）：

1. 打开 [install.sh](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/install.sh)，数出全部 `die` 调用（共 10 处左右），按「依赖缺失 / 平台不符 / 驱动太老 / GPU 不支持 / 下载校验失败 / 版本不匹配 / 安装目录异常」归类。
2. 对每一类写出：触发它的最小复现条件（例如把 `PATH` 里去掉 `sha256sum`）。

有 GPU 的机器可选做执行型验证：

```bash
curl -fsSL https://raw.githubusercontent.com/pegainfer-project/pegainfer/main/install.sh | bash
pegainfer --version
```

**需要观察的现象**：归类表覆盖了安装器所有失败出口；执行型实践中 `pegainfer --version` 输出版本号，且 `~/.local/share/pegainfer/current` 是指向 `versions/v*` 的符号链接。

**预期结果**：安装器任何一步失败都以明确的人类可读消息退出，不会留下半安装状态（临时目录用 `trap ... EXIT` 清理）。线上实际安装输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：安装器为什么先跑 `bin/pegainfer --version` 而不是直接移动文件？

答案：既验证解包出的二进制在这台机器上真的可执行（缺 glibc/OpenSSL/驱动时会在此暴露），又取出实际版本号用于命名 `versions/v<版本>` 目录，并能在用户指定 `PEGAINFER_VERSION` 时做最终一致性校验（L68-L73）。

**练习 2**：`current` 链接为什么用 `ln -sfn ... current.new` + `mv -Tf` 两步而不是直接 `ln -sfn` 到 `current`？

答案：先写临时名再原子 rename（`mv -Tf`），保证任何时刻观察者看到的 `current` 要么是旧完整版本、要么是新的，不会出现指向半建目录的中间态；这对正在运行、依赖 `current` 路径的服务进程尤其重要。

**练习 3**：你的驱动是 550，`install.sh` 会走到哪一步失败？源码构建受影响吗？

答案：预安装在驱动自检一步失败（需要 580+，因为捆绑 CUDA 13 运行时）；源码默认构建不受此限制，其驱动下限是 R545 / CUDA 12.3，因此源码通道仍然可用。

### 4.4 Docker 开发环境

#### 4.4.1 概念说明

PegaInfer 的构建依赖是一张精确的版本网：CUDA 13.2 基础镜像、钉住的 Rust nightly、Triton 3.7.1、TileLang 0.1.12、NCCL ≥ 2.30.4、protoc、clang、OpenSSL……在本机逐一对齐很痛苦。`docker/Dockerfile.dev` 把这一切固化成镜像，`docker/dev.sh` 提供三个子命令（`build` / `shell` / `run`）操作它，并处理好三件本机做麻烦的事：

1. **源码原地挂载**：仓库按原始绝对路径挂进容器（build.rs 内嵌的绝对路径、子模块检查都能工作），编译产物落回宿主机 `target/`（通过缓存卷）。
2. **编译缓存持久化**：cargo registry/git 缓存与 `target` 目录放在 `~/.cache/pegainfer-dev`，容器销毁后重建不丢；且 `target` 缓存按镜像里的 CUDA base 自动分命名空间，换基础镜像不会互相污染。
3. **硬件特殊设备透传**：GB300 NVL72 跨机架 LSA 需要的 IMEX channel 设备、无 GIN 网卡的 `EP_DISABLE_GIN` 开关等，脚本自动处理。

#### 4.4.2 核心流程

```text
docker/dev.sh build   # 用 docker/Dockerfile.dev 构建镜像（默认 CUDA 13.2 base）
docker/dev.sh shell   # 交互式容器：仓库原路径挂载 + 缓存卷 + --gpus all
docker/dev.sh run cargo build --release            # 一次性构建
docker/dev.sh run cargo build --release --features qwen35   # 用镜像里的 Triton venv
PEGAINFER_CUDA_SM=90 docker/dev.sh run cargo build --release --features glm52
PEGAINFER_MODEL_DIR=/models/Qwen3-4B docker/dev.sh shell    # 只读挂载权重
```

容器内环境已由 Dockerfile 预置：`CUDA_HOME=/usr/local/cuda`、`PEGAINFER_TRITON_PYTHON=/opt/pegainfer-venv/bin/python`、`PEGAINFER_TILELANG_PYTHON=同上`、`PEGAINFER_NCCL_ROOT=/opt/nccl`——即 4.2 讲的那些环境变量在容器里开箱即用。

#### 4.4.3 源码精读

镜像基础与构建参数：

[docker/Dockerfile.dev:L1-L16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/Dockerfile.dev#L1-L16) —— 默认 `nvidia/cuda:13.2.0-devel-ubuntu24.04`，可用 `CUDA_IMAGE` 构建参数覆盖；把所用 CUDA 镜像写入 label，供 dev.sh 做缓存命名空间。

apt 依赖与 NCCL 稳定化：

[docker/Dockerfile.dev:L18-L55](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/Dockerfile.dev#L18-L55) —— 安装 build-essential/clang/protobuf/openssl/nccl 等（注释要求与 setup_dev.sh 保持同步）；把多架构的 libnccl 符号链接集中到 `/opt/nccl`，并用一段 C 程序在构建镜像时验证 NCCL 版本码 ≥ 2.30.4。

预置环境变量与工具链 venv：

[docker/Dockerfile.dev:L57-L100](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/Dockerfile.dev#L57-L100) —— 设置 `CUDA_HOME`、NCCL、`TRITON_PTXAS_PATH` 等环境变量（L57-L61）；用 uv 建 `/opt/pegainfer-venv` 装钉版本的 Triton/TileLang 并设为 `PEGAINFER_TRITON_PYTHON`/`PEGAINFER_TILELANG_PYTHON`（L71-L89）；按 rust-toolchain.toml 里的 channel 预装 nightly（L93-L99）。

dev.sh 的挂载与缓存布局：

[docker/dev.sh:L39-L59](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/dev.sh#L39-L59) —— 从镜像 label 推导缓存命名空间；docker 参数固定 `--gpus all --ipc host --network host`、仓库原路径挂载、cargo registry/git 缓存卷、`target` 挂到缓存目录下的分命名空间路径。

环境变量与模型目录的按需转发：

[docker/dev.sh:L82-L96](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/dev.sh#L82-L96) —— `PEGAINFER_CUDA_SM` 与 `EP_DISABLE_GIN` 仅在宿主机设置了时才注入容器；`PEGAINFER_MODEL_DIR` 必须是已存在的绝对目录，以只读方式按原路径挂载。

容器工作流总览文档：

[docker/README.md:L9-L47](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/README.md#L9-L47) —— build/shell/run 用法、qwen35 与 glm52 的容器构建示例、模型目录挂载、CUDA base 覆盖与缓存命名空间说明。

#### 4.4.4 代码实践

**实践目标**：在容器里完成一次默认 Qwen3 构建，体会「工具链全预置、宿主机零配置」。

**操作步骤**（需要 Docker + NVIDIA Container Toolkit，GPU 可选）：

1. 构建镜像（一次性，较慢）：

   ```bash
   docker/dev.sh build
   ```

2. 在一次性容器里构建：

   ```bash
   docker/dev.sh run cargo build --release
   ```

3. 宿主机查看产物是否落到共享的 `target/`：

   ```bash
   ls -lh target/release/pegainfer
   ```

无 Docker 环境时改做**阅读型实践**：对照 [docker/dev.sh:L48-L59](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/dev.sh#L48-L59) 画出「宿主机路径 ↔ 容器内路径」的挂载映射表（仓库、三个缓存卷、可选模型目录、可选 git 公共目录），并解释为什么 `target` 缓存要按 CUDA base 镜像分命名空间。

**需要观察的现象**：容器内 `cargo build` 不再需要任何宿主机 Rust/CUDA 配置；第二次构建因缓存卷命中而明显变快。

**预期结果**：`target/release/pegainfer` 出现在宿主机仓库目录下。容器内构建耗时与镜像构建耗时**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么仓库要按「原始绝对路径」挂载进容器，而不是挂到 `/workspace` 之类的固定路径？

答案：见 [docker/dev.sh:L54-L58](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/dev.sh#L54-L58)——`--volume "$repo_root:$repo_root"` 让容器内外路径一致，这样宿主机 IDE、构建脚本里的绝对路径引用、以及 build.rs 对 git/子模块的检查在两边看到同一个世界；`Dockerfile.dev` 的 `WORKDIR /workspace/pegainfer` 只是默认值，实际由挂载路径接管。

**练习 2**：在容器里构建 `--features qwen35` 为什么不用像本机那样先 `uv venv` 装 Triton？

答案：Dockerfile 已把 `/opt/pegainfer-venv/bin/python`（内含钉版本的 Triton）设为 `PEGAINFER_TRITON_PYTHON`，build.rs 的 `find_triton_python()` 第一步就命中它。本机构建则需要 README 折叠段给出的三步：`uv venv` → `uv pip install triton` → `export PEGAINFER_TRITON_PYTHON=.venv/bin/python`。

**练习 3**：`docker/dev.sh shell` 与 `docker/dev.sh run ...` 的核心区别是什么？

答案：`shell` 起一个**交互式**（`-it`）命名容器默认跑 `/bin/bash`，适合反复操作；`run` 起**一次性**（`--rm`、无 `-it`）容器执行给定命令后即销毁，适合 CI 式的单命令构建。两者共享同一套挂载与缓存配置。

## 5. 综合实践

**任务**：走通「构建 → 启动 → 请求」全链路，并用证据回答一个问题——*默认 Qwen3 构建在哪个环节依赖 CUDA，在哪个环节不依赖 Python？*

**步骤**（按机器条件三选一执行路径）：

1. **准备**（所有路径通用）：

   ```bash
   git submodule update --init pegainfer-kernels/third_party/flashinfer
   git -C pegainfer-kernels/third_party/flashinfer submodule update --init 3rdparty/cccl
   ```

2. **路径 A（本机有 GPU + CUDA Toolkit）**：

   ```bash
   export CUDA_HOME=/usr/local/cuda
   time cargo run --release -- --model-path models/Qwen3-4B
   # 另开终端，权重需先下载到 models/Qwen3-4B（huggingface-cli download Qwen/Qwen3-4B --local-dir models/Qwen3-4B）
   curl -s http://localhost:8000/v1/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"models/Qwen3-4B","prompt":"The capital of France is","max_tokens":32}'
   ```

   （curl 命令取自 [README.md:L188-L196](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L188-L196)。）

3. **路径 B（有 CUDA Toolkit、无 GPU）**：用 4.1.4 的 `PEGAINFER_CUDA_SM=120 cargo build --release -p pegainfer-server` 验证可编译，记录产物大小与耗时；启动留待有 GPU 的机器。

4. **路径 C（Docker 环境）**：按 4.4.4 用 `docker/dev.sh` 完成构建。

5. **取证**：把构建日志（重点看 `pegainfer-kernels` 段）中的 nvcc 调用、SM 探测输出、以及「全程无 python 字样」三个证据分别贴进你的笔记；有 GPU 的路径 A 再补一条 curl 的 JSON 响应。

**预期结果**：路径 A 得到一个在 8000 端口响应 OpenAI 格式补全的服务；三条路径都能给出第 5 步的三个构建期证据。若你的环境三条路径都不可用，请完成 4.2.4 / 4.3.4 / 4.4.4 的阅读型实践作为替代。运行结果**待本地验证**。

## 6. 本讲小结

- 默认构建 = workspace 的 `default-members`（`pegainfer-server`）+ 默认 feature `qwen3`，依赖闭包里没有任何 Python；`--features qwen35` 等才会引入构建期 Python/Triton。
- `CUDA_HOME`（→`CUDA_PATH`→`/usr/local/cuda` 兜底）由 `pegainfer-build::CudaToolkit::discover` 消费，决定去哪找 `nvcc` 与库；SM 目标由 `detect_sm_targets()` 决定：`PEGAINFER_CUDA_SM` 覆盖 → `nvidia-smi` 探测 → 都失败则构建失败。
- 新 clone 必须初始化 FlashInfer（及其 cccl）子模块，否则 CUDA <13 上会报 `cuda/cmath: No such file`；`scripts/setup_dev.sh` 是这一切的一键版（但 CUDA Toolkit 是前提，它只检测不安装）。
- `install.sh` 提供 CUDA 13 预编译通道：环境/驱动/架构自检 → SHA256 校验 → `versions/v*` 多版本布局 + `current` 原子切换；驱动门槛（580+）高于源码构建（R545+）。
- `docker/dev.sh build|shell|run` 固化全部工具链版本，仓库按原路径挂载、编译缓存按 CUDA base 分命名空间持久化，容器内 `PEGAINFER_TRITON_PYTHON` 等变量开箱即用。
- GPU 构建永远用 `--release`；`--` 之后才是传给服务器二进制的运行期参数（如 `--model-path`）。

## 7. 下一步学习建议

- **下一讲 u1-l3（Workspace 全景）**：构建链里出现的每个 crate（frontend、core、kernels、sample……）各自的职责边界，学会「一个功能该去哪个 crate 找」。
- **u1-l4（第一个请求与 pegainfer-sim）**：不依赖 GPU 跑通前端全链路，把本讲 curl 的请求在模拟引擎上复现。
- 想深挖本讲的构建系统，直接跳读 **u4-l2（kernels 构建系统）**：nvcc 并行编译池、Triton/TileLang AOT 的完整流水线。
- 延伸阅读（仓库内文档）：[docs/subsystems/kernels/build-rs-submodule-init.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/kernels/build-rs-submodule-init.md) 讲子模块初始化细节，[docker/README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docker/README.md) 是容器开发权威文档。
