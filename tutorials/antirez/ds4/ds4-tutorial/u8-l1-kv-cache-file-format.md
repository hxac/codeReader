# 磁盘 KV 缓存文件格式

## 1. 本讲目标

本讲专门拆解 `ds4-server`（以及 `ds4-agent`）写入磁盘的 **KVC 缓存文件** 的字节布局。读完本讲，你应该能够：

1. 说出一个 `.kv` 文件由哪几段拼成，并按字节画出 KVC 固定头（48 字节）的每一字段。
2. 解释「渲染文本（rendered text）」为什么既是文件名、又是查找身份，以及它和文件内 DS4 payload 的分工。
3. 说清楚可选的 **KTM（tool-id map）** 段在什么条件下出现、由谁写入、加载时如何被消费。
4. 把 README 里的字节布局图与 `ds4_kvstore.h` 的宏、`ds4_kvstore.c` 的 `fill_header` / `read_header` / `store` 函数逐字段对上号。

本讲只讲**文件格式本身**，不展开 DS4 payload 内部（那是 u8-l3 的 DSV4 序列化）、保存时机与淘汰策略（u8-l2）。payload 在本讲里被视为一段「不透明的 `payload_bytes` 字节」。

## 2. 前置知识

在进入字节布局前，先用三句话回顾几个关键概念（细节见前置讲义）：

- **渲染文本（rendered text）**：把一段 token 序列用分词器**解码回人类可读的 UTF-8 文本**。它不是 token id 序列本身，而是「模型当年看到的那串字节」。详见 u3-l3。
- **DS4 payload / checkpoint**：把一条 `ds4_session` 的完整推理时间线（token 序列 + logits + 逐层 KV 缓存 + 压缩前沿）序列化后的字节块。加载它就能恢复一条「已经 prefill 完」的会话，免去重新 prefill。详见 u8-l3。
- **单活 KV session**：进程内同时只有一条活的 `ds4_session`。当无状态客户端重发整段对话、或服务器重启后，旧 checkpoint 只能靠**磁盘 KVC 文件**续命。详见 u7-l1。
- **为什么用普通 `read`/`write` 而非 `mmap`**：进程已经用 mmap 映射了几十到几百 GB 的模型权重（u3-l1），恢复缓存时不想再叠加 VM 映射，所以 KVC 文件刻意走普通 I/O。

一句话定位：**KVC 文件 = 一份自描述的「前缀快照」**，它用渲染文本当身份证、用 SHA1 当文件名、把可恢复的推理状态作为 payload 紧随其后，再可选地挂上工具回放表。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ds4_kvstore.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h) | 对外头文件：定义固定头大小、扩展位、reason 枚举、`ds4_kvstore_entry` 条目结构、trailer hooks 回调接口，以及一组长函数声明。 |
| [ds4_kvstore.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c) | 实现：magic/version 宏、SHA1、小端读写、`fill_header`/`read_header`、目录扫描、存/取主流程。本讲主要看这一个文件。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | `Disk KV Cache` 一节给出了官方字节布局图与 KTM 段说明，是本讲的对照基准。 |

> 范围提示：`ds4_session_stage_payload` / `ds4_session_write_staged_payload` / `ds4_session_load_payload`（在 `ds4.c`）负责生产与消费 payload 字节，但它们的**内部**布局属于 u8-l3。本讲只把 payload 当作「一段长度已知的字节」。

## 4. 核心概念与源码讲解

### 4.1 KVC 固定头（48 字节固定头）

#### 4.1.1 概念说明

每一个 `.kv` 文件都以一段**定长 48 字节**的固定头开头。它的作用是文件级自描述：

- **谁能读它**：magic `"KVC"` + version 让加载方一眼判断「这确实是个 KVC 文件、且是我认识的版本」。
- **它描述的是哪种快照**：量化档（2 或 4 bit）、模型 id、上下文大小、token 数。
- **它有多大**：payload 字节数（用来知道 payload 段在哪里结束、trailer 从哪里开始）。
- **它被用过多少次**：命中计数与时间戳，供淘汰评分用（u8-l2）。

固定头里**不放**渲染文本本身——文本长度单独用紧跟其后的一个 `u32` 表达，文本字节再跟在后面。这样头本身保持定长、可被 `touch`（只改命中计数/时间戳）原地重写而不碰变长部分。

#### 4.1.2 核心流程

写入一个 KVC 文件的 5 段顺序（`store_live_prefix_text` 内）：

```
[48 字节固定头] [4 字节 text_bytes(u32 LE)] [text_bytes 字节渲染文本]
                                         [payload_bytes 字节 DS4 payload]
                                         [可选 KTM trailer]
```

文件总字节数（`kv_cache_file_size_bytes`）：

\[
\text{file\_bytes} = \underbrace{48 + 4}_{\text{fixed}} + \text{text\_bytes} + \text{payload\_bytes} + \text{trailer\_bytes}
\]

固定头 48 字节内部按**小端（little-endian）**布局如下：

| 偏移 | 大小 | 字段 | 含义 |
|------|------|------|------|
| 0 | u8[3] | magic = `"KVC"` | 文件魔数 |
| 3 | u8 | version = 1 | 格式版本 |
| 4 | u8 | quant_bits | routed 专家量化档（2 或 4） |
| 5 | u8 | reason | 保存原因码（见下） |
| 6 | u8 | ext_flags | 扩展位，bit0 = 追加了 tool-id map |
| 7 | u8 | model_id | 模型 id（Flash=0，向后兼容旧文件该字节恒为 0） |
| 8 | u32 | tokens | 快照覆盖的 token 数 |
| 12 | u32 | hits | 命中计数 |
| 16 | u32 | ctx_size | 写入时的上下文容量 |
| 20 | u8 | payload ABI = 2 | DS4 payload 格式守卫（见 4.1.4 提示） |
| 21 | u8[3] | 保留（0） | 对齐/预留 |
| 24 | u64 | created_at | 创建 Unix 时间戳 |
| 32 | u64 | last_used | 最近使用 Unix 时间戳 |
| 40 | u64 | payload_bytes | 紧随文本之后的 DS4 payload 字节数 |

`reason` 字段的取值由枚举决定：

| 值 | 含义 |
|----|------|
| 0 | unknown |
| 1 | cold（冷存：会话切换/淘汰前抢救） |
| 2 | continued（生成中按间隔增量存） |
| 3 | evict（被淘汰时存） |
| 4 | shutdown（进程关闭时存） |
| 5 | agent-system（agent 系统会话） |
| 6 | agent-session（agent 用户会话） |

#### 4.1.3 源码精读

固定头大小与扩展位定义在头文件里：

[ds4_kvstore.h:11-18](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L11-L18) —— `DS4_KVSTORE_FIXED_HEADER` 定为 48，并定义 4 个扩展位（bit0 就是本讲的 tool-id map）。

[ds4_kvstore.h:20-28](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L20-L28) —— `ds4_kvstore_reason` 枚举，对应上表 7 个原因码。

magic / version / payload ABI 三个常量写在 .c 文件顶端：

[ds4_kvstore.c:25-32](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L25-L32) —— `"KVC"`、version=1、`KV_CACHE_PAYLOAD_ABI=2`。

固定头由 `ds4_kvstore_fill_header` 逐字节填装，这是字段↔偏移的权威映射：

[ds4_kvstore.c:393-415](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L393-L415) —— 先 `memset` 清零 48 字节，再按上表写入；u32 用 `ds4_kvstore_le_put32`、u64 用内部 `kv_le_put64`，全部小端。

读回时由 `ds4_kvstore_read_header` 做镜像解码，并夹带三道校验：

[ds4_kvstore.c:417-440](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L417-L440) —— 校验 magic+version（行 421-422）、校验 payload ABI（行 423）、解码各字段，并在末尾（行 439）拒绝 `tokens==0` 或 `quant_bits` 非 2/4 的文件。注意它在固定头之后**紧接着**再读一个 4 字节 `text_bytes`（行 435-437），把它单独放进出参 `*text_bytes`，并不计入 48 字节头。

小端读写原语本身很简单：

[ds4_kvstore.c:198-213](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L198-L213) —— `kv_le_put64` / `kv_le_get64`，逐字节移位，确保跨平台一致（u32 版本 `ds4_kvstore_le_put32`/`get32` 思路相同）。

#### 4.1.4 代码实践

**目标**：把 `fill_header` 的代码行与上面的字段表逐行对齐，确认偏移无误。

**操作步骤**：

1. 打开 [ds4_kvstore.c:393-415](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L393-L415)。
2. 对照本讲 4.1.2 的字段表，逐行核对：`h[0..3]`、`h[4]`、`h[5]`、`h[6]`、`h[7]`、`h+8`、`h+12`、`h+16`、`h[20]`、`h+24`、`h+32`、`h+40`。
3. 找出 `fill_header` 里**没有**显式赋值、靠 `memset` 留作 0 的字节范围。

**需要观察的现象 / 预期结果**：

- 你会发现 `h[21..23]` 没有任何赋值语句，确实是保留字节。
- 你会发现 `h[20]` 被 `KV_CACHE_PAYLOAD_ABI`（=2）显式写入，而 README 的布局图把它标成 `u8[4] reserved`。**代码为准**：`read_header` 在 [ds4_kvstore.c:423](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L423) 会拒绝 `h[20] != 2` 的文件，所以字节 20 实际是 **payload ABI 守卫**，只有 21-23 才是真正保留。这是一个 README 与代码细微不一致的点，知道它的好处是：将来 payload 格式升级（DSV4 version 3）时，可以靠抬升这个 ABI 号让旧文件自动失效。

> 命令运行说明：本实践是纯源码阅读，不需要模型、不需要运行 ds4，结论可直接从代码读出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tokens`、`hits`、`ctx_size` 用 `u32` 而 `created_at`、`payload_bytes` 用 `u64`？

**参考答案**：token 数、命中数、上下文容量都是「数量级有限、用 u32 足够」的值；而 Unix 时间戳（秒）和 payload 字节数（一个长上下文快照可达数 GB）会超过 u32 上限（约 42 亿），必须用 u64，避免 2038 问题与文件大小截断。

**练习 2**：如果有人手改了一个 `.kv` 文件，把字节 3（version）从 1 改成 9，加载会发生什么？

**参考答案**：`read_header` 在 [ds4_kvstore.c:421-422](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L421-L422) 检查 `h[3] != KV_CACHE_VERSION`，会直接返回 false。该文件在目录扫描 `kv_cache_refresh` 时被静默跳过（`read_entry_file` 返回 false），等于「不存在」。

---

### 4.2 渲染文本与文件名（查找身份）

#### 4.2.1 概念说明

KVC 文件用**渲染文本**作为查找身份，这解决了一个无状态 API 的核心难题：

> 客户端每次请求都把整段对话重新发来。服务器怎么知道「这次的前缀，我之前已经 prefill 过、并且快照在了磁盘上」？

答案是：**把渲染文本的 SHA1 当文件名，把渲染文本本身存进文件**。下次请求来时，把 incoming prompt 渲染成文本，对目录里的每个 `.kv` 文件做「我的渲染文本是不是以你的渲染文本为前缀」的检查。命中就复用。

这里有一个微妙但关键的分工（README 与头文件注释都强调）：

- **渲染文本**只回答「这段字节是不是同一个前缀？」——它是**查找键**。
- **DS4 payload** 才携带**精确的 token id 序列与图状态**——它是**权威内容**。

为什么文本和 payload 要分开？因为「字节前缀命中」不等于「token 序列完全一致」。DeepSeek 的 BPE 可能把模型当年生成的 1 个 token，在客户端回发时拆成 2 个规范 token（解码文本相同、token 边界不同）。此时**前缀的 token 序列仍以 payload 为准**，服务器只对命中前缀之后的**新文本后缀**重新分词，再增量 prefill。

#### 4.2.2 核心流程

**写盘时**（确定文件名 + 内容）：

```
store_tokens (前缀 token)
   │  ds4_kvstore_render_tokens_text  → 解码成渲染文本 text
   ▼
text  ──sha1──>  sha (40 hex)  ──>  <sha>.kv  (文件名 = 查找键)
text  本身写入文件 (供加载时再次 sha 校验 + 前缀比对)
store_tokens 的 DS4 payload 写入文件 (权威 token/状态)
```

**读盘/查找时**（`try_load_text` 内）：

```
incoming prompt_text
   │  对每个 entry：读出 cached_text，重算 sha
   ▼
sha 必须等于文件名 sha          (cached text hash mismatch → 丢)
cached_text 必须是 prompt 的前缀 (cached text prefix mismatch → 跳)
model_id 必须一致                (different model → 跳)
命中 → ds4_session_load_payload 恢复权威状态
       → 仅对 prompt_text + text_bytes 之后的新文本后缀重新分词
```

文件名的合法形态由 `ds4_kvstore_sha_hex_name` 严格校验：必须是 40 个十六进制字符 + `.kv`，共 43 字符。

#### 4.2.3 源码精读

SHA1 实现是内置的（不依赖外部库），输出 40 字符小写十六进制：

[ds4_kvstore.c:321-328](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L321-L328) —— `ds4_kvstore_sha1_bytes_hex`：对一段字节算 SHA1，再经 `hex20` 转 40 hex。

文件名↔路径的互转：

[ds4_kvstore.c:330-338](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L330-L338) —— `ds4_kvstore_sha_hex_name`：把目录项名还原成 sha，长度必须正好 43 且后缀 `.kv`，否则跳过（这决定了目录扫描只认合法缓存文件，其它文件被无视）。

[ds4_kvstore.c:348-353](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L348-L353) —— `ds4_kvstore_path_for_sha`：反方向，`<dir>/<sha>.kv`。

存盘时算 sha 并据此建路径：

[ds4_kvstore.c:992-994](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L992-L994) —— `ds4_kvstore_sha1_bytes_hex(text, text_len, sha)` 得到文件名主部，`ds4_kvstore_path_for_sha` 拼出完整路径。注意这里 sha 的输入是**渲染文本字节**，不是 token id 序列。

`ds4_kvstore_entry` 结构体（内存中一条目的字段）几乎就是「固定头字段 + sha + path + 派生大小」：

[ds4_kvstore.h:36-57](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L36-L57) —— 顶部注释把「文件名是渲染字节前缀、payload 才是权威 token/状态」这一分工写得很清楚，是理解本模块的钥匙。

加载时的三重校验（sha、前缀、model_id）：

[ds4_kvstore.c:1245-1270](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1245-L1270) —— 读 `cached_text`、重算 `text_sha` 与文件名 sha 比对、再用 `ds4_kvstore_byte_prefix_match` 判断是否为 incoming prompt 的前缀；model_id 不符直接判失败。

命中后的「权威 payload + 仅后缀重新分词」：

[ds4_kvstore.c:1276-1290](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1276-L1290) —— 先 `ds4_session_load_payload` 恢复权威 token 历史，再用 `ds4_kvstore_build_prompt_from_exact_prefix_and_text_suffix` 只对命中字节之后的文本后缀分词。这段正是「文本是查找键、payload 是权威」的落点。

README 对应的权威说明：

[README.md:1011-1019](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1011-L1019) —— 「cache key is the SHA1 of the rendered byte prefix」「payload still stores the exact token IDs」「written with ordinary read/write, not mmap」。

#### 4.2.4 代码实践

**目标**：亲手算一个最小 KVC 文件的文件名，验证「文件名 = SHA1(渲染文本)」。

**操作步骤**：

1. 假设某段渲染文本就是字符串 `hello`（仅为示例，真实文本是解码后的对话）。
2. 用任意工具算 `hello` 的 SHA1（命令行示例）：
   ```sh
   printf '%s' "hello" | sha1sum
   ```
   预期得到 `aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d`（「待本地验证」：以你本地实际输出为准）。
3. 按 [ds4_kvstore.c:348-353](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L348-L353) 的拼法，对应文件名应为 `aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d.kv`，共 43 字符。
4. 用 `ds4_kvstore_sha_hex_name` 的规则（[ds4_kvstore.c:330-338](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L330-L338)）自检：长度是否 43、后缀是否 `.kv`、前 40 位是否全是十六进制。

**需要观察的现象 / 预期结果**：

- 文件名完全由渲染文本决定，**与 token id 序列无关**。两段 token 序列不同、但解码文本字节相同的前缀，会落到同一个 `.kv` 文件（命中后由 payload 区分权威 token）。
- 这也解释了为什么加载时必须**重算** `cached_text` 的 sha 并与文件名比对：防止文件被改名或文本段损坏后「误命中」。

> 命令运行说明：第 2 步的 `sha1sum` 是普通 shell 命令，可在任何机器运行；若不想运行，直接对照代码逻辑推演即可。

#### 4.2.5 小练习与答案

**练习 1**：如果两个不同的 token 前缀解码出**完全相同**的渲染文本，它们会共用一个 `.kv` 文件吗？这是 bug 吗？

**参考答案**：会共用同一个文件名（sha 相同）。这不是 bug：文件内 payload 存的是**其中一个**前缀的权威 token/状态，加载后服务器以 payload 的 token 为准、只对后缀重新分词。因为「解码文本相同」意味着对模型而言上下文等价，复用是安全的。这正是设计上「文本当键、payload 当权威」的妙处。

**练习 2**：为什么加载时除了比前缀，还要再算一次 `cached_text` 的 sha 与文件名比？光比前缀不够吗？

**参考答案**：文件名可能被外部改名、或文本段在磁盘上损坏。重算 sha 并与文件名比对能发现「文件名与内容不自洽」的情况（`read_header` 路径会记 `cached text hash mismatch`），避免用一个内容被篡改的快照去恢复会话。这是一道额外的完整性校验。

---

### 4.3 KTM 段（可选的 tool-id map）

#### 4.3.1 概念说明

`.kv` 文件可以挂一个**可选尾部段**：KTM（tool-id map，工具 id 映射）。它解决的是**工具调用的精确回放**问题（背景见 u7-l4）：

> 模型当年用 DSML 文本格式采样了一次工具调用。客户端把这次调用以 JSON 回发。如果服务器重新渲染出的 DSML 字节和模型当年采样的字节**不完全一致**，KV 前缀就会在工具调用处断开，后缀要重 prefill。

KTM 段存的是「不可猜测的 API tool id → 模型当年精确采样的 DSML 字节块」的映射。有了它，重启后的服务器能用 tool id 找回**逐字节相同**的 DSML，让渲染文本与活 KV 逐字节对齐，从而保住前缀复用。

关键点：

- **可选**：只有当固定头 `ext_flags` 的 bit0（`DS4_KVSTORE_EXT_TOOL_MAP`）置位时才存在。
- **辅助回放记忆，不是模型状态**：它不影响 logits、不参与推理；丢失它只会退回到「规范化 JSON→DSML 渲染」的后备路径，正确性不变，只是可能损失一些前缀复用。
- **格式自描述**：KTM 段自己有 `"KTM"` magic + version，未来可以加新的扩展段而不混淆。

#### 4.3.2 核心流程

**何时写 KTM**：

```
store_live_prefix_text(... hooks ...)
   │  hooks 非 NULL 且 hooks->write 存在？
   ▼ 是
ext_flags |= DS4_KVSTORE_EXT_TOOL_MAP   (bit0)
写完 header/text/payload 后，调 hooks->write(fp, text) 追加 KTM 字节
trailer_bytes 计入文件总大小与预算检查
```

**何时读 KTM**：

```
try_load_text(... hooks ...)
   │  load_payload 成功？
   ▼
if (hdr.ext_flags & hooks->ext_flag)   即 bit0 置位
   hooks->load(ud, fp, load_wanted)    读取 KTM 段
```

**KTM 段内部布局**（来自 README，由服务器侧 hooks 实际写入，kvstore 本身只搬字节）：

```
0   u8[3]  magic = "KTM"
3   u8     version = 1
4   u32    entry count
对每个 entry：
0   u32    tool id 字节长度
4   u32    sampled DSML 字节长度
8   bytes  tool id
…   bytes  精确采样的 DSML 块
```

注意：**kvstore 不知道也不解析 KTM 的内部格式**。它只负责（a）在 ext_flags 里打个 bit、（b）调用 hooks 回调把字节追加进去 / 读出来。KTM 的实际生成与消费在 `ds4_server.c`（u7-l4 讲）。本模块只讲「这个段在 KVC 文件里的位置与触发条件」。

#### 4.3.3 源码精读

扩展位定义：

[ds4_kvstore.h:15](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L15) —— `DS4_KVSTORE_EXT_TOOL_MAP = (1u<<0)`，对应固定头字节 6 的 bit0。

trailer hooks 接口（kvstore 与服务器之间的契约）：

[ds4_kvstore.h:91-98](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L91-L98) —— `ds4_kvstore_trailer_hooks` 含 `ext_flag`、`serialized_size`、`write`、`load`、`load_wanted`。kvstore 通过这组回调把「尾部段」完全外包给调用方。

存盘时决定是否置位 + 追加：

[ds4_kvstore.c:1073-1091](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1073-L1091) —— 行 1074 算 `ext_flags`（有 hooks 且预估有字节才置 tool-map 位；text override 再或上对应位）；行 1085-1090 是文件写入的**完整 5 段顺序**：`fwrite(h,48)` → `fwrite(tb,4)` → `fwrite(text)` → `ds4_session_write_staged_payload` → `kv_trailer_write`。最后一项就是 KTM。

trailer 三个回调的薄包装：

[ds4_kvstore.c:867-881](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L867-L881) —— `kv_trailer_serialized_size` / `kv_trailer_write`：hooks 为空或无对应回调时直接返回 true（等于不写 trailer）。

加载时按位消费 trailer：

[ds4_kvstore.c:1291-1293](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1291-L1293) —— payload 加载成功后，仅当 `hdr.ext_flags & hooks->ext_flag` 才调 `hooks->load`。顺序很重要：**先 payload，后 trailer**，因为 trailer 是辅助记忆，模型状态必须先就位。

「重写 trailer」的独立路径（命中已有兼容文件时只更新 KTM，不重写 payload）：

[ds4_kvstore.c:883-921](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L883-L921) —— `kv_cache_rewrite_trailer`：用 `DS4_KVSTORE_FIXED_HEADER + 4 + text_bytes + payload_bytes` 算出 trailer 起点偏移，`ftruncate` 截到此处再重写 trailer，并把 ext_flags 或上 tool-map 位后重写固定头。这条路径让「同前缀但 tool map 更新了」的情况不必重做昂贵的 payload 序列化。

README 对 KTM 段与加载顺序的官方说明：

[README.md:1062-1089](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1062-L1089) —— 「optional tool-id map is present only when header extension bit 0 is set」「A cache hit restores the session payload first, then loads the map if present」，并给出 KTM 段字节布局。

#### 4.3.4 代码实践

**目标**：验证「KTM 段的有无完全由 hooks 决定，且不影响固定头以外的写入顺序」。

**操作步骤**：

1. 读 [ds4_kvstore.c:1073-1091](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1073-L1091)，把 5 个写入调用按顺序抄下来。
2. 假设两次存盘：一次 `hooks=NULL`，一次 `hooks` 非空且 `write` 有值。问：两次写出的文件，前 3 段（header+tb+text）是否完全相同？
3. 对照 [ds4_kvstore.c:1074-1075](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1074-L1075) 确认 `ext_flags` 在两种情况下是否不同，进而确认「第 4 段 payload 是否相同」「文件是否一个有 KTM 一个没有」。

**需要观察的现象 / 预期结果**：

- `hooks=NULL` 时：`ext_flags=0`，没有第 5 段，文件总大小 = `48+4+text_bytes+payload_bytes`。
- `hooks` 非空且 `trailer_est_bytes>0` 时：`ext_flags` bit0 置位，文件末尾多出 KTM 段，总大小多 `trailer_bytes`。
- 关键结论：**KTM 是纯增量尾部**。一个不支持工具的客户端（不传 hooks）写出的 `.kv` 文件，可以被任何加载方安全读取——加载方看到 bit0=0 就跳过 `hooks->load`。

> 命令运行说明：本实践为源码阅读型，结论直接来自 `store_live_prefix_text` 的写入序列。

#### 4.3.5 小练习与答案

**练习 1**：如果 KTM 段损坏了，加载会失败吗？推理结果会错吗？

**参考答案**：不会让加载失败、也不会让推理出错。KTM 是辅助回放记忆而非模型状态。`try_load_text` 里 `hooks->load` 发生在 payload 加载**成功之后**（[ds4_kvstore.c:1291-1293](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1291-L1293)），即便它失败，权威的 KV 状态已经恢复，最坏后果是回退到规范化 JSON→DSML 渲染（u7-l4），损失一点前缀复用，正确性不受影响。

**练习 2**：为什么 KTM 段要自带 `"KTM"` magic 和 version，而不是直接靠固定头的 ext_flags bit0 推断全部内容？

**参考答案**：固定头的 bit0 只回答「有没有 trailer」，不描述 trailer 的内部格式。自带 magic+version 让尾部段**自描述、可独立演进**：将来可以新增别的扩展段（别的 bit），或升级 KTM 自身格式（抬 version），加载方按 magic 分发、按 version 解析，互不干扰。这与 KVC 头的 magic/version 是同一套设计哲学。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份 **KVC 文件内存布局图**。

**任务**：假设一个快照——渲染文本为 12 字节、DS4 payload 为 1000 字节、带一个含 2 个条目的 KTM 段（KTM 段头 8 字节 + 两个 entry，假设合计 60 字节）。请你：

1. **画出整文件的字节布局图**，标注每一段的起始偏移与长度，并算出文件总大小。
2. **填出固定头 48 字节的关键字段**：偏移 0/3/4/6/7/8/16/20/40 分别应填什么（reason 自选一个，比如 cold=1；model_id=0 Flash；tokens=自定；payload_bytes=1000）。
3. **写出文件名**：它是哪段字节的 SHA1？为什么 payload 和 KTM 不参与文件名？
4. **指出加载方如何定位 KTM 段的起点**：给出用到的偏移计算公式（提示：固定头 + 4 + text_bytes + payload_bytes）。

**参考要点（请先自己画再对照）**：

- 文件总大小 = `48 + 4 + 12 + 1000 + 60 = 1124` 字节。
- 段边界：`[0..48)` 固定头，`[48..52)` text_bytes(u32=12)，`[52..64)` 渲染文本，`[64..1064)` DS4 payload，`[1064..1124)` KTM。
- 文件名 = SHA1(渲染文本 `[52..64)` 这 12 字节)。payload 与 KTM 不参与，是因为文件名只编码「查找键」（渲染前缀），权威内容与辅助记忆的变化不应改文件名，否则每次 tool map 更新都会让缓存「换名失踪」（这也正是 `kv_cache_rewrite_trailer` 能原地改 KTM 而不动文件名的原因）。
- KTM 起点偏移 = `DS4_KVSTORE_FIXED_HEADER(48) + 4 + text_bytes(12) + payload_bytes(1000) = 1064`，对应 [ds4_kvstore.c:898-899](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L898-L899) 的算法。

> 命令运行说明：本实践是手工推演，无需运行 ds4。若你想用真实文件验证，可在启用 `--kv-disk-dir` 跑过一次后，用 `od -A d -t x1 <sha>.kv | head` 查看前 48 字节并与你的图对照（「待本地验证」：取决于你是否本地跑过服务器）。

## 6. 本讲小结

- 一个 `.kv` 文件 = **48 字节固定头 + 4 字节 text_bytes + 渲染文本 + DS4 payload + 可选 KTM**，全部小端，总大小 `48+4+text+payload+trailer`。
- 固定头由 `fill_header`/`read_header` 镜像读写，magic `"KVC"`+version+payload ABI 三道校验保证自描述；字段覆盖量化档、模型 id、token 数、上下文、命中计数、两个时间戳与 payload 字节数。
- **渲染文本既是文件名（SHA1）又是查找身份**；payload 才是权威 token/图状态。加载时三重校验（sha、前缀、model_id），命中后只对新文本后缀重新分词。
- **KTM 是纯增量可选尾部**，由 `ext_flags` bit0 标记、由服务器侧 hooks 写入/读出；kvstore 不解析其内部，先恢复 payload 再读 KTM，丢失 KTM 只损失复用不损正确性。
- README 的布局图与代码基本一致，唯一需注意：README 把字节 20 标为保留，代码实际把它用作 **payload ABI 守卫（=2）**，读取时会拒绝不匹配的文件。

## 7. 下一步学习建议

- 想知道**什么时候**会写出这些文件、何时被淘汰：继续学 **u8-l2（KV store 策略与淘汰）**，它讲 cold/continued/evict/shutdown 四种保存时机与命中半衰期淘汰评分。
- 想知道固定头里 `payload_bytes` 那段**内部**长什么样：继续学 **u8-l3（会话 payload 序列化 DSV4）**，它拆解 13 个 u32 头、token/logits、逐层 KV 与压缩前沿。
- 想知道 KTM 段里的 tool id → DSML 映射在服务器里如何被**使用**：回看 **u7-l4（工具调用：DSML、精确回放、规范化）**，它讲 rax 基数树与精确回放如何消除前缀失配。
- 建议结合本讲动手把综合实践的布局图落在纸上，再带着它去读 u8-l3，这样 DSV4 头部就能直接「嵌」进本讲的 payload 段位置，形成完整的文件心智模型。
