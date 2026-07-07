# 会话 payload 序列化（DSV4）

## 1. 本讲目标

本讲钻进 `.kv` 缓存文件的「核心内馅」——**DS4 session payload**。

学完后你应该能够：

1. 画出 DSV4 payload 的字节布局：13 个小端 `u32` 头部、checkpoint token 序列、logits、逐层 KV 张量。
2. 说清楚为什么 **logits 必须紧随 checkpoint tokens 保存**，以及这一设计如何让「加载快照」省掉一次额外的 decode。
3. 区分 raw 滑动窗口 KV、compressed KV、ratio-4 indexer KV 三类逐层张量在序列化里的差异，以及**压缩前沿（compressor frontier）**为何必须一起写盘。
4. 读懂 `ds4_session_save_payload` / `ds4_session_load_payload` 的防御性写法（先建临时 checkpoint、全部成功才提交、末尾 `remaining==0` 校验）。

---

## 2. 前置知识

本讲假设你已经读过 u4-l2（KV 缓存设计）与 u8-l1（磁盘 KV 缓存文件格式）。这里只做一句话回顾，不重复展开：

- **KVC 文件**是一个「三明治」：48 字节固定头 + 渲染文本 + **DS4 payload** + 可选 tool-id map（u8-l1）。本讲只讲中间那层「DS4 payload」内部到底装了什么。
- 一条 `ds4_session` 的状态由三类逐层 KV 构成（u4-l2）：固定容量的 **raw 滑动窗口**（存最近 token 的精确 KV）、只增不删的 **compressed KV**（compressor 把每 `ratio` 个 token 压成 1 行）、以及 ratio-4 层独有的 **indexer KV**（给压缩行打分选 top-k）。其中 compressor 还有一个「正在攒但尚未吐行」的半窗口，叫 **compressor frontier**。
- **checkpoint** 是 session 当前 KV 所对应的精确 token 序列；`checkpoint_valid` 标记它是否可信（u2-l3）。

几个本讲会用到的术语：

- **payload**：引擎拥有的、与图状态强绑定的序列化字节流。服务器/agent 只拥有外层 KVC 头部与策略，**payload 的内部格式由引擎（`ds4.c`）独占**——这一点写在头注释里（见下文源码精读）。
- **live row count**：实际「有数据」的行数，区别于「容量 capacity」。payload 的大小按 live 行数算，不按容量算，这是磁盘缓存能随上下文增长而非性能爆炸的关键。
- **小端（little-endian）**：整个 payload 多字节整数一律低字节在前。

---

## 3. 本讲源码地图

本讲涉及的关键文件与函数：

| 文件 | 作用 |
|------|------|
| `ds4.h` | 声明 payload 的 magic / version / u32 字段数常量，以及 `ds4_session_save_payload` / `ds4_session_load_payload` 等公共接口。 |
| `ds4.c` | payload 的真正实现：字节读写原语、字节计数、save / load 主体、CPU 与 GPU 两条分支。 |
| `README.md` | 给出 payload 的「人话」字节布局说明，是理解代码的权威对照表。 |

核心代码点（行号均对应当前 HEAD）：

- 常量定义：[ds4.h:L302-L309](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L302-L309)（含 `DSV4` magic、version、字段数）。
- 公共接口声明：[ds4.h:L311-L318](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L311-L318)。
- 字节读写原语：[ds4.c:L23306-L23379](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23306-L23379)。
- frontier 字节计算：[ds4.c:L23402-L23410](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23402-L23410)。
- 字节计数 `ds4_session_payload_bytes`：[ds4.c:L24165-L24189](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24165-L24189)。
- save 主体 `ds4_session_save_payload`：[ds4.c:L24273-L24480](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24273-L24480)。
- load 主体 `ds4_session_load_payload`：[ds4.c:L24482-L24847](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24482-L24847)。
- README 人话布局：[README.md:L1091-L1127](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1091-L1127)。

---

## 4. 核心概念与源码讲解

### 4.1 DSV4 头部：13 个 u32 字段

#### 4.1.1 概念说明

payload 的第一段是**自描述头部**：13 个小端 `u32`，共 52 字节。它的作用是「在读任何张量之前，先确认这个文件确实是我们写的、且是为当前模型布局写的」。

头部承担三类信息：

1. **身份校验**：magic + version，挡掉非 payload 文件和旧版本文件。
2. **图布局描述**：层数、head 维度、indexer head 维度、词表大小——这些必须和当前编译进 `ds4.c` 的模型常量逐位相等，否则拒绝。
3. **运行时容量与计数**：上下文大小、prefill chunk、raw ring 容量、raw 窗口长度、压缩容量、checkpoint token 数、实际写盘的 raw 行数。

注意第 4.1 节里字段 3（prefill chunk）和字段 4/5（raw 容量/窗口）的区别，它直接影响「旧快照能否被新运行时加载」，下文细讲。

#### 4.1.2 核心流程

头部读写的总流程是：

```
定义 13 个 u32  ->  逐个 payload_write_u32（小端）  ->  后续段落继续追加
读取时：逐个 payload_read_u32  ->  校验 magic/version  ->  校验图布局  ->  继续读后续段落
```

13 个字段的官方语义（与代码一一对应）见 README：

| 序号 | 字段 | 语义 |
|------|------|------|
| 0 | magic | `"DSV4"`，即 `0x34565344` |
| 1 | payload version | `2` |
| 2 | saved context size | 写盘时的上下文大小 |
| 3 | prefill chunk size | 写盘时的 prefill chunk |
| 4 | raw KV ring capacity | raw 环形缓冲容量 |
| 5 | raw sliding-window length | raw 滑动窗口长度 |
| 6 | compressed KV capacity | 压缩 KV 容量 |
| 7 | checkpoint token count | checkpoint 的 token 数 |
| 8 | layer count | 层数（`DS4_N_LAYER`） |
| 9 | raw/head KV dimension | head 维度（`DS4_N_HEAD_DIM`） |
| 10 | indexer head dimension | indexer head 维度（`DS4_N_INDEXER_HEAD_DIM`） |
| 11 | vocabulary size | 词表大小（`DS4_N_VOCAB`） |
| 12 | live raw rows serialized below | 实际写盘的 raw 行数 |

#### 4.1.3 源码精读

magic / version / 字段数三个常量集中在 `ds4.h`：

[ds4.h:L304-L306](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L304-L306) 定义 magic `0x34565344`（注释明示「DSV4」）、version `2`、字段数 `13`。它旁边还有一组 `DSVL` 常量（layer payload），那是分布式场景的「单层 payload」，不在本讲范围。

> 一个小细节：`0x34565344` 写成小端字节是 `0x44 0x53 0x56 0x34`，正好是 ASCII `'D' 'S' 'V' '4'`。所以用十六进制编辑器打开 payload，开头四个字节肉眼就能读出 `DSV4`。

写头部的代码（GPU 分支为例，CPU 分支结构相同）：

[ds4.c:L24357-L24380](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24357-L24380)：先把 13 个字段填进 `header[13]` 数组（注释里逐字段列了序号含义），再用一个循环逐个 `payload_write_u32`。注意字段 4 和字段 5 在 GPU 分支分别是 `g->raw_cap` 和 `g->raw_window`，而在 CPU 分支（[ds4.c:L24282-L24299](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24282-L24299)）两者都被填成 `ds4_default_raw_cap(ctx_size)`——因为 CPU 后端的 raw 窗口就等于默认容量，没有独立的「窗口」概念。

读头部时只做两道硬校验，位于 load 函数最开头：

[ds4.c:L24490-L24498](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24490-L24498)：读 13 个 u32，再判 `h[0]` 是不是 magic、`h[1]` 是不是 version，任何一项不符直接返回 "unsupported session payload version"。其余字段（context / layout）的校验在 CPU 分支与 GPU 分支里分别做。

图布局校验尤其值得注意：

[ds4.c:L24513-L24525](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24513-L24525)（CPU）/ [ds4.c:L24652-L24664](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24652-L24664)（GPU）。其中：

- 字段 8/9/10/11（层数 / head 维度 / indexer head 维度 / 词表）必须**逐位等于**编译进来的 `DS4_N_LAYER` 等常量，否则报 "written for a different DS4 layout"。这正是 README 说的「payload 只在兼容的 `ds4.c` 构建间可移植」。
- 字段 5（raw_window）必须等于当前运行时的 raw 窗口，否则报 "graph chunk layout does not match current runtime"。
- **但字段 3（prefill_cap）被显式忽略**——见注释 [ds4.c:L24519-L24521](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24519-L24521)：

> `prefill_cap is scratch scheduling capacity, not durable KV layout. Old checkpoints remain valid as long as the raw KV window matches.`

这呼应了 u6-l1 的结论「chunk 是路径旋钮而非性能旋钮」：改变 prefill chunk 改的是调度，不改 KV 的语义布局，所以旧快照样能加载（只是重放后缀时走不同的 chunk 路径）。

#### 4.1.4 代码实践

**目标**：用十六进制视角验证头部前 8 字节确实是 `DSV4` + version 2。

**步骤**：

1. 如果你有一台能跑 ds4 的机器，启动 server 并开启磁盘 KV：`./ds4-server --kv-disk-dir /tmp/ds4-kv ...`，发一次长 prompt 触发一次 cold save。
2. 在 `/tmp/ds4-kv` 里找到任一 `.kv` 文件。
3. 用 `xxd` 看 KVC 文件头越过 48 字节固定头 + 4 字节 text_bytes + 渲染文本之后的字节——也就是 payload 起点。命令大致是（待本地验证具体偏移）：

   ```sh
   # 48 (KVC头) + 4 (text_bytes) + N (渲染文本) 之后才是 payload
   xxd -s $((48 + 4 + <text_bytes>)) -l 16 <文件>.kv
   ```

**预期现象**：开头 4 字节是 `44 53 56 34`（`DSV4`），接下来 4 字节是 `02 00 00 00`（version=2，小端）。

**说明**：如果没有 GPU 机器，这一步属于「待本地验证」。你也可以直接读 [README.md:L1093-L1107](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1093-L1107) 的字段表，把它与 [ds4.c:L24357-L24377](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24357-L24377) 的 `header[]` 初始化顺序逐行对照，确认 README 的「人话字段表」和代码的「数组初始化」完全一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么头部同时存了「raw ring capacity（字段 4）」和「raw sliding-window length（字段 5）」两个看似重复的值？它们何时不同？

参考答案：在 GPU 后端，raw 缓冲是一个为 ubatch 调度的**环形缓冲（ring）**，其物理容量 `raw_cap` 可以大于滑动窗口逻辑长度 `raw_window`；只有最后 `raw_window` 行（逻辑上）是需要持久化的「活」行。CPU 后端没有这个区分，故两者被填成同一个值。保存时只用 `raw_window` 决定写多少行，但两个值都要写进去供加载侧做一致性校验。

**练习 2**：假设有人改了 `DS4_N_HEAD_DIM`（模型 head 维度）后重新编译 ds4，旧的 `.kv` 文件还能加载吗？为什么？

参考答案：不能。load 会比较头部字段 9 与当前 `DS4_N_HEAD_DIM`，不等即返回 "written for a different DS4 layout"。头部里的 layout 字段（8/9/10/11）就是为这种「换模型布局」场景设计的硬闸门。

---

### 4.2 tokens 与 logits：跳过一次 decode

#### 4.2.1 概念说明

头部之后紧跟两段：

1. **checkpoint token 序列**：`u32[token_count]`，即 KV 当前对应的精确 token id 列表。
2. **logits**：`float32[vocab_size]`，即「在这个 checkpoint 之后、采样下一个 token 所需的原始 logits」。

第二段是本讲最关键的设计。直觉上，KV 缓存只描述「注意力历史」，为什么要把 logits 也写盘？

因为 **session 的「下一步可采样」状态 = KV 缓存 + 当前位置的 logits**。如果只存 KV，加载后你必须立刻跑一次前向（一次 decode）才能拿到下一个 token 的 logits；而存了 logits，加载后可以**直接采样或直接取 argmax，零额外前向**。对于「冷启动恢复一条长对话」的场景，省掉这一次 decode 不只是省时间，更避免了在恢复路径上引入一次与本应「逐位复现」的官方向量不一致的前向。

#### 4.2.2 核心流程

save 侧（CPU 与 GPU 完全一致）：

```
写头部(13 u32)
  ->  逐个写 checkpoint token（u32）
  ->  一次性写 logits（vocab * sizeof(float) 字节）
  ->  写逐层行计数（见 4.3）
  ->  写逐层张量（见 4.3）
```

load 侧对称：读 token 进一个**临时** `token_vec new_checkpoint`，读 logits 直接覆盖 `s->logits`，**只有全部成功才提交**。

为什么 logits「正好」紧随 tokens？因为它在数学上就是「checkpoint 的最后那个 token 经过整条前向后的产物」，逻辑上属于 checkpoint 的一部分，放在一起最自然，也方便读者一眼看出「这段字节 = 这条 checkpoint 的完整可采样状态」。

#### 4.2.3 源码精读

save 中写 token + logits 的两行（GPU 分支）：

[ds4.c:L24381-L24384](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24381-L24384)：先循环写每个 token id（强转 `uint32_t`），再 `payload_write_bytes` 一次性写 `DS4_N_VOCAB * sizeof(float)` 字节的 logits。CPU 分支见 [ds4.c:L24303-L24306](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24303-L24306)，完全同构。

README 对这一段的解释最为直接：

[README.md:L1122-L1127](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1122-L1127)：logits 是宿主 `ds4_session` 缓冲里的原始 IEEE-754 `float32`，紧跟 checkpoint tokens 保存，**目的是让加载后的快照能直接从精确的下一 token 分布采样或续写，而不必多跑一次 decode**。同段还点明：MTP draft 的 logits / 状态**不**持久化，加载磁盘快照后 draft 状态被作废，由正常生成重建（呼应 u6-l2「draft 状态不属权威 KV」）。

load 侧的防御性写法值得细看（CPU 分支）：

[ds4.c:L24538-L24552](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24538-L24552)：token 读进**全新的** `token_vec new_checkpoint`（初始化为 `{0}`），logits 读进 `s->logits`。注意此时 `s->checkpoint` 还是旧值，没动。

直到整段 payload（包括下面 4.3 的逐层张量）全部读完且 `remaining==0`，才在 [ds4.c:L24630-L24634](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24630-L24634) 真正提交：

```c
token_vec_free(&s->checkpoint);
s->checkpoint = new_checkpoint;
s->checkpoint_valid = true;
s->mtp_draft_valid = false;
```

也就是说，**任何一步失败都会 `token_vec_free(&new_checkpoint)` 后返回 1，原 session 状态毫发无损**。这是一种「先影子构建、成功才切换」的失败安全模式，也是为什么 load 路径里到处是 `token_vec_free(&new_checkpoint); return 1;`。

#### 4.2.4 代码实践

**目标**：从源码出发，论证「加载 payload 后可直接采样，无需额外 decode」。

**步骤**：

1. 打开 [ds4.c:L24499-L24634](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24499-L24634)（CPU load 分支）。
2. 追踪加载完成后 session 的状态：`checkpoint_valid = true`，`s->logits` 已被 `payload_read_bytes` 填好（[ds4.c:L24547](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24547)）。
3. 对照 u4-l3 的生成循环 `sample → eval`：采样器 `ds4_sample_logits` 只读 `s->logits`，**不碰 KV、不前向**。所以加载后调用一次采样即可拿到下一个 token，全程零前向。

**预期结果**：你能用三句话讲清这条链：`load_payload` 写好 `s->logits` → `checkpoint_valid=true` → 采样器只读 logits → 拿到 next token，期间没有 `ds4_session_eval`。这正是「免去一次额外 decode」的字面含义。

**说明**：若想实证，可在 server 加载命中磁盘 KV 后，用 `--trace` 观察日志里是否出现一次「为首个 token 而做的 decode」——理论上不应出现（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果 payload 不存 logits，只存 KV，加载后要得到下一个 token 需要额外做什么？

参考答案：需要用 checkpoint 最后一个 token 跑一次完整的 `ds4_session_eval`（一次 decode：过 43 层前向 + output head），才能重新算出 `s->logits`，然后才能采样。这次 decode 在长上下文下并不便宜，且会把一条「纯恢复」路径混入一次前向，增加与官方向量对齐的复杂度。

**练习 2**：logits 在文件里占多少字节？（以 Flash 为例，词表约 128k）

参考答案：`DS4_N_VOCAB * sizeof(float)`。若词表约 128000，则约 \(128000 \times 4 = 512000\) 字节，约 500 KiB。相比几十到几百 MB 的逐层 KV，logits 占比很小，性价比极高。

---

### 4.3 逐层 KV 与 frontier：raw / compressed / indexer

#### 4.3.1 概念说明

token + logits 之后，是 payload 的「大头」：**逐层 KV 张量**。这里把 u4-l2 讲过的三类逐层缓存与一个「半成品状态」映射到字节流上：

- **raw 滑动窗口 KV**：每层都有，存最近若干 token 的精确 KV。**只有最后 `raw_live` 行需要写盘**，因为更老的行已经被滑出窗口、不再被注意。
- **compressed KV**：ratio≠0 的层有，只增不删。要写「活的」前 `n_comp` 行。
- **compressor frontier**：compressor 正在攒的「半窗口」——它还没攒够 `ratio` 个 token、还没吐成一个完整压缩行，但里面的累加状态决定了下一个压缩行长什么样。**必须随行计数器一起序列化，否则恢复后压缩行会对不上**。这就是 u4-l2 反复强调的「frontier 不可廉价回退」在序列化层面的体现：它被当成和压缩行同等权威的状态一起写盘。
- **indexer KV**：仅 ratio-4 层有，结构与 compressed 类似但维度是 indexer head dim，也带自己的 frontier。

每层的写盘内容取决于它的 `compress_ratio`（Flash 第 0/1 层为 0，之后偶数 4 奇数 128，见 u4-l2），ratio=0 的层只写 raw、跳过其余。

#### 4.3.2 核心流程

写盘顺序（每层 `il`）：

```
if ratio == 0:
    只写 raw_live 行
else:
    写 raw_live 行
    写 n_comp 行 compressed KV
    写 attn_state_kv（frontier）
    写 attn_state_score（frontier）
    if ratio == 4:
        写 n_index_comp 行 indexer KV
        写 index_state_kv（frontier）
        写 index_state_score（frontier）
```

注意三个工程细节：

1. **行计数先于张量集中写出**：所有层的 `n_comp` 先一起写（`DS4_N_LAYER` 个 u32），再所有层的 `n_index_comp` 一起写，**然后**才开始写张量字节。这样读侧可以先把「每层有多少行」全部读到内存，再按计划读张量。
2. **raw 行按逻辑位置顺序写**，不按物理 ring 顺序写（见下）。
3. **GPU 路径额外有 f16↔f32 转换**：compressed KV 在 GPU 上可能以 f16 存储（`DS4_GPU_ATTN_COMP_CACHE_F16`），写盘时统一升精度为 f32，读盘时再降回 f16，使磁盘格式与后端无关。

#### 4.3.3 源码精读

先看「行计数集中写出」的写法（GPU 分支）：

[ds4.c:L24385-L24390](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24385-L24390)：先一个循环写完所有层的 `layer_n_comp[il]`，再一个循环写完所有层的 `layer_n_index_comp[il]`。CPU 分支对应 [ds4.c:L24307-L24312](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24307-L24312)，写的是 `cpu_cache.layer[il].n_comp / n_index_comp`。

逐层张量的写盘（GPU 分支）核心循环：

[ds4.c:L24394-L24476](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24394-L24476)。要点逐段看：

- **raw 行按逻辑顺序写**：[ds4.c:L24395-L24409](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24395-L24409)。注释直接说 `The file does not care where the rows happened to live physically in the source graph`——物理 ring 里第 `pos` 行落在 `pos % raw_cap`，但写盘时按逻辑 `pos` 顺序逐行用 `payload_write_tensor_span` 抽出，使磁盘上是连续的逻辑行。
- **compressed 行从行 0 起连续写**：[ds4.c:L24412-L24433](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24412-L24433)。注释点明 `Compressed rows are append-only from row zero, so the live prefix is contiguous`，所以直接从偏移 0 写 `n_comp` 行；f16 后端走 `payload_write_tensor_span_f16_as_f32` 升精度。
- **frontier 两张量**：[ds4.c:L24434-L24449](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24434-L24449)，写 `attn_state_kv` 和 `attn_state_score`，长度由 `layer_attn_state_bytes(ratio)` 决定。
- **ratio-4 的 indexer 段**：[ds4.c:L24450-L24475](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24450-L24475)，结构与 compressed 对称，只是维度换成 `DS4_N_INDEXER_HEAD_DIM`，长度用 `layer_index_state_bytes(ratio)`。

frontier 字节数的计算：

[ds4.c:L23402-L23410](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23402-L23410)。对 ratio=4，`coff=2`；对 ratio=128，`coff=1`。frontier 字节数为

\[
\text{bytes} = \text{coff} \times \text{DS4\_N\_HEAD\_DIM} \times \text{coff} \times \text{ratio} \times \text{sizeof(float)}
\]

直觉上：frontier 要存「`coff` 份、每份 `ratio` 个槽位、每槽 `DS4_N_HEAD_DIM` 维」的累加状态——也就是正在攒的那个半窗口。indexer frontier 把 `DS4_N_HEAD_DIM` 换成 `DS4_N_INDEXER_HEAD_DIM`。

CPU 后端的逐层写盘结构相同，只是直接写宿主 `float*` 缓冲（`layer->raw_kv`、`layer->attn_comp_kv` 等），见 [ds4.c:L24313-L24343](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24313-L24343)，并多一道「raw_live 不能超过 n_raw」的断言（[ds4.c:L24315-L24319](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24315-L24319)）。这些字段对应的结构体是 `ds4_layer_cache`：[ds4.c:L8300-L8316](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8300-L8316)，GPU 后端对应的是 `ds4_gpu_graph` 里那组 `layer_*` 张量数组：[ds4.c:L10336-L10342](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10336-L10342)。

「只存活行、不存容量」的字节计数体现在 `session_payload_live_tensor_bytes`，它的注释把设计意图说得最清楚：

[ds4.c:L23425-L23428](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23425-L23428)：

> `This is deliberately based on live row counts rather than capacities so the disk cache scales with saved tokens, not with the maximum context size used to allocate the graph.`

也就是说，就算你用 1M 上下文分配了巨型图，只要这条会话只生成了 2000 个 token，payload 也只占 2000 token 对应的 KV，而不是 1M token 的容量。这是磁盘 KV 能在「大 ctx、小会话」场景下不爆炸的根本原因。`ds4_session_payload_bytes`（[ds4.c:L24165-L24189](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24165-L24189)）就是把「头部 + tokens + logits + 两个行计数数组 + 活张量」加总，供外层在写盘前做预算/淘汰判断（u8-l2）。

读侧的逐层恢复（GPU 分支）把「逻辑行」重新摆回「物理 ring」：

[ds4.c:L24731-L24748](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24731-L24748)。注释点明：`Rebuild the physical raw ring expected by the current graph. This is why the file stores rows in logical order instead of dumping bytes from the old ring layout.`——读第 `pos` 行时写回物理位置 `pos % raw_cap`。compressed 与 frontier 的恢复用同样的「偏移 0 起、长度由 n_comp / ratio 决定」的方式，见 [ds4.c:L24749-L24818](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24749-L24818)。

最后两道收尾校验保证「不多不少」：

- [ds4.c:L24625-L24629](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24625-L24629) / [ds4.c:L24825-L24829](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24825-L24829)：`remaining != 0` 则报 "trailing payload bytes"，拒绝。结合外层传入的 `payload_bytes`（来自 KVC 头第 40 字节，u8-l1），整个读过程被严格框定在那段字节内，多一个字节都算损坏。
- GPU 分支还在读张量前后各做一次 `ds4_gpu_synchronize()`（[ds4.c:L24720-L24724](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24720-L24724) 与 [ds4.c:L24830-L24834](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24830-L24834)），确保设备状态在恢复前后都稳定。

#### 4.3.4 代码实践

**目标**：亲手算出一个 session 的 payload 字节数，验证「随 token 数缩放、不随容量爆炸」。

**步骤**：

1. 读 `ds4_session_payload_bytes`：[ds4.c:L24165-L24189](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24165-L24189)。
2. 设想两个 session：A 用 1M 上下文但只跑了 2048 token；B 用 32k 上下文跑了 2048 token。
3. 注意公式里**没有**任何「× ctx_size」的容量项——raw 行数取 `raw_live`（受 `raw_window` 与 `checkpoint_len` 的最小值约束，见 [ds4.c:L23418-L23423](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23418-L23423)），compressed 行数取 `layer_n_comp[il]`（≈ `checkpoint_len / ratio`），都与「分配了多大 ctx」无关。

**预期结果**：A 与 B 的 payload 字节数几乎相同（仅 raw_window 上限略有差异）。这印证了「磁盘缓存随已保存 token 缩放」。

**说明**：这一步是「源码阅读型实践」，无需运行。可结合 u8-l2 的淘汰评分公式（`score = (衰减命中+1)·tokens/file_size`）理解：因为 `file_size` 随 `tokens` 增长，文件不会因为「恰好在超大 ctx 进程里生成」就被不公正地惩罚。

#### 4.3.5 小练习与答案

**练习 1**：为什么 raw 缓存只写最后 `raw_live` 行，而 compressed 缓存要写从行 0 起的全部 `n_comp` 行？

参考答案：raw 是滑动窗口，滑出窗口的旧行**不再被任何注意读取**，写了也没用，所以只留窗口内的活行。compressed 是只增不删的长期记忆，稀疏注意可以在前缀里**任选**行打分，所以从行 0 起的全部活行都必须保留。这正是 u4-l2「raw 管近期、compressed 管长期」在序列化上的体现。

**练习 2**：frontier（`attn_state_kv` / `attn_state_score`）如果不存会怎样？

参考答案：compressor 正在攒的半窗口累加状态会丢失，恢复后「下一个压缩行」会从一个错误的起点开始累积，导致后续所有压缩行都偏离正确值，长上下文注意力全错。所以 frontier 被当成和压缩行同等权威的状态一起写盘——这是「frontier 不可廉价回退」在持久化层面的强制要求。

**练习 3**：raw 行为什么按「逻辑位置顺序」写，而不是直接把物理 ring 的字节倒出来？

参考答案：因为写盘时的源图与读盘时的目标图，其物理 ring 容量 `raw_cap` 可能不同（比如换了一台机器、或重新分配了图）。按逻辑顺序写「最后 raw_live 行」，读侧再按 `pos % raw_cap` 摆回当前 ring，就能让快照在不同物理布局间可移植；直接倒字节则会把源 ring 的物理布局焊死进文件。

---

## 5. 综合实践

把三个最小模块串起来，做一次「payload 全程跟踪」。

**任务**：给定一次「server 冷启动 + 命中磁盘 KV 恢复」的场景，画出 payload 从**生成**到**消费**的完整链路，并标注每一段对应的源码位置。

**建议步骤**：

1. **生成（写盘侧）**：从 `ds4_kvstore` 触发保存开始（u8-l2 讲过的 cold/continued/evict/shutdown 四时机）。它先调 `ds4_session_stage_payload`（[ds4.c:L24219-L24271](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24219-L24271)）把 payload 写到一个 `/tmp` 临时文件并量出精确字节数 `payload_bytes`——这一步让外层能在**真正写 KVC 文件之前**就拿到字节数做预算/淘汰判断（见 u8-l2）。然后 `ds4_kvstore` 把 KVC 头（含 `payload_bytes`）+ 渲染文本 + 拷贝 staged payload + 可选 KTM 拼成最终 `.kv` 文件（见 `ds4_kvstore.c` 的 save 主流程，本讲不展开）。
2. **payload 内部**：对照本讲 4.1/4.2/4.3，标注 staged payload 里 13 个头字段、tokens、logits、两个行计数数组、逐层 raw/compressed/indexer + frontier 的先后顺序。
3. **消费（读盘侧）**：下次同一会话进来，`ds4_kvstore` 用渲染文本前缀命中文件后，调 `ds4_session_load_payload(session, fp, hdr.payload_bytes, ...)`（见 `ds4_kvstore.c` 的 load 主流程）。注意它传入的 `payload_bytes` 来自 KVC 头第 40 字节——load 函数用这个值做 `remaining` 配额，读完必须 `remaining==0`。
4. **恢复后状态**：标注加载完成时 session 的状态：`checkpoint_valid=true`、`s->logits` 已就绪、`mtp_draft_valid=false`（draft 要靠后续生成重建）。然后说明「紧接着的一次采样不需要 decode」。

**交付物**：一张时序图或表格，左列是「字节段 / 状态」，右列是「源码位置 + 一句话说明」。重点回答两个问题：

- (a) logits 为什么紧跟 tokens？（答：让加载后零额外 decode 即可采样；见 4.2）
- (b) 为什么 payload 字节数能在外层写盘前就知道？（答：staging 先写临时文件量出 `payload_bytes`，外层据此做预算/淘汰，再正式落盘；见综合实践步骤 1）

如果你有可运行环境，可额外用 `strace` 观察 server 命中 KV 时是否有 `read` 紧跟一次「首 token 前向」的迹象；理论上命中后首个 token 不应触发完整 decode（待本地验证）。

---

## 6. 本讲小结

- DSV4 payload = **13 个小端 u32 头部 + token 序列 + logits + 两个行计数数组 + 逐层 raw/compressed/indexer KV 与 frontier**，全部小端，整个 payload 由引擎（`ds4.c`）独占格式，外层 KVC 只负责头与策略。
- 头部 13 字段承担身份校验（magic `DSV4` / version 2）、图布局校验（层/head/indexer/vocab 必须逐位匹配）与运行时容量描述；其中 **prefill_cap 被显式忽略**（它只是调度容量，不是持久布局），raw_window 则必须匹配。
- **logits 紧随 checkpoint tokens**，使加载后的快照能直接从精确的下一 token 分布采样，**省掉一次额外 decode**；MTP draft 状态不持久化，加载后作废重建。
- 逐层张量遵循「raw 只存最后窗口内的活行（按逻辑顺序）、compressed/indexer 从行 0 起存全部活行、frontier 必须随行计数一起写」；raw 按逻辑序写是为了在不同物理 ring 间可移植。
- payload 字节数**按 live 行数算、不按容量算**，所以磁盘缓存随已保存 token 缩放；load 采用「先影子构建、成功才提交、末尾 remaining==0」的失败安全模式，任何一步失败都不污染原 session。
- 读侧的 `remaining` 配额由外层 KVC 头的 `payload_bytes` 传入，把 payload 严格框定在固定字节范围内，多一个字节即判损坏。

---

## 7. 下一步学习建议

- **向上**：读 u8-l2（KV store 策略与淘汰），看 `payload_bytes` 如何参与「文件大小 vs 预算」判断与淘汰评分，以及 cold save 为什么要把 checkpoint 裁 32 尾 token 再对齐到 2048——你会发现裁剪/对齐后的 token 数正是本讲 `checkpoint.len` 的来源。
- **向旁**：读 u7-l5（实时 KV 前缀复用与检查点改写），看「命中磁盘快照」之后如何只对文本后缀重新分词——那一步依赖本讲 payload 里存的「精确 checkpoint tokens」作为权威身份。
- **向深（分布式）**：读 `ds4_distributed.c` 里的 `ds4_dist_session_save_payload` / `ds4_dist_session_load_payload`（[ds4.c:L24278-L24280](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24278-L24280) 的转发点），看协调者如何把多个 worker 的层张量拉回来合并进同一条 DSV4 流、加载时再按当前 route 切回去（README [L1129-L1133](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1129-L1133)）。这是 u9 系列讲义的前置。
- **动手**：挑一个 `ds4_session_save_payload` 的失败分支（如 `remaining != 0`），思考如果删掉那道校验会引入什么静默 bug——这是理解「为什么 payload 读路径写得这么啰嗦」的最佳方式。
