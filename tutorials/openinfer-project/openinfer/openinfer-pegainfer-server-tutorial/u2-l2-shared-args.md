# u2-l2 · SharedArgs：跨模型线共享的参数面

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 PegaInfer CLI 上的**三类 flag**：核心 flag（`CORE_ARGS`，4 个，任何线都可用、由 server 自己消费）、`SharedArgs` 中**按线消费**的共享子集（16 个，由每条线的 `consumed_shared_args` 声明）、以及各条线经 `augment_cli` 挂上的**独占 flag**。
2. 说出一条共享 flag 在启动过程中要过的**四道门**：值门（clap `value_parser`）→ 依赖门（clap `requires_all`，来源是 `SHARED_ARG_REQUIREMENTS` 与各线 `arg_requirements`）→ 归属门（`validate_provided` 的 consume-or-reject）→ 语义门（`SharedArgs::validate` + 线内 `validate`），并能指出前两道在**解析期**、后两道在**检测之后、引擎加载之前**。
3. 解释 `provided_args()` 如何借助 clap 的 `ValueSource` 区分「用户显式提供」与「默认值填充」，以及为什么这个区分是整套校验成立的前提。
4. 在真实二进制上亲手触发两条措辞来源不同的规则错误，并逐字把消息对应回它的模板源头。

本讲紧接 u2-l1：上一讲建立了 `ModelLine` trait 这条接缝，本讲拆开接缝上流动的最重要的一类数据——共享参数面 `SharedArgs`。u2-l3（注册表自检）与 u2-l4（Qwen3 的完整校验矩阵）都建立在本讲的概念上。

## 2. 前置知识

本讲默认你读过 u2-l1（`ModelLine` trait）与 u1-l3（main 启动主流程）。用通俗语言补齐四个概念：

- **clap derive 的「字段即 flag」**：`#[derive(clap::Args)]` 的结构体里，每个字段自动变成一个命令行参数——字段名 `kv_offload_host_gib` 按 kebab-case 变成 `--kv-offload-host-gib`；`#[arg(long, default_value_t = 8.0)]` 这类属性控制拼写、默认值、取值方式。所以读 `SharedArgs` 的结构体定义就等于读 `--help` 的目录。
- **`ArgAction::Set` 与布尔开关的区别**：Rust 的 `bool` 字段默认是「出现即 true」的开关（`--kv-offload`）；而 `cuda_graph` 显式声明了 `action = clap::ArgAction::Set`，于是它**必须带值**（`--cuda-graph=false` 才能关掉）。`--help` 里前者不带占位符、后者带 `<CUDA_GRAPH>`。
- **`value_parser`（值解析器）**：clap 在解析期对每个值跑的校验函数。可以是类型自带的（`u16`、`f64`）、带范围的（`value_parser!(u32).range(1..=99)`），也可以是自定义函数（本讲的 `parse_offload_gib`）。值不过关直接解析失败，进程退出。
- **`ValueSource`（值来源）**：clap 给 `ArgMatches` 里每个值记录的「出身」——`CommandLine`（用户在命令行写了）、`EnvVariable`（来自环境变量）、`DefaultValue`（默认值填充）。同一个 `--tp-size 1`，写了就是 `CommandLine`，没写而用默认值 1 就是 `DefaultValue`。**值相同时出身可以不同**，这是本讲第 3 节的主角。

一个贯穿全讲的直觉：**「定义一个 flag」和「允许用户用这个 flag」是两件事**。`SharedArgs` 定义了 20 个 flag，全部挂在每条命令上（所以 `--help` 全都看得见）；但检测出模型线之后，只有被那条线声明消费的子集才允许出现——其余的会被拒掉。这就是「参数面」一词的含义：面是全的，使用权是按线裁剪的。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs) | **本讲主战场**：`CORE_ARGS`、`SharedArgs` 定义、`SHARED_ARG_REQUIREMENTS`、`SharedArgs::validate`、`provided_args`、`build_command`（把依赖转成 `requires_all`）都在这里 |
| [pegainfer-qwen3/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs) | 一条真实线的消费声明（`consumed_shared_args` 的 15 项清单）、线内依赖（`arg_requirements`）与线内规则（`validate`） |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs) | 消费方：四道门在 main 里的触发顺序，以及把 `provided` 集合打进日志的那一行 |

对照参考（各线消费面的差异，只看清单不必精读）：[pegainfer-deepseek-v2-lite/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-deepseek-v2-lite/src/model_line.rs)、[pegainfer-gemma4/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-gemma4/src/model_line.rs)、[pegainfer-kimi-k2/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-kimi-k2/src/model_line.rs)、[pegainfer-glm52/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-glm52/src/model_line.rs)。

## 4. 核心概念与源码讲解

### 4.1 SharedArgs：三层 flag 体系中的共享层

#### 4.1.1 概念说明

整个 server 的命令行由三块拼成：

1. **核心 flag（`CORE_ARGS`，4 个）**：`model_path`、`served_model_name`、`port`、`tool_call_parser`。它们与检测出哪条线无关，由 **server 二进制自己消费**（main.rs 直接读它们拼装 HTTP 前端），所以对每条线无条件放行。
2. **共享 flag（`SharedArgs` 其余 16 个字段）**：多家模型线都用得上的通用旋钮——并行度（`tp_size`/`dp_size`）、CUDA Graph、KV 卸载与 P2P、prefill/decode 交叠等。**定义在 frontend，使用权按线裁剪**：每条线在自己的 `consumed_shared_args()` 里列出「我读哪些」，用户显式提供了清单之外的共享 flag 就报错。
3. **线独占 flag**：各线经 `augment_cli` 挂上（如 Qwen3 的 `--enable-lora`、`--batch-invariant`），天然只属于那一条线（u2-l4 精读）。

为什么要把「共享」单独抽一层？因为如果每个旋钮都塞进各模型 crate，同一名词（比如 `--tp-size`）会在 8 个 crate 里重复定义 8 遍，拼写、默认值、帮助文案会各自漂移；反过来全塞进 server，又会让 server 认识模型专属概念、破坏 u2-l1 讲过的纯分发接缝。`SharedArgs` 是折中：**通用名词集中定义一次，消费权按线声明**。

#### 4.1.2 核心流程

一条共享 flag 从声明到生效，要经过这条流水线（编号对应四道门，前两道在 clap 解析内部，后两道在 main 里）：

```text
声明期（编译期 + 进程启动）
  SharedArgs 字段的 #[arg(...)] 属性        ← 拼写、默认值、value_parser
  ModelLineRegistry::new                    ← 校验 SHARED_ARG_REQUIREMENTS
      └─ build_command                      ← 把依赖表转成 clap requires_all

运行期（main 逐阶段）
  ① get_matches                             ← 值门 + 依赖门（clap，失败即退出码 2）
  ② provided_args(matches)                  ← 算出「用户显式提供」集合
  ③ 读 config.json → registry.detect        ← 裁决出唯一一条线
  ④ registry.validate_provided              ← 归属门（consume-or-reject）
  ⑤ shared.validate(&provided)              ← 语义门·共享层规则
  ⑥ line.validate(&ctx, &provided)          ← 语义门·线内规则（u2-l4）
  ⑦ launch（spawn_blocking）                ← 昂贵的引擎加载，最后才发生
```

要点：**所有 CLI 层面的裁决（①–⑥）都排在引擎加载（⑦）之前**，且越便宜的越靠前——u1-l3 讲过的「廉价校验前置」在参数面上的具体化。

#### 4.1.3 源码精读

先看三层里的第一层——核心 flag 表，以及注册表对它的自检：

[pegainfer-frontend/src/model_line.rs:68-74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L68-L74) —— `CORE_ARGS` 是 4 个 flag id 的静态切片，文档一句话：「无论检测出什么类型，每条模型线都接受的 flag」。

[pegainfer-frontend/src/model_line.rs:510-515](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L510-L515) —— 注册表构造时逐个断言 `CORE_ARGS` 里的 id 确实存在于 `SharedArgs`——核心表与结构体是两份手写的事实，这里用断言把它们钉在一起（改了字段忘改表，启动即 panic，而不是运行时静默放行）。

共享层的主体是这一个结构体（20 个字段，每个字段的文档注释就是 `--help` 里那段文案）：

[pegainfer-frontend/src/model_line.rs:94-103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L94-L103) —— 结构体上方的**普通注释**解释了设计：「被多于一条模型线共享的 CLI flag；每条线经 `consumed_shared_args` 声明自己读的子集，子集之外提供共享 flag 会在校验时失败」。注意一个 clap derive 的小坑（注释原话）：这里故意用普通注释而非文档注释，因为结构体上的文档注释会被 clap 当作命令的 about 文案、**覆盖掉** main.rs 里设置的 `about`。

[pegainfer-frontend/src/model_line.rs:122-127](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L122-L127) —— `cuda_graph` 是「带值布尔」的样板：`default_value_t = true` 加 `action = clap::ArgAction::Set`，于是写法是 `--cuda-graph=false`。它的文档注释还顺带声明了各线对它的态度（GLM5.2 拒绝、Qwen3 LoRA 模式强制关、Qwen3.5 拒绝 `false`……）——**同一面旗，不同线的语义在各自的 `validate` 里兑现**。

[pegainfer-frontend/src/model_line.rs:160-170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L160-L170) —— KV 卸载的两个旗子：`kv_offload_host_gib` 用自定义 `value_parser = parse_offload_gib`（见 4.2.3），`kv_offload_hugepages` 是普通开关。注意 `kv_offload_host_gib` 有默认值 8.0——但它被 `SHARED_ARG_REQUIREMENTS` 钉在 `--kv-offload` 上，所以默认值从不生效，除非用户同时开了卸载（4.2 展开）。

[pegainfer-frontend/src/model_line.rs:212-220](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L212-L220) —— prefill/decode 交叠的两件套：`decode_overlap` 是三值枚举（`off`/`stream`/`green-ctx`，枚举定义在 [pegainfer-frontend/src/model_line.rs:81-92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L81-L92)）；`decode_sm_pct` 带 `value_parser!(u32).range(1..=99)`——**值域校验发生在 clap 解析期**，本讲实践的第二条错误就与它成对。

完整的字段表（本讲的参考件，也是综合实践的核对目标）：

| # | 字段 id | CLI 拼写 | 默认值 | 类别 | Qwen3 消费 |
| --- | --- | --- | --- | --- | --- |
| 1 | `model_path` | `--model-path` | `<server crate>/../models/Qwen3-4B`（编译期拼接，[L79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L79)） | 核心 | 全线可用 |
| 2 | `served_model_name` | `--served-model-name` | 无（回落到模型路径） | 核心 | 全线可用 |
| 3 | `port` | `--port` | 8000 | 核心 | 全线可用 |
| 4 | `tool_call_parser` | `--tool-call-parser` | `auto` | 核心 | 全线可用 |
| 5 | `cuda_graph` | `--cuda-graph` | true（写 `=false` 关闭） | 共享 | ✓ |
| 6 | `dump_graph_png` | `--dump-graph-png` | 无 | 共享 | ✓ |
| 7 | `device_ordinal` | `--device-ordinal` | 0 | 共享 | ✓ |
| 8 | `tp_size` | `--tp-size` | 1 | 共享 | ✓ |
| 9 | `dp_size` | `--dp-size` | 无 | 共享 | **✗**（Kimi-K2/GLM5.2 消费） |
| 10 | `kv_offload` | `--kv-offload` | false | 共享 | ✓ |
| 11 | `kv_offload_host_gib` | `--kv-offload-host-gib` | 8.0（须正有限数） | 共享 | ✓ |
| 12 | `kv_offload_hugepages` | `--kv-offload-hugepages` | false | 共享 | ✓ |
| 13 | `kv_p2p_metaserver_addr` | `--kv-p2p-metaserver-addr` | 无 | 共享 | ✓ |
| 14 | `kv_p2p_advertise_addr` | `--kv-p2p-advertise-addr` | 无 | 共享 | ✓ |
| 15 | `kv_p2p_nics` | `--kv-p2p-nics` | 空表（逗号分隔多值） | 共享 | ✓ |
| 16 | `no_prefix_cache` | `--no-prefix-cache` | false | 共享 | ✓ |
| 17 | `dflash_draft_model_path` | `--dflash-draft-model-path` | 无 | 共享 | ✓ |
| 18 | `max_prefill_tokens` | `--max-prefill-tokens` | 无 | 共享 | ✓ |
| 19 | `decode_overlap` | `--decode-overlap` | off | 共享 | ✓ |
| 20 | `decode_sm_pct` | `--decode-sm-pct` | 20（限 1..=99） | 共享 | ✓ |

消费权的声明处——Qwen3 的清单：

[pegainfer-qwen3/src/model_line.rs:149-167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L149-L167) —— Qwen3Line 消费 15 个共享 flag：**16 个非核心共享 flag 里除了 `dp_size` 之外全要**。Qwen3 是 TP 线（单机 1–2 卡），没有数据并行面，`--dp-size` 对它没有意义——用户显式给了就会被归属门拒成 `--dp-size is not used by Qwen3`。

各线消费面的横向对比（体会「子集」二字——从 1 项到 15 项不等）：

| 模型线 | 消费项数 | 清单要点 |
| --- | --- | --- |
| deepseek-v2-lite | 1 | 仅 `cuda_graph`（[L27-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-deepseek-v2-lite/src/model_line.rs#L27-L29)） |
| gemma4 | 3 | `device_ordinal`、`cuda_graph`、`tp_size`（[L22-24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-gemma4/src/model_line.rs#L22-L24)） |
| kimi-k2 | 3 | `tp_size`、`dp_size`、`cuda_graph`（[L72-74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-kimi-k2/src/model_line.rs#L72-L74)） |
| qwen35 | 7 | 不碰任何 KV 卸载/P2P flag（[L95-105](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen35/src/model_line.rs#L95-L105)） |
| glm52 | 11 | 含 `dp_size` 与全部 KV 卸载/P2P，但不要 `device_ordinal`/`decode_overlap`（[L106-120](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-glm52/src/model_line.rs#L106-L120)） |
| qwen3 | 15 | 除 `dp_size` 外全要（见上） |

归属门的裁决逻辑本身（哪个阶段查这张清单）：

[pegainfer-frontend/src/model_line.rs:605-630](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L605-L630) —— `validate_provided`：对 `provided` 集合里的每个 id，按「核心 → 被检测线消费的共享 → 该线独占」的顺序短路检查，三样都不是就返回 `CliError::UnconsumedFlag`。注意它只查**显式提供**的 flag（入参是 `provided`，不是全部字段）——这就是为什么默认值填充的 `dp_size=None` 不会误伤 Kimi-K2 之外的线。

#### 4.1.4 代码实践：亲手推导并核对字段表

1. **实践目标**：不抄讲义，从源码与二进制两边独立推出上面那张 20 行字段表，并核对一致。
2. **操作步骤**：
   - 通读 [pegainfer-frontend/src/model_line.rs:98-221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L98-L221)，每个字段抄三样：字段名、CLI 拼写（注意显式 `long = "..."` 覆盖者，如 `dflash_draft_model_path`）、默认值。
   - 对照 [pegainfer-qwen3/src/model_line.rs:149-167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L149-L167) 填「Qwen3 消费」列。
   - 运行 `cargo run --release -- --help`，逐行核对拼写与默认值；数一数 Qwen3 **独占** flag 有几个（答案：13 个，来自 `Qwen3Cli` 的 13 个字段）。
3. **需要观察的现象**：`--help` 的 Options 区先列共享 flag、后列 Qwen3 独占 flag（`build_command` 里 `SharedArgs::augment_args` 先挂、各线 `augment_cli` 后挂，见 [pegainfer-frontend/src/model_line.rs:552-556](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L552-L556)）；`--cuda-graph <CUDA_GRAPH>` 带值占位符而 `--kv-offload` 是裸开关。
4. **预期结果**：三处来源（结构体、消费清单、`--help`）完全对齐；你能回答「Qwen3 构建的二进制里用户能显式提供的 flag 总数」＝ 4 核心 + 15 消费共享 + 13 独占 = 32。
5. 若本机构建受限（见 u1-l2），`--help` 一步可改为阅读 [pegainfer-qwen3/src/model_line.rs:35-116](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L35-L116) 的 `Qwen3Cli` 定义，结论**待本地验证**的只是呈现顺序。

#### 4.1.5 小练习与答案

**练习 1**：`CORE_ARGS` 里的 4 个 flag 与其他 16 个共享 flag 的本质区别是什么？为什么 K3 的 `consumed_shared_args`（[pegainfer-k3/src/model_line.rs:193-207](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-k3/src/model_line.rs#L193-L207)）把 `model_path`/`served_model_name`/`port` 也列了进去？

**答案**：核心 flag 由 **server 二进制自己消费**（main.rs 读它们拼 HTTP 前端），与检测出的线无关，所以无条件放行；其余共享 flag 的消费权按线声明。K3 把核心三项也列出来是**冗余但无害**的——那边的注释明说是为了让清单「一眼看全这条线接受的全部 flag」；`validate_provided` 的短路顺序（核心检查在最前）保证重复声明不改变结果。

**练习 2**：`--cuda-graph=false` 与 `--kv-offload=false`，哪个能通过解析？为什么？

**答案**：前者能。`cuda_graph` 声明了 `action = clap::ArgAction::Set`（带值布尔，L126）；`kv_offload` 是普通 `bool` 字段加 `default_value_t = false`（L157），clap 把它当作出现即 true 的开关，**不接受值**——`--kv-offload=false` 直接解析失败（想表达「关」就是不写它）。

**练习 3**：用户在 stock 构建（只有 Qwen3）上运行 `--model-path <某个 qwen3 目录> --dp-size 8`，会发生什么？消息长什么样？

**答案**：解析期一切正常（`dp_size` 合法地挂在命令上）；检测出 Qwen3 后，归属门 `validate_provided` 发现 `dp_size` 既非核心、也不在 Qwen3 的消费清单、也非其独占 flag，返回 `CliError::UnconsumedFlag`，消息逐字为 `--dp-size is not used by Qwen3`（模板见 [pegainfer-frontend/src/model_line.rs:54-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L54-L55)，qwen3 的单测 `rejects_other_lines_flags` 恰好断言这条，[pegainfer-qwen3/src/model_line.rs:502-510](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L502-L510)）。

### 4.2 跨 flag 规则的两层执行：`requires_all` 与 `validate()`

#### 4.2.1 概念说明

「`--kv-offload-host-gib` 必须伴随 `--kv-offload`」这类**跨 flag 规则**在 PegaInfer 里分两层落地：

- **依赖门（声明式，clap `requires_all`）**：形如「给了 A 就必须给 B」的**存在性依赖**。数据源是两张表——共享层的 `SHARED_ARG_REQUIREMENTS`（5 条）与各线的 `arg_requirements()`（Qwen3 有 4 条）；注册表把它们统一转成 clap 的 `requires_all`，由 clap **在解析期**执行。错误消息由 clap 模板渲染（列缺失参数 + usage 行），项目不可控文案。
- **语义门（命令式，手写 `validate`）**：依赖**值**或**模式**的规则（「`--decode-sm-pct` 只在 green-ctx 模式下有意义」「`--dump-graph-png` 需要开 CUDA Graph」）。`SharedArgs::validate` 放**对每个消费者都成立**的共享层规则；线特有规则放各线的 `validate`（u2-l4）。错误消息是项目自己的 thiserror 模板（`CliError::Rule` 散文），**文案即 API**。

为什么不全用 clap 属性声明依赖？trait 文档给了答案：**clap 自带的 schema 断言在 release 构建里是禁用的**——纯 clap 属性写法在 release 下失去「被引用的 id 必须存在且归属正确」这层保护。所以注册表在构造时自己校验每条依赖的 source/target 归属，再转成 `requires_all`。

#### 4.2.2 核心流程

四道门与两层规则的完整地图（一条 flag 的视角）：

```text
                     ┌─ 解析期（get_matches，失败 → clap 渲染错误，退出码 2）
  值门               │  value_parser：类型 / range(1..=99) / parse_offload_gib
  依赖门             │  requires_all：SHARED_ARG_REQUIREMENTS + 各线 arg_requirements
                     └──────────── config.json 尚未被读取！────────────
                     ┌─ 检测后、引擎加载前（失败 → anyhow 上抛，退出码 1）
  归属门             │  validate_provided：核心 / 被消费共享 / 独占，三选一
  语义门·共享        │  SharedArgs::validate（本节）
  语义门·线内        │  ModelLine::validate（u2-l4）
                     └────────────────────────────────────────────────
  引擎加载           │  launch（昂贵的 GPU/权重工作，永远最后）
```

一个常被忽略的时序细节：**依赖门连 config.json 都还没读**——所以触发它不需要任何模型目录；而语义门发生在 detect 之后，需要一份能被认领的 config.json 才走得到。本节实践正是利用这个时序差设计两条对照实验。

#### 4.2.3 源码精读

共享层的依赖表：

[pegainfer-frontend/src/model_line.rs:223-232](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L223-L232) —— `SHARED_ARG_REQUIREMENTS`：5 条 `(源, 依赖目标列表)`。KV 卸载三件套钉在 `--kv-offload` 上；KV P2P 三件套互相咬合。注意 `kv_p2p_advertise_addr` 只要求 `kv_p2p_metaserver_addr`，看似漏了 `kv_offload`——但 `kv_p2p_metaserver_addr` 自己要求 `[kv_offload, kv_p2p_advertise_addr, kv_p2p_nics]`，clap 的 requires 按**传递闭包**生效，链路最终闭合。

线内依赖表的样板（依赖目标可以跨「独占 → 共享」）：

[pegainfer-qwen3/src/model_line.rs:169-179](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L169-L179) —— Qwen3 的 4 条依赖全是 KV P/D（与 vLLM 对接的 prefill/decode 分离）相关：独占 flag `kv_pd_vllm_seed` 要求共享 flag `kv_p2p_metaserver_addr` 加自己的 `kv_pd_vllm_namespace`——依赖可以横跨独占与共享两层。

依赖表如何变成 clap 行为、注册表又如何自检：

[pegainfer-frontend/src/model_line.rs:552-564](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L552-L564) —— `build_command` 用 `mut_args` 遍历每个参数，凡 id 出现在依赖表里就给它 `requires_all(targets)`。注释解释了为什么不用逐个 `mut_arg`：那会重排参数、打乱 `--help` 的顺序。

[pegainfer-frontend/src/model_line.rs:464-477](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L464-L477) —— `validate_requirement`：注册表构造期对每条依赖断言三件事——source 确归声明者所有、target 不能是 source 自己、target 必须是共享或同一条线的（不允许 A 线的 flag 依赖 B 线的 flag）。在 [pegainfer-frontend/src/model_line.rs:536-543](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L536-L543) 逐条执行；`ModelLineRegistry::new` 的文档（[L503-506](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L503-L506)）点明动机：release 构建里 clap 的等价 schema 断言被禁用，所以自己验。

语义门·共享层的四条规则：

[pegainfer-frontend/src/model_line.rs:234-259](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L234-L259) —— `SharedArgs::validate`，文档定位精准：「对**每个消费者**都成立的共享 flag 交互；线特有规则住在各线的 `ModelLine::validate`」。四条规则依次：

1. `dump_graph_png.is_some() && !cuda_graph` → `--dump-graph-png requires --cuda-graph=true`（**纯值判断**，不查 provided——因为两个默认值 `None`/`true` 天然不触发）。
2. `provided.contains("device_ordinal") && tp_size > 1` → 显式给 `--device-ordinal` 在张量并行下会被无视，宁可报错也不静默（**必须查 provided**：默认值 0 时若查值，每次 TP 启动都会误伤）。
3. `provided.contains("decode_sm_pct") && decode_overlap != GreenCtx` → 本讲实践的对照错误（同理**必须查 provided**：默认 20 不该触发）。
4. `validate_parser_overrides(tool_call_parser)` → 解析器名是否真实注册过——注释点明动机：显式解析器名被 `FromStr` 原样接受，只有注册表能分辨拼写错误，「赶在引擎加载浪费之前」在这里查。

自定义值解析器（值门的一员）：

[pegainfer-frontend/src/model_line.rs:262-271](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L262-L271) —— `parse_offload_gib`：`f64` 能解析只是第一步，还必须是正的有限数——`NaN`/`inf`/`0`/负数都在**解析期**被拒，错误消息自带 flag 名（`invalid --kv-offload-host-gib: ...`）。

两层门在 main 里的触发顺序：

[src/main.rs:157-166](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L157-L166) —— `get_matches()`（值门 + 依赖门在此刻生效）→ `provided_args` → **之后**才读 config.json——依赖门的错误发生在任何文件系统访问之前。

[src/main.rs:177-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L177-L186) —— 检测之后的语义门链条：`validate_provided`（归属门）→ `shared.validate`（共享语义门）→ `line.validate`（线内语义门）。三行代码就是四道门的后三道。

#### 4.2.4 代码实践：触发两条措辞来源不同的错误

这是本讲的核心运行实践，**全程无需 GPU 与权重**。

1. **实践目标**：在真实二进制上分别命中依赖门与语义门，观察消息形态、发生时机与退出码的差异，并逐字把消息对应回模板源头。
2. **操作步骤**：
   - **实验 A（依赖门）**：直接运行
     ```bash
     cargo run --release -- --kv-offload-host-gib 8
     ```
     注意**没有** `--model-path`、没有任何模型目录——这是有意的。
   - **实验 B（语义门）**：先建一个只含 config.json 的目录（示例 fixture，内容只需让 Qwen3 的 probe 认领，见 [pegainfer-qwen3/src/config.rs:895-910](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/config.rs#L895-L910)——probe 只查 `model_type` 与 `architectures` 两项）：
     ```json
     {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}
     ```
     然后运行：
     ```bash
     cargo run --release -- --model-path <该目录> --decode-sm-pct 50
     ```
   - 对两个实验分别记录：错误消息全文、是否出现 `=== pegainfer - Qwen3 (GPU) ===` 横幅、退出码（`echo $?`）。
3. **需要观察的现象**：
   - 实验 A：clap 渲染的错误，含「以下必需参数未提供」一类措辞并列出缺失的 `--kv-offload`，带 usage 行；无横幅（还没读到横幅那一步）；退出码 2。
   - 实验 B：一行散文 `--decode-sm-pct only applies with --decode-overlap=green-ctx`；**无横幅**（横幅在 main.rs L188，而 `shared.validate` 在 L178——错误发生在横幅之前、引擎加载之前）；退出码 1。
   - 加做对照：`--decode-sm-pct 50 --decode-overlap green-ctx` 现在能通过全部四道门，进程推进到引擎加载、因目录里没有权重而失败——失败点后移本身就是「CLI 裁决全部前置」的证据。
4. **预期结果（两条错误为何措辞来源不同）**：
   - 实验 A 的消息由 **clap 框架模板**渲染：`SHARED_ARG_REQUIREMENTS` 第 1 条（L224）经 `build_command` 的 `requires_all`（L558-563）装到 `kv_offload_host_gib` 上，clap 在解析期发现 flag 出现而依赖缺失，用**它自己的**错误格式（MissingRequiredArgument：缺失清单 + usage）报告——文案归框架所有，项目只能控制「哪条依赖」。
   - 实验 B 的消息是**项目自己的 thiserror 模板**：`SharedArgs::validate` 第 3 条（L248-252）构造的 `CliError::Rule("--decode-sm-pct only applies with --decode-overlap=green-ctx")`，经 main.rs L178 的 `?` 转成 anyhow 错误上抛——一行散文、无 usage、退出码 1。文案即 API，测试可以逐字断言。
   - 一句话总结：**前者是「clap 替你写的错」，后者是「项目自己写的错」**；时序上前者连 config.json 都没碰，后者已经过检测。
5. 确切的终端输出格式（clap 的缺失参数文案、anyhow 的 `Error:` 前缀）与退出码数值**待本地验证**；无法构建二进制时，替代方案是综合实践里的 `parse_for_line` 草稿测试（同样覆盖两层）。

#### 4.2.5 小练习与答案

**练习 1**：`--decode-sm-pct 0` 会被四道门中的哪一道拒绝？发生在什么时候？

**答案**：**值门**。`decode_sm_pct` 的属性是 `value_parser!(u32).range(1..=99)`（L219），0 越界在 clap 解析期即被拒——与实验 A 同一时刻（读 config.json 之前）、同一种 clap 渲染错误、退出码 2。对比实验 B：`50` 是合法值，值门放行，最后倒在语义门。

**练习 2**：为什么 `kv_p2p_advertise_addr` 的依赖清单里没有 `kv_offload`？这样写安全吗？

**答案**：安全，因为 clap 的 requires 按传递闭包生效：`--kv-p2p-advertise-addr` 要求 `--kv-p2p-metaserver-addr`（L230），而后者要求 `[kv_offload, kv_p2p_advertise_addr, kv_p2p_nics]`（L226-229）——单独给 `--kv-p2p-advertise-addr` 时，缺失清单最终会补齐到 `--kv-offload` 与 `--kv-p2p-nics`。链式声明避免了三处重复写全量依赖。

**练习 3**：把「`--dump-graph-png` 需要开 CUDA Graph」从 `SharedArgs::validate` 搬进 clap 的 `requires_all` 可行吗？有什么代价？

**答案**：**不可行**（用 `requires_all` 表达不了）。该规则的条件是**值**（`cuda_graph` 必须为 `true`），而 `requires_all` 只表达**存在性**（「给了 A 就必须也给 B」）；`--cuda-graph=false` 时 `--cuda-graph` 明明「提供了」，依赖门只会检查它存在、不会检查它的值。值敏感的规则只能落在语义门手写——这正是两层分工的判据：存在性依赖进依赖表，值/模式规则进 `validate`。

### 4.3 provided_args()：用 ValueSource 区分「显式提供」与「默认填充」

#### 4.3.1 概念说明

整个校验体系有一个隐含前提：**只有「用户显式写的 flag」才需要裁决归属与规则；「默认值填充的 flag」不算用户意志**。原因很直接——20 个共享 flag 里绝大多数带默认值（`decode_sm_pct=20`、`cuda_graph=true`、`port=8000`……），如果把默认值也算「提供了」，那么：

- 归属门会把每个默认填充的 flag 都拿去问「被这条线消费了吗」——gemma4 不消费 `decode_sm_pct`，于是**每一次** gemma4 启动都死于 `UnconsumedFlag`；
- 语义门的 `provided.contains("decode_sm_pct")` 检查会永真，`--decode-overlap` 默认 off 时所有启动全被 green-ctx 规则拒绝。

clap 的 `ArgMatches` 恰好为每个值记录了出身 `ValueSource`：`CommandLine`、`EnvVariable`、`DefaultValue`……`provided_args()` 就是把这个出身过滤出来，汇成一张 `BTreeSet<String>`——「用户显式提供的 flag id 集合」。它是归属门与两层语义门的**共同入参**，也被 main 打进日志、被各线的 `validate` 复用。

#### 4.3.2 核心流程

```text
命令行 argv ──clap 解析──> ArgMatches（每个值带 ValueSource）
                                │
                    provided_args(matches, cmd)
                                │
              ┌─────────────────┴─────────────────┐
              │ 过滤 1：只留真实参数 id            │  （matches.ids() 也产出
              │         （丢掉 clap 合成的组 id）   │    合成的 group id）
              │ 过滤 2：ValueSource ∈ {CommandLine,│
              │          EnvVariable}              │
              └─────────────────┬─────────────────┘
                                ▼
                BTreeSet<String>（如 {"model_path", "tp_size"}）
                                │
     ┌──────────────────────────┼──────────────────────────┐
     ▼                          ▼                          ▼
validate_provided        shared.validate             line.validate
（归属门）           （provided.contains 查成员）   （如 Qwen3 的 max_loras 检查）
     └──────────── 还有 main 的启动日志：user-set flags {provided:?} ──────────┘
```

#### 4.3.3 源码精读

函数本体只有 20 行：

[pegainfer-frontend/src/model_line.rs:640-661](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L640-L661) —— 两道过滤：先用命令自己的真实参数 id 集合做交集（注释解释：`matches.ids()` 也会产出 clap 的**合成组 id**，必须滤掉，否则组 id 会被当成 flag 拿去查归属）；再按 `ValueSource` 只留 `CommandLine | EnvVariable`。文档一句话：「用户显式设置（命令行或环境变量）的 arg id，供 consume-or-reject 用」。

它的直接消费者之一是启动日志——`provided` 集合在运行时是**可观察**的：

[src/main.rs:190-194](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L190-L194) —— 引擎加载之前打印 `Runtime: model_path=…, user-set flags {provided:?}`——把 BTreeSet 的 Debug 输出直接给用户看。这行日志是调试「我的 flag 去哪了」的第一现场（flag 被归属门拒掉时，对照这行能立刻看出 clap 眼中你显式给了什么）。

线内语义门对同一集合的复用：

[pegainfer-qwen3/src/model_line.rs:191-197](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L191-L197) —— Qwen3 的规则「`--max-loras`/`--max-lora-rank` 必须伴随 `--enable-lora`」：两个旗子都有默认值（64 之类），若不查 provided 而查「值非默认」，用户永远无法在非 LoRA 模式下静默使用默认值——所以同样以 `provided.contains(...)` 为条件。**同一张集合，三层语义门共用**。

单测把行为钉死：

[pegainfer-frontend/src/model_line.rs:974-986](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L974-986) —— `provided_args_reports_only_explicitly_set_flags`：解析 `["test", "--tp-size", "2", "--line-a-flag"]` 后断言集合含 `tp_size` 与 `line_a_flag`、**不含** `port`（尽管 port 有默认值 8000，`ArgMatches` 里它的值存在、出身是 `DefaultValue`）。

以及归属门对「未消费共享 flag」的拒绝样例（与 4.1 表格呼应）：

[pegainfer-frontend/src/model_line.rs:917-926](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L917-L926) —— StubLine 未声明消费 `kv_offload`，显式提供即得 `--kv-offload is not used by LineA`，逐字断言。

顺带说明 `EnvVariable` 分支的现状：`SharedArgs` 目前**没有任何字段声明 `.env(...)`**，所以实践中集合里只会出现 `CommandLine` 出身的 id；保留 `EnvVariable` 过滤是让「将来某 flag 改从环境变量读取」时语义不突变——环境变量的值同样是用户意志，理应接受裁决。

#### 4.3.4 代码实践：亲眼看 provided 集合

1. **实践目标**：让 `provided_args` 的结果在运行时可见，并验证「值等于默认值的显式 flag」也在集合里。
2. **操作步骤**：
   - 复用 4.2.4 实验 B 的 config.json 目录，依次运行（每次都让进程走到引擎加载失败为止，那正好在日志行之后）：
     ```bash
     cargo run --release -- --model-path <目录> --port 9000
     cargo run --release -- --model-path <目录> --port 9000 --tp-size 1
     ```
   - 各自找到 `user-set flags {...}` 这行日志（在 `=== pegainfer - Qwen3 (GPU) ===` 横幅与 `Loading engine...` 之后、引擎报错之前）。
   - 再读单测 [pegainfer-frontend/src/model_line.rs:974-986](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L974-L986)，与日志互相印证。
3. **需要观察的现象**：第一次的集合是 `{"model_path", "port"}`；第二次多了 `"tp_size"`——**尽管 `--tp-size 1` 的值与默认值完全相同**，它仍在集合里，因为出身是 `CommandLine`。两次都不含 `decode_sm_pct`、`cuda_graph` 等默认填充的 flag。
4. **预期结果**：你能用一句话说清 provided 语义：「集合记录的是**用户写了什么**，不是**值是什么**」；并解释为什么语义门的条件必须写成 `provided.contains(...)` 而不是比较值——后者在「用户显式写了等于默认值的值」与「用户没写」两种场景下无法区分，前者天然区分。
5. 日志的精确格式（含 BTreeSet 的 Debug 花括号渲染）与 info 级别是否默认可见**待本地验证**（`logging::init_default()` 的级别配置见 pegainfer-core）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `provided_args` 不过滤 `ValueSource`（把默认值也算「提供」），最先崩掉的是哪一层？给出一个具体启动场景。

**答案**：归属门。`validate_provided` 会把每个默认填充的 flag 拿去查归属：用 `--features gemma4` 构建、正常启动 gemma4（消费清单只有 `device_ordinal`/`cuda_graph`/`tp_size` 三项），默认填充的 `decode_sm_pct=20`、`no_prefix_cache=false` 等十余个 id 都不在清单里，进程在检测后立刻死于 `--decode-sm-pct is not used by Gemma 4` 一类的错误——**任何非满配线都无法启动**。

**练习 2**：`--tp-size 1` 与不写 `--tp-size`，在 `ArgMatches` 与 `provided_args` 两个层面各有什么区别？

**答案**：`ArgMatches` 层面两者几乎无区别——`tp_size` 的值都是 1、`get::<usize>("tp_size")` 都返回 `Some(1)`；区别只在 `value_source("tp_size")`：前者 `CommandLine`、后者 `DefaultValue`。`provided_args` 层面因此一个含 `tp_size`、一个不含。这正是「值比较法」不可用、「出身过滤法」可用的最小反例。

**练习 3**：为什么 `matches.ids()` 需要第一道过滤（对齐真实参数 id）？组 id 是什么？

**答案**：clap 支持 argument group（把若干 flag 归组、按组设互斥/依赖）；解析结果里这些**组**也会作为 id 出现在 `matches.ids()` 中——它们不是任何真实 flag。若不滤掉，组 id 会流进 `provided` 集合，被归属门当作未知 flag 拒绝（既非核心、非共享、也非线独占），导致合法命令被误拒。代码注释原话：「matches.ids() also yields clap's synthetic group ids; keep only real args」（L642）。

## 5. 综合实践：一条 flag 过四道门

把本讲三个模块串成一个任务：以 `--kv-offload-host-gib` 和 `--decode-sm-pct` 为标本，把「三层 flag + 四道门 + provided 语义」完整走一遍。分三部分，前两部分无需 GPU。

**第一部分：产出并核对字段表（4.1 的交付物）**

按 4.1.4 的步骤独立整理 20 行字段表（字段名 / CLI 拼写 / 默认值 / Qwen3 是否消费），并与 4.1.3 的参考表核对。你的表应能回答：Qwen3 构建下用户可显式提供的 flag 总数是多少（32），`dp_size` 归谁消费（Kimi-K2/GLM5.2）。

**第二部分：三条 argv、三道门（4.2 + 4.3 的运行验证）**

在**你自己的练习 checkout**里建一个集成测试（示例代码，放在如 `pegainfer-qwen3/tests/scratch_shared_args.rs`；需要工作区可构建，见 u1-l2，无需 GPU 权重）：

```rust
// 示例代码：用 frontend 公开的测试帮手 parse_for_line 复现 server 的校验链
use pegainfer_frontend::model_line::parse_for_line;
use pegainfer_qwen3::model_line::MODEL_LINE;

#[test]
fn shared_flag_rules_fire_at_distinct_layers() {
    // 依赖门：解析期即失败（requires_all 缺 --kv-offload），消息由 clap 渲染
    let e = parse_for_line(&MODEL_LINE, &["pegainfer", "--kv-offload-host-gib", "8"])
        .unwrap_err();
    assert!(e.to_string().contains("required arguments"), "{e}");

    // 归属门：dp_size 不在 Qwen3 消费清单，消息来自 thiserror 模板
    let e = parse_for_line(&MODEL_LINE, &["pegainfer", "--dp-size", "8"]).unwrap_err();
    assert!(e.to_string().contains("--dp-size is not used by Qwen3"), "{e}");

    // 语义门：decode_sm_pct 提供但模式不是 green-ctx
    let e = parse_for_line(&MODEL_LINE, &["pegainfer", "--decode-sm-pct", "50"]).unwrap_err();
    assert!(e.to_string().contains("--decode-overlap=green-ctx"), "{e}");

    // 值门：0 越过 1..=99 的边界，同样在解析期被拒
    let e = parse_for_line(&MODEL_LINE, &["pegainfer", "--decode-sm-pct", "0"]).unwrap_err();
    assert!(e.to_string().contains("invalid value"), "{e}");
}
```

`parse_for_line`（[pegainfer-frontend/src/model_line.rs:665-681](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L665-L681)）正是 server 主流程「解析 → provided → 归属门 → 共享语义门」的测试版收拢，qwen3 自己的单测也用它。运行 `cargo test --release -p pegainfer-qwen3 --test scratch_shared_args`；断言里的具体消息片段（clap 的 "required arguments"/"invalid value" 措辞）以本机为准，**待本地验证**。

**第三部分：把现象对应回源码（验收）**

对照 4.2.4 的两个运行实验与本测试的四个断言，填一张归因表：

| 输入 | 被哪道门拒绝 | 消息出自谁的手 | 发生时刻（config.json 读取前/后） |
| --- | --- | --- | --- |
| `--kv-offload-host-gib 8` | 依赖门 | clap 模板 | 前 |
| `--decode-sm-pct 0` | 值门 | clap 模板 | 前 |
| `--dp-size 8` | 归属门 | `CliError::UnconsumedFlag` 模板 | 后 |
| `--decode-sm-pct 50` | 语义门 | `CliError::Rule` 散文 | 后 |

能不看讲义填满并说清每行的源码行号依据，本讲即通关。

## 6. 本讲小结

- **三层 flag**：核心（`CORE_ARGS` 4 个，server 自消费、全线可用）/ 共享（`SharedArgs` 其余 16 个，消费权由各线 `consumed_shared_args` 声明，Qwen3 要 15 个、gemma4 只要 3 个）/ 线独占（`augment_cli` 挂上）。「定义」与「使用权」分离是参数面的核心设计。
- **四道门**：值门（`value_parser`，如 `range(1..=99)`、`parse_offload_gib`）与依赖门（`requires_all`，来自 `SHARED_ARG_REQUIREMENTS` 与各线 `arg_requirements`）在**解析期**由 clap 执行，连 config.json 都未读取、退出码 2；归属门（`validate_provided` 的 consume-or-reject）与语义门（`SharedArgs::validate` + 线内 `validate`）在**检测之后、引擎加载之前**，错误经 anyhow 上抛、退出码 1。
- **两层规则的判据**：存在性依赖（给了 A 必须给 B）进依赖表、由 clap 兜底；值/模式敏感的规则（`=false`、green-ctx 才适用）只能手写进 `validate`——`requires_all` 表达不了值。依赖不走 clap 属性的原因是 release 构建禁用了 clap 的 schema 断言，注册表在构造期自验每条依赖的归属。
- **provided 语义**：`provided_args` 用 `ValueSource` 过滤出「用户显式写过的 flag id 集合」（只认 `CommandLine`/`EnvVariable`），三层校验共用这张集合；`--tp-size 1` 与不写 `--tp-size` 值相同但出身不同——「值比较」不可用、「出身过滤」才成立，否则默认值会误杀每一个非满配模型线。
- **可观察性**：`provided` 集合经 `Runtime: … user-set flags {provided:?}` 日志在引擎加载前打印，是排查 flag 归属问题的第一现场。

## 7. 下一步学习建议

- **u2-l3（ModelLineRegistry）**：本讲多次借用的注册期自检——flag id/拼写唯一性 panic、`validate_requirement` 的归属断言、`detect` 的收集逻辑——在那里完整展开。
- **u2-l4（Qwen3Line 的校验矩阵）**：本讲只示范了线内语义门的一条规则（`max_loras` 查 provided）；下一讲拆完整冲突矩阵（LoRA、batch-invariant、eagle3、DFlash）与 `launch` 如何把 `SharedArgs` + `Qwen3Cli` 折成 `Qwen3LaunchOptions`。
- **u2-l5（ServePlan）**：核心 flag 之一的 `tool_call_parser` 如何经 `plan.resolve_tool_call_parser` 与线自带默认合成最终选择。
- 想动手：把综合实践的草稿测试扩一条——`["pegainfer", "--decode-sm-pct", "50", "--decode-overlap", "green-ctx"]` 应当**通过** `parse_for_line`（返回 Ok），再对照 4.2.4 的对照实验，体会「过了 CLI 四道门之后，进程才第一次碰到真正的模型世界」。
