# 显存预算：上下文上限推导与实测 KV 池填充

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 `glm52_cap_bytes` 这本「精确账本」：为什么一个候选上下文上限（cap）的显存开销可以写成**纯算术**，以及这让它可以被单元测试。
2. 复述 `derive_max_model_len` 的两条路径：显式 `--max-model-len` 的四道校验门，与自动模式在 64 对齐候选上的二分搜索。
3. 解释 #818 之后的**两阶段实测填充**：cap 与 KV 池容量解耦，池大小由「构建后实测的 fleet 最小空闲 VRAM ÷ 每块 slab 边际成本」决定，floor 池与 `GLM52_KV_POOL_BLOCKS` 是两道栅栏。
4. 说明 post-build headroom 再探测如何把「账本漂移」变成启动期失败，而不是服务中途 OOM。

本讲只回答一个问题：**启动那一刻，这台机器上每个请求最多能吃多长的上下文、整个 rank 能装下多少 KV 页？**

## 2. 前置知识

- **显存（VRAM）预算**：权重加载完之后，GPU 上剩余的空闲显存要在「KV cache 池」「CUDA Graph 实例」「cuBLAS/DeepGEMM 工作区」「碎片」之间分配。本讲讨论的就是这笔分配的决策代码。
- **fleet 最小值**：EP 模式下每个 rank 持有自己那份权重和 KV，任何分配决策都必须按**所有 rank 里最穷的那个**来定，否则最穷的 rank 会 OOM 拖垮整个集体通信组（fate-sharing）。
- **cap（`max_model_len`）与池（pool）**：cap 是*单个请求*的上下文长度上限；池是 *rank 全局*的 KV 页总数。旧设计里池 = `slots × cap`（每个 slot 都按满 cap 预留），两者耦合；#818 之后解耦——cap 只要求「装得下一个满长请求」，池容量由实测空闲显存另外决定。
- **64-token 页**：GLM5.2 的 KV 以 64 token 为一页（FlashMLA 稀疏页 = index-K 缓存块 = 对齐单位，u4-l2 会展开）。所以 cap 必须是 64 的倍数，池以「块」为单位计数。
- **二分搜索的单调性**：候选 cap 的开销随 cap 单调不减，于是「找最大的放得下的 cap」可以在对齐候选上二分，代价是对数次账本计算而不是线性扫描。
- **精确账本 vs 经验 reserve**：能从常量精确算出的字节数走账本；无法建模的（碎片、graph 实例）走经验 reserve 常量。本讲的核心张力就在这两类之间。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs) | 预算决策的家：reserve 常量、`glm52_cap_bytes`、`derive_max_model_len`、`build_rank_models`（实测填充）、`ensure_post_build_headroom`，以及 `max_model_len_tests` 单测 |
| [src/model/mod.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs) | 页几何常量（页字节构成、stride）、`glm52_pool_blocks`、`glm52_arena_bytes` 账本、两阶段构建的 `finish_kv`（真正分配 slab） |
| [src/runner.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs) | worker 侧两阶段：`build_fixed`（阶段一，报告实测空闲 VRAM）与 `finish_kv`（阶段二，按定好的块数分配） |
| [src/mla_front.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs)、[src/moe_ep.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs) | 探测后落地的两笔「精确追加开销」：MLA q_a|kv_a 打包孪生、SM100 DeepGEMM 缓冲增量 |

## 4. 核心概念与源码讲解

### 4.1 精确账本 glm52_cap_bytes：一个候选 cap 的每一字节

#### 4.1.1 概念说明

要回答「这个 cap 放不放得下」，前提是能**精确**算出它花多少字节。GLM5.2 的选择是：把所有随 cap 与池块数线性变化的分配写成纯函数 `glm52_arena_bytes`（几何常量全部来自 u2-l1 的编译期 `const`），再加上投机通道的开销，就是 `glm52_cap_bytes`。精确账本带来两个直接收益：

- **可单测**：函数不碰 CUDA，测试可以拿假想的空闲字节数反演预算（见 4.2.4）。
- **可作边际差分**：账本对块数严格线性，`cost(blocks+1) − cost(blocks)` 就是每块的 slab 边际成本，实测填充直接拿它当单价（见 4.3）。

无法精确建模的部分不许混进账本，而是走 reserve 常量（4.2 讲），两类字节泾渭分明。

#### 4.1.2 核心流程

```text
glm52_cap_bytes(cap, pool_blocks, drafter, prefill_only, moe_topo)
  = glm52_arena_bytes(cap, pool_blocks, prefill_only)   # KV slab + 各类表
  + dspark ? glm52_dspark_arena_bytes(cap)              # DSpark 草稿通道（只随 cap 缩放）
  | mtp    ? glm52_mtp_arena_bytes(cap, pool_blocks, topo)  # native MTP（随 cap 与块数）
  | none   ? 0
```

`glm52_arena_bytes` 内部是五项求和：

1. **slab**：`pool_blocks × 3,503,040 B + 3,502,592 B`（每块一页 stride，外加一页内容的尾部余量）；
2. **rope 表**：`2 × cap × 32 × 2 B`（cos/sin，bf16）；
3. **indexer logits 桶缓冲**：所有 decode bucket 行数之和 × `cap` 向上取整到 256 × 6 B（bf16 + f32）；
4. **块表**：bucket 行数之和 × 表宽 × 4 B；
5. **prefill 解包池**（仅 prefill-only）：`pool_blocks × 64 × 512 × 2 B + pool_blocks × 4 B`。

其中一页的内容账是纯几何（u4-l2 展开）：

\[ 78 \times (64 \times 656) + 21 \times (64 \times 128 + 64 \times 4) + (64 \times 656) + (64 \times 128 + 64 \times 4) = 3{,}274{,}752 + 177{,}408 + 50{,}432 = 3{,}502{,}592 \text{ B} \]

即 78 个 MLA 切片 + 21 个 full-indexer 的 index-K 切片 + 第 78 层 MTP 镜像（一个 MLA 切片 + 一个 index-K 切片）。stride 把内容向上取整到 656-byte 的 token 行：\( 5{,}340 \times 656 = 3{,}503{,}040 \) B。

#### 4.1.3 源码精读

[lib.rs:660-675](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L660-L675) 是账本入口：固定构建 arena 加上所选投机通道的开销。

[model/mod.rs:205-240](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L205-L240) 是五项求和的实现；slab 一项就是 `块数 × GLM52_KV_PAGE_STRIDE + GLM52_KV_PAGE_CONTENT_BYTES`。

[model/mod.rs:139-163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L139-L163) 钉死页的三个字节常量，并用编译期断言保证 stride 是 656 的倍数、pad 尾巴不足一行——这正是边际差分成立的前提。

[model/mod.rs:177-203](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L177-L203) `glm52_page_layout` 用同一份构造把内容账加出来并 `assert_eq!` 到 `GLM52_KV_PAGE_CONTENT_BYTES`——**布局函数和字节常量互为对方的测试**，谁漂移谁编译期/测试期爆红。

[model/mod.rs:112-130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L112-L130) 定义对齐常量与 nominal 池公式：一个 slot 的终身页数是 \( \lceil (cap+1)/64 \rceil \)（+1 来自悬空 token 契约，u7-l4 展开），池再 `+1` 是 padding 页（block 0）。

[model/mod.rs:943-1021](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L943-L1021) 是消费端：`finish_kv` 真正 `alloc_zeros` 这块 slab，行数是 `pool_blocks + scratch_blocks`（EP MTP 的草稿 scratch 页排在池区之后），与账本公式逐字对应。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证页内容账，确信 `3,502,592` 不是魔法数。
2. **操作步骤**：
   - 打开 [model/mod.rs:142-156](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L142-L156)，记下三个切片常量：`GLM52_KV_PAGE_MLA_BYTES = 64×656`、`GLM52_KV_PAGE_IDXK_BYTES = 64×(128+4)`、`GLM52_KV_PAGE_CONTENT_BYTES`。
   - 用计算器复算上面的公式（78 层 MLA、21 层 index-K、MTP 镜像一组）。
   - 再算 stride：\( \lceil 3{,}502{,}592 / 656 \rceil \times 656 \)。
3. **需要观察的现象**：两个结果分别等于 `GLM52_KV_PAGE_CONTENT_BYTES` 与 `GLM52_KV_PAGE_STRIDE`；pad 尾巴 = 448 B < 656 B，恰好满足 [model/mod.rs:159-163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L159-L163) 的编译期断言。
4. **预期结果**：数字逐项吻合。若中途想看 `glm52_page_layout` 逐层加总，读 [model/mod.rs:177-203](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L177-L203)，它结尾的 `assert_eq!` 就是你刚手算的等式。

#### 4.1.5 小练习与答案

**练习 1**：prefill-only 形态下每多买一个池块，边际成本比 decode 形态多多少字节？
**答案**：多 `64 × 512 × 2 + 4 = 65,540` B（解包 bf16 KV 池 + 每块 4 字节），见 [model/mod.rs:230-238](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L230-L238) 的 `prefill_unpacked` 项；decode 形态该项为 0。

**练习 2**：为什么账本对 `pool_blocks` 必须严格线性？
**答案**：因为实测填充用 `cost(floor+1) − cost(floor)` 当每块单价（[lib.rs:1365-1382](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1365-L1382)）；只要账本线性，一次差分就是任意块数下的边际成本，除法即可换算块数。若存在非线性项（如按池大小分档的表），差分就不再代表单价。

**练习 3**：账本为什么把 DSpark 与 native MTP 写成两个不同签名（一个只吃 `cap`，一个吃 `cap + pool_blocks + topo`）？
**答案**：DSpark 草稿状态只随 cap 缩放（[lib.rs:668-671](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L668-L671)）；native MTP 的草稿 KV 镜像住在 slab 里、还随拓扑走 EP scratch 页，所以随池块数与拓扑缩放。签名差异本身就是开销结构的文档。

### 4.2 derive_max_model_len：显式校验与自动二分

#### 4.2.1 概念说明

权重落 GPU 后，引擎从加载报告取 fleet 最小空闲 VRAM（[lib.rs:916-922](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L916-L922)），先精确扣掉两笔探测后才会落地的开销——MLA q_a|kv_a 打包孪生（[mla_front.rs:220-227](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L220-L227)）与 SM100 DeepGEMM 增量（[moe_ep.rs:69-73](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L69-L73)），见 [lib.rs:923-939](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L923-L939)。然后 `derive_max_model_len` 在这笔预算上做决策：

- **reserve 分层**：先扣经验 reserve 得到 `budget_bytes`：
  - `GLM52_VRAM_RESERVE_BYTES = 5 GiB`（[lib.rs:599-611](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L599-L611)）：覆盖探测后无法精确建模的分配——MLA W_UK/W_UV 反量化净增量、DeepEP 集合通信缓冲、8 个整步 graph 实例、cuBLAS 工作区、碎片。注释里记录了 8×H200 实测最差 rank 约 3.05 GiB，留 ~2 GiB 余量。
  - DSpark 开启时再加 `GLM52_DSPARK_VRAM_RESERVE_BYTES = 5 GiB`（复制式草稿权重 + dense scratch，在探测*之后*加载）。
  - prefill-only 再加 scratch 预留：`3 GiB 固定 + 160 KiB × chunk 行数`（[lib.rs:724-735](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L724-L735)）。
- **显式 cap**（用户给了 `--max-model-len`）：四道门全部通过才接受。
- **自动模式**：在 64 对齐候选上二分找「nominal 池也放得下」的最大 cap。
- **floor 池**：决策最后给出实测填充的下限——nominal 池（`slots × cap`）放得下就用 nominal（与旧行为一致），否则退到「一个满长请求」池。

设计哲学：**宁可启动失败，不可带病服务**。预算不足时错误消息把每个数字（需求、预算、reserve、探测值）都打出来，见 [lib.rs:792-802](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L792-L802)。

#### 4.2.2 核心流程

```text
budget = min_free_vram − post_weight_charge − reserve
显式路径（requested = Some(C)）:
  1) C ≥ 4096（GLM52_MIN_MODEL_LEN）
  2) C ≤ 1,048,576（GLM52_MAX_CONTEXT，checkpoint 的 max_position_embeddings）
  3) C % 64 == 0（否则报出两侧最近的合法值）
  4) cap_bytes(C, one_request_pool(C)) + graph_reserve ≤ budget
自动路径（requested = None）:
  在 [0, MAX_CONTEXT/64] 上二分，谓词 = cap_bytes(c·64, nominal_pool(c·64)) ≤ budget
  结果 < 4096 则拒绝启动
floor:
  cap_bytes(cap, nominal) ≤ budget ? nominal : one_request(cap)
```

注意两条路径用的**池假设不同**：自动模式按 nominal（slots × cap）搜索——语义是「连传统并发池都放得下的最大 cap」；显式路径只需 one-request 池放得下（#818 解耦的核心：显式大 cap 合法，并发交给实测填充给）。二分正确性依赖账本对 cap 单调；`mid = ⌈(lo+hi)/2⌉` 保证区间长度为 1 时也能推进。

#### 4.2.3 源码精读

[lib.rs:743-758](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L743-L758) 是函数签名与 reserve/budget 的组装，函数 doc 直说「kept free of CUDA so the policy is unit-testable」。

[lib.rs:759-803](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L759-L803) 显式路径：四道 ensure 门；第 4 道把 graph reserve 一起算进需求——注释解释了为什么：cap 落在 `(budget−reserve, budget]` 区间时若不校验，实测填充会退到 one-request 池「侥幸通过」，然后在 FinishKv 或懒捕获 graph 时裸 OOM。

[lib.rs:804-833](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L804-L833) 自动路径二分；推导结果低于 `GLM52_MIN_MODEL_LEN = 4096` 时报错——预算小到这种程度是配置错误，不是可服务的引擎（[lib.rs:628-630](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L628-L630)）。

[lib.rs:834-854](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L834-L854) floor 决策与 `Glm52ContextBudget` 的组装；[lib.rs:644-656](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L644-L656) 该结构体的 doc 点明「池块数不再在这里决定，账本只给下限，权威数字来自实测两阶段构建」。

[lib.rs:679-681](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L679-L681) 与 [lib.rs:720-722](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L720-L722) 分别是 one-request 池与 nominal 池公式：\( \lceil (C+1)/64 \rceil + 2 \) 与 \( S \times \lceil (C+1)/64 \rceil + 1 \)（S 为 slot 数，默认 8，[model/mod.rs:101-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L101-L110)）。

[lib.rs:950-979](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L950-L979) 启动日志：把 cap 来源（显式/推导）、探测值、两笔精确扣减、floor-pool arena、reserve、budget 全部打出——日志、测试与决策消费同一组数字，不会漂移。

#### 4.2.4 代码实践

1. **实践目标**：跑通 `max_model_len_tests` 单测，理解「测试反演账本」的手法。
2. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-glm52 --features glm52 --lib max_model_len_tests
   ```
   先读 [lib.rs:1798-1815](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1798-L1815) 的 `free_for` 辅助函数：它用**同一个** `glm52_cap_bytes` 反推出「恰好预算出一个 cap」的空闲字节数，所以测试考的是策略本身而不是一份平行公式。
   再挑三个测试对照源码读：`derived_cap_is_aligned_and_scales_with_free_vram`（[lib.rs:1817-1842](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1817-L1842)）、`floor_falls_back_to_a_one_request_pool_for_an_explicit_big_cap`（[lib.rs:1855-1872](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1855-L1872)）、`derived_cap_never_exceeds_the_checkpoint_ceiling`（[lib.rs:1965-1977](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1965-L1977)）。
3. **需要观察的现象**：测试不初始化 GPU（策略函数无 CUDA 依赖），但编译 crate 需要能编 `pegainfer-kernels` 的 CUDA 工具链（无 `nvidia-smi` 的机器可设 `PEGAINFER_CUDA_SM` 覆盖 SM 目标）。
4. **预期结果**：全部测试通过；特别地，第一个测试断言 `free_for(10_048)` 的精确预算恰好推导出 10_048——这正是「精确账本」的含金量：预算边界可被逐字节命中。运行结果**待本地验证**（本环境未执行）。

#### 4.2.5 小练习与答案

**练习 1**：用户传 `--max-model-len 5000` 会发生什么？
**答案**：第 3 道门拒绝：5000 不是 64 的倍数，错误消息给出两侧最近合法值 4992 与 5056（[lib.rs:769-775](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L769-L775)）；单测 [lib.rs:1993-2009](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1993-L2009) 断言了消息里确实含这两个数。项目哲学是拒绝而非静默取整。

**练习 2**：cap = 99,968、8 个 slot 时，nominal 池与 one-request 池各多少块？
**答案**：每 slot 页数 \( \lceil 99{,}969/64 \rceil = 1{,}563 \)；nominal = 8 × 1,563 + 1 = 12,505 块；one-request = 1,563 + 2 = 1,565 块（[lib.rs:679-681](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L679-L681)、[lib.rs:720-722](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L720-L722)）。后者约为前者的 1/8——这就是显式大 cap 能启动的原因。

**练习 3**：为什么 `derived_cap_never_exceeds_the_checkpoint_ceiling` 要拿 `usize::MAX / 2` 这么夸张的空闲值？
**答案**：验证二分的上界 `hi = GLM52_MAX_CONTEXT / 64`（[lib.rs:807](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L807)）：显存再富裕，cap 也封顶在 checkpoint 的 `max_position_embeddings = 1,048,576`（[config.rs:18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L18)），不会拿模型没学过的位置外推。

### 4.3 measured pool fill：两阶段构建与 slab 边际成本

#### 4.3.1 概念说明

cap 定了之后，池该买多少块？旧答案是 `slots × cap`（与 cap 耦合）；#818 之后改成**实测填充**：

1. 每个 worker 先做**阶段一构建**（`build_fixed`）：分配一切不随池大小变化的固定部分，然后**实测**本卡空闲 VRAM 并回报（[runner.rs:714-741](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L714-L741)）。
2. 引擎取 fleet 最小值，减去 graph reserve（默认 3 GiB，`GLM52_GRAPH_RESERVE_GIB` 可覆盖）与**已知的 post-finish 开销**（DeepGEMM 缓冲、DSpark 草稿权重，[lib.rs:987-992](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L987-L992)），再除以账本差分出的每块 slab 单价，就是可负担块数；但它**永远不低于 floor**（[lib.rs:704-715](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L704-L715)）。
3. 每个 worker 做**阶段二**（`finish_kv`）：按定好的块数分配 slab 并回报（[runner.rs:743-763](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L743-L763)）。

好处：**池容量与 cap 解耦**。同一个 cap 下，显存多就多买页（更多并发、更长前缀缓存驻留），显存少就少买页；cap 只受 one-request 门槛约束。调度侧的 `BlockPool` 与 admission（u7-l4）按页数做终身预留，所以池小一点也不影响正确性，只影响并发度。

两道栅栏保护 `GLM52_KV_POOL_BLOCKS`（手工钉死池大小的 A/B 旋钮）：

- **下栅栏**：pin ≥ one-request 池——低于它等于宣传一个任何请求都放不下的 cap，admission 会让不可能的请求永远堵在 FIFO 头。
- **上栅栏**：pin ≤ 实测可负担块数——高于它要么 slab 分配直接失败，要么吃掉 graph reserve 死在预捕获。

两道都「拒绝启动，报出数字」，见 [lib.rs:1387-1413](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1387-L1413)。

#### 4.3.2 核心流程

```text
阶段一: 所有 worker build_fixed → 各自回报 free_vram（全部回报后才继续）
min_free = min(回报值)
usable = min_free − graph_reserve(默认 3 GiB) − post_finish_reserve
blocks  = max(usable / slab_bytes_per_block, floor_blocks)
   其中 slab_bytes_per_block = cap_bytes(floor+1) − cap_bytes(floor)   # 账本差分
GLM52_KV_POOL_BLOCKS=P 时:
   P ≥ one_request(cap) 且 P ≤ blocks(以 one_request 为 floor 重算) 才接受 P
阶段二: 所有 worker finish_kv(blocks)
```

两个阶段**刻意分开**：阶段一可能 OOM 或打包漂移失败，必须所有 rank 都报成功才有人进入阶段二——否则一个 rank 失败会把同伴晾在集合通信初始化里（[lib.rs:1319-1327](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1319-L1327) 的 doc 详述了这个失败语义）。

#### 4.3.3 源码精读

[lib.rs:1342-1364](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1342-L1364) 阶段一的异步下发与回收；注意 rank 用**全局编号**标注日志（多进程 fleet 下每个进程的第一个 worker 不都叫 rank 0）。

[lib.rs:1365-1382](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1365-L1382) slab 边际成本：对同一本账本做 `floor+1` 与 `floor` 两次求值再相减，注释点明「账本线性，一次差分即边际」。

[lib.rs:1383-1421](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1383-L1421) `GLM52_KV_POOL_BLOCKS` 的两道栅栏与未设置时的实测填充。

[lib.rs:1422-1434](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1422-L1434) 池日志：块数、折合 token 数、超出 nominal 多少、每个输入量——#818 解耦在日志里的样子。

[model/mod.rs:997-1021](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L997-L1021) 阶段二消费端：把占位几何 rebind 到真实块数、按 `pool_blocks + scratch_blocks` 分配 slab；doc 强调「引擎的 BlockPool 与这里的 slab 必须同一块数——pool block id 直接索引 slab 页」。

[lib.rs:1087](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1087) `kv_total_blocks = pool_blocks - 1`：减掉的 1 就是 padding 页（block 0），随后经 watch 通道进入 `SchedulerMetrics` 供调度器与前端观察。

[lib.rs:1030-1032](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1030-L1032) 每个逻辑 rank 一个 `BlockPool::new(64, pool_blocks)`——纯 CPU 对象，在引擎 spawn 之前建好（store 的 rank 表在构建期冻结）。

[lib.rs:1874-1891](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1874-L1891) 实测填充的单测：三个断言分别覆盖「只剩 reserve 时 floor 不缩水」「每字节按边际单价买块」「已知 post-finish 开销与 reserve 一并扣减」。

#### 4.3.4 代码实践

1. **实践目标**：手推一次实测填充，并预判 `GLM52_KV_POOL_BLOCKS` 越栅栏时的失败消息。
2. **操作步骤**：
   - 设定假想场景（示例推演，非实测）：阶段一后 fleet 最小空闲 20 GiB，graph reserve 默认 3 GiB，post-finish 开销 0；cap 已定为 40,960（nominal 池 8×641+1 = 5,129 块放得下，故 floor = nominal）。
   - 计算单价：`slab_bytes_per_block = 3,503,040 B`（decode 形态，见 4.1 练习）。
   - 算块数：\( \lfloor (20 - 3) \times 2^{30} / 3{,}503{,}040 \rfloor = \lfloor 18{,}253{,}611{,}008 / 3{,}503{,}040 \rfloor = 5{,}210 \) 块 → 取 max(5,210, 5,129) = 5,210 块，超出 nominal 81 块。
   - 再推演两个非法 pin：`GLM52_KV_POOL_BLOCKS=10`（< one-request 的 643）与 `GLM52_KV_POOL_BLOCKS=99999`（> 5,211 的可负担上限），对照 [lib.rs:1394-1412](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1394-L1412) 写出会命中哪条 ensure。
3. **需要观察的现象**：三个数字链（探测 → usable → 块数）与日志 [lib.rs:1422-1434](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1422-L1434) 的字段一一对应；非法 pin 的错误消息各自引用 one-request 下限或可负担上限。
4. **预期结果**：手算块数 5,210、两个 pin 分别被下/上栅栏拒绝。注意 20 GiB 是假设值，真实启动请看日志里的「min rank free」；若你在 GPU 机器上，可加 `RUST_LOG=info` 观察启动日志验证（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么实测填充只在阶段一与阶段二**之间**做，而不是加载权重前就定好池？
**答案**：因为只有阶段一之后，所有固定分配（权重、buckets、rope 表、indexer 桶缓冲）才真正落地，空闲 VRAM 才是可信的测量值；加载前估算要复刻整个 CUDA 分配器的行为，必然回到经验常量。两阶段构建让「测量」成为可能（[lib.rs:1319-1327](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1319-L1327)）。

**练习 2**：`GLM52_GRAPH_RESERVE_GIB` 是池填充里「唯一剩下的估计」（[lib.rs:683-690](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L683-L690)）。为什么它不像 5 GiB reserve 那样拆成精确项？
**答案**：它能覆盖的开销（懒捕获的整步 graph 实例、cuBLAS/FlashInfer 工作区、碎片）没有常量公式可依，且发生在 FinishKv *之后*——此时已无法再改池大小，只能预留。做成旋钮允许在实测漂移时手工调大调小做 A/B。

**练习 3**：nominal 池作 floor 有什么兼容性意义？
**答案**：显存宽裕时 `max(usable/单价, nominal) = nominal` 附近，行为与解耦前的旧默认几乎一致（单测 `floor_is_the_nominal_pool_when_the_ledger_affords_it`，[lib.rs:1844-1853](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1844-L1853)）；floor 只在「显式大 cap 挤掉了 nominal」时才退到 one-request——解耦是**放宽**约束而非改变默认。

### 4.4 post-build headroom 再探测

#### 4.4.1 概念说明

预算再精确也是模型。若 reserve/账本随代码演进漂移（比如新加一条未入账的分配），错误应该在**启动期**爆出来，而不是服务中途 OOM——后者在 EP fleet 里意味着集体通信组整体坍塌、难以归因。所以构建完成后引擎再探一次每 rank 的空闲 VRAM，低于 `GLM52_POST_BUILD_MIN_FREE_BYTES = 1 GiB` 就拒绝启动（[lib.rs:632-637](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L632-L637)）：这 1 GiB 是 graph 实例化与碎片的最小呼吸空间。

#### 4.4.2 核心流程

```text
FinishKv 全员完成 → SetupComm 之前/前后（post_comm_startup 闭包内）
对每个 worker 异步发 FreeVram 探测 → 回收每 rank 空闲值
free < 1 GiB ? 启动失败（错误带具体数字） : 记 info 日志继续
```

它在 [lib.rs:1034-1041](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1034-L1041) 的 `post_comm_startup` 闭包里被调用，位于 prefill kernel 预检与 DSpark 草稿加载之后、KV store 构建之前。

#### 4.4.3 源码精读

[lib.rs:1293-1317](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1293-L1317) `ensure_post_build_headroom`：全员异步探测、逐 rank 校验、失败消息建议「调低 --max-model-len 或释放显存」，成功则把每 rank 空闲值打进日志。

[runner.rs:740](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L740) 与 [runner.rs:694](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L694) 是同一探测原语的三个使用点：权重加载报告、阶段一回报、显式 FreeVram 指令——一次实现，三处复用。

[mla_front.rs:213-219](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L213-L219) 的 doc 是这条防线的注脚：q_a|kv_a 孪生约 1.2 GiB 若不入账，「未入账的分配会吃掉 post-build headroom 地板」；混合架构 fleet 里账本可能算错，re-probe 是兜底。

#### 4.4.4 代码实践

1. **实践目标**：源码阅读型实践——追踪一次「账本漂移」如何被 re-probe 拦截。
2. **操作步骤**：
   - 假想有人给 `finish_kv` 增加了一块 1.5 GiB 的未入账缓冲（不要真的改）。
   - 沿启动时序标注：4.2 的显式/自动 cap 校验（ unaware）→ 4.3 的实测填充（unaware）→ FinishKv 真正分配 → re-probe。
   - 查 [lib.rs:1303-1309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1303-L1309) 的错误消息文案，确认它把 rank 号、实测空闲值与地板值都打了出来。
3. **需要观察的现象**：漂移缓冲把某 rank 空闲压到 1 GiB 以下时，启动在这里失败并给出可行动的信息，而不是服务几分钟后 OOM。
4. **预期结果**：能写出「哪一步发现、报什么数字、用户被建议做什么」三段式结论；此为静态分析，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：re-probe 的地板是 1 GiB，而 4.2 的 reserve 是 5 GiB，两者矛盾吗？
**答案**：不矛盾，层次不同。5 GiB reserve 是*进入预算前*的经验扣减（覆盖账本外开销并留余量）；1 GiB 是*全部构建完成后*的最终校验地板——若实际只花了 reserve 的一半，re-probe 会放行更多隐性余量；若开销超预算，re-probe 兜底。前者防超支，后者防漂移。

**练习 2**：为什么 re-probe 失败是可接受的启动失败，而服务中途 OOM 不可接受？
**答案**：启动失败发生在任何集合通信组进入长生命周期之前（或配合广播 Shutdown 拆除，见 [lib.rs:993-1001](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L993-L1001)），错误定位直接；服务中 OOM 会让一个 rank 掉队，free-running fleet 里其余 rank 全速空转直到 DeepEP 超时（约 100 s），事后难以归因。这与 u7-l1 的 fate-sharing 纪律一脉相承。

## 5. 综合实践

**任务：写一个「GLM5.2 显存预算模拟器」，把本讲三段决策串成一条链。**

用 Python 复刻公式（**示例代码**，非项目源码，数字以源码常量为准）：

```python
# 示例代码：GLM5.2 显存预算模拟器（decode / EP8 / 无 drafter 的简化版）
PAGE_STRIDE = 3_503_040          # model/mod.rs GLM52_KV_PAGE_STRIDE
CONTENT     = 3_502_592          # GLM52_KV_PAGE_CONTENT_BYTES
MLA_TOKENS  = 576 // 9           # 每层 rope 半维 32 * 2 表 * bf16 —— 见下方练习
RESERVE     = 5 << 30            # lib.rs GLM52_VRAM_RESERVE_BYTES
GRAPH_RESERVE = 3 << 30          # glm52_graph_reserve_bytes 默认
MIN_LEN, MAX_CTX, ALIGN = 4096, 1_048_576, 64
BUCKET_ROWS = sum([1, 2, 4, 8, 16, 32, 48, 64, 96])   # GLM52_DECODE_BUCKETS 之和
SLOTS = 8

def arena(cap, blocks):                      # model/mod.rs glm52_arena_bytes 的 decode 分支
    slab = blocks * PAGE_STRIDE + CONTENT
    rope = 2 * cap * 32 * 2
    logits = BUCKET_ROWS * ((cap + 255) // 256 * 256) * (2 + 4)
    tables = BUCKET_ROWS * ((cap + ALIGN - 1) // ALIGN) * 4
    return slab + rope + logits + tables

cap_bytes = arena                             # 无 drafter 时 glm52_cap_bytes == arena
one_request = lambda c: (c + 1 + ALIGN - 1) // ALIGN + 2
nominal = lambda c: SLOTS * ((c + 1 + ALIGN - 1) // ALIGN) + 1

def derive(free_after_weights):
    budget = free_after_weights - RESERVE
    lo, hi = 0, MAX_CTX // ALIGN
    while lo < hi:                            # lib.rs:804-833 的二分
        mid = (lo + hi + 1) // 2
        if cap_bytes(mid * ALIGN, nominal(mid * ALIGN)) <= budget: lo = mid
        else: hi = mid - 1
    cap = lo * ALIGN
    assert cap >= MIN_LEN
    floor = nominal(cap) if cap_bytes(cap, nominal(cap)) <= budget else one_request(cap)
    return cap, floor, budget

def fill(free_after_fixed, floor):            # lib.rs:704-715 实测填充
    usable = free_after_fixed - GRAPH_RESERVE
    unit = cap_bytes(0, floor + 1) - cap_bytes(0, floor)   # 差分 = PAGE_STRIDE
    return max(usable // unit, floor)

# 场景：权重后空闲 30 GiB、固定构建后 fleet 最小空闲 28 GiB
cap, floor, budget = derive(30 << 30)
blocks = fill(28 << 30, floor)
print(f"cap={cap}, floor={floor} blocks, pool={blocks} blocks (~{blocks*64/1024:.0f}K tokens)")
```

要求：

1. 跑通后回答：这个场景推导出多大的 cap？池比 nominal 多出多少块？参考答案（按源码常量精确复刻时）：cap = 60,928，floor = nominal = 7,625 块，实测填充买满 7,662 块——多出 37 块（约 2.4K token 的额外并发/缓存容量）。
2. 修改 `SLOTS = 32`（对照 `GLM52_DECODE_SLOTS`，[model/mod.rs:101-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L101-L110)），解释 cap 为什么变小、池为什么几乎不变——参考答案：cap 缩到约 15,232（nominal 池的每 token 成本翻了 4 倍），而池仍停在 7,662 块附近（可用字节与单价都没变）。「cap 与池解耦」的直观演示。
3. 示例里埋了一行与任何计算无关的干扰项（提示：它模仿了一个 MLA 维度常量）。找到并删掉它，并说出它模仿的真实常量是谁、属于哪条链路。
4. 对照 [lib.rs:950-979](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L950-L979) 与 [lib.rs:1422-1434](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1422-L1434) 的真实日志字段，列出模拟器没建模的项（DSpark/MTP 通道、prefill scratch、post-finish 开销、DeepGEMM 计费、TP4 专属缓存、post-weight 精确扣减）。

注意：模拟器只做教学推演；真实数字以启动日志为准。运行结果**待本地验证**。

## 6. 本讲小结

- **精确账本**：`glm52_cap_bytes = glm52_arena_bytes + 投机通道`，五项求和全部来自编译期几何常量，对 cap 与块数严格线性；线性性让「一次差分 = 每块 slab 单价（3,503,040 B）」。
- **cap 决策**：`derive_max_model_len` 不碰 CUDA 所以可单测；显式 cap 过四道门（最小值、checkpoint 上限、64 对齐、one-request 池 + graph reserve 放得下），自动模式按 nominal 池二分；两条路径的池假设不同是 #818 解耦的体现。
- **reserve 分层**：5 GiB 基础 reserve（账本外开销）+ DSpark 5 GiB + prefill scratch（3 GiB + 160 KiB/行）+ graph reserve（默认 3 GiB，池填充侧）+ 精确扣减（q_a|kv_a 孪生、DeepGEMM 增量）——精确与经验两类字节分开记账。
- **实测填充**：两阶段构建（build_fixed → 实测 fleet 最小空闲 → finish_kv），池 = max(可用字节 ÷ slab 单价, floor)；`GLM52_KV_POOL_BLOCKS` 被上下两道栅栏夹住，越界即拒绝启动。
- **headroom 再探测**：构建完成后每 rank 空闲须 ≥ 1 GiB，把账本漂移变成带数字的启动失败而非服务中 OOM。
- **日志即合同**：启动日志打出的每个数字与决策、单测消费的是同一组值（`free_for` 用同一账本反演），三者不会漂移。

## 7. 下一步学习建议

- **u4-l2（KV 页布局）**：本讲把页当成 3.5 MB 的黑盒单价；下一讲拆开它——78 个 MLA 切片与 21 个 index-K 切片在一页内如何排布、为什么 stride 必须是 656 的倍数。
- **u7-l4（admission）**：池解耦之后，「一个请求终身要多少页」的公式 \( \lceil (prompt + max\_tokens)/64 \rceil \)（含悬空 token 的 +1）在 admission 里变成 honor-or-reject 的预留纪律，是池容量决定的并发语义的另一半。
- **u7-l5（分页 KV 前缀缓存）**：实测填充买来的池块由 `BlockPool` 管理，释放块按内容哈希匹配成前缀缓存——池的「多买」部分直接转化为缓存驻留能力。
- 顺手阅读 [lib.rs:1789-2097](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1789-L2097) 的整个 `max_model_len_tests` 模块：它是「策略纯函数化 + 同一账本反演」测试范式的最佳样例，值得在你自己的项目里复制。
