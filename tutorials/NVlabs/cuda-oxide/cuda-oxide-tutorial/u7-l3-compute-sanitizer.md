# NVIDIA Compute Sanitizer 正确性检查（cargo oxide sanitize）

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `cargo oxide sanitize <example>` 把一个 cuda-oxide 程序跑在 NVIDIA Compute Sanitizer 下。
- 说清四种工具 `memcheck` / `racecheck` / `initcheck` / `synccheck` 各自查什么、什么时候该用哪一个。
- 准确描述 sanitizer 参数与目标程序参数的分隔约定（第一个 `--` 与第二个 `--`），不再猜哪段参数传给了谁。
- 解释为什么 `cargo oxide sanitize` 默认会注入 `--error-exitcode 86`、以及它在 CI 里"以退出码判定成败"的意义与陷阱。
- 看懂 `cargo-oxide` 是如何发现 `compute-sanitizer`、如何在保持优化的同时打开设备行表（line-tables），从而让报告里出现 Rust 源码文件名与行号的。

## 2. 前置知识

本讲是 **专家层（advanced）** 讲义，承接两篇前置讲义：

- **u1-l3 工具链与 cargo-oxide 驱动**：你已经知道 `cargo oxide` 是一个自定义 cargo 子命令，靠 `.cargo/config.toml` 别名或 `cargo-xxx` 约定被发现；知道 `doctor` 用零副作用的 `resolve_doctor_context` 给机器做体检；知道后端 `librustc_codegen_cuda.so` 走 5 步发现链定位。本讲里的 `compute-sanitizer` 发现机制与这套思路一脉相承。
- **u7-l2 cuda-gdb 调试与设备端 debug intrinsics**：你已经知道 `CUDA_OXIDE_DEBUG=line-tables` 能在不关优化的前提下给设备 PTX 加一张"机器指令 → Rust 源码行"的映射表，让调试器/工具能把错误归因到源码。本讲的 `sanitize` 默认就会打开这张表。

如果你还不知道下面这些 cuda-oxide 基础术语，建议先回到 U1/U2 单元补课：

| 术语 | 一句话解释 |
|:-----|:-----------|
| `#[cuda_module]` / `#[kernel]` | 过程宏：标注一个模块为"设备代码容器"，其中的 `#[kernel]` 函数会被编译成 PTX 入口 |
| host / device | 单源编译：同一份 `.rs` 同时产出宿主 x86_64 机器码与内嵌 PTX |
| `DisjointSlice` | 越界安全的并行写入切片，`get_mut(idx)` 返回 `Option<&mut T>` |
| raw `LaunchConfig` | 未经证明的原始启动几何（grid/block/shared_mem），启动需 `unsafe` |
| 内存序 / 作用域 | 原子操作的两个正交维度，详见 u5-l2 |

接下来你会看到，**Compute Sanitizer 解决的是"GPU 内核不出错"的最后一块拼图**：cuda-gdb 帮你"停下来看现场"，而 Compute Sanitizer 帮你"在不改代码的前提下，自动把静默错误变成可定位的报告"。

## 3. 本讲源码地图

本讲全部围绕 **`cargo-oxide` 工具层 crate**，不涉及编译器内部。下表是本讲会引用的关键源码点：

| 文件 | 作用 | 本讲关注 |
|:-----|:-----|:---------|
| `crates/cargo-oxide/src/main.rs` | CLI 定义（clap）与分发 | `Sanitize` 子命令、`SanitizerTool` 枚举、参数分隔 |
| `crates/cargo-oxide/src/commands.rs` | 所有子命令的实现 | `codegen_sanitize`、`run_compute_sanitizer`、退出码注入、行表、工具发现 |
| `crates/cargo-oxide/README.md` | 用户文档 | `sanitize` 子命令的用法与示例 |
| `cuda-oxide-book/.../error-handling-and-debugging.md` | 教科书 | Compute Sanitizer 章节的官方叙述 |

一个宏观结论先放在这里：**`sanitize` 子命令 = `run` 的构建路径 + 把产物喂给 `compute-sanitizer`**。它复用了 `run` 的整套 codegen 后端发现、架构探测、`--no-fmad` 透传机制，唯一的增量是"在二进制外面再套一层 `compute-sanitizer` 进程"。理解这一点能让你把本讲的大量细节归位到一条主线。

## 4. 核心概念与源码讲解

### 4.1 sanitize 子命令与 SanitizerTool

#### 4.1.1 概念说明

NVIDIA **Compute Sanitizer** 是 CUDA Toolkit 自带的一组"运行时正确性检查器"。它的工作方式很朴素：在你的程序和 GPU 驱动之间插入一层拦截，劫持每一次内核启动、每一次内存访问、每一次同步调用，按选定的规则检查有没有违规，再把违规连同源码位置打印出来。

它不是编译期检查，**必须真的把内核跑起来**才能发现问题——这正是 cuda-oxide 用 `cargo oxide sanitize` 一条命令"构建 + 运行 + 检查"的原因。

Compute Sanitizer 提供 **四种工具**，每种盯一类错误，互不替代：

| 工具 | 检查目标 | 典型错误 |
|:-----|:---------|:---------|
| `memcheck` | 内存访问正确性（默认） | 越界读写、对齐错误、空指针、内存泄漏（配合 `--leak-check`） |
| `racecheck` | 共享内存（shared memory）数据竞争 | 同一 warp/CTA 内对共享内存的并发读写未加同步 |
| `initcheck` | 全局内存未初始化读 | 读了一个从未写过的设备内存字节 |
| `synccheck` | 同步原语的错误使用 | `__syncthreads` 的线程到达数不匹配、barrier 滥用 |

NVIDIA 自己的建议是 **把它们当互补工具**：`racecheck` / `initcheck` / `synccheck` **都不做** 完整的内存访问检查，所以排查内存安全问题应优先用 `memcheck`，其余工具按症状补充。

#### 4.1.2 核心流程

`sanitize` 的端到端流程可以画成三段：

```text
cargo oxide sanitize <example> [--tool <T>] [-- ...]
        │
        ▼
[1] 构建（与 cargo oxide run 同路径）
    ├── 解析 example 名（workspace 必填 / standalone 用当前目录名）
    ├── 探测目标架构（--arch > CUDA_OXIDE_TARGET > 探测 GPU）
    ├── touch_main_rs + codegen_build_host_binary
    │      └── 默认注入 CUDA_OXIDE_DEBUG=line-tables（行表）
    └── 产物：release 可执行文件路径
        │
        ▼
[2] 发现 compute-sanitizer
    PATH → CUDA toolkit root → /usr/local/cuda 等回退路径
        │
        ▼
[3] 组装并启动 compute-sanitizer 进程
    compute-sanitizer --tool <T> --error-exitcode 86 [用户 sanitizer 参数] <binary> [用户应用参数]
    └── 失败（exit≠0）→ 透传退出码；成功 → 提示"请人工阅读报告"
```

第 [1] 段就是 `run` 的代码，本讲不重复；第 [2][3] 段是 `sanitize` 独有的增量，下文逐节拆。

#### 4.1.3 源码精读

CLI 层在 `main.rs` 里用 clap 定义了 `Sanitize` 变体。注意三个细节：`--tool` 用 `value_enum` 且默认值是 `Memcheck`；`sanitizer_args` 用 `last = true` 捕获 `--` 之后的所有参数；`--tool` 文档明示了四种取值。

[cargo-oxide/src/main.rs:L81-L110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L81-L110) — `Sanitize` 子命令的 clap 定义：`--tool` 默认 `Memcheck`，`sanitizer_args` 用 `#[arg(last = true, ...)]` 收集第一个 `--` 之后的所有内容。

四种工具本身只是一个 `ValueEnum`：

[cargo-oxide/src/main.rs:L250-L267](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L250-L267) — `SanitizerTool` 枚举与 `as_str()`：把 Rust 风格的 `Memcheck` 映射成 `compute-sanitizer` 期望的小写 `memcheck`。

`main()` 收到 `Sanitize { .. }` 后，先做 example 名解析，再调用 `split_sanitizer_and_application_args` 把"sanitizer 参数"与"应用参数"切开，最后交给 `commands::codegen_sanitize`：

[cargo-oxide/src/main.rs:L334-L360](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L334-L360) — 分发逻辑：把 `tool.as_str()`、切好的 `sanitizer_args` 与 `application_args` 一起传下去。

`codegen_sanitize` 是实现入口。它有两条路径：interop（含 `device-crates` 的 Tile/SIMT 互操作构建）和普通构建。两条路径最终都汇聚到同一个 `run_compute_sanitizer`：

[cargo-oxide/src/commands.rs:L276-L366](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L276-L366) — `codegen_sanitize`：构建二进制（普通或 interop）后，统一调用 `run_compute_sanitizer`。注意 interop 分支会打印 `SANITIZE INTEROP` 横幅并把 `sanitizer_line_tables: true` 透传给设备 crate 构建。

#### 4.1.4 代码实践

实践目标：在不接 GPU 的机器上，仅凭源码确认 `--tool` 的默认值，并验证四种工具名能被解析。

操作步骤：

1. 打开上面的 `SanitizerTool` 枚举源码链接，确认 `Memcheck => "memcheck"` 的映射。
2. 阅读同文件里的单元测试 `sanitize_parser_defaults_to_memcheck` 与 `sanitize_parser_accepts_tool_and_trailing_sanitizer_args`：
   - [cargo-oxide/src/main.rs:L624-L649](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L624-L649) — `sanitize_parser_accepts_tool_and_trailing_sanitizer_args`：断言 `--tool racecheck` 解析成功、`--` 之后的 `--kernel-name kns=vecadd` 收入 `sanitizer_args`。
   - [cargo-oxide/src/main.rs:L672-L680](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L672-L680) — `sanitize_parser_defaults_to_memcheck`：断言省略 `--tool` 时 `tool == SanitizerTool::Memcheck`。
3. 在 `crates/cargo-oxide/` 下运行 `cargo test sanitize_parser`，确认以下行为：未指定 `--tool` 时默认是 `Memcheck`、`--tool racecheck` 能解析、`--` 之后的 `--kernel-name kns=vecadd` 被原样收入 `sanitizer_args`。

需要观察的现象：测试通过，断言确认未指定 `--tool` 时 `tool == SanitizerTool::Memcheck`。

预期结果：四个相关测试全绿；这相当于在没有 GPU 的情况下"证明"了 CLI 解析层的行为。

如果你没有该工具链环境，可标注「待本地验证」后改为纯阅读型实践：手动对照枚举与测试，在纸上写出 `cargo oxide sanitize vecadd --tool synccheck` 这条命令解析后 `tool` 与 `sanitizer_args` 的值。

#### 4.1.5 小练习与答案

**练习 1**：如果你的内核偶尔读到错误的共享内存值，应该优先用哪种工具？为什么不是 `memcheck`？

**参考答案**：优先用 `racecheck`。`racecheck` 专门盯共享内存的数据竞争，能指出哪两个线程的读写未加同步；而 `memcheck` 查的是地址合法性（越界/对齐/未分配），对"地址合法但访问无序"通常不报错。NVIDIA 文档明确：`racecheck`/`initcheck`/`synccheck` 互补且都不做完整内存访问检查。

**练习 2**：下面这条命令会以哪个工具运行？

```bash
cargo oxide sanitize sharedmem
```

**参考答案**：`memcheck`。`--tool` 未指定时取默认值 `SanitizerTool::Memcheck`（见 `main.rs` 的 `default_value_t`）。要换工具必须显式 `--tool racecheck`。

---

### 4.2 参数分隔约定：sanitizer 参数 vs 应用参数

#### 4.2.1 概念说明

`compute-sanitizer` 的原生命令行长这样：

```text
compute-sanitizer [options] [your-program] [your-program-options]
```

也就是说，它**自己**要吃一组选项（如 `--tool`、`--leak-check full`），**然后**才是你的程序路径，**再然后**才是传给你程序的参数。两层"参数"叠在一起，最容易踩的坑就是"我加的 `--foo` 到底给了 sanitizer 还是给了我的程序？"

`cargo oxide sanitize` 的设计是：用**两个 `--`** 把这两层隔开。

- 第一个 `--`：之后的内容全部交给 `compute-sanitizer`（在二进制之前）。
- 第二个 `--`：之后的内容才传给你的程序（在二进制之后）。

#### 4.2.2 核心流程

CLI 层先用 clap 的 `last = true` 把第一个 `--` 之后的所有 token 收进 `sanitizer_args`。然后 `split_sanitizer_and_application_args` 在这个集合里**再找一次 `--`**，按它切两段：

```text
原始:              --leak-check full -- --case oob --verbose-target
                    ↑ sanitizer 参数 ↑   ↑ 应用参数 ↑
                                          （此 -- 之前都属于 sanitizer）
```

伪代码：

```text
fn split(args):
    找到 args 中第一个 "== --" 的位置 separator
    if 找到:
        sanitizer_args = args[..separator]
        application_args = args[separator+1..]
    else:
        sanitizer_args = args   # 全部给 sanitizer
        application_args = []
```

最终在 `run_compute_sanitizer` 里组装成：

```text
compute-sanitizer --tool <T> [--error-exitcode 86] <sanitizer_args> <binary> <application_args>
```

#### 4.2.3 源码精读

[cargo-oxide/src/main.rs:L269-L274](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L269-L274) — `split_sanitizer_and_application_args`：用 `position` 找第一个 `--`，前面给 sanitizer、后面给应用。找不到就把全部当 sanitizer 参数。

对应的测试把这种"双重分隔"固化成断言：

[cargo-oxide/src/main.rs:L651-L669](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L651-L669) — `sanitize_args_split_at_second_separator_for_application_args`：输入 `["--leak-check","full","--","--case","oob","--verbose-target"]`，断言 sanitizer 段是 `["--leak-check","full"]`，应用段是 `["--case","oob","--verbose-target"]`。

组装动作在 `run_compute_sanitizer`：

[cargo-oxide/src/commands.rs:L1156-L1161](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1156-L1161) — 命令组装顺序：先 `--tool <T>`，再 `invocation_args.args`（含默认退出码与用户 sanitizer 参数），再 `binary`，最后 `application_args`。这正好对应 `compute-sanitizer [options] [program] [program-options]` 的形态。

README 也给了最直观的示例，便于记忆：

[cargo-oxide/README.md:L110-L120](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/README.md#L110-L120) — 两层 `--` 的真实示例：`-- --leak-check full -- --app-flag value` 表示 sanitizer 加 `--leak-check full`、程序加 `--app-flag value`。

#### 4.2.4 代码实践

实践目标：亲手验证"参数最终归属"，避免日后误把程序参数写成 sanitizer 参数。

操作步骤（无 GPU 也可做"阅读型"）：

1. 阅读上面的 `split_sanitizer_and_application_args` 源码与对应测试。
2. 在纸上手算下面三条命令各自的 `sanitizer_args` 与 `application_args`：

   ```text
   (a) cargo oxide sanitize vecadd -- --leak-check full
   (b) cargo oxide sanitize my_app -- --leak-check full -- --app-flag value
   (c) cargo oxide sanitize debug --tool synccheck -- --kernel-name kns=sync
   ```

3. 对照 README 的示例（链接见上）核对答案。

需要观察的现象 / 预期结果：

- (a) sanitizer=`["--leak-check","full"]`，application=`[]`。
- (b) sanitizer=`["--leak-check","full"]`，application=`["--app-flag","value"]`。
- (c) sanitizer=`["--kernel-name","kns=sync"]`，application=`[]`（注意 `--tool synccheck` 在第一个 `--` 之前，是 cargo-oxide 自己的 flag，不进入 sanitizer_args）。

如果你能在本地运行，把 (b) 跑在任意一个能启动的示例上，并加上 `-v` 观察 `cargo-oxide` 打印的 `Running compute-sanitizer --tool ... ...` 那一行，确认参数顺序与你的手算一致。无法运行则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：用户想给程序传一个 `--seed 42`，却写成了 `cargo oxide sanitize app -- --seed 42`。会发生什么？

**参考答案**：`--seed 42` 会被当成 `compute-sanitizer` 的选项（因为只有一个 `--`，落在 sanitizer 段）。`compute-sanitizer` 不认识 `--seed`，多半报错或忽略，程序根本收不到这个参数。正确写法是 `cargo oxide sanitize app -- -- --seed 42`（用第二个 `--` 切到应用段）。

**练习 2**：为什么 `--tool` 不需要写在第一个 `--` 之后？

**参考答案**：`--tool` 是 **`cargo-oxide` 自己**解析的 clap flag（见 `Sanitize` 变体定义），它由 `cargo-oxide` 翻译成 `--tool <as_str()>` 再传给 `compute-sanitizer`。第一个 `--` 之后的参数是"原样透传"给 `compute-sanitizer` 的，不经过 cargo-oxide 的 clap，所以 `--tool` 必须放在第一个 `--` 之前。

---

### 4.3 默认退出码注入：--error-exitcode 86 与"弱化"检测

#### 4.3.1 概念说明

这是本讲**最容易被忽略却最关键**的设计点。

Compute Sanitizer 的默认行为有个"坑"：**即使它发现了大量错误，进程也以退出码 0 退出**。这意味着如果你在 CI 里写：

```bash
cargo oxide sanitize vecadd && echo "ALL GOOD"
```

哪怕内核疯狂越界，CI 也会打印 `ALL GOOD`。原因详见官方文档与 README 的说明：

[cargo-oxide/README.md:L127-L134](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/README.md#L127-L134) — "Compute Sanitizer's own error exit code defaults to zero"，cargo-oxide 因此默认补 `--error-exitcode 86`。

`cargo-oxide` 的对策是：**只要你没有显式给 `--error-exitcode`，就自动在前面加上 `--error-exitcode 86`**，让任何工具发现（finding）都使进程非零退出，从而"挂掉"脚本和 CI。退出码 `86` 是 `cargo-oxide` 自己挑的约定常量。

但仅注入退出码还不够稳健，因为 sanitizer 另有两个选项会**弱化**退出码的语义：

- `--check-exit-code no`：让 sanitizer 不检查目标程序的退出码。
- `--require-cuda-init no`：让"目标程序未能完成 CUDA 初始化"也不算错误。

当用户显式传了这两个之一，"退出码 0"就**不能再证明报告干净**。所以 `cargo-oxide` 在收尾时会**主动提醒**："请人工阅读报告，退出码本身不是干净的证明。"

#### 4.3.2 核心流程

退出码处理用一个纯函数 `sanitizer_invocation_args` 完成决策，**不接触任何 I/O**，便于单测：

```text
fn sanitizer_invocation_args(用户 sanitizer 参数):
    if 用户已显式给 --error-exitcode（分离或等号形式）:
        → 原样返回用户参数，标记 uses_default_error_exitcode = false
    else:
        → 在参数前插入 ["--error-exitcode", "86"]，标记 = true
    再扫描: 是否含 --check-exit-code=no / --require-cuda-init=no
        → 标记 status_checks_weakened
```

收尾时根据这两个布尔标志打印不同提示：

| 情况 | 提示 |
|:-----|:-----|
| 用了默认 86 | （正常）报告完成，提醒仍要人工看报告 |
| 用户显式给了退出码 | "你给的退出码说了算" |
| 检测到弱化选项 | "你给的选项可能让目标/CUDA 初始化失败也退出 0" |

#### 4.3.3 源码精读

常量与决策结构：

[cargo-oxide/src/commands.rs:L1081-L1088](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1081-L1088) — `DEFAULT_SANITIZER_ERROR_EXITCODE = "86"` 与 `SanitizerInvocationArgs` 结构（含 `uses_default_error_exitcode`、`status_checks_weakened` 两个布尔）。

核心决策函数：

[cargo-oxide/src/commands.rs:L1090-L1115](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1090-L1115) — `sanitizer_invocation_args`：先判断有没有显式 `--error-exitcode`（同时识别分离形式 `--error-exitcode 0` 与等号形式 `--error-exitcode=0`）；没有就在头部插入默认 86。

弱化检测的细节（同时容忍大小写、分离/等号两种写法）：

[cargo-oxide/src/commands.rs:L1117-L1128](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1117-L1128) — `sanitizer_option_is_no`：判断某个 `--name` 是否被设为 `no`。

收尾提示，对应"退出码不再可靠"的人类语言：

[cargo-oxide/src/commands.rs:L1190-L1204](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1190-L1204) — 完成后按 `uses_default_error_exitcode` / `status_checks_weakened` 打印不同提醒，并始终强调"exit status alone is not a clean-report assertion"。

整个决策被密集的单元测试焊死，这是它最重要的特性——退出码语义不靠人脑记，靠测试钉：

[cargo-oxide/src/commands.rs:L4197-L4243](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L4197-L4243) — 三个测试：默认注入 86、显式 `--error-exitcode 0` 不被覆盖（含分离/等号/重复三种形态）、四种"弱化"写法都被识别。

#### 4.3.4 代码实践

实践目标：理解"退出码 0 ≠ 干净报告"，避免在 CI 里被假阴性坑到。

操作步骤：

1. 阅读上面三个测试，在心里跑一遍 `sanitizer_invocation_args(&["--error-exitcode=0".to_string()])`，确认它**不会**被改成 86。
2. 在你的 CI 脚本设计里，写下这样一条规则："`cargo oxide sanitize` 必须以非零退出码判定失败；若脚本里看到退出码 0，仍需 grep 报告里是否含 `========` Error 之类的 finding 标记。"
3. （可选，需 CUDA）故意写一个会越界的内核（见 4.6 综合实践），跑 `cargo oxide sanitize badkernel`，观察进程退出码是 86 而非 0。

需要观察的现象：sanitizer 报告里出现 `========= Invalid __global__ read of size ...` 之类的 finding，进程退出码为 86。

预期结果：因为内核确实越界，`memcheck` 必报，cargo-oxide 注入的 `--error-exitcode 86` 让进程以 86 退出。若你显式加 `-- --error-exitcode 0`，进程会以 0 退出但报告里仍有 finding——这正是"退出码不可靠"的演示。无 GPU 则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：用户故意写 `cargo oxide sanitize vecadd -- --error-exitcode 0`，cargo-oxide 会强行覆盖成 86 吗？

**参考答案**：不会。`sanitizer_invocation_args` 检测到显式 `--error-exitcode`（等号或分离形式都算）就直接原样返回用户参数，`uses_default_error_exitcode = false`。这也是测试 `sanitizer_preserves_explicit_zero_error_exitcode_without_claiming_detection` 锁定的行为。

**练习 2**：为什么收尾提示里要专门提 `--check-exit-code no` 和 `--require-cuda-init no`？

**参考答案**：这两个选项会让"目标程序异常退出"或"未完成 CUDA 初始化"不再触发非零退出码，从而让"退出码 0"含金量下降。cargo-oxide 检测到它们就把 `status_checks_weakened = true` 并打印提醒，防止用户被 0 退出码误导。即便如此，cargo-oxide **永远不**仅凭退出码断言报告干净——它只报告"完成了"，让你自己看报告。

---

### 4.4 compute-sanitizer 的发现与启动

#### 4.4.1 概念说明

`compute-sanitizer` 不是 Rust 编译出来的，它是 CUDA Toolkit 里的一个原生可执行文件（通常在 `$CUDA_HOME/bin/compute-sanitizer`）。所以 `cargo-oxide` 要在运行前**找到它**。

这与 u1-l3 讲过的"codegney 后端 `.so` 发现链"是同一类问题，但解法更简单，因为 `compute-sanitizer` 是个标准命令行工具，可以直接走 `PATH` 查找。`cargo-oxide` 还叠加了"CUDA toolkit root 推断"和"几个常见绝对路径"两道兜底，最大化在各种 Linux 安装方式下都能命中。

#### 4.4.2 核心流程

```text
find_cuda_toolkit_executable("compute-sanitizer", [回退路径]):
    1. find_executable("compute-sanitizer", [])   # 走 PATH
    2. cuda_toolkit_root(...) / bin / compute-sanitizer   # 推断 toolkit root
    3. 依次试回退路径: /usr/local/cuda/bin/..., /opt/cuda/bin/..., /usr/bin/...
    全部失败 → 打印 "compute-sanitizer not found"，提示跑 doctor，exit 1
```

找到后，`run_compute_sanitizer` 组装 `Command`，注入环境（`apply_config_env`、`apply_ld_library_path`），打印一行人类可读的 `Running compute-sanitizer ...`，然后 `.status()` 同步等待。

#### 4.4.3 源码精读

发现函数本体：

[cargo-oxide/src/commands.rs:L3562-L3589](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L3562-L3589) — `find_cuda_toolkit_executable`：三段式发现——`PATH` → `cuda_toolkit_root/bin/<name>` → 回退绝对路径。`cuda_toolkit_root` 会综合环境变量与项目配置 `[env]`（如 `CUDA_TOOLKIT_PATH`）。

启动与错误处理：

[cargo-oxide/src/commands.rs:L1130-L1153](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1130-L1153) — `run_compute_sanitizer` 头部：找不到 `compute-sanitizer` 时提示运行 `cargo oxide doctor` 检查 CUDA 安装，并 `exit(1)`。

命令组装与运行（与 4.2.3 引用的同一段，重点看 `.args(["--tool", tool])` 与 `.arg(binary)`）：

[cargo-oxide/src/commands.rs:L1155-L1188](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1155-L1188) — 组装命令、设置 `current_dir(example_dir)`、注入库搜索路径、打印启动横幅、`.status()` 等待并按 sanitizer 的退出码退出。

工具发现路径也被一个测试焊死——证明项目 `[env]` 里的 `CUDA_TOOLKIT_PATH` 会被采纳：

[cargo-oxide/src/commands.rs:L4317-L4344](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L4317-L4344) — `sanitizer_tool_lookup_uses_project_cuda_toolkit_root`：构造临时 toolkit root，证明 `find_cuda_toolkit_executable` 会在 `CUDA_TOOLKIT_PATH/bin` 下找到工具。

#### 4.4.4 代码实践

实践目标：确认你这台机器能否被 `cargo-oxide` 找到 `compute-sanitizer`，以及它会被解析成哪个绝对路径。

操作步骤：

1. 运行 `cargo oxide doctor`，看 "CUDA toolkit" 与相关检查项是否报告 `compute-sanitizer` 所在的 toolkit root（doctor 逻辑见 u1-l3）。
2. 直接在终端 `which compute-sanitizer` 或 `ls /usr/local/cuda/bin/compute-sanitizer`。
3. 如果都不在 `PATH`，按 4.4.2 的发现顺序，在 `.cargo/cuda-oxide.toml` 的 `[env]` 里设 `CUDA_TOOLKIT_PATH = "/your/cuda"`，再让 sanitize 走第 2 段（toolkit root）发现。

需要观察的现象：`cargo oxide sanitize <可运行示例>` 启动时打印的 `Running compute-sanitizer --tool ...` 行里，能看到它确实拉起了一个可执行文件。

预期结果：在有 CUDA Toolkit 的机器上，发现成功；否则报 `compute-sanitizer not found` 并提示跑 `doctor`。无 GPU/无 Toolkit 的机器标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：发现顺序里为什么把 `PATH` 放在 toolkit root 之前？

**参考答案**：让用户能通过修改 `PATH` 用一个自定义/升级版的 `compute-sanitizer` 覆盖 toolkit 自带版本，符合"用户显式选择优先"的惯例（与 u1-l3 讲的 `CUDA_OXIDE_BACKEND` 等显式覆盖优先级一致）。toolkit root 与回退路径是"用户没显式指定时的合理猜测"。

**练习 2**：如果 `compute-sanitizer` 既不在 `PATH`，toolkit root 也推断不出来，会发生什么？

**参考答案**：`find_cuda_toolkit_executable` 返回 `None`，`run_compute_sanitizer` 用 `unwrap_or_else` 打印 "Error: compute-sanitizer not found."，提示运行 `cargo oxide doctor`，然后 `exit(1)`——绝不在没有工具的情况下静默跳过检查。

---

### 4.5 行表（line-tables）默认开启与编译指纹

#### 4.5.1 概念说明

Compute Sanitizer 报告里如果只有 PTX 汇编地址，对 Rust 程序员几乎没用——你需要它告诉你"出错的是 `src/main.rs:42`"。这就需要**设备行表**：一张从 PTX 指令地址到 Rust 源码行的映射表（u7-l2 已讲过 `CUDA_OXIDE_DEBUG=line-tables` 的含义）。

`cargo oxide sanitize` 的贴心之处在于：**它默认就帮你打开行表**，而**不关闭普通优化**。也就是说，你拿到的是"优化的 PTX + 一张行表"，既不至于像 `full` debug 那样拖慢 GPU，又能让 sanitizer 报告归因到源码。

但有个工程难点：**Cargo 不会给任意的后端环境变量做指纹**。如果 `cargo-oxide` 只是设一个 `CUDA_OXIDE_DEBUG=line-tables` 环境变量，Cargo 不会因为"我要 sanitize"就重新编译——它会复用上一次 `run` 留下的、没有行表的产物。于是 `cargo-oxide` 的解法是：**把 sanitize 专属的输出设置揉进一个特殊的 `--cfg` 哈希**，强制 Cargo 重编。

#### 4.5.2 核心流程

```text
普通 sanitize 构建路径（codegen_build_host_binary）:
    apply_common_codegen_env(...)               # no_fmad 等通用设置
    apply_default_sanitizer_line_tables(cmd)    # 设 CUDA_OXIDE_DEBUG=line-tables（若用户没设）
    fingerprint = sanitize_codegen_fingerprint_cfg(...)   # 算一个 --cfg 哈希
    apply_codegen_rustflags(..., &[fingerprint])          # 把哈希作为 --cfg 注入
```

`apply_default_sanitizer_line_tables` 的关键细节是：**用户显式的 `CUDA_OXIDE_DEBUG`（包括 `off`）永远优先**。也就是说，如果你故意想用"无行表"跑 sanitize，设 `CUDA_OXIDE_DEBUG=off` 即可，cargo-oxide 不会强行覆盖你。

#### 4.5.3 源码精读

行表默认注入：

[cargo-oxide/src/commands.rs:L3002-L3011](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L3002-L3011) — `apply_default_sanitizer_line_tables`：仅当进程级与项目级都没有 `CUDA_OXIDE_DEBUG` 时才设为 `line-tables`，留出用户显式覆盖（含 `off`）的口子。

指纹计算（把 sanitize 设置变成 Cargo 能感知的 cfg）：

[cargo-oxide/src/commands.rs:L1722-L1756](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1722-L1756) — `sanitize_codegen_fingerprint_cfg`：用 FNV 风格哈希混合 `sanitize-line-tables-v1` 标记、通用 codegen 指纹、探测到的设备架构、PTX 目录，输出形如 `cuda_oxide_internal_codegen_env="<16 hex>"` 的 cfg。注释点明动机：Cargo 不给任意后端环境变量做指纹，所以专门塞进一个 cfg 让"切到 sanitize 模式"触发重编。

调用点（普通构建路径里把行表与指纹接上）：

[cargo-oxide/src/commands.rs:L665-L669](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L665-L669) — `codegen_build_host_binary` 里：先 `apply_default_sanitizer_line_tables`，再算 `sanitize_codegen_fingerprint_cfg` 并注入 rustflags。

指纹真的会随"会影响输出的设置"变化，这一点也有测试锁定：

[cargo-oxide/src/commands.rs:L4295-L4315](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L4295-L4315) — `sanitize_fingerprint_tracks_output_affecting_settings`：切换 `no_fmad` / 目标架构 / 显式架构 / ptx 目录都会让指纹不同（`assert_ne`）。

教科书层面的叙述（line-tables 默认开启、用户显式设置仍优先）：

[cuda-oxide-book/gpu-programming/error-handling-and-debugging.md:L351-L353](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/cuda-oxide-book/gpu-programming/error-handling-and-debugging.md#L351-L353) — 教科书 "Compute Sanitizer" 小节：line-tables 默认开启、显式 `CUDA_OXIDE_DEBUG` 仍优先。

#### 4.5.4 代码实践

实践目标：验证"sanitize 默认开行表"且"用户可覆盖"，并理解为什么需要指纹。

操作步骤：

1. 阅读上面的 `apply_default_sanitizer_line_tables` 与 `sanitize_codegen_fingerprint_cfg`。
2. 在脑海里走一遍：先 `cargo oxide run vecadd`（不开行表），再 `cargo oxide sanitize vecadd`。因为指纹不同（多了 `sanitize-line-tables-v1`），Cargo 会**重编**而不是复用，于是 sanitize 拿到带行表的 PTX。
3. （可选，需 GPU）对比两条命令的 sanitizer 报告：`cargo oxide sanitize vecadd`（默认行表）的报告里应能看到 `.rs:行号`；`CUDA_OXIDE_DEBUG=off cargo oxide sanitize vecadd` 的报告里则只剩 PTX 地址。

需要观察的现象：默认 sanitize 报告带源码行；显式 `CUDA_OXIDE_DEBUG=off` 时报告退化成 PTX 地址。

预期结果：证明 `apply_default_sanitizer_line_tables` 的"用户优先"逻辑——你设了 `off`，它就尊重 `off`。无 GPU 则标注「待本地验证」，改为阅读 `apply_default_sanitizer_line_tables` 的 `is_none()` 守卫确认逻辑。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `cargo-oxide` 不直接 `cmd.env("CUDA_OXIDE_DEBUG", "line-tables")` 就完事，还要再算一个指纹 cfg？

**参考答案**：因为 Cargo 不给任意的后端环境变量做指纹（注释原话："Cargo does not fingerprint arbitrary backend env vars"）。如果只设环境变量，从 `run` 切到 `sanitize` 时 Cargo 会复用旧的、无行表的产物，sanitize 报告就没了源码归因。把设置揉进一个 `--cfg` 哈希，Cargo 才会因为 cfg 变化而强制重编。

**练习 2**：用户已经把 `CUDA_OXIDE_DEBUG=off` 写进了项目的 `.cargo/cuda-oxide.toml` 的 `[env]`。`cargo oxide sanitize` 会强行开行表吗？

**参考答案**：不会。`apply_default_sanitizer_line_tables` 同时检查进程级 `std::env::var_os("CUDA_OXIDE_DEBUG")` 与项目级 `project_config_env(ctx, "CUDA_OXIDE_DEBUG")`，只要任一存在就跳过注入。这是"用户显式设置（含 `off`）永远优先"的设计。

---

### 4.6 四种工具的检查目标与常见错误排查

#### 4.6.1 概念说明

把前几节学到的"怎么用"收束成"用哪个、看到什么、怎么修"。这一节是一张速查表，把 cuda-oxide 的类型系统/契约（U2/U5 已学）与 sanitizer 的发现对应起来，让你在看到 sanitizer 报告时能立刻定位到代码里的根因。

#### 4.6.2 核心流程（排查决策树）

```text
症状
 ├── 编译通过，但跑出 wrong results / 随机结果
 │     ├── 怀疑越界、空指针、对齐  →  --tool memcheck（默认）
 │      └── 怀疑未初始化设备内存读  →  --tool initcheck
 │
 ├── 共享内存读到的值"旧的/乱的"，尤其跨线程
 │     └── 缺 sync_threads 或 race  →  --tool racecheck
 │
 ├── 程序在某个 barrier 之后卡死或行为怪异
 │     └── 同步原语误用             →  --tool synccheck
 │
 └── 以上都不命中，且只在大数据量下出错
       └── 先 memcheck 打底，再组合 racecheck/synccheck
```

#### 4.6.3 源码精读（对照表）

`memcheck` 默认这件事在 CLI 与 README 都有明示：

[cargo-oxide/README.md:L103-L120](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/README.md#L103-L120) — README 的 sanitize 小节："`memcheck` is the default tool; use `--tool racecheck`/`initcheck`/`synccheck` for ..."，并给出两层 `--` 的示例。

教科书给出了 cuda-oxide 特有的排查对照表（与 cuda-oxide 的 `DisjointSlice`、`sync_threads`、`LaunchConfig` 等概念绑定）：

[cuda-oxide-book/gpu-programming/error-handling-and-debugging.md:L463-L473](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/cuda-oxide-book/gpu-programming/error-handling-and-debugging.md#L463-L473) — "Common pitfalls" 表：把 cuda-oxide 的 `DisjointSlice`、`sync_threads`、`LaunchConfig` 等抽象与典型症状一一对应。

把 sanitizer 工具与 cuda-oxide 概念的映射整理成下表（综合 README 与教科书）：

| sanitizer 工具 | 典型 cuda-oxide 根因 | 对应讲义 |
|:---------------|:---------------------|:---------|
| `memcheck`（越界/未分配） | 用裸指针越界，而非 `DisjointSlice::get_mut`；`LaunchConfig` 的 grid/block 与缓冲区长度不匹配 | u1-l4、u2-l2、u2-l4 |
| `memcheck`（泄漏） | 设备内存 `Drop` 路径在 panic 下未走完 | u2-l5 |
| `racecheck` | 共享内存读写之间缺 `sync_threads()`；`DisjointSlice` 几何与启动几何不匹配 | u2-l3、u2-l4 |
| `initcheck` | `DeviceBuffer` 分配后未 `zeroed`/未写就传入内核读取 | u2-l5 |
| `synccheck` | `__syncthreads` 到达数不匹配；mbarrier arrive/wait 协议错误 | u2-l3、u5-l3 |

教科书还强调了"互补性"原则——NVIDIA 建议把四个工具当互补工具用，`racecheck`/`initcheck`/`synccheck` 都不做完整内存访问检查，排查内存安全应先 `memcheck`：

[cuda-oxide/README.md:L135-L136](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/README.md#L135-L136) — "NVIDIA recommends treating the tools as complementary"。

#### 4.6.4 代码实践

这是本讲的**主实践任务**（也是规格指定的代码实践）：分别用 `memcheck` 与 `racecheck` 跑两个"故意写错"的内核，定位并修复。

实践目标：亲眼看一次 memcheck 与 racecheck 的报告形态，并把它们归因到 cuda-oxide 的具体错误模式。

**步骤 A：构造一个越界内核（针对 memcheck）**

在 `crates/rustc-codegen-cuda/examples/` 下仿照 `vecadd` 新建一个示例 `sanitize_oob`，核函数故意写到缓冲区末尾之外。**示例代码**（仅示意，非仓库原有代码）：

```rust
use cuda_device::{kernel, thread, DisjointSlice};

#[kernel]
pub unsafe fn oob_write(mut out: DisjointSlice<f32>) {
    let idx = thread::index_1d();
    // 故意越界：写到 out.len() 位置（合法索引应是 0..len-1）
    let bad = idx.get();
    if let Some(slot) = out.get_mut(bad) {
        *slot = 1.0;
    }
    // 注意：若你用裸指针 *(out.as_ptr().add(huge)) 才会被 memcheck 抓到；
    // DisjointSlice::get_mut 本身会越界返回 None（安全），所以为了演示
    // memcheck，需要绕过它用裸指针，或把长度参数与实际缓冲长度对不上。
}
```

注意：`DisjointSlice::get_mut` 本身是越界安全的（返回 `None`），所以要让 `memcheck` 真正报错，需要**绕过**它（用裸指针 `unsafe` 写）或让宿主侧 `LaunchConfig` 的 grid 比缓冲区大、再用裸指针索引。前者对应 u2-l2 讲的"把越界交给 `Option`"的安全设计——sanitize 的价值正是抓住"你绕过了安全抽象"的时刻。

运行：

```bash
cargo oxide sanitize sanitize_oob --tool memcheck
```

需要观察的现象：报告中出现形如 `========= Invalid __global__ write of size 4 ... ======== ... at src/main.rs:XX` 的 finding，进程退出码 86。

预期结果：`memcheck` 抓到越界写，cargo-oxide 注入的 `--error-exitcode 86` 让进程非零退出。修复方法：改回 `DisjointSlice::get_mut` 的安全访问，或校正 `LaunchConfig` 的 grid 与缓冲长度。

**步骤 B：构造一个数据竞争内核（针对 racecheck）**

仿照 `sharedmem` 示例，写一个核：所有线程先写共享内存，**故意不加 `sync_threads()`** 就读邻居线程写的值。**示例代码**（仅示意）：

```rust
use cuda_device::{kernel, thread, SharedArray};

#[kernel]
pub unsafe fn race_shared(mut out: DisjointSlice<f32>) {
    let idx = thread::index_1d();
    let shared = SharedArray::<f32, 256>::new();
    shared[idx.get() % 256] = idx.get() as f32;
    // 故意省略 sync_threads()
    let stolen = shared[(idx.get() + 1) % 256];
    if let Some(slot) = out.get_mut(idx.get()) {
        *slot = stolen;
    }
}
```

运行：

```bash
cargo oxide sanitize race_shared --tool racecheck
```

需要观察的现象：`racecheck` 报告 `========= Race reported ...` 指出共享内存同一地址的写后读未加屏障，并归因到源码行。

预期结果：`racecheck` 抓到 shared memory hazard。修复方法：在写与读之间加 `cuda_device::sync_threads()`（详见 u2-l3）。

如果你没有 GPU 或没有 CUDA Toolkit，标注「待本地验证」，并把实践改为**阅读型**：阅读 `examples/sharedmem` 与 `examples/atomics` 的源码，找出其中所有 `sync_threads()` 与原子操作的位置，解释"如果删掉其中某个 `sync_threads()`，racecheck 会在哪一行报警"。

#### 4.6.5 小练习与答案

**练习 1**：你的内核跑出的结果偶尔正确偶尔错乱，且 `memcheck` 没报任何错。下一步该用哪个工具？

**参考答案**：用 `racecheck`。`memcheck` 只查地址合法性，"地址合法但并发无序"不归它管；偶发错误几乎总是共享内存竞争或同步缺失，正是 `racecheck` 的目标。

**练习 2**：为什么 cuda-oxide 的 `DisjointSlice::get_mut` 会让 `memcheck`"无事可报"？

**参考答案**：`DisjointSlice::get_mut(idx)` 在越界时返回 `None`，越界访问在 Rust 层就被 `Option` 挡掉了，根本不会产生越界的 PTX 内存访问指令，`memcheck` 自然无错可报。这正体现了 u2-l2 讲的设计哲学——把 unsafe 边界从"每次访问"推离。`memcheck` 真正能抓的，是你**绕过** `DisjointSlice` 用裸指针、或宿主侧 `LaunchConfig` 几何与缓冲区不匹配的情况。

**练习 3**：在 CI 里如何用一条命令既查内存安全又保证出错时挂掉脚本？

**参考答案**：`cargo oxide sanitize <example>`（默认 `memcheck` + 默认 `--error-exitcode 86`）。任何 finding 都让进程以 86 退出，CI 自然失败。注意不要画蛇添足加 `-- --error-exitcode 0`，否则会失去这个保证。

## 5. 综合实践

把本讲全部内容串起来，设计一个**三步排查流水线**的小任务。

**背景**：你接手了一个 cuda-oxide 写的 GEMM-like 内核，它在小矩阵上正确、在大矩阵上偶尔出错。你要用 Compute Sanitizer 把根因定位出来。

**任务**：

1. **打底（memcheck）**：先用默认工具跑一遍，确认没有越界/未分配。
   ```bash
   cargo oxide sanitize gemm_like
   ```
   记录是否出现 finding。若退出码为 86，先按 4.6.3 的对照表把越界根因修掉（典型：`LaunchConfig::for_num_elems` 算出的 grid 与 `DisjointSlice` 长度不匹配）。

2. **查竞争（racecheck）**：若 memcheck 干净，切到 racecheck 找共享内存竞争。
   ```bash
   cargo oxide sanitize gemm_like --tool racecheck
   ```
   重点检查 tile 加载与计算之间是否缺 `sync_threads()`、`DisjointSlice` 几何是否与 `#[launch_contract]` 声明的 domain/block 一致（u2-l1、u2-l4）。

3. **查同步（synccheck）**：若仍无果，切到 synccheck 查 barrier 使用。
   ```bash
   cargo oxide sanitize gemm_like --tool synccheck -- --kernel-name kns=gemm_like
   ```
   注意这里用了**第二个 `--` 之前的 sanitizer 参数**（`--kernel-name`，透传给 compute-sanitizer），它让我们只检查指定内核。

**验收标准**：

- 每一步你都能说清"为什么用这个工具、它查什么、报告归因到哪一行源码"。
- 修复后，三条命令的退出码都为 0，**且**你亲自看过报告确认没有 finding（因为退出码 0 不等于干净，见 4.3）。
- 你能解释为什么 cargo-oxide 默认开行表（4.5）、为什么默认注入退出码 86（4.3）。

如果你没有合适的内核或没有 GPU，把综合实践降级为**源码阅读型**：挑 `examples/gemm` 或 `examples/warp_reduce`，按 4.6.3 的对照表逐条标注"这段代码若出错，会被哪个工具抓到、抓在哪一行"，并把结论写成一份排查清单。无法运行的命令统一标注「待本地验证」。

## 6. 本讲小结

- `cargo oxide sanitize <example>` = `run` 的构建路径 + 在产物外套一层 `compute-sanitizer` 进程；构建复用 codegen 后端发现、架构探测、`--no-fmad` 透传，增量只在运行层。
- 四种工具互补：`memcheck`（默认，内存访问）、`racecheck`（共享内存竞争）、`initcheck`（未初始化读）、`synccheck`（同步误用）；排查内存安全优先 `memcheck`，其余按症状补充。
- 参数分隔用**两个 `--`**：第一个 `--` 之后给 sanitizer、第二个 `--` 之后才给你的程序；`--tool` 是 cargo-oxide 自己的 flag，必须写在第一个 `--` 之前。
- **退出码语义**是本讲的关键：sanitizer 默认即使报错也退出 0，cargo-oxide 因此默认注入 `--error-exitcode 86`；显式给出则尊重用户；检测到 `--check-exit-code no`/`--require-cuda-init no` 会提醒"退出码已弱化"，且永不凭退出码断言报告干净。
- `compute-sanitizer` 的发现走 `PATH → toolkit root → 回退路径` 三段式，找不到时提示跑 `doctor`；项目 `[env]` 的 `CUDA_TOOLKIT_PATH` 会被采纳。
- sanitize 默认开**行表**（`CUDA_OXIDE_DEBUG=line-tables`，不关优化），且为了绕过"Cargo 不给后端环境变量做指纹"，专门算了一个 `--cfg` 哈希强制重编；用户显式的 `CUDA_OXIDE_DEBUG`（含 `off`）永远优先。

## 7. 下一步学习建议

- **回到 u7-l2** 复习 `CUDA_OXIDE_DEBUG` 的三档（`off`/`line-tables`/`full`），你会更深刻地理解为什么 sanitize 选 `line-tables` 而非 `full`：sanitize 需要的是"归因到源码行"，不需要 `full` 那种关优化的本地变量可检视能力。
- **结合 u7-l1（compile_fail 与安全契约）**：很多 sanitizer 能抓的运行时错误（越界、几何不匹配），cuda-oxide 试图用类型系统与 `#[launch_contract]` 在**编译期**就挡掉。两套机制一静一动，互为兜底。
- **进阶阅读**：阅读 `crates/cargo-oxide/src/commands.rs` 中 `run_compute_sanitizer` 与 `find_cuda_toolkit_executable` 的完整实现，对比 `codegen_debug`（u7-l2）的 cuda-gdb 发现逻辑，体会 cargo-oxide 对"外部原生工具"的统一封装范式。
- **实践延伸**：把综合实践里的三步流水线固化进一个 CI 脚本，对每个 PR 跑 `memcheck`；当某天退出码变成 86，你已经知道该怎么读了。
