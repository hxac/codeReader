# MTP 投机解码

## 1. 本讲目标

本讲讲解 ds4 中的「多 token 预测（Multi-Token Prediction，MTP）投机解码」路径。学完本讲后，你应该能够：

- 说清楚什么是投机解码（speculative decoding），以及它为什么在「贪婪解码 + GPU」场景下可能提速。
- 理解 ds4 把 DeepSeek V4 的 MTP 块当作「草案模型（drafter）」、把主模型当作「验证模型（verifier/target）」的分工。
- 读懂 `ds4_session_eval_speculative_argmax` 这台状态机：先提交一个目标 token、再让 MTP 草拟一个短后缀、再用目标图批量验证、最后只提交被接受的前缀并在失配时回滚。
- 理解 `--mtp-margin` 置信度门控为什么是「避免慢速部分接受」的关键旋钮。
- 知道为什么 MTP 被明确标注为实验性、为什么它的 draft 状态不随磁盘 KV 持久化。

本讲承接 u4-l3（生成与采样）——MTP 只在贪婪路径上生效，理解 argmax 是前置。本讲不展开 GPU 内核细节（u5-l2）与分布式/SSD（MTP 在这两条路径上被显式禁用）。

## 2. 前置知识

### 2.1 为什么普通自回归「慢」

普通自回归生成是「每吐一个 token，就跑一次完整的前向」。对一个 43 层、激活约 13B 参数的模型，每一次 decode（解码）都要把 43 层全部跑一遍，却只换来 1 个 token。也就是说：

\[ \text{吞吐} \approx \frac{1}{\text{单次 decode 耗时}} \]

GPU 在 decode 阶段往往「算不满」（每层只处理 1 个 token 的 batch），大量算力被浪费。

### 2.2 投机解码的直觉

投机解码的核心想法是：用一个**更便宜的小模型（draft model / 草案模型）**先猜出接下来的 k 个 token，然后让**大模型（target / 验证模型）**一次性把这 k 个位置一起算出来做验证。如果大模型也认同这些 token，那一次大模型前向就能产出多个 token；如果不认同，就接受前面一致的前缀，丢弃后面的。

关键收益在于：把 k 个 token 喂给大模型做一次**批量（batch）前向**，成本和单 token decode 相差不大（GPU 终于算得满了），却可能一次接受好几个 token。于是吞吐的上限变成：

\[ \text{加速比} \approx \frac{\text{平均每次接受的 token 数}}{1} \]

### 2.3 贪婪解码下的「接受」很简单

ds4 的 MTP 路径只服务**贪婪解码**（argmax，即永远取概率最大的 token，见 u4-l3）。在贪婪设定下，「大模型是否同意草案 token」非常简单：

> 草案 token \(d_i\) 被接受，当且仅当大模型在接受前缀之后的 argmax 恰好等于 \(d_i\)。

这与「带随机采样的投机解码」不同——采样版需要做概率比修正（rejection sampling），而贪婪版只看 argmax 是否相等，实现大大简化。

### 2.4 DeepSeek V4 的 MTP 块

DeepSeek V4 在训练时就内置了一个「多 token 预测」块（`mtp.0.*` 张量）。它本质上是一个**单层的 transformer**，输入是「当前 token 的嵌入 + 上一层的隐状态」，输出是「下一个 token 的分布」。它天然适合当草案模型——比完整的 43 层主模型便宜得多，但又共享了主模型的词表和表示空间。ds4 正是把它加载成一个独立的轻量 GGUF，专门负责起草。

> 术语约定：本讲里 **draft / 草案** 指 MTP 块猜出的 token；**target / 验证** 指主 DeepSeek V4 模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | MTP 权重绑定、草案前向、批量验证、投机状态机都在这里 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 公共 API：`ds4_engine_options` 的 MTP 字段、`ds4_session_eval_speculative_argmax` 声明 |
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | CLI 主流程里调用投机路径的分支 |
| [ds4_help.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c) | `--mtp` / `--mtp-draft` / `--mtp-margin` 的帮助文本 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | MTP 是实验性、不持久化的官方说明 |
| [download_model.sh](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh) | 下载 MTP 支持模型的入口 |

## 4. 核心概念与源码讲解

### 4.1 MTP 草案模型

#### 4.1.1 概念说明

第一个最小模块要回答的问题是：**草案从哪里来？**

ds4 的答案是——加载一个独立的 MTP GGUF 文件，它只包含一个 transformer 块（DeepSeek V4 训练时内置的 `mtp.0` 块）。这个草案模型有两个特点：

1. **共享主模型的大部分结构**：它复用主模型的词嵌入（token embedding）、输出头（output head）的结构语义，但有自己的投影矩阵（`e_proj`/`h_proj`）和归一化（`enorm`/`hnorm`）。
2. **有自己的 KV 缓存**：因为草案在「假设的未来 token」上跑，它的 raw 滑动窗口 KV（见 u4-l2）记录的是「投机未来」，和主模型的 KV 严格分离。主模型的 KV 只在被接受后才推进。

一个关键设计：**MTP 块不是采样器，主模型才是输出流的唯一权威**。MTP 只是「建议」候选 token，最终什么 token 进上下文，永远由主模型的 argmax 决定。

#### 4.1.2 核心流程

MTP 草案模型的加载与单步起草流程如下：

```
1. ds4_engine_open 时，如果传了 --mtp FILE 且不是分布式/SSD 流式：
   ├── model_open(mtp_model, mtp_path)   # mmap 加载 MTP GGUF
   ├── mtp_weights_bind()                # 把 mtp.0.* 张量绑进 ds4_mtp_weights
   └── e->mtp_ready = true

2. 每次主模型 decode 一个真实 token 时（probe）：
   ├── 顺带用 MTP 块算一个「下一个 token」的草案 → mtp_draft_token
   └── mtp_draft_valid = true            # 标记草案可用

3. 投机入口取用这个草案作为 drafts[0]，再递归起草 drafts[1..]。
```

注意第 2 步：ds4 把**第一个草案 token 的起草「搭便车」在主模型每次 decode 上**。这样到投机入口时，`drafts[0]` 几乎是免费的——主模型 decode 已经算出了它所需的隐状态。

#### 4.1.3 源码精读

**MTP 权重结构** 是一张语义指针表，核心是一个 `ds4_layer_weights block`（即一个完整的 transformer 层），外加 MTP 专属的投影与输出头：

[ds4.c:3064-3074](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3064-L3074) 定义 `ds4_mtp_weights`，其中 `e_proj`/`h_proj` 负责把「token 嵌入」和「上层隐状态」投影到同一空间再相加，`hc_head_*` 是 MTP 自己的输出头。

**绑定函数** 把 GGUF 里以 `mtp.0.` 开头的张量按名字绑进这张表（与 u3-l2 的主权重绑定同构）：

[ds4.c:4436-4474](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4436-L4474) — `mtp_weights_bind` 用 `required_tensor` 逐个查 `mtp.0.hc_head_base.weight`、`mtp.0.e_proj.weight` 等，最后调用 `mtp_weights_validate_layout` 校验。

**引擎打开时加载 MTP** 的位置在主模型加载之后、GPU 初始化之前。注意三个互斥条件：分布式、SSD 流式都不兼容 MTP：

[ds4.c:25682-25696](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25682-L25696) — 检查 `mtp_path` 非空且 `role == DS4_DISTRIBUTED_NONE`；若是 SSD 流式则报错退出；否则 `model_open(&e->mtp_model, ...)` 并置 `e->mtp_ready = true`，打印 `MTP support model loaded ... (draft=N)`。

**单步起草** 的数学：MTP 块把 token 嵌入（经 `enorm`、`e_proj`）与上一层隐状态（经 `hnorm`、`h_proj`）相加作为输入，过自己的单层 attention（用独立的 `mtp_raw_cache`）和 FFN，再用 `hc_head` 输出 logit 取 argmax：

[ds4.c:19924-19993](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19924-L19993) — `metal_graph_eval_mtp_draft_from_hc`：先 `ds4_gpu_embed_token_hc_tensor`（嵌入）、`ds4_gpu_rms_norm` + `matmul_q8_0`（`enorm`/`e_proj`），再对 `prev_hc` 做 `hnorm`/`h_proj`，最后 `ds4_gpu_add_tensor` 把两者相加成 `mtp_input_hc`。它的包装函数 [ds4.c:20039-20060](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20039-L20060) `metal_graph_eval_mtp_draft` 只是把它以主模型当前隐状态 `g->cur_hc` 为种子启动。

**搭便车 probe**：主模型每次 decode 真实 token 后，若启用了 MTP，会顺带跑一次起草，把结果存进 session：

[ds4.c:27135-27150](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27135-L27150) — 当 `mtp_should_draft` 为真时调用 `metal_graph_eval_mtp_draft`，把返回的 `mtp_top`（或 fallback 的 `sample_argmax`）存入 `s->mtp_draft_token` 并置 `s->mtp_draft_valid = true`。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：看清「MTP 草案 token 是在主模型 decode 时搭便车算出来的」这条数据流。

1. 在 `ds4.c` 中定位 `ds4_session_eval_internal`（`ds4_session_eval` 的实现，约 ds4.c:27056 起）。
2. 找到其中 `mtp_should_draft` 的判定（[ds4.c:27109-27111](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27109-L27111)），列出它要求同时满足的三个条件。
3. 跟踪 `s->mtp_draft_token` 这个字段：它在哪里被写入？哪里被读取（提示：投机入口 `drafts[0]`）？
4. **预期结果**：你能画出「主 decode → probe 起草 → 存 mtp_draft_token → 下一次投机入口取用」这条链路，并解释为什么 `drafts[0]` 几乎不计额外成本。

本实践为纯阅读，无需 GPU，**可在任意机器上完成**。

#### 4.1.5 小练习与答案

**练习 1**：`ds4_mtp_weights` 里为什么有一个 `ds4_layer_weights block` 字段，而不是像主模型那样的 `layer[DS4_MAX_LAYER]` 数组？

> **参考答案**：因为 MTP 块只包含**单个** transformer 层（DeepSeek V4 训练时的 `mtp.0` 块），所以只需要一个 `block`，而不是 43 层的数组。这也正是它比主模型便宜得多的根本原因。

**练习 2**：`ds4_engine_has_mtp` 在哪些情况下返回 `false`？

> **参考答案**：见 [ds4.c:24043-24047](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24043-L24047)。三种情况：CPU 后端、分布式（`role != DS4_DISTRIBUTED_NONE`）、或 `mtp_ready` 为假（没加载 MTP 文件）。即 MTP 只在「单机 GPU + 已加载 MTP」时可用。

---

### 4.2 接受 / 拒绝逻辑（验证）

#### 4.2.1 概念说明

第二个最小模块回答：**草案 token 怎样被接受或拒绝？**

这是投机解码正确性的命脉。核心原则只有一句：

> **主模型是唯一权威。** 任何 token 是否进上下文，最终都由主模型的 argmax 决定；MTP 的草案只是「提前算好，等主模型确认」。

因此验证逻辑是：把草案后缀喂给主模型做一次**批量前向**，逐位置比较主模型的 argmax 与草案 token。从第 1 个草案开始，连续相等的都被接受；一旦某个位置失配，该位置及之后全部丢弃。

此外有一个「免费午餐」：主模型在 commit（提交）第一个真实 token 时，**已经算出了下一步的 logit**。所以 `drafts[0]` 可以零成本验证——直接比主模型这个 logit 的 argmax 与 `drafts[0]` 即可。如果连这第一关都过不了，说明 MTP 这次猜错了第一步，整个投机直接放弃，回到普通单 token decode。

#### 4.2.2 核心流程

`ds4_session_eval_speculative_argmax` 是一台四步状态机（注释见 [ds4.c:27160-27166](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27160-L27166)）：

```
输入：first_token（主模型已经决定要 commit 的第一个 token）、max_tokens、eos_token
输出：accepted[]（本次实际提交的 token 数组）、返回值 n_accept

步骤 1  commit first_token
        └── ds4_session_eval(s, first_token)   # 推进主模型 KV，刷新 s->logits
        accepted[0] = first_token

步骤 2  免费验证 drafts[0]
        drafts[0] = s->mtp_draft_token          # 上一次 decode 搭便车算的草案
        if argmax(s->logits) != drafts[0]:
            return n_accept                      # 第一步就猜错，放弃投机

步骤 3  递归起草 drafts[1..draft_cap-1]
        └── 用 MTP 块在前一个 draft 隐状态上继续起草
            （写入 MTP 自己的 raw cache，记录 mtp_base_raw 作为回滚锚点）

步骤 4  批量验证 + 提交
        ├── 用主模型对整个 draft 后缀做一次 layer-major 批量前向
        ├── commit_drafts = 1 + 连续 argmax 匹配的个数
        ├── 把 commit_drafts 个 token 推进 checkpoint 与 accepted
        └── 若 commit_drafts < draft_n（部分接受/全拒）：
            └── spec_frontier_restore 回滚主模型 KV 到投机前
```

关键点：**部分接受（partial accept）**——只接受前缀、丢弃后缀——必须把主模型的 KV 缓存回滚到投机开始之前，否则主模型状态会被「错误的未来 token」污染。这就是为什么需要 `spec_frontier_snapshot` / `spec_frontier_restore` 这对快照原语。

#### 4.2.3 源码精读

**函数签名与早退分支**。投机入口先处理两个「退化」情况——分布式和 CPU 都不真正投机，只提交第一个 token：

[ds4.c:27167-27191](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27167-L27191) — `ds4_session_eval_speculative_argmax` 签名；`s->distributed` 分支只 `ds4_session_eval` 后返回 1；CPU 分支同理；`DS4_NO_GPU` 编译期直接报错。

**注释讲清立场**——MTP 是草案器，不是采样器，主模型定义精确输出流：

[ds4.c:27194-27205](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27194-L27205) — 先 `ds4_session_eval(s, first_token)` 提交第一个 token，处理 EOS / 容量上限等早退。

**免费验证 drafts[0]**——这是投机能否继续的「第一关」：

[ds4.c:27238-27250](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27238-L27250) — `if (sample_argmax(s->logits, DS4_N_VOCAB) != drafts[0])` 则 `return n_accept`（仅含 first_token）。否则把 `drafts[0] == eos` 时把 `draft_cap` 钳到 1。

**递归起草循环** 用双缓冲隐状态（`mtp_state_hc`/`mtp_next_hc` 交替）让 MTP 块自回归地往后猜：

[ds4.c:27264-27287](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27264-L27287) — 循环调用 `metal_graph_eval_mtp_draft_from_hc`，把结果填进 `drafts[draft_n]`，遇到 EOS 即停。

**批量验证** 是提速的来源：把整个 draft 后缀一次喂给主模型，layer-major 跑 43 层，得到每个位置的 argmax：

[ds4.c:21117-21156](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21117-L21156) — `metal_graph_verify_suffix_tops`：上传 draft token、做嵌入、对每一层 `metal_graph_encode_layer_batch`，n_tokens 受 `prefill_cap` 限制（草案很短，远小于 cap）。

**接受计数** 的核心循环——逐位置比较，第一个失配即停：

[ds4.c:27478-27482](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27478-L27482) — `commit_drafts` 从 1 起，对 `i = 1..` 比较 `row_tops[i-1] != drafts[i]`，不等就 break。这就是贪婪投机的接受规则。

**回滚原语**——部分接受时必须把主模型每层的压缩 KV 前沿恢复到投机前：

[ds4.c:24057-24087](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24057-L24087) — `ds4_spec_frontier` 结构记录每层的 `n_comp`/`n_index_comp`（压缩/indexer 前沿行数）与 `mtp_n_raw`；`spec_frontier_snapshot` 把每层 attention/indexer 的 KV 张量整块拷贝到 `spec_*` 备份张量，`spec_frontier_restore` 反向拷回。

#### 4.2.4 代码实践（源码阅读 + 可选运行）

> **实践目标**：描述 draft token 如何被验证接受/拒绝，并解释为什么部分接受时必须回滚主模型 KV。

**A. 源码阅读（任意机器）**

1. 读 `ds4_session_eval_speculative_argmax`（[ds4.c:27167](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27167) 起）。
2. 在验证分支（约 [ds4.c:27477-27553](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27477-L27553)）里找到 `commit_drafts` 的计算与三个出口：全接受（`commit_drafts == draft_n`）、prefix-1 部分接受、回滚重放。
3. 回答：当 `commit_drafts == 1`（即 drafts[1] 被拒绝）时，主模型的 KV 缓存此刻处于什么状态？代码用哪两个函数把它恢复？

> **预期结果**：你会指出此时主模型 KV 已被「假设的 drafts」批量前向污染，必须用 `spec_frontier_restore`（或 `spec_frontier_commit_prefix1`）把每层压缩/indexer 前沿恢复到 `start` 位置，再（必要时）重放被接受的前缀。

**B. 可选运行（需 macOS Metal 或 Linux CUDA 机器 + 已下载 Flash 模型）**

```sh
./download_model.sh mtp                       # 下载约 3.5GB 的 MTP GGUF
./ds4 --mtp <目录>/DeepSeek-V4-Flash-MTP-Q4K-Q8_0-F32.gguf \
      --mtp-draft 2 --temp 0 -p "用一句话解释递归。"
DS4_MTP_CONF_LOG=1 ./ds4 --mtp <...> --mtp-draft 2 --temp 0 -p "..."
```

观察 `DS4_MTP_CONF_LOG=1` 打印的 `drafted=N committed=M`：`committed` 接近 `drafted` 说明接受率高、有加速；`committed` 长期为 1 说明草案几乎总被拒。**若没有合适硬件，本步骤标注「待本地验证」，A 部分已足以达成实践目标。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `drafts[0]` 的验证是「免费」的，而 `drafts[1]` 不是？

> **参考答案**：commit `first_token` 时主模型已经算出了下一步的完整 logit（`s->logits`），所以 `drafts[0]` 只需一次 `sample_argmax` 比较即可验证，零额外前向。而 `drafts[1]` 需要主模型在「接受 drafts[0] 之后」的新上下文上再算一次，必须走批量验证前向，不是免费的。

**练习 2**：如果草案后缀全部被拒绝（`commit_drafts == 1`），相比普通 decode，投机路径多付出了哪些成本？

> **参考答案**：多付出了 (a) 起草 drafts[1..] 的 MTP 块前向；(b) 一次批量验证前向；(c) 回滚主模型 KV 的快照恢复。换来的却只有 1 个 token（drafts[0] 本可免费得到）。这就是「慢速部分接受」——也是下一节置信度门控要规避的情形。

---

### 4.3 置信度门控（`--mtp-margin`）

#### 4.3.1 概念说明

第三个最小模块回答：**怎么避免「花钱验证却只接受 1 个 token」？**

由 4.2 可知，投机的真正收益来自**全接受或接近全接受**；如果草案模型自己对下一个 token 都很犹豫，那 drafts[1] 大概率会被主模型拒绝，于是你花了「起草 + 验证 + 回滚」的成本，却只拿到本可免费的 drafts[0]。这种 partial accept 不赚反亏。

ds4 的对策是**置信度门控**：在递归起草出 drafts[1] 后，看 MTP 块给出这个 token 时的「置信度」。如果置信度太低，就直接放弃 drafts[1]，把 drafts[0] 当成普通 decode 来 commit，跳过批量验证。这个置信度阈值就是 `--mtp-margin`。

> 术语：**margin（边际）** = 草案 logit 中第一名与第二名的差值 `logit(top0) - logit(top1)`。margin 越大，说明 MTP 越「笃定」自己的选择，主模型同意的概率也越高。

#### 4.3.2 核心流程

门控逻辑只针对当前生产深度 `draft_n == 2`（即起草了 drafts[0]、drafts[1] 两个）：

```
if 非严格模式 且 draft_n == 2 且 margin 阈值 > 0:
    margin = logits_top2(mtp_logits).top0 - .top1
    if margin < 阈值 (默认 3.0):
        # 草案不自信，跳过批量验证
        对 drafts[0] 做一次普通 decode（metal_graph_eval_token_raw_swa）
        commit drafts[0]，返回（只拿到 1 个 token，但省掉了验证+回滚）
        return
    # 否则 margin 足够高，继续走批量验证（可能拿到 2 个 token）
```

阈值来源优先级：`DS4_MTP_MIN_MARGIN` 环境变量 > `--mtp-margin` 参数 > 默认 `3.0`。

需要强调门控的两个前提：

- **只在非严格模式生效**。`--quality` 或 `DS4_MTP_STRICT` 下走的是精确验证器（`metal_graph_verify_decode2_exact`），不做 margin 跳过，以保证与官方向量逐位对齐。
- **门控只优化速度，不影响正确性**。无论跳不跳过，最终进上下文的 token 都由主模型 argmax 决定；跳过路径里 drafts[0] 也经过了主模型的一次真实 decode 确认。

#### 4.3.3 源码精读

**默认值** 在引擎打开时设定：

[ds4.c:25561-25563](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25561-L25563) — `mtp_draft_tokens` 缺省 1、上限 16；`mtp_margin` 缺省 3.0（`opt->mtp_margin < 0` 时取默认）。

**选项声明与 CLI 解析**：

[ds4.h:96-101](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L96-L101) — `ds4_engine_options` 里的 `mtp_path`、`mtp_draft_tokens`、`mtp_margin` 三字段。

[ds4_cli.c:1459-1464](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1459-L1464) — `--mtp`、`--mtp-draft`、`--mtp-margin` 解析（margin 限 0..1000）。帮助文本见 [ds4_help.c:173-177](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L173-L177)。

**门控本体**——计算 margin 并在过低时跳过批量验证：

[ds4.c:27295-27336](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27295-L27336) — 当 `!strict_mtp && draft_n == 2 && mtp_margin_threshold > 0` 时，用 `logits_top2` 算出 `mtp_last_margin = v0 - v1`；若 `< mtp_margin_threshold`，则对 `drafts[0]` 跑一次 `metal_graph_eval_token_raw_swa`，commit 它，`DS4_MTP_KEEP_ACCEPTED(1)` 后返回。环境变量覆盖点在 [ds4.c:27222-27227](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27222-L27227)（`DS4_MTP_MIN_MARGIN`）。

**严格模式选择精确验证器**——保证官方向量复现：

[ds4.c:27338-27347](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27338-L27347) — 注释说明：非 quality 的 Metal 路径在批量归约扰动近乎平局的 logit 时可能选到不同的贪婪 token；`--quality` / `DS4_MTP_STRICT` 改用 `metal_graph_verify_decode2_exact`，保持单 token 目标流但**不是速度收益**。

**CLI 只在贪婪路径启用投机**——这是 MTP 生效的总开关：

[ds4_cli.c:483-511](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L483-L511) — 仅当 `temperature <= 0`（贪婪）且 `mtp_draft_tokens > 1` 且未设 `DS4_MTP_SPEC_DISABLE` 时才调 `ds4_session_eval_speculative_argmax`；否则退回普通 `ds4_session_eval`。

#### 4.3.4 代码实践（源码阅读 + 可选调参）

> **实践目标**：理解 margin 门控如何权衡「加速」与「白跑」，并设计一个对比实验。

1. 阅读门控段 [ds4.c:27295-27336](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27295-L27336)，确认：(a) 它只在 `draft_n == 2` 触发；(b) 跳过分支里 drafts[0] 仍经过一次主模型真实 decode。
2. 阅读 README 对 MTP 的定位 [README.md:691-695](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L691-L695) 与 [README.md:136-140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L136-L140)，注意「实验性」「至多是轻微提速」。
3. **可选调参实验（需 GPU + 模型）**：固定 prompt，分别用 `--mtp-margin 0`（关掉门控）、`--mtp-margin 3`（默认）、`--mtp-margin 50`（几乎总是跳过）各跑一次，用 `DS4_MTP_CONF_LOG=1` 统计平均 `committed`，并对比墙上时间。
   - **预期现象**：margin=0 时 `committed` 平均更高但每次都要验证+可能回滚；margin 过大时投机几乎退化为普通 decode。
   - **若无可运行硬件**，标注「待本地验证」，并基于源码写出你对三种取值下行为差异的**预测**。

#### 4.3.5 小练习与答案

**练习 1**：把 `--mtp-margin` 设成 `0` 会怎样？设成一个很大的值（比如 1000）又会怎样？

> **参考答案**：设成 0 等于关闭门控（`mtp_margin_threshold > 0.0f` 不成立），每次 draft_n==2 都走批量验证，接受率高时收益最大，但草案不准时会频繁 partial accept 而白跑。设成 1000 则 margin 几乎永远达不到，门控几乎总是跳过 drafts[1]，投机退化为「只白拿 drafts[0]」≈ 普通 decode，失去加速意义。

**练习 2**：为什么 `--quality` 模式下 margin 门控不生效？

> **参考答案**：`--quality`（或 `DS4_MTP_STRICT`）要求与官方实现逐位对齐，故走精确验证器 `metal_graph_verify_decode2_exact`（见 [ds4.c:27346-27347](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27346-L27347)）。门控的跳过分支会改写提交路径，可能扰动近乎平局的贪婪选择，因此在严格模式下被禁用。注释也明说精确验证器「不是速度收益」。

**练习 3**：MTP 的 draft 状态为什么不随磁盘 KV 持久化？

> **参考答案**：见 [README.md:1122-1127](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1122-L1127)。磁盘 KV 存的是主模型的 checkpoint tokens + logits + 逐层 KV，是「权威输出流」的快照；MTP 的 draft logits/隐状态只是「投机未来」的临时猜测，不属于权威状态。加载快照后，下一次 decode 会重新搭便车算出新的 draft（见 4.1.2 步骤 2），所以无需持久化、也无需在序列化格式里为它预留位置。

## 5. 综合实践

> **任务**：在源码层面把一次完整的 MTP 投机周期「画成时序图」，并据此解释 ds4 为什么把 MTP 标为「实验性、至多轻微提速」。

请完成以下步骤（纯阅读，任意机器即可；带 ★ 的为可选运行）：

1. **建立时序**。以 `ds4_session_eval_speculative_argmax`（[ds4.c:27167](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27167)）为中心，画出一次调用的时序，至少包含这些动作及其调用者：
   - `ds4_session_eval(first_token)`（提交第一个 token，顺带 probe 出 `mtp_draft_token`）
   - `sample_argmax(s->logits)` 免费验证 drafts[0]
   - `metal_graph_eval_mtp_draft_from_hc` 递归起草 drafts[1]
   - `logits_top2` 计算 margin（若 draft_n==2）
   - `metal_graph_verify_suffix_tops` 批量验证（或 margin 不足时跳过）
   - `spec_frontier_snapshot` / `spec_frontier_restore` 回滚
2. **标注每个动作的「成本」与「产出」**：哪个动作是免费的（搭便车），哪个是投机多付出的？
3. **两种结局**：分别画出「全接受（commit_drafts == draft_n）」和「部分接受（commit_drafts == 1）」两条路径在时序图上的分歧点。
4. **★ 可选运行验证**：用 `DS4_MTP_CONF_LOG=1 ./ds4 --mtp <...> --mtp-draft 2 --temp 0 -p "..."` 收集若干次 `drafted/committed/margin`，统计接受率，验证你时序图里对「成本/产出」的判断。
5. **结论**：基于上述分析，用 3–5 句话解释 README 为什么把 MTP 描述为「correctness-gated」「at most a slight speedup」——重点说清「加速依赖接受率，而接受率不可控，门控只能减少白跑、不能保证收益」。

> 预期产出：一张时序图 + 一段「为什么 MTP 是实验性」的论证。若未运行第 4 步，请在结论里标注「接受率数据待本地验证」。

## 6. 本讲小结

- **MTP = 草案器，不是采样器**：DeepSeek V4 的 `mtp.0` 块被加载成独立的轻量 GGUF，只负责「猜」下一个 token；主模型永远是输出流的唯一权威。
- **第一个草案免费**：主模型每次 decode 真实 token 时搭便车算出 `mtp_draft_token`，所以投机入口的 `drafts[0]` 几乎不计额外成本，且可用主模型刚算出的 logit 免费验证。
- **接受规则很简单**：贪婪设定下，草案 token 被接受当且仅当主模型在该位置的 argmax 与之相等；连续匹配构成接受前缀，第一个失配点之后全部丢弃。
- **部分接受必须回滚**：`spec_frontier_snapshot`/`restore` 保存并恢复每层压缩/indexer KV 前沿，防止「错误的投机未来」污染主模型状态。
- **`--mtp-margin` 是速度旋钮，不是正确性旋钮**：它用草案 logit 的 top0−top1 边际预测「这次会不会白跑」，过低就跳过 drafts[1]；`--quality`/`DS4_MTP_STRICT` 下禁用，改走精确验证器以复现官方向量。
- **实验性 + 不持久化**：MTP 只在「单机 GPU + 贪婪 + 非分布式 + 非 SSD 流式」下可用，提速依赖不可控的接受率；draft 状态不属于权威 KV，不写进磁盘快照，加载后由下次 decode 重建。

## 7. 下一步学习建议

- **回到正确性基线**：重读 u4-l3 的采样与 argmax，确认你理解为什么 MTP 只挂在 `temperature <= 0` 的贪婪分支。
- **理解回滚的代价**：本讲的 `spec_frontier_snapshot` 拷贝的是 u4-l2 讲过的「压缩 KV 前沿」与 indexer 行。结合 u4-l2 思考：为什么回滚压缩前沿比回滚 raw 滑动窗口贵得多？（提示：raw 是定长环形，压缩前沿要逐层拷贝整块张量。）
- **看 GPU 内核如何承载验证**：`metal_graph_verify_suffix_tops` 的 `metal_graph_encode_layer_batch` 是 u5-l2 讲的 layer-major 图在「多 token batch」上的复用——这正是投机能把 GPU 算满的关键，建议结合 u5-l2 阅读。
- **分布式为何禁用 MTP**：u9 系列会讲 coordinator/worker 的流水线，你会看到跨机延迟使「短后缀批量验证」毫无收益，这就是 [ds4.c:25683](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25683) 在分布式下直接跳过 MTP 的根因。
- **动手前读官方说明**：运行前务必读 [README.md:136-140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L136-L140) 与 [download_model.sh](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh) 的 `mtp` 目标说明，确认你的模型档位（q2-imatrix / q2-q4-imatrix / q4-imatrix）与 MTP GGUF 兼容。
