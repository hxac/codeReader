# pegainfer-cupti 是什么：四个文件搭起的 CUPTI 桥

> 本讲是《openinfer-pegainfer-cupti 学习手册》的第一讲。不需要任何 CUDA / CUPTI 前置经验；文中所有行号与永久链接均对应 commit `8886aa47`，已用只读 git 命令逐一核对。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 pegainfer-cupti 的职责边界：它只做**单区间（range）的硬件计数器采样**——把一段被命名的 GPU 工作测量成一组 `f64` 指标值；它不做吞吐基准测试，也不参与推理服务。
2. 画出从 Rust 调用方 → C++ 实现 → CUPTI 库 的完整调用链路。
3. 在仓库中找到它的全部消费方，并解释为什么它被设计成独立 crate、且不进入默认构建图。

## 2. 前置知识

### 2.1 硬件性能计数器与 CUPTI

GPU 内部除了执行 kernel 的硬件，还有一组**硬件性能计数器**：DRAM 读写字节数、L2 吞吐、tensor core 占用率、活跃 warp 比例等。它们回答「这段时间硬件到底在忙什么」——只掐秒表知道「耗时 2 ms」，却分不清是卡在访存带宽还是算力；计数器能。

CUPTI（CUDA Profiling Tools Interface）是 NVIDIA 官方提供的 C API，Nsight 系列工具的底层就基于它。它随 CUDA toolkit 一起安装，但被放在 `extras/CUPTI/` 目录，**不在**主 `include/`、`lib64/` 里——这个「藏在 extras 里」的细节直接决定了下一讲 build.rs 的探测逻辑。

### 2.2 Range Profiler：按命名区间采样

CUPTI 的 Range Profiler 子 API 允许你给一段 GPU 工作打上名字（range），CUPTI 只统计该区间内的计数器。由于一块 GPU 同时可开的计数器组有限，指标较多时 CUPTI 会把 kernel **重放**多遍、每遍采集一组——因此被测 kernel 必须可以安全地重复发射。本讲只需建立这条直觉，回放循环的精读放在 u2-l3。

### 2.3 C ABI：Rust 与 C++ 之间的「海关」

Rust 和 C++ 无法直接互调对方的对象，只能通过 **C ABI** 交换最朴素的数据：指针、整数、裸数组、函数指针。异常不能跨语言边界抛出，错误只能以「int 状态码 + 调用方提供的字符缓冲」回传。pegainfer-cupti 整个 crate 做的事，就是把一次 CUPTI 采样在这条海关线上封装干净。

### 2.4 Cargo workspace 与 feature 门控

本仓库是单仓库多 crate（workspace）：根 `Cargo.toml` 的 `members` 列出全部成员。声明为 `optional = true` 的依赖只有被某个 feature 引用时才参与编译；`[[bin]]` 的 `required-features` 让一个可执行文件只在 feature 打开时才构建。这套机制正是「CUPTI 不进默认构建图」的关键（见 4.2）。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `pegainfer-cupti/Cargo.toml` | 12 | crate 清单：**零运行时依赖**，只有两个构建依赖 |
| `pegainfer-cupti/build.rs` | 34 | 构建脚本：探测 CUDA/CUPTI、编译 C++ 源文件、输出链接指令 |
| `pegainfer-cupti/csrc/range_profiler.cpp` | 402 | C++ 实现：CUPTI 会话全流程 + 唯一的 `extern "C"` 导出入口 |
| `pegainfer-cupti/src/lib.rs` | 145 | Rust 封装：声明外部符号 + 唯一公开函数 `profile_range_with_prepare` |
| `Cargo.toml`（仓库根） | — | workspace 成员表与统一依赖声明 |
| `pegainfer-qwen3/Cargo.toml` | — | 消费方接线：optional 依赖 + `kernel-report` feature + `required-features` |
| `pegainfer-qwen3/src/bin/qwen3_kernel_report.rs` | 1500+ | 唯一 import 本 crate 的 Rust 代码（内核度量报告工具） |

辅助阅读（非源码）：`.github/workflows/ci.yml`（CI 为何跳过本 crate）、`docs/subsystems/kernels/kernel-op-reports.md` 与 `docs/models/qwen3/model-crate.md`（设计决策的团队记录）。

## 4. 核心概念与源码讲解

### 4.1 crate 职责与四文件分工

#### 4.1.1 概念说明

pegainfer-cupti 是一座「桥」：一端是 Rust 世界里想做内核度量的工具（目前只有 qwen3 的 kernel-report），另一端是 NVIDIA 的 CUPTI Range Profiler。它把「建立会话、配置指标、回放采集、解码求值」这整套繁琐流程，压缩成**一个** C ABI 函数，再在 Rust 侧包成**一个**公开函数。

它的职责边界值得先划清：

- **做**：单区间采样——给一段 GPU 工作命名，返回该区间的硬件计数器换算值（`Vec<f64>`）。
- **不做**：吞吐/延迟基准（那是 `pegainfer-bench` 与各模型 bench 的事）、多区间编排、火焰图、参与线上推理。

四个文件的分工是一眼可见的纵向切开：

| 文件 | 角色 | 语言 | 关注点 |
|---|---|---|---|
| `Cargo.toml` | 清单 | TOML | 声明「这个 crate 没有任何运行时依赖」 |
| `build.rs` | 构建脚本 | Rust | 找到 CUDA/CUPTI，把 C++ 编进静态库并接上链接 |
| `csrc/range_profiler.cpp` | 实现 | C++ | CUPTI 全流程 + `extern "C"` 入口（402 行） |
| `src/lib.rs` | 封装 | Rust | 外部符号声明 + 类型转换 + 错误合并（145 行） |

#### 4.1.2 核心流程

构建期（`cargo build` 时自动执行）：

```
build.rs
  ├─ CudaToolkit::discover()          探测 CUDA 工具链根目录
  ├─ 拼 extras/CUPTI/include 与 lib64 路径
  ├─ cc::Build 以 C++17 编译 csrc/range_profiler.cpp
  │    → 静态库 pegainfer_cupti_range_profiler
  └─ 输出 cargo:rustc-link-lib=cuda / cupti / stdc++ 等链接指令
```

运行期（消费方调用时）：

```
Rust: profile_range_with_prepare(context, dev, range, metrics, prepare, launch)
  → C ABI: pegainfer_cupti_profile_range(12 个参数)
      → CUPTI: 初始化 → 建会话 → 配置指标 → 回放（回调回到 Rust）→ 解码求值
  ← metric_values 数组（f64）+ 错误字符串
```

#### 4.1.3 源码精读

先看 crate 清单——它短到可以全文引用：

```toml
[package]
edition = "2024"
license = "Apache-2.0"
name = "pegainfer-cupti"
version = "0.1.0"

[build-dependencies]
cc = { workspace = true }
pegainfer-build = { workspace = true }

[lints]
workspace = true
```

[Cargo.toml:L1-L12](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml#L1-L12) —— crate 清单全文：注意**没有 `[dependencies]` 段**。运行期它一个 Rust 依赖都不需要，连 `cudarc` 都不依赖——CUDA 上下文由调用方以裸指针传入。

[Cargo.toml:L7-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml#L7-L9) —— 仅有的两个构建依赖：`cc` 负责「在 build.rs 里调 C++ 编译器」，`pegainfer-build` 提供共享的 CUDA 工具链探测（下一讲主角）。

[build.rs:L5-L8](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L5-L8) —— 探测工具链并拼出 CUPTI 的 extras 路径；源码注释原话是「CUPTI ships outside the main toolkit dirs in classic installs」。

```rust
build
    .cpp(true)
    .std("c++17")
    .warnings(false)
    .file(manifest_dir.join("csrc/range_profiler.cpp"))
    .includes(&toolkit.include_dirs);
if cupti_include.is_dir() {
    build.include(&cupti_include);
}
build.compile("pegainfer_cupti_range_profiler");
```

[build.rs:L10-L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L10-L20) —— 以 C++17 编译 C++ 源文件为静态库 `pegainfer_cupti_range_profiler`；Rust 侧的 `extern` 声明最终就链接到这个库里的符号。

[csrc/range_profiler.cpp:L1-L6](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L1-L6) —— C++ 侧开头即引入 CUPTI 的四个 Profiler 头文件（`cupti_profiler_host.h`、`cupti_profiler_target.h`、`cupti_range_profiler.h`、`cupti_result.h`）与 CUDA 驱动头 `cuda.h`；这些头文件正是普通安装里要 extras 路径才能找到的东西。

#### 4.1.4 代码实践

1. **实践目标**：用三条只读命令建立「这个 crate 有多小」的体感。
2. **操作步骤**：在 `pegainfer-cupti/` 目录下执行：

   ```bash
   wc -l Cargo.toml build.rs csrc/range_profiler.cpp src/lib.rs
   grep -n "dependencies" Cargo.toml
   grep -n "compile" build.rs
   ```

3. **需要观察的现象**：`wc -l` 的四个数字；`grep dependencies` 只命中 `[build-dependencies]` 而没有 `[dependencies]`；`build.rs` 里 `compile` 那行生成的静态库名。
4. **预期结果**：行数依次为 `12 / 34 / 402 / 145`（本讲环境已在 HEAD `8886aa47` 实测）；清单中无运行时依赖段；静态库名为 `pegainfer_cupti_range_profiler`。

#### 4.1.5 小练习与答案

1. **问**：为什么这个 crate 的 Rust 侧不需要 `cudarc` 之类的 CUDA 绑定？
   **答**：因为它不自己创建 CUDA 资源——`CUcontext` 由调用方创建好并以裸指针（`*mut c_void`）传入，crate 只负责「在这个现成上下文上」做采样（见 [src/lib.rs:L7](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L7) 的类型别名 `CuContext`）。
2. **问**：静态库 `pegainfer_cupti_range_profiler` 这个名字在哪里决定、在哪里使用？
   **答**：在 [build.rs:L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L20) 由 `build.compile(...)` 决定；`cc` 会自动生成等价的 `cargo:rustc-link-lib=static=pegainfer_cupti_range_profiler` 指令，把 [src/lib.rs:L11](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L11) 声明的符号 `pegainfer_cupti_profile_range` 链接进来。
3. **问**：如果把这个 crate 的 C++ 实现直接挪进某个模型 crate，最先遇到什么麻烦？
   **答**：每个引用它的 crate 都要在自己的构建脚本里重复 CUPTI 探测与链接，且 CUPTI 会进入该 crate 的默认构建图——没有 CUDA extras 的环境（例如 CI）将无法构建（对照 4.2 的 CI 说明）。

### 4.2 workspace 中的位置与依赖关系

#### 4.2.1 概念说明

pegainfer-cupti 是 openinfer workspace 的普通一员，与各模型 crate（pegainfer-qwen3、pegainfer-gemma4……）平级。它被两条「边」连接到外部：

- **构建期**：从根 workspace 继承 `cc` 与 `pegainfer-build` 两个依赖的版本。
- **运行期**：目前只有 `pegainfer-qwen3` 通过 **optional 依赖 + feature** 引用它。

设计动机在团队文档里写得非常直白：`docs/subsystems/kernels/kernel-op-reports.md` 记载「`kernel-report` feature 携带通用工具依赖与 `pegainfer-cupti`，bin 依赖该 feature，这样普通的 Qwen3 library/server 构建不会把 CUPTI 拉进默认依赖图」（[kernel-op-reports.md:L34](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L34)）。CI 也因此把它排除在 clippy 之外：注释原话「pegainfer-cupti is out: building it needs CUPTI headers the toolkit above omits」（[.github/workflows/ci.yml:L240-L243](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/.github/workflows/ci.yml#L240-L243)）。

#### 4.2.2 核心流程

依赖关系图（构建图视角）：

```
workspace 根 Cargo.toml
  ├─ members: …, "pegainfer-cupti"（第 8 行）, …
  ├─ workspace.dependencies: pegainfer-cupti = { path = "pegainfer-cupti" }
  │
  ├─▶ pegainfer-cupti        [build-deps] cc, pegainfer-build
  │       （无运行时依赖；编译产物 = 静态库 + 链接指令）
  │
  └─▶ pegainfer-qwen3        [dependencies]
          pegainfer-cupti = { workspace = true, optional = true }   ← 默认不编译
          [features] kernel-report = […, "dep:pegainfer-cupti", …]  ← 打开才编译
          [[bin]] qwen3_kernel_report  required-features = ["kernel-report"]
```

关键机制：`optional = true` + feature 里写 `dep:pegainfer-cupti`，意味着「只有启用 `kernel-report` 时才解析这条依赖」；`[[bin]]` 的 `required-features` 再保证该 bin 在 feature 关闭时干脆不参与构建。三层叠加，CUPTI 被完全挡在默认构建图之外。

#### 4.2.3 源码精读

```
[workspace]
default-members = ["pegainfer-server"]
members = [
"pegainfer-frontend",
"pegainfer-sim",
"pegainfer-server",
"pegainfer-core",
"pegainfer-cupti",
…
```

[根 Cargo.toml:L1-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L1-L9) —— workspace 成员表：`pegainfer-cupti` 在第 8 行，和各模型 crate 平级；注意 `default-members` 只有 `pegainfer-server`，根目录裸跑 `cargo build` 时连各模型 crate 都不默认构建。

```toml
pegainfer-build = { path = "pegainfer-build" }
pegainfer-core = { path = "pegainfer-core" }
pegainfer-cupti = { path = "pegainfer-cupti" }
```

[根 Cargo.toml:L122-L124](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L122-L124) —— workspace 统一依赖表中，本 crate 以 path 依赖登记，供成员用 `{ workspace = true }` 继承。

消费方的三层接线（全在 pegainfer-qwen3 清单里）：

```toml
pegainfer-cupti = { workspace = true, optional = true }   # L20：可选依赖

[features]
kernel-report = [
  "dep:comfy-table",
  "dep:hex",
  "dep:pegainfer-bench",
  "dep:pegainfer-cupti",                                   # L40
  …
]

[[bin]]
name = "qwen3_kernel_report"
path = "src/bin/qwen3_kernel_report.rs"
required-features = ["kernel-report"]                      # L56
```

[pegainfer-qwen3/Cargo.toml:L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/Cargo.toml#L20) —— optional 依赖声明：默认构建不解析。
[pegainfer-qwen3/Cargo.toml:L36-L44](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/Cargo.toml#L36-L44) —— `kernel-report` feature 通过 `dep:pegainfer-cupti` 把它拉进编译。
[pegainfer-qwen3/Cargo.toml:L53-L56](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/Cargo.toml#L53-L56) —— bin 的 `required-features`：feature 不开，这个工具二进制根本不存在。

另外，`Cargo.lock` 中也能看到本 crate 的条目；用 `git grep 'pegainfer-cupti' <commit> -- ':/'` 在整个仓库搜索，Rust 代码里的 import 只有一处（见 4.4）。

#### 4.2.4 代码实践

1. **实践目标**：亲手枚举「谁引用了 pegainfer-cupti」。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   grep -rn "pegainfer-cupti" --include=Cargo.toml .
   git grep -n "pegainfer_cupti" -- ':/' '*.rs'
   ```

3. **需要观察的现象**：第一条命令命中的文件与行号；第二条命令（下划线形式，即 Rust 标识符）命中的 `.rs` 文件。
4. **预期结果**：第一条命中根 `Cargo.toml` 两处（第 8 行 members、第 124 行 workspace 依赖）和 `pegainfer-qwen3/Cargo.toml` 两处（第 20 行、第 40 行）；第二条只命中本 crate 的三个文件与 `pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:20` 一处 import。（本讲环境已用 `git grep` 在 HEAD `8886aa47` 核对。）

#### 4.2.5 小练习与答案

1. **问**：如果删掉 `optional = true` 会发生什么？
   **答**：`pegainfer-qwen3` 的所有构建（包括默认的 library/server 构建）都会解析并编译 `pegainfer-cupti`，进而要求构建环境有 CUPTI 头文件与库——没有 extras 的环境（如 CI 的 clippy job）将直接构建失败。
2. **问**：`required-features = ["kernel-report"]` 与「让 bin 默认编译、运行时再判断」相比，好处是什么？
   **答**：问题在编译期就被挡住：feature 未启用时 bin 目标不构建，既避免无 CUPTI 环境编译失败，也避免生产二进制带上 profiler 依赖；不需要任何运行时分支。
3. **问**：为什么根 workspace 的 `workspace.dependencies` 里也要登记 `pegainfer-cupti`？
   **答**：成员用 `{ workspace = true }` 统一继承版本与来源（path），避免各 crate 自行写 path 造成漂移——这与 `cc`、`pegainfer-build` 的登记方式一致。

### 4.3 唯一公开 API `profile_range_with_prepare` 一览

#### 4.3.1 概念说明

整个 crate 对外只暴露**一个** Rust 函数：

```rust
pub unsafe fn profile_range_with_prepare<'a>(
    context: CuContext,
    device_index: usize,
    range_name: &str,
    metric_names: &[&str],
    prepare: Option<&'a mut dyn FnMut() -> Result<(), String>>,
    launch: &'a mut dyn FnMut() -> Result<(), String>,
) -> Result<Vec<f64>, CuptiProfilerError>
```

它把 C ABI 的 12 个裸参数收敛成 6 个 Rust 参数：字符串变成 `&str`/`&[&str]`、输出缓冲变成返回的 `Vec<f64>`、两个函数指针变成两个闭包（`prepare` 可选）。返回值顺序与 `metric_names` 一一对应。函数是 `unsafe` 的：`context` 必须是活的 `CUcontext`，且回调只能在该 context 上发射工作——这条契约写在文档注释里（[src/lib.rs:L79-L84](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L79-L84)）。

#### 4.3.2 核心流程

完整调用链（本讲只需认结构，细节在后两单元）：

```
pegainfer-qwen3/src/bin/qwen3_kernel_report.rs   ← Rust 调用方：构造 prepare/launch 闭包
        │
        ▼
pegainfer-cupti/src/lib.rs
   profile_range_with_prepare()                  ← 唯一公开 API（unsafe fn）
        │  CString 转换、预分配 values/error 缓冲、闭包打包进 CallbackState
        ▼  extern "C"，符号名 pegainfer_cupti_profile_range
pegainfer-cupti/csrc/range_profiler.cpp          ← C++ 实现（cc 静态库）
        │  会话装配 → 回放循环（prepare/launch 经蹦床回到 Rust 闭包）→ 解码求值
        ├──► libcupti（CUPTI Range Profiler API）
        └──► libcuda（CUDA Driver API）
        │
        ▼
   metric_values（写回 Rust 预分配的 Vec<f64>）／error 缓冲（NUL 结尾字符串）
```

C++ 侧拿到控制权后是一条直线流水线（每步一个辅助函数，精读在 u2）：

```
初始化 CUPTI（只一次）→ 切换 context → 查芯片代号 → 拿 counter availability
→ 建 Host 对象 → 把指标名编译成 config image → 建 Range 对象
→ 建 counter data 容器 → set_config → 回放循环 profile_passes
→ decode_data → evaluate 写回 metric_values
```

#### 4.3.3 源码精读

先看 C ABI 两侧的对应关系。C++ 导出（12 个参数）：

```cpp
extern "C" int pegainfer_cupti_profile_range(
    CUcontext context, size_t device_index, const char *range_name,
    const char **metric_names, size_t metric_count, PegaCuptiCallback prepare_fn,
    PegaCuptiCallback launch_fn, void *userdata, double *metric_values,
    size_t metric_value_count, char *error, size_t error_len) {
```

[csrc/range_profiler.cpp:L358-L362](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L358-L362) —— 导出函数完整签名。回调类型 `PegaCuptiCallback` 在 [csrc/range_profiler.cpp:L18](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L18) 定义为 `int (*)(void *)`——接收 userdata、返回 int 状态。

Rust 侧的镜像声明：

```rust
unsafe extern "C" {
    fn pegainfer_cupti_profile_range(
        context: CuContext,
        device_index: usize,
        range_name: *const c_char,
        metric_names: *const *const c_char,
        metric_count: usize,
        prepare_fn: CuptiCallback,
        launch_fn: CuptiCallback,
        userdata: *mut c_void,
        metric_values: *mut f64,
        metric_value_count: usize,
        error: *mut c_char,
        error_len: usize,
    ) -> c_int;
}
```

[src/lib.rs:L10-L25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L10-L25) —— Rust 2024 的 `unsafe extern` 声明块：12 个参数与 C++ 侧逐一同序对应；`CuptiCallback` 别名是 `Option<unsafe extern "C" fn(*mut c_void) -> c_int>`（[src/lib.rs:L7-L8](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L7-L8)），`Option` 恰好表达「可空函数指针」，与 C++ 侧的判空约定对齐。

参数方向一览（理解这条边界的关键）：

| # | C++ 参数 | 方向 | 含义 |
|---|---|---|---|
| 1 | `CUcontext context` | 输入 | 在哪个 CUDA 上下文测量 |
| 2 | `size_t device_index` | 输入 | 设备序号（用于查芯片代号） |
| 3 | `const char *range_name` | 输入 | 区间名 |
| 4-5 | `metric_names` / `metric_count` | 输入 | 指标名数组及其长度 |
| 6-7 | `prepare_fn` / `launch_fn` | 输入 | 准备/发射两个回调（prepare 可空） |
| 8 | `void *userdata` | 输入 | 回调携带的状态指针 |
| 9-10 | `metric_values` / `metric_value_count` | **输出** | 指标值缓冲，**调用方预分配** |
| 11-12 | `error` / `error_len` | **输出** | 错误字符串缓冲，调用方预分配 |

C++ 入口先做防御校验再干活：

```cpp
if (context == nullptr)      { throw std::runtime_error("CUDA context is null"); }
if (range_name == nullptr)   { throw std::runtime_error("range name is null"); }
if (metric_count == 0)       { throw std::runtime_error("metric list is empty"); }
if (metric_names == nullptr || metric_values == nullptr)
                              { throw std::runtime_error("metric input or output buffer is null"); }
```

[csrc/range_profiler.cpp:L363-L375](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L363-L375) —— 入口参数校验，任何一条不满足都在触碰 CUDA 之前失败。通过后是一条 18 行的直线流水线（[csrc/range_profiler.cpp:L377-L394](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L377-L394)）：`initialize_cupti_once` → `CurrentContextGuard` → `chip_name` → `counter_availability_image` → `initialize_host` → `create_config_image` → `enable_range_profiler` → `create_counter_data_image` → `set_config` → `profile_passes` → `decode_data` → `evaluate` → 成功 `return 0`。所有异常都被最外层 `catch` 压缩成写入 `error` 缓冲的字符串并 `return 1`（[csrc/range_profiler.cpp:L395-L402](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L395-L402)）——异常绝不跨过 FFI 边界。

Rust 侧收尾同样规矩：

```rust
if status == 0 {
    return Ok(values);
}
let ffi_error = unsafe { CStr::from_ptr(error.as_ptr()) }
    .to_string_lossy()
    .into_owned();
let callback_error = state.error.take();
match callback_error {
    Some(callback_error) if !ffi_error.is_empty() => Err(/* 两者拼接 */),
    Some(callback_error) => Err(/* 仅回调错误 */),
    None => Err(/* 仅 FFI 错误 */),
}
```

[src/lib.rs:L130-L145](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L130-L145) —— 状态码 0 即成功；失败时合并两条错误来源（FFI 错误缓冲 + 回调暂存的错误），三分支构造 `CuptiProfilerError`。缓冲预分配发生在 [src/lib.rs:L101-L103](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L101-L103)：`values` 长度等于指标数、错误缓冲固定 4096 字节。

#### 4.3.4 代码实践

1. **实践目标**：不看讲义，凭源码独立列出 C ABI 的 12 个参数并标明方向（输入/输出/回调）。
2. **操作步骤**：打开 [src/lib.rs:L10-L25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L10-L25) 与 [csrc/range_profiler.cpp:L358-L362](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L358-L362)，逐行对照抄写一张自己的参数表；再用 [src/lib.rs:L85-L92](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L85-L92) 标注「每个 C 参数由哪个 Rust 参数/局部变量提供」。
3. **需要观察的现象**：两侧参数顺序是否完全一致；输出缓冲（`metric_values`、`error`）在 Rust 侧来自哪些局部变量。
4. **预期结果**：12 个参数一一对应，顺序一致；`metric_values` 来自 `values.as_mut_ptr()`、`error` 来自 `error.as_mut_ptr()`（见 [src/lib.rs:L115-L128](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L115-L128) 的调用点）。纯源码阅读，无需 GPU。

#### 4.3.5 小练习与答案

1. **问**：C++ 侧有 12 个参数，Rust 公开函数却只有 6 个参数，「消失」的参数去哪了？
   **答**：`range_name`/`metric_names` 由 `&str`/`&[&str]` 经 `CString` 转换得到（[src/lib.rs:L94-L101](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L94-L101)）；`metric_count` 由切片长度推出；`metric_values`/`metric_value_count` 变成返回值 `Vec<f64>`；`error`/`error_len` 变成 `Err(CuptiProfilerError)`；`userdata` 与两个函数指针由闭包打包的 `CallbackState` 承担。
2. **问**：为什么 `prepare_fn` 在 Rust 声明里是 `Option<...>`，而 `launch_trampoline` 调用时却直接 `Some(...)`？
   **答**：`prepare` 语义上可选（不清理缓存也能测）；Rust 侧只在 `state.prepare.is_some()` 时才传蹦床函数指针（[src/lib.rs:L109-L113](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L109-L113)），C++ 侧对空指针有对应的判空逻辑。而 `launch` 是必选——没有发射就没有被测对象。
3. **问**：`error_len` 传 4096，如果错误信息超过 4095 字节会怎样？
   **答**：C++ 侧 `write_error` 用 `std::min(error_len - 1, message.size())` 截断并保证 NUL 结尾（[csrc/range_profiler.cpp:L25-L32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L25-L32)），错误信息被截断但不会越界写。

### 4.4 消费方 qwen3_kernel_report 的引用方式

#### 4.4.1 概念说明

全仓库 import 本 crate 的 Rust 代码只有一处：`pegainfer-qwen3/src/bin/qwen3_kernel_report.rs`（第 20 行）。这个工具对模型的各类 kernel（attention、GEMM 等）做逐 kernel 度量，CUPTI 采样是其中「硬件计数器」那一半。它展示了 `profile_range_with_prepare` 的标准用法模板：

- **指标清单**：一个 `const &[&str]`，NVIDIA 的指标命名形如 `gpu__time_duration.sum`、`dram__bytes.sum`。
- **prepare 闭包**：测量前清理 L2 缓存——这样量到的是「冷缓存」行为；关键是清缓存的流量**不计入**测量区间。
- **launch 闭包**：发射被测 kernel；CUPTI 多遍回放时会重复调用它。

这套「先清缓存、再在区间内只发射被测 kernel」的形态，从 crate 诞生起就是它的服务对象——团队记录写道：本 crate 最初为 attention bench 添加，「只测量 attention launch 区间，并让 bench 在 `cuptiRangeProfilerStart` 之前清 L2，使清缓存流量排除在测量区间外」（[docs/models/qwen3/model-crate.md:L353](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/models/qwen3/model-crate.md#L353)）。

#### 4.4.2 核心流程

一次 `measure_cupti_dense`（GEMM 类 kernel 的测量函数）的时序：

```
DenseCase::new + pre_measure        ← 准备权重/输入，不在测量内
L2CacheClear::new                   ← 构造清缓存辅助对象
range_name = "qd/{op}-{kind}/r{rows}"
──────────────────── 进入 profile_range_with_prepare ────────────────────
  每一遍回放：
    prepare 闭包：cache_clear.clear()     ← 在计数区间开始之前（不计数）
    launch 闭包：case.launch_once()       ← 只有这段工作被计数
──────────────────────────────────────────────────────────────────────────
返回 Vec<f64> ──cupti_metric_map──▶ BTreeMap<指标名, 数值>（存进 JSON 报告）
```

#### 4.4.3 源码精读

```rust
use pegainfer_cupti::profile_range_with_prepare;
```

[qwen3_kernel_report.rs:L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L20) —— 全仓库唯一的 import；这个 bin 由 `kernel-report` feature 门控（见 4.2）。

```rust
const CUPTI_METRICS: &[&str] = &[
    "gpu__time_duration.sum",
    "sm__cycles_elapsed.avg.per_second",
    "dram__bytes.sum",
    "dram__bytes_op_read.sum",
    "dram__bytes_op_write.sum",
    "lts__t_bytes.sum",
    "sm__throughput.avg.pct_of_peak_sustained_elapsed",
    "smsp__warps_active.avg.pct_of_peak_sustained_active",
    "sm__pipe_tensor_cycles_active.avg.pct_of_peak_sustained_elapsed",
    …
];
```

[qwen3_kernel_report.rs:L149-L162](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L149-L162) —— 共 12 个指标：耗时、频率、DRAM/L2 字节数、SM 吞吐与 warp 活跃百分比、tensor pipe 占用等——足够离线推断「访存受限还是算力受限」。

测量主体（GEMM 路径）：

```rust
let mut cache_clear = L2CacheClear::new(&case.borrow().ctx)?;
let context = case.borrow().cu_context_ptr();
let device_ordinal = case.borrow().ctx.device_ordinal;
let range_name = format!("qd/{}-{}/r{rows}", op.name, kind.label());

let mut prepare = || {
    cache_clear.clear(&clear_ctx).map_err(|err| format!("{err:#}"))
};
let mut launch = || {
    case.borrow_mut().launch_once().map_err(|err| format!("{err:#}"))
};
let values = unsafe {
    profile_range_with_prepare(
        context,
        device_ordinal,
        &range_name,
        CUPTI_METRICS,
        Some(&mut prepare),
        &mut launch,
    )?
};
Ok(cupti_metric_map(&values))
```

[qwen3_kernel_report.rs:L1087-L1113](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1087-L1113) ——（所在函数 `measure_cupti_dense` 始于 [L1079](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1079)）调用模板全景：从 case 取出裸的 `cu_context_ptr()` 与设备序号，两个 `FnMut` 闭包分别负责清 L2 与发射 kernel，错误被压成 `String` 交给封装层。文件中 [L943](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L943) 与 [L1392](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1392) 附近还有针对其他 kernel 形态的同类调用。

```rust
fn cupti_metric_map(values: &[f64]) -> BTreeMap<String, f64> {
    CUPTI_METRICS
        .iter()
        .copied()
        .zip(values.iter().copied())
        .map(|(metric, value)| (metric.to_string(), value))
        .collect()
}
```

[qwen3_kernel_report.rs:L1491-L1498](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1491-L1498) —— 返回值与指标名的对应关系在消费方用 `zip` 显式固定：`Vec<f64>` 的第 i 个值就是 `CUPTI_METRICS[i]` 的读数，随后作为原始指标写进 JSON 报告（解读放在离线分析层，不塞进运行器）。

顺带一提，工具默认开启 CUPTI，可用 `--no-cupti` 关闭（[L201](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L201) 的 `no_cupti` 标志，[L637](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L637) 汇成 `cupti` 开关）——方便只测延迟、不动计数器的场合。

#### 4.4.4 代码实践

1. **实践目标**：不运行也能「走一遍」调用模板；有 GPU 的环境再看真实输出。
2. **操作步骤**：
   - 无 GPU：对照 [L1087-L1113](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1087-L1113) 手绘 4.4.2 的时序图，并在 [L149-L162](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L149-L162) 中给每个指标标注「时间/字节/百分比」三类量纲。
   - 有 GPU + CUDA 工具链：`cargo run --release -p pegainfer-qwen3 --features kernel-report --bin qwen3_kernel_report -- --help`，观察子命令与 `--no-cupti` 等选项（**待本地验证**；本讲义环境无 GPU）。
3. **需要观察的现象**：`--help` 是否出现；`CUPTI_METRICS` 的 12 个名字能否按量纲归成三类。
4. **预期结果**：帮助文本正常打印（编译成功的前提是机器装了带 CUPTI extras 的 CUDA 工具链）；指标三类分布为 2 个时间/频率类、4 个字节类、6 个百分比类。

#### 4.4.5 小练习与答案

1. **问**：`cupti_metric_map` 为什么能直接 `zip`，顺序对应有保障吗？
   **答**：有。封装层把 `metric_names` 原序传给 C++，C++ 的 `evaluate` 按同样的顺序把每个指标的换算值写进 `metric_values`（[csrc/range_profiler.cpp:L391-L392](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp#L391-L392) 调用处参数顺序可见），因此第 i 个值对应第 i 个指标名——这正是 C ABI「按位置约定」契约的体现。
2. **问**：为什么清 L2 放在 prepare（区间外）而不是 launch（区间内）？
   **答**：清 L2 产生大量 DRAM 流量，若计入区间会把 `dram__bytes.sum` 等访存指标严重污染；放在区间外，量到的就是被测 kernel 自身的冷缓存行为（出处见 [model-crate.md:L353](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/models/qwen3/model-crate.md#L353) 的设计记录）。
3. **问**：launch 闭包为什么必须是「可重复安全执行」的？
   **答**：CUPTI 采集 12 个指标时大概率触发多遍回放，每遍都会调用 launch（重放机制的直觉见 2.2；循环精读在 u2-l3）。`launch_once` 在这里指「一次完整的测量发射」，而非「整个生命周期只调一次」。

## 5. 综合实践

**任务**：用三条命令确认本 crate 的 workspace 归属、枚举全部消费方，并写下「CUPTI 为什么不进默认构建图」的结论（5–10 行笔记）。

**步骤**：

1. 在仓库根目录执行（确认 workspace 成员身份）：

   ```bash
   cargo metadata --no-deps --format-version 1 | grep -A2 pegainfer-cupti
   ```

2. 枚举消费方：

   ```bash
   grep -rn "pegainfer-cupti" --include=Cargo.toml .
   ```

3. 打开 4.2 引用的四处清单行与 [.github/workflows/ci.yml:L240-L243](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/.github/workflows/ci.yml#L240-L243)、[kernel-op-reports.md:L34](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L34)，写笔记回答：为什么 CUPTI 依赖不放进默认构建图？

**预期结果与观察点**：

- 第 1 步：JSON 的 `workspace_members` 数组中出现 `"pegainfer-cupti"`（**待本地验证**：本讲义环境未执行 cargo）。
- 第 2 步：命中且仅命中根 `Cargo.toml`（L8、L124）与 `pegainfer-qwen3/Cargo.toml`（L20、L40）；`--include=Cargo.toml` 不会匹配 `Cargo.lock`。
- 第 3 步笔记的参考要点（供自查）：① CUPTI 头/库藏在 toolkit extras，CI 环境没有（ci.yml 注释），进默认图会令 clippy/构建失败；② 推理服务不需要 profiler，链接 `libcupti` 会扩大生产二进制的体积与运行时依赖面；③ 独立成 crate 后本体「零 Rust 依赖 + 单函数边界」，审阅成本极低；④ `optional` + feature + `required-features` 三层门控让「工具才付费」——默认构建完全无感。

## 6. 本讲小结

- pegainfer-cupti 只做一件事：把一段被命名的 GPU 工作采样成 `Vec<f64>` 硬件计数器值；吞吐基准与服务运行都不归它管。
- 四个文件纵向分工：`Cargo.toml`（零运行时依赖的清单）、`build.rs`（CUPTI 探测 + C++ 静态库 + 链接指令）、`csrc/range_profiler.cpp`（402 行 CUPTI 全流程与 C ABI 入口）、`src/lib.rs`（145 行封装）。
- 唯一公开 API 是 `profile_range_with_prepare`：6 个 Rust 参数收敛了 C ABI 的 12 个裸参数，输出缓冲变返回值，异常变字符串错误。
- 它是 workspace 第 8 号成员，唯一 Rust 消费方是 `pegainfer-qwen3` 的 `qwen3_kernel_report`，通过 `optional` 依赖 + `kernel-report` feature + `required-features` 三层门控与默认构建图隔离。
- 消费方调用模板：`CUPTI_METRICS` 指标清单 + prepare 清 L2（区间外）+ launch 发射被测 kernel，返回值经 `cupti_metric_map` 变成「指标名 → 数值」落盘。

## 7. 下一步学习建议

- 下一讲 **u1-l2《构建与链接》**：精读 `build.rs` 如何用 `pegainfer_build::CudaToolkit::discover()` 找到 CUDA、为什么 CUPTI 要从 `extras/CUPTI/{include,lib64}` 补路径、`cc` 与 `cargo:rustc-link-*` 指令如何协作。
- 之后 **u1-l3** 会真正跑一次 `qwen3_kernel_report`，对比 `--no-cupti` 前后的报告差异（需要 GPU 环境）。
- 若想直接深入语言边界，可跳到 u2-l1（C ABI 契约逐参数精读）与 u2-l2（CUPTI 会话装配七步流水线），但建议按顺序先完成 u1。
