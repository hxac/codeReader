# pegainfer-sample 是什么：全局定位与分层架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 `pegainfer-sample` 这个 crate 在整个推理引擎里承担的两个**模型无关**职责：`select_batch`（批式选下一个 token）与 `token_logprobs_batch`（批式提取 logprob）。
2. 手画出 `pegainfer-kernels → pegainfer-sample → 各模型 crate` 的分层图，并说出每一层各自「拥有」什么。
3. 只凭源码列出这个 crate 的全部 `pub` API，并解释为什么它要用 `pub use` 把 `BatchSamplingRow` 等底层原语**再导出**出来。
4. 复述模块文档里 Kimi-K2 的例外：它为什么自带贪心路径，却仍然复用本 crate 的非贪心采样入口与 logprob 通道。

本讲是整套手册的第一篇，不要求你写任何代码，但要求你**逐行读**一个约 700 行的 `src/lib.rs` 的骨架——这个 crate 小而密，通读它的一半就等于握住了所有模型共享的采样策略。

## 2. 前置知识

本讲需要的背景概念不多，用通俗语言先补齐：

- **LLM 解码的最后一步**：模型每前向一次，产出一个「logits 矩阵」——每个正在生成的请求一行，每行是整个词表（几万到十几万个 token）上的原始得分。把一行得分变成「下一个 token 是谁」，就是**采样**（sampling）。最简单的采样是 argmax（取得分最高的那个 token，也叫贪心 greedy）；更常见的是按概率随机抽，用 `temperature`、`top_k`、`top_p`、`min_p` 这些参数控制随机性的形状（本讲只需知道名字，数学细节在单元二展开）。
- **logits arena（logits 竞技场）**：本项目把多个请求的 logits 行拼成一个连续的设备端矩阵，代码里叫 arena。可以把它想成一个 `行数 × 词表宽` 的 bf16 二维表。
- **logprob**：被选中 token 的对数概率 \(\log p(\text{picked})\)，由 log-softmax 算出。API 返回 `logprobs` 时就要算它，还常附带「概率最高的前 k 个候选」（top-k）。
- **Rust workspace 与 crate 分层**：这个仓库是 cargo workspace，每个子目录一个 crate。依赖方向就是分层方向：谁 `use` 谁，谁就在上层。
- **`pub use` 再导出（re-export）**：Rust 里把别的 crate 的项用自己的名字再次暴露出去。下游只需依赖本 crate，不必直接依赖底层 crate——这是「门面（facade）」手法。
- **CUDA 基础词汇**：device（显卡）、host（CPU 侧）、H2D/D2H（主机到设备 / 设备到主机的拷贝）、pinned memory（页锁定内存，D2H 异步回读的关键，单元四细讲）。

不需要你会写 CUDA；本讲引用的代码都是 Rust。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| `src/lib.rs`（约 700 行，本 crate 唯一源码文件） | 顶部 L1-L25 是模块文档，写明定位与分层；L27-L52 是 import / 再导出块；其余是采样与 logprob 的实现 | 本讲精读 L1-L52（文档 + API 面），实现体只看函数签名 |
| `Cargo.toml`（23 行） | 声明依赖：`anyhow` / `cudarc` / `half` / `pegainfer-frontend` / `pegainfer-kernels`，外加 dev 依赖 `criterion` 与一个 bench 目标 | 依赖表是分层图的直接证据 |
| （仓库存档文档，供交叉核对）`docs/subsystems/sampling/pegainfer-sample.md`、`docs/playbooks/hawk-visibility-audit.md` | 团队自己写的采样子系统文档与可见性审计记录 | 实践任务里用来核对一个命名差异 |

## 4. 核心概念与源码讲解

### 4.1 crate 模块文档与定位

#### 4.1.1 概念说明

一个 LLM 推理引擎有很多模型（Qwen3、Kimi-K2、GLM5.2……），但「把 logits 变成 token」这件事对每个模型都一样。如果每个模型 crate 各写一份采样，行为就会漂移：A 模型的贪心确定、B 模型的贪心随机、种子语义各不相同。`pegainfer-sample` 存在的意义就是**把采样收敛成唯一一层**：所有模型 crate 的采样都必须路由经过它。

模块文档第一句就给出定位：

> Batched token selection and host logprob formatting — the shared sampling layer every model crate routes through.

它承担两个**模型无关**（model-agnostic）的职责：

1. **`select_batch`**：一个 logits arena 进，每行一个 token id 出。文档特意强调「没有逐行的逃生舱」（no per-row escape hatch）——调用方无法退化成 `for i { sample(i) }` 这种逐行循环，批式入口是唯一形状。
2. **`token_logprobs_batch`**：批式设备端 logprob 提取；`token_logprob_from_row` 是它的人类可读的主机端单行参考实现（两者互证，单元三展开）。

#### 4.1.2 核心流程

两条平行的数据流，方向都是从模型 crate 流向用户：

```text
职责一：选 token
  模型 forward 产出 logits arena（bf16，行=请求，列=词表）
        │
        ▼
  select_batch(arena, params, steps, seed, scratch)
        │  按参数把行分成三路：贪心行 → 批式 indexed argmax
        │                    无种子采样行 → 一次 FlashInfer 批式调用
        │                    带种子行 → 逐行单调用（可重放）
        ▼
  Vec<u32>：每行一个 token id，按行序归位

职责二：logprob（仅当请求方要求时运行）
  同一个 arena + 每行选中的 token + top_k
        │
        ▼
  token_logprobs_batch（设备端一次 launch）
        │
        ▼
  Vec<TokenLogprob>：每行 { logprob, rank, top-k 候选 }
```

「路由」的判定核心是一个公开谓词 `effectively_greedy`：除了显式贪心参数，\( \text{top\_p} \le 1/V \)（\(V\) 为词表宽）时 softmax 核里只剩 argmax 一个 token（因为概率最大值恒满足 \( \max_i p_i \ge 1/V \)），这类行也必须走确定性的 argmax 路径。完整论证留给单元二，这里只需记住：**路由是策略，策略住在本 crate**。

#### 4.1.3 源码精读

模块文档全文只有 25 行，信息密度极高，逐段读：

[src/lib.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L1-L13) — crate 定位与两个职责：第 1-2 行给出「所有模型 crate 共同路由经过的共享采样层」这一定位；第 6-11 行描述 `select_batch` 的三路分发（贪心行批式 indexed argmax，其余行批式 FlashInfer temperature/top-k/top-p/min_p，min_p 行在融合快路径外单独分区，带种子行做单行可重放调用），并强调没有逐行逃生舱；第 12-13 行把 `token_logprobs_batch` 与主机参考实现 `token_logprob_from_row` 配成一对。

[src/lib.rs:L15-L19](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L15-L19) — 分层契约：`.cu` 源码 / FFI / 低层批式原语住在 `pegainfer-kernels`（CUDA 构建的所有者）；本 crate 拥有**策略**（贪心/非贪心路由、logprob 数学）和可复用的 scratch 缓冲；`SamplingParams` / `TokenLogprob` 契约类型住在「CUDA 无关的契约 crate」里。（这段里有个命名疑点——文档写的是 `pegainfer-engine`，而 import 实际来自 `pegainfer_frontend`，见 4.3.3 与综合实践。）

[src/lib.rs:L21-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L21-L25) — Kimi-K2 例外：Kimi-K2 保留自己的贪心路径——一个**分片词表**的局部 argmax，其 top-1 logit 再喂给跨 rank 的 DP 归约（#236/#237）；`select_batch` 假设整个词表在一块 arena 上，表达不了这个分片场景。但它的非贪心行仍然走再导出的 `gpu_sample_batch_into`，logprob 仍然走 `token_logprob_from_row`。换句话说：**例外只豁免了贪心路由，不豁免采样语义**。

一个补充事实：模块文档说「every model crate」，从 workspace 的 `Cargo.toml` 依赖关系核实，直接依赖本 crate 的模型 crate 是五个——`pegainfer-qwen3`、`pegainfer-qwen35`、`pegainfer-kimi-k2`、`pegainfer-glm52`、`pegainfer-gemma4`（`deepseek-v2-lite` 与 `k3` 不在列表里）。读文档时用依赖表校对措辞，是个好习惯。

#### 4.1.4 代码实践

**实践目标**：不写代码，靠通读模块文档建立一个可以复述的「 crate 自我介绍」。

**操作步骤**：

1. 打开 `src/lib.rs`，只读 L1-L25（模块文档），读三遍：第一遍抓两个职责的名字，第二遍抓分层里「谁拥有什么」，第三遍抓 Kimi-K2 例外的因果链（为什么自带贪心 → 为什么仍复用另外两条通道）。
2. 合上文件，用自己的话回答三个问题（每题一句话）：
   - 这个 crate 的两个模型无关职责分别叫什么？
   - 「策略」和「原语」的边界在哪一层？
   - Kimi-K2 豁免了什么、没有豁免什么？

**需要观察的现象**：你会发现模块文档没有一处提到具体模型名（除了 Kimi-K2 的例外说明）——这是「模型无关」在文字上的体现。

**预期结果**：三个问题的参考答案——`select_batch` 与 `token_logprobs_batch`；原语（.cu/FFI/批式底层调用）在 `pegainfer-kernels`，策略（路由判定、logprob 数学、scratch 复用）在本 crate；Kimi-K2 只豁免贪心路由（分片词表 + 跨 rank DP 归约），非贪心采样与 logprob 仍走本 crate 入口。

#### 4.1.5 小练习与答案

**练习 1**：模块文档说 `select_batch`「no per-row escape hatch」（没有逐行逃生舱）。从 API 设计角度，这句话防的是什么？

**答案**：防止模型 crate 的作者把批式调用退化成 `for i { sample(row_i) }` 式的逐行循环——那样每行一次核 launch、种子语义也各自为政，批式性能与全引擎统一的采样行为都会失守。批式入口是唯一形状，行为收敛在策略层。

**练习 2**：Kimi-K2 为什么不能直接用 `select_batch` 处理贪心行？

**答案**：它的词表在多卡间分片（EP），贪心需要「各 rank 局部 argmax → top-1 logit 跨 rank DP 归约」两步；而 `select_batch` 假设整词表在同一块 arena 上一次性 argmax，表达不了分片场景。

**练习 3**：`token_logprob_from_row` 和 `token_logprobs_batch` 是什么关系？

**答案**：同一数学的两种实现——前者是主机端单行参考实现（O(V) 一遍，纯 CPU），后者是设备端批式孪生（一次 kernel launch + 一次紧凑回读）。两者可互相验证，是单元三的主角。

### 4.2 Cargo.toml 依赖与分层

#### 4.2.1 概念说明

在 cargo workspace 里，**依赖表就是分层图的直接证据**：`Cargo.toml` 里出现的每个依赖，都回答「这一层站在谁的肩膀上」。`pegainfer-sample` 的依赖表极短——5 个运行时依赖——这本身就是信息：策略层故意保持薄，不直接碰任何模型、不直接写任何 CUDA。

#### 4.2.2 核心流程

把依赖表翻译成一张三层图（本讲最重要的产出，综合实践里你要手画它）：

```text
┌─────────────────────────────────────────────────────────────────┐
│  上层：模型 crate（每个模型一份）                                  │
│  qwen3 · qwen35 · kimi-k2 · glm52 · gemma4                       │
│  拥有：权重加载、prefill/decode 执行、各自调度器                    │
│  消费：select_batch / token_logprobs_batch / SampleScratch /      │
│        再导出的底层原语                                           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 依赖
┌───────────────────────────▼─────────────────────────────────────┐
│  中层：pegainfer-sample（本 crate）                               │
│  拥有：采样策略（贪心/非贪心路由、种子混合、logprob 数学）、         │
│        可复用 scratch（SampleScratch 一次分配、跨步复用）          │
└──────┬──────────────────────────────────────────┬───────────────┘
       │ 依赖                                     │ 依赖
┌──────▼───────────────────────┐   ┌─────────────▼────────────────┐
│ pegainfer-kernels            │   │ pegainfer-frontend（契约 crate）│
│ 拥有：.cu 源码与 nvcc 构建、   │   │ 拥有：SamplingParams、          │
│ FFI、批式原语（argmax、       │   │ TokenLogprob 等 CUDA 无关契约   │
│ gpu_sample_batch_into、       │   │ 类型；本 crate 只从它 import    │
│ logprob_topk_batch）          │   │ 两个类型并再导出一个            │
└──────────────────────────────┘   └──────────────────────────────┘
       ▲ 交叉依赖：kernels 不依赖 sample；frontend 不依赖 kernels
       （旁支）cudarc：CUDA driver 的 Rust 封装，kernels 与本 crate 都用
```

#### 4.2.3 源码精读

[Cargo.toml:L1-L6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L1-L6) — 包声明：crate 名 `pegainfer-sample`，版本 0.1.0，edition 2024，`autobenches = false` 关掉基准自动发现（配合文末手工声明的 bench 目标）。

[Cargo.toml:L8-L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L8-L13) — 五个运行时依赖，各司其职：

| 依赖 | 角色 | 在 lib.rs 里的用点 |
|---|---|---|
| `anyhow` | 错误处理（`Result` / `anyhow!` / `ensure!`） | 每个入口函数的校验与报错 |
| `cudarc` | CUDA driver 的 Rust 封装 | `CudaSlice`（设备缓冲）、`PinnedHostSlice`（固定内存回读槽） |
| `half` | `bf16` 类型 | `top1_values` 缓冲、内置单元测试的输入 |
| `pegainfer-frontend` | CUDA 无关契约 crate | `TokenLogprob`、`SamplingParams`（L32-L33） |
| `pegainfer-kernels` | CUDA 构建所有者与批式原语 | argmax / 采样 / logprob 原语与张量类型（L37-L52） |

[Cargo.toml:L15-L23](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L15-L23) — dev 侧：`criterion` 只进 `[dev-dependencies]`（采样策略的正式产物不背基准框架）；`[lints] workspace = true` 继承全仓库统一的 lint 口径；`[[bench]] name = "select_batch", harness = false` 手工声明一个用 criterion 自带 harness 的微基准。dev 段与运行时段分得干净，是「策略层保持薄」的又一处体现。

逆向看一层：`grep` 整个 workspace 的 `Cargo.toml`，`pegainfer-sample` 的消费者正是 4.1.3 里列出的五个模型 crate——上层图不是推测，是依赖表的事实。

#### 4.2.4 代码实践

**实践目标**：亲手用只读命令验证「哪些模型 crate 站在本 crate 之上」。

**操作步骤**：

1. 在仓库根目录执行（只读，无需 GPU）：
   ```bash
   grep -l "pegainfer-sample" */Cargo.toml
   ```
2. 对比输出与 4.2.2 分层图的上层列表。
3. 再看根 `Cargo.toml` 的 `[workspace.dependencies]` 段里 `pegainfer-sample` 的声明方式（是否 `path` 依赖、是否有 feature），确认它是纯内部 crate。

**需要观察的现象**：命令输出恰好是五个模型 crate 的 `Cargo.toml`（外加 workspace 根清单）；`pegainfer-core`、`pegainfer-kv-cache` 等其他 crate 不出现。

**预期结果**：输出包含 `pegainfer-gemma4/Cargo.toml`、`pegainfer-glm52/Cargo.toml`、`pegainfer-kimi-k2/Cargo.toml`、`pegainfer-qwen3/Cargo.toml`、`pegainfer-qwen35/Cargo.toml`。若你本地的 HEAD 不同，名单可能随提交演进——以命令输出为准。

#### 4.2.5 小练习与答案

**练习 1**：五个运行时依赖里，哪两个是「分层依赖」（决定本 crate 站在谁上面），哪三个是「工具依赖」？

**答案**：分层依赖是 `pegainfer-kernels`（提供 CUDA 原语与构建）和 `pegainfer-frontend`（提供契约类型）；工具依赖是 `anyhow`（错误处理）、`cudarc`（CUDA driver 封装）、`half`（bf16 数值类型）。

**练习 2**：`criterion` 为什么在 `[dev-dependencies]` 而不在 `[dependencies]`？

**答案**：它只在编译测试与基准时需要；放进 dev 段后，正常构建出来的采样库不携带基准框架的任何代码，依赖面更小、编译更快。

**练习 3**：如果让 `pegainfer-kernels` 反过来依赖 `pegainfer-sample`，会发生什么？

**答案**：分层被破坏，形成上下颠倒——策略层与原语层的边界消失，底层构建 crate 不得不感知上层路由策略，任何模型侧的采样语义调整都可能波及 CUDA 构建。依赖单向（kernels → 无、sample → kernels、模型 → sample）是这个架构的硬约束。

### 4.3 公共 API 面与再导出

#### 4.3.1 概念说明

一个 crate 的 `pub` 项清单就是它对外的**全部**承诺——Rust 里没导出的都是私房菜，随时可以改。本 crate 特别的地方是有四条 `pub use` **再导出**：把 `pegainfer-frontend` 与 `pegainfer-kernels` 的项用本 crate 的名字再暴露一次。意图写在注释里：让「必须自己驱动贪心路径的模型」（点名 Kimi-K2）仍然够得着**唯一的采样入口**，而不必直接下沉到 `pegainfer-kernels` 去拿 `gpu_sample_batch_into`——否则它绕过策略层，seeded 行的语义就可能和 `select_batch` 分叉。

#### 4.3.2 核心流程

调用方向一图看清（实线 = 本 crate 自己的 API，虚线 = 再导出的别人家的 API）：

```text
模型 crate
   │
   ├── select_batch ────────────► SampleScratch（自建，跨步复用）
   │        └─ 内部调用 argmax_batch_bf16_split_indexed_into（私有 use）
   ├── token_logprobs_batch ────► LogprobRequest（自建描述类型）
   │        └─ 内部调用 logprob_topk_batch_bf16_into（私有 use）
   ├── token_logprob_from_row ──► 纯 CPU，无设备依赖
   ├── effectively_greedy / mix_seed ──► 策略谓词与种子混合（公开给自带
   │        贪心路径的模型，用来对齐 select_batch 的语义）
   ├── greedy_argmax_ids / greedy_stage_readback /
   │   greedy_stage_resident / greedy_collect_resident
   │        └─ CUDA 图友好的分阶段贪心四件套（单元四主角）
   ├──╌ SamplingParams ╌╌╌╌╌╌╌╌► 再导出自 pegainfer_frontend::sampler
   ├──╌ BatchSamplingRow ╌╌╌╌╌╌► 再导出自 pegainfer_kernels::ops
   ├──╌ BatchSamplingScratch ╌╌► 再导出自 pegainfer_kernels::ops
   └──╌ gpu_sample_batch_into ╌► 再导出自 pegainfer_kernels::ops
```

注意分寸：**三个再导出的 kernels 原语是「给例外的模型用的后门」，但后门开在策略层门口**——它们与 `mix_seed`、`effectively_greedy` 同层可见，文档互相引用，语义不会漂。

#### 4.3.3 源码精读

先看 import 块的骨架，注意 `use`（私有）与 `pub use`（再导出）的分界：

[src/lib.rs:L27-L33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L27-L33) — 工具类 import（`anyhow`、`cudarc`）之后，第 32-33 行从 `pegainfer_frontend` 拿契约类型：`TokenLogprob` 是私有 `use`（只在内部组装返回值），`SamplingParams` 是 `pub use`（模型 crate 从这里拿参数类型）。注意：模块文档 L18 说契约类型住在 `pegainfer-engine`，而 import 实际路径是 `pegainfer_frontend`——这个命名差异见下方「旁注」。

[src/lib.rs:L34-L47](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L34-L47) — 三条 kernels 再导出，每条都带同一段注释：「再导出底层的批式采样，让必须自驱贪心路径的模型仍够得着唯一采样入口，而不是直接扎进 `pegainfer-kernels`——例如 Kimi-K2（见模块文档）」。对照 kernels 侧的定义可以看见被导出的到底是什么：[pegainfer-kernels/src/ops/sampling.rs:L14-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L14-L29) 是 `BatchSamplingRow`——「一个非贪心行的全部采样参数」（行号、temperature、top_k、top_p、min_p），文档注明贪心行属于 argmax 路径、min_p 行可以随意混批。

[src/lib.rs:L42-L52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L42-L52) — 私有 `use` 的四个 kernels 原语（indexed argmax、partials 长度公式、批式 logprob）与四个张量设施（`DeviceContext`、`HiddenStates`、`has_stream_override`、`stream_spin_wait`）。它们没有再导出：模型 crate 不该直接碰这些，它们是本 crate 实现策略的原料。

**完整 `pub` 清单**（行号以当前 HEAD 为准，函数体的细节留给后续各讲）：

| 类别 | 项 | 位置 | 一句话说明 |
|---|---|---|---|
| 自有·类型 | `SampleScratch` | [L61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L61) | 一次分配的解码采样缓冲（指针稳定，跨步复用） |
| 自有·方法 | `SampleScratch::new` / `with_selection_width` | [L93](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L93) / [L102](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L102) | 按 `(vocab, max_rows)` 建缓冲；后者把「可发射宽度」与 arena 宽度分开 |
| 自有·方法 | `max_rows` / `vocab` | [L153-L159](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L153-L159) | 容量只读访问器 |
| 自有·函数 | `select_batch` | [L189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L189) | 职责一：logits arena → 每行一个 token id |
| 自有·函数 | `greedy_argmax_ids` / `greedy_stage_readback` / `greedy_stage_resident` / `greedy_collect_resident` | [L345](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L345) / [L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L371) / [L396](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L396) / [L413](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L413) | CUDA 图友好的分阶段贪心四件套（单元四） |
| 自有·函数 | `mix_seed` | [L441](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L441) | SplitMix64：把请求种子与请求内步数混成 philox 种子 |
| 自有·函数 | `effectively_greedy` | [L455](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L455) | 路由谓词：这行能否走 argmax 而不改变语义 |
| 自有·函数 | `token_logprob_from_row<T>` | [L473](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L473) | 职责二的主机参考实现（泛型 f32/bf16） |
| 自有·类型 | `LogprobRequest` | [L525](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L525) | 一行 logprob 请求：行号、选中 token、top_k |
| 自有·函数 | `token_logprobs_batch` | [L533](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L533) | 职责二的设备端批式孪生 |
| 再导出 | `SamplingParams` | [L33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L33) | ← `pegainfer_frontend::sampler` |
| 再导出 | `BatchSamplingRow` / `BatchSamplingScratch` / `gpu_sample_batch_into` | [L37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L37) / [L41](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L41) / [L47](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L47) | ← `pegainfer_kernels::ops`（给自驱贪心路径的模型用） |

> **旁注：`pegainfer-engine` 命名差异**。模块文档 L18 写「`SamplingParams`/`TokenLogprob` stay in `pegainfer-engine`」，但 L32-L33 的实际路径是 `pegainfer_frontend::engine` / `pegainfer_frontend::sampler`。核对仓库文档可以解释：`docs/playbooks/hawk-visibility-audit.md` L43 记录了「旧的 `pegainfer_engine` 排除项已随该 crate 消失——引擎契约现在住在 `pegainfer-frontend`」，`docs/subsystems/frontend/frontend-architecture.md` L106 也把 `pegainfer-engine` 归入「2026-08 整合前的旧表」。结论：`pegainfer-engine` 是**整合前的旧 crate 名**，lib.rs 的模块文档是一处未随整合更新的措辞残留；实际契约在 `pegainfer-frontend` 的 `engine` / `sampler` 模块里（见 [pegainfer-frontend/src/lib.rs:L1-L16](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/lib.rs#L1-L16)，其中说明契约半边「不含任何 CUDA 类型」）。这不妨碍理解——按「契约 = frontend crate 的 engine/sampler 模块」读即可。

#### 4.3.4 代码实践

**实践目标**：不看本讲义，用一条命令独立枚举出 crate 的全部对外承诺。

**操作步骤**：

1. 在 `pegainfer-sample` 目录执行（只读）：
   ```bash
   grep -n "pub use\|pub fn\|pub struct" src/lib.rs
   ```
2. 把输出整理成两栏清单：左栏「本 crate 定义」，右栏「再导出（含来源）」。
3. 与 4.3.3 的表格逐行核对；多出来的项（如 `pub fn max_rows` 这类方法不会被上面的模式匹配到）补上——可以再用 `grep -n "    pub fn" src/lib.rs` 抓方法。

**需要观察的现象**：`pub use` 恰好四条；私有 `use pegainfer_kernels::ops::...`（argmax、logprob 原语）不在 `pub` 输出里——它们刻意没有暴露。

**预期结果**：与 4.3.3 表格一致（11 个自有 pub 项 + 4 条再导出；方法 `new` / `with_selection_width` / `max_rows` / `vocab` 需要第二条 grep 补全）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `argmax_batch_bf16_split_indexed_into`（L42）用私有 `use`，而 `gpu_sample_batch_into`（L47）用 `pub use`？

**答案**：`select_batch` 已经把贪心行封装成批式 argmax 路径，模型 crate 没有理由直接碰底层 argmax 核；而 `gpu_sample_batch_into` 是 Kimi-K2 这类自驱贪心路径的模型在处理**非贪心行**时必须够得着的入口，公开再导出让它不必直接依赖 `pegainfer-kernels`，语义仍然过策略层这道门。

**练习 2**：`SamplingParams` 从这里再导出，对模型 crate 意味着什么？

**答案**：模型 crate 只需依赖 `pegainfer-sample` 就能同时拿到参数类型与采样入口，依赖图更扁；更重要的是参数语义（哪些字段算「显式贪心」）由策略层统一解释，避免各模型对同一契约类型各念各的经。

**练习 3**：不看表，说出这个 crate 有几个「职责一」入口、几个「职责二」入口。

**答案**：职责一（选 token）一个批式入口 `select_batch`，外加为 CUDA 图解码准备的分阶段贪心四件套（`greedy_argmax_ids` / `greedy_stage_readback` / `greedy_stage_resident` / `greedy_collect_resident`，是同一职责的图友好变体）；职责二（logprob）两个入口——设备端批式 `token_logprobs_batch` 与主机单行参考 `token_logprob_from_row`。

## 5. 综合实践

**任务**：产出「一张图 + 一张清单 + 一段解释」，把本讲三个模块串起来。全程无需 GPU。

1. **手画分层图**。先读 [src/lib.rs:L1-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L1-L25) 的模块文档与 [Cargo.toml:L8-L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L8-L13) 的依赖表，然后凭理解画出三层图（kernels 提供 / sample 拥有 / 模型 crate 消费），画完再对照 4.2.2 检查。要求每层至少写两行注记：一行「拥有什么」，一行「不拥有什么」（例如 kernels 不拥有路由策略；sample 不拥有任何 `.cu`；模型 crate 不拥有采样行为）。
2. **列 pub 清单**。用 4.3.4 的两条 grep 命令独立枚举，整理成「本 crate 定义 / 再导出」两栏表，并在每个再导出旁注一句「它是为谁开的门」。
3. **核对命名差异（待确认点的闭环）**。模块文档 L18 说契约类型在 `pegainfer-engine`，import 却来自 `pegainfer_frontend`。请打开 `docs/playbooks/hawk-visibility-audit.md`（L43 附近）与 `docs/subsystems/frontend/frontend-architecture.md`（L106 附近），找出 `pegainfer-engine` 这个名字的历史下落，然后写 50 字以内的结论。参考结论：`pegainfer-engine` 是整合前的旧 crate 名，2026-08 前后其内容并入 `pegainfer-frontend`，lib.rs 模块文档是一处未更新的旧称；实际契约在 `pegainfer_frontend::engine` 与 `::sampler`。
4. **（可选，需能构建 kernels 的环境）** 运行 `cargo doc --no-deps -p pegainfer-sample` 生成文档，对照你手写的清单检查渲染出的 API 面。若当前机器没有 CUDA 工具链，这一步会因依赖构建失败而跑不通——跳过即可，纯源码阅读已覆盖全部信息（此步**待本地验证**）。

**完成标志**：合上所有文件，你能在白纸上复现分层图、说出两个模型无关职责的名字，并解释「再导出是给谁开的门」。

## 6. 本讲小结

- `pegainfer-sample` 是所有模型 crate 共享的采样层，两个模型无关职责：`select_batch`（logits arena → 每行一个 token id）与 `token_logprobs_batch`（批式 logprob 提取，配主机参考实现 `token_logprob_from_row`）。
- 分层单向：`pegainfer-kernels` 拥有 `.cu`/FFI 与低层批式原语，本 crate 拥有策略（贪心/非贪心路由、种子混合、logprob 数学）与可复用 scratch，五个模型 crate（qwen3 / qwen35 / kimi-k2 / glm52 / gemma4）在上层消费。
- 对外承诺 = 11 个自有 `pub` 项 + 4 条再导出（`SamplingParams`、`BatchSamplingRow`、`BatchSamplingScratch`、`gpu_sample_batch_into`）；再导出是给「必须自驱贪心路径的模型」（Kimi-K2）开的门，门开在策略层门口，语义不漂。
- Kimi-K2 例外只豁免贪心路由（分片词表局部 argmax + 跨 rank DP 归约），非贪心行与 logprob 仍走本 crate。
- 模块文档里的 `pegainfer-engine` 是整合前的旧 crate 名，实际契约类型在 `pegainfer-frontend` 的 `engine` / `sampler` 模块——读文档时用 import 路径和仓库存档文档校对措辞。

## 7. 下一步学习建议

- 下一篇（u1-l2）讲这个 crate 的**验证面**：三个不需要 GPU 的 CPU 单元测试（lib.rs L651-L704 内嵌）、两个需要 GPU 的集成测试文件（`tests/select_batch.rs`、`tests/gpu_logprobs.rs`）、criterion 微基准与延迟探针。建议先自己翻一遍 `tests/` 目录再读那一讲。
- 如果你想先睹为快主链路：直接读 [src/lib.rs:L189-L321](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L189-L321) 的 `select_batch` 函数体，只看「它把行分成了哪三堆」，细节等单元二。
- 交叉阅读（仓库自带文档）：`docs/subsystems/sampling/pegainfer-sample.md` 是团队维护的采样子系统文档，与本讲互为补充；读时留意它同样沿用了 `pegainfer-engine` 旧称。
