# u2-l4 CLI 入口与子命令分发

## 1. 本讲目标

承接 u1-l2 的核心结论——`pegainfer-bench` 在当前 HEAD 是 **lib-only crate**（`src/` 只有 `lib.rs`，没有 `main.rs`/`cli.rs`）。本讲回答规划稿想问的同一个问题：「命令行进来之后，代码是怎么分发到基准循环的？」但答案必须落到真实源码上：**CLI 入口不在 pegainfer-bench，而在模型 crate 的 feature 门控 bin 里**。

学完本讲，你应该能够：

1. 说清库 crate 与二进制目标的分工：谁拥有 CLI、为什么、`required-features` 在其中起什么作用。
2. 读懂 `qwen3_model_report` 用 clap derive 定义的 `Cli` 结构（位置参数 `command` + 一组 `--long` 参数），并对照 Kimi-K2 侧入口的异同。
3. 画出完整的分发链路：`main()` 校验 → 运行时 trace 得到 `KernelCall` 调度 → `measure_catalog` 按 key 去重 → `measure_call` 按 op 名 match 到 `measure_*` provider → `measure_loop` CUDA 事件计时 → `accumulate`/`rollup_row` 汇总输出。
4. 掌握本仓库的 anyhow 错误处理惯例：`Result`、`bail!`、`ensure!`、`with_context` 各自用在什么位置。

## 2. 前置知识

- **clap derive**：Rust 最常用的命令行解析库。给结构体派生 `Parser`，字段就变成命令行参数：普通字段是位置参数，`#[arg(long)]` 是 `--long` 选项，`default_value`/`default_value_t` 给默认值，`ValueEnum` 把枚举变成受限取值。解析失败（未知 flag、缺参数）由 clap 自己报错退出，根本不会进入 `main` 函数体。
- **cargo 的 bin target 与 `required-features`**：`src/bin/*.rs` 每个文件自动成为一个二进制目标；`Cargo.toml` 里 `[[bin]]` 段可以用 `required-features = [...]` 声明「不开这些 feature 就不编译这个 bin」。本仓库用它把报告工具（依赖 CUPTI 等 profiler 库）挡在生产依赖图之外。
- **`KernelCall`（预习，u2-l5/后续讲义会展开）**：`pegainfer_kernels::tensor` 里的「擦除类型」张量元数据，一次内核调用被记录为 `op`（op 名字符串）+ `inputs`/`outputs`（带命名轴的张量规格）+ `attrs`（字符串属性）。模型报告的整条链路都在消费它。
- **anyhow 四件套**：`Result<T>`（`Result<T, anyhow::Error>` 别名）、`bail!`（立刻返回一个错误）、`ensure!`（条件不满足就报错，类似断言）、`with_context`（用 `?` 向上传播时给错误追加一层上下文）。`main() -> Result<()>` 返回 `Err` 时，Rust 运行时打印错误并以非零码退出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml) | 本 crate 清单：只有 `[package]`/`[dependencies]`/`[lints]`，**无 `[[bin]]`**——「没有 CLI」的直接证据 |
| [`src/lib.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs) | 库本体（273 行）：`LatencyStats`、`measure_loop`、`bench_key`、`KernelCall` 访问器、rollup 构件——CLI 们复用的全是这里的东西 |
| [`../pegainfer-qwen3/src/bin/qwen3_model_report.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs) | **本讲主走读对象**（1101 行）：Qwen3 模型级算子报告 CLI，clap 定义 + `main` 分发 + op 级 provider 表 |
| [`../pegainfer-qwen3/Cargo.toml`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/Cargo.toml) | `kernel-report` feature 定义与两个 bin 的 `required-features` 注册 |
| [`../pegainfer-kimi-k2/src/bin/kimi_model_report.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/bin/kimi_model_report.rs) | 第二个 CLI 入口（Kimi-K2 侧），结构与 qwen3 版同构，参数略有差异 |
| [`../pegainfer-kimi-k2/src/kernel_report.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs) | Kimi 侧的 provider 集合：直接 `use pegainfer_bench::measure_loop` 并 re-export `LatencyStats` 等 |
| [`../docs/subsystems/kernels/kernel-op-reports.md`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/subsystems/kernels/kernel-op-reports.md) | 官方文档：pegainfer-bench 抽取史、已验证的运行命令 |

> **结构勘误（承接 u1-l2）**：本手册规划稿假设本 crate 有 `src/main.rs` + `src/cli.rs`（「约 31KB 单文件、getopts 风格 `-c/-n/-i/-o` 短参数」）。当前 HEAD `223206b7` 下这些文件不存在，getopts 也不在依赖里；实际 CLI 用的是 **clap derive**，且不在本 crate。本讲全部内容以真实源码为准。

## 4. 核心概念与源码讲解

### 4.1 库与二进制的分工：CLI 入口为什么不在 pegainfer-bench

#### 4.1.1 概念说明

Rust 里「一个 crate」可以是库（产出 `.rlib` 给别人 `use`）、可以是二进制（产出可执行文件）、也可以两者都有。`pegainfer-bench` 选择了**纯库**形态：它自己是不能 `cargo run` 的。

这不是偶然，而是演化结果。官方文档记录了这条时间线：模型级算子报告最初整体长在 `pegainfer-qwen3` 里（2026-05 的 #944 把 harness 源码移进 `src/bin/report_support/`），随后其中**模型无关**的部分——计时循环、延迟统计、按 op / 按调用点的汇总——被抽成独立 crate，就是今天的 `pegainfer-bench`；见 [kernel-op-reports.md:L481](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/subsystems/kernels/kernel-op-reports.md#L481) 的 follow-up 记录。TL;DR 也写明「The model-agnostic timing loop, latency stats, and by-op/by-call-site rollup now live in the `pegainfer-bench` crate, shared by the Kimi-K2 model report」——[kernel-op-reports.md:L6](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/subsystems/kernels/kernel-op-reports.md#L6)。

为什么 CLI 留在模型 crate？两个原因都能从源码读出来：

1. **CLI 是模型特定的**。`measure_call` 的 op 分发表（4.3 节）绑定 Qwen3 的算子集合与几何常量（头数、层数、RMSNorm eps 都来自 `pegainfer_qwen3::batch_decode_trace`），放进共享库就成了错误的抽象方向。
2. **feature 门控需要「就近」**。报告工具要拉 CUPTI、comfy-table、toml 等 tool 依赖，用 `required-features = ["kernel-report"]` 把它们挡在生产的模型 crate 依赖图之外；这是文档明确记下的教训——[kernel-op-reports.md:L466](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/subsystems/kernels/kernel-op-reports.md#L466)。

#### 4.1.2 核心流程

依赖与调用方向（注意箭头是「调用」，与「依赖」同向）：

```text
cargo run --release -p pegainfer-qwen3 --features kernel-report --bin qwen3_model_report -- decode ...
        │
        ▼
qwen3_model_report（bin，clap CLI + 模型特定 provider 表）
        │  use pegainfer_bench::{LatencyStats, measure_loop 消费品, accumulate, rollup_row, ...}
        ▼
pegainfer-bench（lib：计时循环 / 统计 / KernelCall 访问器 / rollup）
        │  use pegainfer_kernels::tensor::{DeviceContext, KernelCall, ...}
        ▼
pegainfer-kernels（CUDA 张量与算子封装，cudarc 之下）
```

`pegainfer-bench` 在棋盘上只被两个模型 crate 消费：qwen3（可选依赖，`kernel-report` feature 打开）与 kimi-k2（普通依赖）。

#### 4.1.3 源码精读

**① bench 清单里没有任何 bin**——[Cargo.toml:L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/Cargo.toml#L1-L15) 只有三段：`[package]`、`[dependencies]`、`[lints]`。没有 `[[bin]]`、没有 `src/main.rs`，cargo 不会为它生成可执行目标。

**② 库的自我定位写在模块文档里**——[src/lib.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L1-L9)：「每个模型 crate 的 kernel-report 工具都需要同样的构建块：CUDA 事件计时循环、延迟统计、对 `KernelCall` 调度的访问器、以及把逐调用延迟折叠成报告行的 rollup。这些构件不携带模型知识，所以放在这里」。

**③ 被依赖的通道**——根清单把它登记进 workspace 依赖：[../Cargo.toml:L121](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L121)（`pegainfer-bench = { path = "pegainfer-bench" }`，`members` 见 [L10](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L10)）。

**④ qwen3 侧：可选依赖 + feature 汇聚 + bin 门控**——[../pegainfer-qwen3/Cargo.toml:L18](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/Cargo.toml#L18) 声明 `pegainfer-bench = { workspace = true, optional = true }`；[L36-L43](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/Cargo.toml#L36-L43) 的 `kernel-report` feature 用 `dep:pegainfer-bench` 把它（连同 comfy-table/cupti/sha2/toml 与 `kernel-call-trace`）一起打开；[L58-L61](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/Cargo.toml#L58-L61) 注册 `qwen3_model_report` bin 并要求 `required-features = ["kernel-report"]`——不开 feature，bin 根本不参与编译，所以普通 server 构建拖不进报告工具链。

**⑤ kimi 侧：普通依赖 + 同样的 bin 门控**——[../pegainfer-kimi-k2/Cargo.toml:L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/Cargo.toml#L19) 是非可选的 `pegainfer-bench = { workspace = true }`；[L47-L50](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/Cargo.toml#L47-L50) 的 `kimi_model_report` bin 同样 `required-features = ["kernel-report"]`。

#### 4.1.4 代码实践

1. **实践目标**：用工具亲眼确认「谁依赖 pegainfer-bench」。
2. **操作步骤**：在仓库根目录执行 `cargo tree -i pegainfer-bench`（`-i` 表示反向：谁依赖它）。
3. **需要观察的现象**：输出应出现两个消费方——`pegainfer-kimi-k2`（直接依赖）与 `pegainfer-qwen3`（标注为 optional，经由 `kernel-report` feature）。
4. **预期结果**：与 ④⑤ 的清单证据一致。**待本地验证**（本讲义撰写环境未运行 cargo）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cargo run --release -p pegainfer-bench -- --help` 必然失败？
答案：`pegainfer-bench` 是 lib-only crate——清单无 `[[bin]]`、`src/` 无 `main.rs`，cargo 找不到可运行的 bin 目标（u1-l2 §4.2.4 已实际验证过同型命令）。

**练习 2**：`required-features = ["kernel-report"]` 挡住的是什么？如果去掉会怎样？
答案：它保证不开 `kernel-report` feature 时 bin 不编译，报告工具的依赖（CUPTI、comfy-table 等）不会进入生产模型 crate / server 的依赖图；去掉后这些 tool 依赖会被默认构建拖上，与文档记下的架构教训相悖。

**练习 3**：模型无关的 `measure_loop` 为什么能同时服务 qwen3 和 kimi 两条模型线？
答案：它只依赖 `DeviceContext` 与一个 `launch: FnMut() -> Result<()>` 闭包——不含任何模型知识（[src/lib.rs:L95-L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L95-L99)），模型差异全部由调用方装进闭包里。

### 4.2 子命令与参数定义：clap derive 的 Cli 结构

#### 4.2.1 概念说明

两个模型报告 CLI 的参数层都是 clap derive：一个派生 `Parser` 的结构体就是全部参数定义。与规划稿猜想的「getopts 短参数」不同，这里用的是**位置子命令字符串 + 长选项**的形态：第一个位置参数 `command` 是普通 `String`，取值合法性由 `main()` 手动校验（MVP 风格——目前只实现了 `decode`），而不是 clap 的 `#[command(subcommand)]` 枚举。好处是改动小、错误信息自己控；代价是 `--help` 里看不到子命令清单。

#### 4.2.2 核心流程

命令行字符串到结构体字段的映射（示例，非项目源码输出）：

```text
$ qwen3_model_report decode --batch-size 16 --kv-len 2048 --format text
                         │        │             │          │
        command="decode"─┘        │             │          └─ format="text"
                     batch_size=16┘      kv_len=2048┘
        （iters=32、model_path="models/Qwen3-4B"、policy="tuned" 取默认值）
```

#### 4.2.3 源码精读

**① Qwen3 的参数定义**——[../pegainfer-qwen3/src/bin/qwen3_model_report.rs:L69-L89](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L69-L89)：`#[derive(Parser)] struct Cli`，字段含义如下——

| 参数 | 类型 / 默认 | 含义 |
| --- | --- | --- |
| `command`（位置参数） | `String`，必填 | 阶段名，目前仅接受 `decode`（L72 注释写明 MVP） |
| `--batch-size` | `usize`，必填 | 解码批的请求数 |
| `--kv-len` | `usize`，必填 | 解码上下文的 KV 长度 |
| `--format` | `String`，默认 `text` | 报告输出格式：`text` / `json` |
| `--out` | `Option<PathBuf>` | 报告 JSON 路径；缺省按 policy/bs/kv 组合生成 |
| `--iters` | `u64`，默认 `DEFAULT_ITERS = 32`（[L67](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L67)） | 每个唯一 KernelCall 的计时迭代数 |
| `--model-path` | `String`，默认 `models/Qwen3-4B` | 权重目录 |
| `--policy` | `String`，默认 `tuned` | 数值策略：`tuned` / `pin` / `per-token`，须与生产一致 |

**② Kimi 的参数定义（对照）**——[../pegainfer-kimi-k2/src/bin/kimi_model_report.rs:L36-L60](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/bin/kimi_model_report.rs#L36-L60)：结构同型，差异是——没有 `--policy`；新增 `--source`（`ValueEnum`：`runtime` | `static`，默认 `runtime`，选择调度来源是运行时 trace 还是静态构造）；`--model-path` 默认 `models/Kimi-K2.5`；`--iters` 默认 16（`DEFAULT_ITERS`，L34）。`TraceSource` 枚举定义在 [L56-L60](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/bin/kimi_model_report.rs#L56-L60)——这是「受限取值交给 clap」的样板，与 `command` 的手动校验形成对比。

**③ 默认输出路径按 policy 区分**——[../pegainfer-qwen3/src/bin/qwen3_model_report.rs:L238-L247](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L238-L247)：未给 `--out` 时，路径形如 `target/model_reports/{MODEL}/decode-{policy}-bs{batch}-kv{kv}.json`，注释解释了为什么要把 policy 编进文件名——否则后一次运行会静默覆盖前一次的 JSON/DOT。

#### 4.2.4 代码实践

1. **实践目标**：把 clap 字段定义与真实 `--help` 输出对上号。
2. **操作步骤**：在有 CUDA 构建环境的机器上，按文档已验证的命令形态（[kernel-op-reports.md:L377](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/subsystems/kernels/kernel-op-reports.md#L377)）执行：`cargo run --release -p pegainfer-qwen3 --features kernel-report --bin qwen3_model_report -- --help`。
3. **需要观察的现象**：帮助文本列出 Usage 与上表全部选项；`command` 作为位置参数出现；`--iters`/`--model-path`/`--policy` 显示默认值。
4. **预期结果**：字段名、默认值与 ① 的表格逐项吻合。注意 `--help` 不加载模型、不碰 GPU，但**构建**本身需要 CUDA 工具链（会编译 `pegainfer-kernels`）。输出文本**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`command` 为什么是 `String` 而不是 `ValueEnum` 或 clap 子命令？
答案：MVP 取舍——目前只实现 `decode`，合法性由 `main()` 开头一行 `if cli.command != PHASE_DECODE { bail!(...) }` 手动把关（见 4.4 ③）；kimi 侧 `--source` 则示范了枚举受控的写法。

**练习 2**：不填 `--iters` 时 qwen3 与 kimi 各测多少次？
答案：qwen3 默认 32（`DEFAULT_ITERS`，L67），kimi 默认 16（L34）——两处常量不同，说明「默认迭代数」被认为是模型/场景相关的决策，而不是库层常数。

**练习 3**：`--format json` 与 `--out` 的关系是什么？
答案：`--out` 决定 JSON 报告文件**写到哪里**（默认 `target/model_reports/...`），`--format` 决定终端**打印**什么：`text` 打印表格报告，`json` 把完整报告 pretty-print 到 stdout（[L252-L260](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L252-L260)）；无论哪种格式，JSON 与 DOT 文件都会写盘。

### 4.3 分发逻辑：从 main() 到 op 级 measure_* provider

#### 4.3.1 概念说明

「分发」在这个代码库里其实是**两级**的：

1. **阶段编排级**：`main()` 按固定顺序推进——解析、校验、设策略、trace 一次真实解码拿到调度、逐个测量、汇总成报告、落盘打印。
2. **op 分发级**：`measure_call` 拿着 `KernelCall.op`（字符串）去 match，把每个 op 路由到对应的 `measure_*` provider 函数。这本质是一张**手写的注册表**：新算子入表就多一个 match 臂，没入表就 `bail!`（见 4.4）。

而 `pegainfer_bench` 的库 API 正是嵌在这条链路中间的共享件：qwen3 的 bin import 了它的统计类型、访问器与 rollup（[L21-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L21-L32)）。还有一个值得注意的差异：**qwen3 的 model report 自带一个本地 `measure_loop`（带 L2CacheClear 冷缓存语义），kimi 则直接用库里的 `measure_loop`**——同一共享库的两种消费方式。

#### 4.3.2 核心流程

```text
main()                                              [qwen3_model_report.rs]
 ├─ Cli::parse()                                    # clap 解析；未知 flag 在此报错
 ├─ 校验 command/bs/kv/iters/format/policy          # 全部 bail!（见 4.4）
 ├─ set_numeric_policy(policy)
 ├─ trace_decode_kernel_calls(model_path, bs, kv)   # 跑一次真实 batch_decode，
 │                                                  # 录下 Vec<KernelCall> 调度
 ├─ measure_catalog(&schedule, iters, policy)       # 唯一化 + 逐个测量：
 │    ├─ bench_key(call)                            # op+形状+attrs → JSON key，去重
 │    ├─ classify(policy, op)                       # Tuned 下的 GEMM → Excluded（不测）
 │    └─ measure_call(call) ──match call.op──► measure_embedding / measure_gemm /
 │          measure_rms_norm_batch / ...  / measure_paged_decode_attention
 │              └─ measure_loop(ctx, iters, launch) # CUDA 事件逐次计时
 ├─ compose_report(...)                             # 遍历调度：accumulate →
 │                                                  # rollup_row / call_site_row
 └─ write_json_report + write_dot_report；按 --format 打印
```

这条链路里，来自 `pegainfer_bench` 的构件是 `LatencyStats`、`accumulate`、`rollup_row`、`call_site_row`、`axis`、`input`、`output`、`attr_usize`、`zero_matrix`（qwen3 的 import 清单 [L21-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L21-L32)）；而 `bench_key` 与 `measure_loop` 在 qwen3 侧是本地版，kimi 侧用库版（见 ⑤⑥）。

#### 4.3.3 源码精读

**① main() 的编排骨架**——[../pegainfer-qwen3/src/bin/qwen3_model_report.rs:L186-L263](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L186-L263)：L187 解析；L188-L202 五组校验；L204-L205 策略；L207 trace；L209-L210 测量目录；L212-L227 两个 `ensure!` 守门（Pin/PerToken 策略必须真的生效）；L228-L237 组装报告；L238-L250 落盘 JSON+DOT；L252-L260 按格式打印。短短 70 多行把整条流水线串完，每步失败都能带着上下文冒泡。

**② measure_catalog：去重与豁免**——[../pegainfer-qwen3/src/bin/qwen3_model_report.rs:L474-L499](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L474-L499)：先跳过 no-op all-reduce（L481-L482）；再用 `bench_key` 生成 JSON 身份键，已在目录里就跳过（L484-L486）——36 层里形状相同的 GEMM 只测一次；`classify` 判定不可信的 op（Tuned 策略下的投影 GEMM 会跑非生产路径）标记 `Excluded` 而不测量（L488-L489）；其余交给 `measure_call`，失败时 `with_context` 附上调用标签与张量规格（L491-L493）。

**③ measure_call：op 注册表**——[../pegainfer-qwen3/src/bin/qwen3_model_report.rs:L501-L517](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L501-L517)：`paged_decode_attention` 因变体处理单独提前返回（L502-L504），随后一个 match 把 `embedding_batch`、`rms_norm_batch`、`gemm_rows`、`qk_norm_rope_batch_decode`、`gemm`、`fused_add_rms_norm_batch`、`silu_mul_fused_batch`、`split_qkv`、`suppress_logits_bf16` 路由到各自 provider；未知 op 走 L515 的 `bail!`——注册表是封闭的，绝不静默跳过。

**④ provider 的固定形状**——以 [measure_embedding（L519-L533）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L519-L533) 为例：用库访问器 `input`/`output`/`axis` 从 `KernelCall` 里读形状 → `zero_matrix` 造设备缓冲 → 把真实算子调用装进闭包交给 `measure_loop`。每个 provider 都是这套「读形状、造假数据、包闭包、计时」的模板。

**⑤ 两个 measure_loop**——库版 [src/lib.rs:L94-L122](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L94-L122)：先 3 次不计时热身（L103-L105），再创建一对 CUDA 事件，逐次 `start.record → launch → end.record → elapsed` 采样；qwen3 本地版 [L828-L851](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L828-L851) 在每次测量前多一步 `cache_clear.clear(ctx)`（冷 L2 语义，服务于 qwen3 的回归框架）。kimi 侧则直接 `use pegainfer_bench::measure_loop`——[../pegainfer-kimi-k2/src/kernel_report.rs:L6-L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs#L6-L17)，还把 `LatencyStats`/`MeasuredCall`/`bench_key` re-export 给自己的 bin。

**⑥ bench_key 的两种实现**——库版 [src/lib.rs:L125-L132](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L125-L132) 直接序列化 `op + inputs + outputs + attrs`；qwen3 本地版 [L876-L898](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L876-L898) 在序列化前滤掉 `no_op`/`tp_world_size` 两个属性——单 rank 下它们不该参与形状身份。

#### 4.3.4 代码实践（源码阅读型，无需 GPU）

1. **实践目标**：亲手验证 4.3.2 的调用链每一条边都真实存在。
2. **操作步骤**：在仓库根目录依次执行并记录行号：
   - `grep -n "trace_decode_kernel_calls" pegainfer-qwen3/src/bin/qwen3_model_report.rs`（应在 L207 附近命中调用，L55 附近命中 import）
   - `grep -n "fn measure_catalog\|fn measure_call\|fn measure_loop\|fn compose_report" pegainfer-qwen3/src/bin/qwen3_model_report.rs`
   - `grep -n "pegainfer_bench::" pegainfer-kimi-k2/src/kernel_report.rs`
3. **需要观察的现象**：每条 grep 命中的行号都能放进 4.3.2 的流程图对应位置。
4. **预期结果**：得到一张「边 → 文件:行号」的完整链路表。若想真实运行，用文档已验证的命令（[kernel-op-reports.md:L385](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/docs/subsystems/kernels/kernel-op-reports.md#L385)：`cargo run --release -p pegainfer-qwen3 --features kernel-report --bin qwen3_model_report -- decode --batch-size 16 --kv-len 2048 --format text`），需要 GPU + `models/Qwen3-4B` 权重，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：36 层模型里同形状的 `gemm` 会被测量多少次？
答案：1 次。`measure_catalog` 用 `bench_key`（op+形状+attrs 的 JSON）做目录键，重复键直接跳过（L484-L486）；`compose_report` 再把这一次的统计按调度里的出现次数累进各 rollup 行。

**练习 2**：为什么 Tuned 策略下投影 GEMM 被标记 `unfaithful_gemmex` 而不是照测？
答案：报告的测量上下文未经过生产级的 gemm_lt 调参，硬测会跑普通 GemmEx——数字看似有效、实际不代表生产路径；所以 `classify`（[L161-L167](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L161-L167)）把它归为 Excluded，报告里明确标注 partial total，想看真实 GEMM 延迟要跑 `--policy pin`。

**练习 3**：新算子要进入报告，最少要动哪几处？
答案：① 在 `measure_call` 的 match 里加一个臂路由到新 provider（或复用已有 provider）；② 写 `measure_<op>` 函数（读形状→造缓冲→闭包→`measure_loop`）；若调度里还没有它，则还要让 trace 路径（`batch_decode_dag` / `batch_decode_trace`）录出对应 `KernelCall`。缺了 ①，运行时会在 L515 `bail!`「no benchmark provider for op」。

### 4.4 错误处理风格：anyhow 的四件套

#### 4.4.1 概念说明

本仓库的工具层统一用 anyhow：函数签名是 `Result<T>`（即 `Result<T, anyhow::Error>`），错误可以携带任意上下文链；`main() -> Result<()>` 返回 `Err` 时运行时打印 `Error: ...` 并以非零码退出。四件套的分工：

| 工具 | 语义 | 典型位置 |
| --- | --- | --- |
| `bail!(...)` | 立刻构造并返回错误 | 参数校验、不可恢复的不一致 |
| `ensure!(cond, ...)` | 条件不满足即报错（断言式） | 防御性检查、策略生效守门 |
| `.with_context(\|\| ...)` | `?` 传播时给底层错误追加一层说明 | 调用链中层（catalog、写文件） |
| `anyhow!(...)` / `ok_or_else` | 构造带格式的错误 / 把 Option 转 Result | 访问器找不到轴/参数时 |

设计取向很一致：**快速失败、错误信息必须能定位到具体调用**，绝不静默跳过或给个默认值糊弄过去。

#### 4.4.2 核心流程

一次错误从底层冒到终端的路径：

```text
provider 内部算子失败（cudarc/serde 的原生错误）
  ─?→ measure_catalog 的 .with_context("failed to measure {label}\n{specs}")
  ─?→ main() -> Result<()> 返回 Err
  ──► Rust 运行时打印错误链（含每层 context），进程非零退出
```

参数校验类错误更短：`main()` 里直接 `bail!`，一步到终端。

#### 4.4.3 源码精读

**① 库层的 anyhow 用法**——[src/lib.rs:L11-L13](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L11-L13) 引入 `Result`/`anyhow`/`bail!`；`from_samples` 对空样本集 `bail!("latency sample set is empty")`（[L57-L60](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L57-L60)）；`measure_loop` 对零迭代 `bail!`（[L100-L102](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L100-L102)）；访问器 `axis` 找不到命名轴时用 `anyhow!` 拼「missing axis `x` in ...」错误（[L134-L140](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs#L134-L140)）。

**② bin 的 anyhow 导入**——[../pegainfer-qwen3/src/bin/qwen3_model_report.rs:L8-L11](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L8-L11)：`Context`（给 `with_context` 用）、`Result`、`anyhow!`、`bail!` 各就各位。

**③ main() 开头的校验墙**——[L186-L203](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L186-L203)：连续五个 `bail!` 分别守 `command`（`only \`decode\` is implemented; got \`...\``）、`--batch-size`、`--kv-len`、`--iters` 的非零、`--format` 的取值。注意它们全部发生在 trace（L207）与任何 `DeviceContext::new` **之前**——错误路径不碰 GPU。

**④ parse_policy 的枚举校验**——[L169-L176](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L169-L176)：字符串到 `NumericPolicy` 的翻译，非法值 `bail!("--policy must be tuned | pin | per-token, got \`...\`")`。

**⑤ ensure! 守策略生效**——[L212-L227](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L212-L227)：Pin/PerToken 模式下检查服务计数器，若没有任何投影 GEMM 走了对应路径就 `ensure!` 报错——防止「策略没生效但报告照常出」的静默假结果。

**⑥ with_context 的中层加固**——`measure_catalog` 调 `measure_call` 失败时附上调用标签与张量规格（[L491-L493](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L491-L493)）；`main` 里再包一层「failed to build strict measured catalog」（[L209-L210](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L209-L210)）；写文件失败也带路径（[L265-L272](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L265-L272)）。另外 `compose_report` 对「调度里有调用但目录里没有测量结果」的情况整体 `bail!` 并列出全部缺失项（[L414-L416](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L414-L416)）——缺一不可，不搞最邻近估计。

#### 4.4.4 代码实践

1. **实践目标**：亲眼观察校验错误的形态与退出码（不碰 GPU）。
2. **操作步骤**：构建成功后（需 CUDA 构建环境）依次运行并记录退出码（`echo $?`）：
   - `cargo run --release -p pegainfer-qwen3 --features kernel-report --bin qwen3_model_report -- hello`
   - `... -- decode --batch-size 0 --kv-len 2048`
   - `... -- decode --batch-size 16 --kv-len 2048 --policy foo`
3. **需要观察的现象**：三条命令都应在进入 trace/GPU 之前失败，stderr 打印 `Error: ...`；退出码非零。
4. **预期结果**：错误文本分别对应 ③④ 的三条 `bail!` 消息（`only \`decode\` is implemented; got \`hello\`` 等）。**待本地验证**（具体措辞以本地运行为准）。

#### 4.4.5 小练习与答案

**练习 1**：`bail!` 和 `ensure!` 在这里的分工？
答案：`bail!` 用于「无条件判死」的分支（如 command 不匹配、格式非法）；`ensure!` 用于「条件断言」（如 Pin 模式下 `pin_served_count > 0`、`ids <= rows`），语义上更接近带副作用的 assert。

**练习 2**：为什么 `measure_call` 失败要用 `with_context` 而不是直接 `bail!`？
答案：底层错误（CUDA、序列化）本身有信息，`with_context` 在保留原始错误链的前提下追加「哪个 KernelCall、什么形状」，终端看到的是分层错误；`bail!` 会丢掉底层错误。

**练习 3**：调度里出现没有 provider 的 op 时，程序会怎样？
答案：不是跳过，而是 `bail!("no benchmark provider for op \`...\`")`（L515）；即使绕过这里，`compose_report` 还有一道「missing bench result」总检查（L414-L416）兜底——两层都体现「宁可失败，不可造假」的原则。

## 5. 综合实践

**任务：给真实 CLI 入口加一个最小子命令 stub，并验证分发链路。**

规划稿的原任务是「给 cli.rs 加 hello 子命令、跑 `cargo run --release -p pegainfer-bench -- hello`」。按当前 HEAD 的事实修正为对真实入口 `qwen3_model_report` 操作：

1. **实践目标**：证明「位置参数 `command` → `main()` 分发」这条链路真实可改、可跑。
2. **操作步骤**：
   - 打开 `pegainfer-qwen3/src/bin/qwen3_model_report.rs`，在 [L188](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/bin/qwen3_model_report.rs#L188) 的 decode 检查**之前**插入（示例代码，非项目原有代码）：

     ```rust
     if cli.command == "hello" {
         println!("hello from qwen3_model_report");
         return Ok(());
     }
     ```

   - 运行 `cargo run --release -p pegainfer-qwen3 --features kernel-report --bin qwen3_model_report -- hello`。
   - 再跑 `... -- decode --batch-size 16 --kv-len 2048 --format text`（需 GPU + 权重，可不跑）确认正常路径不受影响。
   - 实验后用 `git checkout -- pegainfer-qwen3/src/bin/qwen3_model_report.rs` 还原，不要提交。
3. **需要观察的现象**：`hello` 打印一行问候并正常退出（exit 0）；`decode` 行为不变；任意其他子命令（如 `world`）仍触发 `only \`decode\` is implemented` 的 `bail!`。
4. **预期结果**：分发链路验证矩阵——

   | 命令 | 预期行为 | 依据 |
   | --- | --- | --- |
   | `hello` | 打印问候，exit 0 | 你新增的分支 |
   | `decode ...` | 走完整报告流水线 | L188 起的原路径 |
   | `world` | 非零退出 + bail 消息 | L188-L190 |

   同时解释：原任务里的 `cargo run --release -p pegainfer-bench -- hello` 为什么必然失败（lib-only crate 无 bin 目标，见 4.1 练习 1）。**待本地验证**（构建需 CUDA 工具链；`hello` 分支本身不触碰 GPU）。

## 6. 本讲小结

- 当前 HEAD 的 `pegainfer-bench` 没有自己的 CLI：清单无 `[[bin]]`、`src/` 只有 `lib.rs`；真实的命令行入口是模型 crate 里 feature 门控的 bin（qwen3 / kimi 各自的 `*_model_report`，`required-features = ["kernel-report"]`）。
- 参数层用 clap derive：位置参数 `command`（String，手动校验，目前仅 `decode`）+ `--batch-size/--kv-len/--format/--out/--iters/--model-path/--policy`；kimi 侧多一个 `ValueEnum` 的 `--source`，少了 `--policy`。
- 分发是两级的：`main()` 编排「校验 → trace 调度 → measure_catalog → compose_report → 落盘打印」；`measure_call` 再按 `KernelCall.op` match 到 `measure_*` provider（封闭注册表，未知 op 直接 `bail!`）。
- `pegainfer_bench` 库 API 嵌在链路中段：`LatencyStats`/`accumulate`/`rollup_row`/`call_site_row`/`axis`/`input`/`output`/`zero_matrix` 被 bin import；qwen3 用本地 `measure_loop`（冷 L2），kimi 直接用库版——同一库的两种消费方式。
- 错误风格是 anyhow 四件套：校验墙用 `bail!`、策略守门用 `ensure!`、中层传播用 `with_context`、访问器用 `anyhow!`/`ok_or_else`；原则是快速失败、错误可定位、绝不静默兜底。
- 规划稿的「cli.rs + getopts 短参数」叙述与当前源码不符，本讲已按 HEAD `223206b7` 全部落到真实文件与行号。

## 7. 下一步学习建议

- 下一讲 u2-l5 进入**计时与统计骨架**：规划稿叫它 `timings.rs`，当前 HEAD 对应的是 [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-bench/src/lib.rs) 里的 `LatencyStats`（mean/stddev/min/p50/p95/p99/max）与 `measure_loop`（热身 + CUDA 事件采样）——重点比较它与 qwen3 本地冷 L2 版的差异。
- 想看 `KernelCall`/`TensorSpec` 的定义本身，读 [`pegainfer-kernels/src/tensor.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kernels/src/tensor.rs)。
- 想看调度从哪来，读 [`pegainfer-qwen3/src/batch_decode_trace.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-qwen3/src/batch_decode_trace.rs)（`trace_decode_kernel_calls` 的家）。
- 想看第二个消费方的完整 provider 表，读 [`pegainfer-kimi-k2/src/kernel_report.rs`](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-kimi-k2/src/kernel_report.rs)，对照它与 qwen3 版注册表的异同。
