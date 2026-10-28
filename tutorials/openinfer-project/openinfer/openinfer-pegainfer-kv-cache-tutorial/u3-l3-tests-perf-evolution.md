# 测试分层、性能回归与机制演进史

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出本 crate 的三层测试体系各自的位置、运行条件与守护对象：`src/pool.rs` 内联 `#[cfg(test)]` 单测、`tests/restore_commit.rs`（纯逻辑、无 GPU）、`tests/lifecycle.rs`（需真实 CUDA 设备）。
2. 为一个新性质选择正确的测试层级——判断依据是「这条性质是否依赖真实显存与 CUDA 上下文」。
3. 理解「测试文件注释即性能回归锚」这一工程实践：#704 的 36ms → 1.6ms 数字为什么写在 `restore_commit.rs` 的文件头注释里，而不是写成一个计时断言。
4. 用 `git log` / `git show` 从提交史还原每个 KV 机制解决的真实问题，产出一张「机制-时间线」表，并能解释 crate 内每个文件是哪次提交、为了哪条模型线的需求而诞生或重构的。

本讲是专家层的收尾：前两讲（u3-l1 恢复-提交、u3-l2 事件流）已经从机制视角读完了 `pool.rs` 的主体，本讲换一个工程视角——**测试怎么组织、性能事实怎么留档、机制怎么随提交演进**。

## 2. 前置知识

- **`#[cfg(test)]` 内联单测**：Rust 允许把测试模块直接写在源码文件末尾，编译测试目标时才编译它。它离被测代码最近，适合守护「实现细节级别」的不变式——这些不变式往往不是公共 API，甚至需要专门的 `#[cfg(test)]` 辅助访问器才能触达。
- **集成测试（`tests/` 目录）**：Cargo 会把 `tests/` 下每个 `.rs` 编译成独立二进制，只能使用 crate 的公共 API。本 crate 有两个：`restore_commit.rs` 与 `lifecycle.rs`。
- **纯逻辑层 vs 物理层**：本手册反复强调的架构事实——`BlockPool` 是纯逻辑块池，不持有显存，构造它只需要两个整数（`block_size`、块数）；`KvCacheManager` 则要配对一个真实的 `KvBuffer`，构造时要创建 CUDA 上下文、分配 bf16 显存。这个分层直接决定了测试需不需要 GPU。
- **性能回归锚（regression anchor）**：有些性能事实（「1024 块提交曾经要 36ms」）不适合写成 CI 计时断言——计时断言在共享runner 上抖动太大。工程折衷是：把数字写进测试文件的文档注释，让任何改动这条路径的人在打开文件的第一秒就看到基准；测试本身守护的是这条路径的**正确性形状**。
- **`git log -- <路径>` 与 `--follow`**：按路径过滤提交史时，Git 默认不追踪重命名。本 crate 的目录名经历了 `pegainfer-kv-cache` → `openinfer-kv-cache`（#318）→ `pegainfer-kv-cache`（#841）两次翻转，所以裸 `git log -- .` 会漏掉一部分只存在于旧路径下的提交，需要 `--follow`（单文件有效）补齐。
- 阅读本讲前，你应已完成 u2-l2（请求生命周期）、u2-l5（前缀缓存与 salt）与 u3-l1（恢复-提交链路）；本讲会直接引用那些机制名，不再重新推导。

## 3. 本讲源码地图

| 文件 | 行数 | 本讲视角下的作用 |
|------|------|------------------|
| [src/pool.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs) | 1009 | 核心源码；其末尾 `mod tests`（L741–L1009，约占全文件 27%）是第一层测试的所在，含 7 个内联测试与一个 `#[cfg(test)]` 辅助访问器 |
| [tests/restore_commit.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs) | 56 | 第二层：纯逻辑集成测试，无 GPU；文件头注释是 #704 性能回归锚；含 `commit_absorbs_full_prefix` 与 `commit_extends_partial_gpu_hit` 两个测试 |
| [tests/lifecycle.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs) | 270 | 第三层：需 CUDA 的端到端生命周期测试，经 `KvCacheManager` 走完整的池+缓冲配对路径；10 个测试 |
| [../CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L61-L79) | — | 仓库级测试约定：`--release`、按 crate 限定作用域、单测过滤器用法 |

辅助工具只有 `git log` / `git show` / `wc -l`，全部只读。

## 4. 核心概念与源码讲解

### 4.1 第一层：`src/pool.rs` 内联 `#[cfg(test)]` 单测——守护协议前提

#### 4.1.1 概念说明

内联单测与被测代码同文件，它的独特价值是能触达**非公共表面**。本 crate 里最有代表性的例子是 `prompt_block_hashes_stable_across_shared_prefix`：它守护的性质是「共享同一前缀的两个请求，其前缀块哈希必须逐位相同」——这是整个 KV offload 查询链的前提假设（u3-l1 讲过，CPU 层用 16 字节内容键寻址，键不一致 = 永远查不中），但它不是公共 API 语义，而是一个跨层协议的前提。为了测试它，源码专门提供了一个只在测试编译时存在的访问器。

这一层的测试对象可以概括为「**哈希稳定性与状态机口径**」：

| 内联测试 | 守护的前提 |
|----------|-----------|
| `prompt_block_hashes_stable_across_shared_prefix` | offload 查询键：共享前缀 → 哈希逐位相同；分叉块 → 哈希必变；salt → 隔离 |
| `active_prefix_hashes_exclude_reclaimable_entries` | `active_prefix_hashes` 只报活跃请求的块，释放后即消失 |
| `cache_salt_isolates_request_and_probe_prefixes` | cache-salt（qwen35 场景）对请求与探测两条入口同样隔离 |
| `probe_and_attach_can_select_a_shorter_exact_boundary` | 可以在精确边界上挂载比命中更短的前缀 |
| `probe_boundary_hash_obeys_reusable_cap_and_final_token_rule` | 边界哈希遵守 \( \lfloor (n-1)/b \rfloor \) 封顶与「最后一个 token 不缓存」规则 |
| `issue_681_profiled_capacity_reuses_blocks_across_requests` | 用 CPU-only 形状复现线上 issue #681：投机请求完成后必须还清全部页 |
| `step_page_indices_exact_at_block_boundaries` | 页表行必须精确覆盖序列（#291 教训）；末尾还有一个「自我退役」断言 |

#### 4.1.2 核心流程

以第一个测试为例，其逻辑流程是：

```text
BlockPool::new(16, 256)               # 纯逻辑池，无 CUDA
构造 prompt A = 3 个共享块 + 1 个分叉块
构造 prompt B = 3 个共享块 + 1 个不同分叉块
分别取 prompt_block_hashes()
断言：
  1. 两个哈希列表的前 3 项逐位相等   ← offload 查询键前提
  2. 第 4 项（分叉块）必不相等
  3. 所有哈希非全零（非平凡）
再用不同 LoRA salt 造请求 C
断言 C 的首块哈希 ≠ A 的首块哈希      ← salt 隔离
```

注意这类测试**从头到尾没有触碰 CUDA**：`BlockPool::new` 只是在内存里建一个 kvbm-logical 块管理器。这就是它们能放在第一层、随 `--lib` 快速运行的原因。

另一个值得单独说的是 `step_page_indices_exact_at_block_boundaries` 的收尾断言——它断言的不是一个期望行为，而是**上游依赖的现状**：kvbm 目前会在块边界多分配一个生成块，如果哪天上游不再过度分配，这个断言会失败，提醒维护者把 `step_page_indices` 和这个测试一起退役。测试在守护契约的同时声明了自己的失效条件。

#### 4.1.3 源码精读

**测试专用的 `#[cfg(test)]` 访问器**——[src/pool.rs:642-651](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L642-L651)：`prompt_block_hashes` 只在测试编译期存在，返回每个完整 prompt 块的 16 字节内容哈希。它的文档注释直接说明了为什么这个函数值得测：这些键「必须对共享前缀的任意两个请求完全一致，否则一个请求暖好的块永远匹配不到下一个请求」。

**测试模块入口**——[src/pool.rs:741-743](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L741-L743)：标准的 `#[cfg(test)] mod tests`，从 L741 延伸到文件末尾 L1009。

**核心测试本体**——[src/pool.rs:745-776](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L745-L776)：`prompt_block_hashes_stable_across_shared_prefix`。L752–L756 构造共享前缀与两个分叉延续；L765–L766 是三条关键断言（共享段相等、分叉段不等）；L770–L775 验证 salt 毒化匹配。每条断言都带一句说明其守护对象的消息字符串。

**issue 复现型测试**——[src/pool.rs:930-950](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L930-L950)：`issue_681_profiled_capacity_reuses_blocks_across_requests` 的文档注释写明它是线上 issue #681 的「CPU-only 形状」——160 个请求页、1643 token 分两段预填、512 输出、17 长度投机验证跨度，跑两轮，每轮结束断言可用块数回到基线。这是「把生产事故固化成最小复现」的典型做法。

**带自我退役条款的测试**——[src/pool.rs:952-1008](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L952-L1008)：`step_page_indices_exact_at_block_boundaries` 的文档注释记录了它的出身（Kimi DP8 死锁、qwen3 FlashInfer 多读一页垃圾，#291），L1003–L1007 的断言 `raw_overshoots > 0` 会在上游停止过度分配时主动失败，失败消息明说「step_page_indices 和本测试可以一起退役了」。

#### 4.1.4 代码实践

**实践：跑通第一层，并读懂一条「自我退役」断言**

1. **实践目标**：确认内联单测无需 GPU 即可运行；体验「测试守护上游依赖现状」的写法。
2. **操作步骤**：
   - 在仓库根目录运行：`cargo test --release -p pegainfer-kv-cache --lib`
   - 想只跑本讲聚焦的一个测试：`cargo test --release -p pegainfer-kv-cache --lib prompt_block_hashes -- --nocapture`
   - 打开 [src/pool.rs:1003-1007](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L1003-L1007)，阅读断言消息原文。
3. **需要观察的现象**：测试输出里出现 `pool::tests` 模块名与各测试名；全部通过。
4. **预期结果**：`pool::tests` 下 7 个测试通过（`prompt_block_hashes_stable_across_shared_prefix`、`active_prefix_hashes_exclude_reclaimable_entries`、`cache_salt_isolates_request_and_probe_prefixes`、`probe_and_attach_can_select_a_shorter_exact_boundary`、`probe_boundary_hash_obeys_reusable_cap_and_final_token_rule`、`issue_681_profiled_capacity_reuses_blocks_across_requests`、`step_page_indices_exact_at_block_boundaries`）。精确输出待本地验证。
5. 思考题（不需要运行）：如果把 L1003 的 `assert!(raw_overshoots > 0)` 删掉，这个测试还能守护什么？会失去什么？

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prompt_block_hashes` 要标 `#[cfg(test)]`，而 `assigned_block_hashes` 是普通 `pub fn`？

**参考答案****：`assigned_block_hashes` 驱动真实的 offload 保存（块封板后产出 `(page_id, 内容哈希)` 交给保存引擎），是生产路径的一部分；`prompt_block_hashes` 只为验证「哈希对共享前缀稳定」这一查询键前提而存在，生产代码没有任何调用点，标 `#[cfg(test)]` 让它不进入发布产物的编译面（也呼应 #858 的公共面收缩原则）。

**练习 2**：`issue_681_...` 测试为什么坚持用「160 页 / 1643 token / 512 输出 / 17 跨度」这种看似奇怪的数字组合？

**参考答案**：这些数字是线上 issue #681 的真实形状（profiled capacity）。用它做参数不是为了覆盖一般性，而是保证「引发事故的精确资源轨迹」被永久钉在 CI 里——如果有人改动了投机解码的块回收逻辑，最先红的就是这个形状。

**练习 3**：`step_page_indices_exact_at_block_boundaries` 遍历了 10 种 prompt 长度 × 23 个 decode 步，为什么还要在最后断言 `raw_overshoots > 0`（即「上游确实过度分配过至少一次」）？

**参考答案**：这个测试的合法性依赖一个外部事实——kvbm 会过度分配生成块，所以裁剪是必要的。断言「过度分配确实发生过」等于给测试装了一个烟雾报警器：一旦上游行为改变（不再过度分配），测试立刻失败并提示维护者连测试带 `step_page_indices` 一起退役，避免测试悄悄变成「永远通过但守护的威胁已不存在」的僵尸。

---

### 4.2 第二、三层集成测试：`restore_commit`（无 GPU）与 `lifecycle`（需 CUDA）

#### 4.2.1 概念说明

`tests/` 下两个文件按「是否需要真实 CUDA 设备」分层，这个划分不是随意的——它精确对应 crate 的逻辑/物理两层架构：

- **`tests/restore_commit.rs`** 测的全是 `BlockPool` 的逻辑性质：探测、预留、提交、再匹配。`BlockPool` 不持有显存，所以整个文件不需要 GPU。它的第二个身份是 **#704 性能回归锚**：文件头注释记录「1024 块提交在注册表 PRT 修复后约 1.6ms，修复前 36ms」。
- **`tests/lifecycle.rs`** 的第一个函数就是 `CudaContext::new(0)`——它经 `KvCacheManager` 走完整路径：真实显存分配、`KvViewDesc` 携带真实设备指针、页表与缓冲配对。10 个测试覆盖 u2-l2 / u2-l3 / u2-l5 讲过的全部生命周期语义。

分层带来的实际收益：**CI 上没有 GPU 也能守住 80% 的行为**（哈希、计数、状态机、恢复-提交形状），只有必须看真实显存的性质（视图布局、padding 稳定性、容量耗尽、drop 归还）才留给有卡的机器。

#### 4.2.2 核心流程

**「测试文件注释即性能回归锚」的工作方式**：

```text
#704 事故：批量恢复 1024 块时调度线程停顿 36ms
  ↓ 修复：kvbm-logical 注册表换前缀树（PRT）实现
  ↓ 同一提交里新增 tests/restore_commit.rs：
      - 文件头注释钉住数字：36ms → ~1.6ms
      - 两个功能测试守护恢复-提交的正确性形状
  ↓ 此后任何人改 commit_loaded_blocks / 注册路径：
      打开文件第一行就看到基准，改动者自觉对照复测
```

注意：这两个测试**没有计时断言**。锚的意义是「把性能事实写在离代码最近的地方让人看见」，而不是在 CI 里跑一个抖动的计时器。功能断言守护的是修复所依赖的形状——批量提交后前缀必须能作为**一个连续整体**被再次匹配。

**两个测试各自覆盖的恢复形状**：

| 测试 | GPU 命中 | CPU 恢复 | 验证点 |
|------|----------|----------|--------|
| `commit_absorbs_full_prefix` | 0 | 33 | 全量恢复后 `held_blocks == 33`，同 prompt 新请求命中 33×16 token |
| `commit_extends_partial_gpu_hit` | 3 | 5 | 部分命中后恢复的 5 块必须挂在**延续哈希**（continuation）之下，拼成 8 块连续前缀 |

第二个形状正是 u3-l1 讲过的关键约束：恢复块的注册哈希必须从 `gpu_hit` 处接续 lineage 链条，偏移错一个块，前缀树就断。

#### 4.2.3 源码精读

**性能锚注释**——[tests/restore_commit.rs:1-5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L1-L5)：文件头自述三件事——本文件测「预留块按探测器延续哈希注册后必须可作为一个连续前缀再匹配」；纯逻辑池测试、无需 GPU；兼作 #704 注册成本回归锚（1024 块提交修复后 ~1.6ms、修复前 36ms）。

**全量恢复形状**——[tests/restore_commit.rs:11-29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L11-L29)：`commit_absorbs_full_prefix`。33 个整块 + 1 个多余 token 使 cacheable 恰为 33（呼应 \( \lfloor (n-1)/b \rfloor \) 规则），探测 → 预留 33 → 提交 → 断言 `held_blocks() == 33` → 新请求 `match_and_add_prefix` 命中 528 个 token。

**部分命中 + 延续注册形状**——[tests/restore_commit.rs:31-56](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L31-L56)：`commit_extends_partial_gpu_hit`。L37–L41 先用一个**真实请求**在 GPU 池种下前 3 块（不是手工注入，保证与生产路径同构）；L45–L51 探测看到 3 块命中、CPU 查询窗口 5 块，恢复后 `held_blocks() == 8`；L53–L55 同 prompt 新请求命中 8×16=128 token——证明恢复块与 GPU 命中块拼成了一条不间断的 lineage 链。

**需要 GPU 的一层的入口**——[tests/lifecycle.rs:3-8](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L3-L8)：`make_manager` 第一行 `CudaContext::new(0)` 就宣告了本文件的运行条件；几何取最小值（1 层、1 KV 头、head_dim=1、block_size=16），把测试成本压到一块极小的显存。

**第三层代表性测试**——[tests/lifecycle.rs:173-192](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L173-L192)：`schedule_without_apply_returns_blocks_on_drop` 守护 u2-l2 讲过的失败恢复语义——只调度不应用（模拟 forward 失败）后 drop 请求，预约块必须如数归还。这条性质涉及真实的块发放与守卫 Drop，只能在配对的池+缓冲上验证。

**跨层联动的 salt 测试**——[tests/lifecycle.rs:237-270](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L237-L270)：`lora_salt_isolates_prefix_cache` 在端到端路径上复验了 4.1 节内联测试的哈希前提：适配器 `a` 暖过的块，base 模型与适配器 `b` 都命中 0，适配器 `a` 复来命中 32（48 token 被 \( \lfloor (48-1)/16 \rfloor = 2 \) 块的封顶截住）。同一条不变式在内联层验「哈希位」，在集成层验「行为结果」。

#### 4.2.4 代码实践

**实践：亲手验证两个文件的运行条件差异**

1. **实践目标**：确认 `restore_commit` 无 GPU 可跑、`lifecycle` 在无 GPU 环境下的失败点即是分层证据。
2. **操作步骤**：
   - 运行：`cargo test --release -p pegainfer-kv-cache --test restore_commit`
   - 再运行：`cargo test --release -p pegainfer-kv-cache --test lifecycle`
   - 若本机无 NVIDIA GPU，观察第二个命令的失败位置。
3. **需要观察的现象**：第一个命令 2 个测试通过；第二个命令在 `make_manager` 的 `CudaContext::new(0)` 处报错（找不到设备 / 驱动），而非任何逻辑断言失败。
4. **预期结果**：`commit_absorbs_full_prefix` 与 `commit_extends_partial_gpu_hit` 通过；`lifecycle` 在无卡机器上于 CUDA 上下文创建处 panic。两项均待本地验证（本讲义撰写环境未执行）。
5. **延伸**：对照 [../CLAUDE.md:61-79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L61-L79) 的测试约定，注意仓库统一的 `--release` 与 `-p` 限定作用域写法，以及 L79 的单测过滤器示例（`cargo test --release --workspace --lib prefix_cache -- --nocapture`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 #704 的性能数字写成一个 `#[ignore]` 的计时测试，而是写进文件头注释？

**参考答案**：共享 CI 机器上的计时抖动极大，36ms/1.6ms 量级的断言要么阈值放宽到失去意义、要么频繁误报；而这条路径的正确性形状（批量提交 → 连续再匹配）是确定性的、值得常跑。把数字钉在注释里，成本为零，却保证改动者打开文件第一眼就看到基准；真要复测，用 bench 手动跑一次即可。这是「性能事实留档 + 正确性形状入 CI」的分工。

**练习 2**：`commit_extends_partial_gpu_hit` 为什么要用真实请求（schedule → apply → release）去种 GPU 命中，而不是直接构造注册块？

**参考答案**：真实请求走的封块、注册、进非活跃池的路径与生产完全同构，隐式验证了「探测能看见真实请求注册的块」；手工注入则会绕开 lineage 链的真实构造，测试就退化成「用测试自己定义的前提验证测试自己」。种子请求只取 prompt 的前 3 块，同时精确控制了 `gpu_hit_blocks() == 3` 这个起点。

**练习 3**：如果你要新增一条「`KvViewDesc` 的设备指针在两次请求之间保持稳定」的测试，应该放进哪一层？为什么？

**参考答案**：放 `tests/lifecycle.rs`（第三层）。指针稳定性是 `KvBuffer` 一次性分配、永不移动的物理性质（u2-l4），必须在真实显存上取证；纯逻辑层的 `BlockPool` 根本没有指针可验。

---

### 4.3 用 git 提交史还原机制演进

#### 4.3.1 概念说明

本 crate 只有 9 个提交（含 2 个纯改名 chore），却是「每条模型线把需求沉淀成共享原语」的完整标本。读懂这张时间线，你就掌握了预判「下一个模型线会往本 crate 加什么」的能力。更重要的是理解主线：**crate 的每次扩张都由一条具体模型线的生产需求驱动，而不是预先设计**。

#### 4.3.2 核心流程

在 crate 目录运行 `git log --oneline -- .` 得到 9 个提交；其中与机制相关的 7 个里程碑如下（日期与改动面均已逐条核实）：

| 提交 | 日期 | PR | 机制 / 解决的问题 | 本 crate 落点 |
|------|------|-----|-------------------|---------------|
| `de26afa4` | 2026-05-28 | #187 | kvbm-logical 集成：qwen3 的 KV 缓存需要块级管理与共享 | **crate 诞生**：5 个模块 + `tests/lifecycle.rs`，共 702 行新增 |
| `267be7c1` | 2026-06-04 | #216 | qwen3 全块匹配前缀缓存：跨请求复用相同 prompt 前缀 | `manager.rs` +32、`lifecycle.rs` 扩展 |
| `d0a02764` | 2026-06-07 | #292 | kimi-k2 分页池：MLA 模型绕过门面直接消费 `BlockPool` | **`pool.rs` 诞生**（+276 行，自 `manager.rs` 抽出 −221 行） |
| `cb49983a` | 2026-06-09 | #316 | kv-offload pegaflow 桥：GPU 逐出后从 CPU 层恢复 | `pool.rs` +235（offload bridge 段）、`buffer.rs` +14 |
| `a157baf6` | 2026-07-19 | #705 | #704 修复：批量恢复注册成本 36ms → ~1.6ms（PRT 落在 kvbm 注册表） | **`tests/restore_commit.rs` 诞生**（+56 行，回归锚） |
| `8f99ea2d` | 2026-08-10 | #858 | 公共面收缩：删除未使用的公共 API | `pool.rs` −55/+2 |
| `bc923e08` | 2026-09-03 | #958 | DSpark 投机验证：hedge 需要事务层不可见的 scratch 页 | `buffer.rs` 新增 `copy_page`、`manager.rs` 新增 `new_with_scratch_pages` |
| `69692ba3` | 2026-09-18 | #836 | qwen35 联合前缀缓存：TP 容量协商 + 精确边界挂载 | `pool.rs` +153、`manager.rs` +15（`from_buffer`、`boundary_hash`、`match_and_add_prefix_up_to` 等） |

叙事主线可以概括为四幕：

1. **诞生与抽取**（#187 → #292）：先是 qwen3 专用的整体几何（五个模块全在一个门面里），kimi-k2 需要 MLA 直连，迫使「请求生命周期」从 `manager.rs` 里抽成独立的 `pool.rs`——这次抽取让 crate 从「qwen3 的缓存」升级为「多条模型线的共享原语」。
2. **跨层扩张**（#316 → #705）：offload 桥把池的触角伸到 CPU/SSD 层；#705 的教训是恢复路径的成本模型——修复落在上游 kvbm 注册表，但回归锚钉在本 crate。
3. **收缩**（#858）：扩张之后主动做减法，`pool.rs` 净删 53 行公共面。
4. **再扩张**（#958 → #836）：投机解码与 TP 各带来新的原语（scratch 页、最小公共容量），且都以「对现有语义零侵入」的方式落地。

另一个细节：`a157baf6` 在裸 `git log -- .` 里**看不见**——它发生时 crate 目录还叫 `openinfer-kv-cache/`（#318 改名 pegainfer→openinfer，#841 又改回），路径过滤不追踪重命名。要找到它需要 `git log --follow -- tests/restore_commit.rs`。

#### 4.3.3 源码精读

本节的「源码」是提交本身。三个建议精读的 diff：

- **[d0a02764](https://github.com/openinfer-project/openinfer/commit/d0a02764)（pool.rs 抽取）**：`git show d0a02764 -- .` 显示 `pool.rs` +276 / `manager.rs` −221——大量代码是从门面**搬运**而非新写，搬运的边界正好就是 u1-l1 讲过的「MLA 模型绕过门面」的接缝。
- **[bc923e08](https://github.com/openinfer-project/openinfer/commit/bc923e08)（scratch 页）**：diff 只在 `buffer.rs` 加 `copy_page`、`manager.rs` 加 `new_with_scratch_pages`——hedge 验证这个复杂的投机解码机制，落到本 crate 只有约 57 行，因为页 ID 从 `num_blocks` 起编就天然对池事务层不可见（u2-l4）。
- **[a157baf6](https://github.com/openinfer-project/openinfer/commit/a157baf6)（#704 修复 + 锚）**：同一个提交里，性能修复落在 `kvbm/kvbm-logical/src/registry/prt.rs`（+52），而回归锚 `tests/restore_commit.rs`（+56）落在消费侧的本 crate——「修在上游、锚在消费方」的分工让锚守护的是用户可见的端到端形状，而非上游内部实现。

配套测试的对应关系（本讲 4.1/4.2 两节已逐条精读）：

| 机制 | 配套测试 |
|------|----------|
| #216/#836 前缀缓存与边界挂载 | `lifecycle.rs` 的 `lora_salt_isolates_prefix_cache`；`pool.rs` 内联的 `probe_*`、`cache_salt_*` 系列 |
| #316/#705 恢复-提交 | `restore_commit.rs` 两个测试 + 文件头性能锚 |
| #292 生命周期与失败恢复 | `lifecycle.rs` 的 `schedule_without_apply_returns_blocks_on_drop` 等 |
| #291 页表精确覆盖 | `pool.rs` 内联的 `step_page_indices_exact_at_block_boundaries` |

#### 4.3.4 代码实践

**实践：从提交史里挖出被改名遮住的里程碑**

1. **实践目标**：掌握按路径追史的技巧，并亲眼看到 `--follow` 的必要性。
2. **操作步骤**：
   - 在 crate 目录运行：`git log --oneline -- .`（应得 9 行）
   - 数一下机制提交与 chore 提交的比例，找出两个纯改名提交（`e03de3a7`、`83549c0e`）
   - 运行：`git log --follow --oneline -- tests/restore_commit.rs`
   - 对比两份输出，找出只出现在第二份里的提交。
3. **需要观察的现象**：第二份命令多出一个 `a157baf6`（2026-07-19，#704/#705）。
4. **预期结果**：9 个提交中有 7 个机制里程碑 + 2 个改名 chore；`a157baf6` 正是 `restore_commit.rs` 的诞生提交，因为当时目录名为 `openinfer-kv-cache/` 而被路径过滤漏掉。
5. **延伸**：`git show -s --format='%h %ad %s' --date=short <hash>` 可以快速给任意提交打日期戳，本讲时间线表即由此生成。

#### 4.3.5 小练习与答案

**练习 1**：`pool.rs`（1009 行）是 #292 从 `manager.rs` 抽出来的，而不是 crate 诞生时就有的。这个抽取的触发因素是什么？

**参考答案**：kimi-k2（MLA 架构）需要绕过 `KvCacheManager` 门面、直接消费逻辑块池并自持物理缓冲（u1-l1、u2-l4）。为了不让 kimi-k2 依赖 qwen3 风格的门面，请求生命周期逻辑被抽成独立的 `pool.rs`，`manager.rs` 缩为池+缓冲的配对层——抽取边界由新消费者的需求决定。

**练习 2**：#958 的 scratch 页机制为什么对本 crate 的改动这么小（两个文件共 +57 行）？

**参考答案**：因为它复用了已有的两个不变式：页 ID 是整数区间（从 `num_blocks` 起编即天然在池的发放范围之外），`KvBuffer` 的页地址是 `base + p × page_stride` 的纯函数（scratch 页无需特殊布局）。于是 `copy_page` 只需校验物理边界，`new_with_scratch_pages` 只需把池容量与缓冲容量解耦——机制复杂性留在模型 crate 的 hedge 逻辑里，本 crate 只提供两个正交原语。

**练习 3**：裸 `git log -- .` 只有 9 个提交，这能说明本 crate 改动很少吗？

**参考答案**：不能直接下此结论。9 个提交里 7 个是机制里程碑，说明提交粒度很大（每个 PR 一次落库）；同时路径重命名还会遮蔽历史（`a157baf6` 就被漏掉）。判断活跃度应结合 `git log --stat` 看每个提交的行数（#187 一次 +702、#836 一次 +166），并配合 `--follow` 补全重命名前的历史。

---

## 5. 综合实践

**任务：产出一份「机制-时间线」表（本讲的主实践）**

1. **实践目标**：把本讲三层测试与七个里程碑串成一张可复查的表格，作为你后续贡献本 crate 时的「地形图」。
2. **操作步骤**：
   - 在 crate 目录运行 `git log --oneline -- .`，抄下 9 个提交；
   - 从中挑三个里程碑提交精读 diff，建议组合：`267be7c1`（#216 前缀缓存）、`d0a02764`（#292 pool.rs 抽取）、`bc923e08`（#958 scratch 页），每个用 `git show <hash> -- .` 阅读；
   - 为每个里程碑回答四问：解决什么问题？主要改动落在 pool/manager/view/buffer 哪个文件？配套测试在哪里（内联 / restore_commit / lifecycle）？引入了哪些本手册前面讲过的术语？
   - 汇总成一张表（参考 4.3.2 的表格骨架，加上「配套测试」列）。
3. **需要观察的现象**：三个 diff 的改动面差异巨大——#216 主要动测试、#292 是大规模搬运、#958 只有 57 行新增。
4. **预期结果**：你的表应与 4.3.2 的行对得上；如果发现任何一行的「落点文件」与你的 diff 观察不符，以 diff 为准修正本讲义（这正是复查）。
5. 建议再补一行 `a157baf6`（用 `git log --follow` 找到它），并注明其特殊性：修复在上游 kvbm、回归锚在本 crate。

## 6. 本讲小结

- 本 crate 的测试分三层：`src/pool.rs` 内联 `#[cfg(test)]`（哈希稳定性与状态机口径，7 个测试）、`tests/restore_commit.rs`（纯逻辑恢复-提交形状，无 GPU，2 个测试）、`tests/lifecycle.rs`（经 `KvCacheManager` 的端到端生命周期，需 CUDA，10 个测试）。
- 分层的判据是架构本身：`BlockPool` 纯逻辑 → 无 GPU 可测；`KvCacheManager`/`KvBuffer` 物理 → 需要真实设备。
- 「测试文件注释即性能回归锚」：#704 的 36ms → ~1.6ms 钉在 `restore_commit.rs` 头注释里，测试本体守护「批量提交后前缀可作为一个连续整体再匹配」的确定性形状，计时留给人工复测。
- 内联层有三个值得模仿的写法：`#[cfg(test)]` 专用访问器触达协议前提（`prompt_block_hashes`）、线上 issue 的 CPU-only 形状固化（`issue_681_*`）、自我退役断言（`raw_overshoots > 0`）。
- 提交史呈「模型线需求驱动的四幕剧」：诞生与抽取（#187/#292）→ 跨层扩张（#316/#705）→ 收缩（#858）→ 再扩张（#958/#836）；`pool.rs` 是 #292 为 MLA 直连而抽取的产物。
- 按路径追史要小心重命名：`a157baf6`（restore_commit.rs 的诞生）需 `--follow` 才能找到。

## 7. 下一步学习建议

至此 `pegainfer-kv-cache` 的三单元十一讲全部完成。建议的后续方向：

1. **跨 crate 验证学过的契约**：读 `pegainfer-kv-offload` 的引擎侧（`a157baf6` 的 diff 里出现了 `openinfer-kv-offload/src/engine.rs`），看 16 字节内容键（u3-l1 的 `sequence_hash_bytes`）在 CPU/SSD 层如何被消费——那是两个 crate 之间唯一的契约点。
2. **看消费者**：挑 `pegainfer-qwen3` 或 `pegainfer-kimi-k2` 的调度器代码，找 `BlockPool`/`KvCacheManager` 的调用点，验证 u1-l1 讲的「门面 vs 直连」两条集成路线。
3. **动手贡献一个测试**：用 4.2.5 练习 3 的思路，在 `tests/lifecycle.rs` 里新增指针稳定性测试（两次 `new_request` 之间 `KvBuffer::device_ptr` 不变），这是对本讲分层原则的最好巩固。
4. **回到仓库级文档**：`docs/subsystems/runtime/kv-cache-design.md`（#187 随 crate 一起落地，491 行）是本 crate 的设计文档，现在你已经具备逐节核对其与实现一致性的能力。
