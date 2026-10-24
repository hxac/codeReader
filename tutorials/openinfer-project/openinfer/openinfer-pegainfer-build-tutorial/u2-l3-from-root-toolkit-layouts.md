# from_root：三种 CUDA 工具包布局的统一探测

## 1. 本讲目标

上一讲（u2-l2）结束时，`target_dirs()` 已经把「当前架构」翻译成了 CUDA 工具包 `targets/` 下的候选目录名。本讲进入探测的核心一步：`CudaToolkit::from_root`。学完本讲，你应该能够：

1. 说出 classic（官方安装器）、conda、NVIDIA HPC SDK 三种 CUDA 工具包布局各自的目录特征。
2. 徒手推演 `from_root` 从一个根目录出发，按什么顺序组装 `include_dirs` 与 `lib_dirs` 候选列表。
3. 理解 HPC SDK 布局中 `file_name()` / `parent()` 路径运算如何定位 `math_libs` 这棵「兄弟目录树」。
4. 解释 nvcc 缺失时的回落语义：`PathBuf::from("nvcc")` 为什么意味着「交给进程 `$PATH` 解析」。

## 2. 前置知识

**什么是「工具包布局」？** CUDA 工具包（nvcc 编译器、`cuda.h` 等头文件、`libcudart` 等库文件）会由不同渠道分发，而不同渠道把文件放在**不同的相对目录结构**里：

- **classic 布局**：NVIDIA 官方 runfile / deb 安装器装到 `/usr/local/cuda`，头文件在 `include/`，库在 `lib64/`（x86_64）或 `lib/`，nvcc 在 `bin/nvcc`。
- **conda 布局**：`conda install cuda-toolkit` 装进环境 prefix（如 `$CONDA_PREFIX`）。架构相关的头文件和库收在 `targets/x86_64-linux/include` 与 `targets/x86_64-linux/lib` 下；环境根部的 `include/` 虽然存在，却被别的 C/C++ 包占用，**往往不含 CUDA 头文件**（这一点写在源码注释里，见 4.1.3）。conda 默认还**不带 nvcc**（要单独装 `cuda-nvcc` 包）。
- **HPC SDK 布局**：NVIDIA HPC SDK 是一套编译器 + 数学库的合集，目录形如 `.../hpc_sdk/<os>/<release>/cuda/<ver>`。CUDA 本体在 `cuda/<ver>` 下，但 cuBLAS、cuSPARSE 等**数学库放在同一 `<release>` 下另一棵兄弟树 `math_libs/<ver>` 里**——库目录不在 CUDA 根之下。

**Path / PathBuf 基础操作**（本讲会用到）：

| 方法 | 含义 |
|---|---|
| `join(x)` | 拼接一个路径分量 |
| `is_file()` / `is_dir()` | 是否为存在的文件 / 目录 |
| `file_name()` | 最后一个分量（`Option<&OsStr>`，根路径返回 `None`） |
| `parent()` | 去掉最后一个分量（`Option<&Path>`） |

**`$PATH` 解析**：`Command::new("nvcc")` 这样以「不含分隔符的裸名字」启动进程时，操作系统（Unix 上是 `execvp`）会逐个搜索 `$PATH` 里的目录来找到可执行文件。构建脚本本身不做这个解析——它只是把裸名字存起来，留给将来真正 `Command::new` 的调用方（如 `pegainfer-kernels/build.rs` 用 `toolkit.nvcc` 调 nvcc 编译 `.cu`）。

**承接前两讲**：u2-l1 讲了 `discover()` 用 `CUDA_HOME` → `CUDA_PATH` → `/usr/local/cuda` 定出根；u2-l2 讲了 `target_dirs()` 产出 `targets/` 下的候选目录名（aarch64 双候选、其余单候选）。本讲的主角 `from_root` 正是这两者的汇合点：拿到根 + 拿到架构目录名 → 组装完整候选 → 收敛成 `CudaToolkit`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `pegainfer-build/src/lib.rs` | 唯一源码文件，本讲全部内容都在这里：`from_root`（L91-L121）、nvcc 探测（L92-L97）、候选组装（L99-L113）、以及三个布局测试（L193-L234） |
| `pegainfer-build/Cargo.toml` | crate 元数据；唯一 dev 依赖 `tempfile` 供测试模块搭假目录树 |

`from_root` 是**私有方法**（没有 `pub`），只被同文件的 `discover()` 调用和 `#[cfg(test)]` 测试模块调用——这也解释了为什么验证它的唯一途径是单元测试（见第 5 节实践）。

## 4. 核心概念与源码讲解

### 4.1 候选目录的组装顺序：一套列表覆盖 classic 与 targets 布局

#### 4.1.1 概念说明

`from_root` 的核心设计是：**不为每种布局写一个分支，而是把所有布局的候选目录按固定优先级拼进同一个列表**，最后统一交给 `existing_deduped` 过滤（存在性过滤 + 规范化去重，详见下一讲 u2-l4）。

这样做的好处：

- 三种布局共用一条代码路径，新增布局只需往列表里 push 候选。
- 列表顺序就是优先级顺序——消费方（如 `header_dir`）取第一个命中者。
- 「目录不存在」不是错误：经典安装不会有 `targets/`，conda 安装可能没有 `lib64/`，不存在的候选在收敛阶段被静默丢弃。

#### 4.1.2 核心流程

以 x86_64、根为 `root` 为例（收敛前）：

```text
nvcc 候选:
  root/bin/nvcc          ← 是普通文件则采用

include_dirs 候选（按序）:
  root/include                          ← classic
  root/targets/x86_64-linux/include     ← conda（aarch64 还会多一个 sbsa-linux 候选）

lib_dirs 候选（按序）:
  root/lib64                            ← classic x86_64
  root/lib                              ← classic aarch64 / conda 根部
  root/targets/x86_64-linux/lib         ← conda
  <release>/math_libs/<ver>/lib64       ← HPC SDK 兄弟树（见 4.2）
  <release>/math_libs/<ver>/lib
```

用一句话概括：**「广撒网、按序收」**——先无条件组装全部候选，再靠文件系统的真实状态筛出答案。

#### 4.1.3 源码精读

先看 `from_root` 的开头两段（nvcc 探测在 4.3 单独讲）：

[pegainfer-build/src/lib.rs:91-104](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L91-L104)：`from_root` 的签名接收 `PathBuf` 类型的根目录；随后三行先把 `root/include`、`root/lib64`、`root/lib` 装进两个可变列表（classic 布局的候选）；再对 `target_dirs()` 的每个返回值循环，把 `targets/<dir>/include` 与 `targets/<dir>/lib` 追加进对应列表（conda 布局的候选）。

```rust
fn from_root(root: PathBuf) -> Self {
    // ... nvcc 探测（见 4.3）...

    let mut include_dirs = vec![root.join("include")];
    let mut lib_dirs = vec![root.join("lib64"), root.join("lib")];
    for target in target_dirs() {
        include_dirs.push(root.join(format!("targets/{target}/include")));
        lib_dirs.push(root.join(format!("targets/{target}/lib")));
    }
    // ... HPC SDK 候选（见 4.2）...
```

注意循环写的是 `for target in target_dirs()`——aarch64 下 `target_dirs()` 返回两个候选（`aarch64-linux`、`sbsa-linux`，u2-l2 已讲），两轮循环会把两个 `targets/...` 目录都追加进去；探测**不要求**第一个候选存在，两棵树哪棵真实存在就留哪棵，同时存在就都留。

[pegainfer-build/src/lib.rs:65-73](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L65-L73)：`CudaToolkit` 结构体的 doc 注释直接说明了设计意图——「One build-time CUDA toolkit resolution covering the classic, conda, and NVIDIA HPC SDK layouts」（一次构建期 CUDA 工具包解析，覆盖 classic、conda、HPC SDK 三种布局），并明确运行期加载（`LD_LIBRARY_PATH`、rpath）不在范围内。字段上 `root`、`nvcc`、`include_dirs` 公有，`lib_dirs` 私有、只能经 `link_search()` 输出。

[pegainfer-build/src/lib.rs:123-124](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L123-L124)：`header_dir` 的 doc 注释点出了候选顺序的一个关键后果——「on conda `include/` exists but lacks the CUDA headers」（conda 下 `include/` 存在但没有 CUDA 头文件）。这正是「按序取第一个命中」而不是「取第一个候选」的原因：`header_dir("cuda.h")` 会跳过空有其名的 `include/`，命中真正含头文件的 `targets/.../include`。

再看两个用真实断言刻画 classic / conda 布局的测试：

[pegainfer-build/src/lib.rs:193-206](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L193-L206)：`classic_layout` 测试用 `TempTree` 搭出 `include/cuda.h`、`bin/nvcc`、`lib64/`（含 `lib64/stubs`），断言 `nvcc` 解析为 `root/bin/nvcc`、`header_dir("cuda.h")` 命中 `root/include`、`lib_dirs` 只剩 `lib64` 一项——因为测试树里没有 `lib/`、没有 `targets/`，其余候选全被收敛阶段过滤掉了。

[pegainfer-build/src/lib.rs:208-224](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L208-L224)：`conda_layout` 测试搭的树是：空的 `include/` 目录（`mkdirs("include")`，只有目录没有文件）、`targets/<arch>/include/cuda.h`、根部 `lib/`、`targets/<arch>/lib/`。断言三件事：`nvcc` 是裸 `"nvcc"`（树里没有 `bin/nvcc`，走了 4.3 的回落）、`header_dir("cuda.h")` 命中 `targets` 下的 include 而不是根部那个空 `include/`、`lib_dirs` 依序为 `[root/lib, root/targets/<arch>/lib]`——注意顺序：根部 `lib` 排在候选列表更前面，所以收敛后它排第一。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何代码，徒手推演候选列表，然后与测试断言互相印证。
2. **操作步骤**：
   - 假设根为 `root`、宿主为 aarch64。先自己写出 `from_root` 收敛**前** `include_dirs` 和 `lib_dirs` 的完整候选列表。
   - 打开 [pegainfer-build/src/lib.rs:54-63](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L54-L63)，确认 aarch64 下 `target_dirs()` 返回 `["aarch64-linux", "sbsa-linux"]` 两个候选。
   - 对照上面的伪代码核对顺序。
3. **需要观察的现象**：include 候选共 3 项（`include` + 两个 `targets/...`），lib 候选共 6 项（`lib64`、`lib` + 两个 `targets/.../lib` + 两个 `math_libs` 候选——后者见 4.2）。
4. **预期结果**：x86_64 下 include 候选 2 项、lib 候选 5 项（`lib64`、`lib`、一个 `targets`、两个 `math_libs`）。可以再看一遍 [pegainfer-build/src/lib.rs:99-113](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L99-L113) 逐行数出来验证你的答案。

#### 4.1.5 小练习与答案

**练习 1**：如果根目录下同时存在 `lib64/` 和 `lib/`（某些官方安装确实如此），收敛后 `lib_dirs` 有几项、什么顺序？

**答案**：2 项，`[root/lib64, root/lib]`。两者都通过 `is_dir()` 过滤，且是不同物理目录，canonicalize 结果不同，去重不会合并；顺序继承候选列表顺序，`lib64` 先 push 所以排第一。

**练习 2**：`conda_layout` 测试里 `tree.mkdirs("include")` 只建了一个空目录、没放任何文件。删掉这一行，测试还会通过吗？为什么作者要保留它？

**答案**：仍然通过——该测试的断言只检查 `nvcc`、`header_dir`、`lib_dirs`，都不依赖根部 `include/` 的存在。保留它是为了忠实模拟真实 conda 环境（根部 `include/` 存在但没装 CUDA 头文件），让 `header_dir` 命中 `targets` 分支这件事在测试里是「被空的根部 include 考验过的」，而不是「因为根部 include 不存在而碰巧命中」。

**练习 3**：为什么把三种布局做成「同一张候选列表」，而不是 `match` 布局类型、每种布局单独一套路径？

**答案**：真实安装常是**混合体**——HPC SDK 的 `cuda/<ver>` 树本身是 classic 形态，同时它的数学库在兄弟树里；conda 环境也可能再装官方包。单一有序列表天然支持任意混合：每个真实存在的目录都按统一优先级入选，不需要先判定「这是哪种布局」再选分支，也避免了「判定错了就整体找不到」的脆弱性。新增布局（u3-l4 的实战主题）也只需往列表 push 新候选。

### 4.2 HPC SDK math_libs 兄弟目录树定位

#### 4.2.1 概念说明

HPC SDK 的目录结构（注释原文：`.../hpc_sdk/<os>/<release>/cuda/<ver>`）：

```text
/opt/nvidia/hpc_sdk/Linux_x86_64/2024/     ← <release>（整套 SDK 的版本层）
├── cuda/                                  ← 各组件中 CUDA 在这里
│   └── 12.6/                              ← 这才是 from_root 拿到的 root
│       ├── bin/nvcc
│       ├── include/
│       └── lib64/
└── math_libs/                             ← 兄弟树：与 cuda 同级
    └── 12.6/                              ← 与 cuda 下的版本号同名
        ├── lib64/                         ← cuBLAS、cuSPARSE 等数学库
        └── lib/
```

难点在于：**数学库目录不在 `root` 之下**，无法用 `root.join(...)` 直接拼出来。必须先从 `root` **向上**走两级找到 `<release>`，再**横向**进入 `math_libs/<ver>`。「兄弟目录」指的就是 `math_libs` 与 `cuda` 在 `<release>` 下互为兄弟。

还有一个前提问题：探测时怎么知道版本号 `<ver>`？答案是不用知道——它就是 `root` 自身的最后一段路径分量，`file_name()` 直接取出来复用。这保证了 math_libs 的版本永远和 CUDA 根的版本一致，无需读任何版本文件。

#### 4.2.2 核心流程

```text
root = .../hpc_sdk/<os>/<release>/cuda/<ver>

version = root.file_name()                  → <ver>
release = root.parent().parent()            → .../<release>   （向上两级：先到 cuda/，再到 <release>/）
math    = release / "math_libs" / version   → .../<release>/math_libs/<ver>

候选追加:  math/lib64, math/lib   → 只进 lib_dirs，不进 include_dirs
```

注意 `root.parent()` 得到的是 `.../<release>/cuda`（还差一级），再 `parent()` 一次才是 `<release>`。两个 `Option` 任意一个为 `None`（根太浅，比如 `/cuda` 或 `/`），整个 `if let` 匹配失败，**静默跳过** math_libs 候选——浅根不 panic，只是少了这两个候选。

#### 4.2.3 源码精读

[pegainfer-build/src/lib.rs:105-113](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L105-L113)：注释先写明 HPC SDK 的目录形状与「math libraries live in the `<release>/math_libs/<ver>` sibling tree」（数学库在 `<release>/math_libs/<ver>` 兄弟树中），随后用元组 `if let` 同时解构出版本与 release 两层，把 `math/lib64`、`math/lib` 两个候选追加进 `lib_dirs`：

```rust
// HPC SDK roots look like .../hpc_sdk/<os>/<release>/cuda/<ver>; the
// math libraries live in the <release>/math_libs/<ver> sibling tree.
if let (Some(version), Some(release)) =
    (root.file_name(), root.parent().and_then(Path::parent))
{
    let math = release.join("math_libs").join(version);
    lib_dirs.push(math.join("lib64"));
    lib_dirs.push(math.join("lib"));
}
```

三个值得学的 Rust 细节：

- **`root.file_name()`** 返回 `Option<&OsStr>`，`root.parent()` 返回 `Option<&Path>`；元组模式 `(Some(version), Some(release))` 要求两者同时为 `Some`，任意一个 `None`（路径太浅）整个块直接跳过。
- **`and_then(Path::parent)`**：`and_then` 接收函数指针 `Path::parent`，等价于 `.and_then(|p| p.parent())`——把「再向上走一级」写成一行。
- **借用而非克隆**：`version` 和 `release` 都是借用（`&OsStr`、`&Path`），`join` 接受 `AsRef<Path>`，无需先复制路径。

[pegainfer-build/src/lib.rs:115-120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L115-L120)：`from_root` 的收尾——候选列表交给 `existing_deduped` 收敛后装入结构体。也就是说 math_libs 候选与 classic 候选**同等待遇**：目录不存在（普通安装没有 `math_libs` 树）就在这里被过滤掉，不产生任何报错或告警。

[pegainfer-build/src/lib.rs:226-234](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L226-L234)：`hpc_sdk_layout_adds_math_libs_sibling` 测试完整复现了上面的树：`release/cuda/12.6/lib64` 与 `release/math_libs/12.6/lib64` 两棵树，断言收敛后 `lib_dirs` 恰为 `[cuda 的 lib64, math_libs 的 lib64]` 两项、且**顺序是 cuda 在前**——因为候选列表里 classic 的 `lib64` 先 push、math_libs 的在后。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「兄弟树」路径运算的正确性，用 `std::path` 在内存里推一遍（不碰文件系统）。
2. **操作步骤**：在仓库根目录用 `cargo` 的临时脚本能力不合适（本 crate 没有 examples 目录），改用最小 Rust 程序验证。在系统临时目录建一个独立 scratch crate（**示例代码**，非项目原有文件）：

   ```rust
   // ~/scratch/pathmath/src/main.rs —— 示例代码
   use std::path::Path;

   fn main() {
       let root = Path::new("/opt/nvidia/hpc_sdk/Linux_x86_64/2024/cuda/12.6");
       if let (Some(version), Some(release)) =
           (root.file_name(), root.parent().and_then(Path::parent))
       {
           let math = release.join("math_libs").join(version);
           println!("{}", math.join("lib64").display());
           println!("{}", math.join("lib").display());
       }
   }
   ```

   运行 `cargo run`（scratch crate 内，`--release` 与否不影响结果）。
3. **需要观察的现象**：输出的两行路径。
4. **预期结果**：
   ```text
   /opt/nvidia/hpc_sdk/Linux_x86_64/2024/math_libs/12.6/lib64
   /opt/nvidia/hpc_sdk/Linux_x86_64/2024/math_libs/12.6/lib
   ```
   即版本号 `12.6` 来自 CUDA 根自身的最后一段，release 是向上两级。若把 `root` 换成 `/cuda`，`parent().and_then(Path::parent)` 为 `None`，程序不输出任何内容——与 `from_root` 跳过 math_libs 候选的行为一致。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`root = /opt/nvidia/hpc_sdk/Linux_aarch64/24.5/cuda/12.4`，写出代码会追加的两个候选目录的完整路径。

**答案**：`/opt/nvidia/hpc_sdk/Linux_aarch64/24.5/math_libs/12.4/lib64` 与 `/opt/nvidia/hpc_sdk/Linux_aarch64/24.5/math_libs/12.4/lib`。`file_name()` 是 `12.4`；向上两级分别是 `.../24.5/cuda` 和 `.../24.5`。

**练习 2**：`root = /cuda` 时这段代码会 panic 吗？行为是什么？

**答案**：不会 panic。`root.file_name()` 是 `Some("cuda")`，但 `root.parent()` 是 `Some("/")`，再 `parent()` 得到 `None`，元组匹配失败，`if let` 块整体跳过——不追加 math_libs 候选，`from_root` 继续正常执行。这是「候选不存在就静默丢弃」哲学的一部分。

**练习 3**：为什么不把 math_libs 路径写成字符串拼接（如 `format!("{}/../../math_libs/{}/lib64", root, version)`）？

**答案**：三个原因。① 分量级 `join` / `parent()` 不依赖 `/` 分隔符，跨平台安全；② `..` 拼接出的路径仍是「未规范化」形态，后面 `canonicalize` 去重时要依赖文件系统解析，而先 `parent()` 得到的已经是干净的规范父路径；③ `Option` 链式向上走天然处理了「路径不够深」的边界，字符串拼接则需要自己判断层级。另外借用式的 `&Path` 组合避免了多余的分配。

### 4.3 nvcc 探测与 `$PATH` 回落

#### 4.3.1 概念说明

`from_root` 做的第一件事是定位 nvcc。规则很简单：

- `root/bin/nvcc` 是普通文件 → 用它（classic、HPC SDK 布局都有）。
- 否则 → 存一个裸名字 `PathBuf::from("nvcc")`，含义是「**我没找到，但也许系统 `$PATH` 里有**」。

回落的存在有现实依据：conda 布局默认不带 nvcc（编译器由 `cuda-nvcc` 单独提供，或干脆依赖系统 PATH 里已有的 nvcc），此时探测**不该失败**——头文件和库目录仍然可用，链接照样能做。这也与 u2-l1 讲过的 discover 软失败哲学一致：CUDA 根的问题只告警不阻断，nvcc 缺失连告警都不发，静默交给 `$PATH`。

关键理解：裸 `"nvcc"` 是一个**单分量相对路径**。`from_root` 不解析它、不查 PATH——解析发生在**未来某个调用方**执行 `Command::new(&toolkit.nvcc)` 的那一刻，由操作系统完成。

#### 4.3.2 核心流程

```text
nvcc = root/bin/nvcc
if nvcc 是普通文件 (is_file):
    保留绝对路径
else:
    nvcc = "nvcc"            ← 单分量相对路径，未来由 execvp 按 $PATH 解析
```

判据用的是 `is_file()` 而不是 `exists()`：`exists()` 对目录也返回真，若 `bin/nvcc` 恰好是个目录（异常但可能），`is_file()` 会正确地走回落分支。

#### 4.3.3 源码精读

[pegainfer-build/src/lib.rs:92-97](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L92-L97)：nvcc 探测的完整实现——先拼 `root/bin/nvcc`，`is_file()` 不成立则替换为裸 `PathBuf::from("nvcc")`：

```rust
let nvcc = root.join("bin/nvcc");
let nvcc = if nvcc.is_file() {
    nvcc
} else {
    PathBuf::from("nvcc")
};
```

两行同名 `let` 是 Rust 惯用的「遮蔽」（shadowing）：第二个 `nvcc` 覆盖第一个，类型不变（都是 `PathBuf`），但语义从「候选绝对路径」变成「确认结果或裸名字」。

[pegainfer-build/src/lib.rs:68-70](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L68-L70)：结构体字段的 doc 注释把这个契约写进了公开 API 文档——「`{root}/bin/nvcc` when present, otherwise bare `nvcc` from `$PATH`」（存在时为 `{root}/bin/nvcc`，否则为来自 `$PATH` 的裸 `nvcc`）。消费方（u3-l2 会精读 `pegainfer-kernels/build.rs`）可以放心地无条件 `Command::new(&toolkit.nvcc)`，无需先判断是否找到。

[pegainfer-build/src/lib.rs:216-218](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L216-L218)：`conda_layout` 测试的第一条断言 `assert_eq!(tk.nvcc, PathBuf::from("nvcc"))` 正是在验证回落——测试树里没有 `bin/nvcc`，断言结果必须是裸名字。这条断言与 `classic_layout` 的 `assert_eq!(tk.nvcc, tree.root().join("bin/nvcc"))`（[L202](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L202)）一正一反，把两种结果都钉死在 CI 里。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「裸 nvcc 走 `$PATH`」在真实进程中的效果，确认回落语义。
2. **操作步骤**（**示例代码**，独立 scratch 环境，不改动本仓库）：

   ```rust
   // ~/scratch/nvccpath/src/main.rs —— 示例代码
   use std::path::PathBuf;
   use std::process::Command;

   fn main() {
       // 模拟 from_root 的两种 nvcc 结果
       let bare: PathBuf = PathBuf::from("nvcc"); // 回落形态
       let output = Command::new(&bare).arg("--version").output();
       match output {
           Ok(o) if o.status.success() => {
               print!("{}", String::from_utf8_lossy(&o.stdout));
           }
           _ => println!("nvcc 不在 $PATH 中（本机无 CUDA），Command 失败属预期"),
       }
       // 观察：bare.is_absolute() == false —— 这正是「交给 $PATH」的判断依据
       println!("is_absolute = {}", bare.is_absolute());
   }
   ```

   分别在有 CUDA 的机器（`nvcc --version` 应打印版本）和无 CUDA 的机器（打印失败提示）上各跑一次 `cargo run`。
3. **需要观察的现象**：裸 `"nvcc"` 能否被 `Command::new` 解析；`is_absolute` 的值。
4. **预期结果**：有 CUDA 的机器上输出 nvcc 版本横幅；无 CUDA 的机器上走失败分支。两种情况下 `is_absolute` 都是 `false`——所以消费方可以用 `nvcc.is_absolute()` 区分「根内找到」与「`$PATH` 回落」。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`root/bin/nvcc` 存在但是个**目录**，`from_root` 会怎么处理？

**答案**：走回落分支，`nvcc` 为裸 `"nvcc"`。判据是 `is_file()`（要求普通文件），目录不满足。用 `exists()` 的话就会把目录当可执行文件存进去，之后 `Command::new` 启动失败且报错难懂——`is_file()` 是更严谨的选择。

**练习 2**：`PathBuf::from("nvcc")` 相对于什么解析？解析发生在 `from_root` 内部吗？

**答案**：相对于进程的 `$PATH`（Unix 上由 `execvp` 逐目录搜索），**不相对于** `root`。解析不发生在 `from_root` 内——它只存储裸名字；真正解析发生在未来消费方（如 `pegainfer-kernels/build.rs`）执行 `Command::new(&toolkit.nvcc)` 时。注意区分：`root.join("nvcc")` 才是相对 root 的路径，而这里没有 join。

**练习 3**：conda 布局下用户真的需要 nvcc（比如要编译 `.cu` 文件），探测结果是一个裸 `"nvcc"`，之后环境里也装好了 `cuda-nvcc`。这套机制还能工作吗？有什么隐患？

**答案**：能工作——只要 `cuda-nvcc` 的 `bin` 在构建时的 `$PATH` 里，`Command::new("nvcc")` 就能解析到。隐患在于构建结果**依赖环境变量之外的状态**：`rerun-if-env-changed` 只跟踪 `CUDA_HOME`/`CUDA_PATH`（u2-l1），`$PATH` 变了、或 `$PATH` 里的 nvcc 换了版本，Cargo 不会自动重跑 build.rs（u3-l3 会系统讨论这类缓存边界）。

## 5. 综合实践

**任务：搭出三种布局的假目录树，一次性打印并核对 `from_root` 的全部输出。**

前置困难：`from_root` 是私有方法，crate 外无法调用。解决办法正是本讲反复出现的模式——**写进 `pegainfer-build` 自己的 `#[cfg(test)]` 测试模块**：子模块 `mod tests` 通过 `use super::*` 可以访问父模块的一切私有项，包括 `from_root` 和私有字段 `lib_dirs`。这是 Rust 单元测试的标准设计，也是这个 crate 唯一的验证入口。

1. **实践目标**：用一棵测试树同时验证三个模块的知识——候选顺序（4.1）、math_libs 兄弟树（4.2）、nvcc 回落（4.3）。

2. **操作步骤**：

   a. 在你自己的克隆里，打开 `pegainfer-build/src/lib.rs`，在 `mod tests` 内（[L161-L299](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L161-L299) 之间）添加一个测试（**示例代码**，非项目原有；`TempTree` 与 `target_dir` 复用测试模块已有的辅助项）：

   ```rust
   #[test]
   fn print_mixed_layout() {
       // 一棵「三合一」树：classic + targets + math_libs 兄弟树，但故意不放 bin/nvcc
       let tree = TempTree::new();
       let target = target_dir();
       tree.touch("include/cuda.h");
       tree.mkdirs("lib64");
       let cuda_root = tree.mkdirs(&format!("release/cuda/{target}")); // 借架构名当版本号
       tree.mkdirs(&format!("release/cuda/{target}/lib64"));
       tree.mkdirs(&format!("release/math_libs/{target}/lib64"));

       let tk = CudaToolkit::from_root(cuda_root);
       println!("nvcc         = {:?}", tk.nvcc);
       println!("include_dirs = {:?}", tk.include_dirs);
       println!("lib_dirs     = {:?}", tk.lib_dirs); // 私有字段，测试模块内可访问
   }
   ```

   b. 在**仓库根目录**运行：

   ```bash
   cargo test --release -p pegainfer-build --lib print_mixed_layout -- --nocapture
   ```

   `--nocapture` 让 `println!` 的输出显示出来。验证完可用 `git checkout -- pegainfer-build/src/lib.rs` 还原（CLAUDE.md 规定 CI 跑 `cargo fmt --check`，临时改动记得还原或先 `cargo fmt`）。

3. **需要观察的现象**：打印出的三行字段值，以及它们与树的结构对应关系。

4. **预期结果**（先自己推演再对照）：

   | 字段 | 预期值 | 依据 |
   |---|---|---|
   | `nvcc` | `"nvcc"`（裸名字） | 树里没有 `release/cuda/<arch>/bin/nvcc`，走回落（4.3） |
   | `include_dirs` | `[<cuda_root>/include]` 一项 | 只有 classic 的 include 存在；`targets/<arch>/include` 没建、被过滤（4.1） |
   | `lib_dirs` | `[<cuda_root>/lib64, <release>/math_libs/<arch>/lib64]` 两项，cuda 在前 | classic 候选先 push、math_libs 候选后 push，顺序保留（4.1 + 4.2） |

   若你的输出与此不符，回到 4.1.2 的候选列表逐项排查哪一步预期错了。预期结果为分析所得，**待本地验证**。

   （若不想改任何源文件，可改用方案 B：把 `lib.rs` 的 `CudaToolkit`、`target_dirs`、`existing_deduped` 三个定义复制进一个独立 scratch crate 并加 `tempfile` dev 依赖，效果等价，只是要同步维护副本。）

## 6. 本讲小结

- `from_root` 把三种布局统一成**一张按序组装的候选目录列表**：classic（`include`、`lib64`、`lib`）在前，conda 的 `targets/<arch>/{include,lib}` 随后（aarch64 双候选都会追加），HPC SDK 的 `math_libs` 候选殿后；不存在的候选由 `existing_deduped` 静默收敛（下一讲精读）。
- HPC SDK 的数学库在 CUDA 根**之外**的兄弟树 `<release>/math_libs/<ver>`：用 `root.file_name()` 复用版本号、`root.parent().and_then(Path::parent)` 向上两级定位 `<release>`；路径太浅时 `Option` 元组匹配失败、静默跳过。
- 列表顺序即优先级：`header_dir` 取第一个真正含目标头文件的目录，所以 conda 下空有其名的根部 `include/` 会被 `targets` 下的真 include 压过。
- nvcc 探测以 `is_file()` 为准：`root/bin/nvcc` 是普通文件则用绝对路径，否则存裸 `PathBuf::from("nvcc")`——单分量相对路径，解析推迟到消费方 `Command::new` 时由 `$PATH`（`execvp`）完成。
- 三个布局测试（`classic_layout`、`conda_layout`、`hpc_sdk_layout_adds_math_libs_sibling`）用 `TempTree` 假目录树把上述行为全部钉进 CI；`from_root` 是私有方法，测试模块是它唯一的验证入口。

## 7. 下一步学习建议

下一讲 **u2-l4（existing_deduped、header_dir 与 link_search）**补上本讲刻意留下的「收敛与输出」半程：`existing_deduped` 如何用 `canonicalize` 处理 conda 常见的符号链接目录（同一物理目录去重）、`header_dir` 在 `pegainfer-kernels/build.rs:2211` 给宿主编译器加 `-I` 的真实消费场景、以及 `link_search` 如何把本讲的 `lib_dirs` 变成 `cargo:rustc-link-search` 指令、无库目录时的告警。建议先重读本讲的 [L115-L120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L115-L120)（收敛调用点），带着「候选列表进去、有效列表出来」的上下文进入下一讲。
