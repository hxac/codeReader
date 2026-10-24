# target_dirs：交叉编译下的架构目录名

## 1. 本讲目标

本讲只精读一个 8 行的私有函数 `target_dirs()`，但它是上一讲 `discover()` 定根之后、下一讲 `from_root()` 探测三种布局之前的必经环节。学完本讲你应该能够：

- 说出 CUDA 工具包 `targets/<dir>/` 目录结构存在的原因，以及 aarch64 工具包为什么有 `aarch64-linux` 与 `sbsa-linux` 两种子目录命名。
- 区分 `CARGO_CFG_TARGET_ARCH`（**构建目标**架构，Cargo 只为 build script 进程设置）与 `std::env::consts::ARCH`（**宿主**架构，编译期烧入）。
- 在不运行 build.rs 的情况下，推断 x86_64 与 aarch64 两种环境下 `target_dirs()` 的返回值，并理解测试模块为什么要通过它间接构造假目录树。

## 2. 前置知识

本讲承接 u1-l3（`lib.rs` 全景导览）与 u2-l1（`discover()` 的环境变量回落链）。回顾两个结论：

- `discover()` 只负责**定根**：`CUDA_HOME` → `CUDA_PATH` → `/usr/local/cuda`，随后把根交给私有的 `from_root()`。
- `from_root()` 负责**布局探测**：把一串候选目录组装出来，再交给 `existing_deduped()` 过滤去重。

本讲需要补三个背景概念：

1. **目标三元组与 target triple**：Rust 的每个编译目标用一个三元组描述，如 `x86_64-unknown-linux-gnu` 或 `aarch64-unknown-linux-gnu`，其中第一段就是架构名。
2. **目标架构 vs 宿主架构**：构建脚本（build.rs）**编译给宿主机运行**，但它服务的却是正在被编译的**目标**。交叉编译（例如在 x86_64 机器上 `cargo build --target aarch64-unknown-linux-gnu`）时两者不同；不加 `--target` 时两者恰好一致。
3. **环境变量按进程隔离**：Cargo 注入的 `CARGO_CFG_*` 变量只出现在 build script 进程的环境里，不会遗传给之后运行的测试二进制。这一点是理解「回落」分支的关键。

## 3. 本讲源码地图

| 文件 | 行段 | 作用 |
|------|------|------|
| `pegainfer-build/src/lib.rs` | 54-63 | `target_dirs()` 本体：读架构、返回候选目录名列表 |
| `pegainfer-build/src/lib.rs` | 91-121 | 调用方 `from_root()`：把候选名拼成 `targets/<dir>/include` 与 `targets/<dir>/lib` |
| `pegainfer-build/src/lib.rs` | 189-191 | 测试帮手 `target_dir()`：取候选列表的第一项 |
| `pegainfer-build/src/lib.rs` | 208-224、236-258 | `conda_layout` 与 `symlinked_dirs_dedupe` 两个消费 `target_dir()` 的测试 |
| `pegainfer-build/Cargo.toml` | 1-8 | `edition = "2024"`，影响实践任务里 `set_var` 的写法 |
| `.github/workflows/ci.yml` | 142-144 | CI 在纯 CPU runner 上跑本 crate 单测的命令 |

## 4. 核心概念与源码讲解

### 4.1 targets 目录与双命名变体

#### 4.1.1 概念说明

在经典的 Linux x86_64 安装（如 `/usr/local/cuda`）里，头文件在 `include/`、库在 `lib64/`，一眼可寻。但 CUDA 工具包还有第二种组织方式：把**与架构相关**的部分收进 `targets/<目录名>/` 下面：

- 头文件在 `targets/<目录名>/include`
- 库在 `targets/<目录名>/lib`（注意是 `lib`，不是 `lib64`）

这样设计是因为同一份工具包安装可能要覆盖多个目标平台（交叉开发），也因为它被 conda 系的拆分包大量使用——conda 安装的 CUDA 常常只有 `targets/x86_64-linux/` 这一路，没有顶层的 `include/`。

麻烦在于 `<目录名>` 对 aarch64 有**两种拼写**：`aarch64-linux` 与 `sbsa-linux`。文档注释一句话讲清了这件事，这也是本函数存在的理由：

- `sbsa-linux` 来自 SBSA（Server Base System Architecture，服务器级 ARM 基准）渠道的发行包，面向 NVIDIA Grace、Ampere Altra 这类服务器芯片；
- `aarch64-linux` 出现在 Jetson/Drive 风格的嵌入式发行包里。

（渠道归属是背景知识；仓库内的权威依据就是下面这段源码注释本身。）

#### 4.1.2 核心流程

`target_dirs()` 的输入是「当前架构」，输出是零歧义的候选名列表：

```text
arch = CARGO_CFG_TARGET_ARCH（或宿主 ARCH 回落）
if arch == "aarch64":
    返回 ["aarch64-linux", "sbsa-linux"]      # 双候选
else:
    返回 [format!("{arch}-linux")]             # 单候选，例如 "x86_64-linux"
```

用公式写，conda 风格的目录就是 \( \texttt{targets}/\langle arch\rangle\texttt{-linux} \)，只有 aarch64 特化为两个取值。

架构到候选列表的完整对照：

| 架构取值 | `target_dirs()` 返回 |
|---|---|
| `aarch64` | `["aarch64-linux", "sbsa-linux"]`（双候选） |
| `x86_64` | `["x86_64-linux"]`（单候选） |
| 其他（如 `arm`） | `["arm-linux"]`（由 `format!("{arch}-linux")` 生成，尽力而为的单候选） |

#### 4.1.3 源码精读

先看函数本体，含文档注释一共 10 行：

> [pegainfer-build/src/lib.rs:54-63](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L54-L63) — `target_dirs()` 的完整定义：文档注释说明 aarch64 双命名与宿主回落，函数体按架构返回候选目录名。

```rust
/// `targets/<dir>` names for the build target; aarch64 toolkits ship as
/// either `aarch64-linux` or `sbsa-linux`. Host arch outside build scripts.
fn target_dirs() -> Vec<String> {
    let arch =
        env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_else(|_| std::env::consts::ARCH.to_string());
    match arch.as_str() {
        "aarch64" => vec!["aarch64-linux".to_string(), "sbsa-linux".to_string()],
        arch => vec![format!("{arch}-linux")],
    }
}
```

三个阅读要点：

1. **可见性**：没有 `pub`，私有函数，仅供 crate 内部的 `from_root()` 与测试使用——它不是库的公开 API 面。
2. **match 第二个臂是绑定模式**：`arch => vec![format!("{arch}-linux")]` 里的 `arch` 不是比较，而是一个新的绑定，遮蔽了外层的 `String`，同时充当兜底分支；`{arch}` 用的是 Rust 2021 起的格式串标识符捕获。任何非 `aarch64` 的取值都走这条臂。
3. **注意 `target_dirs` 是复数**：返回的是**列表**而不是单个名字，因为 aarch64 必须两个都探。

再看调用点，候选名在这里变成真实路径：

> [pegainfer-build/src/lib.rs:99-104](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L99-L104) — `from_root()` 先放入 classic 布局的候选（`include`、`lib64`、`lib`），再把每个 targets 候选名的 `include` 与 `lib` 追加到两个列表尾部。

```rust
let mut include_dirs = vec![root.join("include")];
let mut lib_dirs = vec![root.join("lib64"), root.join("lib")];
for target in target_dirs() {
    include_dirs.push(root.join(format!("targets/{target}/include")));
    lib_dirs.push(root.join(format!("targets/{target}/lib")));
}
```

两处细节值得留意：classic 候选排在**前面**，所以同一次安装里若两套目录都存在，`header_dir()` 会优先命中顶层 `include/`；而 `targets/` 下只有 `lib`，没有 `lib64`——与 NVIDIA 实际安装布局一致。

最后看测试如何消费它：

> [pegainfer-build/src/lib.rs:189-191](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L189-L191) — 测试帮手 `target_dir()`：`target_dirs().remove(0)`，取**第一个**候选目录名。

> [pegainfer-build/src/lib.rs:208-224](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L208-L224) — `conda_layout` 测试：用 `target_dir()` 造 `targets/<候选>/include/cuda.h` 与 `targets/<候选>/lib` 的假树，断言 `header_dir("cuda.h")` 命中 targets 下的 include、`lib_dirs` 同时收录顶层 `lib` 与 targets 下的 `lib`。

> [pegainfer-build/src/lib.rs:236-258](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L236-L258) — `symlinked_dirs_dedupe` 测试（`#[cfg(unix)]`）：同样以 `target_dir()` 定键，把 `include`/`lib` 做成指向 targets 目录的符号链接，验证去重。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：理解测试为什么不硬编码 `x86_64-linux`，而要绕道 `target_dir()`。
2. **操作步骤**：
   - 打开 `lib.rs`，对照 [189-191 行](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L189-L191)，跟踪 `target_dir()` 在 `conda_layout`（213、215 行）与 `symlinked_dirs_dedupe`（240-242 行）中的每次使用。
   - 回答：同一测试里「建树」和「断言」是否用的是同一个候选名？
   - 如果你的机器装有 CUDA，可以再执行 `ls /usr/local/cuda/targets`（或 `$CUDA_HOME/targets`）看一眼真实的目录名。
3. **需要观察的现象**：每次调用 `target_dir()` 都即时读取架构，树和断言永远同键；真实安装上 x86_64 机器应看到 `x86_64-linux`，aarch64 机器应看到两种命名之一。
4. **预期结果**：测试因此**与宿主架构无关**——CI runner 换成 ARM 机器也不用改测试。`ls` 的结果随本机安装而定，**待本地验证**。
5. 本实践为纯阅读型，未执行命令，以上 `ls` 观察点同样**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 aarch64 要返回两个候选，而 x86_64 只返回一个？

**参考答案**：NVIDIA 的 aarch64 发行渠道对 `targets/` 子目录有两种命名——服务器（SBSA）渠道的 `sbsa-linux` 与 Jetson/Drive 风格的 `aarch64-linux`，代码无法预先区分，只能两个都探；x86_64 发行包统一叫 `x86_64-linux`，一个候选就够。

**练习 2**：`targets/` 候选下的库目录叫 `lib` 还是 `lib64`？

**参考答案**：`lib`。见 [lib.rs:103](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L103)，只有 classic 布局才同时探 `lib64` 与 `lib` 两个顶层候选（[lib.rs:100](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L100)）。

**练习 3**：假设某厂商发行包把目录命名为 `targets/x86_64-linux-gpu`，本函数能探到吗？

**参考答案**：不能。`format!("{arch}-linux")` 生成的是 `x86_64-linux`，与 `x86_64-linux-gpu` 不匹配，探测落空；该包需要扩展 `target_dirs()`（u3-l4 的二次开发主题）或自建带符号链接的根并用 `CUDA_HOME` 指过去。

### 4.2 CARGO_CFG_TARGET_ARCH 的来源

#### 4.2.1 概念说明

`CARGO_CFG_TARGET_ARCH` 是 Cargo **专为 build script 进程设置**的环境变量，来自 `CARGO_CFG_*` 家族：Cargo 把编译目标的所有 `cfg` 键值（`target_arch`、`target_os`、`target_env`、`unix` 等）逐个注入为 `CARGO_CFG_<大写键>`。其中 `CARGO_CFG_TARGET_ARCH` 就是 `cfg!(target_arch = "...")` 的字符串形式——**描述的是正在被编译的目标，而不是跑着 build.rs 的宿主**。

这一区分在交叉编译时是实质性的：在 x86_64 主机上执行 `cargo build --target aarch64-unknown-linux-gnu`，build.rs 进程仍运行在 x86_64 上（build script 总是为宿主编译），但它要找的 CUDA 库必须是 aarch64 的——那正好放在 `targets/aarch64-linux/lib` 里。让探测跟随 `CARGO_CFG_TARGET_ARCH` 而非宿主架构，交叉编译才能链接到正确的库。

#### 4.2.2 核心流程

```text
cargo build [--target aarch64-unknown-linux-gnu]
        │  Cargo 解析目标三元组 → cfg(target_arch = "aarch64")
        ▼
Cargo 为宿主机编译 build.rs 并启动 build script 进程，
注入 CARGO_CFG_TARGET_ARCH=aarch64
        │
        ▼
build-dependencies 里的 target_dirs() 读到 "aarch64"
        → 返回 ["aarch64-linux", "sbsa-linux"]
```

两个坐标轴要分开记：

| 变量 | 语义 | 谁设置 | 何时确定 |
|---|---|---|---|
| `CARGO_CFG_TARGET_ARCH` | 构建目标的架构 | Cargo，仅注入 build script 进程 | 每次 build script 启动时 |
| `std::env::consts::ARCH` | 当前进程的（编译目标）架构 | 无——编译期常量 | rustc 编译该二进制时烧入 |

#### 4.2.3 源码精读

> [pegainfer-build/src/lib.rs:57-58](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L57-L58) — 一行完成「优先目标架构、回落宿主架构」的取值：`env::var` 读不到时由 `unwrap_or_else` 交给 `std::env::consts::ARCH`。

```rust
let arch =
    env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_else(|_| std::env::consts::ARCH.to_string());
```

> [pegainfer-build/src/lib.rs:59-62](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L59-L62) — `match` 的第一臂特判 aarch64 双候选，第二臂用绑定模式兜底生成 `{arch}-linux` 单候选（见 4.1.3 的三个阅读要点）。

还要记住它**没有任何 `pub`**：消费方 `pegainfer-kernels` 和 `pegainfer-cupti` 的 build.rs 只能经由 `CudaToolkit::discover()` 间接享受这套架构判断，无法（也不需要）单独调用 `target_dirs()`。

#### 4.2.4 代码实践

本讲的主实践任务：用三个断言钉死三种状态下的返回值。

1. **实践目标**：验证 `CARGO_CFG_TARGET_ARCH` 未设置、`aarch64`、`x86_64` 三种状态下 `target_dirs()` 分别返回双候选、单候选、单候选。
2. **操作步骤**：在 scratch crate 里复制 `target_dirs()` 实现（或在你自己的 pegainfer-build 克隆的测试模块里仿写），加入下面的测试。由于修改环境变量是**进程级全局副作用**，三个断言放进**同一个测试**按顺序执行；本 crate 是 `edition = "2024"`（[pegainfer-build/Cargo.toml:2](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/Cargo.toml#L2)），`set_var`/`remove_var` 是 unsafe 函数，必须包在 `unsafe` 块里（edition 2021 的 scratch crate 则不需要）：

   ```rust
   // 示例代码（非项目原有代码）：三态断言 target_dirs() 的返回值。
   #[test]
   fn target_dirs_under_three_arch_states() {
       // ① 未设置：回落到宿主架构。宿主若是 aarch64，结果同样是双候选！
       unsafe { std::env::remove_var("CARGO_CFG_TARGET_ARCH") };
       let host = std::env::consts::ARCH;
       let expected = if host == "aarch64" {
           vec!["aarch64-linux".to_string(), "sbsa-linux".to_string()]
       } else {
           vec![format!("{host}-linux")]
       };
       assert_eq!(target_dirs(), expected);

       // ② aarch64：双候选形态
       unsafe { std::env::set_var("CARGO_CFG_TARGET_ARCH", "aarch64") };
       assert_eq!(
           target_dirs(),
           vec!["aarch64-linux".to_string(), "sbsa-linux".to_string()]
       );

       // ③ x86_64：单候选形态
       unsafe { std::env::set_var("CARGO_CFG_TARGET_ARCH", "x86_64") };
       assert_eq!(target_dirs(), vec!["x86_64-linux".to_string()]);
   }
   ```

   若写进 pegainfer-build 自己的测试模块，运行时加 `--test-threads=1` 防止与 `conda_layout` 等并行测试互相踩环境变量：

   ```bash
   cargo test --release -p pegainfer-build --lib -- --test-threads=1
   ```

   （CI 的等价入口见 [.github/workflows/ci.yml:142-144](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/.github/workflows/ci.yml#L142-L144)，CI 还多一个 `--locked`。）
3. **需要观察的现象**：三个断言依次通过；尤其注意 ① 里对「宿主恰好是 aarch64」的分支处理——测试的期望值必须镜像源码 `match` 的两个分支。
4. **预期结果**：断言 ② 返回两个元素、断言 ③ 返回一个元素、断言 ① 与宿主架构联动。逻辑上由源码直接推出，但本讲义编写过程未运行该测试，**待本地验证**。
5. 若拆成三个独立 `#[test]`，必须始终用 `--test-threads=1` 运行，否则环境变量的修改会与并行测试竞态。

#### 4.2.5 小练习与答案

**练习 1**：在 x86_64 主机上执行 `cargo build --target aarch64-unknown-linux-gnu`，build.rs 里 `CARGO_CFG_TARGET_ARCH` 和 `std::env::consts::ARCH` 各是什么？

**参考答案**：`CARGO_CFG_TARGET_ARCH=aarch64`（描述目标），`std::env::consts::ARCH="x86_64"`（build script 二进制本身按宿主编译）。此时 `target_dirs()` 走双候选分支。

**练习 2**：为什么本函数必须读 `CARGO_CFG_TARGET_ARCH` 而不能只用 `std::env::consts::ARCH`？

**参考答案**：交叉编译时宿主是 x86_64、目标是 aarch64，要链接的 CUDA 库在目标的 `targets/aarch64-linux/lib` 下；若跟随宿主架构就会去探 `targets/x86_64-linux`，链接到错误架构的库。

**练习 3**：`CARGO_CFG_` 前缀家族还有哪些常用成员？

**参考答案**：`CARGO_CFG_TARGET_OS`（如 `linux`）、`CARGO_CFG_TARGET_ENV`（如 `gnu`）、`CARGO_CFG_UNIX`（布尔型 cfg，设为空串表示为真）等——每个都对应一个目标 `cfg` 键，同样只注入 build script 进程。

### 4.3 非 build script 上下文的宿主架构回落

#### 4.3.1 概念说明

`CARGO_CFG_*` 变量**只出现在 build script 进程**的环境里。`cargo test` 起的测试二进制、`cargo run` 起的普通程序、文档示例，统统没有这个变量——于是 `env::var(...)` 返回 `Err`，`unwrap_or_else` 的回落分支接管，取 `std::env::consts::ARCH`：**当前进程的架构**，由 rustc 在编译该二进制时烧入的常量。文档注释里那句 "Host arch outside build scripts." 说的正是这件事。

这对测试的正确性是决定性的：`conda_layout` 等测试在测试二进制里调用 `from_root()` → `target_dirs()`，走的是宿主回落分支。CI runner（x86_64）上探 `targets/x86_64-linux`，ARM 开发机上自动变成 aarch64 的双候选——测试帮手 `target_dir()` 取第一个候选，恰好让建树与断言始终同键（见 4.1.4 的实践）。

注意一个语义细节：这个回落分支在 `discover()` 的真实使用场景里**几乎不会触发**——build.rs 里 `CARGO_CFG_TARGET_ARCH` 总是存在。它是为「库代码在 build script 之外被复用」（典型如单元测试）准备的兜底，而不是第二套探测策略。

#### 4.3.2 核心流程

```text
cargo test -p pegainfer-build --lib
   │  Cargo 只在「运行 build.rs」时注入 CARGO_CFG_*；
   │  测试二进制进程的环境里没有 CARGO_CFG_TARGET_ARCH
   ▼
target_dirs() 内 env::var("CARGO_CFG_TARGET_ARCH") → Err
   ▼  unwrap_or_else 回落
std::env::consts::ARCH（编译期烧入的宿主架构）
   ▼
x86_64 机器 → ["x86_64-linux"]；aarch64 机器 → ["aarch64-linux", "sbsa-linux"]
```

#### 4.3.3 源码精读

> [pegainfer-build/src/lib.rs:54-55](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L54-L55) — 文档注释的 "Host arch outside build scripts." 一句，就是回落分支的行为契约：build script 之外取宿主架构。

> [pegainfer-build/src/lib.rs:57-58](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L57-L58) — 回落的实现点：`unwrap_or_else(|_| std::env::consts::ARCH.to_string())`，失败路径不区分「变量缺失」与「变量为空」等具体 `VarError`，一律回落。

> [pegainfer-build/src/lib.rs:189-191](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L189-L191) — 回落分支在测试中的直接受益者：`target_dir()` 不硬编码任何目录名，使整套布局测试与宿主架构解耦。

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认 build script 进程与测试二进制进程的环境差异。
2. **操作步骤**：新建一个 scratch crate（示意如下），其 build.rs 打印两个来源的架构；再在 `tests/` 或 `src` 的单测里打印同样两项：

   ```rust
   // 示例代码（非项目原有代码）：build.rs
   fn main() {
       println!(
           "build.rs: CARGO_CFG_TARGET_ARCH={:?}, consts::ARCH={:?}",
           std::env::var("CARGO_CFG_TARGET_ARCH"),
           std::env::consts::ARCH
       );
   }
   ```

   ```rust
   // 示例代码（非项目原有代码）：tests/arch.rs
   #[test]
   fn print_arch_sources() {
       println!(
           "test:     CARGO_CFG_TARGET_ARCH={:?}, consts::ARCH={:?}",
           std::env::var("CARGO_CFG_TARGET_ARCH"),
           std::env::consts::ARCH
       );
   }
   ```

   依次运行 `cargo build`（看 build.rs 输出）与 `cargo test -- --nocapture`（看测试输出）。
3. **需要观察的现象**：build.rs 一侧 `CARGO_CFG_TARGET_ARCH` 为 `Ok("x86_64")`（与宿主一致）；测试一侧为 `Err(NotPresent)`，两者 `consts::ARCH` 相同。
4. **预期结果**：如上两行输出；若用 `--target` 交叉构建，build.rs 一侧的 `Ok` 值将变成目标架构而 `consts::ARCH` 不变。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`cargo test` 运行 pegainfer-build 的单元测试时，`CARGO_CFG_TARGET_ARCH` 在测试二进制里设置了吗？

**参考答案**：没有。它只注入 build script 进程；测试二进制里 `env::var` 返回 `Err`，`target_dirs()` 走宿主 `std::env::consts::ARCH` 回落。

**练习 2**：既然如此，为什么测试模块从不需要区分「跑在 CI 的 x86_64」还是「跑在 ARM 开发机」？

**参考答案**：因为建树与断言都经由 `target_dir()`（[lib.rs:189-191](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L189-L191)）取同一个即时计算的候选名，自动跟随宿主架构，两侧永远一致。

**练习 3**：`std::env::consts::ARCH` 是运行时检测 CPU 得到的吗？

**参考答案**：不是。它是编译期常量，rustc 按这个二进制的编译目标架构烧进程序；对 build script 和测试二进制而言该目标就是宿主。

## 5. 综合实践

**任务：为 aarch64 的双候选写一个「双命中」布局测试。**

把本讲三个最小模块串起来：设置环境变量模拟目标架构（4.2）、验证双候选确实**都被探测**（4.1）、并体会测试进程走宿主回落的处境（4.3）。

1. 在你自己的 pegainfer-build 克隆里打开 `pegainfer-build/src/lib.rs` 的 `mod tests`（这是练习性修改，请勿提交；本讲义的生成过程没有改动任何源码），仿照 `conda_layout` 新增：

   ```rust
   // 示例代码（非项目原有代码）：双候选同时存在时都被收录。
   #[test]
   fn conda_layout_aarch64_probes_both_candidates() {
       unsafe { std::env::set_var("CARGO_CFG_TARGET_ARCH", "aarch64") };
       let tree = TempTree::new();
       tree.touch("targets/aarch64-linux/include/cuda.h");
       tree.touch("targets/sbsa-linux/include/cuda.h");
       tree.mkdirs("targets/aarch64-linux/lib");
       tree.mkdirs("targets/sbsa-linux/lib");

       let tk = CudaToolkit::from_root(tree.root().to_path_buf());
       assert_eq!(tk.include_dirs.len(), 2);
       assert_eq!(tk.lib_dirs.len(), 2);
       assert_eq!(
           tk.header_dir("cuda.h"),
           Some(tree.root().join("targets/aarch64-linux/include"))
       );
   }
   ```

2. 用 `cargo test --release -p pegainfer-build --lib -- --test-threads=1` 运行（env 修改与并行测试互踩，必须单线程）。
3. 观察并解释三个现象：
   - `include_dirs.len() == 2`：两个候选目录都存在时**同时**被收录，而不是命中第一个就停；
   - `header_dir` 命中列表中**靠前**的 `aarch64-linux`——它由 [lib.rs:125-130](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L125-L130) 的 `find` 语义决定；
   - 删掉 `tree.touch("targets/aarch64-linux/include/cuda.h")` 那行再跑：`include_dirs.len()` 变 1 且 `header_dir` 改命中 `sbsa` 路径——探测**不要求第一个候选存在**。
4. 预期结果：以上断言与两种变体行为均可由源码逻辑推出；本讲义编写过程未运行该测试，**待本地验证**。

## 6. 本讲小结

- `target_dirs()` 是 `discover()`（定根）与 `from_root()`（探测布局）之间的架构适配器，把「当前架构」翻译成 `targets/` 下的候选目录名。
- aarch64 工具包有 `aarch64-linux` 与 `sbsa-linux` 两种 `targets/` 子目录命名，因此返回**双候选**；其余架构由 \( \langle arch\rangle\texttt{-linux} \) 生成**单候选**。
- `CARGO_CFG_TARGET_ARCH` 由 Cargo 仅注入 build script 进程，描述**构建目标**架构，交叉编译时与宿主不同；`std::env::consts::ARCH` 是编译期烧入的宿主架构。
- build script 之外（典型如单元测试）`CARGO_CFG_TARGET_ARCH` 不存在，`unwrap_or_else` 回落宿主架构——测试帮手 `target_dir()` 因此与宿主架构解耦。
- 候选名在 [lib.rs:101-104](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L101-L104) 被拼成 `targets/<dir>/include` 与 `targets/<dir>/lib` 追加到 classic 候选之后，随后交给 `existing_deduped()` 过滤。

## 7. 下一步学习建议

下一讲 **u2-l3《from_root：三种 CUDA 工具包布局的统一探测》** 把本讲的候选目录放进完整图景：classic、conda、HPC SDK 三种布局如何由同一套候选逻辑覆盖、`math_libs` 兄弟树如何定位、nvcc 缺失时如何回落 `$PATH`。建议带着一个问题去读：`from_root()` 若一个候选目录都找不到会怎样（可先看 [lib.rs:145-159](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L145-L159) 的 `existing_deduped` 与 `unknown_layout_yields_nothing` 测试）。
