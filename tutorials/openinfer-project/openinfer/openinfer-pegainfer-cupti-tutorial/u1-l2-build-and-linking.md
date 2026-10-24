# 构建与链接：build.rs 如何找到藏在 extras 里的 CUPTI

> **承接上一讲（u1-l1）**：你已经知道 pegainfer-cupti 是 Rust 通向 NVIDIA CUPTI Range Profiler 的一座桥，四个文件分工明确，唯一公开 API 是 `profile_range_with_prepare`。本讲回答下一个自然的问题：**那份约 400 行的 C++ 实现（`csrc/range_profiler.cpp`）是怎么被编译成库、CUPTI 的头文件和库又是怎么被找到并链接进来的？** 全部答案集中在 crate 里那份只有 34 行的 `build.rs`。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `build.rs` 在 Cargo 构建流程中的位置——它在包被编译之前运行，通过 `println!("cargo:...")` 向 Cargo 回传指令，影响后续的编译与链接。
2. 解释 CUPTI 在经典 CUDA 安装中为什么位于 `<CUDA 根目录>/extras/CUPTI/` 而不是主 `include/`、`lib64/`，以及 `build.rs` 如何用 `is_dir()` 做「存在才追加」的增量处理。
3. 逐行看懂 `cc::Build` 的配置链（`cpp` / `std` / `warnings` / `file` / `includes` / `compile`），并说出 `cuda`、`cupti`、`stdc++` 三条链接指令各自的理由。
4. 掌握 `cargo:rerun-if-changed` 的精确语义：一旦发出该指令，Cargo 默认的「包内任意文件变化都重跑构建脚本」即被关闭。
5. 说清 CI 为什么把这个 crate 排除在 clippy 之外——本质是「构建期对 CUDA/CUPTI 的硬依赖」在无 GPU 环境上的必然代价。

## 2. 前置知识

### 2.1 build.rs：Cargo 的构建脚本

`build.rs` 是 Cargo 约定的特殊文件：如果你的包根目录下有它，Cargo 会**先把它编译成可执行文件并运行**，然后才开始编译你的包。它通常用来：

- 生成 Rust 代码或头文件；
- 编译宿主机的 C/C++ 代码；
- 探测外部依赖（比如 CUDA 工具链）的位置。

构建脚本通过标准输出向 Cargo「回话」：所有以 `cargo:` 开头的行都是指令，其余行只是普通日志。本讲会见到三种指令：

| 指令 | 作用 |
|---|---|
| `cargo:rustc-link-search=native=<路径>` | 告诉 rustc：链接时把这个路径加入库搜索路径（相当于给最终链接加一个 `-L`） |
| `cargo:rustc-link-lib=<名字>` | 告诉 rustc：最终链接时链接名为 `<名字>` 的库（相当于 `-l`） |
| `cargo:rerun-if-changed=<路径>` | 告诉 Cargo：只有这个路径变化时才重新运行构建脚本 |

一个关键心智模型：**这些指令不是立刻生效的**。它们被 Cargo 收集起来，在「最终把整个 crate（以及依赖它的二进制）链接成产物」的那一步才变成 `-L` / `-l` 参数。

### 2.2 编译与链接：两条搜索路径

C/C++ 构建要过两关，每关都有自己的搜索路径：

- **编译关（头文件）**：`#include <cupti.h>` 能否找到，取决于 `-I` 指定的头文件搜索路径（以及编译器的默认路径）。
- **链接关（库文件）**：`-lcupti` 要在 `-L` 指定的库搜索路径里找到 `libcupti.so` 或 `libcupti.a`（以及系统默认路径）。

pegainfer-cupti 的设计哲学是**错误前置**：CUDA/CUPTI 缺失时，让构建在编译期或链接期就失败，而不是把问题拖到运行期。三个阶段对应的失败形式：

| 阶段 | 缺什么 | 典型报错（示意） |
|---|---|---|
| 编译期 | 找不到 CUPTI 头文件 | `fatal error: cupti_profiler_target.h: No such file or directory` |
| 链接期 | 找不到 `libcupti` | `cannot find -lcupti` |
| 运行期 | （本项目设计上不允许漏到这里） | —— |

### 2.3 cc crate：build-dependency 里的常客

[cc](https://crates.io/crates/cc) 是 Rust 生态里最常用的构建依赖之一：它把「找到系统 C/C++ 编译器、拼参数、调用」包装成了声明式的 `cc::Build` API。本讲的 `build.rs` 用它把一份 C++ 源文件编成**静态库**。特别注意 `compile(name)` 这一步会自动做两件事：

1. 调用 C++ 编译器把所有 `.file(...)` 登记的源文件编成 `lib<name>.a`；
2. 自动向 Cargo 发出 `cargo:rustc-link-lib=static=<name>` 与指向产物目录的 `cargo:rustc-link-search` 指令。

也就是说，cc 负责「我自己编出来的库」的链接声明；**外部系统库**（CUDA、CUPTI、libstdc++）则要 build.rs 自己用 `cargo:rustc-link-lib` 声明——这是 4.3 节的主角。

### 2.4 CUPTI 是什么、装在哪

CUPTI（CUDA Profiling Tools Interface）是 NVIDIA 官方的性能分析接口层：读硬件性能计数器、做 kernel 时间线插桩都靠它。它与 nvcc、cuBLAS 这类「跑程序」的组件不同，属于「观测程序」的组件。因此在经典的 CUDA 安装中，它不在主 `include/`、`lib64/` 里，而在：

```
<CUDA 根目录>/extras/CUPTI/include   ← 头文件（如 cupti.h、cupti_profiler_target.h）
<CUDA 根目录>/extras/CUPTI/lib64     ← 库文件（libcupti.so）
```

`build.rs` 第 6 行的注释说的就是这件事，原文是：`// CUPTI ships outside the main toolkit dirs in classic installs.`（CUPTI 在经典安装中不随主工具链目录发布。）另外，各发行版也可能通过包管理器单独安装 CUPTI（装进系统头文件/库目录），所以探测逻辑必须容忍「extras 下没有」的情况——这正是 4.2 节 `is_dir()` 分支存在的原因。

### 2.5 workspace 依赖与本 crate 的「零运行时依赖」

看一眼 Cargo.toml 就能确认上一讲的结论：这个包**没有任何 `[dependencies]`**，只有 `[build-dependencies]`，且两条都是 `{ workspace = true }`——版本与配置由仓库根的 workspace 统一管理，本 crate 不自带版本号。`[lints] workspace = true` 表示它接受 workspace 统一的 lint 配置（这一点在 4.4 节还会用到）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用它 |
|---|---|---|
| `pegainfer-cupti/build.rs` | 本讲主角：34 行构建脚本 | 全文逐行精读 |
| `pegainfer-cupti/Cargo.toml` | 包定义 | 只看 build-dependencies 与 lints 两节 |
| `pegainfer-cupti/csrc/range_profiler.cpp` | 被 build.rs 编译的 C++ 源文件 | 本讲只把它当「编译输入」；其内部实现留给单元 2 |
| `pegainfer-build/src/lib.rs` | `CudaToolkit` 探测 API 的定义处 | 通过 build.rs 的调用点反推其公开接口；定义行号本讲标注待确认 |
| `.github/workflows/ci.yml` | CI 工作流 | 讨论对本 crate 的 clippy 排除；具体行号本讲标注待确认 |
| `pegainfer-cupti/src/lib.rs` | Rust 侧封装（下一站） | 本讲只在结尾指路，不深入 |

> 诚实声明：本讲的撰写环境只能读取 `pegainfer-cupti/` 目录内的文件，`pegainfer-build/src/lib.rs` 与 `.github/workflows/ci.yml` 无法直接读取。凡涉及这两个文件的内容，本文只陈述能从 crate 内源码**确证**的部分，内部行号一律标注「待确认」，并给出读者自行验证的命令。

## 4. 核心概念与源码讲解

本讲的四个最小模块：

| 模块 | 解决的问题 |
|---|---|
| 4.1 CudaToolkit::discover 与 root/include_dirs | CUDA 装在哪？谁来统一回答？ |
| 4.2 CUPTI 头文件与 lib64 的 extras 路径处理 | CUPTI 藏在 extras，怎么找到又怎么容错？ |
| 4.3 cc 编译与链接指令输出 | C++ 源文件怎么变成静态库，链接清单怎么拼出来？ |
| 4.4 CI 排除该 crate 的原因 | 构建期硬依赖对 CI 意味着什么？ |

### 4.1 CudaToolkit::discover 与 root/include_dirs：先回答「CUDA 装在哪」

#### 4.1.1 概念说明

任何想引用 CUDA 头文件的原生代码构建，都要先回答同一个问题：**这台机器的 CUDA 工具链根目录在哪？** 答案并不唯一——可能是 `/usr/local/cuda` 软链接，可能是 `/usr/local/cuda-12.4`，可能是包管理器装的散件，也可能只存在于某个自定义前缀里；`nvcc` 可能在 `PATH` 里也可能不在。

openinfer workspace 的做法是把「找 CUDA」这件事抽成一个共享 crate：`pegainfer-build`，对外提供 `CudaToolkit` 类型。这样「CUDA 在哪」在整个仓库里只有一份答案，修一处、处处生效——这就是「单一事实来源」在构建系统里的体现（u1-l1 的实践已经让你用 `grep -rn "pegainfer-cupti" --include=Cargo.toml` 看过谁在引用谁；对 `pegainfer-build` 可以用同样的办法列出它的消费方）。

#### 4.1.2 核心流程

从 build.rs 的视角，探测与消费只有五步：

```
Cargo 启动 build.rs（注入 CARGO_MANIFEST_DIR 环境变量）
    │
    ▼
CudaToolkit::discover()            # 定位 CUDA 工具链，返回 toolkit
    │
    ├─ toolkit.root                # 工具链根目录（PathBuf），4.2 节用它拼 extras 路径
    ├─ toolkit.include_dirs        # 主工具链头文件目录集合 → 4.3 节喂给 cc
    └─ toolkit.link_search()       # 发出主工具链的 cargo:rustc-link-search 指令
```

#### 4.1.3 源码精读

build.rs 的开头 5 行（L4-L8）完成全部探测与路径准备：

> [pegainfer-cupti/build.rs:L4-L8](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L4-L8)
> 这 5 行做了三件事：从 `CARGO_MANIFEST_DIR` 拿到包根目录、调用 `CudaToolkit::discover()` 探测工具链、再用 `toolkit.root` 拼出 CUPTI 的两个 extras 路径。

```rust
let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
let toolkit = pegainfer_build::CudaToolkit::discover();
// CUPTI ships outside the main toolkit dirs in classic installs.
let cupti_include = toolkit.root.join("extras/CUPTI/include");
let cupti_lib64 = toolkit.root.join("extras/CUPTI/lib64");
```

三个细节：

- **L4 用 `CARGO_MANIFEST_DIR` 而不是相对路径**：Cargo 保证这个环境变量指向包根目录的绝对路径。build.rs 运行时的工作目录并不总是包根，用相对路径定位 `csrc/` 不可靠；L15 的 `.file(manifest_dir.join("csrc/range_profiler.cpp"))` 依赖这里的绝对化。
- **L5 是本 crate 与探测逻辑的唯一耦合点**：`discover()` 是无参关联函数，返回的 `toolkit` 随后在三处被消费——L7/L8 读 `.root`，L16 读 `.include_dirs`，L22 调 `.link_search()`。
- **L7/L8 的 `join` 只负责拼路径**，此刻没有任何 IO 发生；「这个路径存不存在」的判断留给 4.2 节的 `is_dir()`。

`toolkit` 的另外两个消费点：

> [pegainfer-cupti/build.rs:L16](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L16)
> 把主工具链的头文件目录集合整体喂给 cc——这是 `-I` 参数的来源之一。

> [pegainfer-cupti/build.rs:L22](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L22)
> 让探测逻辑自己发出主工具链的库搜索路径指令——这是 `-L` 参数的来源之一。

关于定义处 [pegainfer-build/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs)（内部行号**待确认**，本讲环境无法读取该文件），从上述调用点可以确证它的公开接口形状：

| 接口 | 从哪一行确证 | 推断的类型形状 |
|---|---|---|
| `CudaToolkit::discover()` | build.rs L5 | 关联函数，返回 `CudaToolkit` |
| `.root` | build.rs L7/L8 | `PathBuf`（能调 `join`） |
| `.include_dirs` | build.rs L16 | 能以 `&` 传入 `cc::Build::includes` 的目录集合（cc 要求 `IntoIterator<Item = AsRef<Path>>`，典型形状是 `Vec<PathBuf>`） |
| `.link_search()` | build.rs L22 | 无返回值方法；副作用是向标准输出发出 `cargo:rustc-link-search` 指令 |

至于 `discover()` 内部按什么顺序找（环境变量？`which nvcc`？固定路径？）、找不到时是 panic 还是别的行为——**待确认**，请用 4.1.4 的实践亲自验证。

#### 4.1.4 代码实践

**实践目标**：把「探测」从一个黑盒函数变成你机器上的具体事实。

**操作步骤**（有 CUDA 工具链的机器）：

```bash
# 1) CUDA 根目录在哪？两问：
ls -d /usr/local/cuda* 2>/dev/null
which nvcc

# 2) 对照 discover() 的结果：跑一次 -v 构建（详见第 5 节综合实践），
#    在编译命令行里找到 -I/usr/local/cuda/include 之类的参数
```

**无 CUDA 的机器**：跳过命令，改画一张「build.rs 五步流程图」（对照 4.1.2），并在 `toolkit.root` 旁边标注你机器上的候选路径（如果装过 CUDA）。

**需要观察的现象**：cc 编译命令里的主工具链 `-I` 与第 1 步 `ls` 结果的对应关系。

**预期结果**：`-I` 参数来自 `toolkit.include_dirs`，而不是 build.rs 里写死的任何路径。

**待本地验证**：本讲撰写环境无 CUDA 工具链，以上现象未实际运行验证。

#### 4.1.5 小练习与答案

**练习 1**：`CudaToolkit::discover()` 在 build.rs 里被调用了几次？它的结果被哪几行消费？
**答案**：恰好一次（L5）——探测只做一遍、结果处处复用。消费点共三处：L7/L8（`.root`）、L16（`.include_dirs`）、L22（`.link_search()`）。

**练习 2**：为什么探测逻辑放在共享的 `pegainfer-build`，而不是每个需要的 crate 复制一份？
**答案**：多个 crate 都要回答「CUDA 在哪」。集中一份可以保证口径一致（同样的环境变量、同样的优先级），修一处所有消费方受益；各写一份则会逐渐漂移。

**练习 3**：`toolkit.root` 和 `toolkit.include_dirs` 分别服务于 `-I` 还是 `-L`？
**答案**：`include_dirs` 直接服务 `-I`（L16，喂给 cc）；`root` 本身既不是 `-I` 也不是 `-L`，它是「拼出更多路径的原点」——本讲里它被用来拼 CUPTI 的 extras include 与 lib64 路径；主工具链的 `-L` 由 `link_search()`（L22）负责。

### 4.2 CUPTI 头文件与 lib64 的 extras 路径处理

#### 4.2.1 概念说明

经典安装的 CUDA 工具链目录大致长这样（**示意图**，具体条目随 CUDA 版本与安装形态变化）：

```
/usr/local/cuda/                 ← toolkit.root 指向这里
├── bin/                         ← nvcc 等
├── include/                     ← cuda.h、cublas_v2.h 等主头文件
├── lib64/                       ← 主库
└── extras/
    └── CUPTI/
        ├── include/             ← CUPTI 头文件
        └── lib64/               ← libcupti
```

CUPTI 被放在 `extras/` 下，是因为它**不参与程序的编译与运行**，只在「分析程序」时才需要——工具链把「跑起来必需」与「观测用」分开放。这带来一个构建上的麻烦：光知道 `toolkit.root` 还不够，还得知道 CUPTI 另有藏身处；而且**不是所有安装都有这个目录**（最小安装，或发行版把 CUPTI 单独装进系统目录的形态）。所以 build.rs 的策略是：extras 路径「**存在才追加，不存在就交给系统默认搜索路径**」。

#### 4.2.2 核心流程

```
cupti_include = root + "extras/CUPTI/include"
cupti_lib64   = root + "extras/CUPTI/lib64"

编译配置阶段：
    if cupti_include.is_dir()  → cc 追加这条 -I                    （L17-L19）
链接指令阶段：
    if cupti_lib64.is_dir()    → 发出 cargo:rustc-link-search=...   （L23-L25）

两个 if 都不命中（CUPTI 不在 extras）：
    → 不追加任何东西，编译器/链接器回落到系统默认搜索路径
    → 系统里装了 CUPTI（如包管理器形态）→ 一切正常
    → 系统里根本没有 CUPTI → 编译期（找不到头文件）或链接期（找不到库）失败
```

注意最后一点：**失败被明确地留在构建期**。这是有意设计——一个引用了 CUPTI 符号的二进制如果到运行时才发现缺库，排查成本高得多。

#### 4.2.3 源码精读

两个路径的定义已在 4.1.3 引用过（L7-L8）；这里看两处「条件追加」：

> [pegainfer-cupti/build.rs:L17-L19](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L17-L19)
> 编译配置阶段：只有当 extras 下的 CUPTI include 目录真实存在时，才把它追加进 cc 的头文件搜索路径。

```rust
if cupti_include.is_dir() {
    build.include(&cupti_include);
}
```

> [pegainfer-cupti/build.rs:L23-L25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L23-L25)
> 链接指令阶段：同理，只有 extras 下的 CUPTI lib64 真实存在时，才向 Cargo 发出对应的库搜索路径指令。

```rust
if cupti_lib64.is_dir() {
    println!("cargo:rustc-link-search=native={}", cupti_lib64.display());
}
```

两个 if 语义完全对称：**一边管 `-I`，一边管 `-L`，都走「存在才追加」**。L22 的 `toolkit.link_search()` 先行发出主工具链的 `-L`，extras 这一条是叠加在它之后的增量。

顺带注意两边时序的差别：include 这边是「配置进 cc 对象、稍后统一生效」；link-search 这边是「立刻打印 cargo 指令」。时序不同，但都发生在编译 `range_profiler.cpp` 与链接最终产物之前，谁也不会迟到。

#### 4.2.4 代码实践

**实践目标**：确认你这台机器上 CUPTI 处于三种形态中的哪一种。

**操作步骤**：

```bash
CUDA_ROOT="${CUDA_HOME:-/usr/local/cuda}"
# 形态 A：经典安装，CUPTI 在 extras
ls "$CUDA_ROOT/extras/CUPTI/include" 2>/dev/null && echo "形态 A: extras"
# 形态 B：包管理器装进了系统目录
ls /usr/include/cupti.h 2>/dev/null && echo "形态 B: 系统目录"
# 两个命令都没输出 → 形态 C：没装
```

**需要观察的现象 / 预期结果**：

| 形态 | 观察到的 | 对 build.rs 的影响 |
|---|---|---|
| A | extras 目录存在 | 两个 if 都命中，追加 `-I` 与 `-L` |
| B | 系统目录里有 cupti 头文件 | 两个 if 都不命中，靠编译器/链接器默认路径 |
| C | 两处皆无 | 构建在编译期或链接期失败（错误前置） |

**待本地验证**：本讲撰写环境无 CUDA，三种形态未逐一实测。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `is_dir()` 探测，而不是无条件把 extras 路径写进编译/链接参数？
**答案**：CUPTI 不是每份 CUDA 安装都随附、也不一定装在 extras。无条件追加会把「不存在组件」伪装成「已配置」；条件追加把「有没有」变成构建脚本里的显式分支，缺失时自然回落到系统默认路径，兼容包管理器安装形态。

**练习 2**：CUPTI 完全没装时，错误以什么形式、在哪个阶段出现？
**答案**：若头文件无处可寻，编译期 `#include` 失败；若头文件能从别处找到但库没有，则链接期 `cannot find -lcupti`。两种都在构建期，不会拖到运行期。

**练习 3**：假如未来 NVIDIA 把 CUPTI 挪回主 `lib64/`，这份 build.rs 要改吗？
**答案**：不用改。`is_dir()` 不成立就跳过追加，而主 `lib64` 的搜索路径已由 `toolkit.link_search()`（L22）覆盖，链接器照样能找到 `libcupti`。

### 4.3 cc 编译与链接指令输出：从一份 .cpp 到一张链接清单

#### 4.3.1 概念说明

`cc::Build` 是「编译作业的声明式描述」：先链式登记属性，最后一次 `compile()` 收尾。收尾时它做两件事——把登记的源文件编成**静态库**，并自动发出链接这个静态库所需的 cargo 指令（见 2.3 节）。但 cc 只管「自己编的东西」；**外部系统库**（cuda、cupti、stdc++）必须由 build.rs 自己用 `cargo:rustc-link-lib` 一条条声明。本模块把这两段拼成一张完整的「链接清单」。

#### 4.3.2 核心流程

build.rs 的 `main` 一共四段，一气呵成：

```
① 探测准备   L4-L8    manifest_dir、toolkit、两个 extras 路径（4.1/4.2 已讲）
② cc 编译    L10-L20  配置并编译 range_profiler.cpp → libpegainfer_cupti_range_profiler.a
③ 链接指令   L22-L31  主 -L、extras -L、三条 -l
④ 增量声明   L33      rerun-if-changed
```

最终 rustc 在链接时收到的库相关参数（**示意**，实际拼写以你机器上 `-v` 输出为准）：

```
-L <主工具链 lib64>                  ← toolkit.link_search()（L22）
-L <extras/CUPTI/lib64>              ← L24，存在时
-l pegainfer_cupti_range_profiler    ← cc 自动发出（静态库本体）
-l cuda                              ← L27
-l cupti                             ← L28
-l stdc++                            ← L30，非 Windows
```

#### 4.3.3 源码精读

cc 配置链：

> [pegainfer-cupti/build.rs:L10-L20](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L10-L20)
> 用 cc 把 `csrc/range_profiler.cpp` 编成静态库：开 C++ 模式、锁 C++17、关警告、登记唯一的源文件、并入主工具链头文件路径，最后在 L20 收尾。

```rust
let mut build = cc::Build::new();
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

逐项理由：

- `.cpp(true)`：使用 C++ 编译器驱动（g++/clang++），而非 C 编译器。
- `.std("c++17")`：实现代码按 C++17 编写，CUPTI 的 profiler 系列头文件与实现用到的语言特性以此为前提。
- `.warnings(false)`：相当于向编译器传 `-w`，屏蔽这份自带 C++ 文件的编译警告——它不参与日常 Rust lint 反馈环，警告只会制造噪音。
- `.file(...)`：只编译这一个文件——它就是上一讲认识的、约 400 行的 CUPTI 会话实现。
- `.includes(&toolkit.include_dirs)` 加上条件追加的 `.include(&cupti_include)`：头文件搜索路径的完整来源（主工具链 + 可能的 CUPTI extras）。
- `.compile("pegainfer_cupti_range_profiler")`：产出 `libpegainfer_cupti_range_profiler.a`，并自动发出 `cargo:rustc-link-lib=static=pegainfer_cupti_range_profiler` 与指向产物目录的 `rustc-link-search`。

链接指令段：

> [pegainfer-cupti/build.rs:L27-L31](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L27-L31)
> 声明三个外部库：cuda（CUDA Driver API）、cupti（profiler API）、stdc++（C++ 标准库，Windows 除外）。

```rust
println!("cargo:rustc-link-lib=cuda");
println!("cargo:rustc-link-lib=cupti");
if !cfg!(target_os = "windows") {
    println!("cargo:rustc-link-lib=stdc++");
}
```

- `-lcuda`：实现在 C++ 里直接调用了 CUDA **Driver API**（如上下文获取与同步，详见单元 2/3 的讲义），这些符号在 `libcuda` 里。
- `-lcupti`：本 crate 的存在意义——`cuptiProfilerInitialize` 等一系列 profiler 符号都来自它。
- `-lstdc++`：C++ 实现用了标准库（异常、字符串等）；非 Windows 平台上最终链接由 rustc 驱动，不会自动带上 C++ 标准库，必须显式声明。Windows/MSVC 走另一套 CRT 链接机制，因此有 `if !cfg!(target_os = "windows")` 分支。

增量声明：

> [pegainfer-cupti/build.rs:L33](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/build.rs#L33)
> 告诉 Cargo：只有这份 C++ 源文件变化时才重新运行构建脚本。

```rust
println!("cargo:rerun-if-changed=csrc/range_profiler.cpp");
```

语义要点（Cargo 手册规定）：

1. 如果一个 build.rs **没有**发出任何 `rerun-if-changed`，Cargo 采用保守策略——包内**任何**文件变化都重跑构建脚本。
2. 一旦发出了哪怕一条，保守策略即被关闭，Cargo 只监视列出的路径。
3. 构建脚本自身（`build.rs`）的变化永远会触发重跑，这是内建行为，不需要也不能用这条指令表达。

对这个 crate 而言，L33 意味着：改 `src/lib.rs`、改文档，都不会重跑 build.rs（自然也不会重编 C++）；只有改 `range_profiler.cpp`（或 `build.rs` 本身）才会。收益是构建更快；代价是：如果未来新增第二个 `.cpp`/`.cu` 文件却忘了同步登记 `rerun-if-changed`，改它不会触发重编——这是使用该指令时最常见的坑。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 cc 生成的编译命令行与三条 `-l`。

**操作步骤**（有 CUDA 工具链的机器）：

```bash
cargo build -p pegainfer-cupti -v 2>&1 | grep -E 'range_profiler|link-lib'
```

**无 CUDA 的机器**：构建会失败，但仍可读 Cargo 落盘的指令——打开 `target/debug/build/pegainfer-cupti-*/output`（构建脚本标准输出的快照），寻找以 `cargo:` 开头的行。

**需要观察的现象**：编译命令里的 `-std=c++17` 与 `-I`（主工具链 + 可能的 extras）；`-l` 清单与 4.3.2 示意的对应关系。

**预期结果**：`-lcupti` 出现在最终链接参数中；`cargo:rerun-if-changed=csrc/range_profiler.cpp` 出现在 output 文件里。

**待本地验证**：本讲撰写环境无法执行 cargo 构建，以上未实际运行。

#### 4.3.5 小练习与答案

**练习 1**：改了 `src/lib.rs` 之后重新构建，build.rs 会重跑吗？`csrc/range_profiler.cpp` 会重编吗？
**答案**：都不会。L33 已把重跑条件收窄到那一个 C++ 文件（`build.rs` 自身变化除外）；`lib.rs` 的变化只触发 Rust 侧的重编译。

**练习 2**：cc 已经自动为静态库发了链接指令，为什么 L27-L31 还要自己打印三条 `-l`？
**答案**：cc 的自动指令只覆盖「它自己编出的静态库」；cuda/cupti/stdc++ 是外部系统库，cc 对它们一无所知，必须由 build.rs 显式声明。

**练习 3**：去掉 L29-L31 的 `stdc++` 链接，最可能在哪个阶段、以什么症状失败？
**答案**：链接期，大量 `undefined reference`（C++ 标准库符号，如 `std::runtime_error` 的实现找不到落点）。头文件在编译时能找到（编译能过），符号却在链接时无库可寻。

**练习 4**：`.warnings(false)` 关掉的是什么？为什么值得？
**答案**：屏蔽这份自带 C++ 文件的编译警告。它不是日常迭代的对象，警告只会污染 cargo 输出与 CI 信号；真正的质量问题由代码评审与测试把关。

### 4.4 CI 排除该 crate 的原因：构建期硬依赖的代价

#### 4.4.1 概念说明

先说一个容易被忽略的机制：**`cargo clippy`（以及 `cargo check`、`cargo test`）都要先编译并执行 build.rs**——因为构建脚本产出的指令（`-l`、`-L`、生成的代码）是后续编译/链接的输入，跳过它就无法完成检查。

于是推论很直接：pegainfer-cupti 的 build.rs **硬依赖** CUDA 工具链与 CUPTI（`CudaToolkit::discover()` 找不到工具链，或编译时找不到 CUPTI 头文件/库，构建即失败）。在没有完整 CUDA/CUPTI 的 CI 环境里，任何把本 crate 纳入范围的 workspace 级 lint 都会挂。因此 CI 的 clippy 步骤把这个 crate 排除在外。

这与上一讲的三层门控是同一个思想在不同维度的体现：

| 防线 | 位置 | 挡住什么 |
|---|---|---|
| optional 依赖 | 消费方 Cargo.toml | 默认构建图不含本 crate |
| `kernel-report` feature | 消费方 feature 表 | 显式开启才编译 |
| `required-features`（bin） | 消费方 bin 声明 | feature 不满足时工具不构建 |
| CI 排除 | `.github/workflows/ci.yml` | 无 GPU/无 CUPTI 的 lint 环境不碰它 |

> **待确认**：CI 排除的具体写法与行号，本讲撰写环境无法读取 `.github/workflows/ci.yml`，不予编造。请用 4.4.4 的命令自行验证。

#### 4.4.2 核心流程

```
cargo clippy --workspace（假设含本 crate 且无排除）
    │
    ▼
Cargo 解析 workspace 成员 → pegainfer-cupti 被纳入
    │
    ▼
编译并运行其 build.rs
    │
    ▼
CudaToolkit::discover() 在无 CUDA 环境下失败，
或编译 range_profiler.cpp 时找不到 CUPTI 头文件
（具体失败点取决于 discover 的实现，待确认）
    │
    ▼
lint 任务失败 → CI 需要在 clippy 步骤排除本 crate
```

#### 4.4.3 源码精读

本 crate 侧可以确证的两个事实：

> [pegainfer-cupti/Cargo.toml:L7-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml#L7-L9)
> 构建依赖只有两条：cc 与 pegainfer-build，均为 workspace 继承——构建期的全部外部需求集中在 build.rs 一处，crate 本体保持零运行时依赖。

> [pegainfer-cupti/Cargo.toml:L11-L12](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/Cargo.toml#L11-L12)
> `[lints] workspace = true`：本 crate 并非拒绝 workspace 的 lint 标准，只是 CI 环境跑不动它的构建脚本。

CI 文件 [.github/workflows/ci.yml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/.github/workflows/ci.yml) 中对本 crate 的排除位置与写法：**待确认**（本讲环境无法读取该文件）。

#### 4.4.4 代码实践

**实践目标**：亲眼确认 CI 里的排除写法（这正是本讲没能完成的验证，交给你）。

**操作步骤**（仓库根目录）：

```bash
grep -n -i -E 'cupti|clippy|exclude' .github/workflows/ci.yml
```

**需要观察的现象**：命中的行里，clippy 相关 job 如何圈定范围——是 `--workspace --exclude pegainfer-cupti` 这类参数，还是按包列表显式枚举。

**预期结果**：能找到一条把 `pegainfer-cupti` 从 clippy 范围剔除的配置；记录其精确行号，补全本讲 4.4.3 的「待确认」。

**待本地验证**：撰写环境无法读取该文件，无法预填答案。

#### 4.4.5 小练习与答案

**练习 1**：为什么「只是跑 lint」的 clippy 也逃不过执行 build script？
**答案**：clippy 本质是「换了诊断驱动的 check」，而 check 仍要拿到完整编译输入——包括构建脚本生成的代码与链接指令，所以 build.rs 必须先被编译并执行。

**练习 2**：三层门控与 CI 排除分别在哪个层面起作用？
**答案**：三层门控（optional 依赖 / feature / required-features）作用于**开发者的默认构建图**——不开 feature 就根本不会构建本 crate；CI 排除作用于**CI 的 lint 任务范围**——即便任务显式圈了 workspace 成员，也把本 crate 剔除，避免在无 CUDA 环境必挂。

**练习 3**：如果团队想在 CI 上也 lint 这个 crate，需要什么条件？
**答案**：CI runner 要有完整 CUDA 工具链且含 CUPTI 头文件/库（extras 或系统形态均可，见 4.2），使 build.rs 能成功执行；否则任何 lint 都过不了构建这一关。

## 5. 综合实践：让构建过程「现形」

本讲的综合实践把四个模块串成一次「构建考古」。**全部步骤待本地验证**（本讲撰写环境既无 CUDA 也无法执行构建命令）。

**实践目标**：用一次 `cargo build -v` 和两次文件修改，把本讲的三类声明（编译配置、链接指令、增量监视）全部变成可见证据。

**操作步骤**：

1. **构建并摘录**（有 CUDA 工具链的机器）：

   ```bash
   cargo build -p pegainfer-cupti -v 2>&1 | tee /tmp/cupti-build.log | grep -E 'range_profiler'
   ```

   在 `/tmp/cupti-build.log` 里找两段：编译 `range_profiler.cpp` 的完整命令行（关注 `-std=c++17` 与 `-I`）；最终链接命令中的 `-lcupti`、`-lcuda`、`-lstdc++`。

2. **触发增量重编**：给 `csrc/range_profiler.cpp` 末尾加一行注释 `// build-watcher`（实验后记得还原），再次 `cargo build -p pegainfer-cupti -v`。观察：构建脚本重跑、C++ 重新编译。

3. **对照负例**：改 `src/lib.rs`（同样加一行注释再还原），再次构建。观察：build.rs **不**重跑，只有 Rust 侧编译。

4. **无 CUDA 的替代路线**：构建失败也没关系，直接读 `target/debug/build/pegainfer-cupti-*/output`——这是 Cargo 保存的构建脚本输出快照，从中摘录所有 `cargo:` 开头的行（`rustc-link-search`、`rustc-link-lib`、`rerun-if-changed`）。

**需要观察的现象**：

- 步骤 1：`-I` 是否同时含主工具链与 `extras/CUPTI/include`（若你的 CUPTI 是 4.2 节的形态 A）；
- 步骤 2 与步骤 3 的行为差异，正是 L33 那行 `rerun-if-changed` 的直接体现；
- 步骤 4（若适用）：失败发生在哪一步——`discover()`（探测）还是编译器报找不到头文件？记下来，它就是 4.1 节「待确认」问题的一半答案。

**预期结果**：你能指着构建日志里的每一行，说出它对应 build.rs 的哪一条语句（L13 → `-std=c++17`，L16/L18 → `-I`，L22/L24 → `-L`，L27-L30 → `-l`，L33 → 增量行为）。

## 6. 本讲小结

- `build.rs` 分四段：探测准备（L4-L8）→ cc 编译（L10-L20）→ 链接指令（L22-L31）→ 增量声明（L33）；CUDA 探测统一来自共享的 `pegainfer_build::CudaToolkit::discover()`，单一事实来源。
- CUPTI 在经典安装中位于 `<root>/extras/CUPTI/{include,lib64}`，build.rs 用 `is_dir()` 做「存在才追加」，缺失时回落系统路径，最终失败被前置到编译期/链接期而不是运行期。
- `cc::Build` 负责把 `csrc/range_profiler.cpp` 编成静态库并自动声明其链接；外部库 `cuda`（Driver API）、`cupti`（profiler API）、`stdc++`（C++ 标准库，非 Windows）由 build.rs 显式声明。
- `cargo:rerun-if-changed` 一经发出就关闭「任意文件变化重跑」的默认策略，本 crate 只监视 `csrc/range_profiler.cpp`（`build.rs` 自身变化仍会触发重跑）。
- clippy/check 也要执行 build script，因此「构建期硬依赖 CUDA/CUPTI」直接推导出 CI 必须把这个 crate 排除在 lint 之外——与三层门控同源的错误前置思想。
- 本讲两处「待确认」：`pegainfer-build/src/lib.rs` 的内部实现与行号；`.github/workflows/ci.yml` 的排除写法与行号——两处都给了验证命令，欢迎补全。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：从消费者视角看 `profile_range_with_prepare` 的真实用法——`qwen3_kernel_report` 如何用一次调用拿到硬件计数器，那也是本讲构建出的库真正的用武之地。
- **想直接深入跨语言边界**：可跳读 [pegainfer-cupti/src/lib.rs:L10-L25](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L10-L25)，看 Rust 侧如何声明 C ABI 函数 `pegainfer_cupti_profile_range`（12 个参数外加返回状态），这是单元 2 首讲（u2-l1）的起点。
- **延伸阅读**：Cargo 手册的 *Build Scripts* 一章（`rerun-if-changed` 语义的权威定义）；cc crate 的文档；NVIDIA CUPTI 文档中关于安装位置与支持平台的说明。
