# 分词器与聊天模板渲染

## 1. 本讲目标

本讲聚焦在「文本如何变成模型能吃的 token 序列」这道桥梁上。读完后你应当能够：

- 说清 ds4 是怎么从 GGUF 里把词表和合并规则读进内存、又建成哪两张查找表的。
- 解释 GPT-2 风格的字节级 BPE（byte-level BPE）在 ds4 里的实现：预切分、字节编码、贪心合并。
- 写出 DeepSeek V4 聊天模板的拼接顺序，以及 `none` / `high` / `max` 三种 thinking 模式如何改变这条序列。
- 区分两条编码路径：原生聊天编码（`ds4_encode_chat_prompt`）与「已渲染聊天文本」回扫路径（`ds4_tokenize_rendered_chat`），并理解 DSML 特殊 token 在其中扮演的角色。
- 用 `--dump-tokens` 独立观察任意提示的分词结果，作为排查「模型答非所问」的第一件工具。

本讲不涉及推理内核，只讨论**编码（encode）**这一侧。解码（把 token 变回文本）只点到为止。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是 BPE 分词

BPE（Byte-Pair Encoding，字节对编码）是一种「从最小单位逐步合并」的分词法。训练时统计文本里哪一对相邻符号最常一起出现，就把它们合并成一个新符号，重复若干轮，得到一张「合并规则表」。推理时分词器照这张表贪心地把输入拼成尽量长的片段，每个片段对应词表里的一个 token id。

ds4 用的是 GPT-2 风格的**字节级 BPE**：先把每个原始字节映射成一个可见的 Unicode 码点，再做合并。这样做的好处是——任何字节序列（包括无效 UTF-8）都能被编码，永远不会出现「无法分词」的字节。

DeepSeek V4 Flash 的词表规模约 12.8 万，存在 GGUF 的 `tokenizer.ggml.tokens`（token 字符串表）与 `tokenizer.ggml.merges`（合并规则表，按训练时合并先后排序）里。

### 2.2 什么是聊天模板

裸语言模型只懂「一串 token」。要让它变成会话助手，需要在每轮对话前后插入**控制 token**，把「系统提示」「用户消息」「助手回答」用模型训练时见过的固定标记包起来。这套包装规则就是聊天模板。

DeepSeek V4 用一组带特殊符号的中文风格标记，例如：

| 标记字符串 | 含义 | 在 ds4 里的字段 |
| --- | --- | --- |
| `<｜begin▁of▁sentence｜>` | 句首（BOS） | `vocab.bos_id` |
| `<｜end▁of▁sentence｜>` | 句尾（EOS） | `vocab.eos_id` |
| `<｜User｜>` | 用户段开始 | `vocab.user_id` |
| `<｜Assistant｜>` | 助手段开始 | `vocab.assistant_id` |
| `<think>` / `</think>` | 思考段开始 / 结束 | `think_start_id` / `think_end_id` |
| `｜DSML｜` | DSML 工具调用块标记 | `vocab.dsml_id` |

注意这些字符串里混用了普通 ASCII 竖线 `|` 与全角竖线 `｜`（U+FF5C）——它们长得像但不是同一个字符，下文会看到 ds4 对此的处理。

### 2.3 thinking 模式为什么影响 token 序列

DeepSeek V4 Flash 有「不思考 / 普通 thinking / Think Max」三档。这三档在**分词阶段**就已经产生差异：开 thinking 时，助手段后要先塞一个 `<think>`（让模型从「思考」开始）；关 thinking 时塞的是 `</think>`（让模型直接给答案）。Think Max 还会额外在开头注入一段「请竭尽全力思考」的超长指令前缀。所以 thinking 模式不是推理时才生效的开关，它直接改写喂给模型的 token 序列。

## 3. 本讲源码地图

本讲主要在两个文件里穿梭：

| 文件 | 作用 | 本讲涉及范围 |
| --- | --- | --- |
| `ds4.c` | 引擎核心，含完整的分词器与聊天编码实现 | 词表加载、BPE、聊天模板、DSML 回扫、dump-tokens |
| `ds4.h` | 公共 API 头：`ds4_tokens`、`ds4_think_mode` 枚举与编码函数声明 | 公共类型与对外接口 |
| `ds4_cli.c` | CLI 前端，决定走原生编码还是「已渲染」回扫，并实现 `--dump-tokens` 短路 | 两条编码路径的分支点 |
| `ds4_help.c` | 帮助文本 | `--dump-tokens` 的一句话说明 |

`ds4.c` 中分词相关代码集中在文件偏后的 `Tokenizer and Chat Prompt Encoding` 一节（约 21685 行起的注释块），全部用静态函数实现，对外只暴露少数 `ds4_*` 包装。

## 4. 核心概念与源码讲解

本讲解析三个最小模块：**词表加载与 BPE 分词机制**、**聊天模板与 thinking 模式**、**DSML 特殊 token、渲染回扫与 dump-tokens**。

### 4.1 词表加载与 BPE 分词机制

#### 4.1.1 概念说明

ds4 的分词器是「**加载时构建查找表，运行时查表 + 合并**」的两段式设计：

1. **加载阶段**（`vocab_load`）：从 mmap 的 GGUF 元数据里读出 token 字符串表和合并规则表，建两张开放寻址哈希表：`token_to_id`（字符串 → id）与 `merge_rank`（合并对 → 训练时的合并次序）。同时按字符串名查到几个特殊控制 token 的 id。
2. **运行阶段**（`bpe_tokenize_text` → `bpe_emit_piece`）：对任意文本做预切分，再对每个片段套用字节级 BPE 贪心合并。

把 BPE 归到「词表加载」这一模块，是因为 BPE 完全依赖加载阶段建好的 `merge_rank` 表——没有这张表，合并无从谈起。

#### 4.1.2 核心流程

**加载阶段**（`vocab_load`）：

```
读 GGUF:  tokenizer.ggml.tokens   → vocab.token[i]，并建 token_to_id
读 GGUF:  tokenizer.ggml.merges   → 建 merge_rank（键="左 右"，值=合并次序 i）
按字符串名查 7 个特殊 token 的 id（BOS/EOS/User/Assistant/<think>/</think>/｜DSML｜）
```

**BPE 分词阶段**（`bpe_tokenize_text` + `bpe_emit_piece`）：

```
对输入文本:
  按 JoyAI 预切分规则切成「词」（数字、CJK、字母串、标点串、空白串……）
  对每个词 piece:
    byte_encode(piece)            # 每个字节 → 一个 GPT-2 码点字符
    把码点串切成初始符号序列（按 UTF-8 字符边界）
    反复:
      在所有相邻符号对里，找 merge_rank 最小的那对
      若找不到任何已知合并对，停止
      否则把这对合并成一个符号
    最后把每个符号查 token_to_id 得到 id 并输出
```

**BPE 合并的选择规则**用数学语言表述：设当前符号序列为 \(s_1, s_2, \dots, s_n\)，对每个相邻对 \( (s_i, s_{i+1}) \) 查表得到合并优先级 \( r_i = \text{merge\_rank}(s_i, s_{i+1}) \)（查不到记 \( r_i = -1 \) 表示不可合并）。每一步选出：

\[
i^* = \arg\min_{\,i\,:\,r_i \ge 0} r_i
\]

把 \( s_{i^*} \) 与 \( s_{i^*+1} \) 合并成一个新符号，序列长度减一，重复直到没有可合并对。选最小 rank 等价于「优先做训练时最早学会的合并」，这与 GPT-2 / tiktoken 的标准行为一致。

**字节级编码**的目的是让合并能在「可见字符」上做而不丢失字节身份。GPT-2 的映射规则是：可打印 ASCII（33–126 等区间）原样保留，其余字节映射到 U+0100 起的码点。这样像换行符 `\n`（字节 0x0A）会被映成一个特殊字符，BPE 就能把它当作普通符号参与合并——这正是 ds4 注释里强调「标点后的换行要留在同一个 BPE 词里」能成立的原因。

#### 4.1.3 源码精读

先看词表结构。`ds4_vocab` 持有 token 字符串数组、两个哈希表，以及 7 个特殊 token 的 id：

[ds4.c:21794-21806](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21794-L21806) — `struct ds4_vocab`：注意它被嵌在 `ds4_engine` 里（`ds4.c:21811`），是引擎级、进程级只读状态，多个 session 共享同一份词表。

`vocab_load` 把 GGUF 两张表读进内存。它用 `model_get_array` 拿到元数据数组的位置，再用 `ds4_cursor` 顺序读字符串，边读边塞哈希表：

[ds4.c:22233-22273](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22233-L22273) — `vocab_load`。其中第 22248–22256 行建 `token_to_id`，第 22258–22264 行建 `merge_rank`（键是「左 空格 右」三段拼接，与 `bpe_rank` 的键构造方式必须一致）。结尾 22266–22272 行用 `vocab_lookup` 按字符串名查 7 个特殊 token——这是「**按名字查 id**」而非按位置，因此 GGUF 词表顺序变化也不会影响。

`vocab_lookup` 是一个「找不到就 `exit(1)`」的强制查找——这些特殊 token 是 ds4 协议的硬依赖，缺一个就说明 GGUF 不是为 DeepSeek V4 准备的：

[ds4.c:22223-22230](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22223-L22230) — `vocab_lookup`。

两张哈希表用的是自定义开放寻址实现（`str_i32_table`），不是 std hashmap：

[ds4.c:21702-21706](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21702-L21706) — `str_i32_table` 结构（键为 `ds4_str` 即 `{ptr,len}`，值为 int）。建表容量取 `expected * 2 + 16` 向上取 2 的幂（`table_init`，ds4.c:21714），保持低装填因子以减少冲突。

进入运行阶段。字节级编码由 `byte_encode` 配合 `gpt2_byte_to_codepoint` 完成：

[ds4.c:21909-21937](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21909-L21937) — `gpt2_byte_to_codepoint` 与 `byte_encode`。`byte_encode` 给每个输出字节预留 4 字节（一个码点最多 4 字节 UTF-8）。

`bpe_rank` 构造合并键「左 右」（中间一个空格）去查 `merge_rank`，返回 -1 表示这对不可合并：

[ds4.c:21961-21975](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21961-L21975) — `bpe_rank`。注意它优先用栈上 512 字节缓冲（`char stack[512]`），超长才堆分配，避免高频小查询打 malloc。

`bpe_emit_piece` 是 BPE 合并主循环，对应 4.1.2 里描述的 \( \arg\min \) 流程：

[ds4.c:21978-22043](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21978-L22043) — `bpe_emit_piece`。第 21997–22025 行是「反复找最小 rank 合并」的循环；第 22027–22039 行把最终符号查 `token_to_id` 得到 id，查不到则退化成逐字节回退（保证任何输入都能编码）。

最后是预切分。`bpe_tokenize_text` 用一长串 `if/else` 复刻 JoyAI-LLM 预切分器的分裂形状（数字每 3 位一组、CJK 连续、标点+字母、空白+换行合并等）。注释明确指出**切分形状会改变最终 BPE 合并结果**，因此必须逐条对齐：

[ds4.c:22133-22221](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22133-L22221) — 预切分规则注释与 `bpe_tokenize_text`。特别注意 22147–22149 与 22184–22192 行的注释：标点后的换行要和标点留在同一个词里，单独切分会让代码类提示的 token 流和长上下文 logits 都出错。这也是为什么 ds4 不直接用通用正则库，而是手写这组分情况判断。

对外暴露的最薄包装是 `ds4_tokenize_text`：

[ds4.c:22308-22310](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22308-L22310) — `ds4_tokenize_text`，直接调用 `bpe_tokenize_text`，是「纯文本（不含特殊 token）」的编码入口。

#### 4.1.4 代码实践

**目标**：验证「加载阶段建的 `merge_rank` 表」确实决定了 BPE 合并顺序，方法是观察同一个词在不同上下文里是否合并出同样的 token。

**操作步骤**（源码阅读型 + 本地验证）：

1. 打开 `ds4.c:22258-22264`，确认 `merge_rank` 的键构造为 `merge_rank_put("左 右")`（`table_put` 在循环里）。
2. 再打开 `ds4.c:21961-21975` 的 `bpe_rank`，确认它用同样的 `"左 右"` 拼接去查。键构造不对称会导致永远查不到——这是一处典型的「写入端与读取端必须对齐」约定。
3. 若本地已有可运行的 `./ds4` 与模型，执行：
   ```sh
   ./ds4 --dump-tokens -p "Hello world"
   ./ds4 --dump-tokens -p "Hello,world"
   ```
   观察 `Hello` 是否被合并成单个 token，而逗号紧跟 `world`（无空格）时的切分是否变化。

**需要观察的现象**：带空格的 `" world"` 通常会以一个「前导空格 + world」的合并 token 出现；而 `,world` 中逗号会与换行等留在标点串里。这印证了 4.1.3 提到的「标点+字母」「空白」预切分规则。

**预期结果**：`Hello` 单独成词后走 BPE，多半合并为 1 个 token；`world` 前的空格被预切分吸进同一个词，因此 ` world` 是一个整体参与合并。具体 token id 因模型版本而异——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`vocab_load` 里 `merge_rank` 表的键为什么用「左 空格 右」三段拼接，而不是直接拼接 `左右`？

**参考答案**：因为 BPE 词表里存在「AB」与「A B」两种不同的 token（前者是合并后的，后者是两个独立 token）。用空格分隔可以无歧义地区分「合并对 (A,B)」和「字符串 "AB"」。`bpe_rank`（ds4.c:21961）与 `vocab_load` 的 `table_put`（ds4.c:22263）用了完全相同的拼接格式，二者必须对齐。

**练习 2**：如果 GGUF 里缺少 `"<think>"` 这个 token，`vocab_load` 会发生什么？为什么这是合理设计？

**参考答案**：`vocab_lookup`（ds4.c:22223）在 `table_get` 失败时会打印 `required tokenizer token is missing` 并 `exit(1)`。这是合理的，因为 `<think>` 是 ds4 协议的硬依赖——聊天模板（4.2 节）必须能插入它。缺它说明这份 GGUF 不是为 DeepSeek V4 准备的，早死好过带着错误协议继续。

---

### 4.2 聊天模板与 thinking 模式

#### 4.2.1 概念说明

加载好词表后，下一步是把「系统提示 + 用户消息」包装成模型训练时见过的聊天格式。这一步**不经过 BPE 合并的随机性**——控制标记是直接按 id 插入的（`token_vec_push` 一个整数），只有用户文本正文才走 BPE。

ds4 的 thinking 模式分三档，由 `ds4_think_mode` 枚举定义：

[ds4.h:25-29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L25-L29) — `DS4_THINK_NONE` / `DS4_THINK_HIGH` / `DS4_THINK_MAX`。

「是否启用 thinking」的判定由 `ds4_think_mode_enabled` 给出：只有 `HIGH` 和 `MAX` 算「开」：

[ds4.c:23178-23180](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23178-L23180) — `ds4_think_mode_enabled`。

Think Max 有个重要约束：它需要至少 384K（393216）token 的上下文窗口，否则会**自动降级**为普通 thinking。这是因为 Think Max 会在序列开头注入一段超长的「竭尽全力思考」指令，小上下文塞不下：

[ds4.c:23199-23204](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23199-L23204) — `ds4_think_mode_for_context`，上下文不足时把 `MAX` 降级为 `HIGH`。

#### 4.2.2 核心流程

一次性 prompt 的聊天模板由 `encode_chat_prompt`（静态）拼接，顺序为：

```
[BOS]
(可选) Think Max 指令前缀 "Reasoning Effort: Absolute maximum..."   ← 仅 THINK_MAX
(可选) 系统提示 system 文本（走 BPE）
[User]                          ← vocab.user_id，按 id 直插
用户正文 prompt 文本（走 BPE）
[Assistant]                     ← vocab.assistant_id
若开 thinking:  <think>          ← think_start_id
若关 thinking:  </think>         ← think_end_id
```

关键点：助手段结尾插的是 `<think>` 还是 `</think>`，完全由 `ds4_think_mode_enabled(think_mode)` 决定。模型从这里开始自回归续写——开 thinking 时它从「思考内容」开始写，关 thinking 时直接写答案。

多轮对话（REPL、服务器）用的是另一组「逐段追加」的 API：`ds4_chat_begin` 起头插 BOS，`ds4_chat_append_message` 按 role（system/user/assistant/tool）追加不同包装，`ds4_chat_append_assistant_prefix` 在每轮助手回答前插 `[Assistant]` 加 `<think>` 或 `</think>`。这组 API 让前端可以增量维护一份 `transcript`（已渲染的 token 序列），配合 u2-l3 讲过的前缀复用，只对新增后缀 prefill。

Think Max 的指令前缀是一段写死的英文，要求模型「穷尽一切思考」：

[ds4.c:65-68](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L65-L68) — `DS4_REASONING_EFFORT_MAX_PREFIX` 常量。它被当作普通文本走 BPE 编码后插在 BOS 之后、系统提示之前。

#### 4.2.3 源码精读

核心拼接函数 `encode_chat_prompt`：

[ds4.c:22285-22306](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22285-L22306) — `encode_chat_prompt`。逐行对照 4.2.2 的流程：第 22291 行 push BOS；22292–22294 行仅在 `THINK_MAX` 时追加指令前缀（走 `bpe_tokenize_text`）；22295–22297 行追加非空系统提示；22298 行 push `[User]`；22299 行追加用户正文；22300 行 push `[Assistant]`；22301–22305 行按 thinking 开关决定结尾插 `<think>` 还是 `</think>`。注意它接收的是 `token_vec *out`，`token_vec` 就是公共类型 `ds4_tokens` 的别名：

[ds4.c:609](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L609) — `typedef ds4_tokens token_vec;`（公共结构 `ds4_tokens` 见 ds4.h:43-47，含 `{int *v; int len; int cap;}`）。

对外包装（加一层 engine 解引用，符合「窄头」边界）：

[ds4.c:22375-22382](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22375-L22382) — `ds4_encode_chat_prompt`。

多轮追加 API 中最有信息量的是 `ds4_chat_append_message`，它按 role 分四种包装，体现了「工具结果」如何被塞回对话：

[ds4.c:22410-22432](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22410-L22432) — `ds4_chat_append_message`。重点看 22417–22422 行的 `assistant` 分支：若助手内容不是以 `<think>` 或 `</think>` 开头，会自动补一个 `</think>`——这保证历史助手段总是「闭合思考后给答案」的形态，避免污染 KV 前缀。22423–22427 行的 `tool`/`function` 分支把工具输出包进 `<tool_result>...</tool_result>`（注意是普通 ASCII 标签，不是 DSML），并用专门的 `bpe_tokenize_tool_result_text`（ds4.c:22388）转义掉内容里可能出现的 `</tool_result>` 收尾标记，防止工具输出提前终止包装——这是一处安全细节。

每轮助手回答前的固定前缀：

[ds4.c:22434-22438](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22434-L22438) — `ds4_chat_append_assistant_prefix`，push `[Assistant]` 后按 thinking 模式选 `<think>`/`</think>`。

#### 4.2.4 代码实践

**目标**：亲眼看到 thinking 模式如何改变 token 序列的「结尾那一个 token」。

**操作步骤**（本地验证）：

1. 用 `--nothink` 与 `--think` 各跑一次 dump-tokens，对比结尾：
   ```sh
   ./ds4 --nothink --dump-tokens -p "说一个笑话"
   ./ds4 --think   --dump-tokens -p "说一个笑话"
   ```
2. 在输出末尾找到 `[Assistant]`（其 id 即 `vocab.assistant_id`）之后的那一个 token：`--nothink` 应是 `</think>`，`--think` 应是 `<think>`。
3. （可选）若机器上下文足够大，试 `--think-max`，观察序列开头是否多出一段 `Reasoning Effort: Absolute maximum...` 文本对应的 token。

**需要观察的现象**：同一句用户提示，仅 thinking 开关不同，序列**头部与中部基本一致**（BOS + `[User]` + 正文 + `[Assistant]`），**仅结尾一个 token 不同**。这正是 4.2.1 所说「thinking 在分词阶段就已产生差异」。

**预期结果**：结尾 token 一个是 `</think>`、一个是 `<think>`；具体 id 见你模型词表——**待本地验证**。若你的 CLI 版本里 `--dump-tokens` 走的是 4.3 节的「已渲染回扫」路径（见下），需要确认它是否套了聊天模板；可改用 `-p` 配合 `--think`/`--nothink` 触发 `ds4_encode_chat_prompt`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `encode_chat_prompt` 在 `THINK_MAX` 时追加指令前缀的位置是「BOS 之后、系统提示之前」，而不是放在用户消息之后？

**参考答案**：因为这段指令是「全局推理预算」的元层指令，逻辑上属于整段对话的设定，应紧随 BOS、先于任何具体消息。放在用户消息之后会把它当成用户输入的一部分，语义错位；放在最前则让模型在读取任何内容前就建立「竭尽全力思考」的基调。这也匹配 DeepSeek 官方 Think Max 的协议形态。

**练习 2**：设 `ctx_size = 200000`，用户请求 `THINK_MAX`。`ds4_think_mode_for_context` 会返回什么？这条请求最终走的是哪种模板？

**参考答案**：返回 `DS4_THINK_HIGH`（因为 200000 < 393216，见 ds4.c:23200）。因此 `ds4_think_mode_enabled` 仍为真，模板结尾插 `<think>`，但**不会**注入 Think Max 指令前缀（`encode_chat_prompt` 第 22292 行的 `if (think_mode == DS4_THINK_MAX)` 不成立）。即：用户想要 Max，实际拿到的是普通 thinking。

---

### 4.3 DSML 特殊 token、渲染回扫与 dump-tokens

#### 4.3.1 概念说明

前两节假设「输入是结构化的（system + prompt）」。但 ds4 有时收到的不是结构化输入，而是**一整段已经渲染好的聊天文本**——例如服务器从磁盘 KV 恢复会话、或用户用 `--prompt-file` 直接喂一段以 `<｜begin▁of▁sentence｜>` 开头的完整转录本。这时需要把这段文本**逐字符扫一遍**：遇到普通文本走 BPE，遇到特殊标记（BOS / `[User]` / `<think>` / `｜DSML｜` 等）就直接按 id 输出、不参与 BPE 合并。这就是「渲染回扫」路径。

`｜DSML｜` 是这条路径要识别的特殊标记之一。DSML（DeepSeek Markup Language，详见 u7-l4）是 ds4 用来表达工具调用的文本格式，模型在生成时会吐出形如 `...<｜tool▁calls▁begin｜>...｜DSML｜...` 的片段。`｜DSML｜` 这个标记用的是**全角竖线** `｜`（U+FF5C），不是 ASCII 的 `|`。如果它被当作普通文本走 BPE，会被切碎成多个 token，模型协议就会错位。因此回扫路径必须把它当作「原子特殊 token」整体识别。

`--dump-tokens` 是观察这一切的窗口：它把输入字符串**原样**走一遍分词（具体走哪条路径见 4.3.3），打印每个 token 的 id 与原文，然后**在推理开始前退出**。这是 ds4 官方推荐的「排查答非所问」第一工具（README Debugging Notes）。

#### 4.3.2 核心流程

**渲染回扫**（`tokenize_rendered_chat_vocab`）：

```
span 起点指向文本开头
逐字符扫描:
  若当前位置命中某个特殊标记串:
    把 [span, 当前) 这段普通文本走 BPE
    直接 push 该特殊 token 的 id
    跳过整个特殊串，span 前移
  否则继续下一字符
收尾: 把 [span, 末尾) 这段走 BPE
```

**CLI 选择两条路径的判定**（`build_prompt`）：

```
若 prompt 以 "<｜begin▁of▁sentence｜>" 开头:
  走渲染回扫（ds4_tokenize_rendered_chat）—— 认为用户给的是完整渲染文本
否则:
  走原生聊天编码（ds4_encode_chat_prompt）—— 套聊天模板
```

**`--dump-tokens` 短路**（CLI `main`）：

```
解析参数后，若 dump_tokens 标志为真:
  要求必须有 -p 或 --prompt-file
  调用 ds4_dump_text_tokenization(model_path, prompt, stdout)
  直接 return，根本不打开 engine、不做推理
```

注意：`ds4_dump_text_tokenization` 内部用的是**渲染回扫路径**（`tokenize_rendered_chat_vocab`），而不是 `ds4_encode_chat_prompt`。这意味着 `--dump-tokens -p "..."` 默认**不会**给你套聊天模板——它只把字符串原样分词。这正好满足「我想看这段文本被切成什么」的需求。

#### 4.3.3 源码精读

特殊标记识别函数 `special_token_at` 列出全部 7 个协议标记：

[ds4.c:22312-22335](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22312-L22335) — `special_token_at`。逐项对照 2.2 节的标记表，注意第 22323 行的 `｜DSML｜` 用的是全角竖线。它对每个候选串做 `strncmp`，命中即返回该 token id 与串长。

回扫主循环：

[ds4.c:22346-22365](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22346-L22365) — `tokenize_rendered_chat_vocab`。22355 行一旦 `special_token_at` 命中，就先把「上一段普通文本」交给 `tokenize_span`（内部 `bpe_tokenize_text`，ds4.c:22337）切分，再 push 特殊 id，并令 `span = p` 继续扫描。这正是 4.3.2 描述的逻辑。对外包装：

[ds4.c:22367-22369](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22367-L22369) — `ds4_tokenize_rendered_chat`。

CLI 的路径判定只看「是否以 BOS 标记开头」：

[ds4_cli.c:283-286](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L283-L286) — `is_rendered_chat_prompt`，注意它在 `ds4_cli.c`，是前端的判定，不在引擎核心。分支点：

[ds4_cli.c:412-419](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L412-L419) — `build_prompt`，二选一调用渲染回扫或原生编码。

dump-tokens 的两处实现。引擎级包装 `ds4_engine_dump_tokens` 把已分好的 token 数组漂亮地打印出来（先一行 JSON 风格的 id 列表，再逐行「id + 原文」）：

[ds4.c:22440-22458](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22440-L22458) — `dump_tokens_fp` 与 `dump_tokens`。注意 22451 行用 `%.*s` 按精确长度打印 token 原文，避免特殊符号（如换行）破坏格式。

独立入口 `ds4_dump_text_tokenization` 是 `--dump-tokens` 的真正实现——它**只**加载模型与词表，走渲染回扫，打印后释放，全程不碰 engine/GPU：

[ds4.c:24929-24944](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24929-L24944) — `ds4_dump_text_tokenization`。对照 4.3.2：第 24935–24936 行 `model_open` + `vocab_load` 只读元数据与词表；24937 行 `tokenize_rendered_chat_vocab`（渲染回扫）；24941–24942 行 `vocab_free` + `model_close` 清理。

CLI 参数解析与短路：

[ds4_cli.c:1546-1547](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1546-L1547) — `--dump-tokens` 被解析为布尔标志。
[ds4_cli.c:1639-1651](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1639-L1651) — `main` 里的短路：校验有 `-p` 后直接 `ds4_dump_text_tokenization` 并 `return`，绕过 `ds4_engine_open`。这就是它「快」的原因——没有 GPU 初始化、没有 KV 分配。

帮助文本与官方说明：

[ds4_help.c:247](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L247) — `--dump-tokens` 一句话说明。
[README.md:1251-1254](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1251-L1254) — 官方指出 DSML 工具闭合标记会被切成两个 token：`</` 与 `｜DSML｜`。这正好印证 `special_token_at` 只把**完整** `｜DSML｜` 当原子，而它前面的 `</` 走的是 BPE（被切成了 `</`）。

#### 4.3.4 代码实践

**目标**：用 `--dump-tokens` 观察一段**带工具描述与 DSML 标记**的提示如何被切分，对照源码确认系统/用户/助手段的拼接与特殊 token 的原子识别。

**操作步骤**（本地验证）：

1. 准备一段已渲染的聊天文本（注意以 BOS 开头，从而触发渲染回扫路径；也可直接观察裸文本）。例如：
   ```sh
   ./ds4 --dump-tokens -p '<｜begin▁of▁sentence｜><｜User｜>调用工具<｜Assistant｜><think>'
   ```
2. 观察输出：每个特殊标记（`<｜begin▁of▁sentence｜>`、`<｜User｜>`、`<｜Assistant｜>`、`<think>`）应各占**单独一行、单个 id**，而中间的「调用工具」会被 BPE 切成若干中文 token。
3. 再测 DSML 标记，验证 README 的说法：
   ```sh
   ./ds4 --dump-tokens -p '</｜DSML｜'
   ```
   预期 `</` 是一个 token、`｜DSML｜` 是另一个 token（共两个 id）。
4. 对照 4.3.3 的 `special_token_at`（ds4.c:22312），确认这些串都在识别列表里；对照 `tokenize_rendered_chat_vocab`（ds4.c:22346），确认普通文本段（如「调用工具」）是被 `tokenize_span` 单独 BPE 的。

**需要观察的现象**：
- 特殊标记**不被 BPE 切碎**，每个对应唯一 id。
- 全角竖线 `｜` 的 DSML 标记被整体识别，而它前面的 ASCII `</` 走 BPE。
- 中文正文走 BPE，可能 1 字 1 token 或多字合并，取决于词表。

**预期结果**：DSML 闭合标记确实呈现为 `</` + `｜DSML｜` 两个 token（与 README 一致）；具体 id 与中文切分数因模型版本而异——**待本地验证**。

> 说明：如果你的 `./ds4` 还没编译，本实践可降级为「源码阅读型」——逐行走 `tokenize_rendered_chat_vocab`（ds4.c:22346），手动模拟输入 `<｜User｜>调用工具` 的扫描过程：在第 0 字节命中 BOS？没有（这段没以 BOS 开头，但 `special_token_at` 仍会在扫到 `<｜User｜>` 时命中）。记录每一步 `span` 与 `p` 的位置，写出最终 token 序列的形状。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--dump-tokens -p "你好"` 的输出里**看不到** `<｜User｜>`、`<think>` 这些标记，而只看到「你好」被切成的 token？

**参考答案**：因为 `--dump-tokens` 走的是 `ds4_dump_text_tokenization`（ds4.c:24929），它内部调用 `tokenize_rendered_chat_vocab`（渲染回扫）而非 `ds4_encode_chat_prompt`（聊天模板）。回扫只对**输入里实际出现**的特殊标记做原子识别，不会**主动添加**模板标记。要看到完整聊天模板的 token，应让 CLI 走原生编码路径——即不以 BOS 开头的 `-p` 在真正推理时会经过 `build_prompt`（ds4_cli.c:412）套模板，但 `--dump-tokens` 短路在模板之前，所以看不到。这是「dump-tokens 看的是『字符串怎么切』，不是『最终喂给模型的完整序列』」的关键区别。

**练习 2**：假设有人误把 DSML 标记里的全角竖线 `｜` 改成 ASCII `|`（写成 `|DSML|`），`special_token_at` 会怎样处理？对工具调用会有什么后果？

**参考答案**：`special_token_at`（ds4.c:22323）的候选串写死是全角 `｜DSML｜`，`strncmp` 不会匹配 ASCII 版本，于是 `|DSML|` 被当作普通文本走 BPE，被切成多个碎片 token。后果是模型协议里的 DSML 块边界丢失，工具调用解析（u7-l4 / u10-l2）无法识别，工具调用失败。这正是 ds4 在多处强调「全角竖线」的原因——它是协议的一部分，不是排版癖好。

## 5. 综合实践

把三个模块串起来，完成一次「从字符串到 token 序列」的完整追踪：

**任务**：给定一句带工具上下文的用户消息，分别用「原生聊天编码」和「渲染回扫」两条路径生成 token 序列，并解释它们为何不同。

**步骤**：

1. 选定输入。结构化版本（走原生编码）：系统提示 `你是助手`，用户消息 `请读取 a.txt`。渲染版本（走回扫）：把上述消息手动渲染成 DeepSeek 模板文本，以 `<｜begin▁of▁sentence｜>` 开头，例如：
   ```
   <｜begin▁of▁sentence｜>你是助手<｜User｜>请读取 a.txt<｜Assistant｜><think>
   ```
2. 用 `--dump-tokens` 观察渲染版本（它会走回扫路径）：
   ```sh
   ./ds4 --dump-tokens -p '<｜begin▁of▁sentence｜>你是助手<｜User｜>请读取 a.txt<｜Assistant｜><think>'
   ```
   记录输出里每个特殊标记的 id 与正文 token。
3. 在源码里手动推演「原生编码」路径：调用 `ds4_encode_chat_prompt(engine, "你是助手", "请读取 a.txt", DS4_THINK_HIGH, out)`（ds4.c:22375）。按 `encode_chat_prompt`（ds4.c:22285）逐行写出它生成的序列：`[BOS] + BPE("你是助手") + [User] + BPE("请读取 a.txt") + [Assistant] + <think>`。
4. 对比两条路径的序列：
   - 顺序、特殊标记位置是否一致？
   - 渲染回扫路径里，`你是助手` 紧跟 BOS、中间**没有**额外分隔；原生编码里系统提示也是直接 BPE 紧跟 BOS——二者形态应当**对齐**。这正是服务器能从磁盘 KV 恢复会话、再用渲染文本复用前缀的基础（衔接 u2-l3 与 u7-l5）。
5. 把 `--think` 换成 `--nothink` 重做第 3 步推演，确认结尾 token 从 `<think>` 变成 `</think>`，序列其余部分不变。

**交付物**：一张表，两列分别是「原生编码序列」与「渲染回扫序列」的 token 形态（用标记名 + 「BPE(正文)」表示即可，不必写具体 id），并写一句话说明它们为何在理想情况下应当一致。

**预期结论**：两条路径殊途同归——原生编码是「按规则构造」，渲染回扫是「按规则解析」，只要渲染文本严格遵循 DeepSeek 模板，二者产出的 token 序列应当逐位对齐。这也是 ds4 服务器「精确 DSML 回放」与「前缀复用」能成立的前提（详见 u7-l4、u7-l5）。

## 6. 本讲小结

- ds4 的词表是**进程级只读**状态，由 `vocab_load`（ds4.c:22233）从 mmap 的 GGUF 读两张表（`token_to_id`、`merge_rank`）并按名字查出 7 个特殊 token id。
- 分词用 GPT-2 风格**字节级 BPE**：`byte_encode` 把字节映成码点，`bpe_emit_piece` 按 `merge_rank` 贪心合并，预切分由 `bpe_tokenize_text`（ds4.c:22153）手写复刻 JoyAI-LLM 规则。
- 聊天模板由 `encode_chat_prompt`（ds4.c:22285）按 `BOS → (可选 Max 前缀) → 系统提示 → [User] → 正文 → [Assistant] → <think>/</think>` 顺序拼接；控制标记**按 id 直插**，正文才走 BPE。
- thinking 三档（none/high/max）在**分词阶段**就改变序列：开关决定结尾是 `<think>` 还是 `</think>`，Think Max 额外在头部注入超长指令，且上下文不足 393216 时自动降级为 high。
- ds4 有两条编码路径：原生聊天编码（`ds4_encode_chat_prompt`）处理结构化输入，渲染回扫（`tokenize_rendered_chat`，ds4.c:22367）处理「已渲染文本」，二者靠 CLI 判定「是否以 BOS 开头」分流。
- `｜DSML｜`（全角竖线）等特殊标记在回扫路径被 `special_token_at`（ds4.c:22312）当作原子整体识别，不进 BPE；`--dump-tokens` 走回扫路径，在推理前退出，是排查分词问题的第一工具。

## 7. 下一步学习建议

- **进入推理内核**：本讲只到「token 序列生成」为止。下一步读 u4-l1（DeepSeek V4 架构总览），看 token id 如何经 token embedding 进入 transformer 层。embedding 入口可先看 `embed_prompt`（ds4.c:21674）。
- **理解前缀复用如何依赖本讲**：本讲产出的 token 序列是 `ds4_session_sync` 的输入。建议接着读 u2-l3（Session 同步与前缀复用），看 session 如何比较新旧 token 序列决定增量 prefill 还是重建。
- **深入 DSML 与工具调用**：若对 `｜DSML｜` 标记的下游用途感兴趣，跳到 u7-l4（工具调用：DSML、精确回放、规范化）与 u10-l2（Agent 工具系统），看模型吐出的 DSML 文本如何被解析成结构化工具调用。
- **解码侧**：本讲未展开 token → 文本的逆过程，可读 `ds4_token_text`（ds4.c:22515）与 CLI 的 `token_printer`（ds4_cli.c:288 起）了解流式解码与 thinking 内容的着色渲染。
