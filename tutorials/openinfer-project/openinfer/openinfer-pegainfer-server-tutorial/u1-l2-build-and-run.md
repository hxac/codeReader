# 构建与运行：feature 矩阵、--release 与全局分配器

> 学习手册 · 单元一（入门）· 第 2 讲 · 依赖第 1 讲「项目定位与架构地图」

## 1. 本讲目标

上一讲我们建立了全局地图：`pegainfer-server` 是一个约 300 行的纯分发二进制，模型逻辑全部住在各自的模型 crate 里。本讲回答一个更具体的问题：**同一个 `src/main.rs`，为什么能编译出"只支持 Qwen3"和"支持 Kimi-K2"两种完全不同的二进制？**

学完本讲你应该能够：

1. 解释 Cargo feature 如何决定二进制内容：`default = ["qwen3"]`，其余模型线经 `--features` 拉入对应 crate；能读懂「拉入依赖」与「同时转发 crate 内部 feature」两种写法的差异。
2. 说出二进制名 `pegainfer`、包名 `pegainfer-server`、CLI 自报的版本号分别来自 `Cargo.toml` 与 `main.rs` 的哪些字段和哪一行代码。
3. 说明为什么 GPU 构建必须 `--release`、为什么非 msvc 平台用 jemallocator 做全局分配器。
4. 会用 `cargo tree -e features` 与 `--version` 验证自己的 feature 构建假设，而不是靠猜。

## 2. 前置知识

用通俗语言把四个概念先过一遍，已熟悉的读者可以跳到源码地图。

- **Cargo feature（特性）**：Cargo 的编译期开关。一个 feature 就是一个名字，打开它等价于"启用一组编译期配置"——最常见的是把某个 `optional = true` 的依赖拉进编译图。feature 只在编译时存在，运行时没有"开关"这回事：没编译进去的代码就是不存在。
- **optional dependency（可选依赖）**：在 `[dependencies]` 里声明为 `optional = true`（本 crate 经 workspace 依赖 + feature 表达成）的依赖默认**不编译**。只有当某个 feature 用 `dep:包名` 指名激活它，或用户直接 `--features 包名` 时，它才进入编译图。
- **debug 与 release 构建**：`cargo build`（默认 dev profile）不做优化、开启调试断言和整数溢出检查；`cargo build --release` 做优化并关掉这些检查。对普通 CLI 两者只差几倍速度，对每个 token 都要跑一遍调度循环的推理引擎，差距会放大到不可用。
- **全局分配器（global allocator）**：Rust 里 `Box`/`Vec`/`String` 等所有堆分配默认走"系统分配器"。用一个 `#[global_allocator]` 标注的 static，可以把整个进程的默认分配器换成别的实现——本讲的主角是 jemalloc。

构建前提（README「Build from source」一节）：使用仓库钉住的 Rust 工具链（[rust-toolchain.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/rust-toolchain.toml#L1-L4)，`nightly-2026-07-10`），装好带 `nvcc` 和 cuBLAS 的 CUDA Toolkit。默认的 Qwen3 构建**全程不需要 Python**。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L1-L52) | 全部三个最小模块的落点：`[features]` 矩阵、`[[bin]]` 与 `default-run`、jemalloc 的 target 依赖 |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L1-L60) | 与 feature 表互为镜像的 `model_lines()`、`#[global_allocator]`、clap 的 `--version` 装配 |
| [../README.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L57-L68) | 「Build from source」与「Always use --release」的官方表述 |
| [../Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L1-L3)（工作区根） | `default-members = ["pegainfer-server"]`——仓库根目录直接 `cargo run` 的原因 |
| [../rust-toolchain.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/rust-toolchain.toml#L1-L4) | 钉住的 nightly 工具链 |

## 4. 核心概念与源码讲解

### 4.1 feature 矩阵：default = qwen3，其余按需拉入

#### 4.1.1 概念说明

PegaInfer 支持多条模型线（Qwen3、Qwen3.5、Gemma 4、DeepSeek-V2-Lite、Kimi-K2、GLM-5.2、Kimi-K3……），但没有一个二进制包含全部。设计是：**每个模型 crate 都是 `pegainfer-server` 的可选依赖，一条模型线对应一个同名 feature**。

这样做的直接收益（上一讲已建立，这里从构建视角再看一遍）：

- 出厂二进制只含 Qwen3：体积小、构建期不需要 Python/Triton；
- 用户按需 `--features kimi-k2` 重建，才把 Kimi-K2 的权重加载、调度器和 Marlin INT4 内核编进来；
- 不需要的模型线的代码**根本不参与编译**，而不是"编译了但运行时不用"。

#### 4.1.2 核心流程

以 `cargo run --release --features gemma4` 为例，从命令到"二进制认识 Gemma 4"的完整链条：

```text
cargo run --release --features gemma4
  │
  ├─ 1. cargo 解析 feature：gemma4 = ["dep:pegainfer-gemma4",
  │                                  "pegainfer-gemma4/gemma4"]
  │
  ├─ 2. 可选依赖 pegainfer-gemma4 被激活 → 进入编译图
  │      同时它在模型 crate 内部的同名 feature "gemma4" 被打开
  │      （内核面归线自己的 feature 拥有）
  │
  ├─ 3. main.rs 中 #[cfg(feature = "gemma4")] 守卫的
  │      &pegainfer_gemma4::model_line::MODEL_LINE 这一行存在
  │      → 注册进 model_lines() 向量
  │
  └─ 4. 运行时 detect 才有机会认领 Gemma 4 的 config.json
```

不加 `--features` 时，第 2、3 步整体消失：`model_lines()` 向量里没有这一项，`detect` 永远不会认领 Gemma 4 的配置——这就是"被编译掉"的确切含义。

#### 4.1.3 源码精读

先看 feature 表本体：

- [Cargo.toml:L34-L48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L34-L48) — `[features]` 段。第 36 行 `default = ["qwen3"]`：不加任何参数构建时只启用 qwen3；第 35 行的注释点明设计意图「Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time」。
- 注意两种写法的差异。第 37 行 `qwen3 = ["dep:pegainfer-qwen3"]` **只拉入依赖**；第 42 行 `gemma4 = ["dep:pegainfer-gemma4", "pegainfer-gemma4/gemma4"]` 在拉入依赖之外，**还要打开模型 crate 内部的同名 feature**。从写法就能读出：gemma4（以及 glm52、k3、kimi-k2、deepseek-v2-lite、qwen35）的内核面挂在自己 crate 的 feature 后面，需要显式转发；qwen3、qwen4 拉入依赖本身就够了。
- 第 46-47 行 `qwen35` 的注释是构建期要求的重要信号：`# Qwen3.5 pulls in the Triton AOT kernels (Python + Triton at build time).` —— 想构建 qwen35，先准备好带 Triton 的 Python 环境。

可选依赖的声明在依赖段里：

- [Cargo.toml:L18-L26](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L18-L26) — 七个模型 crate（deepseek-v2-lite、gemma4、glm52、k3、kimi-k2、qwen3、qwen35、qwen4 共八个条目）全部带 `optional = true`。对照第 14-16 行的 `anyhow`、`clap`、`log`：它们没有 optional，是任何构建都必需的骨架。

代码侧的镜像是 `model_lines()`：

- [src/main.rs:L24-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L24-L43) — 每个条目前面都有 `#[cfg(feature = "...")]` 守卫。feature 关掉时该行代码不存在；又因为依赖也没编译，两侧互相印证、不会出现"引用了不存在的 crate"的编译错误。这个函数就是 feature 矩阵在源码里的唯一落点——`main.rs` 其余部分对模型线一无所知。

README 对这套机制的官方表述：

- [../README.md:L142-L144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L142-L144) — 「Only **`qwen3`** is enabled by default... Build other lines with `--features <feature>`」，并说明启动时由 `--model-path` 指向的 checkpoint 的 `config.json` 识别模型家族（检测门的细节是第 4 讲的主题）。
- [../README.md:L73-L80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L73-L80) — Qwen3.5 的构建步骤：`uv venv` + 装 triton + `PEGAINFER_TRITON_PYTHON=.venv/bin/python`，再 `cargo run --release --features qwen35`。

#### 4.1.4 代码实践

**实践目标**：不读一行模型代码，用 `cargo tree` 证明「默认构建编入了哪些线、`--features` 如何把可选依赖点亮」。

**操作步骤**（在工作区根目录执行；`cargo tree` 只做依赖解析，不编译代码，纯 CPU 环境即可）：

```bash
cd <仓库根目录>

# 1) 默认 feature 集的依赖图（含 feature 边）
cargo tree -p pegainfer-server -e features > /tmp/server-default.txt

# 2) 加 gemma4 后的依赖图
cargo tree -p pegainfer-server -e features --features gemma4 > /tmp/server-gemma4.txt

# 3) 对比两份图
diff /tmp/server-default.txt /tmp/server-gemma4.txt
```

**需要观察的现象**：

- 默认图中应出现 `pegainfer-qwen3` 节点（经由 `default` → `qwen3` → `dep:pegainfer-qwen3` 这条 feature 链），且**没有** `pegainfer-gemma4`；
- 加 `--features gemma4` 后，diff 的增量里应出现 `pegainfer-gemma4` 节点及其 `gemma4` feature 边。

**预期结果**：你能指出 optional dependency 被激活的准确位置——不是运行时判断，而是依赖图里多了一个节点。图中具体行的措辞随 cargo 版本略有差异，以「`pegainfer-gemma4` 字样是否出现」为准。**待本地验证**（本讲义撰写环境未执行 cargo）。

#### 4.1.5 小练习与答案

**练习 1**：出厂（stock）二进制为什么无法服务 Kimi-K2 的 checkpoint？用户会看到什么？

答案：`default = ["qwen3"]` 未启用 `kimi-k2`，`pegainfer-kimi-k2` 是 optional 依赖、没进编译图，`model_lines()` 里没有对应条目，运行时 `detect` 得到 `NoMatch`；由于 `kimi_k25` 属于已知家族，`feature_gate_hint` 会附加提示 rebuild with `--features kimi-k2`（提示机制的精读在第 4 讲）。

**练习 2**：`qwen35` 与 `qwen3` 的 feature 定义差在一处转发（`"pegainfer-qwen35/qwen35"`），这个转发在构建期意味着什么？

答案：它打开 `pegainfer-qwen35` crate 内部的同名 feature，而那条线挂着 Triton AOT 内核生成——所以构建 qwen35 需要带 Triton 的 Python（`PEGAINFER_TRITON_PYTHON`）；qwen3 的定义只拉依赖，因此默认构建可以做到全程无 Python。

**练习 3**：如果把 `--features kimi-k2` 拼错成 `--features kimi_k2`，会发生什么？

答案：cargo 直接报 unknown feature 错误、构建失败。feature 名是编译期契约，不存在"静默忽略"——这也是为什么 `model_lines()` 的 cfg 守卫字符串和 feature 表必须逐字符一致。

### 4.2 二进制的身份：[[bin]]、default-run 与 --version

#### 4.2.1 概念说明

三个容易混淆的名字要分开：

| 名字 | 值 | 来源 |
| --- | --- | --- |
| 包名（crate 名） | `pegainfer-server` | `Cargo.toml` 的 `[package] name` |
| 产物二进制名 | `pegainfer` | `Cargo.toml` 的 `[[bin]] name` |
| CLI 自报的家门 | `pegainfer 0.1.1` | `main.rs` 里 `clap::Command::new("pegainfer")` + 编译期版本宏 |

包名与二进制名**故意不同**：crate 在 workspace 里叫 `pegainfer-server`（职责清晰），用户敲的命令是 `pegainfer`（短、好装到 PATH 上）。`[[bin]]` 就是这次改名的落点。`default-run` 则回答"这个包有多个 bin 时 `cargo run` 选哪个"——本包目前只有一个 bin，它是防漂移的显式声明：将来若加入第二个 bin，`cargo run` 的行为不会悄悄改变。

`--version` 的输出来自两处代码的配合：名字是 `clap::Command::new("pegainfer")` 的字面量，版本号是 `env!("CARGO_PKG_VERSION")`——一个编译期宏，取的就是 `[package] version = "0.1.1"`。

#### 4.2.2 核心流程

在仓库根目录执行 `cargo run --release -- --version` 的解析链：

```text
仓库根目录（虚拟 workspace，无 [package]）
  │
  ├─ 1. cargo 需要知道运行哪个包
  │      → 根 Cargo.toml: default-members = ["pegainfer-server"]
  │
  ├─ 2. 该包内选哪个 bin
  │      → pegainfer-server/Cargo.toml: [[bin]] name = "pegainfer"（default-run 同名）
  │
  ├─ 3. 编译并运行 target/release/pegainfer --version
  │
  └─ 4. clap 打印 "<name> <version>"
         → name 来自 Command::new("pegainfer")
         → version 来自 env!("CARGO_PKG_VERSION") = "0.1.1"
```

#### 4.2.3 源码精读

- [Cargo.toml:L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L1-L11) — `[package]` 段：第 6 行 `name = "pegainfer-server"`（包名）、第 7 行 `version = "0.1.1"`（`--version` 的最终来源）、第 3 行 `default-run = "pegainfer"`；第 9-11 行 `[[bin]]` 把产物命名为 `pegainfer`、指向 `src/main.rs`。
- [src/main.rs:L152-L156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L152-L156) — `main()` 里构建 clap 命令：第 153 行 `clap::Command::new("pegainfer")` 决定 `--version`/`--help` 里显示的程序名；第 154 行 `.version(env!("CARGO_PKG_VERSION"))` 把包版本注入。clap 默认的 `--version` 输出格式就是「名字 + 空格 + 版本」。
- [../Cargo.toml:L1-L3](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L1-L3) — 工作区根是虚拟 manifest，第 2 行 `default-members = ["pegainfer-server"]`。没有这一行，根目录的 `cargo run` 会直接报错（cargo 不知道运行哪个包）；有了它，README 里根目录一键 `cargo run --release -- --model-path ...` 才成立。

#### 4.2.4 代码实践

**实践目标**：把「三个名字」各自钉到具体字段/代码行上，验证你对自己构建产物的假设。

**操作步骤**：

```bash
# 1) 无需编译即可读取包元数据（纯 CPU、秒级）
cargo metadata --no-deps --format-version 1 | jq '.packages[] | select(.name=="pegainfer-server") | {name, version, binaries: [.bins[].name]}'

# 2) 有 CUDA 环境时，真正构建并询问版本（首次构建耗时较长）
cargo run --release -- --version

# 3) 确认产物文件名
ls -la target/release/pegainfer
```

**需要观察的现象 / 预期结果**：

- 步骤 1 应返回 `name: "pegainfer-server"`、`version: "0.1.1"`、`binaries: ["pegainfer"]`——包名与 bin 名不同、版本来自 `[package]`；
- 步骤 2 预期输出一行 `pegainfer 0.1.1`（clap 的标准 version 输出格式）；
- 步骤 3 确认磁盘上的文件叫 `pegainfer` 而不是 `pegainfer-server`。

**待本地验证**（本讲义撰写环境未执行 cargo；步骤 2 需要完整 CUDA 工具链）。

#### 4.2.5 小练习与答案

**练习 1**：`--version` 输出里的字符串 `pegainfer` 来自哪一行代码？来自 `[[bin]] name` 吗？

答案：来自 [src/main.rs:L153](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L153) 的 `clap::Command::new("pegainfer")` 字面量。`[[bin]] name` 只决定产物文件名，不影响 CLI 自报的家门——两者目前一致，靠人工保持同步。

**练习 2**：为什么在仓库根目录直接 `cargo run --release -- --model-path models/Qwen3-4B` 就能启动服务，而不需要 `-p pegainfer-server`？

答案：工作区根是虚拟 manifest，[../Cargo.toml:L2](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L2) 的 `default-members = ["pegainfer-server"]` 把默认运行目标指定为本包；包内再由 `default-run = "pegainfer"` 选中唯一的 bin。

**练习 3**：把 `[package] version` 改成 `0.2.0` 并重新构建后，`--version` 输出如何变化？为什么？

答案：输出变为 `pegainfer 0.2.0`。`env!("CARGO_PKG_VERSION")` 是编译期宏，在编译时读取包版本并烧进二进制，所以必须重新编译才能看到变化。

### 4.3 为什么 GPU 构建必须 --release

#### 4.3.1 概念说明

README 的表述毫不含糊：**「Always use `--release` for GPU builds.」**（[../README.md:L68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L68)）。

直觉层面的原因：推理引擎的性能热点有两块——GPU kernel 和**主机侧的调度循环**（批组装、KV 簿记、token 拷贝、每 step 的状态机推进）。debug 构建对后者是灾难：

1. **无优化**：Rust 代码不内联、不做边界检查消除，调度循环慢一到两个数量级；
2. **调试开销**：dev profile 默认开启 `debug-assertions` 和整数溢出检查，热循环里每次索引、每次算术都多付钱；
3. GPU 每一步都在等主机喂活，主机慢 → 整条流水线慢 → 常见结局是"看起来卡死/超时"，而不是"慢一点但能跑"。

两个容易忽略的边界：

- `--release` 改变的是 **Rust 主机代码**的编译档位；CUDA 侧的 `.cu` 由 `pegainfer-kernels` 的 `build.rs` 调 `nvcc` 编译，其优化参数不走 cargo 的 profile 开关；
- 测试同样要 release：README「Development」一节的示例测试命令全部带 `--release`（[../README.md:L208-L218](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L208-L218)），"Run checks in release mode"。

#### 4.3.2 核心流程

dev 与 release 两个 profile 的关键差异（Cargo 默认值，本仓库未覆写这两个 profile）：

| 维度 | dev（默认） | release |
| --- | --- | --- |
| `opt-level` | 0（不优化） | 3 |
| `debug-assertions` | 开 | 关 |
| `overflow-checks` | 开 | 关 |
| 对引擎的意义 | 每 step 的调度/簿记代码慢 10-100 倍，GPU 等主机 | 主机侧跟得上 GPU |

#### 4.3.3 源码精读

- [../README.md:L57-L68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L57-L68) — 官方构建步骤：`export CUDA_HOME=/usr/local/cuda` 后 `cargo run --release -- --model-path models/Qwen3-4B`；第 68 行就是那句「Always use --release」。同时说明本 crate 是 server 入口、模型实现在各模型 crate。
- [../README.md:L208-L211](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L208-L211) — 单元测试也用 `cargo test --release --workspace --lib`，理由同源：含 GPU/张量逻辑的测试在 debug 下会慢到超时。
- [../rust-toolchain.toml:L1-L4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/rust-toolchain.toml#L1-L4) — 仓库钉住 `nightly-2026-07-10`（含 rustfmt、clippy）。`edition = "2024"`（[Cargo.toml:L4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L4)）也依赖较新的编译器——构建前不要随意切换工具链。

#### 4.3.4 代码实践

**实践目标**：用「编译时间」这个不依赖 GPU 的指标，直观感受两个 profile 的差异（产物优化路径完全不同）。

**操作步骤**：

```bash
# 1) 清理后各做一次完整默认构建，记录耗时
cargo clean
time cargo build            # dev profile
cargo clean
time cargo build --release  # release profile
```

两个产物分别为 `target/debug/pegainfer` 与 `target/release/pegainfer`，可用 `ls -la` 顺带对比体积。

**需要观察的现象 / 预期结果**：两个 profile 的编译时间不同（release 通常更慢，因为要做优化），产物体积差异明显；`target/debug/` 与 `target/release/` 下各有一个名为 `pegainfer` 的可执行文件。**运行性能对比需要 GPU 与模型权重，待本地验证**；在没有 CUDA 的机器上，本实践的编译步骤也可能因 kernels 依赖而失败——此时退化为纯阅读型实践：对照 4.3.2 的表格，向自己复述每个开关对调度循环的影响。

#### 4.3.5 小练习与答案

**练习 1**：debug 构建慢，具体慢在哪几层？

答案：主机侧 Rust 调度/簿记代码完全未优化（opt-level=0），叠加默认开启的 `debug-assertions` 与整数溢出检查；GPU 每步依赖主机推进，主机慢导致整条流水线拖垮。

**练习 2**：`--release` 会让 nvcc 把 CUDA kernel 编得更快吗？

答案：不会直接改变。`.cu` 的编译由 `pegainfer-kernels` 的 `build.rs` 调 nvcc 完成，优化参数由那边决定；`--release` 改变的是 Rust 代码（含 build script 自身运行）的档位。

**练习 3**：为什么仓库连单元测试命令都写 `--release`？

答案：测试同样执行引擎/张量逻辑，debug 下慢到容易超时；README「Development」一节的示例（`cargo test --release --workspace --lib` 等）是官方约定的标准跑法。

### 4.4 jemalloc 全局分配器

#### 4.4.1 概念说明

- 先给不熟悉的读者一句话：`#[global_allocator]` 是 Rust 的一个语言级挂钩——把一个实现了 `GlobalAlloc` trait 的 static 标注上去，进程内所有默认堆分配（`Vec`、`Box`、`String`……）就都走它。
- 本项目在**非 msvc 平台**把全局分配器换成了 jemalloc（[src/main.rs:L20-L22](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L20-L22)）。
- **通用工程理由**（诚实标注：仓库源码和文档都没有为这个选择写注释或基准，以下是社区通行解释，不是仓库声明的事实）：一个长期运行、多线程的 serving 进程会持续产生大量小对象分配（每请求的 token 缓冲、事件、状态结构）。glibc 的 ptmalloc2 在多线程下锁竞争与碎片更明显；jemalloc 的 per-thread arena + tcache 能降低竞争、抑制碎片。Windows 的 MSVC 工具链没有可用的 jemallocator 移植，所以用 `cfg` 门排除，回落到系统分配器。
- 一个值得记住的工程细节：这个开关**成对出现在两处**——`Cargo.toml` 的 target 依赖（控制"依赖是否编译"）和 `main.rs` 的 cfg static（控制"代码是否存在"）。两侧用同一个条件 `not(target_env = "msvc")` 保持一致。

#### 4.4.2 核心流程

```text
编译期（目标平台判定）:
  Linux/macOS（非 msvc）
    ├─ Cargo.toml: tikv-jemallocator 进入依赖图
    └─ main.rs:    static GLOBAL 存在，带 #[global_allocator]
         → 进程所有默认堆分配走 jemalloc
  Windows MSVC
    ├─ Cargo.toml: 依赖不存在
    └─ main.rs:    static 被 cfg 掉
         → 使用 MSVC 系统分配器

运行期:
  任意 Vec/String/Box 分配 → GLOBAL.alloc() → jemalloc
```

#### 4.4.3 源码精读

- [Cargo.toml:L31-L32](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L31-L32) — target 依赖段：`[target.'cfg(not(target_env = "msvc"))'.dependencies]` 下的 `tikv-jemallocator = "0.6"`。注意它与上面 `[dependencies]` 段的分界——这行只在非 msvc 目标上生效，且**不是** optional：不需要 feature 开关，平台符合就编。
- [src/main.rs:L20-L22](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L20-L22) — 与依赖段条件完全相同的 `#[cfg(not(target_env = "msvc"))]` 守卫 + `#[global_allocator]` 标注的 `static GLOBAL: tikv_jemallocator::Jemalloc`。这三行就是整个机制的全部代码；jemalloc 以库的形式被静态链入，无任何运行时配置。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：验证"两处 cfg 成对"这一结构，并推演漂移的后果。

**操作步骤**：

1. 打开上面两个链接，确认两处条件字符串逐字符相同：`not(target_env = "msvc")`；
2. 在工作区根执行反向依赖查询（只解析、不编译）：

```bash
cargo tree -p pegainfer-server -i tikv-jemallocator
```

3. 阅读并回答两个假设问题（不用真改代码）：
   - 若删掉 Cargo.toml 第 31-32 行、保留 main.rs 第 20-22 行，编译会怎样？
   - 反之，保留依赖、cfg 掉 static，又会怎样？

**需要观察的现象 / 预期结果**：

- 步骤 2 在 Linux 上应显示 `tikv-jemallocator` 是 `pegainfer-server` 的直接依赖（反向树的根），印证它是被 target 门控引入的顶层选择而非某个库的传递依赖；
- 步骤 3 的答案：前一情形 `tikv_jemallocator` 这个名字解析不到，**编译失败**（依赖不在图里）；后一情形能编译，只是留下一个没被使用的多余依赖。也就是说这对 cfg 的"同步"由编译器单向强制（代码引用依赖），反方向（依赖没代码用）不报错——这正是要靠 review 保持成对的原因。

**待本地验证**（步骤 2 的输出形式；本讲义撰写环境未执行 cargo）。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个 cfg 门要写在 Cargo.toml 和 main.rs 两个地方？

答案：Cargo 的 target-specific dependencies 只控制"依赖是否参与编译"，`#[cfg]` 只控制"这段代码是否存在"。要"非 msvc 才有 jemalloc"必须两侧同条件；漂移的后果见上面实践第 3 步——一侧是硬编译错误，另一侧是静默的死依赖。

**练习 2**：换成 jemalloc 后，进程里哪些分配走它？

答案：所有经 Rust 默认分配器的堆分配（`Vec`/`String`/`Box` 等）。本 crate 没有绕开默认分配器的用法；"显式指定其他分配器的库不受影响"是通用边界，不针对本仓库。

**练习 3**：在 Windows MSVC 构建下，运行时内存分配行为与 Linux 构建有何不同？

答案：MSVC 目标上 `tikv-jemallocator` 不进依赖图、`GLOBAL` static 被 cfg 掉，进程使用 MSVC 的系统分配器；二进制中也不含 jemalloc 代码。

## 5. 综合实践

**任务：给你的构建画一张"构建画像"。**

把本讲四个模块串成一次可复现的检查，产出半页笔记：

1. **feature 集**：执行 4.1.4 的 `cargo tree` 对比（默认 vs `--features gemma4`），在笔记里记下默认构建激活的模型线（应为 qwen3 一条）与 gemma4 带来的新节点，并用一句话解释 `dep:` 转发链。
2. **二进制身份**：执行 4.2.4 的 `cargo metadata --no-deps` 查询，抄下包名、版本、bin 名三元组；若环境允许再跑 `cargo run --release -- --version`，把实际输出与三元组互相印证，并指出输出中每个字符的来源字段/代码行。
3. **档位与分配器**：在笔记里写下两件事的"为什么"各一句（为什么必须 `--release`；为什么非 msvc 换 jemalloc——并注明后者仓库未附官方理由）。
4. **自检问题**（写进笔记末尾）：如果明天要在这台机器上服务 GLM-5.2，你需要哪条构建命令？提示：从 4.1.3 的 feature 表出发，注意 glm52 行的转发写法与它挂的 TileLang/sm_90a 构建要求（README 环境变量表）。

预期产出：一份能回答"我编的二进制里有什么、叫什么、为什么这么编"的画像笔记。所有 cargo 命令的观察结果**待本地验证**。

## 6. 本讲小结

- **feature 矩阵**：`default = ["qwen3"]`；每条模型线一个 feature，激活 optional 依赖（部分线还要转发 crate 内部同名 feature 以点亮内核面）；`model_lines()` 的 `#[cfg]` 守卫是矩阵在源码里的唯一落点。
- **二进制身份**：包名 `pegainfer-server`、产物名 `pegainfer`（`[[bin]]`）、CLI 自报 `pegainfer 0.1.1`（`clap::Command::new` 字面量 + `env!("CARGO_PKG_VERSION")`）；仓库根 `default-members` 让根目录 `cargo run` 直接可用。
- **--release**：主机侧调度循环未优化 + 调试断言开销会让 GPU 流水线拖垮，README 明示 GPU 构建与测试都必须 release；nvcc 侧优化不受 cargo profile 控制。
- **jemalloc**：非 msvc 平台经两处成对的 `cfg(not(target_env = "msvc"))`（依赖 + `#[global_allocator]` static）把全局分配器换成 jemalloc；这是通用工程选择，仓库未附专门说明。
- **验证手段**：`cargo tree -e features` 看 feature 图、`cargo metadata --no-deps` 看包身份、`--version` 看运行时自报——三者都不需要读模型代码。

## 7. 下一步学习建议

构建只是入口。下一讲（u1-l3「main.rs 全景」）将走进 `main()` 本体：`ModelLineRegistry::new` → `build_command` → 读 `config.json` → `detect` → 两级 `validate` → `serve_plan` → `spawn_blocking` 加载引擎 → 三条 serve 分支的选择。建议提前通读 [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L146-L289) 的 `main()` 函数，并留意本讲已见过的两处伏笔：`model_lines()` 如何被注册表消费、`--version` 所在的 clap 命令如何被 `build_command` 扩展。检测门（`detect` 与 `feature_gate_hint`）的完整机制则在第 4 讲展开。
