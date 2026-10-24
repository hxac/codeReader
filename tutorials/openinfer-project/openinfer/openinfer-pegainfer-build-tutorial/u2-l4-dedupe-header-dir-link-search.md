# existing_deduped、header_dir 与 link_search：探测结果的收敛与输出

## 1. 本讲目标

u2-l3 结束时，`from_root` 已经把三种工具包布局的候选目录按固定优先级拼进了两张原始列表（include 候选与 lib 候选），但那还是「广撒网」的形态。本讲补完探测的最后两步：**收敛**（`existing_deduped` 把原始候选变成可靠结果）与**输出**（`header_dir` 服务头文件查找、`link_search` 服务链接搜索）。学完本讲，你应该能够：

1. 说出 `existing_deduped` 的双重职责——存在性过滤 + `canonicalize` 规范化去重——以及为什么 conda 布局下同一物理目录会出现两次、必须去重。
2. 解释 `header_dir(header)`「按优先级取第一个真正包含该头文件的目录」的语义，并追踪它在 `pegainfer-kernels/build.rs` 里变成 nvcc `-isystem` 参数的完整路径。
3. 掌握 `link_search` 发射的 `cargo:rustc-link-search=native=...` 指令形态、`lib_dirs` 为空时的告警行为，以及 `lib_dirs` 为什么被设计成私有字段。

## 2. 前置知识

**符号链接（symlink）与 `canonicalize`。** 符号链接是一个「内容是另一个路径」的特殊文件，访问它时操作系统自动跳到目标路径。`Path::canonicalize()` 返回一条**绝对路径、解析掉全部符号链接与 `..`/`.` 之后**的「物理路径」——同一物理目录无论从哪条写法走进去，`canonicalize` 的结果都相同。conda 正是用符号链接组织环境的：环境根部的 `include/` 常常是指向 `targets/<arch>/include` 的符号链接（或干脆被别的包占用）。

**`-I` 与 `-isystem`：告诉编译器去哪找 `#include`。** C/C++ 编译器（cc/c++）用 `-I<dir>` 把目录加进头文件搜索路径；nvcc 同样接受 `-I`，还接受 `-isystem <dir>`（把目录标记为「系统头」路径，抑制其中的告警）。`pegainfer-kernels` 编译 `.cu` 文件时必须能找到 `cuda.h`，所以它需要「真正放着 cuda.h 的那个目录」。这也是 `header_dir` 的文档注释里 "host-compiler `-I` flags need this" 的含义。

**`cargo:rustc-link-search` 回顾（u1-l2 讲过）。** build script 用 `println!("cargo:rustc-link-search=native=<dir>")` 告诉 Cargo：链接时把 `<dir>` 加进库搜索路径（等价于给 rustc 传 `-L native=<dir>`）。它只解决「去哪找库」；至于「链接哪些库」，由另外的 `cargo:rustc-link-lib=<name>` 指令决定，两者配合使用。

**「规范化身份作键、原始路径作输出」。** 这是本讲最重要的一个设计点：去重时用 `canonicalize` 的结果判断「是不是同一个物理目录」，但保留下来的却是**第一次出现时的原始路径**（可能是符号链接形态）。为什么这样设计，见 4.1.1。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
|---|---|
| `pegainfer-build/src/lib.rs` | `existing_deduped`（L145-L159）、`from_root` 末尾对它的两处调用（L115-L120）、`header_dir`（L123-L130）、`link_search`（L132-L142）、字段可见性（L67-L73）、符号链接去重测试 `symlinked_dirs_dedupe`（L236-L258）、conda 布局测试（L208-L224）、未知布局测试（L260-L269） |
| `pegainfer-kernels/build.rs` | 消费方视角：`discover()` 与 `header_dir("cuda.h")` 兜底（L2209-L2213）、nvcc 的 `-isystem` 参数（L2347-L2357）、Triton AOT 对 `cuda.h` 的硬断言（L1321-L1327）、`cuda_include` 流向 glm52 cute-dsl 与 TileLang 任务（L2227-L2230、L2743-L2752、L2767-L2775、L2874-L2875）、main 末尾的 `link_search()` 调用与 Windows 特例（L2882-L2890）、kernel-lab 共享库的对照用法（L927-L944） |

`existing_deduped` 与 `from_root` 一样是私有函数，只能被本 crate 调用；`header_dir`、`link_search` 则是 `CudaToolkit` 的公开方法，消费方（kernels、cupti）直接使用。

## 4. 核心概念与源码讲解

本讲的三个角色在探测流水线里的位置：

```text
discover() 定根 (u2-l1)
    │
from_root() 组装原始候选 (u2-l3)
    │
    ├── existing_deduped(include_dirs) ──→ CudaToolkit.include_dirs（公有）
    │                                          │
    │                                          └─→ header_dir("cuda.h") → nvcc -isystem / 宿主编译器 -I
    │
    └── existing_deduped(lib_dirs) ─────→ CudaToolkit.lib_dirs（私有）
                                               │
                                               └─→ link_search() → cargo:rustc-link-search=native=...
```

### 4.1 existing_deduped：目录存在性过滤与规范化去重

#### 4.1.1 概念说明

`from_root` 组装的候选列表是「无条件拼接」的：classic 布局的 `include`、`lib64`、`lib`，conda 布局的 `targets/<arch>/...`，HPC SDK 的 `math_libs` 兄弟树，全部混在一张列表里（u2-l3 的 4.1.2）。任何真实安装只会命中其中一部分，所以收敛函数有两重职责：

1. **存在性过滤**：不存在的候选静默丢弃。「目录不存在」不是错误——经典安装没有 `targets/`，conda 安装可能没有 `lib64/`。
2. **规范化去重**：conda 布局常用符号链接把环境根部的 `include`/`lib` 指向 `targets/<arch>/` 下的真身。于是同一物理目录会以两条不同写法出现在候选列表里（`root/include` 与 `root/targets/x86_64-linux/include`）。不去重的后果：`include_dirs` 里有两个「同一个目录」，`link_search` 发射两条指向同一处的 `-L` 指令，输出冗余又干扰诊断。

设计要点是「**规范化身份作键、原始路径作输出**」：

- **键**用 `canonicalize()` 结果：符号链接解析后指向同一物理目录的两条路径，键相同，第二条被判为重复。
- **输出**用第一次出现的**原始路径**：消费方看到的仍然是「工具包的自然写法」（比如 conda 根部的 `include`），而不是一条解析到底的规范路径。后面 4.2 会看到测试明确断言 `header_dir` 命中的是符号链接路径。

#### 4.1.2 核心流程

```text
existing_deduped(dirs):
    seen ← 空列表          # 已见过的规范化身份
    out  ← 空列表          # 保留下来的原始路径
    对 dirs 中每个 dir（按候选优先级顺序）:
        若 dir 不是目录          → 跳过        # 存在性过滤
        canon ← canonicalize(dir)，失败则退回 dir 本身
        若 canon 不在 seen 中    → 记入 seen，把 dir 追加进 out
    返回 out
```

它维护的不变式可以用一句话说清：`out` 中每个元素到其规范化身份的对应是**单射**——同一物理目录至多出现一次；且被保留的是**首次出现**的写法，因此候选列表的优先级顺序（classic → targets → math_libs）完整传递到结果里。

两个实现细节值得注意：

- `seen` 是 `Vec` + 线性 `contains`，看似 O(n²)，但候选列表最长也就 6 项（include 2~3 项、lib 4~6 项），线性扫描足够，还省掉一个 `HashSet` 导入——简单优先。
- `canonicalize` 失败时退回 `dir.clone()` 兜底：`is_dir()` 刚通过后 `canonicalize` 几乎不可能失败（除非并发删除），兜底保证函数永不 panic，与整个 crate「软失败」的哲学一致（对比 `find_package` 的硬 panic，见 u2-l5）。

#### 4.1.3 源码精读

先看 `existing_deduped` 本体，函数只有 15 行：

[pegainfer-build/src/lib.rs:L145-L159](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L145-L159) —— 私有收敛函数：L149-L150 用 `is_dir()` 过滤不存在的候选；L152 求规范化身份（失败退回原始路径）；L153-L156 以规范化身份查重，只有新身份才把**原始路径**收进 `out`。

调用点在 `from_root` 的收尾，两张列表各过一遍：

[pegainfer-build/src/lib.rs:L115-L120](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L115-L120) —— 构造 `CudaToolkit` 时，`include_dirs` 与 `lib_dirs` 都先经 `existing_deduped` 收敛，`root` 原样保存。

收敛结果落在结构体字段上，而两个字段的可见性不同：

[pegainfer-build/src/lib.rs:L67-L73](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L67-L73) —— `include_dirs` 是 `pub`（消费方需要自己遍历或用 `header_dir`），`lib_dirs` 没有 `pub`，是私有字段：外部唯一能消费它的出口是 `link_search()`（见 4.3）。

符号链接去重有一个专门的单元测试 `symlinked_dirs_dedupe`（`#[cfg(unix)]`，因为 Windows 建符号链接需要特权）：

[pegainfer-build/src/lib.rs:L236-L258](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L236-L258) —— 测试先用 `touch`/`mkdirs` 搭出 `targets/<arch>/include/cuda.h` 与 `targets/<arch>/lib`（L241-L242），再把根部的 `include`、`lib` 建成指向它们的符号链接（L243-L252），完整模拟 conda 布局；然后断言收敛后 `include_dirs` 与 `lib_dirs` 各只剩一项（L255-L256），且 `header_dir("cuda.h")` 命中的是**符号链接路径** `root/include` 而非 targets 路径（L257）。

对照一个「什么都没收敛出来」的用例：

[pegainfer-build/src/lib.rs:L260-L269](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L260-L269) —— 未知布局（只有一个 `weird/place` 目录）探测后两个列表都为空、`header_dir` 返回 `None`。空 `lib_dirs` 正是 4.3 中 `link_search` 告警分支的触发条件。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认符号链接去重测试通过，并把它的每条断言对应到 4.1.2 的不变式。
2. **操作步骤**：在仓库根目录运行（纯 CPU，不需要 GPU 和 CUDA）：

   ```bash
   cargo test --release -p pegainfer-build --lib symlinked -- --nocapture
   ```

   然后打开 [lib.rs:L236-L258](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L236-L258)，把三条断言（L255-L257）逐条与 4.1.2 的不变式对应起来。
3. **需要观察的现象**：测试名 `symlinked_dirs_dedupe` 出现在输出中且状态为 `ok`；若在测试里临时加 `println!("{:?}", tk.include_dirs)`（本地实验，勿提交），会看到唯一元素是符号链接路径而非 targets 路径。
4. **预期结果**：该测试通过。CI 的 cpu-tests 作业本身就运行整个 `cargo test -p pegainfer-build --lib` 套件（u1-l1 已确认 7 个测试全绿），单个过滤运行同样应通过。编写本讲义时未在本地重跑此命令，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么去重的键用 `canonicalize` 的结果、输出却保留原始路径？反过来（键用原始路径、输出用规范路径）行不行？

> 参考答案：键必须用规范化身份，否则符号链接形态与真身形态会被当成两个目录，去重失效。输出保留原始路径，是因为原始路径才是「工具包的自然写法」：诊断信息、`-I`/`-L` 参数对用户更有意义，且不改变用户指定根目录的形态。若输出用规范路径，功能上也能链接成功，但测试 L257 的断言会变、告警信息可读性下降。

**练习 2**：若把 `root/include` 从符号链接换成一个**真目录**且其中也有 `cuda.h`（classic 与 conda 混合布局），`include_dirs` 会有几项？为什么？

> 参考答案：两项。`root/include`（真目录）与 `root/targets/<arch>/include` 是**两个不同的物理目录**，`canonicalize` 结果不同，不算重复；存在性过滤也拦不住它们。去重只针对「同一物理目录的多种写法」，不针对「都能用但内容重复的目录」——此时靠**列表顺序**表达优先级：classic 的 `include` 排在前面（见 4.2 的 `header_dir` 语义）。

**练习 3**：`canonicalize()` 在什么情况下会失败？失败时代码的行为是什么？

> 参考答案：典型情况是路径在 `is_dir()` 检查与 `canonicalize()` 之间被并发删除（竞态），或权限问题。失败时 L152 用 `unwrap_or_else(|_| dir.clone())` 退回原始路径作键：完全相同写法的路径仍能去重，函数不会 panic——软失败设计的又一处体现。

### 4.2 header_dir：定位真正包含头文件的目录

#### 4.2.1 概念说明

4.1 里已经知道，conda 布局下 `root/include` **目录存在但往往不含 CUDA 头文件**（被 conda 的其他包占用）。而 kernels 编译 `.cu` 需要 `cuda.h`——「拿 `include_dirs` 的第一项」因此是不可靠的，需要的是「**真正包含 `cuda.h` 的那个目录**」。这就是 `header_dir(header: &str)` 的职责：把「目录列表」变成「对某个具体头文件而言正确的目录」。它返回 `Option<PathBuf>`：所有候选里都没有该文件时返回 `None`，把「找不到」如实交给消费方决定兜底策略。

注意它解决的是**构建期**的头文件定位（nvcc 编译 `.cu`、宿主编译器编 C++ 垫片时加 `-I`/`-isystem`），与运行期的动态库加载（`LD_LIBRARY_PATH`、rpath）无关——后者在结构体的文档注释里明确写着 out of scope。

#### 4.2.2 核心流程

```text
header_dir(header):
    对 include_dirs 按顺序遍历（顺序即优先级，u2-l3 组装的 classic → targets）:
        若 dir/header 是普通文件（is_file() 会跟随符号链接）→ 返回 Some(dir)
    全部未命中 → 返回 None
```

两个关键语义：

- **取第一个命中**（`find`），不是收集全部命中。候选顺序已经表达了优先级，消费方只需要一个目录。
- **`is_file()` 跟随符号链接**：在 4.1 的符号链接场景下，`root/include/cuda.h` 虽是经符号链接访问的，`is_file()` 为真，所以命中的是符号链接路径——这正是测试 L257 断言的内容。

#### 4.2.3 源码精读

`header_dir` 的文档注释把动机写得非常直白：

[pegainfer-build/src/lib.rs:L123-L130](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L123-L130) —— 注释点明两件事：宿主编译器的 `-I` 参数需要的是「真正含该头文件的目录」；conda 布局下 `include/` 存在却缺 CUDA 头。实现体（L125-L130）就是一次带 `is_file()` 谓词的 `find` + `cloned()`。

conda 布局测试的断言展示了它相对「取第一项」的差别：

[pegainfer-build/src/lib.rs:L219-L223](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L219-L223) —— 测试树里 `root/include` 是**空目录**（只 `mkdirs`、没 `touch` 头文件，见 L212），`cuda.h` 只在 `targets/<arch>/include` 下；断言 `header_dir("cuda.h")` 正确跳过空壳、返回 targets 路径。若实现改成 `include_dirs.first()`，这个测试立刻失败。

再看消费方。`pegainfer-kernels/build.rs` 的 main 开头几行就是标准用法：

[pegainfer-kernels/build.rs:L2209-L2213](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2209-L2213) —— kernels 先 `discover()`，再用 `header_dir("cuda.h")` 定位 CUDA 头目录；`unwrap_or_else` 的兜底值得注意：所有候选都没有 `cuda.h` 时（比如机器上没装 CUDA），回落到 classic 位置的 `root/include`，让构建在稍后编译 `.cu` 时由编译器报出「找不到 cuda.h」的错，而不是在 build script 里提前 panic。

拿到的 `cuda_include` 随后进入 nvcc 参数：

[pegainfer-kernels/build.rs:L2347-L2357](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2347-L2357) —— 每个 `.cu` 的 nvcc 命令行带上 `-isystem <cuda_include>`（L2353-L2354），保证 `#include <cuda.h>` 等头文件可解析；用 `-isystem` 而非 `-I`，让 CUDA 头按系统头处理、不产生告警。

它还被传给多条特性通道：

[pegainfer-kernels/build.rs:L2227-L2230](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2227-L2230) —— `glm52` 特性开启时，`cuda_include` 作为参数传入 `build_glm52_cutedsl_fp8_dsl`（cute-dsl 宿主侧构建，内部以头目录参数的形式消费）。

[pegainfer-kernels/build.rs:L2743-L2752](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2743-L2752) —— TileLang 家族（gemma4）的 nvcc 任务同样接收 `&cuda_include` 编译生成的 CUDA 本体；紧随其后的 W4A16 批次（L2754-L2761）与 `k3` 特性的 `K3_TILELANG` 批次（L2767-L2775）走同一通道。

还有一个「软兜底之后有硬断言」的对照点。`qwen35` 特性开启时，Triton AOT 的宿主侧包装由 cc 编译（注释明说：不像 nvcc，cc 自己找不到 cuda.h）：

[pegainfer-kernels/build.rs:L1321-L1327](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L1321-L1327) —— `compile_triton_aot_kernels` 对 `cuda_include.join("cuda.h")` 做 `assert!`，失败文案直接给出动作提示「set CUDA_HOME to a CUDA toolkit root」；调用点在 main 的 [L2874-L2875](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2874-L2875)。也就是说：若 L2211-L2213 的软兜底把目录指到了一个没有 `cuda.h` 的位置，失败会被推迟到这里，以一条带修复提示的 panic 报出——「探测层软失败、必需依赖处硬失败」的两段式设计。

#### 4.2.4 代码实践

1. **实践目标**：用「断言推演」确认你理解了 `header_dir` 的按文件探测与优先级语义。
2. **操作步骤**：阅读 [conda_layout 测试（L208-L224）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L208-L224)与 [symlinked_dirs_dedupe 测试（L236-L258）](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L236-L258)，然后回答两个问题（先自己推，答案见练习 1、3）：
   - 在 conda 测试树上调用 `tk.header_dir("cuda_runtime.h")` 返回什么？
   - 在符号链接测试树上，若把 `root/include` 符号链接替换为真目录并放入 `cuda.h`，`header_dir("cuda.h")` 返回哪个路径？
3. **需要观察的现象**：两问的答案都能只靠源码语义推出，不需要运行。
4. **预期结果**：第一问 `None`（探测的是**具体文件**，树里只有 `cuda.h`，两个候选目录下都没有 `cuda_runtime.h`）；第二问 `root/include`（classic 候选排在列表最前且包含该文件，`find` 取第一命中）。想动手验证的话，可在第 5 节综合实践的 scratch 程序里加两行调用——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`header_dir` 是「按目录探测」还是「按文件探测」？这带来什么行为差异？

> 参考答案：按**文件**探测——`dir.join(header).is_file()`，header 名字是参数。所以同一棵 conda 树上，`header_dir("cuda.h")` 命中，`header_dir("cuda_runtime.h")` 却可能返回 `None`（文件不存在时）。调用方传的必须是「你想 include 的那个头文件的名字」。

**练习 2**：为什么 `header_dir` 用 `find`（第一个命中）而不是返回全部命中目录的列表？

> 参考答案：候选列表的顺序（classic → targets）已经编码了优先级（u2-l3 的组装顺序），第一个命中即最优选择；消费方（nvcc `-isystem`、宿主编译器 `-I`）也只需要一个目录。返回列表会把「优先级裁决」推给每个消费方重复做，还容易导出不一致的 `-I` 顺序。

**练习 3**：kernels 在 L2211-L2213 的 `unwrap_or_else(|| toolkit.root.join("include"))` 兜底，在 conda 布局下指向哪里？会有什么后果？

> 参考答案：指向 `root/include`——conda 布局下这个目录存在但缺 `cuda.h`。后果是 nvcc 的 `-isystem` 指向一个没有 CUDA 头的目录，编译 `.cu` 时由编译器报「cuda.h: No such file or directory」；若同时开了 `qwen35`，则会更早在 Triton AOT 的 `assert!`（L1323-L1327）处以带「set CUDA_HOME」提示的 panic 失败。兜底把「没装 CUDA」的失败点从 build script 开头推迟到真正需要头文件的地方；诊断路径的深入分析在 u3-l2。

### 4.3 link_search：把库目录发射成链接搜索指令

#### 4.3.1 概念说明

探测的最后一环是**输出**：把私有字段 `lib_dirs` 变成 Cargo 能理解的链接搜索指令。`link_search()` 做两件事：

1. **空列表告警**：`lib_dirs` 为空意味着这个根下没有任何已知布局的库目录（比如 4.1 里 `unknown_layout_yields_nothing` 的场景）。此时函数**只告警、不 panic**——随后 link 指令（`cargo:rustc-link-lib=cudart` 等）发出的链接任务可能靠系统默认路径成功，也可能失败，那是消费方与链接器的事。这与 `discover()` 对无效根的软失败（u2-l1）、`find_package` 的硬 panic（u2-l5）构成三种失败策略的对照。
2. **逐目录发射指令**：对每个库目录打印一行 `cargo:rustc-link-search=native=<dir>`，`native` 表示「本机平台的库格式」（Linux 上即 `.so`/`.a` 的搜索路径，等价 rustc 的 `-L native=`）。

`lib_dirs` 之所以设计成私有字段（L72），就是为了让 `link_search()` 成为它的**唯一出口**：消费方无法绕过空列表告警直接读列表、也无法拿到未收敛的原始候选——告警语义由库统一保证。

#### 4.3.2 核心流程

```text
link_search():
    若 lib_dirs 为空:
        打印 cargo:warning=no CUDA library dir found under <root>
    对 lib_dirs 中每个目录（去重后的优先级顺序）:
        打印 cargo:rustc-link-search=native=<dir>
```

注意与消费方的分工：`link_search` 只负责「去哪找」（search path）；「链接哪些库」由消费方随后自行发射 `cargo:rustc-link-lib`——库名单取决于它的产物构成，pegainfer-build 无从知晓。

#### 4.3.3 源码精读

`link_search` 本体只有 11 行：

[pegainfer-build/src/lib.rs:L132-L142](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L132-L142) —— L133-L138 是空列表告警分支（软失败）；L139-L141 对每个库目录发射一行 `cargo:rustc-link-search=native=...`。得益于 4.1 的去重，同一物理目录绝不会产生两行指令。

主要消费方 kernels 在 main 的**末尾**调用它，且 Windows 走特例：

[pegainfer-kernels/build.rs:L2882-L2890](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2882-L2890) —— 先为 `OUT_DIR`（静态库 `libkernels_cuda.a` 所在地）发射一条搜索路径；Windows 上工具包库在 `root/lib/x64`（MSVC 约定布局），kernels 直接打印该目录而不调用 `link_search()`；非 Windows 才交给 `toolkit.link_search()` 发射探测到的全部库目录。

紧接着的库名单展示了 search 与 link 两类指令的配合：

[pegainfer-kernels/build.rs:L2891-L2899](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2891-L2899) —— 在 `link_search()` 铺好的搜索路径上，点名链接 `kernels_cuda`（静态）、`cudart`、`cublas`、`cublasLt`、`cuda`、`nvrtc`。这 6 条是无条件的基础名单；真正条件性的是后面的 NCCL（`moe` 特性经 `link_deepep_nccl`，L2900-L2902）与 `stdc++`（非 Windows，L2903-L2905）。没有前面的 search path，这些 `-l` 只会去系统默认路径找。

另一个消费点作对照——kernel-lab 共享库不走 `link_search`，而是直接用 `toolkit.root` 拼路径：

[pegainfer-kernels/build.rs:L927-L944](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L927-L944) —— `link_kernel_lab_shared` 接收整个 `&pegainfer_build::CudaToolkit`，给 nvcc 链接参数传 `-L{toolkit.root}/lib64`。这是消费方按需直取公有 `root` 字段的例子：给外部进程传 `-L` 参数本就不需要 `cargo:` 指令。

#### 4.3.4 代码实践

1. **实践目标**：梳理 kernels 里「search path → 库名单」的指令序列，理解 `link_search` 在完整链接链路中的位置。
2. **操作步骤**：打开 [build.rs:L2882-L2905](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2882-L2905)，把每一条 `println!("cargo:...")` 分类抄成两列：左列「搜索路径类」（`rustc-link-search`），右列「库名类」（`rustc-link-lib`）；再给每条库名标注来源（基础名单 / feature 条件 / 平台条件）。
3. **需要观察的现象**：搜索路径类指令有三种来源——`OUT_DIR`、Windows 特例的 `root/lib/x64`（或非 Windows 平台 `toolkit.link_search()` 发出的多行）；库名类 6 条基础名单加上条件性的 NCCL 与 `stdc++`。
4. **预期结果**：得到一张「先铺路、后点名」的对照表，能回答「`libcudart` 从哪里被找到」（`link_search()` 发射的 toolkit 库目录之一，或系统默认路径）。纯源码阅读即可完成，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`lib_dirs` 为空时 `link_search()` 输出几条指令？构建会立刻失败吗？

> 参考答案：输出 1 条 `cargo:warning`（提示根目录下没有库目录）、0 条 `rustc-link-search`。构建不会立刻失败——这是软失败设计；若后续 `rustc-link-lib=cudart` 等能在系统默认路径找到库，链接仍会成功。

**练习 2**：为什么 `link_search()` 只发射 `rustc-link-search` 而不把 `cudart`、`cublas` 等库名也一起发射？

> 参考答案：职责分离。哪些库需要链接取决于消费方的产物构成（kernels 的 L2891-L2899 是基础名单，NCCL、`stdc++` 等条件项随 feature 与平台变化），pegainfer-build 作为探测库无从知晓；它只对「工具包里有哪些目录」负责。把库名单塞进探测库会让两个 crate 强耦合。

**练习 3**：Windows 上 kernels 为什么不调用 `link_search()`？

> 参考答案：Windows 的 CUDA 工具包按 MSVC 约定把库放在 `root/lib/x64`（还有 Win32 等子目录），`from_root` 的候选列表（`lib64`/`lib`/`targets`）是为 Unix 布局设计的；kernels 直接打印 `root/lib/x64`（L2883-L2887）更直白。这也说明 `link_search` 的候选列表是「约定俗成的 Unix 布局清单」，不是普适真理。

## 5. 综合实践

把本讲三个环节串起来：亲手搭一棵 conda 风格的符号链接树，验证收敛、`header_dir` 与 `link_search` 的全部行为。全程不需要 GPU/CUDA，也不改动仓库源码。

1. **实践目标**：复现 `symlinked_dirs_dedupe` 测试的场景并扩展两个对照实验，观察「符号链接去重 vs 混合布局」「非空输出 vs 空输出」的差异。

2. **操作步骤**：

   在仓库**外**新建一个 scratch 项目（不要写进仓库）：

   ```bash
   cargo new --bin dedupe-lab
   cd dedupe-lab
   # 在 Cargo.toml 的 [dependencies] 加一行: tempfile = "3"
   ```

   把 `dedupe-lab/src/main.rs` 替换为下面的**示例代码**（探测函数体复制自 `pegainfer-build/src/lib.rs` 的对应实现，仅去掉与本实验无关的 nvcc/HPC SDK 分支，把 `main` 换成搭树与打印逻辑）：

   ```rust
   // 示例代码：复制自 pegainfer-build/src/lib.rs 的探测实现，用于本地观察
   use std::path::PathBuf;

   fn target_dirs() -> Vec<String> {
       let arch = std::env::var("CARGO_CFG_TARGET_ARCH")
           .unwrap_or_else(|_| std::env::consts::ARCH.to_string());
       match arch.as_str() {
           "aarch64" => vec!["aarch64-linux".to_string(), "sbsa-linux".to_string()],
           arch => vec![format!("{arch}-linux")],
       }
   }

   fn existing_deduped(dirs: Vec<PathBuf>) -> Vec<PathBuf> {
       let mut seen = Vec::new();
       let mut out = Vec::new();
       for dir in dirs {
           if !dir.is_dir() {
               continue;
           }
           let canon = dir.canonicalize().unwrap_or_else(|_| dir.clone());
           if !seen.contains(&canon) {
               seen.push(canon);
               out.push(dir);
           }
       }
       out
   }

   struct CudaToolkit {
       root: PathBuf,
       include_dirs: Vec<PathBuf>,
       lib_dirs: Vec<PathBuf>,
   }

   impl CudaToolkit {
       fn from_root(root: PathBuf) -> Self {
           let mut include_dirs = vec![root.join("include")];
           let mut lib_dirs = vec![root.join("lib64"), root.join("lib")];
           for target in target_dirs() {
               include_dirs.push(root.join(format!("targets/{target}/include")));
               lib_dirs.push(root.join(format!("targets/{target}/lib")));
           }
           Self {
               include_dirs: existing_deduped(include_dirs),
               lib_dirs: existing_deduped(lib_dirs),
               root,
           }
       }

       fn header_dir(&self, header: &str) -> Option<PathBuf> {
           self.include_dirs
               .iter()
               .find(|dir| dir.join(header).is_file())
               .cloned()
       }

       fn link_search(&self) {
           if self.lib_dirs.is_empty() {
               println!("cargo:warning=no CUDA library dir found under {}", self.root.display());
           }
           for dir in &self.lib_dirs {
               println!("cargo:rustc-link-search=native={}", dir.display());
           }
       }
   }
   ```

   `main` 分三个场景（接在同一个文件里）：

   ```rust
   // 示例代码（续）：三个观察场景
   fn main() {
       // 场景一：conda 符号链接布局（复刻 symlinked_dirs_dedupe 测试）
       let tree = tempfile::tempdir().unwrap();
       let root = tree.path().to_path_buf();
       let target = target_dirs().remove(0);
       let inc = root.join(format!("targets/{target}/include/cuda.h"));
       std::fs::create_dir_all(inc.parent().unwrap()).unwrap();
       std::fs::write(&inc, b"").unwrap();
       std::fs::create_dir_all(root.join(format!("targets/{target}/lib"))).unwrap();
       std::os::unix::fs::symlink(
           root.join(format!("targets/{target}/include")),
           root.join("include"),
       )
       .unwrap();
       std::os::unix::fs::symlink(
           root.join(format!("targets/{target}/lib")),
           root.join("lib"),
       )
       .unwrap();

       let tk = CudaToolkit::from_root(root.clone());
       assert_eq!(tk.include_dirs.len(), 1, "符号链接去重后 include 只剩一项");
       assert_eq!(tk.lib_dirs.len(), 1, "符号链接去重后 lib 只剩一项");
       assert_eq!(tk.header_dir("cuda.h"), Some(root.join("include")));
       println!("场景一 include_dirs = {:?}", tk.include_dirs);
       println!("场景一 lib_dirs     = {:?}", tk.lib_dirs);
       tk.link_search(); // 应恰好一行 rustc-link-search

       // 场景二：混合布局 —— include 换成真目录并放 cuda.h，再补一个真 lib64
       std::fs::remove_file(root.join("include")).unwrap();
       std::fs::create_dir_all(root.join("include")).unwrap();
       std::fs::write(root.join("include/cuda.h"), b"").unwrap();
       std::fs::create_dir_all(root.join("lib64")).unwrap();
       let tk2 = CudaToolkit::from_root(root.clone());
       println!("场景二 include_dirs = {:?}", tk2.include_dirs);
       println!("场景二 lib_dirs     = {:?}", tk2.lib_dirs);
       println!("场景二 header_dir   = {:?}", tk2.header_dir("cuda.h"));
       tk2.link_search(); // 库目录应为 lib64 与 lib（符号链接）两项

       // 场景三：空树 —— 触发告警分支
       let empty = tempfile::tempdir().unwrap();
       CudaToolkit::from_root(empty.path().to_path_buf()).link_search();
   }
   ```

   运行：

   ```bash
   cargo run --release
   ```

3. **需要观察的现象**：

   - 场景一：`include_dirs`、`lib_dirs` 各只有一项，且是**符号链接路径**（`.../include`、`.../lib`，不是 `.../targets/<arch>/...`）；`link_search` 恰好输出一行 `cargo:rustc-link-search=native=...`。
   - 场景二：`include_dirs` 变成两项（真 `include` 与 `targets/.../include` 是两个物理目录，不去重）；`header_dir("cuda.h")` 返回真 `include`（classic 优先）；`lib_dirs` 为 `[.../lib64, .../lib]` 两项，输出两行 link-search（targets 下的 lib 仍被符号链接去重吸收）。
   - 场景三：先输出一行 `cargo:warning=no CUDA library dir found under ...`，随后没有任何 link-search 行。

4. **预期结果**：三个断言全部通过、输出与上述现象一致。行为依据是仓库内已有的 `symlinked_dirs_dedupe`（L236-L258）、`conda_layout`（L208-L224）、`unknown_layout_yields_nothing`（L260-L269）三个测试与 4.1-4.3 的源码语义；编写本讲义时未在本地执行此 scratch 程序，**待本地验证**。

5. **思考题（选做）**：场景一里 `link_search` 输出的那一行用的是符号链接路径。仅凭这行输出，你能判断出「它曾被去重过」吗？（提示：不能——输出只剩原始路径，这正是 4.1.1「规范化身份作键、原始路径作输出」的设计选择。）

## 6. 本讲小结

- `existing_deduped`（[lib.rs:L145-L159](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L145-L159)）承担探测的收敛：`is_dir()` 过滤不存在候选，`canonicalize` 身份去重符号链接造成的重复；键用规范化身份、输出保留首次出现的原始路径，候选顺序（classic → targets → math_libs）即优先级。
- `header_dir(header)`（[lib.rs:L123-L130](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L123-L130)）按**具体文件**探测、取第一命中，解决 conda「`include/` 存在但缺 CUDA 头」的问题；kernels 用它拿 `cuda.h` 目录，经 `-isystem` 进 nvcc（build.rs:2353-2354），并流向 glm52 cute-dsl、TileLang（gemma4/k3）与 Triton AOT 通道；Triton AOT 处还有一道对 `cuda.h` 的硬断言（build.rs:1321-1327）。
- `link_search()`（[lib.rs:L132-L142](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-build/src/lib.rs#L132-L142)）是私有 `lib_dirs` 的唯一出口：空列表只告警不 panic（软失败），非空则逐目录发射 `cargo:rustc-link-search=native=...`；库名单由消费方自己发射，职责分离。
- 消费方形态各异：kernels 非 Windows 走 `link_search()`、Windows 走 `root/lib/x64` 特例（build.rs:2882-2890）；kernel-lab 直接用 `toolkit.root` 拼 `-L`（build.rs:927-944）——探测结果的出口设计（`root`/`nvcc`/`include_dirs` 公有 + `lib_dirs` 经方法输出）恰好覆盖这三类用法。
- 失败策略分层并存：`discover` 的无效根告警、`link_search` 的空列表告警是「探测层」软失败；kernels 对 `header_dir` 的兜底把失败推迟到真正消费处（nvcc 报错或 Triton AOT 断言）；`find_package` 则是「必需依赖」的硬 panic（u2-l5 展开）。

## 7. 下一步学习建议

- 下一讲 **u2-l5（find_package：通用包探测模板与历史演变）**：看 `#[cfg(test)]` 里的 `find_package` 如何把「候选 × 检查文件」的探测思路推广成通用模板，以及它从 `pub` 降级为测试专用的历史（`git log --follow` 追溯）。
- 之后进入 **u3-l2（消费方集成）**：从 kernels 的视角把本讲的 `header_dir` 兜底（build.rs:2209-2213）、`link_search` 调用（build.rs:2882-2890）与 nvcc 编译、静态库打包、链接全链路串成一张数据流图。
- 若想巩固本讲的测试手法，**u3-l1（无 GPU 环境下的确定性测试设计）** 会系统拆解 `TempTree` 夹具与五个布局场景测试的设计意图，本讲的 `symlinked_dirs_dedupe` 正是其中之一。
